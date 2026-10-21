import json
import pytest
from google.auth.exceptions import RefreshError, TransportError


@pytest.mark.parametrize("exception,reason", [(RefreshError("invalid_grant"),"reauthorize_required"), (RefreshError("temporary", retryable=True),"temporary_unavailable"), (TransportError("SYNTHETIC_NETWORK_DETAIL"),"temporary_unavailable")])
def test_failed_refresh_preserves_durable_cache_and_has_safe_reason(monkeypatch, exception, reason):
    from mailorganizer import auth
    class Store:
        value = json.dumps({"scopes":list(auth.GOOGLE_SCOPES),"token_uri":"https://oauth2.googleapis.com/token"})
        def get_password(self,*args): return self.value
        def set_password(self,*args): pytest.fail("Failed refresh must not overwrite stored credentials")
    class Credentials:
        valid=False
        def refresh(self,*args): raise exception
    store=Store()
    before=store.value
    monkeypatch.setattr(auth.Credentials,"from_authorized_user_info",lambda *a,**k:Credentials())
    with pytest.raises(auth.AuthError) as caught:
        auth.google_access_token("gmail-1",store)
    assert caught.value.reason == reason
    assert "SYNTHETIC_NETWORK_DETAIL" not in str(caught.value)
    assert caught.value.__context__ is None
    assert store.value == before


def test_missing_cache_requires_auth_not_repeated_browser():
    from mailorganizer import auth
    class Store:
        def get_password(self,*args): return None
    with pytest.raises(auth.AuthError) as caught:
        auth.google_access_token("gmail-1",Store())
    assert caught.value.reason == "reauthorize_required"
