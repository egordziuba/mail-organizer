"""Isolated OAuth tests: all identities, credentials and stores are synthetic."""
import json
import pytest
from mailorganizer import auth as a


class MemoryStore:
    def __init__(self):
        self.data = {}
    def get_password(self, service, slot):
        return self.data.get((service, slot))
    def set_password(self, service, slot, value):
        self.data[service, slot] = value


def test_status_is_local_and_slots_isolated():
    store = MemoryStore()
    assert a.auth_status('gmail-one', 'gmail', store) == {
        'account_id': 'gmail-one', 'provider': 'gmail', 'authorized': False,
        'live_verified': False}
    store.set_password(a.SERVICE, 'gmail:gmail-one', json.dumps({'token': 'SECRET'}))
    assert a.auth_status('gmail-one', 'gmail', store)['authorized'] is True
    assert a.auth_status('gmail-two', 'gmail', store)['authorized'] is False
    assert a.auth_status('gmail-one', 'outlook', store)['authorized'] is False
    assert 'SECRET' not in repr(a.auth_status('gmail-one', 'gmail', store))


@pytest.mark.parametrize('account', ['../x', '', 'hello world', 'a:b', 'A', 'a@b.test'])
def test_account_slug_rejected(account):
    with pytest.raises(ValueError):
        a.auth_status(account, 'gmail', MemoryStore())


def test_plaintext_backend_rejected(monkeypatch):
    import keyring
    class Plaintext:
        pass
    monkeypatch.setattr(keyring, 'get_keyring', lambda: Plaintext())
    with pytest.raises(a.AuthError, match='Windows'):
        a.auth_status('gmail-one', 'gmail')


def test_google_loopback_and_refresh_isolated(monkeypatch, tmp_path):
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.oauth2.credentials import Credentials
    client = tmp_path / 'synthetic-client.json'
    client.write_text(json.dumps({'installed': {'client_id': 'synthetic', 'client_secret': 'synthetic',
        'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
        'token_uri': 'https://oauth2.googleapis.com/token', 'redirect_uris': ['http://localhost']}}))
    store = MemoryStore()
    scopes = ['https://www.googleapis.com/auth/gmail.readonly']
    class FakeCredentials:
        token = 'SECRET_ACCESS'
        refresh_token = 'SECRET_REFRESH'
        expired = True
        valid = False
        scopes = ['https://www.googleapis.com/auth/gmail.readonly']
        granted_scopes = scopes
        def to_json(self):
            return json.dumps({'token': self.token, 'refresh_token': self.refresh_token, 'scopes': self.scopes,
                'token_uri': 'https://oauth2.googleapis.com/token', 'client_id': 'synthetic'})
        def refresh(self, request):
            self.token = 'SECRET_NEW'
            self.valid = True
            self.expired = False
    credentials = FakeCredentials()
    class FakeFlow:
        def run_local_server(self, **kwargs):
            assert kwargs['port'] == 0
            assert kwargs['open_browser'] is True
            assert kwargs['authorization_prompt_message'] == ''
            return credentials
    def factory(config, scopes, **kwargs):
        assert scopes == ['https://www.googleapis.com/auth/gmail.readonly']
        assert 'installed' in config
        return FakeFlow()
    monkeypatch.setattr(InstalledAppFlow, 'from_client_config', factory)
    monkeypatch.setattr(Credentials, 'from_authorized_user_info', lambda info, scopes: credentials)
    metadata = a.google_authorize('gmail-one', client, store)
    assert metadata['authorized'] is True
    assert 'SECRET' not in repr(metadata)
    assert a.google_access_token('gmail-one', store) == 'SECRET_NEW'
    assert a.auth_status('gmail-two', 'gmail', store)['authorized'] is False
    assert len(store.data) == 1


def test_google_rejects_broad_cached_scopes_before_refresh():
    store = MemoryStore()
    store.set_password(a.SERVICE, 'gmail:gmail-one', json.dumps({
        'token': 'SECRET', 'scopes': ['https://mail.google.com/']}))
    with pytest.raises(a.AuthError, match='scope'):
        a.google_access_token('gmail-one', store)


def test_google_missing_auth_requires_explicit_consent():
    with pytest.raises(a.AuthError, match='authorization required'):
        a.google_access_token('gmail-one', MemoryStore())


