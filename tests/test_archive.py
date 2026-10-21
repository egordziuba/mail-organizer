"""SYNTHETIC email fixtures only; no credentials/network."""
from email.message import EmailMessage
from pathlib import Path
import hashlib
import pytest


def synthetic(body='We received your application', subject='SYNTHETIC Bewerbung'):
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = 'Synthetic Sender <sender@example.invalid>'
    msg['Date'] = 'Thu, 01 Jan 2026 12:00:00 +0000'
    msg.set_content(body)
    return msg


def test_crash_after_original_is_replayed_on_reopen(tmp_path, monkeypatch):
    import mailorganizer.archive as module
    archive = module.Archive(tmp_path)
    raw = synthetic('SYNTHETIC durable orphan').as_bytes()
    real_write = module.atomic_write
    def fail_manifest(path, data):
        if path.name == 'attachments.json':
            raise OSError('SYNTHETIC simulated crash before DB commit')
        real_write(path, data)
    with monkeypatch.context() as patch:
        patch.setattr(module, 'atomic_write', fail_manifest)
        with pytest.raises(OSError, match='simulated crash'):
            archive.ingest('SYNTHETIC', 'crash', raw)
    assert archive.records() == []
    assert next(tmp_path.rglob('original.eml')).read_bytes() == raw
    reopened = module.Archive(tmp_path)
    assert len(reopened.records()) == 1
    assert reopened.records()[0]['body'].strip() == 'SYNTHETIC durable orphan'
    assert reopened.ingest('SYNTHETIC', 'crash', raw)['inserted'] is False


def test_attachment_manifest_and_inline_exclusion(tmp_path):
    from mailorganizer.archive import Archive
    msg = synthetic(subject='SYNTHETIC Українська відповідь')
    msg.add_attachment(b'SYNTHETIC invoice', maintype='text', subtype='plain', filename='../CON.txt')
    msg.add_attachment(b'SYNTHETIC second', maintype='text', subtype='plain', filename='../CON.txt')
    msg.add_attachment(b'SYNTHETIC image', maintype='image', subtype='png', filename='logo.png', disposition='inline', cid='<logo>')
    record = Archive(tmp_path).ingest('SYNTHETIC', 'attachments', msg.as_bytes())
    assert record['subject'] == 'SYNTHETIC Українська відповідь'
    assert len(record['attachments']) == 2
    assert len({item['path'] for item in record['attachments']}) == 2
    assert record['body'].strip() == 'We received your application'
    for item in record['attachments']:
        assert hashlib.sha256(Path(item['path']).read_bytes()).hexdigest() == item['sha256']
        assert item['extraction_status'] == 'extracted'
    import json
    manifest = Path(record['original_path']).with_name('attachments.json')
    assert json.loads(manifest.read_text(encoding='utf-8')) == record['attachments']
    assert record['attachment_manifest_sha256'] == hashlib.sha256(manifest.read_bytes()).hexdigest()


def test_html_visible_text_and_long_body(tmp_path):
    from mailorganizer.archive import Archive
    msg = synthetic()
    msg.set_content('<html><head><style>hidden style</style></head><body><p>SYNTHETIC &amp; visible</p><script>hidden script</script><p>' + 'x' * 50000 + '</p></body></html>', subtype='html')
    record = Archive(tmp_path).ingest('SYNTHETIC', 'html', msg.as_bytes())
    assert 'SYNTHETIC & visible' in record['body']
    assert 'hidden script' not in record['body'] and 'hidden style' not in record['body']
    assert 'x' * 50000 in record['body']


def test_same_id_other_account_and_changed_raw_conflict(tmp_path):
    from mailorganizer.archive import Archive
    archive = Archive(tmp_path)
    raw = synthetic().as_bytes()
    a = archive.ingest('SYNTHETIC-A', 'id', raw)
    b = archive.ingest('SYNTHETIC-B', 'id', raw)
    assert a['message_key'] != b['message_key']
    assert len(archive.records()) == 2
    changed = synthetic('SYNTHETIC changed').as_bytes()
    with pytest.raises(ValueError, match='conflict'):
        archive.ingest('SYNTHETIC-A', 'id', changed)
    assert archive.get(a['message_key'])['raw_sha256'] == hashlib.sha256(raw).hexdigest()
    assert Path(a['original_path']).read_bytes() == raw


def test_ingest_persist_and_deduplicate(tmp_path):
    from mailorganizer.archive import Archive
    raw = synthetic().as_bytes()
    archive = Archive(tmp_path)
    record = archive.ingest('gmail/synthetic', '../provider-id', raw)
    assert record['inserted'] is True
    assert record['account_id'] == 'gmail/synthetic'
    assert record['message_id'] == '../provider-id'
    assert record['subject'] == 'SYNTHETIC Bewerbung'
    assert 'sender@example.invalid' in record['sender']
    assert record['date'] and record['body'].strip() == 'We received your application'
    assert record['attachments'] == []
    assert record['topic'] == 'confirmations'
    assert Path(record['original_path']).read_bytes() == raw
    assert record['raw_sha256'] == hashlib.sha256(raw).hexdigest()
    assert archive.ingest('gmail/synthetic', '../provider-id', raw)['inserted'] is False
    reopened = Archive(tmp_path)
    assert len(reopened.records()) == 1
    assert reopened.get(record['message_key'])['subject'] == record['subject']
    assert reopened.get('missing') is None
