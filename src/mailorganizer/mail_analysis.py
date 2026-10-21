"""Pure, conservative registry from supplied mail records; no I/O or models.

Evidence quotes are exact substrings. Counts come from records, not generated
text. Archived history is retained, while only the reliably latest message in
an account-scoped case can yield proposed actions. Nothing modifies calendars.
"""
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta, date
from email.utils import parsedate_to_datetime, parseaddr
import re


RULES = [
    ('security', 'verification', False, r'verification code|Bestätigungscode|код подтверждения|код підтвердження'),
    ('university', 'enrollment_confirmed', False, r'enrollment (?:is )?confirmed|Immatrikulation bestätigt|вы зачислены|вас зараховано'),
    ('university', 'admission', True, r'offer of admission|Zulassungsbescheid'),
    ('jobs', 'application_received', False,
     r'(?:We )?received your application|(?:Wir haben )?Ihre Bewerbung erhalten|Bewerbung[ -]?Eingangsbestätigung|заявка получена|получили вашу заявку|отримали вашу заявку|заявку отримано'),
    ('jobs', 'rejected', False, r'(?:Your )?application (?:was |has been )?rejected|Absage erteilen|заявка отклонена|заявку відхилено'),
    ('jobs', 'interview', True, r'(?:We )?invite you to an interview|Einladung zum Vorstellungsgespräch|приглашаем на собеседование|запрошуємо на співбесіду'),
    ('jobs', 'offer', True, r'(?:We )?offer you the position|job offer|предлагаем вам должность|пропонуємо вам посаду'),
    ('jobs', 'documents_requested', True, r'Please (?:send|submit) the missing documents|reichen Sie die Dokumente|fehlende Unterlagen|предоставьте документы|надайте документи'),
    ('finance', 'payment_due', True, r'Bitte zahlen Sie die Rechnung|Rechnung: Zahlungsfrist|payment overdue|Zahlung fällig|оплата просрочена|оплата прострочена|Please pay the (?:invoice|tuition fee)'),
    ('confirmations', 'receipt', False, r'Anmeldebestätigung|Ihre Anmeldung wurde bestätigt|registration (?:is )?confirmed|регистрация подтверждена|реєстрацію підтверджено'),
]
UNIVERSITY = r'\b(?:university|college|Hochschule|Universität|tuition|Immatrikulation)\b|университет|університет'
NOISE = r'\bnewsletter\b|\bunsubscribe\b|abbestellen|рассылк|розсилк'
QUOTE_START = re.compile(r'^\s*(?:On .+wrote\s*:|Am .+schrieb.*:|[- ]*Original (?:Message|Nachricht)[- ]*|-----Original|От\s*:|Від\s*:|From\s*:|>)', re.I)


def _evidence(row, source_id, quote, **extra):
    return dict(account_id=row.get('account_id'), message_id=row.get('message_id'),
                message_key=row.get('message_key'), source_id=source_id, quote=quote, **extra)


def _active(text):
    lines = []
    for line in str(text or '').splitlines(keepends=True):
        if QUOTE_START.match(line):
            break
        lines.append(line)
    return ''.join(lines)


def _timestamp(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        try:
            dt = parsedate_to_datetime(str(value))
        except (ValueError, TypeError, OverflowError):
            return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc)


def _metadata(row, metadata):
    account, mid = row.get('account_id'), row.get('message_id')
    supplied = metadata.get((account, mid), metadata.get(account, {}).get(mid, {})) or {}
    return {**(row.get('metadata') or {}), **supplied}


def _mail_date(row):
    if row.get('date'):
        return row['date']
    headers = row.get('headers') or {}
    return next((value for name, value in headers.items() if name.casefold() == 'date'), None)


def _documents(row, document_records):
    documents = list(row.get('attachments') or [])
    for doc in document_records:
        # Never join by a provider ID without its account, or by filename/domain.
        identity = (doc.get('account_id'), doc.get('message_id'))
        if identity == (row.get('account_id'), row.get('message_id')):
            documents.append(doc)
        elif doc.get('message_key') and doc['message_key'] == row.get('message_key') and doc.get('account_id') == row.get('account_id'):
            documents.append(doc)
    result, seen = [], set()
    for index, doc in enumerate(documents):
        identifier = str(doc.get('attachment_id') or doc.get('sha256') or doc.get('id') or f'index:{index}')
        if identifier not in seen:
            result.append((identifier, doc))
            seen.add(identifier)
    return result


