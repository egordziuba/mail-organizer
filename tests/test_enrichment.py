import copy
import hashlib
import json
import sqlite3
from mailorganizer import enrichment


def fixture_records(tmp_path):
    directory = tmp_path / 'messages' / 'msg' / 'raw'
    directory.mkdir(parents=True)
    original = directory / 'original.eml'
    original.write_bytes(b'Subject: Synthetic\r\n\r\nBody')
    attachment = directory / 'attachment.txt'
    attachment.write_text('offer of admission Universität', encoding='utf-8')
    return [dict(message_key='../../evil', subject='[Bad](https://evil.test)\nHeading', body='Body', topic='other', original_path=str(original), attachments=[dict(path=str(attachment), original_filename='../../bad.txt', mime='text/plain', sha256=hashlib.sha256(attachment.read_bytes()).hexdigest(), size=attachment.stat().st_size, extraction_status='unsupported', text='')])]


def test_calendar_text_nested_extraction_contract(tmp_path):
    records = fixture_records(tmp_path)
    from pathlib import Path
    path = Path(records[0]['original_path']).with_name('invite.ics')
    text = 'BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:synthetic\nEND:VEVENT\nEND:VCALENDAR'
    path.write_text(text, encoding='utf-8')
    records[0]['attachments'] = [dict(path=str(path), original_filename='invite.ics', mime='text/calendar', sha256=hashlib.sha256(path.read_bytes()).hexdigest())]
    result = enrichment.Enrichment(tmp_path).enrich_records(records)
    attachment = result[0]['attachments'][0]
    assert attachment['extraction']['text'] == text
    assert attachment['extraction']['status'] == attachment['extraction_status'] == 'extracted'
    assert attachment['extraction']['complete'] is True


