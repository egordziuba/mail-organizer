"""Synthetic fixtures only: deterministic evidence-backed mail analysis."""
from copy import deepcopy
import pytest


@pytest.mark.parametrize('date_text,expected', [('2026-10-22', '2026-10-22'), ('22.10.2026', '2026-10-22'), ('22. Oktober 2026', '2026-10-22')])
def test_explicit_deadline_is_review_only_all_day_with_exact_quote(date_text, expected):
    row = message(body=f'Bitte zahlen Sie die Rechnung bis zum {date_text}.')
    result = run([row])
    proposal = result['calendar_proposals'][0]
    assert proposal['kind'] == 'deadline_all_day'
    assert proposal['fields']['date'] == expected
    assert proposal['fields']['start_time'] is None
    assert proposal['fields']['end_time'] is None
    assert proposal['fields']['timezone'] is None
    assert proposal['proposed_timezone'] == 'Europe/Berlin'
    assert proposal['stage'] == 'proposed' and proposal['requires_review'] is True
    assert date_text in [e['quote'] for e in proposal['evidence']]
    assert result['calendar_modified'] is False


def test_meeting_time_no_invented_duration_and_unknown_timezone_review():
    result = run([message(body='We invite you to an interview on 2026-10-20 at 14:30.')])
    p = result['calendar_proposals'][0]
    assert p['kind'] == 'meeting'
    assert p['fields']['start_time'] == '14:30'
    assert p['fields']['end_time'] is None
    assert set(p['review_reasons']) >= {'missing_end_time', 'unknown_timezone'}
    assert any(e['quote'] == '14:30' for e in p['evidence'])


def test_meeting_explicit_end_time_and_timezone_are_evidenced():
    result = run([message(body='We invite you to an interview on 20.10.2026 from 14:30 to 15:15 Europe/Berlin.')])
    p = result['calendar_proposals'][0]
    assert p['fields'] == {'date': '2026-10-20', 'start_time': '14:30', 'end_time': '15:15', 'timezone': 'Europe/Berlin'}
    assert any(e['quote'] == 'Europe/Berlin' for e in p['evidence'])


def test_past_deadline_remains_history_not_upcoming_proposal():
    result = run([message(body='Bitte zahlen Sie die Rechnung bis zum 01.10.2026.')])
    assert not result['calendar_proposals']
    observed = result['cases'][0]['date_observations'][0]
    assert observed['temporal_status'] == 'past'
    assert observed['evidence']


@pytest.mark.parametrize('date_text,reason', [('31.02.2026', 'invalid_date'), ('22.10.', 'missing_year'), ('tomorrow', 'relative_date_requires_review')])
def test_uncertain_dates_do_not_become_calendar_proposals(date_text, reason):
    result = run([message(body=f'Please send the missing documents by {date_text}.')])
    assert not result['calendar_proposals']
    review = next(r for r in result['review_items'] if r['reason'] == reason)
    assert review['evidence'][0]['quote'] == date_text
    if date_text == 'tomorrow':
        assert review['suggested_date'] == '2026-10-02'
        assert review['anchor_evidence']['source_id'] == 'date'


def test_dates_unrelated_to_actions_and_self_promises_not_scheduled():
    result = run([message(body='Please send the missing documents. Our company was founded on 2026-10-20.')])
    assert not result['calendar_proposals']
    result = run([message(body='I will send the missing documents by 2026-10-20.')])
    assert not result['action_items']
    assert not result['calendar_proposals']


@pytest.mark.parametrize('meta', [{'labelIds': ['SENT']}, {'role': 'outgoing'}, {'role': 'sent'}])
def test_outgoing_request_is_not_a_recipient_action(meta):
    result = run([message(body='Please send the missing documents by 2026-10-20.')],
                 provider_metadata={('gmail-1', 'm1'): meta})
    assert not result['action_items']
    assert not result['calendar_proposals']
    assert result['cases'][0]['evidence']


def test_self_promise_containing_rule_phrase_is_not_action():
    result = run([message(body='I will send the fehlende Unterlagen by 2026-10-20.')])
    assert not result['action_items']
    assert not result['calendar_proposals']