def _sources(row, documents):
    sources = [('body', _active(row.get('body', '')))]
    for identifier, doc in documents:
        parts = doc.get('parts') or doc.get('extraction', {}).get('parts') or []
        if parts:
            for index, part in enumerate(parts):
                source = str(part.get('source_id') or f'page:{part.get("page", index + 1)}')
                sources.append((f'attachment:{identifier}:{source}', _active(part.get('text', ''))))
        elif doc.get('text'):
            sources.append((f'attachment:{identifier}', _active(doc['text'])))
    return sources


def _observed(row, metadata, sources):
    hits = []
    for source_id, text in sources:
        for topic, state, action, pattern in RULES:
            for match in re.finditer(pattern, text, re.I):
                prefix = re.split(r'[.!?\n;]', text[:match.start()])[-1][-80:]
                if re.search(r'\b(?:not|no|never|kein\w*|nicht|не|нет|без)\b', prefix, re.I):
                    continue
                self_promise = re.search(r'\b(?:I will|Ich werde|я буду|я надішлю)\b', prefix, re.I)
                hits.append((topic, state, action and not self_promise, _evidence(row, source_id, match.group())))
    # Subject is a fallback only: reply subjects often describe obsolete states.
    if not hits:
        text = str(row.get('subject', ''))
        for topic, state, action, pattern in RULES:
            match = re.search(pattern, text, re.I)
            if match:
                hits.append((topic, state, action, _evidence(row, 'subject', match.group())))
    states = {h[1] for h in hits}
    conflict = 'rejected' in states and bool(states & {'interview', 'documents_requested', 'offer'})
    topic = hits[0][0] if hits else 'other'
    context = '\n'.join(text for _, text in sources) + '\n' + str(row.get('subject', ''))
    university = re.search(UNIVERSITY, context, re.I)
    if university and topic in {'jobs', 'finance'} and states & {'application_received', 'documents_requested', 'payment_due'}:
        topic = 'university'
        for source_id, text in sources + [('subject', str(row.get('subject', '')))]:
            match = re.search(UNIVERSITY, text, re.I)
            if match:
                hits.append((topic, 'context', False, _evidence(row, source_id, match.group())))
                break
    state = 'unknown' if conflict or not hits else hits[0][1]
    noise = re.search(NOISE, sources[0][1] + '\n' + str(row.get('subject', '')), re.I)
    if noise:
        topic = 'newsletters'
    labels = {str(label).upper() for label in metadata.get('labelIds', [])}
    outgoing = 'SENT' in labels or str(metadata.get('role', '')).casefold() in {'sent', 'outgoing'}
    reasons = []
    if metadata.get('source_deleted') or row.get('source_deleted'):
        reasons.append('source_deleted')
    reasons.extend(sorted(labels & {'SPAM', 'TRASH'}))
    if noise:
        reasons.append('newsletter_noise')
    relevant = topic not in {'other', 'newsletters'}
    return dict(category=topic, status=state, needs_review=conflict or not hits,
                evidence=[h[3] for h in hits], action_required=not conflict and not outgoing and any(h[2] for h in hits),
                active=not reasons, exclusion_reasons=reasons, relevant=relevant,
                review_reason='conflicting_current_status' if conflict else 'unknown_status')


MONTHS = ('Januar Februar März April Mai Juni Juli August September Oktober November Dezember').split()
DATE_TOKEN = re.compile(
    r'\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}\.\d{1,2}\.(?:\d{4})?|'
    r'\d{1,2}\.\s*(?:' + '|'.join(MONTHS) + r')(?:\s+\d{4})?|'
    r'tomorrow|morgen|next\s+\w+)(?!\w)', re.I)


