"""Isolated account OAuth cache; never shares Hermes credential files.

Injected test stores implement get_password(service, slot) and
set_password(service, slot, value). Default store is Windows WinVault only.
"""
import re
import json
import base64
import uuid
from pathlib import Path
import keyring
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from keyring.backends.Windows import WinVaultKeyring

SERVICE = 'mail-organizer.oauth.v1'


class AuthError(RuntimeError):
    """Sanitized authentication error with a stable operational reason."""
    def __init__(self, message, *, reason="configuration_error"):
        super().__init__(message)
        self.reason = reason if reason in {"configuration_error", "reauthorize_required", "temporary_unavailable"} else "configuration_error"


def _refresh_reason(error):
    from google.auth.exceptions import RefreshError, TransportError
    from requests.exceptions import RequestException
    if isinstance(error, (TransportError, RequestException, OSError)) or getattr(error, "retryable", False):
        return "temporary_unavailable"
    if isinstance(error, RefreshError):
        return "configuration_error" if "invalid_client" in str(error) else "reauthorize_required"
    return "configuration_error"


def _slot(account_id, provider):
    if not isinstance(account_id, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', account_id):
        raise ValueError('account_id must be a lowercase slug (1-64 characters)')
    if provider not in ('gmail', 'outlook'):
        raise ValueError('provider must be gmail or outlook')
    return provider + ':' + account_id


class _VaultStore:
    """Chunk large OAuth caches under the Windows CredentialBlob size limit.

    Each chunk stays inside WinVault/DPAPI; no disk credential fallback.
    The manifest is committed last, so a failed update keeps the previous cache.
    """
    _prefix = 'mail-organizer-chunks-v1:'

    def __init__(self, backend):
        self._backend = backend

    def get_password(self, service, slot):
        raw = self._backend.get_password(service, slot)
        if not raw or not raw.startswith(self._prefix):
            return raw
        manifest = json.loads(raw[len(self._prefix):])
        generation, count = manifest['generation'], manifest['count']
        if (not re.fullmatch(r'[a-f0-9]{32}', generation)
                or type(count) is not int or not 1 <= count <= 4096):
            raise AuthError('Invalid vault manifest')
        parts = [self._backend.get_password(service, f'{slot}:{generation}:{index}')
                 for index in range(count)]
        if any(not isinstance(part, str) for part in parts):
            raise AuthError('Incomplete vault cache')
        return base64.b64decode(''.join(parts), validate=True).decode('utf-8')

    def set_password(self, service, slot, value):
        encoded = base64.b64encode(value.encode('utf-8')).decode('ascii')
        chunks = [encoded[index:index + 1000] for index in range(0, len(encoded), 1000)]
        if not 1 <= len(chunks) <= 4096:
            raise AuthError('Credential cache size exceeds vault limit')
        old = self._backend.get_password(service, slot)
        generation = uuid.uuid4().hex
        for index, chunk in enumerate(chunks):
            self._backend.set_password(service, f'{slot}:{generation}:{index}', chunk)
        self._backend.set_password(service, slot, self._prefix + json.dumps({
            'generation': generation, 'count': len(chunks)}))
        # Only remove superseded chunks owned by this application's slot.
        if old and old.startswith(self._prefix):
            try:
                manifest = json.loads(old[len(self._prefix):])
                if (re.fullmatch(r'[a-f0-9]{32}', manifest['generation'])
                        and type(manifest['count']) is int and 1 <= manifest['count'] <= 4096):
                    for index in range(manifest['count']):
                        self._backend.delete_password(service, f"{slot}:{manifest['generation']}:{index}")
            except Exception:
                pass


def _store(store):
    if store is not None:
        return store
    backend = keyring.get_keyring()
    if not isinstance(backend, WinVaultKeyring):
        raise AuthError('Secure Windows credential vault backend required')
    return _VaultStore(backend)


def _load(store, slot):
    failed = False
    try:
        result = store.get_password(SERVICE, slot)
    except Exception:
        failed = True
    if failed:
        raise AuthError('Credential vault read failed')
    return result


def auth_status(account_id: str, provider: str, store=None) -> dict:
    slot = _slot(account_id, provider)
    return {'account_id': account_id, 'provider': provider,
            'authorized': bool(_load(_store(store), slot)), 'live_verified': False}


GOOGLE_SCOPES = ('https://www.googleapis.com/auth/gmail.readonly',)


def _save(store, slot, value):
    failed = False
    try:
        store.set_password(SERVICE, slot, value)
    except Exception:
        failed = True
    if failed:
        raise AuthError('Credential vault write failed')


def _json(raw):
    result = None
    try:
        result = json.loads(raw)
    except (ValueError, TypeError):
        pass
    if not isinstance(result, dict):
        raise AuthError('Invalid credential cache; authorization required')
    return result


def _google_scopes(scopes):
    if (not isinstance(scopes, (list, tuple))
            or not all(isinstance(scope, str) for scope in scopes)
            or set(scopes) != set(GOOGLE_SCOPES)):
        raise AuthError('Unsafe or missing Google scope')


def _google_credentials_scopes(credentials):
    _google_scopes(credentials.scopes)
    granted = credentials.granted_scopes
    if granted is not None:
        _google_scopes(granted)


def google_authorize(account_id: str, client_file: Path, store=None) -> dict:
    """Explicit user consent only: desktop loopback, readonly, separate vault slot."""
    slot = _slot(account_id, 'gmail')
    vault = _store(store)
    credentials = None
    try:
        config = json.loads(Path(client_file).read_text(encoding='utf-8'))
        installed = config.get('installed', {})
        if (installed.get('auth_uri') != 'https://accounts.google.com/o/oauth2/auth'
                or installed.get('token_uri') != 'https://oauth2.googleapis.com/token'):
            raise ValueError('Invalid desktop OAuth configuration')
        flow = InstalledAppFlow.from_client_config(config, scopes=list(GOOGLE_SCOPES))
        credentials = flow.run_local_server(host='localhost', port=0, open_browser=True,
            authorization_prompt_message='', timeout_seconds=180,
            access_type='offline', prompt='consent', include_granted_scopes='false')
    except Exception:
        pass
    if credentials is None:
        raise AuthError('Google authorization failed; check desktop client configuration and consent')
    _google_credentials_scopes(credentials)
    if not credentials.refresh_token:
        raise AuthError('Google offline authorization required')
    _save(vault, slot, credentials.to_json())
    return auth_status(account_id, 'gmail', vault)


def google_access_token(account_id: str, store=None) -> str:
    slot = _slot(account_id, 'gmail')
    vault = _store(store)
    raw = _load(vault, slot)
    if not raw:
        raise AuthError('Google authorization required', reason='reauthorize_required')
    info = _json(raw)
    _google_scopes(info.get('scopes'))
    if info.get('token_uri') != 'https://oauth2.googleapis.com/token':
        raise AuthError('Unsafe Google token endpoint')
    credentials = None
    failure_reason = 'reauthorize_required'
    try:
        credentials = Credentials.from_authorized_user_info(info, scopes=list(GOOGLE_SCOPES))
        if not credentials.valid:
            credentials.refresh(Request())
    except Exception as error:
        failure_reason = _refresh_reason(error)
        credentials = None
    if credentials is None or not credentials.valid or not credentials.token:
        raise AuthError('Google token acquisition failed', reason=failure_reason)
    _google_credentials_scopes(credentials)
    _save(vault, slot, credentials.to_json())
    return credentials.token


MICROSOFT_SCOPES = ('Mail.Read', 'User.Read')
MICROSOFT_AUTHORITY = 'https://login.microsoftonline.com/common'


def _client_id(client_id):
    if not isinstance(client_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', client_id):
        raise ValueError('A valid Microsoft public client ID is required')


def _microsoft_scopes(scopes, *, require_mail=True):
    if not isinstance(scopes, str):
        raise AuthError('Unsafe or missing Microsoft scope')
    normalized = {scope.lower().removeprefix('https://graph.microsoft.com/') for scope in scopes.split()}
    if (not normalized.issubset({'mail.read', 'user.read', 'openid', 'profile', 'offline_access'})
            or (require_mail and not {'mail.read', 'user.read'}.issubset(normalized))):
        raise AuthError('Unsafe or missing Microsoft scope')


def _microsoft_cache_scopes(cache):
    data = _json(cache.serialize())
    for kind in ('AccessToken', 'RefreshToken'):
        entries = data.get(kind, {})
        if not isinstance(entries, dict):
            raise AuthError('Invalid Microsoft credential cache')
        for entry in entries.values():
            if not isinstance(entry, dict):
                raise AuthError('Invalid Microsoft credential cache')
            _microsoft_scopes(entry.get('target', ''), require_mail=kind == 'AccessToken')


def _microsoft_app(client_id, cache):
    import msal
    app = None
    failure_reason = 'configuration_error'
    try:
        app = msal.PublicClientApplication(client_id, authority=MICROSOFT_AUTHORITY,
                                           token_cache=cache, enable_broker_on_windows=False)
    except Exception as error:
        failure_reason = _refresh_reason(error)
    if app is None:
        raise AuthError('Microsoft client initialization failed', reason=failure_reason)
    return app


def _microsoft_result(result):
    if not isinstance(result, dict) or not isinstance(result.get('access_token'), str) or not result['access_token']:
        code = result.get('error') if isinstance(result, dict) else None
        reason = 'temporary_unavailable' if code in {'temporarily_unavailable', 'server_error'} else 'reauthorize_required'
        if code in {'invalid_client', 'unauthorized_client'}:
            reason = 'configuration_error'
        raise AuthError('Microsoft token acquisition failed', reason=reason)
    _microsoft_scopes(result.get('scope'))


def microsoft_authorize(account_id: str, client_id: str, store=None) -> dict:
    """Explicit MSAL browser loopback consent, Mail.Read only, common authority."""
    import msal
    slot = _slot(account_id, 'outlook')
    _client_id(client_id)
    vault = _store(store)
    cache = msal.SerializableTokenCache()
    app = _microsoft_app(client_id, cache)
    result = None
    try:
        result = app.acquire_token_interactive(scopes=list(MICROSOFT_SCOPES), port=0,
                                               timeout=180, prompt='select_account')
    except Exception:
        pass
    _microsoft_result(result)
    _microsoft_cache_scopes(cache)
    accounts = None
    try:
        accounts = app.get_accounts()
    except Exception:
        pass
    if not isinstance(accounts, list) or len(accounts) != 1 or not accounts[0].get('home_account_id'):
        raise AuthError('Microsoft account selection failed')
    _save(vault, slot, json.dumps({'client_id': client_id,
        'home_account_id': accounts[0]['home_account_id'], 'cache': cache.serialize()}))
    return auth_status(account_id, 'outlook', vault)


def microsoft_access_token(account_id: str, client_id: str, store=None) -> str:
    """Silent cache refresh only. Never opens a browser."""
    import msal
    slot = _slot(account_id, 'outlook')
    _client_id(client_id)
    vault = _store(store)
    raw = _load(vault, slot)
    if not raw:
        raise AuthError('Microsoft authorization required', reason='reauthorize_required')
    saved = _json(raw)
    if saved.get('client_id') != client_id:
        raise AuthError('Microsoft client mismatch; authorization required')
    cache = msal.SerializableTokenCache()
    valid = False
    try:
        cache.deserialize(saved['cache'])
        valid = True
    except Exception:
        pass
    if not valid:
        raise AuthError('Invalid Microsoft credential cache; authorization required')
    _microsoft_cache_scopes(cache)
    app = _microsoft_app(client_id, cache)
    result = None
    failure_reason = None
    try:
        accounts = [account for account in app.get_accounts()
                    if account.get('home_account_id') == saved.get('home_account_id')]
        if len(accounts) == 1:
            result = app.acquire_token_silent(list(MICROSOFT_SCOPES), account=accounts[0])
    except Exception as error:
        failure_reason = _refresh_reason(error)
    if failure_reason is not None:
        raise AuthError('Microsoft token acquisition failed', reason=failure_reason)
    _microsoft_result(result)
    _microsoft_cache_scopes(cache)
    saved['cache'] = cache.serialize()
    _save(vault, slot, json.dumps(saved))
    return result['access_token']
