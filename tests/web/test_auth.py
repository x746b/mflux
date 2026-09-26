import pytest

from mflux.web.auth import LoginThrottle, WebAuth

pytestmark = pytest.mark.fast

KEY = "correct-horse-battery"


def test_hash_round_trip_and_rejects_wrong_key():
    stored = WebAuth.hash_key(KEY)
    assert KEY not in stored
    assert WebAuth.check_key(KEY, stored)
    assert not WebAuth.check_key("wrong-horse-battery", stored)
    assert not WebAuth.check_key(KEY, "garbage")


def test_non_ascii_key_compares_without_crashing():
    assert not WebAuth.check_key("café-café-café", WebAuth.hash_key(KEY))


def test_session_token_valid_until_key_changes():
    auth = WebAuth("secret", WebAuth.hash_key(KEY))
    token = auth.create_session_token()
    assert auth.verify_session_token(token)
    auth.set_api_key("another-long-key-123")
    assert not auth.verify_session_token(token)


def test_session_token_from_other_secret_is_rejected():
    stored = WebAuth.hash_key(KEY)
    token = WebAuth("secret-a", stored).create_session_token()
    assert not WebAuth("secret-b", stored).verify_session_token(token)
    assert not WebAuth("secret-a", stored).verify_session_token("not-a-token")


def test_no_sessions_without_a_key():
    auth = WebAuth("secret", None)
    assert not auth.verify_session_token(auth.create_session_token())
    assert not auth.verify_api_key(KEY)


def test_csrf_token_is_bound_to_session():
    auth = WebAuth("secret", WebAuth.hash_key(KEY))
    token = auth.create_session_token()
    assert auth.verify_csrf(token, auth.csrf_token(token))
    assert not auth.verify_csrf(token, auth.csrf_token(None))
    assert not auth.verify_csrf(token, None)


@pytest.mark.parametrize(
    ("key", "ok"), [(KEY, True), ("short", False), ("has a space in it", False), ("naïve-long-key-1", False)]
)
def test_new_key_validation(key, ok):
    assert (WebAuth.validate_new_key(key) is None) is ok


def test_fingerprint_does_not_leak_key():
    assert KEY not in WebAuth.fingerprint(KEY)
    assert len(WebAuth.fingerprint(KEY)) == 8


def test_throttle_backs_off_after_free_attempts():
    throttle = LoginThrottle()
    for _ in range(LoginThrottle.FREE_ATTEMPTS):
        assert throttle.retry_after("1.2.3.4") == 0
        throttle.record_failure("1.2.3.4")
    throttle.record_failure("1.2.3.4")
    assert throttle.retry_after("1.2.3.4") > 0
    assert throttle.retry_after("5.6.7.8") == 0
    throttle.record_success("1.2.3.4")
    assert throttle.retry_after("1.2.3.4") == 0
