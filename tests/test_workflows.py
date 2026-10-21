"""Синтетические проверки: никаких провайдеров и реальной почты."""
from copy import deepcopy
from datetime import datetime, timezone
import pytest
from mailorganizer.workflows import build_workflows
import json


def test_body_dates_are_raw_review_candidates_and_ocr_never_invents_time():
    r = record()
    r.update(body='Bitte reichen Sie Unterlagen bis 12.10.2026 ein.', topic='university', subtype='documents_requested', action_required=True, evidence=[{'source_id':'body', 'quote':'bis 12.10.2026'}])
    out = build_workflows([r], now=NOW)
    p = out['calendar_proposals'][0]
    assert p['proposed_kind']=='unknown' and p['start'] is None
    assert p['raw_date_quote']=='bis 12.10.2026' and p['status']=='requires_review'
    assert out['actions'][0]['temporal_state']=='future'
    past = deepcopy(r); past['body']=past['body'].replace('12.10.2026','12.10.2020'); past['evidence'][0]['quote']='bis 12.10.2020'
    assert build_workflows([past], now=NOW)['actions'][0]['temporal_state']=='past'
    r['body']=''; r['attachments']=[{'name':'scan.doc','extraction':{'status':'needs_ocr','text':''}}]
    assert build_workflows([r], now=NOW)['calendar_proposals']==[]


def test_archived_sent_security_no_calendar_or_body_model_dump():
    for labels, topic in [(['SENT'],'jobs'),(['TRASH'],'finance'),(['SPAM'],'jobs'),([], 'security')]:
        r = ics_record(); r['provider_metadata']={'labelIds':labels}; r['topic']=topic
        r['action_required']=True
        out = build_workflows([r], now=NOW)
        assert out['calendar_proposals']==[] and out['model_queue']==[]


def test_export_only_four_derived_safe_files(tmp_path):
    from mailorganizer.workflows import export_workflows
    r = record(); r.update(subject='<script>x</script>|\n[link](https://evil)', action_required=True)
    out = build_workflows([r], now=NOW)
    before = deepcopy(out)
    paths = export_workflows(tmp_path, out)
    assert set(p.name for p in tmp_path.iterdir())=={'workflows.json','ACTIONS.md','CASES.md','calendar_proposals.json'}
    assert set(paths)=={'workflows','actions','cases','calendar_proposals'}
    assert json.loads((tmp_path/'workflows.json').read_text(encoding='utf8'))==out
    md = (tmp_path/'CASES.md').read_text(encoding='utf8')
    assert '<script>' not in md and '[link](' not in md
    assert '&#124;' in md and '&lt;script&gt;' in md
    assert 'm1' in md and out==before


def test_naive_now_rejected_and_rfc2822_date_used():
    with pytest.raises(ValueError, match='timezone-aware'):
        build_workflows([], now=datetime(2026,1,1))
    out = build_workflows([record(date='Fri, 9 Oct 2026 12:00:00 +0200')],now=NOW)
    assert out['cases'][0]['latest_date']=='2026-10-09T10:00:00+00:00'



def test_calendar_attachment_identity_and_structure_are_not_invented():
    r=ics_record(); att=r['attachments'][0]
    att['original_filename']='calendar.ics'; att.pop('name'); att['mime']='application/octet-stream'
    assert len(build_workflows([r],now=NOW)['calendar_proposals'])==1
    att['extraction']['text']=att['extraction']['text'].replace('BEGIN:VCALENDAR\n','')
    p=build_workflows([r],now=NOW)['calendar_proposals'][0]
    assert p['status']=='requires_review' and 'invalid_calendar_structure' in p['review_reasons']


def test_invalid_evidence_marks_case_review_and_archived_receipt_visible():
    r=record(); r.update(action_required=True,evidence=[{'source_id':'body','quote':'Invented'}])
    assert build_workflows([r],now=NOW)['cases'][0]['status']=='requires_review'
    r=record(provider_metadata={'labelIds':['TRASH']})
    assert build_workflows([r],now=NOW)['cases'][0]['disposition']=='archive_no_action'


