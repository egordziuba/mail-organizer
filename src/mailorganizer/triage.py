"""Conservative deterministic local triage; never schedules anything."""
import re

RULES = [
    ('security', 'verification', False, r'verification code|Bestätigungscode|код подтверждения|код підтвердження'),
    ('jobs', 'application_received', False, r'Bewerbung[ -]?Eingangsbestätigung|(?:Wir haben )?Ihre Bewerbung erhalten'),
    ('jobs', 'rejected', False, r'Absage erteilen|application (?:was |has been )?rejected'),
    ('university', 'documents_requested', True, r'fehlende Unterlagen|reichen Sie die Dokumente'),
    ('finance', 'payment_due', True, r'Bitte zahlen Sie die Rechnung|Rechnung: Zahlungsfrist'),
    ('confirmations', 'receipt', False, r'Anmeldebestätigung|Ihre Anmeldung wurde bestätigt'),
    ('jobs', 'interview', True, r'invite you to an interview|Einladung zum Vorstellungsgespräch|приглашаем на собеседование|запрошуємо на співбесіду'),
    ('university', 'admission', True, r'offer of admission|Zulassungsbescheid|вы зачислены|вас зараховано'),
    ('finance', 'payment_due', True, r'payment overdue|Zahlung fällig|оплата просрочена|оплата прострочена'),
    ('housing', 'viewing', True, r'viewing appointment|Besichtigungstermin|просмотр квартиры|перегляд квартири'),
    ('confirmations', 'receipt', False, r'(?:We )?received your application|Eingang Ihrer Bewerbung bestätigt|заявка получена|заявку отримано'),
]

def classify(subject: str, body: str, sender: str = '') -> dict:
    hits = []
    for topic, subtype, action, pattern in RULES:
        for source_id, text in [('subject', subject), ('body', body)]:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                # Negation in the same nearby clause invalidates a positive signal.
                prefix = re.split(r'[.!?\n;]', text[:match.start()])[-1][-80:]
                if re.search(r'\b(?:not|no|cannot|never|kein\w*|nicht|не|нет|без)\b', prefix, re.I):
                    continue
                hits.append((topic, subtype, action, {'source_id': source_id, 'quote': match.group()}))
    if not hits:
        return dict(topic='other', subtype='unknown', action_required=False, needs_review=True, evidence=[])
    topic, subtype, action, _ = hits[0]
    return dict(topic=topic, subtype=subtype, action_required=any(h[2] for h in hits),
                needs_review=True, evidence=[h[3] for h in hits])
