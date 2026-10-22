import pytest


def test_cli_help_lists_readonly_commands_and_no_send_or_calendar_writer(capsys):
    from mailorganizer.cli import main
    with pytest.raises(SystemExit) as result:
        main(["--help"])
    assert result.value.code == 0
    output = capsys.readouterr().out
    for command in ["demo", "import-eml", "report", "tasks", "validate-results", "sync", "auth", "status"]:
        assert command in output
    assert "send-mail" not in output
    assert "create-event" not in output


def test_export_paths_cannot_overwrite_archived_sources(archive_command_lock, tmp_path):
    from mailorganizer.cli import main
    from mailorganizer.archive import Archive
    folder = tmp_path / "demo"
    assert main(["demo", "--root", str(folder)]) == 0
    archived = Archive(folder).records()[0]
    original = __import__("pathlib").Path(archived["original_path"])
    before = original.read_bytes()
    assert main(["report", "--root", str(folder), "--output", str(original)]) == 2
    assert original.read_bytes() == before
    target = original.parent / "danger.html"
    assert main(["report", "--root", str(folder), "--output", str(target)]) == 2
    assert not target.exists()
    assert main(["tasks", "--root", str(folder), "--output", str(original)]) == 2
    assert original.read_bytes() == before



def test_demo_refuses_unlabelled_existing_data(archive_command_lock, tmp_path):
    from mailorganizer.cli import main
    folder = tmp_path / "existing"
    folder.mkdir()
    marker = folder / "important.txt"
    marker.write_text("SYNTHETIC existing-data marker")
    assert main(["demo", "--root", str(folder)]) == 2
    assert not (folder / "dataset.json").exists()
    assert marker.read_text() == "SYNTHETIC existing-data marker"



def test_sync_is_readonly_capped_and_rejects_duplicate_accounts(archive_command_lock, monkeypatch, tmp_path, capsys):
    import json
    from mailorganizer import auth, connectors
    from mailorganizer.cli import main
    class SyntheticGmail:
        def __init__(self, token):
            assert token == "SYNTHETIC_TEST_TOKEN"
        def get_profile(self):
            return {"emailAddress":"synthetic@example.test"}
        def iter_messages(self, query="", max_messages=100):
            yield "synthetic-provider-id", b"Subject: SYNTHETIC receipt\r\nFrom: service@example.test\r\n\r\nWe received your application."
    monkeypatch.setattr(auth, "google_access_token", lambda account_id: "SYNTHETIC_TEST_TOKEN")
    monkeypatch.setattr(connectors, "GmailReader", SyntheticGmail)
    folder = tmp_path / "live-simulation"
    command = ["sync", "--root", str(folder), "--account", "gmail-1", "--max-messages", "2"]
    assert main(command) == 0
    manifest = json.loads((folder / "sync_status.json").read_text(encoding="utf-8"))
    assert manifest["gmail-1"]["full_mailbox_verified"] is False
    assert manifest["gmail-1"]["downloaded"] == 1
    assert main(command) == 0
    second = ["sync", "--root", str(folder), "--account", "gmail-2", "--max-messages", "2"]
    assert main(second) == 2
    assert "SYNTHETIC_TEST_TOKEN" not in capsys.readouterr().out



def test_auth_dispatch_uses_isolated_slot_and_never_prints_token(monkeypatch, capsys, tmp_path):
    from mailorganizer import auth
    from mailorganizer.cli import main
    calls = []
    def authorize(account_id, client_file):
        calls.append((account_id, client_file))
        return {"account_id":account_id, "provider":"gmail", "authorized":True, "live_verified":False}
    monkeypatch.setattr(auth, "google_authorize", authorize)
    client = tmp_path / "desktop-oauth-client.json"
    client.write_text("{}")
    assert main(["auth", "--account", "gmail-2", "--client-file", str(client)]) == 0
    assert calls == [("gmail-2", client)]
    assert "access_token" not in capsys.readouterr().out


def test_status_is_not_a_live_verification(monkeypatch, capsys):
    from mailorganizer import auth
    from mailorganizer.cli import main
    monkeypatch.setattr(auth, "auth_status", lambda account_id,provider: {"account_id":account_id,"provider":provider,"authorized":False,"live_verified":False})
    assert main(["status", "--account", "gmail-1"]) == 0
    assert '\"live_verified\": false' in capsys.readouterr().out



def test_model_batch_validation_is_atomic_and_never_authorizes_calendar(archive_command_lock, tmp_path):
    import json
    from mailorganizer.cli import main
    folder = tmp_path / "demo"
    assert main(["demo", "--root", str(folder)]) == 0
    assert main(["tasks", "--root", str(folder)]) == 0
    tasks = [json.loads(line) for line in (folder / "model_tasks.jsonl").read_text(encoding="utf-8").splitlines()]
    assert tasks
    outputs = tmp_path / "outputs.jsonl"
    output = tasks[0]["expected_output"]
    outputs.write_text(json.dumps(output), encoding="utf-8")
    assert main(["validate-results", "--root", str(folder), "--input", str(outputs)]) == 0
    before = (folder / "model_proposals.json").read_bytes()
    proposals = json.loads(before)
    assert proposals[0]["calendar_write_allowed"] is False
    assert proposals[0]["state"] == "proposed"
    invalid = dict(output, task_id="invented")
    outputs.write_text(json.dumps(output) + "\n" + json.dumps(invalid), encoding="utf-8")
    assert main(["validate-results", "--root", str(folder), "--input", str(outputs)]) == 2
    assert (folder / "model_proposals.json").read_bytes() == before



def test_demo_is_repeatable_and_separate_from_real_archive(archive_command_lock, tmp_path):
    import json
    from mailorganizer.cli import main
    folder = tmp_path / "demo"
    assert main(["demo", "--root", str(folder)]) == 0
    summary = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
    assert summary["messages"] == 6
    assert summary["accounts"] == 3
    assert summary["data_kind"] == "synthetic"
    assert main(["demo", "--root", str(folder)]) == 0
    assert json.loads((folder / "summary.json").read_text(encoding="utf-8"))["messages"] == 6
    source = tmp_path / "fake-real.eml"
    source.write_bytes(b"Subject: real-source-test\r\n\r\nBody")
    assert main(["import-eml", "--root", str(folder), "--input", str(source), "--account", "gmail-1"]) == 2



def test_import_eml_archives_and_reports_without_changing_source(archive_command_lock, tmp_path):
    from email.message import EmailMessage
    from mailorganizer.cli import main
    source = tmp_path / "SYNTHETIC.eml"
    message = EmailMessage()
    message["Subject"] = "SYNTHETIC Bewerbung Eingangsbestätigung"
    message["From"] = "hr@example.test"
    message["Message-ID"] = "<synthetic-import@example.test>"
    message.set_content("Vielen Dank für Ihre Bewerbung. Wir haben Ihre Bewerbung erhalten.")
    raw = message.as_bytes()
    source.write_bytes(raw)
    archive = tmp_path / "archive"
    assert main(["import-eml", "--root", str(archive), "--input", str(source), "--account", "gmail-1"]) == 0
    assert main(["report", "--root", str(archive)]) == 0
    assert (archive / "report.html").exists()
    assert source.read_bytes() == raw
    assert "gmail-1" in (archive / "report.html").read_text(encoding="utf-8")

