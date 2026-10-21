from mailorganizer import runtime


def config():
    return {"interval_seconds":900,"backfill_pause_seconds":2,"backfill_retry_seconds":60,"accounts":[{"id":"gmail-1","enabled":True},{"id":"gmail-2","enabled":True},{"id":"outlook-1","enabled":False}]}


def test_backfill_progress_continues_without_waiting_fifteen_minutes():
    cycle={"accounts":{"gmail-1":{"state":"backfill_pending","pending":200,"fetched_this_step":20},"gmail-2":{"state":"synced"}}}
    assert runtime.cycle_wait_seconds(config(),cycle)==2


def test_no_progress_failure_has_backoff_not_busy_loop():
    cycle={"accounts":{"gmail-1":{"state":"needs_attention","pending":200,"fetched_this_step":0},"gmail-2":{"state":"synced"}}}
    assert runtime.cycle_wait_seconds(config(),cycle)==60


def test_synced_accounts_return_to_normal_interval():
    cycle={"accounts":{"gmail-1":{"state":"synced"},"gmail-2":{"state":"synced"},"outlook-1":{"state":"backfill_pending","pending":5}}}
    assert runtime.cycle_wait_seconds(config(),cycle)==900
