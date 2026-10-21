import pytest


def test_microsoft_identity_verification_has_required_readonly_scope():
    from mailorganizer import auth
    assert set(auth.MICROSOFT_SCOPES) == {"Mail.Read", "User.Read"}
    auth._microsoft_scopes("Mail.Read User.Read openid profile offline_access")
    with pytest.raises(auth.AuthError):
        auth._microsoft_scopes("Mail.Read User.Read Mail.Send")