def test_microsoft_interactive_and_silent_use_private_cache(monkeypatch):
    import msal
    calls = []
    class FakeApp:
        def __init__(self, client_id, authority, token_cache, **kwargs):
            assert client_id == 'synthetic-client'
            assert authority == 'https://login.microsoftonline.com/common'
            self.cache = token_cache
        def get_accounts(self):
            return [{'home_account_id': 'synthetic-home'}]
        def acquire_token_interactive(self, scopes, **kwargs):
            assert scopes == ['Mail.Read', 'User.Read']
            assert kwargs['port'] == 0
            calls.append('interactive')
            self.cache.has_state_changed = True
            return {'access_token': 'SECRET', 'scope': 'Mail.Read User.Read openid profile offline_access',
                    'id_token_claims': {'oid': 'synthetic'}}
        def acquire_token_silent(self, scopes, account):
            assert scopes == ['Mail.Read', 'User.Read']
            assert account['home_account_id'] == 'synthetic-home'
            calls.append('silent')
            return {'access_token': 'SECRET_REFRESHED', 'scope': 'Mail.Read User.Read'}
    monkeypatch.setattr(msal, 'PublicClientApplication', FakeApp)
    store = MemoryStore()
    metadata = a.microsoft_authorize('outlook-one', 'synthetic-client', store)
    assert metadata['authorized'] is True
    assert 'SECRET' not in repr(metadata)
    assert a.microsoft_access_token('outlook-one', 'synthetic-client', store) == 'SECRET_REFRESHED'
    assert calls == ['interactive', 'silent']
    assert list(store.data) == [(a.SERVICE, 'outlook:outlook-one')]
    with pytest.raises(a.AuthError, match='client'):
        a.microsoft_access_token('outlook-one', 'another-client', store)


def test_microsoft_missing_auth_never_launches_browser():
    with pytest.raises(a.AuthError, match='authorization required'):
        a.microsoft_access_token('outlook-one', 'synthetic-client', MemoryStore())


def test_microsoft_rejects_write_scopes(monkeypatch):
    import msal
    class FakeApp:
        def __init__(self, *args, **kwargs):
            pass
        def acquire_token_interactive(self, **kwargs):
            return {'access_token': 'SECRET', 'scope': 'Mail.Read Mail.Send'}
    monkeypatch.setattr(msal, 'PublicClientApplication', FakeApp)
    store = MemoryStore()
    with pytest.raises(a.AuthError, match='scope'):
        a.microsoft_authorize('outlook-one', 'synthetic-client', store)
    assert store.data == {}


def test_default_win_vault_chunks_large_cache_without_plaintext(monkeypatch):
    import keyring
    class SyntheticWinVault(MemoryStore):
        def set_password(self, service, slot, value):
            assert len(value.encode('utf-16-le')) <= 2560
            super().set_password(service, slot, value)
    backend = SyntheticWinVault()
    monkeypatch.setattr(a, 'WinVaultKeyring', SyntheticWinVault)
    monkeypatch.setattr(keyring, 'get_keyring', lambda: backend)
    store = a._store(None)
    value = json.dumps({'cache': 'SYNTHETIC' * 3000, 'name': 'Тест'})
    store.set_password(a.SERVICE, 'outlook:outlook-one', value)
    assert store.get_password(a.SERVICE, 'outlook:outlook-one') == value
    assert len(backend.data) > 1
    assert store.get_password(a.SERVICE, 'outlook:outlook-two') is None


@pytest.mark.parametrize('scopes', [[{}], [1], 'https://mail.google.com/'])
def test_malformed_google_scopes_fail_safely(scopes):
    store = MemoryStore()
    store.set_password(a.SERVICE, 'gmail:gmail-one', json.dumps({'token': 'SECRET', 'scopes': scopes}))
    with pytest.raises(a.AuthError, match='scope'):
        a.google_access_token('gmail-one', store)


def test_vault_exception_has_no_secret_context():
    class BrokenStore(MemoryStore):
        def get_password(self, *args):
            raise RuntimeError('SECRET_REFRESH_TOKEN')
    with pytest.raises(a.AuthError) as error:
        a.auth_status('gmail-one', 'gmail', BrokenStore())
    assert 'SECRET' not in str(error.value)
    assert error.value.__context__ is None


def test_microsoft_silent_failure_never_calls_interactive(monkeypatch):
    import msal
    class FakeApp:
        def __init__(self, *args, **kwargs):
            pass
        def get_accounts(self):
            return [{'home_account_id': 'synthetic-home'}]
        def acquire_token_silent(self, *args, **kwargs):
            raise RuntimeError('SECRET')
        def acquire_token_interactive(self, *args, **kwargs):
            pytest.fail('Silent refresh may not initiate consent')
    monkeypatch.setattr(msal, 'PublicClientApplication', FakeApp)
    store = MemoryStore()
    store.set_password(a.SERVICE, 'outlook:outlook-one', json.dumps({
        'client_id': 'synthetic-client', 'home_account_id': 'synthetic-home', 'cache': '{}'}))
    with pytest.raises(a.AuthError) as error:
        a.microsoft_access_token('outlook-one', 'synthetic-client', store)
    assert 'SECRET' not in str(error.value)
    assert error.value.__context__ is None
