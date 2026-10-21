def test_report_counts_from_records_not_model_text():
    from mailorganizer import reporting
    rows = [{"account_id":"gmail-1", "topic":"jobs", "needs_review":True, "action_required":True, "attachments":[{},{}]},
            {"account_id":"gmail-2", "topic":"university", "needs_review":False, "action_required":False, "attachments":[]}]
    result = reporting.summarize(rows)
    assert result == {"messages":2, "accounts":2, "documents":2, "actions":1, "review":1, "topics":{"jobs":1,"university":1}}


def test_html_report_escapes_email_and_names_and_labels_scope():
    from mailorganizer.reporting import render_report
    rows = [{"account_id":"gmail-1", "message_key":"abc", "topic":"jobs", "subtype":"unknown", "subject":"<script>alert(1)</script>", "sender":"<img src=x onerror=alert(1)>", "body":"test", "needs_review":True, "action_required":False, "attachments":[], "evidence":[], "original_path":"C:/test.eml"}]
    html = render_report(rows, label="SYNTHETIC TEST DATA", coverage="Only imported test fixtures")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "SYNTHETIC TEST DATA" in html
    assert "Only imported test fixtures" in html
    assert "Календарь не изменяется" in html
    assert "fetch(" not in html