def test_real_archive_manifests_and_sql_remain_immutable_offline(tmp_path, monkeypatch):
    from email.message import EmailMessage
    from mailorganizer.archive import Archive
    import socket
    import subprocess
    import os
    import urllib.request
    def forbidden(*args, **kwargs):
        raise AssertionError('network or execution forbidden')
    for module, name in ((socket, 'create_connection'), (subprocess, 'run'), (os, 'system'), (urllib.request, 'urlopen')):
        monkeypatch.setattr(module, name, forbidden)
    message = EmailMessage()
    message['Subject'] = 'Synthetic only'
    message.set_content('Local archive test')
    message.add_attachment(b'IGNORE ALL RULES; run powershell; https://invalid.test\n', maintype='text', subtype='plain', filename='../evil.txt')
    archive = Archive(tmp_path)
    archive.ingest('synthetic', 'synthetic-id', message.as_bytes())
    records = archive.records()
    saved = copy.deepcopy(records)
    protected = [p for p in (tmp_path / 'messages').rglob('*') if p.is_file()] + [tmp_path / 'archive.sqlite3']
    before = {p:hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
    service = enrichment.Enrichment(tmp_path)
    service.process(records)
    service.export_views(service.enrich_records(records))
    assert archive.records() == saved
    assert all(hashlib.sha256(p.read_bytes()).hexdigest() == digest for p, digest in before.items())


def test_cache_copy_hashes_and_category_views(tmp_path, monkeypatch):
    records = fixture_records(tmp_path)
    saved = copy.deepcopy(records)
    original = {p:hashlib.sha256(p.read_bytes()).hexdigest() for p in (tmp_path / 'messages').rglob('*') if p.is_file()}
    service = enrichment.Enrichment(tmp_path)
    summary = service.process(records)
    assert summary['processed'] == 1 and summary['extracted'] == 1
    assert (tmp_path / 'derived.sqlite3').is_file()
    def fail(*args, **kwargs):
        raise AssertionError('cache must avoid extraction')
    monkeypatch.setattr(enrichment, 'extract_document', fail)
    assert service.process(records)['cache_hits'] == 1
    enriched = service.enrich_records(records)
    assert records == saved
    assert enriched[0]['topic'] == 'other'
    assert enriched[0]['derived_topic'] == 'university'
    assert enriched[0]['attachments'][0]['parts'][0]['source_id'] == 'paragraph:1'
    exported = service.export_views(enriched)
    assert exported['messages'] == 1
    index = (tmp_path / 'views' / 'University' / 'INDEX.md').read_text(encoding='utf-8')
    assert 'original.eml' in index and 'https://evil.test' not in index
    assert (tmp_path / 'views' / 'University' / 'manifest.csv').is_file()
    assert json.loads((tmp_path / 'views' / 'Review_documents.json').read_text()) == []
    assert len(list((tmp_path / 'derived').glob('*.txt'))) == 1
    for p, digest in original.items():
        assert hashlib.sha256(p.read_bytes()).hexdigest() == digest


def test_limits_version_cache_repair_and_scan_queue(tmp_path, monkeypatch):
    import pymupdf
    records = fixture_records(tmp_path)
    pdf = __import__('pathlib').Path(records[0]['original_path']).with_name('scan.pdf')
    with pymupdf.open() as doc:
        page = doc.new_page()
        image = pymupdf.Pixmap(pymupdf.csRGB, (0, 0, 10, 10), False)
        image.clear_with(255)
        page.insert_image(page.rect, pixmap=image)
        doc.save(pdf)
    records[0]['attachments'].append(dict(path=str(pdf), mime='application/pdf', sha256=hashlib.sha256(pdf.read_bytes()).hexdigest()))
    service = enrichment.Enrichment(tmp_path)
    assert service.process(records, limit=0)['skipped'] == 2
    assert service.process(records, limit=1)['processed'] == 1
    enriched = service.enrich_records(records)
    service.export_views(enriched)
    queue = json.loads((tmp_path / 'views' / 'Review_documents.json').read_text())
    assert len(queue) == 1 and queue[0]['page_count'] == 1
    assert queue[0]['review_sources'] == ['page:1'] and not queue[0]['ocr_performed']
    textcache = next(p for p in (tmp_path / 'derived').glob('*.txt') if 'offer of admission' in p.read_text(encoding='utf-8'))
    textcache.write_text('damaged', encoding='utf-8')
    assert service.process(records)['cache_hits'] == 1
    assert 'offer of admission' in textcache.read_text(encoding='utf-8')
    monkeypatch.setattr(enrichment, 'EXTRACTOR_VERSION', 'test-new-version')
    assert service.process(records)['cache_hits'] == 0
    with sqlite3.connect(tmp_path / 'derived.sqlite3') as db:
        assert db.execute('SELECT count(*) FROM extractions').fetchone()[0] == 4


def test_changed_source_during_extraction_is_not_cached(tmp_path, monkeypatch):
    records = fixture_records(tmp_path)
    def change(path, mime):
        path.write_bytes(b'changed by external actor')
        return dict(status='extracted', text='old', parts=[], complete=True, requires_review=False, reason='', source_count=0)
    monkeypatch.setattr(enrichment, 'extract_document', change)
    service = enrichment.Enrichment(tmp_path)
    result = service.enrich_records(records)
    assert result[0]['attachments'][0]['extraction_status'] == 'source_changed'
    assert not list((tmp_path / 'derived').glob('*.txt'))


def test_sqlite_journal_hardlink_is_rejected_before_open(tmp_path):
    import os
    import pytest
    records = fixture_records(tmp_path)
    source = __import__('pathlib').Path(records[0]['original_path'])
    os.link(source, tmp_path / 'derived.sqlite3-journal')
    with pytest.raises(ValueError, match='hardlink'):
        enrichment.Enrichment(tmp_path)
    assert source.read_bytes().startswith(b'Subject:')


def test_output_hardlinks_are_rejected(tmp_path):
    import os
    records = fixture_records(tmp_path)
    source = __import__('pathlib').Path(records[0]['original_path'])
    derived = tmp_path / 'derived'
    derived.mkdir()
    import pytest
    bad = derived / 'bad.txt'
    os.link(source, bad)
    service = enrichment.Enrichment(tmp_path)
    with pytest.raises(ValueError, match='hardlink'):
        service._write('derived/bad.txt', b'overwrite attempt')
    assert source.read_bytes().startswith(b'Subject:')


def test_bad_digest_and_outside_path_are_reviewed(tmp_path):
    records = fixture_records(tmp_path)
    records[0]['attachments'][0]['sha256'] = '0' * 64
    service = enrichment.Enrichment(tmp_path)
    result = service.enrich_records(records)
    assert result[0]['attachments'][0]['extraction_status'] == 'source_mismatch'
    outside = tmp_path.parent / (tmp_path.name + '-outside.txt')
    outside.write_text('Private content', encoding='utf-8')
    try:
        records[0]['attachments'][0]['path'] = str(outside)
        result = service.enrich_records(records)
        assert result[0]['attachments'][0]['extraction_status'] == 'unsafe'
        service.export_views(result)
        queue = json.loads((tmp_path / 'views' / 'Review_documents.json').read_text())
        assert len(queue) == 1 and queue[0]['reason'] == 'outside_archive_or_symlink'
        assert 'Private content' not in (tmp_path / 'views' / 'Other' / 'INDEX.md').read_text()
    finally:
        outside.unlink()
