from pathlib import Path
import pytest
from mailorganizer.archive import Archive
from mailorganizer.connectors import ConnectorError


class SyntheticReader:
    def __init__(self, ids, email='synthetic@example.test', page_size=70):
        self.ids = list(ids)
        self.email = email
        self.page_size = page_size
        self.raw_calls = []
        self.list_calls = []
        self.history_calls = []
        self.history = {'historyId': 'opaque-final'}
    def get_profile(self):
        return {'emailAddress': self.email, 'historyId': 'opaque-baseline'}
    def list_page(self, **kwargs):
        self.list_calls.append(kwargs)
        start = int(kwargs.get('page_token') or 0)
        end = start + self.page_size
        result = {'messages': [{'id': mid} for mid in self.ids[start:end]], 'resultSizeEstimate': 99999}
        if end < len(self.ids):
            result['nextPageToken'] = str(end)
        return result
    def get_raw(self, mid):
        self.raw_calls.append(mid)
        return {'id': mid, 'raw': b'Subject: synthetic\r\n\r\nSynthetic only', 'labelIds': ['INBOX']}
    def get_metadata(self, mid):
        return {'id': mid, 'threadId': 'thread-' + mid, 'internalDate': '123', 'labelIds': ['INBOX']}
    def history_page(self, start_history_id, page_token=None):
        self.history_calls.append((start_history_id, page_token))
        return self.history


def sync(root, reader, account='gmail1', **kwargs):
    from mailorganizer.gmail_sync import GmailSync
    return GmailSync(root, account, reader, **kwargs)


def test_pilot_original_enriched_without_raw_or_manifest_mutation(tmp_path):
    reader = SyntheticReader(['pilot'])
    archive = Archive(tmp_path)
    archive.ingest('gmail1', 'pilot', b'Subject: pilot\r\n\r\nOriginal')
    before = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file() and p.suffix not in ('.sqlite3',)}
    calls = []
    get_metadata = reader.get_metadata
    reader.get_metadata = lambda mid: (calls.append(mid) or get_metadata(mid))
    s = sync(tmp_path, reader)
    result = s.step(max_messages=1)
    assert result['full_mailbox_verified']
    assert reader.raw_calls == [] and calls == ['pilot']
    assert s.message_metadata()['pilot'] == dict(get_metadata('pilot'), source_deleted=False)
    for relative, content in before.items():
        assert (tmp_path / relative).read_bytes() == content
    assert sync(tmp_path, reader).step()['full_mailbox_verified']
    assert calls == ['pilot']


def test_disk_guard_blocks_without_attempt_and_resumes(tmp_path):
    import sqlite3
    reader = SyntheticReader(['a'])
    free = [0]
    s = sync(tmp_path, reader, min_free_bytes=100, disk_free=lambda root: free[0])
    result = s.step()
    assert result['state'] == 'disk_space_blocked'
    assert not result['full_mailbox_verified']
    assert reader.list_calls == [] and reader.raw_calls == []
    free[0] = 10000
    get_raw = reader.get_raw
    def fill_disk(mid):
        result = get_raw(mid)
        free[0] = 0
        return result
    reader.get_raw = fill_disk
    assert s.step()['state'] == 'disk_space_blocked'
    with sqlite3.connect(s.database) as db:
        assert db.execute('SELECT phase, attempts FROM queue').fetchone() == ('pending', 0)
    assert Archive(tmp_path).records() == []
    free[0] = 10000
    reader.get_raw = get_raw
    assert sync(tmp_path, reader, min_free_bytes=100, disk_free=lambda root: free[0]).step()['full_mailbox_verified']


@pytest.mark.parametrize('value', ['false', 0, 1, None, []])
def test_scope_requires_actual_boolean(tmp_path, value):
    with pytest.raises(ValueError):
        sync(tmp_path, SyntheticReader([]), include_spam_trash=value)


def test_scope_cannot_change_after_restart(tmp_path):
    reader = SyntheticReader([])
    sync(tmp_path, reader, query='in:inbox').step()
    reader.list_calls.clear()
    result = sync(tmp_path, reader).step()
    assert result['state'] == 'needs_attention'
    assert not result['full_mailbox_verified']
    assert reader.list_calls == []


@pytest.mark.parametrize('duplicate', [False, True])
def test_pilot_registry_identity_rejected_before_enumeration(tmp_path, duplicate):
    import json
    import hashlib
    reader = SyntheticReader(['secret'])
    email = reader.email if duplicate else 'changed@example.test'
    digest = hashlib.sha256(('gmail:' + email.casefold()).encode()).hexdigest()
    (tmp_path / 'account_registry.json').write_text(json.dumps({
        'gmail2' if duplicate else 'gmail1': {'provider': 'gmail', 'identity_sha256': digest}
    }))
    result = sync(tmp_path, reader).step()
    assert result['state'] == 'needs_attention'
    assert reader.list_calls == [] and reader.raw_calls == []
    assert result['reason'] == 'identity_mismatch'


