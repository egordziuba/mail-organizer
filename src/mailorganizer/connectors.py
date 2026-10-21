"""GET-only readers. Synthetic transports: callable(url, headers) -> dict | bytes.

Transport errors can use urllib.error.HTTPError for status/retry headers.
A capped iteration is NOT evidence of complete mailbox coverage.
"""
import base64
import json
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler


class ConnectorError(RuntimeError):
    """Sanitized provider failure; contains neither URLs nor server bodies."""

    def __init__(self, message, *, http_status=None):
        super().__init__(message)
        self.http_status = http_status if type(http_status) is int else None


def _check_url(url, host):
    try:
        p = urlsplit(url)
        valid = (p.scheme == 'https' and p.netloc == host and p.hostname == host
                 and not p.username and not p.password and not p.fragment)
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ConnectorError('Unsafe provider URL rejected')


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward bearer credentials via redirects.
        return None


def _http_get(url, headers):
    request = Request(url, headers=headers, method='GET')
    with build_opener(_NoRedirect()).open(request, timeout=30) as response:
        raw = response.read()
        if response.headers.get_content_type() == 'application/json':
            return json.loads(raw)
        return raw


def _cap(value):
    if type(value) is not int or value <= 0:
        raise ValueError('max_messages must be a positive integer')


