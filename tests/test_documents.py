"""SYNTHETIC attachments only."""
from pathlib import Path
import hashlib
import pytest

@pytest.mark.parametrize('mime', ['application/pdf', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'application/octet-stream'])
def test_binary_not_claimed_extracted(tmp_path, mime):
    from mailorganizer.documents import save_attachment
    item = save_attachment(tmp_path, 0, 'SYNTHETIC.bin', mime, b'\x00SYNTHETIC')
    assert item['extraction_status'] == 'unsupported'
    assert item['text'] == ''

def test_existing_bytes_never_overwritten(tmp_path):
    from mailorganizer.documents import atomic_write
    target = tmp_path / 'SYNTHETIC.txt'
    atomic_write(target, b'first')
    atomic_write(target, b'first')
    with pytest.raises(ValueError, match='conflict'):
        atomic_write(target, b'second')
    assert target.read_bytes() == b'first'

def test_unknown_charset_is_honest(tmp_path):
    from mailorganizer.documents import save_attachment
    result = save_attachment(tmp_path, 0, 'SYNTHETIC.txt', 'text/plain', b'text', 'not-a-charset')
    assert result['extraction_status'] == 'decode_error'
    assert result['text'] == ''


def test_safe_duplicate_attachments(tmp_path):
    from mailorganizer.documents import save_attachment
    for index, filename in enumerate(['../CON.txt', '../CON.txt', 'C:\\secret\\AUX.txt', 'a/b:evil?.txt']):
        item = save_attachment(tmp_path, index, filename, 'text/plain', b'SYNTHETIC text')
        path = Path(item['path'])
        assert path.parent == tmp_path
        assert path.exists() and path.read_bytes() == b'SYNTHETIC text'
        assert path.name.startswith(f'{index:04d}_')
        assert item['sha256'] == hashlib.sha256(b'SYNTHETIC text').hexdigest()
        assert item['original_filename'] == filename
        assert item['extraction_status'] == 'extracted'
        assert item['text'] == 'SYNTHETIC text'
    assert len(list(tmp_path.iterdir())) == 4
