import pytest


def test_unknown_cannot_disable_review():
    from mailorganizer.model_contract import make_task, validate_result
    task = make_task({"message_key":"synthetic", "subject":"Question", "body":"Unclear message"})
    result = dict(task["expected_output"], needs_review=False)
    assert validate_result(task, result)["needs_review"] is True


@pytest.mark.parametrize("quote", ["[LINK REDACTED]", "[CODE REDACTED]", "Go [LINK REDACTED]"])
def test_redaction_placeholder_is_not_source_evidence(quote):
    from mailorganizer.model_contract import make_task, validate_result
    task = make_task({"message_key":"synthetic", "subject":"Question", "body":"Go https://example.test/reset?secret=hidden 123456"})
    result = dict(task["expected_output"], topic="jobs", subtype="interview", action_required=True,
                  evidence=[{"source_id":"body","quote":quote}])
    with pytest.raises(ValueError):
        validate_result(task, result)
