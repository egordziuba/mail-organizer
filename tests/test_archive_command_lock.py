from conftest import windows_only

import threading
from contextlib import contextmanager
from types import SimpleNamespace
import sys

import pytest


ARCHIVE_COMMANDS = ["sync", "demo", "import-eml", "report", "prepare", "tasks", "validate-results"]


@pytest.mark.parametrize("command", ARCHIVE_COMMANDS)
def test_archive_commands_hold_lock_through_dispatch(command, tmp_path, monkeypatch):
    from mailorganizer import cli
    events = []
    result = object()
    args = SimpleNamespace(command=command, root=tmp_path / "archive")

    @contextmanager
    def lock(identity):
        assert identity == args.root.resolve() / "archive-writer"
        events.append("acquired")
        try:
            yield
        finally:
            events.append("released")

    def dispatch(received):
        assert received is args
        assert events == ["acquired"]
        events.append("dispatched")
        return result

    monkeypatch.setattr(cli, "_archive_lock", lock)
    monkeypatch.setattr(cli, "_dispatch_unlocked", dispatch)
    assert cli._dispatch(args) is result
    assert events == ["acquired", "dispatched", "released"]


@pytest.mark.parametrize("command", ARCHIVE_COMMANDS)
def test_busy_archive_commands_never_dispatch(command, tmp_path, monkeypatch):
    from mailorganizer import cli
    from mailorganizer.runtime import WorkerBusy

    @contextmanager
    def busy(identity):
        raise WorkerBusy("Synthetic contention")
        yield

    monkeypatch.setattr(cli, "_archive_lock", busy)
    monkeypatch.setattr(cli, "_dispatch_unlocked", lambda args: pytest.fail("Archive must stay locked"))
    with pytest.raises(WorkerBusy):
        cli._dispatch(SimpleNamespace(command=command, root=tmp_path))


def test_default_archive_lock_uses_real_mutex_and_preserves_platform_guard(tmp_path, capsys):
    from mailorganizer import cli
    from mailorganizer.runtime import NamedMutex

    lock = cli._archive_lock(tmp_path / "archive-writer")
    assert isinstance(lock, NamedMutex)
    if sys.platform == "win32":
        with lock:
            assert lock.handle is not None
        assert lock.handle is None
    else:
        with pytest.raises(RuntimeError, match="Windows runtime required"):
            with lock:
                pytest.fail("Unsupported platform must not acquire a lock")
        for command in ARCHIVE_COMMANDS:
            args = SimpleNamespace(command=command, root=tmp_path)
            with pytest.raises(RuntimeError, match="Windows runtime required"):
                cli._dispatch(args)
        assert cli.main(["report", "--root", str(tmp_path)]) == 2
        assert '"error_type": "RuntimeError"' in capsys.readouterr().out
        assert not (tmp_path / "archive.sqlite3").exists()


@windows_only
def test_report_does_not_replay_archive_while_another_command_is_writing(tmp_path):
    from mailorganizer.cli import main
    from mailorganizer.runtime import NamedMutex
    root=tmp_path / "archive";root.mkdir()
    ready,release=threading.Event(),threading.Event()
    def owner():
        with NamedMutex(root.resolve() / "archive-writer"):
            ready.set();release.wait(5)
    thread=threading.Thread(target=owner);thread.start();assert ready.wait(2)
    try:
        assert main(["report","--root",str(root)]) == 2
        assert not (root / "archive.sqlite3").exists()
    finally:
        release.set();thread.join()
