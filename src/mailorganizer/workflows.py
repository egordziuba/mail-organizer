"""Offline evidence-led proposals. No provider, credential, model or calendar IO."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import re
import html
import json
from pathlib import Path


def _md(value):
    text = re.sub(r'[\r\n\t]+', ' ', str(value))
    for char in ('\\', '`', '*', '_', '[', ']', '(', ')', '#', '!'):
        text = text.replace(char, '\\' + char)
    return html.escape(text, quote=True).replace('|', '&#124;')


def export_workflows(root: Path, workflows) -> dict:
    """Write only four derived artifacts at fixed paths below the caller's root."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    paths = dict(workflows=root/'workflows.json', actions=root/'ACTIONS.md', cases=root/'CASES.md', calendar_proposals=root/'calendar_proposals.json')
    # Check all destinations before writing any; never follow existing symlinks.
    if any(path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_nlink != 1)) for path in paths.values()):
        raise ValueError('derived destination must not be a link or nonregular file')
    paths['workflows'].write_text(json.dumps(workflows, ensure_ascii=False, sort_keys=True, indent=2)+'\n', encoding='utf-8')
    paths['calendar_proposals'].write_text(json.dumps(workflows['calendar_proposals'], ensure_ascii=False, sort_keys=True, indent=2)+'\n', encoding='utf-8')
    cases = ['# Дела — наблюдения, не подтверждение результата', '', '| Message keys | Account | Subject | Topics / subtypes | Status | Latest date |', '|---|---|---|---|---|---|']
    for c in workflows['cases']:
        cells = [', '.join(c['source_message_keys']), c['account_id'], c.get('subject',''), ', '.join(c['topics'] + c['subtypes']), c['status'], c['latest_date'] or 'unknown']
        cases.append('| '+' | '.join(_md(x) for x in cells)+' |')
    actions = ['# Действия — предложения для проверки', '', 'Никаких внешних изменений. archive_no_action сохраняет источник для решения пользователя.', '', '| Message key | Topic / subtype | Status / disposition | Time | Exact evidence |', '|---|---|---|---|---|']
    for a in workflows['actions']:
        cells = [a['source_message_key'], a['topic']+' / '+a['subtype'], a['status']+' / '+a['disposition'], a['temporal_state']+' / '+', '.join(a['raw_deadline_quotes']), '; '.join(s['source_id']+': '+s['quote'] for s in a['sources'])]
        actions.append('| '+' | '.join(_md(x) for x in cells)+' |')
    paths['cases'].write_text('\n'.join(cases)+'\n', encoding='utf-8')
    paths['actions'].write_text('\n'.join(actions)+'\n', encoding='utf-8')
    return {key:str(path) for key,path in paths.items()}


TOPICS = {'jobs', 'university', 'finance', 'confirmations', 'housing', 'security', 'other'}

def _date(record):
    try:
        stamp = record.get('provider_metadata', {}).get('internalDate')
        if stamp is not None:
            return datetime.fromtimestamp(int(stamp) / 1000, timezone.utc)
        value = parsedate_to_datetime(record.get('date', ''))
        return value.astimezone(timezone.utc) if value.tzinfo else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None

def _evidence(record):
    sources = {'body': record.get('body', ''), 'subject': record.get('subject', '')}
    for part in record.get('evidence_parts', []):
        sources[part.get('source_id')] = part.get('text', '')
    for att in record.get('attachments', []):
        extraction = att.get('extraction', {})
        for part in extraction.get('parts', []):
            sources[f"attachment:{att.get('sha256', '')}:{part.get('source_id', '')}"] = part.get('text', '')
    return [dict(source_message_key=record['message_key'], source_id=e['source_id'], quote=e['quote']) for e in record.get('evidence', []) if isinstance(e, dict) and isinstance(e.get('quote'), str) and e['quote'] and e['quote'] in sources.get(e.get('source_id'), '')]

def _groups(records):
    by_key = {r['message_key']:r for r in records}
    parents = {key:key for key in by_key}
    def root(key):
        while parents[key] != key:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key
    def join(a, b):
        a, b = root(a), root(b)
        parents[max(a, b)] = min(a, b)
    threads, ids = {}, {}
    for r in records:
        key, account = r['message_key'], r['account_id']
        thread = r.get('provider_metadata', {}).get('threadId')
        if thread:
            identity = (account, str(thread))
            if identity in threads:
                join(key, threads[identity])
            threads[identity] = key
        headers = {k.lower(): str(v) for k, v in r.get('headers', {}).items()}
        mid = re.fullmatch(r'\s*(<[^<>\s]+>)\s*', headers.get('message-id', ''))
        if mid:
            ids.setdefault((account, mid[1]), []).append(key)
    for r in records:
        # Never bridge distinct provider threads using weaker RFC headers.
        if r.get('provider_metadata', {}).get('threadId'):
            continue
        headers = {k.lower(): str(v) for k, v in r.get('headers', {}).items()}
        for ref in re.findall(r'<[^<>\s]+>', headers.get('references', '') + ' ' + headers.get('in-reply-to', '')):
            candidates = ids.get((r['account_id'], ref), [])
            if len(candidates) == 1:
                target = by_key[candidates[0]]
                if not target.get('provider_metadata', {}).get('threadId'):
                    join(r['message_key'], candidates[0])
    groups = {}
    for r in records:
        groups.setdefault(root(r['message_key']), []).append(r)
    return groups

