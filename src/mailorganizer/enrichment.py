"""Local-only derived cache and navigable views; immutable archive is read-only."""
import copy
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from urllib.parse import quote
import re
from .extraction import EXTRACTOR_VERSION, extract_document
from .triage import classify

CATEGORIES = ('University', 'Jobs', 'Finance', 'Confirmations', 'Housing', 'Security', 'Other')


def _digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(65536):
            result.update(chunk)
    return result.hexdigest()


def _review(status, reason):
    return dict(status=status, text='', parts=[], complete=False, requires_review=True,
                reason=reason, source_count=0, review_sources=[])


class Enrichment:
    """No archive/SQL records are changed. All returned records are deep copies."""
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.derived = self._output_path('derived')
        self.derived.mkdir(exist_ok=True, mode=0o700)
        self.database = self._output_path('derived.sqlite3')
        with self._connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS extractions (sha256 TEXT NOT NULL, extractor_version TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(sha256, extractor_version))')
        os.chmod(self.database, 0o600)

    def _output_path(self, relative):
        path = self.root / relative
        if not path.is_relative_to(self.root) or '..' in Path(relative).parts:
            raise ValueError('Unsafe output path')
        for node in [path, *path.parents]:
            if node == self.root:
                break
            if node.is_symlink() or (node.exists() and node.resolve() != node):
                raise ValueError('Derived output contains symlink or junction')
            if node.is_file() and node.stat().st_nlink != 1:
                raise ValueError('Derived output contains hardlink')
        if path.is_relative_to(self.root / 'messages'):
            raise ValueError('Archive originals are protected')
        return path

    def _connect(self):
        for suffix in ('', '-journal', '-wal', '-shm'):
            self._output_path('derived.sqlite3' + suffix)
        return sqlite3.connect(self.database)

    def _write(self, relative, data):
        path = self._output_path(relative)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._output_path(relative)
        fd, temporary = tempfile.mkstemp(prefix='.pending-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return path

    def _source(self, raw):
        path = Path(str(raw))
        if not path.is_absolute():
            path = self.root / path
        archive = self.root / 'messages'
        if not path.is_relative_to(archive):
            raise ValueError('outside_archive_or_symlink')
        for node in [path, *path.parents]:
            if node == self.root:
                break
            if node.is_symlink() or (node.exists() and node.resolve() != node):
                raise ValueError('outside_archive_or_symlink')
        resolved = path.resolve()
        if not resolved.is_relative_to(archive) or not resolved.is_file() or resolved.stat().st_nlink != 1:
            raise ValueError('outside_archive_or_symlink')
        return resolved

    def _extract(self, attachment):
        try:
            path = self._source(attachment.get('path', ''))
        except ValueError:
            return _review('unsafe', 'outside_archive_or_symlink'), False
        try:
            before = _digest(path)
            if before != attachment.get('sha256'):
                return _review('source_mismatch', 'sha256_mismatch'), False
            with self._connect() as db:
                row = db.execute('SELECT result FROM extractions WHERE sha256=? AND extractor_version=?', (before, EXTRACTOR_VERSION)).fetchone()
            result = None
            if row:
                try:
                    candidate = json.loads(row[0])
                    txt = self._output_path(f'derived/{before}-{EXTRACTOR_VERSION}.txt')
                    if candidate['source_sha256'] == before and candidate['extractor_version'] == EXTRACTOR_VERSION and txt.is_file() and _digest(txt) == candidate['text_sha256']:
                        result = candidate
                except (KeyError, ValueError, OSError, TypeError):
                    pass
            hit = result is not None
            if result is None:
                result = extract_document(path, attachment.get('mime', ''))
            if _digest(path) != before:
                return _review('source_changed', 'original_changed_during_extraction'), False
            if not hit:
                text_bytes = result['text'].encode('utf-8')
                result = dict(result, source_sha256=before, extractor_version=EXTRACTOR_VERSION,
                              text_sha256=hashlib.sha256(text_bytes).hexdigest())
                self._write(f'derived/{before}-{EXTRACTOR_VERSION}.txt', text_bytes)
                with self._connect() as db:
                    db.execute('INSERT OR REPLACE INTO extractions VALUES (?, ?, ?)', (before, EXTRACTOR_VERSION, json.dumps(result, ensure_ascii=False)))
            return result, hit
        except OSError:
            return _review('unreadable', 'source_unreadable'), False

    def process(self, records: list[dict], limit: int | None = None) -> dict:
        """Limit counts attachments; return aggregate counters, never private text."""
        if limit is not None and limit < 0:
            raise ValueError('limit must be nonnegative')
        counts = dict(processed=0, extracted=0, requires_review=0, cache_hits=0, skipped=0)
        for record in records:
            for attachment in record.get('attachments', []):
                if limit is not None and counts['processed'] >= limit:
                    counts['skipped'] += 1
                    continue
                result, hit = self._extract(attachment)
                counts['processed'] += 1
                counts['cache_hits'] += int(hit)
                counts['extracted'] += int(result['status'] == 'extracted')
                counts['requires_review'] += int(result['requires_review'])
        return counts

    def enrich_records(self, records) -> list[dict]:
        enriched = copy.deepcopy(records)
        for record in enriched:
            evidence, topics, notes = [], [], []
            # Subject/body classification remains provisional; attachments keep exact provenance.
            body_signal = classify(record.get('subject', ''), record.get('body', ''), record.get('sender', ''))
            if body_signal['topic'] != 'other':
                topics.append(body_signal['topic'])
                evidence.extend(body_signal['evidence'])
            for attachment in record.get('attachments', []):
                result, hit = self._extract(attachment)
                attachment['extraction'] = copy.deepcopy(result)
                attachment.update(text=result['text'], extraction_status=result['status'], parts=copy.deepcopy(result['parts']),
                                  complete=result['complete'], requires_review=result['requires_review'],
                                  extraction_reason=result['reason'], source_count=result['source_count'],
                                  review_sources=result.get('review_sources', []), source_sha256=result.get('source_sha256'),
                                  extractor_version=EXTRACTOR_VERSION, cache_hit=hit)
                if result['requires_review']:
                    notes.append(dict(sha256=attachment.get('sha256', ''), reason=result['reason']))
                for part in result['parts']:
                    signal = classify('', part['text'])
                    if signal['topic'] != 'other':
                        topics.append(signal['topic'])
                        for item in signal['evidence']:
                            evidence.append(dict(source_id=f"attachment:{attachment.get('sha256', '')}:{part['source_id']}", quote=item['quote']))
            unique = set(topics)
            record['derived_topic'] = next(iter(unique)) if len(unique) == 1 else 'other'
            record['derived_evidence'] = evidence
            record['derived_provisional'] = True
            record['derived_notes'] = notes + ([dict(reason='conflicting_category_signals')] if len(unique) > 1 else [])
        return enriched

    @staticmethod
    def _label(value):
        # Remove active URLs and markdown/HTML syntax, including control characters.
        text = re.sub(r'https?://\S+', '[external URL omitted]', str(value))
        text = re.sub(r'[\x00-\x1f\x7f<>\[\]()!*`#|\\]', ' ', text)
        return ' '.join(text.split())[:300]

    def _link(self, source, directory):
        try:
            path = self._source(source)
        except ValueError:
            return ''
        relative = os.path.relpath(path, directory).replace('\\', '/')
        return quote(relative, safe='/.-_')

    def export_views(self, records) -> dict:
        """Generate local indexes; confirmed topic wins over provisional attachment topic."""
        groups = {category: [] for category in CATEGORIES}
        queue = []
        for record in records:
            topic = str(record.get('topic', 'other')).lower()
            if topic == 'other':
                topic = str(record.get('derived_topic', 'other')).lower()
            category = next((c for c in CATEGORIES if c.lower() == topic), 'Other')
            groups[category].append(record)
            for attachment in record.get('attachments', []):
                if attachment.get('requires_review', attachment.get('extraction_status') != 'extracted'):
                    try:
                        source = str(self._source(attachment.get('path', '')))
                    except ValueError:
                        source = ''
                    queue.append(dict(message_key=record.get('message_key', ''), original_path=source,
                                      sha256=attachment.get('sha256', ''), reason=attachment.get('extraction_reason', 'not_extracted'),
                                      status=attachment.get('extraction_status', 'not_extracted'),
                                      page_count=attachment.get('source_count', 0) if Path(source).suffix.lower() == '.pdf' else None,
                                      source_count=attachment.get('source_count', 0), review_sources=attachment.get('review_sources', []),
                                      ocr_performed=False))
        for category, items in groups.items():
            directory = self._output_path(f'views/{category}')
            markdown = [f'# {category}', '', 'Local archive links. Derived classification is provisional; no payment/deadline claims are verified.', '']
            output = io.StringIO(newline='')
            writer = csv.writer(output)
            writer.writerow(['message_id', 'subject', 'sender', 'date', 'original_link', 'attachment_links', 'snapshot_link', 'provisional', 'review_count'])
            for record in items:
                identity = hashlib.sha256(str(record.get('message_key', record.get('original_path', ''))).encode()).hexdigest()
                title = self._label(record.get('subject', '')) or '(no subject)'
                original = self._link(record.get('original_path', ''), directory)
                links = []
                review_count = 0
                markdown.extend([f'## {title}', f"Sender: {self._label(record.get('sender', ''))} | Date: {self._label(record.get('date', ''))}", f'[Original .eml]({original})' if original else 'Original unavailable / unsafe path'])
                snapshot = [str(record.get('subject', '')), '', str(record.get('body', ''))]
                for attachment in record.get('attachments', []):
                    link = self._link(attachment.get('path', ''), directory)
                    label = self._label(attachment.get('original_filename', 'attachment'))
                    status = self._label(attachment.get('extraction_status', 'unknown'))
                    if link:
                        links.append(link)
                        markdown.append(f'- [{label}]({link}) — {status}')
                        snapshot.extend(['', label, str(attachment.get('text', ''))])
                    else:
                        markdown.append(f'- {label} — unsafe/unavailable original')
                    review_count += int(attachment.get('requires_review', attachment.get('extraction_status') != 'extracted'))
                snap = f'{identity}.txt'
                self._write(f'views/{category}/{snap}', '\n'.join(snapshot).encode('utf-8'))
                markdown.extend([f'[Readable snapshot]({snap})', f'Documents requiring review: {review_count}', ''])
                # Neutralize spreadsheet formula injection in user-controlled CSV cells.
                def cell(value):
                    value = self._label(value)
                    return "'" + value if value.startswith(('=', '+', '-', '@')) else value
                writer.writerow([identity, cell(record.get('subject', '')), cell(record.get('sender', '')), cell(record.get('date', '')), original, ';'.join(links), snap, True, review_count])
            self._write(f'views/{category}/INDEX.md', '\n'.join(markdown).encode('utf-8'))
            self._write(f'views/{category}/manifest.csv', output.getvalue().encode('utf-8-sig'))
        self._write('views/Review_documents.json', json.dumps(queue, ensure_ascii=False, indent=2).encode('utf-8'))
        return dict(messages=len(records), categories=len(CATEGORIES), review_documents=len(queue))