def _dates(row, obs, sources, now):
    """Date tokens belong only to an evidenced action clause, never the subject."""
    observations = []
    if not obs['action_required']:
        return observations
    for source_id, text in sources:
        for clause in re.split(r'(?<!\d)[.!?](?:\s+|$)|[\n;]', text):
            if not any(e['source_id'] == source_id and e['quote'] in clause for e in obs['evidence']):
                continue
            if not any(action and re.search(pattern, clause, re.I) for _, _, action, pattern in RULES):
                continue
            tokens = list(DATE_TOKEN.finditer(clause))
            if not tokens:
                continue
            evidence = [_evidence(row, source_id, m.group()) for m in tokens]
            fields = dict(date=None, start_time=None, end_time=None, timezone=None)
            reason = None
            if len(tokens) > 1:
                reason = 'ambiguous_multiple_dates'
            else:
                token = tokens[0].group()
                if re.fullmatch(r'tomorrow|morgen|next\s+\w+', token, re.I):
                    reason = 'relative_date_requires_review'
                else:
                    try:
                        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', token):
                            parsed = date.fromisoformat(token)
                        else:
                            numeric = re.fullmatch(r'(\d{1,2})\.(\d{1,2})\.(\d{4})?', token)
                            german = re.fullmatch(r'(\d{1,2})\.\s*(\w+)(?:\s+(\d{4}))?', token)
                            parts = numeric or german
                            if not parts or not parts[3]:
                                reason = 'missing_year'
                            else:
                                month = int(parts[2]) if numeric else next(i for i, name in enumerate(MONTHS, 1) if name.casefold() == parts[2].casefold())
                                parsed = date(int(parts[3]), month, int(parts[1]))
                        if not reason:
                            fields['date'] = parsed.isoformat()
                    except (ValueError, StopIteration):
                        reason = 'invalid_date'
            kind = 'meeting' if obs['status'] == 'interview' else 'deadline_all_day'
            review_reasons = []
            if kind == 'meeting':
                times = list(re.finditer(r'\b(?:[01]\d|2[0-3]):[0-5]\d\b', clause))
                if times:
                    fields['start_time'] = times[0].group()
                    evidence.append(_evidence(row, source_id, times[0].group()))
                if len(times) == 2 and re.search(r'\b(?:to|bis)\b|–|-', clause[times[0].end():times[1].start()], re.I):
                    fields['end_time'] = times[1].group()
                    evidence.append(_evidence(row, source_id, times[1].group()))
                tz = re.search(r'\bEurope/Berlin\b', clause)
                if tz:
                    fields['timezone'] = tz.group()
                    evidence.append(_evidence(row, source_id, tz.group()))
                review_reasons = [name for name, value in [('missing_start_time', fields['start_time']), ('missing_end_time', fields['end_time']), ('unknown_timezone', fields['timezone'])] if value is None]
            temporal = 'unknown' if not fields['date'] else ('past' if fields['date'] < now.date().isoformat() else 'upcoming')
            observation = dict(kind=kind, fields=fields, temporal_status=temporal,
                               evidence=evidence, review_reasons=review_reasons, reason=reason)
            if reason == 'relative_date_requires_review' and tokens[0].group().casefold() in {'tomorrow', 'morgen'}:
                # Use the mail's explicit offset, not the analyzer clock or UTC day.
                raw = _mail_date(row)
                try:
                    anchor = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
                except ValueError:
                    try:
                        anchor = parsedate_to_datetime(str(raw))
                    except (ValueError, TypeError, OverflowError):
                        anchor = None
                if anchor is not None and anchor.tzinfo is not None:
                    observation['suggested_date'] = (anchor.date() + timedelta(days=1)).isoformat()
                    observation['anchor_evidence'] = _evidence(row, 'date', str(raw))
            observations.append(observation)
    by_source = defaultdict(list)
    for observation in observations:
        by_source[observation['evidence'][0]['source_id']].append(observation)
    for group in by_source.values():
        if len({o['fields']['date'] for o in group if o['fields']['date']}) > 1:
            for observation in group:
                observation['reason'] = 'ambiguous_multiple_dates'
                observation['fields']['date'] = None
                observation['temporal_status'] = 'unknown'
    return observations