def _ics_datetime(prop):
    head, value, raw = prop
    params = dict(p.split('=', 1) for p in head.split(';')[1:] if '=' in p)
    if params.get('VALUE') == 'DATE':
        return datetime.strptime(value, '%Y%m%d').date(), True
    if not re.fullmatch(r'\d{8}T\d{6}Z?', value):
        raise ValueError('invalid_date')
    dt = datetime.strptime(value.rstrip('Z'), '%Y%m%dT%H%M%S')
    if value.endswith('Z'):
        return dt.replace(tzinfo=timezone.utc), False
    tzid = params.get('TZID', '').strip('"')
    if not tzid:
        raise ValueError('missing_timezone')
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        zone = ZoneInfo(tzid)
    except ZoneInfoNotFoundError:
        raise ValueError('unsupported_timezone') from None
    # A DST fold/gap has no unambiguous wall time; never pick one silently.
    first, second = dt.replace(tzinfo=zone, fold=0), dt.replace(tzinfo=zone, fold=1)
    if first.utcoffset() != second.utcoffset():
        raise ValueError('ambiguous_local_time')
    return first, False


def _calendar(record, now):
    proposals = []
    for att in record.get('attachments', []):
        extraction = att.get('extraction', {})
        text = extraction.get('text', att.get('calendar_text', ''))
        name = att.get('name', att.get('original_filename', att.get('original_name', '')))
        if not (str(att.get('mime', '')).split(';')[0]=='text/calendar' or name.lower().endswith('.ics')) or not text:
            continue
        lines = []
        for line in text.splitlines():
            if line.startswith((' ', '\t')) and lines:
                lines[-1] += line[1:]
            else:
                lines.append(line)
        method = next((line.split(':',1)[1] for line in lines if line.startswith('METHOD:')), '')
        events, current = [], None
        for line in lines:
            if line == 'BEGIN:VEVENT':
                current = []
            elif line == 'END:VEVENT' and current is not None:
                events.append(current); current = None
            elif current is not None:
                current.append(line)
        if not events:
            events = [[]]
        for index, event in enumerate(events):
            props = {}
            for line in event:
                if ':' not in line:
                    continue
                head, value = line.split(':', 1)
                props.setdefault(head.split(';')[0].upper(), []).append((head, value, line))
            def value(name):
                return props.get(name, [('', '', '')])[0][1]
            reasons = []
            if lines.count('BEGIN:VCALENDAR') != 1 or lines.count('END:VCALENDAR') != 1 or lines.count('BEGIN:VEVENT') != lines.count('END:VEVENT') or not event:
                reasons.append('invalid_calendar_structure')
            for field in ('UID','DTSTART','DTEND','SUMMARY','STATUS'):
                if len(props.get(field, []))>1:
                    reasons.append('duplicate_' + field.lower())
            if not value('UID'):
                reasons.append('missing_uid')
            if 'RRULE' in props or 'RDATE' in props or 'EXDATE' in props or 'RECURRENCE-ID' in props:
                reasons.append('unsupported_rrule')
            cancel = method.upper()=='CANCEL' or value('STATUS').upper()=='CANCELLED'
            if cancel:
                reasons.append('cancellation')
            start, end, all_day = None, None, False
            for field in ('DTSTART','DTEND'):
                if field not in props:
                    if field=='DTSTART':
                        reasons.append('missing_dtstart')
                    continue
                if len(props[field]) != 1:
                    continue
                try:
                    parsed, day = _ics_datetime(props[field][0])
                    if field=='DTSTART':
                        start, all_day = parsed, day
                    else:
                        end = parsed
                except ValueError as exc:
                    reason = str(exc)
                    reasons.append(reason if reason in {'missing_timezone','unsupported_timezone','ambiguous_local_time','invalid_date'} else 'invalid_date')
            if end is None and not all_day:
                reasons.append('missing_dtend')
            if start is not None and end is not None:
                if type(start)!=type(end) or end <= start:
                    reasons.append('invalid_interval')
            temporal = 'unknown'
            if start is not None:
                temporal = 'past' if (start < now.date() if all_day else start < now) else 'future'
            if not att.get('sha256'):
                reasons.append('missing_attachment_sha256')
            if extraction.get('status') not in (None, 'extracted') or extraction.get('complete') is False:
                reasons.append('incomplete_extraction')
            proposals.append(dict(proposal_id=record['message_key'] + ':ics:' + str(index) + ':' + str(att.get('sha256','')), source_message_key=record['message_key'], source_attachment_sha256=att.get('sha256'), raw_date_quote=props.get('DTSTART',[('','','')])[0][2], raw_date_quotes=[p[2] for p in props.get('DTSTART',[]) + props.get('DTEND',[])], uid=value('UID'), summary=value('SUMMARY'), description=value('DESCRIPTION'), method=method, event_status=value('STATUS'), timezone=(props.get('DTSTART',[('','','')])[0][0]), all_day=all_day, start=start.isoformat() if start is not None else None, end=end.isoformat() if end is not None else None, state='proposed', status='requires_review' if reasons else 'proposed', review_reasons=sorted(set(reasons)), proposed_kind='calendar_update' if cancel else 'event', operation='review_cancellation' if cancel else 'review_create', temporal_state=temporal, calendar_write_allowed=False, reminder_allowed=False))
    return proposals