def test_conflicting_thread_and_calendar_uid_require_review():
    a = ics_record(); a['flags']=['quoted_invite_new_rejection']
    p = build_workflows([a], now=NOW)['calendar_proposals'][0]
    assert p['status']=='requires_review' and 'case_conflict' in p['review_reasons']
    a = ics_record(); b = ics_record('DTSTART:20261012T100000Z\nDTEND:20261012T110000Z')
    b['message_key']='second'
    out = build_workflows([a,b],now=NOW)
    assert all(p['status']=='requires_review' and 'conflicting_uid' in p['review_reasons'] for p in out['calendar_proposals'])
    b['account_id']='gmail-b'
    assert all(p['status']=='proposed' for p in build_workflows([a,b],now=NOW)['calendar_proposals'])


def test_export_refuses_hardlink_to_original(tmp_path):
    import os
    from mailorganizer.workflows import export_workflows
    original = tmp_path/'original.eml'; original.write_text('original',encoding='utf8')
    output = tmp_path/'derived'; output.mkdir()
    os.link(original, output/'workflows.json')
    with pytest.raises(ValueError, match='link'):
        export_workflows(output,build_workflows([],now=NOW))
    assert original.read_text(encoding='utf8')=='original'


def test_unread_and_unknown_security_are_not_requests():
    rows = [record('unread', provider_metadata={'labelIds':['UNREAD']}), record('security')]
    rows[1].update(topic='security', subtype='unknown', action_required=False)
    assert build_workflows(rows,now=NOW)['actions']==[]
    assert build_workflows(rows,now=NOW)['model_queue']==[]


def ics_record(properties='DTSTART:20261011T100000Z\nDTEND:20261011T110000Z', method='REQUEST', uid='UID:event-1\n'):
    text = f'BEGIN:VCALENDAR\nVERSION:2.0\nMETHOD:{method}\nBEGIN:VEVENT\n{uid}SUMMARY:Inter\n view\n{properties}\nEND:VEVENT\nEND:VCALENDAR'
    return record('ics', attachments=[{'name':'invite.ics','mime':'text/calendar','sha256':'abc123','extraction':{'status':'extracted','text':text,'parts':[{'source_id':'calendar','text':text}]}}])


def test_ics_proposals_preserve_sources_aware_time_and_past():
    r = ics_record()
    out = build_workflows([r], now=NOW)
    p = out['calendar_proposals'][0]
    assert p['status'] == p['state'] == 'proposed'
    assert p['calendar_write_allowed'] is False
    assert p['source_message_key'] == 'ics' and p['source_attachment_sha256']=='abc123'
    assert p['raw_date_quote'] == 'DTSTART:20261011T100000Z'
    assert p['start'] == '2026-10-11T10:00:00+00:00' and p['end']=='2026-10-11T11:00:00+00:00'
    assert p['summary'] == 'Interview' and p['temporal_state']=='future'
    past = build_workflows([ics_record('DTSTART:20200101T100000Z\nDTEND:20200101T110000Z')], now=NOW)['calendar_proposals'][0]
    assert past['temporal_state']=='past' and past['reminder_allowed'] is False


@pytest.mark.parametrize('properties,uid,method,reason', [
    ('DTSTART:20261011T100000\nDTEND:20261011T110000','UID:x\n','REQUEST','missing_timezone'),
    ('DTSTART:20261311T100000Z\nDTEND:20261311T110000Z','UID:x\n','REQUEST','invalid_date'),
    ('DTSTART:20261011T100000Z\nDTSTART:20261012T100000Z\nDTEND:20261011T110000Z','UID:x\n','REQUEST','duplicate_dtstart'),
    ('DTSTART:20261011T100000Z\nDTEND:20261011T110000Z\nRRULE:FREQ=DAILY','UID:x\n','REQUEST','unsupported_rrule'),
    ('DTSTART:20261011T100000Z\nDTEND:20261011T110000Z','','REQUEST','missing_uid'),
    ('DTSTART:20261011T100000Z','UID:x\n','REQUEST','missing_dtend'),
    ('DTSTART:20261011T100000Z\nDTEND:20261011T110000Z','UID:x\n','CANCEL','cancellation'),
    ('DTSTART:20261011T100000Z\nDTEND:20261011T110000Z\nSTATUS:CANCELLED','UID:x\n','REQUEST','cancellation'),
])
def test_ics_invalid_or_cancelled_never_authorizes_calendar(properties, uid, method, reason):
    p = build_workflows([ics_record(properties, method, uid)], now=NOW)['calendar_proposals'][0]
    assert p['status']=='requires_review' and reason in p['review_reasons']
    assert not p['calendar_write_allowed'] and not p['reminder_allowed']
    if reason=='cancellation':
        assert p['proposed_kind']=='calendar_update' and p['operation']=='review_cancellation'