def _delay(headers, attempt):
    raw = headers.get('Retry-After') if headers else None
    if raw:
        try:
            seconds = float(raw)
        except (ValueError, TypeError):
            try:
                seconds = (parsedate_to_datetime(raw) - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                seconds = 2 ** attempt
        # Stop instead of retrying sooner than Retry-After when wait is excessive.
        return max(0, seconds) if seconds <= 60 else None
    return 2 ** attempt


class _Reader:
    def __init__(self, token: str, transport=None, *, sleep=time.sleep):
        if not isinstance(token, str) or not token or any(ch in token for ch in '\r\n'):
            raise ValueError('A valid access token is required')
        self._token = token
        self._transport = transport or _http_get
        self._sleep = sleep

    def _get(self, url):
        _check_url(url, self._host)
        headers = {'Authorization': 'Bearer ' + self._token}
        if self._host == 'graph.microsoft.com':
            headers['Prefer'] = 'IdType="ImmutableId"'
        failure = 'Provider request failed'
        status = None
        for attempt in range(3):
            delay = None
            try:
                return self._transport(url, headers)
            except HTTPError as exc:
                status = exc.code
                failure = 'Provider HTTP error ' + str(exc.code)
                if (exc.code == 429 or 500 <= exc.code <= 599) and attempt < 2:
                    delay = _delay(exc.headers, attempt)
            except (URLError, TimeoutError, ConnectionError, OSError):
                status = None
                failure = 'Provider request failed'
                if attempt < 2:
                    delay = 2 ** attempt
            except Exception:
                status = None
                failure = 'Provider request failed'
            if delay is None:
                break
            self._sleep(delay)
        # Raise outside except so no secret-bearing exception context survives.
        raise ConnectorError(failure, http_status=status)

    def _dict(self, url):
        data = self._get(url)
        if not isinstance(data, dict):
            raise ConnectorError('Invalid provider JSON response')
        return data


class GmailReader(_Reader):
    _host = 'gmail.googleapis.com'
    _base = 'https://gmail.googleapis.com/gmail/v1/users/me/'

    def get_profile(self) -> dict:
        return self._dict(self._base + 'profile')

    def list_page(self, query='', page_token=None, include_spam_trash=True, page_size=500) -> dict:
        _cap(page_size)
        if not isinstance(query, str):
            raise ValueError('query must be text')
        params = {'q': query, 'maxResults': min(500, page_size),
                  'includeSpamTrash': 'true' if include_spam_trash else 'false'}
        if page_token is not None:
            params['pageToken'] = page_token
        return self._dict(self._base + 'messages?' + urlencode(params))

    def get_metadata(self, message_id) -> dict:
        if not isinstance(message_id,str) or not message_id:raise ValueError('message_id must be nonempty text')
        data=self._dict(self._base+'messages/'+quote(message_id,safe='')+'?format=metadata')
        return {k:data[k] for k in ('id','threadId','labelIds','internalDate','historyId','sizeEstimate') if k in data}

    def get_raw(self, message_id) -> dict:
        if not isinstance(message_id, str) or not message_id:
            raise ValueError('message_id must be nonempty text')
        data = self._dict(self._base + 'messages/' + quote(message_id, safe='') + '?format=raw')
        decoded = None
        try:
            raw = data.get('raw')
            if isinstance(raw, str):
                decoded = base64.b64decode(raw + '=' * (-len(raw) % 4), altchars=b'-_', validate=True)
        except (ValueError, UnicodeError):
            pass
        if decoded is None:
            raise ConnectorError('Invalid provider MIME encoding')
        result = {k: data[k] for k in ('id', 'threadId', 'labelIds', 'internalDate', 'historyId', 'sizeEstimate') if k in data}
        result['raw'] = decoded
        return result

    def history_page(self, start_history_id, page_token=None) -> dict:
        params = {'startHistoryId': start_history_id, 'maxResults': 500}
        if page_token is not None:
            params['pageToken'] = page_token
        return self._dict(self._base + 'history?' + urlencode(params))

    def iter_messages(self, query: str = '', max_messages: int = 100):
        _cap(max_messages)
        if not isinstance(query, str):
            raise ValueError('query must be text')
        count, page = 0, None
        seen = set()
        while True:
            params = {'q': query, 'maxResults': min(500, max_messages - count), 'includeSpamTrash': 'false'}
            if page:
                params['pageToken'] = page
            data = self._dict(self._base + 'messages?' + urlencode(params))
            messages = data.get('messages', [])
            if not isinstance(messages, list):
                raise ConnectorError('Invalid provider message list')
            for message in messages:
                if not isinstance(message, dict) or not isinstance(message.get('id'), str) or not message['id']:
                    raise ConnectorError('Invalid provider message ID')
                mid = message['id']
                raw = self._dict(self._base + 'messages/' + quote(mid, safe='') + '?format=raw').get('raw')
                decoded = None
                try:
                    if isinstance(raw, str):
                        decoded = base64.b64decode(raw + '=' * (-len(raw) % 4), altchars=b'-_', validate=True)
                except (ValueError, UnicodeError):
                    pass
                if decoded is None:
                    raise ConnectorError('Invalid provider MIME encoding')
                yield mid, decoded
                count += 1
                if count >= max_messages:
                    return
            page = data.get('nextPageToken')
            if not page:
                return
            if not isinstance(page, str) or page in seen:
                raise ConnectorError('Invalid provider pagination')
            seen.add(page)


class GraphReader(_Reader):
    _host = 'graph.microsoft.com'
    _base = 'https://graph.microsoft.com/v1.0/me'

    def get_profile(self) -> dict:
        return self._dict(self._base)

    def iter_messages(self, folder: str = 'inbox', max_messages: int = 100):
        _cap(max_messages)
        if not isinstance(folder, str) or not folder:
            raise ValueError('folder must be nonempty text')
        url = self._base + '/mailFolders/' + quote(folder, safe='') + '/messages?' + urlencode({'$select': 'id', '$top': min(100, max_messages)})
        seen = set()
        count = 0
        while url:
            if url in seen:
                raise ConnectorError('Invalid provider pagination')
            seen.add(url)
            data = self._dict(url)
            messages = data.get('value', [])
            if not isinstance(messages, list):
                raise ConnectorError('Invalid provider message list')
            for message in messages:
                if not isinstance(message, dict) or not isinstance(message.get('id'), str) or not message['id']:
                    raise ConnectorError('Invalid provider message ID')
                mid = message['id']
                raw = self._get(self._base + '/messages/' + quote(mid, safe='') + '/$value')
                if not isinstance(raw, bytes):
                    raise ConnectorError('Invalid provider MIME response')
                yield mid, raw
                count += 1
                if count >= max_messages:
                    return
            url = data.get('@odata.nextLink')
            if url is not None and not isinstance(url, str):
                raise ConnectorError('Invalid provider pagination')
