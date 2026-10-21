import pytest


def test_partial_microsoft_access_scope_is_not_treated_as_ready():
    from mailorganizer import auth
    with pytest.raises(auth.AuthError):
        auth._microsoft_result({"access_token":"SYNTHETIC","scope":"Mail.Read"})


def test_microsoft_network_failure_is_retryable_and_preserves_cache(monkeypatch):
    import json,msal
    from requests.exceptions import ConnectionError
    from mailorganizer import auth
    class Store:
        value=json.dumps({"client_id":"synthetic-client","home_account_id":"synthetic-home","cache":"{}"})
        def get_password(self,*args):return self.value
        def set_password(self,*args):pytest.fail("Failed refresh must not overwrite cache")
    class App:
        def __init__(self,*args,**kwargs):pass
        def get_accounts(self):return [{"home_account_id":"synthetic-home"}]
        def acquire_token_silent(self,*args,**kwargs):raise ConnectionError("SYNTHETIC_DETAIL")
    monkeypatch.setattr(msal,"PublicClientApplication",App)
    with pytest.raises(auth.AuthError) as caught:
        auth.microsoft_access_token("outlook-1","synthetic-client",Store())
    assert caught.value.reason == "temporary_unavailable"
    assert "SYNTHETIC_DETAIL" not in str(caught.value)


def test_microsoft_discovery_network_error_does_not_demand_reconsent(monkeypatch):
    import msal
    from requests.exceptions import ConnectionError
    from mailorganizer import auth
    def failed(*args,**kwargs):raise ConnectionError("SYNTHETIC_OFFLINE")
    monkeypatch.setattr(msal,"PublicClientApplication",failed)
    with pytest.raises(auth.AuthError) as caught:
        auth._microsoft_app("synthetic-client",msal.SerializableTokenCache())
    assert caught.value.reason == "temporary_unavailable"

