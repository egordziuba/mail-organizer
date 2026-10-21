from pathlib import Path


def test_provider_index_separates_accounts_and_persists(tmp_path):
    from mailorganizer.archive import Archive
    archive=Archive(tmp_path)
    archive.ingest("gmail-1","synthetic-one",b"Subject: SYNTHETIC\r\n\r\nBody")
    archive.ingest("gmail-2","synthetic-two",b"Subject: SYNTHETIC\r\n\r\nBody")
    assert archive.provider_ids("gmail-1") == {"synthetic-one"}
    assert archive.has("gmail-1","synthetic-one") is True
    assert archive.has("gmail-2","synthetic-one") is False
    assert Archive(tmp_path).provider_ids("gmail-2") == {"synthetic-two"}


def test_reopen_replays_orphans_but_does_not_rehash_known_originals(tmp_path,monkeypatch):
    from mailorganizer.archive import Archive
    archive=Archive(tmp_path)
    archive.ingest("gmail-1","synthetic-one",b"Subject: SYNTHETIC\r\n\r\nBody")
    def forbidden(path):raise AssertionError("Known original must not be read again on every sync")
    monkeypatch.setattr(Path,"read_bytes",forbidden)
    assert Archive(tmp_path).has("gmail-1","synthetic-one")