@pytest.mark.parametrize('separator', ['. ', '; ', '\n'])
def test_same_source_conflicting_requests_do_not_propose_two_dates(separator):
    result = run([message(body=f'Please send the missing documents by 2026-10-20{separator}Please send the missing documents by 2026-10-22.')])
    assert not result['calendar_proposals']
    assert any(r['reason'] == 'ambiguous_multiple_dates' for r in result['review_items'])


def test_record_metadata_and_date_header_are_supported_without_mutation():
    row = message(body='Please send the missing documents by tomorrow.')
    row.pop('date')
    row['headers'] = {'Date': 'Thu, 1 Oct 2026 23:30:00 -0400'}
    row['metadata'] = {'threadId': 'embedded'}
    original = deepcopy(row)
    result = run([row])
    review = next(r for r in result['review_items'] if r['reason'] == 'relative_date_requires_review')
    assert review['suggested_date'] == '2026-10-02'
    assert review['anchor_evidence']['quote'] == row['headers']['Date']
    assert result['cases'][0]['case_id'] == 'gmail-1:thread:embedded'
    assert row == original
    row['metadata']['role'] = 'outgoing'
    assert not run([row])['action_items']


def test_unknown_status_goes_to_human_model_queue_without_calendar_guess():
    result = run([message(body='Your appointment is next Wednesday.')])
    assert result['review_items'][0]['reason'] == 'unknown_status'
    assert result['model_candidates'][0]['task'] == 'review_observed_case'
    assert not result['calendar_proposals']


def test_multiple_dates_in_same_request_are_not_arbitrarily_selected():
    result = run([message(body='Please send the missing documents by 2026-10-20 or 2026-10-22.')])
    assert not result['calendar_proposals']
    assert any(r['reason'] == 'ambiguous_multiple_dates' for r in result['review_items'])


@pytest.mark.parametrize('body,category,status,action', [
    ('Wir haben Ihre Bewerbung erhalten.', 'jobs', 'application_received', False),
    ('Leider müssen wir Ihnen eine Absage erteilen.', 'jobs', 'rejected', False),
    ('Приглашаем на собеседование.', 'jobs', 'interview', True),
    ('Запрошуємо на співбесіду.', 'jobs', 'interview', True),
    ('Ми отримали вашу заявку.', 'jobs', 'application_received', False),
    ('Ваша заявка отклонена.', 'jobs', 'rejected', False),
    ('We offer you the position.', 'jobs', 'offer', True),
    ('University: please submit the missing documents.', 'university', 'documents_requested', True),
    ('Hochschule: Bitte reichen Sie die Dokumente ein.', 'university', 'documents_requested', True),
    ('Вас зараховано до університету.', 'university', 'enrollment_confirmed', False),
    ('Вы зачислены в университет.', 'university', 'enrollment_confirmed', False),
    ('University: We received your application.', 'university', 'application_received', False),
    ('University: Please pay the tuition fee.', 'university', 'payment_due', True),
    ('Bitte zahlen Sie die Rechnung.', 'finance', 'payment_due', True),
    ('Оплата просрочена.', 'finance', 'payment_due', True),
    ('Оплата прострочена.', 'finance', 'payment_due', True),
    ('Ihre Anmeldung wurde bestätigt.', 'confirmations', 'receipt', False),
    ('Код підтвердження: 123456.', 'security', 'verification', False),
])
def test_multilingual_observed_states(body, category, status, action):
    result = run([message(body=body)])
    case = result['cases'][0]
    assert (case['category'], case['status']) == (category, status)
    assert bool(result['action_items']) is action
    assert all(e['quote'] in body for e in case['evidence'])


def test_attachment_pages_and_external_documents_have_exact_evidence():
    row = message(body='See attached documents.')
    row['attachments'] = [{'sha256': 'abc', 'parts': [{'source_id': 'page:2', 'text': 'Bitte zahlen Sie die Rechnung bis zum 22.10.2026.'}]}]
    docs = [{'account_id': 'gmail-1', 'message_id': 'm1', 'attachment_id': 'def',
             'parts': [{'source_id': 'page:1', 'text': 'Rechnung: Zahlungsfrist'}]}]
    result = run([row], document_records=docs)
    assert result['cases'][0]['category'] == 'finance'
    sources = {e['source_id'] for e in result['action_items'][0]['evidence']}
    assert sources == {'attachment:abc:page:2', 'attachment:def:page:1'}
    assert result['summaries']['documents'] == 2
    assert result['cases'][0]['institution'] is None
    assert result['cases'][0]['sender_verified'] is False


