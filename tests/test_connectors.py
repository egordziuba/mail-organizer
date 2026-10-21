"""Synthetic transport tests: never access real mail."""
import base64
import pytest
from mailorganizer import connectors as c


def test_gmail_pages_raw_and_profile():
    calls = []
    def transport(url, headers):
        calls.append((url, headers))
        if '/profile' in url:
            return {'emailAddress': 'synthetic@example.test'}
        if '/messages/' in url:
            return {'raw': base64.urlsafe_b64encode(b'Subject: synthetic\r\n\r\nhello').decode().rstrip('=')}
        return {'messages': [{'id': 'b' if 'pageToken=' in url else 'a'}], **({} if 'pageToken=' in url else {'nextPageToken': 'next'})}
    reader = c.GmailReader('SECRET', transport)
    assert [i for i, raw in reader.iter_messages(query='is:unread')] == ['a', 'b']
    assert reader.get_profile()['emailAddress'] == 'synthetic@example.test'
    assert 'includeSpamTrash=false' in calls[0][0]
    assert all(h['Authorization'] == 'Bearer SECRET' for _, h in calls)
    assert 'SECRET' not in repr(reader)


def test_graph_pages_mime_immutable():
    calls = []
    def transport(url, headers):
        calls.append((url, headers))
        if url.endswith('/$value'):
            return b'Subject: synthetic\r\n\r\nhello'
        if url.endswith('/me'):
            return {'id': 'synthetic'}
        return {'value': [{'id': 'b' if 'skiptoken' in url else 'a'}], **({} if 'skiptoken' in url else {'@odata.nextLink': 'https://graph.microsoft.com/v1.0/me/messages?$skiptoken=x'})}
    reader = c.GraphReader('SECRET', transport)
    assert [mid for mid, raw in reader.iter_messages()] == ['a', 'b']
    assert reader.get_profile() == {'id': 'synthetic'}
    assert all(h['Prefer'] == 'IdType="ImmutableId"' for _, h in calls)


@pytest.mark.parametrize('cls', ['GmailReader', 'GraphReader'])
@pytest.mark.parametrize('cap', [0, -1, True, 1.5, '1'])
def test_invalid_caps(cls, cap):
    with pytest.raises(ValueError):
        list(getattr(c, cls)('SECRET', lambda *_: {}).iter_messages(max_messages=cap))


def test_graph_nextlink_host_rejected_without_leak():
    calls = []
    def transport(url, headers):
        calls.append(url)
        return {'value': [], '@odata.nextLink': 'https://evil.test/?SECRET'}
    with pytest.raises(c.ConnectorError) as error:
        list(c.GraphReader('SECRET', transport).iter_messages())
    assert len(calls) == 1
    assert 'SECRET' not in str(error.value)


def test_bounded_retry_sanitizes_errors():
    from urllib.error import HTTPError
    calls = []
    sleeps = []
    def transport(url, headers):
        calls.append(url)
        raise HTTPError(url + 'SECRET', 429, 'SECRET', {'Retry-After': '2'}, None)
    reader = c.GmailReader('SECRET', transport, sleep=sleeps.append)
    with pytest.raises(c.ConnectorError) as error:
        reader.get_profile()
    assert len(calls) == 3
    assert sleeps == [2.0, 2.0]
    assert 'SECRET' not in str(error.value)
    assert error.value.__cause__ is None


def test_long_retry_after_does_not_retry_early():
    from urllib.error import HTTPError
    calls = []
    def transport(url, headers):
        calls.append(url)
        raise HTTPError(url, 503, 'synthetic', {'Retry-After': '120'}, None)
    reader = c.GmailReader('SECRET', transport, sleep=lambda _: pytest.fail('must not retry early'))
    with pytest.raises(c.ConnectorError):
        reader.get_profile()
    assert len(calls) == 1


@pytest.mark.parametrize('nextlink', [
    'http://graph.microsoft.com/v1.0/me/messages',
    'https://graph.microsoft.com.evil.test/messages',
    'https://user@graph.microsoft.com/messages',
    'https://graph.microsoft.com:443/messages',
    '//graph.microsoft.com/messages',
])
def test_all_unsafe_nextlinks_rejected(nextlink):
    calls = []
    def transport(url, headers):
        calls.append(url)
        return {'value': [], '@odata.nextLink': nextlink}
    with pytest.raises(c.ConnectorError):
        list(c.GraphReader('SECRET', transport).iter_messages())
    assert len(calls) == 1


def test_default_transport_get_only_no_redirect(monkeypatch):
    captured = []
    class Headers:
        def get_content_type(self):
            return 'application/json'
    class Response:
        headers = Headers()
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def read(self):
            return b'{"emailAddress":"synthetic@example.test"}'
    class Opener:
        def open(self, request, timeout):
            captured.append(request)
            return Response()
    monkeypatch.setattr(c, 'build_opener', lambda redirect: Opener())
    assert c.GmailReader('SECRET').get_profile()['emailAddress'] == 'synthetic@example.test'
    assert captured[0].method == 'GET'
    assert c._NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.test') is None


def test_cap_prevents_extra_mime_and_page_calls():
    calls = []
    def transport(url, headers):
        calls.append(url)
        if '/messages/' in url:
            return {'raw': 'eA'}
        return {'messages': [{'id': 'a'}, {'id': 'b'}], 'nextPageToken': 'unused'}
    assert list(c.GmailReader('SECRET', transport).iter_messages(max_messages=1)) == [('a', b'x')]
    assert len(calls) == 2
