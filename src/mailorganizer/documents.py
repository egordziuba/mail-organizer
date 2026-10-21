"""Local attachment storage. No execution, network, or OCR."""
import hashlib
import os
from pathlib import Path
import re
import tempfile


def atomic_write(path: Path, data: bytes) -> None:
    """Publish complete bytes without overwriting existing different content."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.pending-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != data:
                raise ValueError(f'Archive content conflict: {path}')
    finally:
        Path(temporary).unlink(missing_ok=True)


def safe_filename(filename: str) -> str:
    name = filename.replace('\\', '/').split('/')[-1]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(' .')[:120]
    if not name:
        name = 'attachment'
    if re.fullmatch(r'CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]', name.split('.')[0], re.I):
        name = '_' + name
    return name


def save_attachment(directory: Path, index: int, filename: str, mime: str,
                    data: bytes, charset: str = 'utf-8') -> dict:
    path = Path(directory) / f'{index:04d}_{safe_filename(filename)}'
    atomic_write(path, data)
    text, status = '', 'unsupported'
    if mime == 'text/plain':
        try:
            text = data.decode(charset, errors='strict')
            status = 'extracted'
        except (LookupError, UnicodeError):
            status = 'decode_error'
    return dict(path=str(path), original_filename=filename, mime=mime,
                sha256=hashlib.sha256(data).hexdigest(), size=len(data),
                extraction_status=status, text=text)