def message(mid='m1', account='gmail-1', body='We received your application.', **extra):
    return dict(account_id=account, message_id=mid, message_key=f'{account}:{mid}',
                subject='Application', body=body, sender='Example Recruiting <hr@example.test>',
                date='Thu, 1 Oct 2026 09:00:00 +0200', attachments=[], **extra)


def run(rows, **kwargs):
    from mailorganizer.mail_analysis import analyze
    return analyze(rows, now='2026-10-10T12:00:00+02:00', **kwargs)


def test_receipts_counts_account_scope_and_immutable_raw_fields():
    rows = [message(), message(account='gmail-2')]
    original = deepcopy(rows)
    result = run(rows)
    assert rows == original
    assert result['summaries']['messages'] == 2
    assert result['summaries']['accounts'] == 2
    assert result['categories']['jobs']['total'] == 2
    assert len(result['cases']) == 2
    assert all(c['status'] == 'application_received' for c in result['cases'])
    assert result['cases'][0]['evidence'][0]['quote'] == 'We received your application'
    assert result['cases'][0]['evidence'][0]['source_id'] == 'body'
    assert result['cases'][0]['evidence'][0]['message_id'] == 'm1'
    assert result['calendar_modified'] is False
    assert not result['action_items']
    assert result['coverage']['complete_archive'] is False


def test_thread_latest_rejection_overrides_invitation_and_quote_history():
    invite = message('old', body='We invite you to an interview on 2026-10-20.')
    latest = message('new', body='Your application was rejected.\nOn Thu, HR wrote:\nWe invite you to an interview on 2026-10-20.')
    invite['date'] = '2026-10-01T09:00:00+02:00'
    latest['date'] = '2026-10-09T09:00:00+02:00'
    latest['subject'] = 'Re: Invitation to interview'
    metadata = {('gmail-1', mid): {'threadId': 'thread-A'} for mid in ('old', 'new')}
    result = run([latest, invite], provider_metadata=metadata)
    assert len(result['cases']) == 1
    case = result['cases'][0]
    assert case['status'] == 'rejected'
    assert case['latest_message_id'] == 'new'
    assert len(case['history']) == 2
    assert not result['action_items']
    assert not result['calendar_proposals']
    assert all(e['message_id'] == 'new' for e in case['evidence'])


def test_same_subject_never_merges_employers_and_same_thread_never_merges_accounts():
    rows = [message('a'), message('b'), message('c', account='gmail-2')]
    metadata = {'gmail-1': {'a': {'threadId': 'same'}}, 'gmail-2': {'c': {'threadId': 'same'}}}
    assert len(run(rows, provider_metadata=metadata)['cases']) == 3


def test_deleted_spam_trash_and_newsletters_remain_visible_but_not_important():
    rows = [message(str(i), body='Please send the missing documents for your application.') for i in range(4)]
    rows[3]['body'] = 'Newsletter: unsubscribe. Please send the missing documents for your application.'
    metadata = {'gmail-1': {'0': {'source_deleted': True}, '1': {'labelIds': ['SPAM']}, '2': {'labelIds': ['TRASH']}}}
    result = run(rows, provider_metadata=metadata)
    assert result['summaries']['messages'] == 4
    assert result['summaries']['active_important'] == 0
    assert not result['action_items']
    assert not result['model_candidates']
    assert sum(c['total'] for c in result['categories'].values()) == 4
    assert any(c['category'] == 'newsletters' for c in result['cases'])


def test_conflicting_current_clauses_require_review_not_action():
    result = run([message(body='We invite you to an interview. Your application was rejected.')])
    assert result['cases'][0]['status'] == 'unknown'
    assert result['cases'][0]['needs_review']
    assert result['review_items'][0]['reason'] == 'conflicting_current_status'
    assert len(result['review_items'][0]['evidence']) == 2
    assert not result['action_items']
    assert result['model_candidates'][0]['case_id'] == result['cases'][0]['case_id']