def analyze(records: list[dict], provider_metadata: dict | None = None,
            document_records: list[dict] | None = None, now: str | None = None) -> dict:
    """Return a JSON-compatible derivation; input dictionaries are never mutated.

    Metadata accepts (account_id,message_id) keys or nested account/message keys.
    ``now`` is an ISO timestamp; omitted means the actual UTC clock. Naive mail
    dates cannot establish a reliable order, and are not silently assigned a TZ.
    """
    metadata = provider_metadata or {}
    analyzed_at = _timestamp(now) if now else datetime.now(timezone.utc)
    if analyzed_at is None:
        raise ValueError('now must be a timezone-aware ISO timestamp')
    grouped = defaultdict(list)
    derived = []
    document_count = 0
    for row in records:
        meta = _metadata(row, metadata)
        documents = _documents(row, document_records or [])
        document_count += len(documents)
        sources = _sources(row, documents)
        obs = _observed(row, meta, sources)
        obs['date_observations'] = _dates(row, obs, sources, analyzed_at)
        thread = meta.get('threadId') or row.get('threadId')
        key = (row.get('account_id'), 'thread' if thread else 'message',
               str(thread or row.get('message_key') or row.get('message_id')))
        timestamp = None
        if meta.get('internalDate') is not None:
            try:
                timestamp = datetime.fromtimestamp(int(meta['internalDate']) / 1000, timezone.utc)
            except (ValueError, TypeError, OverflowError, OSError):
                pass
        timestamp = timestamp or _timestamp(_mail_date(row))
        grouped[key].append((row, obs, timestamp))
        derived.append(obs)
    cases, actions, reviews, candidates, proposals = [], [], [], [], []
    for key, members in sorted(grouped.items(), key=lambda item: str(item[0])):
        members.sort(key=lambda m: (m[2] or datetime.min.replace(tzinfo=timezone.utc), str(m[0].get('message_id'))))
        reliable = len(members) == 1 or (all(m[2] is not None for m in members) and members[-1][2] != members[-2][2])
        row, obs, stamp = members[-1]
        case_id = ':'.join(str(k) for k in key)
        address = parseaddr(str(row.get('sender', '')))[1]
        case = dict(case_id=case_id, account_id=key[0], category=obs['category'],
                    status=obs['status'] if reliable else 'unknown',
                    latest_message_id=row.get('message_id') if reliable else None,
                    message_ids=[m[0].get('message_id') for m in members],
                    history=[dict(message_id=m[0].get('message_id'), date=m[0].get('date'),
                                  status=m[1]['status'], category=m[1]['category'], evidence=m[1]['evidence'],
                                  active=m[1]['active'], date_observations=m[1]['date_observations']) for m in members],
                    date_observations=obs['date_observations'] if reliable else [],
                    evidence=obs['evidence'] if reliable else [e for m in members for e in m[1]['evidence']],
                    active=obs['active'], exclusion_reasons=obs['exclusion_reasons'],
                    needs_review=obs['needs_review'] or not reliable,
                    sender_address=address, sender_domain=address.rsplit('@', 1)[1] if '@' in address else None,
                    institution=None, sender_verified=False)
        case['summary'] = {'status': case['status'], 'observed_quotes': [e['quote'] for e in case['evidence'][:3]]}
        cases.append(case)
        if case['active'] and case['needs_review']:
            reviews.append(dict(case_id=case_id, reason=obs['review_reason'] if reliable else 'uncertain_message_order',
                                evidence=case['evidence']))
        if case['active'] and case['needs_review']:
            candidates.append(dict(case_id=case_id, candidate_id=case_id, message_ids=case['message_ids'],
                                   evidence=case['evidence'], task='review_observed_case', stage='proposed'))
        if case['active'] and reliable and obs['action_required']:
            actions.append(dict(action_id=case_id + ':action', case_id=case_id, kind=case['status'],
                                stage='proposed', requires_review=True, evidence=case['evidence']))
            for index, observation in enumerate(case['date_observations']):
                if observation['reason']:
                    reviews.append(dict(case_id=case_id, **{k: v for k, v in observation.items() if k in {'reason', 'evidence', 'suggested_date', 'anchor_evidence'}}))
                    case['needs_review'] = True
                elif observation['temporal_status'] == 'upcoming':
                    proposals.append(dict(proposal_id=f'{case_id}:calendar:{index}', case_id=case_id,
                                          kind=observation['kind'], fields=observation['fields'],
                                          proposed_timezone='Europe/Berlin' if observation['fields']['timezone'] is None else None,
                                          stage='proposed', requires_review=True,
                                          review_reasons=observation['review_reasons'], evidence=observation['evidence']))
    counts = Counter(d['category'] for d in derived)
    categories = {topic: dict(total=count, active=sum(d['active'] and d['category'] == topic for d in derived),
                             important=sum(d['active'] and d['relevant'] and d['category'] == topic for d in derived))
                  for topic, count in sorted(counts.items())}
    return dict(schema_version=1, analyzed_at=analyzed_at.isoformat(),
                coverage={'complete_archive': False, 'scope': 'supplied_records_only', 'account_ids': sorted({str(r.get('account_id')) for r in records})},
                summaries={'messages': len(records), 'accounts': len({r.get('account_id') for r in records}),
                           'documents': document_count,
                           'cases': len(cases), 'active_important': sum(c['active'] and c['category'] not in {'other', 'newsletters'} for c in cases),
                           'actions': len(actions), 'review': len(reviews)},
                categories=categories, cases=cases, action_items=actions, calendar_proposals=proposals, review_items=reviews,
                model_candidates=candidates, calendar_modified=False)