def test_all_day_and_supported_timezone():
    p = build_workflows([ics_record('DTSTART;VALUE=DATE:20261011')], now=NOW)['calendar_proposals'][0]
    assert p['all_day'] and p['start']=='2026-10-11' and p['end'] is None
    assert p['status']=='proposed'
    p = build_workflows([ics_record('DTSTART;TZID=Europe/Berlin:20261011T100000\nDTEND;TZID=Europe/Berlin:20261011T110000')], now=NOW)['calendar_proposals'][0]
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        ZoneInfo('Europe/Berlin')
    except ZoneInfoNotFoundError:
        # Windows without tzdata must fail closed, not invent CET offsets.
        assert p['start'] is None and 'unsupported_timezone' in p['review_reasons']
    else:
        assert p['start']=='2026-10-11T10:00:00+02:00'


NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)

def record(key='m1', **kwargs):
    return dict(message_key=key, account_id='gmail-a', subject='Receipt', body='We received your application', topic='jobs', subtype='application_received', action_required=False, evidence=[{'source_id':'body','quote':'We received your application'}], **kwargs)

def test_empty_and_receipts_are_observed_not_accepted():
    assert build_workflows([], now=NOW)['cases'] == []
    r = record()
    before = deepcopy(r)
    out = build_workflows([r], now=NOW)
    assert out['schema_version'] == 1
    assert out['cases'][0]['status'] == 'observed'
    assert out['cases'][0]['subtypes'] == ['application_received']
    assert out['actions'] == out['calendar_proposals'] == out['model_queue'] == []
    assert r == before

def test_thread_identity_requires_account_or_real_reference():
    rows = [record('a', provider_metadata={'threadId':'t'}), record('b', provider_metadata={'threadId':'t'}), record('c', account_id_other='unused', provider_metadata={'threadId':'t'})]
    rows[2]['account_id'] = 'gmail-b'
    rows += [record('d'), record('e'), record('f', headers={'Message-ID':'<f@x>'}), record('g', headers={'References':'<f@x>', 'Message-ID':'<g@x>'})]
    out = build_workflows(rows, now=NOW)
    groups = [set(c['source_message_keys']) for c in out['cases']]
    assert {'a','b'} in groups and {'f','g'} in groups
    assert {'c'} in groups and {'d'} in groups and {'e'} in groups

def test_only_incoming_evidenced_requests_and_conflicts_are_reviewed():
    rows = []
    for key, labels in [('in', []), ('sent',['SENT']), ('trash',['TRASH']), ('spam',['SPAM'])]:
        r = record(key, provider_metadata={'labelIds':labels, 'internalDate':'1000'})
        r.update(topic='finance', subtype='payment_due', action_required=True, body='Bitte zahlen', evidence=[{'source_id':'body','quote':'Bitte zahlen'}])
        rows.append(r)
    fake = deepcopy(rows[0]); fake.update(message_key='fake', evidence=[{'source_id':'body','quote':'Invented'}]); rows.append(fake)
    conflict = record('conflict', flags=['quoted_invite_new_rejection']); conflict['action_required']=True; rows.append(conflict)
    out = build_workflows(rows, now=NOW)
    assert {a['source_message_key'] for a in out['actions']} == {'in','trash','spam','conflict'}
    assert all(a['deadline'] is None for a in out['actions'])
    assert next(a for a in out['actions'] if a['source_message_key']=='trash')['disposition']=='archive_no_action'
    assert next(c for c in out['cases'] if c['source_message_keys']==['conflict'])['status']=='requires_review'
    assert next(c for c in out['cases'] if c['source_message_keys']==['in'])['latest_date'] == '1970-01-01T00:00:01+00:00'
    assert {q['message_key'] for q in out['model_queue']} == {'in','fake','conflict'}
    assert all(set(q)=={'message_key','reason','priority','task_type'} for q in out['model_queue'])
