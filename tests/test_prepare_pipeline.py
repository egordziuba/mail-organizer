import json


def test_prepare_generates_local_documents_cases_and_readonly_proposals(archive_command_lock, tmp_path):
    from mailorganizer.cli import main
    root=tmp_path / "synthetic"
    assert main(["demo","--root",str(root)]) == 0
    assert main(["prepare","--root",str(root)]) == 0
    assert (root / "views" / "University" / "INDEX.md").exists()
    assert (root / "ACTIONS.md").exists()
    assert (root / "CASES.md").exists()
    assert (root / "calendar_proposals.json").exists()
    analysis=json.loads((root / "analysis.json").read_text(encoding="utf-8"))
    assert analysis["summaries"]["messages"] == 6
    assert analysis["calendar_modified"] is False
    assert not any(p.get("calendar_write_allowed",False) for p in json.loads((root / "calendar_proposals.json").read_text()))