def test_connections_close_after_transaction(tmp_path):
    import sqlite3
    s = sync(tmp_path, SyntheticReader([]))
    with s._connect() as db:
        db.execute('SELECT 1')
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        db.execute('SELECT 1')


def test_durable_catalog_more_than_100_per_step_and_no_refetch(tmp_path):
    reader = SyntheticReader([str(n) for n in range(155)] + ['0'])
    a = Archive(tmp_path)
    a.ingest('gmail1', '0', reader.get_raw('0')['raw'])
    reader.raw_calls.clear()
    first = sync(tmp_path, reader).step(max_messages=40, max_pages=1)
    assert first['fetched_this_step'] == 40
    assert not first['catalog_complete']
    assert not first['full_mailbox_verified']
    for _ in range(5):
        last = sync(tmp_path, reader).step(max_messages=40, max_pages=1)
    assert last['completed'] == 155
    assert last['pending'] == 0
    assert last['full_mailbox_verified']
    assert len(reader.raw_calls) == 154
    assert len(set(reader.raw_calls)) == 154
    assert all(c['include_spam_trash'] and c['query'] == '' for c in reader.list_calls)
    assert reader.history_calls[0][0] == 'opaque-baseline'
    sync(tmp_path, reader).step()
    assert len(reader.raw_calls) == 154


def test_account_identity_and_slots_isolated(tmp_path):
    one = SyntheticReader(['same'])
    two = SyntheticReader(['same'], email='other@example.test')
    assert sync(tmp_path, one).step()['completed'] == 1
    assert sync(tmp_path, two, 'gmail2').step()['completed'] == 1
    assert len(Archive(tmp_path).records()) == 2
    bad = sync(tmp_path, one, 'gmail3').step()
    assert bad['state'] == 'needs_attention'
    assert not bad['full_mailbox_verified']
    changed = sync(tmp_path, two).step()
    assert changed['state'] == 'needs_attention'
    assert not changed['full_mailbox_verified']


def test_history_add_labels_delete_atomic_cursor_and_no_original_deletion(tmp_path):
    reader = SyntheticReader(['old', 'labelled'])
    s = sync(tmp_path, reader)
    assert s.step()['full_mailbox_verified']
    before = {r['message_id']: Path(r['original_path']) for r in Archive(tmp_path).records()}
    reader.history = {'historyId': 'opaque-intermediate', 'nextPageToken': 'history-page-2', 'history': [
        {'id': 'event-a', 'messages': [{'id': 'incoming'}, {'id': 'old'}],
         'messagesAdded': [{'message': {'id': 'incoming', 'labelIds': ['INBOX']}}],
         'messagesDeleted': [{'message': {'id': 'old'}}],
         'labelsRemoved': [{'message': {'id': 'labelled'}, 'labelIds': ['INBOX']}],
         'labelsAdded': [{'message': {'id': 'labelled'}, 'labelIds': ['STARRED']}]}
    ]}
    pending = s.step(max_pages=1)
    assert not pending['full_mailbox_verified']
    assert pending['completed'] == 2 and pending['deleted'] == 1
    assert sorted(reader.raw_calls) == ['incoming', 'labelled', 'old']
    assert s.message_metadata()['labelled']['labelIds'] == ['STARRED']
    assert s.message_metadata()['old']['source_deleted']
    assert before['old'].exists()
    reader.history = {'historyId': 'opaque-end'}
    assert sync(tmp_path, reader).step(max_pages=1)['full_mailbox_verified']
    assert reader.history_calls[-1] == ('opaque-final', 'history-page-2')
    sync(tmp_path, reader).step()
    assert reader.history_calls[-1] == ('opaque-end', None)


def test_no_full_verification_for_filtered_scope(tmp_path):
    assert not sync(tmp_path, SyntheticReader([]), query='in:inbox').step()['full_mailbox_verified']


def test_one_bad_message_and_raw_404_do_not_block_others(tmp_path):
    reader = SyntheticReader(['bad', 'gone', 'good'])
    original = reader.get_raw
    def get_raw(mid):
        if mid == 'bad':
            raise ValueError('SECRET body')
        if mid == 'gone':
            raise ConnectorError('gone', http_status=404)
        return original(mid)
    reader.get_raw = get_raw
    first = sync(tmp_path, reader).step()
    assert first['completed'] == 1 and first['failed'] == 1 and first['deleted'] == 1
    assert first['fetched_this_step'] == 3
    assert not first['full_mailbox_verified']
    assert 'SECRET' not in (tmp_path / 'gmail_sync.sqlite3').read_bytes().decode('latin1')
    reader.get_raw = original
    assert sync(tmp_path, reader).step()['failed'] == 1  # cooldown
    import sqlite3
    with sqlite3.connect(tmp_path / 'gmail_sync.sqlite3') as db:
        db.execute('UPDATE queue SET retry_at=0')
    assert sync(tmp_path, reader).step()['full_mailbox_verified']


