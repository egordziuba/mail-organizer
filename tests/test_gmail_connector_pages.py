import base64
from urllib.parse import urlsplit, parse_qs
from urllib.error import HTTPError, URLError
import pytest
from mailorganizer.connectors import GmailReader, ConnectorError


def test_page_and_raw_metadata():
    calls = []
    def transport(url, headers):
        calls.append(url)
        if '/messages/' in url:
            return dict(id='a/b', raw=base64.urlsafe_b64encode(b'Subject: synthetic\r\n\r\nx').decode(), labelIds=['INBOX'], historyId='opaque')
        return {'messages': [{'id': 'a/b'}], 'resultSizeEstimate': 123}
    reader = GmailReader('synthetic', transport)
    assert reader.list_page(page_token='opaque', page_size=900)['resultSizeEstimate'] == 123
    params = parse_qs(urlsplit(calls[0]).query, keep_blank_values=True)
    assert params == {'q': [''], 'maxResults': ['500'], 'includeSpamTrash': ['true'], 'pageToken': ['opaque']}
    assert reader.get_raw('a/b')['raw'] == b'Subject: synthetic\r\n\r\nx'
    assert reader.get_raw('a/b')['labelIds'] == ['INBOX']
    reader.history_page('opaque')
    assert parse_qs(urlsplit(calls[-1]).query)['startHistoryId'] == ['opaque']


def test_safe_status_and_transient_retries():
    calls, waits = [], []
    def transport(url, headers):
        calls.append(url)
        if len(calls) < 3:
            raise URLError('SECRET')
        return {}
    assert GmailReader('synthetic', transport, sleep=waits.append).get_profile() == {}
    assert waits == [1, 2]
    def gone(url, headers):
        raise HTTPError(url, 404, 'SECRET', {}, None)
    with pytest.raises(ConnectorError) as caught:
        GmailReader('synthetic', gone).get_profile()
    assert caught.value.http_status == 404
    assert 'SECRET' not in str(caught.value)
    assert caught.value.__context__ is None


def test_429_retry_after_is_honored_and_bounded():
    calls, waits = [], []
    def throttled(url, headers):
        calls.append(url)
        raise HTTPError(url, 429, 'SECRET', {'Retry-After': '3'}, None)
    with pytest.raises(ConnectorError) as caught:
        GmailReader('synthetic', throttled, sleep=waits.append).get_profile()
    assert len(calls) == 3 and waits == [3, 3]
    assert caught.value.http_status == 429


def test_http_status_is_not_stale_after_non_http_failure():
    calls = []
    def transport(url, headers):
        calls.append(url)
        if len(calls) == 1:
            raise HTTPError(url, 503, 'SECRET', {}, None)
        raise ValueError('SECRET JSON')
    with pytest.raises(ConnectorError) as caught:
        GmailReader('synthetic', transport, sleep=lambda _: None).get_profile()
    assert caught.value.http_status is None
    assert str(caught.value) == 'Provider request failed'

