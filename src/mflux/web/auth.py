# SPDX-License-Identifier: Apache-2.0
# Adapted from oMLX (https://github.com/jundot/omlx), omlx/admin/auth.py.
# Copyright oMLX contributors. Licensed under the Apache License, Version 2.0; see NOTICE.
# Changes: class-based API, hashed key storage, per-key session versioning, CSRF tokens,
# and a login throttle.

import hashlib
import hmac
import secrets
import threading
import time

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

SESSION_COOKIE_NAME = "mflux_web_session"
SESSION_MAX_AGE = 86400
REMEMBER_ME_MAX_AGE = 30 * 86400
PBKDF2_ITERATIONS = 240_000


class WebAuth:
    def __init__(self, secret_key: str, api_key_hash: str | None):
        self._secret_key = secret_key
        self._serializer = URLSafeTimedSerializer(secret_key, salt="mflux-web-session")
        self._verified_digests: set[str] = set()
        self._lock = threading.Lock()
        self.api_key_hash = api_key_hash

    @property
    def enabled(self) -> bool:
        return self.api_key_hash is not None

    def set_api_key(self, api_key: str) -> None:
        with self._lock:
            self.api_key_hash = WebAuth.hash_key(api_key)
            self._verified_digests.clear()

    def verify_api_key(self, provided: str | None) -> bool:
        if not provided or self.api_key_hash is None:
            return False
        digest = hashlib.sha256(provided.encode("utf-8", "surrogatepass")).hexdigest()
        with self._lock:
            if digest in self._verified_digests:
                return True
        if not WebAuth.check_key(provided, self.api_key_hash):
            return False
        with self._lock:
            self._verified_digests.add(digest)
        return True

    def create_session_token(self, remember: bool = False) -> str:
        return self._serializer.dumps({"v": self._key_version(), "remember": remember})

    def verify_session_token(self, token: str | None) -> bool:
        if not token or not self.enabled:
            return False
        try:
            data = self._serializer.loads(token, max_age=None)
            max_age = REMEMBER_ME_MAX_AGE if data.get("remember") else SESSION_MAX_AGE
            data = self._serializer.loads(token, max_age=max_age)
        except (BadSignature, SignatureExpired):
            return False
        # Sessions die with the key that created them: changing the key logs everyone out.
        return hmac.compare_digest(str(data.get("v", "")), self._key_version())

    def csrf_token(self, session_token: str | None) -> str:
        message = f"csrf:{session_token or 'anonymous'}".encode()
        return hmac.new(self._secret_key.encode(), message, hashlib.sha256).hexdigest()

    def verify_csrf(self, session_token: str | None, provided: str | None) -> bool:
        if not provided:
            return False
        return hmac.compare_digest(self.csrf_token(session_token), provided)

    @staticmethod
    def hash_key(api_key: str) -> str:
        salt = secrets.token_bytes(16)
        derived = hashlib.pbkdf2_hmac("sha256", api_key.encode("utf-8", "surrogatepass"), salt, PBKDF2_ITERATIONS)
        return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${derived.hex()}"

    @staticmethod
    def check_key(api_key: str, stored: str) -> bool:
        try:
            scheme, iterations, salt_hex, hash_hex = stored.split("$")
        except ValueError:
            return False
        if scheme != "pbkdf2_sha256":
            return False
        derived = hashlib.pbkdf2_hmac(
            "sha256", api_key.encode("utf-8", "surrogatepass"), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(derived.hex(), hash_hex)

    @staticmethod
    def fingerprint(api_key: str) -> str:
        # Log rejected keys by fingerprint so a mistyped secret never lands in the log.
        return hashlib.sha256(api_key.encode("utf-8", "surrogatepass")).hexdigest()[:8]

    @staticmethod
    def validate_new_key(api_key: str) -> str | None:
        # HTTP headers arrive latin-1 decoded, so a non-ASCII key could be configured but
        # never sent intact; reject it up front instead of producing silent 401s.
        if len(api_key) < 12:
            return "API key must be at least 12 characters"
        if any(c.isspace() for c in api_key):
            return "API key must not contain whitespace"
        if not api_key.isascii() or not api_key.isprintable():
            return "API key must contain only printable ASCII characters"
        return None

    def _key_version(self) -> str:
        return hashlib.sha256((self.api_key_hash or "").encode()).hexdigest()[:16]


class LoginThrottle:
    FREE_ATTEMPTS = 5
    MAX_DELAY_SECONDS = 15 * 60
    MAX_TRACKED_CLIENTS = 10_000

    def __init__(self):
        self._failures: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def retry_after(self, client: str) -> int:
        with self._lock:
            count, last = self._failures.get(client, (0, 0.0))
        if count < LoginThrottle.FREE_ATTEMPTS:
            return 0
        delay = min(2 ** (count - LoginThrottle.FREE_ATTEMPTS), LoginThrottle.MAX_DELAY_SECONDS)
        remaining = int(last + delay - time.monotonic()) + 1
        return max(remaining, 0)

    def record_failure(self, client: str) -> None:
        with self._lock:
            if len(self._failures) >= LoginThrottle.MAX_TRACKED_CLIENTS and client not in self._failures:
                # Forget the stalest entry so a spray of source addresses cannot grow this forever.
                oldest = min(self._failures, key=lambda key: self._failures[key][1])
                self._failures.pop(oldest)
            count, _ = self._failures.get(client, (0, 0.0))
            self._failures[client] = (count + 1, time.monotonic())

    def record_success(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)