def test_resume_after_ingest_committed_but_queue_commit_killed(tmp_path, monkeypatch):
    reader = SyntheticReader(['a', 'b'])
    ingest = Archive.ingest
    killed = False
    def crash(self, account, mid, raw):
        nonlocal killed
        result = ingest(self, account, mid, raw)
        if not killed:
            killed = True
            raise KeyboardInterrupt('synthetic crash')
        return result
    monkeypatch.setattr(Archive, 'ingest', crash)
    with pytest.raises(KeyboardInterrupt):
        sync(tmp_path, reader).step(workers=1)
    downloaded = list(reader.raw_calls)
    monkeypatch.setattr(Archive, 'ingest', ingest)
    assert sync(tmp_path, reader).step(workers=1)['full_mailbox_verified']
    assert reader.raw_calls.count(downloaded[0]) == 1


def test_expired_history_rescans_without_deleting_originals_or_refetch(tmp_path):
    reader = SyntheticReader(['old', 'kept'])
    assert sync(tmp_path, reader).step()['full_mailbox_verified']
    originals = [Path(r['original_path']) for r in Archive(tmp_path).records()]
    history = reader.history_page
    def expired(*args, **kwargs):
        reader.history_page = history
        raise ConnectorError('expired', http_status=404)
    reader.history_page = expired
    reader.ids = ['kept', 'new']
    recovery = sync(tmp_path, reader).step(max_pages=1)
    assert recovery['state'] == 'backfill_pending'
    assert not recovery['catalog_complete']
    assert recovery['deleted'] == 0
    final = sync(tmp_path, reader).step()
    assert final['full_mailbox_verified']
    assert final['deleted'] == 1 and final['completed'] == 2
    assert all(path.exists() for path in originals)
    assert reader.raw_calls.count('kept') == 1
    assert reader.history_calls[-1][0] == 'opaque-baseline'


def test_expired_list_page_restart_retains_completed_and_no_early_deletes(tmp_path):
    reader = SyntheticReader(['a', 'b', 'c'], page_size=1)
    assert sync(tmp_path, reader).step(max_pages=1)['completed'] == 1
    original = reader.list_page
    def expired(**kwargs):
        reader.list_page = original
        raise ConnectorError('expired', http_status=400)
    reader.list_page = expired
    reader.ids = ['b', 'c']
    first = sync(tmp_path, reader).step(max_pages=1)
    assert first['state'] == 'backfill_pending'
    assert first['deleted'] == 0
    assert not first['full_mailbox_verified']
    assert sync(tmp_path, reader).step(max_pages=1)['deleted'] == 0
    final = sync(tmp_path, reader).step()
    assert final['full_mailbox_verified'] and final['deleted'] == 1
    assert reader.raw_calls.count('a') == 1


def test_repeated_catalog_token_fails_closed(tmp_path):
    reader = SyntheticReader(['a', 'b'], page_size=1)
    s = sync(tmp_path, reader)
    s.step(max_pages=1)
    def repeated(**kwargs):
        return {'messages': [{'id': 'bad-uncommitted'}], 'nextPageToken': '1'}
    reader.list_page = repeated
    result = s.step(max_pages=2)
    assert result['state'] == 'needs_attention'
    assert not result['full_mailbox_verified']
    assert 'bad-uncommitted' not in s.message_metadata()


def test_history_transaction_rolls_back_events_and_cursor_on_bad_page(tmp_path):
    reader = SyntheticReader(['old'])
    s = sync(tmp_path, reader)
    s.step()
    reader.history = {'historyId': 'should-not-advance', 'history': [
        {'messagesDeleted': [{'message': {'id': 'old'}}]},
        {'messagesAdded': [{'message': {'id': ''}}]}
    ]}
    assert s.step()['state'] == 'needs_attention'
    assert not s.message_metadata()['old']['source_deleted']
    reader.history = {'historyId': 'next-good'}
    assert sync(tmp_path, reader).step()['full_mailbox_verified']
    assert reader.history_calls[-1] == ('opaque-final', None)


