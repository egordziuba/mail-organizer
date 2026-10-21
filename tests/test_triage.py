"""Only SYNTHETIC text; no real mailbox data."""
import pytest

@pytest.mark.parametrize('text', ['We do not invite you to an interview', 'We cannot offer of admission', 'No verification code is needed', 'Keine Einladung zum Vorstellungsgespräch', 'не приглашаем на собеседование', 'не запрошуємо на співбесіду'])
def test_negated_signal_is_not_actionable(text):
    from mailorganizer.triage import classify
    result = classify('SYNTHETIC', text)
    assert result['action_required'] is False
    assert result['subtype'] == 'unknown'
    assert result['needs_review'] is True

def test_mixed_signals_do_not_hide_required_action():
    from mailorganizer.triage import classify
    result = classify('SYNTHETIC verification code', 'We invite you to an interview')
    assert result['action_required'] is True
    assert result['needs_review'] is True


@pytest.mark.parametrize('phrase,topic,subtype,action', [
 ('We received your application', 'confirmations', 'receipt', False),
 ('Eingang Ihrer Bewerbung bestätigt', 'confirmations', 'receipt', False),
 ('заявка получена', 'confirmations', 'receipt', False),
 ('заявку отримано', 'confirmations', 'receipt', False),
 ('offer of admission', 'university', 'admission', True),
 ('Zulassungsbescheid', 'university', 'admission', True),
 ('вы зачислены', 'university', 'admission', True),
 ('вас зараховано', 'university', 'admission', True),
 ('payment overdue', 'finance', 'payment_due', True),
 ('Zahlung fällig', 'finance', 'payment_due', True),
 ('оплата просрочена', 'finance', 'payment_due', True),
 ('оплата прострочена', 'finance', 'payment_due', True),
 ('viewing appointment', 'housing', 'viewing', True),
 ('Besichtigungstermin', 'housing', 'viewing', True),
 ('просмотр квартиры', 'housing', 'viewing', True),
 ('перегляд квартири', 'housing', 'viewing', True),
 ('verification code', 'security', 'verification', False),
 ('Bestätigungscode', 'security', 'verification', False),
 ('код подтверждения', 'security', 'verification', False),
 ('код підтвердження', 'security', 'verification', False),
])
def test_decisive_multilingual_evidence(phrase, topic, subtype, action):
    from mailorganizer.triage import classify
    result = classify('SYNTHETIC ' + phrase, '')
    assert (result['topic'], result['subtype'], result['action_required']) == (topic, subtype, action)
    assert result['evidence'] == [{'source_id': 'subject', 'quote': phrase}]
    assert result['needs_review'] is True

@pytest.mark.parametrize('phrase', ['invite you to an interview', 'Einladung zum Vorstellungsgespräch', 'приглашаем на собеседование', 'запрошуємо на співбесіду'])
def test_interview_requires_human_date_review(phrase):
    from mailorganizer.triage import classify
    result = classify('SYNTHETIC', phrase)
    assert (result['topic'], result['subtype'], result['action_required'], result['needs_review']) == ('jobs', 'interview', True, True)
    assert result['evidence'] == [{'source_id': 'body', 'quote': phrase}]


def test_unknown_requires_review():
    from mailorganizer.triage import classify
    assert classify('SYNTHETIC hello', 'No decisive content.') == {
        'topic': 'other', 'subtype': 'unknown', 'action_required': False,
        'needs_review': True, 'evidence': []}
