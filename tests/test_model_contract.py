import pytest


def record():
    return {"message_key": "synthetic-key", "subject": "SYNTHETIC: Interview", "body": "We invite you to an interview on 20 October 2026 at 14:00.", "topic": "jobs"}


def test_task_is_bounded_has_source_digest_and_no_tools():
    from mailorganizer.model_contract import make_task
    task = make_task(record())
    assert task["task_id"]
    assert task["source_sha256"]
    assert task["message_key"] == "synthetic-key"
    assert task["tools"] == []
    assert task["input"]["body"] == record()["body"]
    assert task["expected_output"]["date_candidate"] is None


def test_sensitive_codes_and_reset_links_are_redacted():
    from mailorganizer.model_contract import make_task
    r = record()
    r["body"] = "Verification code: 123456. Reset https://example.test/reset?token=SECRET"
    task = make_task(r)
    assert "123456" not in task["input"]["body"]
    assert "SECRET" not in task["input"]["body"]

@pytest.mark.parametrize("change", [{"topic": []}, {"evidence":[{"source_id":[],"quote":"Interview"}]}])
def test_type_confusion_rejected_cleanly(change):
    from mailorganizer.model_contract import make_task, validate_result
    task = make_task(record())
    output = valid_result(task)
    output.update(change)
    with pytest.raises(ValueError):
        validate_result(task, output)


@pytest.mark.parametrize("change", [{"body": "x" * 12001}, {"topic": "security"}])
def test_oversized_and_security_messages_are_blocked_for_model(change):
    from mailorganizer.model_contract import make_task
    r = record()
    r.update(change)
    with pytest.raises(ValueError):
        make_task(r)



def test_date_candidate_is_raw_evidence_and_always_needs_review():
    from mailorganizer.model_contract import make_task, validate_result
    task = make_task(record())
    output = valid_result(task)
    output["needs_review"] = False
    output["date_candidate"] = {"source_id":"body","quote":"20 October 2026 at 14:00","raw":"20 October 2026 at 14:00"}
    assert validate_result(task, output)["needs_review"] is True
    output["date_candidate"]["raw"] = "2030-01-01"
    with pytest.raises(ValueError):
        validate_result(task, output)


@pytest.mark.parametrize("evidence", [[], [{"source_id":"body","quote":"Invented deadline"}], [{"source_id":"untrusted","quote":"Interview"}], [{"source_id":"body","quote":""}]])
def test_action_requires_exact_nonempty_source_evidence(evidence):
    from mailorganizer.model_contract import make_task, validate_result
    task = make_task(record())
    output = valid_result(task)
    output["evidence"] = evidence
    with pytest.raises(ValueError):
        validate_result(task, output)


@pytest.mark.parametrize("change", [{"topic": "execute"}, {"action_required": "yes"}, {"send": True}, {"task_id": "stale"}, {"subtype": None}, {"needs_review": 1}, {"reason": None}])
def test_schema_and_task_identity_are_strict(change):
    from mailorganizer.model_contract import make_task, validate_result
    task = make_task(record())
    output = valid_result(task)
    output.update(change)
    with pytest.raises(ValueError):
        validate_result(task, output)



def valid_result(task):
    output = dict(task["expected_output"])
    output.update(topic="jobs", subtype="interview", action_required=True, needs_review=True,
                  evidence=[{"source_id": "body", "quote": "We invite you to an interview"}])
    return output


def test_valid_result_stays_a_proposal_not_a_calendar_action():
    from mailorganizer.model_contract import make_task, validate_result
    task = make_task(record())
    result = validate_result(task, valid_result(task))
    assert result["state"] == "proposed"
    assert result["calendar_write_allowed"] is False
    assert result["message_key"] == record()["message_key"]


