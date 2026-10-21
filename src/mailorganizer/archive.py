"""Read-only with respect to providers: durable local RFC822 archive."""
from email import policy
from email.parser import BytesParser
import hashlib
import json
from pathlib import Path
import sqlite3
from contextlib import contextmanager
from .documents import atomic_write, save_attachment
from html.parser import HTMLParser
from .triage import classify


class _VisibleHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.chunks = []

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'head'):
            self.hidden += 1
        elif tag in ('p', 'br', 'div', 'li', 'tr') and not self.hidden:
            self.chunks.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'head'):
            self.hidden = max(0, self.hidden - 1)
        elif tag in ('p', 'div', 'li', 'tr') and not self.hidden:
            self.chunks.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.chunks.append(data)


def _body_text(msg):
    body = msg.get_body(preferencelist=('plain', 'html'))
    if body is None:
        return ''
    payload = body.get_payload(decode=True) or b''
    try:
        text = payload.decode(body.get_content_charset() or 'utf-8', errors='replace')
    except LookupError:
        text = payload.decode('utf-8', errors='replace')
    if body.get_content_type() == 'text/html':
        parser = _VisibleHTML()
        parser.feed(text)
        parser.close()
        return ''.join(parser.chunks).strip()
    return text


class Archive:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / 'archive.sqlite3'
        with self._connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS messages (message_key TEXT PRIMARY KEY, record TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS provider_index (account_id TEXT, message_id TEXT, message_key TEXT UNIQUE, PRIMARY KEY(account_id,message_id))')
            for key, text in db.execute('SELECT m.message_key,m.record FROM messages m LEFT JOIN provider_index p ON p.message_key=m.message_key WHERE p.message_key IS NULL').fetchall():
                record=json.loads(text)
                db.execute('INSERT OR IGNORE INTO provider_index VALUES (?,?,?)',(record['account_id'],record['message_id'],key))
        with self._connect() as db:
            completed_keys={row[0] for row in db.execute('SELECT message_key FROM messages')}
        # Identity is published before original; only unknown complete originals are replayed.
        for identity in sorted((self.root / 'messages').glob('*/*/identity.json')):
            if identity.parent.parent.name in completed_keys:
                continue
            original = identity.with_name('original.eml')
            if original.exists():
                info = json.loads(identity.read_text(encoding='utf-8'))
                self.ingest(info['account_id'], info['message_id'], original.read_bytes())

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.database, timeout=30)
        try:
            db.execute('PRAGMA synchronous=FULL')
            with db:
                yield db
        finally:
            db.close()

    def provider_ids(self, account_id):
        with self._connect() as db:
            return {row[0] for row in db.execute('SELECT message_id FROM provider_index WHERE account_id=?',(account_id,))}

    def has(self, account_id, message_id):
        with self._connect() as db:
            return db.execute('SELECT 1 FROM provider_index WHERE account_id=? AND message_id=?',(account_id,message_id)).fetchone() is not None

    def records(self) -> list[dict]:
        with self._connect() as db:
            return [json.loads(row[0]) for row in db.execute('SELECT record FROM messages ORDER BY message_key')]

    def get(self, message_key: str) -> dict | None:
        with self._connect() as db:
            row = db.execute('SELECT record FROM messages WHERE message_key=?', (message_key,)).fetchone()
        return json.loads(row[0]) if row else None

    def ingest(self, account_id: str, message_id: str, raw: bytes) -> dict:
        key = hashlib.sha256(json.dumps([account_id, message_id], ensure_ascii=False).encode()).hexdigest()
        digest = hashlib.sha256(raw).hexdigest()
        old = self.get(key)
        if old:
            if old['raw_sha256'] != digest:
                raise ValueError(f'Archive message conflict: {key}; existing original retained')
            return dict(old, inserted=False)
        directory = self.root / 'messages' / key / digest
        original = directory / 'original.eml'
        atomic_write(directory / 'identity.json', json.dumps(dict(account_id=account_id, message_id=message_id), ensure_ascii=False, sort_keys=True).encode('utf-8'))
        atomic_write(original, raw)
        msg = BytesParser(policy=policy.default).parsebytes(raw)
        text = _body_text(msg)
        subject, sender = str(msg.get('Subject', '')), str(msg.get('From', ''))
        attachments = []
        for part in msg.walk():
            if part.is_multipart() or part.get_content_disposition() == 'inline':
                continue
            if part.get_content_disposition() != 'attachment' and not part.get_filename():
                continue
            attachments.append(save_attachment(directory / 'attachments', len(attachments),
                part.get_filename() or 'attachment', part.get_content_type(),
                part.get_payload(decode=True) or b'', part.get_content_charset() or 'utf-8'))
        manifest_bytes = json.dumps(attachments, ensure_ascii=False, sort_keys=True).encode('utf-8')
        atomic_write(directory / 'attachments.json', manifest_bytes)
        record = dict(message_key=key, account_id=account_id, message_id=message_id,
                      subject=subject, sender=sender, date=str(msg.get('Date', '')),
                      body=text, attachments=attachments, original_path=str(original),
                      raw_sha256=digest, attachment_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                      inserted=True, **classify(subject, text, sender))
        with self._connect() as db:
            db.execute('INSERT INTO messages VALUES (?, ?)', (key, json.dumps(record, ensure_ascii=False)))
            db.execute('INSERT OR IGNORE INTO provider_index VALUES (?,?,?)',(account_id,message_id,key))
        return record
