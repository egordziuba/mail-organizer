import threading


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
