def test_backfill_is_continued_without_waiting_fifteen_minutes():
    from mailorganizer.runtime import cycle_delay
    assert cycle_delay({"accounts":{"gmail-1":{"state":"backfill_pending","pending":1000}}},900) == 2
    assert cycle_delay({"accounts":{"gmail-1":{"state":"needs_attention","pending":1000,"failed":5}}},900) == 2
    assert cycle_delay({"accounts":{"gmail-1":{"state":"needs_attention","pending":0,"failed":5}}},900) == 60
    assert cycle_delay({"accounts":{"gmail-1":{"state":"synced","pending":0}}},900) == 900
    assert cycle_delay({"accounts":{"gmail-1":{"state":"retry_later"}}},900) == 900
