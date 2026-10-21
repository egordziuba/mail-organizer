"""Resumable, GET-only Gmail catalog and immutable original backfill.

The caller must hold its root-wide process mutex. SQLite commits are serialized;
only network RAW requests run in worker threads. No credentials are persisted.
History cursors are opaque, and are advanced only at the end of a history run.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import shutil
import errno
import time

from .archive import Archive
from .connectors import ConnectorError


class DiskSpaceBlocked(RuntimeError):
    """No download/ingest retries are charged for local disk pressure."""


class GmailSync:
    def __init__(self, root: Path, account_id: str, reader, *, include_spam_trash=True, query='',
                 min_free_bytes=1024 ** 3, disk_free=None):
        if type(min_free_bytes) is not int or min_free_bytes < 0:
            raise ValueError('Invalid disk reserve')
        self.min_free_bytes = min_free_bytes
        self._disk_free = disk_free or (lambda path: shutil.disk_usage(path).free)
        if not isinstance(account_id, str) or not account_id or not isinstance(query, str) or type(include_spam_trash) is not bool:
            raise ValueError('Invalid sync configuration')
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.account_id = account_id
        self.reader = reader
        self.query = query
        self.include_spam_trash = bool(include_spam_trash)
        self.database = self.root / 'gmail_sync.sqlite3'
        self._archive = None
        with self._connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS slots (
                    account TEXT PRIMARY KEY, identity TEXT UNIQUE, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS queue (
                    account TEXT NOT NULL, mid TEXT NOT NULL, phase TEXT NOT NULL,
                    generation INTEGER NOT NULL, metadata TEXT NOT NULL DEFAULT '{}',
                    attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
                    reason TEXT, PRIMARY KEY(account, mid));
            ''')
            db.execute('INSERT OR IGNORE INTO slots VALUES (?, NULL, ?)',
                       (account_id, json.dumps(self._initial())))

    def _check_disk(self, write_bytes=0):
        if self._disk_free(self.root) < self.min_free_bytes + write_bytes:
            raise DiskSpaceBlocked('disk_space_blocked')

    def _initial(self):
        return dict(catalog_complete=False, history_caught_up=False, baseline=None,
                    cursor=None, list_token=None, history_token=None, list_seen=[],
                    history_seen=[], generation=1, error=None, query=self.query,
                    include_spam_trash=self.include_spam_trash)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.database, timeout=30)
        try:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA synchronous=FULL')
            with db:
                yield db
        finally:
            db.close()

    def _load(self, db):
        return json.loads(db.execute('SELECT state FROM slots WHERE account=?',
                                   (self.account_id,)).fetchone()[0])

    def _save(self, db, state):
        db.execute('UPDATE slots SET state=? WHERE account=?',
                   (json.dumps(state), self.account_id))

    def _bind_identity(self, db, email):
        normalized = email.strip().casefold()
        identity = hashlib.sha256(('gmail:' + normalized).encode('utf-8')).hexdigest()
        legacy = hashlib.sha256(normalized.encode('utf-8')).hexdigest()
        registry_path = self.root / 'account_registry.json'
        registry = json.loads(registry_path.read_text(encoding='utf-8')) if registry_path.exists() else {}
        if not isinstance(registry, dict):
            raise ConnectorError('identity_mismatch')
        for account, entry in registry.items():
            if not isinstance(entry, dict) or not isinstance(entry.get('identity_sha256'), str):
                raise ConnectorError('identity_mismatch')
            digest = entry['identity_sha256']
            if (account == self.account_id and digest != identity) or (account != self.account_id and digest == identity):
                raise ConnectorError('identity_mismatch')
        for row in db.execute('SELECT account, identity FROM slots WHERE identity IS NOT NULL'):
            if (row['account'] == self.account_id and row['identity'] not in (identity, legacy)) or (row['account'] != self.account_id and row['identity'] in (identity, legacy)):
                raise ConnectorError('identity_mismatch')
        return identity

    def _existing(self):
        if self._archive is None:
            self._archive = Archive(self.root)
        if hasattr(self._archive, 'provider_ids'):
            return set(self._archive.provider_ids(self.account_id))
        return {r['message_id'] for r in self._archive.records() if r['account_id'] == self.account_id}

    def _enqueue(self, db, state, mid, existing):
        if not isinstance(mid, str) or not mid:
            raise ConnectorError('Invalid provider message ID')
        phase = 'metadata_pending' if mid in existing else 'pending'
        previous=db.execute('SELECT generation,phase FROM queue WHERE account=? AND mid=?',(self.account_id,mid)).fetchone()
        db.execute('''INSERT INTO queue(account, mid, phase, generation) VALUES (?, ?, ?, ?)
                      ON CONFLICT(account, mid) DO UPDATE SET generation=excluded.generation''',
                   (self.account_id, mid, phase, state['generation']))
        if previous and previous['generation'] != state['generation'] and mid in existing:
            db.execute("UPDATE queue SET phase='metadata_pending',attempts=0,retry_at=0 WHERE account=? AND mid=?",(self.account_id,mid))

    def _rescan(self, db, state, baseline):
        state.update(catalog_complete=False, history_caught_up=False, baseline=baseline,
                     cursor=None, list_token=None, history_token=None,
                     list_seen=[], history_seen=[], generation=state['generation'] + 1,
                     error=None, rescan_requested=False)
        with db:
            self._save(db, state)

    @staticmethod
    def _next_token(data, seen):
        token = data.get('nextPageToken')
        if token is not None and (not isinstance(token, str) or not token or token in seen):
            raise ConnectorError('Invalid provider pagination')
        return token

    def _catalog_page(self, db, state, existing):
        data = self.reader.list_page(query=self.query, page_token=state['list_token'],
                                     include_spam_trash=self.include_spam_trash, page_size=500)
        messages = data.get('messages', [])
        if not isinstance(messages, list):
            raise ConnectorError('Invalid provider message list')
        token = self._next_token(data, state['list_seen'])
        with db:
            for message in messages:
                if not isinstance(message, dict):
                    raise ConnectorError('Invalid provider message ID')
                self._enqueue(db, state, message.get('id'), existing)
                mid = message['id']
                db.execute("UPDATE queue SET phase=? WHERE account=? AND mid=? AND phase IN ('deleted', 'metadata_only')",
                           ('completed' if mid in existing else 'pending', self.account_id, mid))
            state['list_token'] = token
            if token:
                state['list_seen'].append(token)
            else:
                state['catalog_complete'] = True
                # Reconciliation happens only after the complete generation.
                # This is a source tombstone; local originals are never removed.
                db.execute("UPDATE queue SET phase='deleted', reason='absent_after_rescan' WHERE account=? AND generation<>?",
                           (self.account_id, state['generation']))
            estimate = data.get('resultSizeEstimate')
            if type(estimate) is int and estimate >= 0:
                state['estimated_size'] = estimate
            self._save(db, state)

    def _history_page(self, db, state, existing):
        data = self.reader.history_page(state['cursor'] or state['baseline'],
                                        page_token=state['history_token'])
        token = self._next_token(data, state['history_seen'])
        cursor = data.get('historyId')
        if not isinstance(cursor, str) or not cursor:
            raise ConnectorError('Invalid provider history cursor')
        history = data.get('history', [])
        if not isinstance(history, list):
            raise ConnectorError('Invalid provider history')
        with db:
            for event in history:
                if not isinstance(event, dict):
                    raise ConnectorError('Invalid provider history')
                # Generic messages are deliberately ignored: Gmail's typed events
                # are authoritative and generic entries are duplicates.
                for kind in ('messagesAdded', 'messagesDeleted', 'labelsRemoved', 'labelsAdded'):
                    changes = event.get(kind, [])
                    if not isinstance(changes, list):
                        raise ConnectorError('Invalid provider history')
                    for change in changes:
                        if not isinstance(change, dict) or not isinstance(change.get('message'), dict):
                            raise ConnectorError('Invalid provider history')
                        message = change['message']
                        mid = message.get('id')
                        previous = db.execute('SELECT phase FROM queue WHERE account=? AND mid=?',
                                              (self.account_id, mid)).fetchone()
                        self._enqueue(db, state, mid, existing)
                        if not previous and mid not in existing and kind in ('labelsAdded', 'labelsRemoved'):
                            # Label changes never initiate MIME downloads. An ID
                            # missing from the catalog requires a new enumeration.
                            db.execute("UPDATE queue SET phase='metadata_only' WHERE account=? AND mid=?", (self.account_id, mid))
                            state['rescan_requested'] = True
                        row = db.execute('SELECT phase, metadata FROM queue WHERE account=? AND mid=?',
                                         (self.account_id, mid)).fetchone()
                        metadata = json.loads(row['metadata'])
                        metadata.update({k: message[k] for k in ('id', 'threadId', 'internalDate', 'historyId', 'sizeEstimate') if k in message})
                        if kind == 'messagesAdded':
                            phase = 'completed' if mid in existing else 'pending'
                            if 'labelIds' in message:
                                metadata['labelIds'] = self._labels(message['labelIds'])
                                for key in ('_labels_partial', '_label_adds', '_label_removes'):
                                    metadata.pop(key, None)
                            db.execute('UPDATE queue SET phase=?, attempts=0, retry_at=0, reason=NULL WHERE account=? AND mid=?',
                                       (phase, self.account_id, mid))
                        elif kind == 'messagesDeleted':
                            db.execute("UPDATE queue SET phase='deleted', reason=NULL WHERE account=? AND mid=?", (self.account_id, mid))
                        else:
                            partial = metadata.get('_labels_partial') or 'labelIds' not in metadata
                            labels = set(self._labels(metadata.get('labelIds', [])))
                            changed = set(self._labels(change.get('labelIds', [])))
                            labels = labels | changed if kind == 'labelsAdded' else labels - changed
                            metadata['labelIds'] = sorted(labels)
                            if partial:
                                adds = set(metadata.get('_label_adds', []))
                                removes = set(metadata.get('_label_removes', []))
                                if kind == 'labelsAdded':
                                    adds |= changed
                                    removes -= changed
                                else:
                                    adds -= changed
                                    removes |= changed
                                metadata.update(_labels_partial=True, _label_adds=sorted(adds),
                                                _label_removes=sorted(removes))
                        db.execute('UPDATE queue SET metadata=? WHERE account=? AND mid=?',
                                   (json.dumps(metadata), self.account_id, mid))
            state['history_token'] = token
            if token:
                state['history_seen'].append(token)
            else:
                state['cursor'] = cursor
                state['history_seen'] = []
                state['history_caught_up'] = True
            self._save(db, state)

    @staticmethod
    def _labels(value):
        if not isinstance(value, list) or any(not isinstance(label, str) for label in value):
            raise ConnectorError('Invalid provider labels')
        return value

    def step(self, max_messages=200, max_pages=20, time_budget_seconds=120, workers=4) -> dict:
        if type(max_messages) is not int or max_messages <= 0 or type(max_pages) is not int or max_pages <= 0:
            raise ValueError('Step limits must be positive integers')
        if type(workers) is not int or workers <= 0 or time_budget_seconds <= 0:
            raise ValueError('Invalid step budget')
        deadline = time.monotonic() + time_budget_seconds
        fetched = inserted = 0
        with self._connect() as db:
            state = self._load(db)
            try:
                self._check_disk()
                profile = self.reader.get_profile()
                email = profile.get('emailAddress')
                baseline = profile.get('historyId')
                if not isinstance(email, str) or not email.strip() or not isinstance(baseline, str) or not baseline:
                    raise ConnectorError('Invalid provider profile')
                identity = self._bind_identity(db, email)
                if state['query'] != self.query or state['include_spam_trash'] != self.include_spam_trash:
                    raise ConnectorError('Sync scope changed')
                with db:
                    db.execute('UPDATE slots SET identity=? WHERE account=?', (identity, self.account_id))
                    state['error'] = None
                    state['history_caught_up'] = False
                    if not state['baseline']:
                        state['baseline'] = baseline
                    self._save(db, state)
                if state.get('rescan_requested'):
                    self._rescan(db, state, baseline)
                existing = self._existing()
                for _ in range(max_pages):
                    if time.monotonic() >= deadline:
                        break
                    catalog = not state['catalog_complete']
                    try:
                        if catalog:
                            self._catalog_page(db, state, existing)
                        else:
                            self._history_page(db, state, existing)
                            if state['history_caught_up']:
                                break
                    except ConnectorError as exc:
                        if (catalog and state['list_token'] and exc.http_status == 400) or (not catalog and exc.http_status == 404):
                            # The profile was sampled BEFORE this enumeration.
                            self._rescan(db, state, baseline)
                        else:
                            raise
                fetched, inserted = self._fetch(db, existing, max_messages, min(4, workers), deadline)
            except Exception as exc:
                # Only a closed enum / numeric HTTP status may reach persistent state.
                with db:
                    safe = self._load(db)
                    safe['error'] = ('disk_space_blocked' if isinstance(exc, DiskSpaceBlocked) or (isinstance(exc, OSError) and exc.errno == errno.ENOSPC)
                                     else 'identity_mismatch' if isinstance(exc, ConnectorError) and str(exc) == 'identity_mismatch' else 'sync_failed')
                    safe['history_caught_up'] = False
                    self._save(db, safe)
        return dict(self.status(), fetched_this_step=fetched, inserted_this_step=inserted)

    def _fetch(self, db, existing, limit, workers, deadline):
        fetched = inserted = 0
        while fetched < limit and time.monotonic() < deadline:
            self._check_disk()
            rows = db.execute("SELECT mid,phase FROM queue WHERE account=? AND (phase IN ('pending','metadata_pending') OR (phase IN ('failed','metadata_failed') AND attempts<5 AND retry_at<=?)) LIMIT ?",
                              (self.account_id, time.time(), min(workers, limit-fetched))).fetchall()
            if not rows:
                break
            ids = [r['mid'] for r in rows]
            for mid in list(ids):
                if mid in existing and next(r['phase'] for r in rows if r['mid']==mid) not in ('metadata_pending','metadata_failed'):
                    with db:
                        db.execute("UPDATE queue SET phase='completed' WHERE account=? AND mid=?", (self.account_id, mid))
                    ids.remove(mid)
            if not ids:
                continue
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [(mid, pool.submit(self.reader.get_metadata if next(r['phase'] for r in rows if r['mid']==mid) in ('metadata_pending','metadata_failed') else self.reader.get_raw, mid)) for mid in ids]
                for mid, future in futures:
                    fetched += 1
                    metadata_only=next(r['phase'] for r in rows if r['mid']==mid) in ('metadata_pending','metadata_failed')
                    try:
                        result = future.result()
                        metadata_only = next(r['phase'] for r in rows if r['mid']==mid) in ('metadata_pending','metadata_failed')
                        if not isinstance(result, dict) or (not metadata_only and not isinstance(result.get('raw'), bytes)):
                            raise ConnectorError('Invalid provider MIME response')
                        if result.get('id', mid) != mid:
                            raise ConnectorError('Invalid provider message identity')
                        metadata = {k: result[k] for k in ('id', 'threadId', 'labelIds', 'internalDate', 'historyId', 'sizeEstimate') if k in result}
                        if 'labelIds' in metadata:
                            self._labels(metadata['labelIds'])
                        # Validate metadata before publishing the immutable original.
                        json.dumps(metadata)
                        if not metadata_only:
                            self._check_disk(len(result['raw']) * 3)
                            record = self._archive.ingest(self.account_id, mid, result['raw'])
                            inserted += bool(record.get('inserted'))
                            existing.add(mid)
                        old_metadata = json.loads(db.execute('SELECT metadata FROM queue WHERE account=? AND mid=?',
                                                            (self.account_id, mid)).fetchone()[0])
                        # A real Gmail snapshot has historyId; it wins prior history data.
                        # A synthetic/minimal provider without that marker retains explicit deltas.
                        if not metadata.get('historyId') and old_metadata.get('_labels_partial'):
                            labels=set(metadata.get('labelIds',[]))
                            labels |= set(old_metadata.get('_label_adds',[]))
                            labels -= set(old_metadata.get('_label_removes',[]))
                            metadata['labelIds']=sorted(labels)
                        with db:
                            db.execute("UPDATE queue SET phase='completed', metadata=?, reason=NULL WHERE account=? AND mid=?",
                                       (json.dumps(metadata), self.account_id, mid))
                    except Exception as exc:
                        if isinstance(exc, DiskSpaceBlocked) or (isinstance(exc, OSError) and exc.errno == errno.ENOSPC):
                            raise DiskSpaceBlocked('disk_space_blocked') from None
                        gone = isinstance(exc, ConnectorError) and exc.http_status == 404
                        with db:
                            attempts = db.execute('SELECT attempts FROM queue WHERE account=? AND mid=?',
                                                  (self.account_id, mid)).fetchone()[0] + 1
                            db.execute('UPDATE queue SET phase=?, attempts=?, retry_at=?, reason=? WHERE account=? AND mid=?',
                                       ('deleted' if gone else 'metadata_failed' if metadata_only else 'failed', attempts,
                                        time.time() + min(3600, 30 * 2 ** min(attempts-1, 7)),
                                        'source_gone' if gone else ('http_'+str(exc.http_status) if isinstance(exc,ConnectorError) and exc.http_status else 'local_'+type(exc).__name__), self.account_id, mid))
        return fetched, inserted

    def status(self) -> dict:
        with self._connect() as db:
            state = self._load(db)
            counts = {r[0]: r[1] for r in db.execute('SELECT phase, COUNT(*) FROM queue WHERE account=? GROUP BY phase', (self.account_id,))}
        pending, failed = counts.get('pending', 0) + counts.get('metadata_only', 0) + counts.get('metadata_pending',0), counts.get('failed', 0)+counts.get('metadata_failed',0)
        complete = state['catalog_complete'] and state['history_caught_up'] and not pending and not failed and not state['error']
        result = dict(status='gmail_sync_step_finished', state='disk_space_blocked' if state['error'] == 'disk_space_blocked' else 'needs_attention' if state['error'] or failed else ('synced' if complete else 'backfill_pending'),
                      catalog_complete=state['catalog_complete'], pending=pending,
                      completed=counts.get('completed', 0), deleted=counts.get('deleted', 0), failed=failed,
                      fetched_this_step=0, inserted_this_step=0,
                      full_mailbox_verified=bool(complete and not state['query'] and state['include_spam_trash']),
                      history_caught_up=state['history_caught_up'])
        if 'estimated_size' in state:
            result.update(estimated_size=state['estimated_size'], estimated_size_is_estimate=True)
        if state['error']:
            result['reason'] = state['error']
        return result

    def message_metadata(self) -> dict:
        with self._connect() as db:
            return {r['mid']: dict({k: v for k, v in json.loads(r['metadata']).items() if not k.startswith('_')}, source_deleted=r['phase'] == 'deleted')
                    for r in db.execute('SELECT mid, metadata, phase FROM queue WHERE account=?', (self.account_id,))}
