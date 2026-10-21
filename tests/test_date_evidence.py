import pytest


def test_masked_digits_cannot_be_used_as_a_date_candidate():
    from mailorganizer.model_contract import make_task,validate_result
    task=make_task({"message_key":"synthetic","subject":"Question","body":"Deadline: 20261025"})
    result=dict(task["expected_output"],date_candidate={"source_id":"body","quote":"[CODE REDACTED]","raw":"[CODE REDACTED]"})
    with pytest.raises(ValueError):validate_result(task,result)