def _body_candidates(record, now):
    candidates = []
    pattern = r'\b(?:Frist(?:\s+bis)?|bis|deadline(?:\s+(?:is|on))?)\s*:?\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\b'
    for index, match in enumerate(re.finditer(pattern, record.get('body', ''), re.I)):
        temporal = 'unknown'
        try:
            day = datetime(int(match[3]), int(match[2]), int(match[1])).date()
            temporal = 'past' if day < now.date() else 'future'
        except ValueError:
            pass
        candidates.append(dict(proposal_id=record['message_key']+':body:'+str(index), source_message_key=record['message_key'], source_id='body', raw_date_quote=match[0], proposed_kind='unknown', state='proposed', status='requires_review', review_reasons=['body_date_not_calendar_event'], start=None, end=None, temporal_state=temporal, calendar_write_allowed=False, reminder_allowed=False))
    return candidates


def build_workflows(records, *, now=None):
    """Produce derived observations only; records and their nested values stay untouched."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('now must be timezone-aware')
    records = sorted(list(records), key=lambda r:r['message_key'])
    if len({r['message_key'] for r in records}) != len(records):
        raise ValueError('duplicate message_key')
    cases, actions, proposals, queue = [], [], [], []
    for key, group in sorted(_groups(records).items()):
        dates = [_date(r) for r in group]; dates = [d for d in dates if d]
        conflict = any(r.get('flags') or (r.get('action_required') is True and not _evidence(r)) for r in group) or len({r.get('subtype') for r in group}) > 1
        all_archived = all(set(r.get('provider_metadata', {}).get('labelIds', r.get('labels', []))) & {'SPAM','TRASH'} for r in group)
        cases.append(dict(case_id=key, account_id=group[0]['account_id'], source_message_keys=[r['message_key'] for r in group], subject=group[0].get('subject',''), topics=sorted({r.get('topic') if r.get('topic') in TOPICS else 'other' for r in group}), subtypes=sorted({r.get('subtype','unknown') for r in group}), status='requires_review' if conflict else 'observed', disposition='archive_no_action' if all_archived else 'active_observation', latest_date=max(dates).isoformat() if dates else None))
        for r in group:
            labels = set(r.get('provider_metadata', {}).get('labelIds', r.get('labels', [])))
            outgoing, archived = 'SENT' in labels, bool(labels & {'SPAM','TRASH'})
            candidates = _body_candidates(r, now)
            if not outgoing and not archived and r.get('topic') != 'security':
                calendar = _calendar(r, now)
                if conflict:
                    for p in calendar:
                        p['status'] = 'requires_review'
                        p['review_reasons'] = sorted(set(p['review_reasons'] + ['case_conflict']))
                proposals.extend(calendar)
                if r.get('action_required') is True:
                    proposals.extend(candidates)
            evidence = _evidence(r)
            requested = r.get('action_required') is True
            if requested and not outgoing and evidence:
                actions.append(dict(action_id=r['message_key'], case_id=key, source_message_key=r['message_key'], topic=r.get('topic','other'), subtype=r.get('subtype','unknown'), status='requires_review' if conflict or archived else 'proposed', disposition='archive_no_action' if archived else 'incoming_request', deadline=None, temporal_state=candidates[0]['temporal_state'] if len(candidates)==1 else 'unknown', raw_deadline_quotes=[p['raw_date_quote'] for p in candidates], sources=evidence))
            if not outgoing and not archived and r.get('topic') in {'jobs','university','finance'} and (requested or r.get('flags') or r.get('subtype')=='unknown'):
                queue.append(dict(message_key=r['message_key'], reason='ambiguous_evidence' if conflict or not evidence else 'explicit_request', priority=1 if requested else 2, task_type='evidence_review'))
    accounts = {r['message_key']:r['account_id'] for r in records}
    uid_groups = {}
    for p in proposals:
        if p.get('uid'):
            uid_groups.setdefault((accounts[p['source_message_key']], p['uid']), []).append(p)
    for same_uid in uid_groups.values():
        signatures = {(p['start'], p['end'], p.get('event_status'), p.get('method')) for p in same_uid}
        if len(signatures)>1:
            for p in same_uid:
                p['status']='requires_review'
                p['review_reasons']=sorted(set(p['review_reasons']+['conflicting_uid']))
    return dict(schema_version=1, cases=cases, actions=actions, calendar_proposals=proposals, model_queue=sorted(queue,key=lambda q:(q['priority'],q['message_key'])), metrics=dict(records=len(records), cases=len(cases), actions=len(actions), calendar_proposals=len(proposals), model_tasks=len(queue)))
