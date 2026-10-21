import pytest


@pytest.mark.parametrize("subject,body,topic,subtype,action", [
    ("Bewerbung Eingangsbestätigung", "Wir haben Ihre Bewerbung erhalten.", "jobs", "application_received", False),
    ("Hochschule: fehlende Unterlagen", "Bitte reichen Sie die Dokumente bis zum 25.10.2026 ein.", "university", "documents_requested", True),
    ("Rechnung: Zahlungsfrist", "Bitte zahlen Sie die Rechnung bis zum 22.10.2026.", "finance", "payment_due", True),
    ("Anmeldebestätigung", "Ihre Anmeldung wurde bestätigt. Keine weitere Aktion ist erforderlich.", "confirmations", "receipt", False),
    ("Bewerbung", "Leider müssen wir Ihnen eine Absage erteilen.", "jobs", "rejected", False),
])
def test_requested_german_categories(subject, body, topic, subtype, action):
    from mailorganizer.triage import classify
    result = classify(subject, body)
    assert result["topic"] == topic
    assert result["subtype"] == subtype
    assert result["action_required"] is action
    assert result["evidence"]


def test_confirmation_is_not_an_offer_or_calendar_event():
    from mailorganizer.triage import classify
    result = classify("Bewerbung Eingangsbestätigung", "Wir haben Ihre Bewerbung erhalten.")
    assert result["subtype"] == "application_received"
    assert result["action_required"] is False
    assert "date" not in result