def test_unknown_label_event_does_not_download_raw_and_blocks_full_claim(tmp_path):
    reader = SyntheticReader([])
    s = sync(tmp_path, reader)
    s.step()
    reader.history = {'historyId': 'unknown-label-cursor', 'history': [
        {'labelsAdded': [{'message': {'id': 'unknown'}, 'labelIds': ['STARRED']}]}
    ]}
    result = s.step()
    assert reader.raw_calls == []
    assert not result['full_mailbox_verified']
    assert result['pending'] == 1
    assert s.message_metadata()['unknown']['labelIds'] == ['STARRED']


def test_catalog_cursor_and_ids_roll_back_together_on_bad_page(tmp_path):
    reader = SyntheticReader(['a'])
    original = reader.list_page
    reader.list_page = lambda **kwargs: {'messages': [{'id': 'uncommitted'}, {'id': ''}], 'nextPageToken': 'bad-token'}
    s = sync(tmp_path, reader)
    assert s.step()['state'] == 'needs_attention'
    assert s.message_metadata() == {}
    reader.list_page = original
    assert sync(tmp_path, reader).step()['full_mailbox_verified']
    assert reader.list_calls[0]['page_token'] is None


def test_pending_guard_and_incoming_during_backfill(tmp_path):
    reader = SyntheticReader(['a', 'b'])
    reader.history = {'historyId': 'incoming-end', 'history': [
        {'messagesAdded': [{'message': {'id': 'incoming'}}]}
    ]}
    first = sync(tmp_path, reader).step(max_messages=1)
    assert first['catalog_complete'] and first['history_caught_up']
    assert first['pending'] == 2 and not first['full_mailbox_verified']
    assert sync(tmp_path, reader).step()['full_mailbox_verified']
    assert sorted(reader.raw_calls) == ['a', 'b', 'incoming']


def test_label_deltas_before_raw_preserve_unaffected_labels(tmp_path):
    reader = SyntheticReader(['pending'])
    reader.history = {'historyId': 'labels-end', 'history': [{
        'labelsAdded': [{'message': {'id': 'pending'}, 'labelIds': ['STARRED']}],
        'labelsRemoved': [{'message': {'id': 'pending'}, 'labelIds': ['INBOX']}]
    }]}
    raw = reader.get_raw
    def labelled(mid):
        return dict(raw(mid), labelIds=['INBOX', 'UNREAD'])
    reader.get_raw = labelled
    s = sync(tmp_path, reader)
    assert s.step()['full_mailbox_verified']
    assert set(s.message_metadata()['pending']['labelIds']) == {'UNREAD', 'STARRED'}


def test_raw_workers_capped_and_ingest_serialized(tmp_path, monkeypatch):
    import threading
    import time
    reader = SyntheticReader([str(i) for i in range(12)])
    lock = threading.Lock()
    active = peak = 0
    get_raw = reader.get_raw
    def delayed(mid):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.015)
            return get_raw(mid)
        finally:
            with lock:
                active -= 1
    reader.get_raw = delayed
    ingest = Archive.ingest
    threads = []
    def serial(self, *args, **kwargs):
        threads.append(threading.get_ident())
        return ingest(self, *args, **kwargs)
    monkeypatch.setattr(Archive, 'ingest', serial)
    first = sync(tmp_path, reader).step(max_messages=5, workers=99)
    assert first['fetched_this_step'] == 5 and first['pending'] == 7
    assert 1 <= peak <= 4
    assert set(threads) == {threading.get_ident()}


def test_repeated_history_token_does_not_commit_event_or_advance(tmp_path):
    reader = SyntheticReader(['old'])
    s = sync(tmp_path, reader)
    s.step()
    reader.history = {'historyId': 'intermediate', 'nextPageToken': 'same'}
    assert not s.step(max_pages=1)['full_mailbox_verified']
    reader.history = {'historyId': 'bad-cursor', 'nextPageToken': 'same', 'history': [
        {'messagesDeleted': [{'message': {'id': 'old'}}]}
    ]}
    assert sync(tmp_path, reader).step()['state'] == 'needs_attention'
    assert not s.message_metadata()['old']['source_deleted']
    reader.history = {'historyId': 'final-cursor'}
    assert sync(tmp_path, reader).step()['full_mailbox_verified']
    assert reader.history_calls[-1] == ('opaque-final', 'same')


def test_failed_download_attempts_bounded_and_never_fake_completed(tmp_path):
    import sqlite3
    reader = SyntheticReader(['bad'])
    calls = []
    def bad(mid):
        calls.append(mid)
        raise ValueError('synthetic malformed')
    reader.get_raw = bad
    for _ in range(7):
        result = sync(tmp_path, reader).step()
        assert result['failed'] == 1 and not result['full_mailbox_verified']
        with sqlite3.connect(tmp_path / 'gmail_sync.sqlite3') as db:
            db.execute('UPDATE queue SET retry_at=0')
    assert len(calls) == 5
    assert result['completed'] == 0






