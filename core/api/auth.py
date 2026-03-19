"""
API 与配置页鉴权。

- auth.api_key: 保护 /{type}/v1/*
- auth.config_secret: 保护 /config 与 /api/config、/api/types

全局鉴权设置优先级：数据库 > 环境变量回退 > YAML > 默认值。
config_secret 在文件模式下会回写为带前缀的 PBKDF2 哈希；环境变量回退模式下仅在内存中哈希。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Literal

from fastapi import HTTPException, Request, status

from core.config.repository import (
    APP_SETTING_AUTH_API_KEY,
    APP_SETTING_AUTH_CONFIG_SECRET_HASH,
    ConfigRepository,
)
from core.config.settings import (
    get,
    get_config_path,
    has_env_override,
    load_config,
    reset_cache,
)

API_AUTH_REALM = "Bearer"
CONFIG_SECRET_PREFIX = "web2api_pbkdf2_sha256"
CONFIG_SECRET_ITERATIONS = 600_000
ADMIN_SESSION_COOKIE = "web2api_admin_session"
DEFAULT_ADMIN_SESSION_TTL_SECONDS = 7 * 24 * 60 * 60
DEFAULT_ADMIN_LOGIN_MAX_FAILURES = 5
DEFAULT_ADMIN_LOGIN_LOCK_SECONDS = 10 * 60
AuthSource = Literal["env", "db", "yaml", "default"]


@dataclass(frozen=True)
class EffectiveAuthSettings:
    api_key_text: str
    api_key_source: AuthSource
    config_secret_hash: str
    config_secret_source: AuthSource

    @property
    def api_keys(self) -> list[str]:
        return parse_api_keys(self.api_key_text)

    @property
    def api_key_env_managed(self) -> bool:
        return False

    @property
    def config_secret_env_managed(self) -> bool:
        return False

    @property
    def config_login_enabled(self) -> bool:
        return bool(self.config_secret_hash)


def parse_api_keys(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if raw is None:
        return []
    text = str(raw).replace("\n", ",")
    return [part.strip() for part in text.split(",") if part.strip()]


def normalize_api_key_text(raw: Any) -> str:
    if isinstance(raw, list):
        return "\n".join(str(item).strip() for item in raw if str(item).strip())
    return str(raw or "").strip()


def _yaml_auth_config() -> dict[str, Any]:
    auth_cfg = load_config().get("auth") or {}
    return auth_cfg if isinstance(auth_cfg, dict) else {}


def _normalize_config_secret_hash(value: Any) -> str:
    secret = str(value or "").strip()
    if not secret:
        return ""
    return secret if _is_hashed_config_secret(secret) else hash_config_secret(secret)


@lru_cache(maxsize=1)
def _hosted_config_secret_hash() -> str:
    return _normalize_config_secret_hash(get("auth", "config_secret", ""))


def build_effective_auth_settings(
    repo: ConfigRepository | None = None,
) -> EffectiveAuthSettings:
    stored = repo.load_app_settings() if repo is not None else {}
    yaml_auth = _yaml_auth_config()

    if APP_SETTING_AUTH_API_KEY in stored:
        api_key_text = normalize_api_key_text(stored.get(APP_SETTING_AUTH_API_KEY, ""))
        api_key_source: AuthSource = "db"
    elif has_env_override("auth", "api_key"):
        api_key_text = normalize_api_key_text(get("auth", "api_key", ""))
        api_key_source = "env"
    elif "api_key" in yaml_auth:
        api_key_text = normalize_api_key_text(yaml_auth.get("api_key", ""))
        api_key_source = "yaml"
    else:
        api_key_text = ""
        api_key_source = "default"

    if APP_SETTING_AUTH_CONFIG_SECRET_HASH in stored:
        config_secret_hash = _normalize_config_secret_hash(
            stored.get(APP_SETTING_AUTH_CONFIG_SECRET_HASH, "")
        )
        config_secret_source: AuthSource = "db"
    elif has_env_override("auth", "config_secret"):
        config_secret_hash = _hosted_config_secret_hash()
        config_secret_source = "env"
    elif "config_secret" in yaml_auth:
        config_secret_hash = _normalize_config_secret_hash(yaml_auth.get("config_secret", ""))
        config_secret_source = "yaml"
    else:
        config_secret_hash = ""
        config_secret_source = "default"

    return EffectiveAuthSettings(
        api_key_text=api_key_text,
        api_key_source=api_key_source,
        config_secret_hash=config_secret_hash,
        config_secret_source=config_secret_source,
    )


def refresh_runtime_auth_settings(app: Any) -> EffectiveAuthSettings:
    repo = getattr(app.state, "config_repo", None)
    settings = build_effective_auth_settings(repo)
    app.state.auth_settings = settings
    return settings


def get_effective_auth_settings(request: Request | None = None) -> EffectiveAuthSettings:
    if request is not None:
        settings = getattr(request.app.state, "auth_settings", None)
        if isinstance(settings, EffectiveAuthSettings):
            return settings
        repo = getattr(request.app.state, "config_repo", None)
        return build_effective_auth_settings(repo)
    return build_effective_auth_settings()


def configured_api_keys(repo: ConfigRepository | None = None) -> list[str]:
    return build_effective_auth_settings(repo).api_keys


def _extract_request_api_key(request: Request) -> str:
    key = (request.headers.get("x-api-key") or "").strip()
    if key:
        return key
    authorization = (request.headers.get("authorization") or "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


def require_api_key(request: Request) -> None:
    expected_keys = get_effective_auth_settings(request).api_keys
    if not expected_keys:
        return
    provided = _extract_request_api_key(request)
    if provided:
        for expected in expected_keys:
            if secrets.compare_digest(provided, expected):
                return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unauthorized. Provide a valid API key.",
        headers={"WWW-Authenticate": API_AUTH_REALM},
    )


def _is_hashed_config_secret(value: str) -> bool:
    return value.startswith(f"{CONFIG_SECRET_PREFIX}$")


def configured_config_secret_hash(repo: ConfigRepository | None = None) -> str:
    return build_effective_auth_settings(repo).config_secret_hash


def config_login_enabled(request: Request | None = None) -> bool:
    return get_effective_auth_settings(request).config_login_enabled


def configured_config_login_max_failures() -> int:
    raw = get("auth", "config_login_max_failures", DEFAULT_ADMIN_LOGIN_MAX_FAILURES)
    try:
        return max(1, int(raw))
    except Exception:
        return DEFAULT_ADMIN_LOGIN_MAX_FAILURES


def configured_config_login_lock_seconds() -> int:
    raw = get("auth", "config_login_lock_seconds", DEFAULT_ADMIN_LOGIN_LOCK_SECONDS)
    try:
        return max(1, int(raw))
    except Exception:
        return DEFAULT_ADMIN_LOGIN_LOCK_SECONDS


def hash_config_secret(secret: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        secret.encode("utf-8"),
        salt,
        CONFIG_SECRET_ITERATIONS,
    )
    return (
        f"{CONFIG_SECRET_PREFIX}"
        f"${CONFIG_SECRET_ITERATIONS}"
        f"${base64.urlsafe_b64encode(salt).decode('ascii')}"
        f"${base64.urlsafe_b64encode(digest).decode('ascii')}"
    )


def verify_config_secret(secret: str, encoded: str) -> bool:
    try:
        prefix, iterations_s, salt_b64, digest_b64 = encoded.split("$", 3)
    except ValueError:
        return False
    if prefix != CONFIG_SECRET_PREFIX:
        return False
    try:
        iterations = int(iterations_s)
        salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_b64.encode("ascii"))
    except Exception:
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256",
        secret.encode("utf-8"),
        salt,
        iterations,
    )
    return hmac.compare_digest(actual, expected)


def ensure_config_secret_hashed(repo: ConfigRepository | None = None) -> None:
    if has_env_override("auth", "config_secret"):
        _hosted_config_secret_hash()
        return
    if repo is not None and repo.get_app_setting(APP_SETTING_AUTH_CONFIG_SECRET_HASH) is not None:
        return
    cfg = load_config()
    auth_cfg = cfg.get("auth")
    if not isinstance(auth_cfg, dict):
        return
    raw_value = auth_cfg.get("config_secret")
    secret = str(raw_value or "").strip()
    if not secret or _is_hashed_config_secret(secret):
        return
    encoded = hash_config_secret(secret)
    config_path = get_config_path()
    if not config_path.exists():
        return
    original = config_path.read_text(encoding="utf-8")
    pattern = re.compile(r"^([ \t]*)config_secret\s*:\s*.*$", re.MULTILINE)
    replacement = None
    for line in original.splitlines():
        match = pattern.match(line)
        if match:
            replacement = f"{match.group(1)}config_secret: '{encoded}'"
            break
    updated: str
    if replacement is not None:
        updated, count = pattern.subn(replacement, original, count=1)
        if count != 1:
            return
    else:
        auth_pattern = re.compile(r"^auth\s*:\s*$", re.MULTILINE)
        match = auth_pattern.search(original)
        if match:
            insert_at = match.end()
            updated = (
                original[:insert_at]
                + "\n"
                + f"  config_secret: '{encoded}'"
                + original[insert_at:]
            )
        else:
            suffix = "" if original.endswith("\n") or not original else "\n"
            updated = (
                original
                + suffix
                + "auth:\n"
                + f"  config_secret: '{encoded}'\n"
            )
    tmp_path = config_path.with_suffix(config_path.suffix + ".tmp")
    tmp_path.write_text(updated, encoding="utf-8")
    tmp_path.replace(config_path)
    reset_cache()
    load_config()


@dataclass
class AdminSessionStore:
    ttl_seconds: int = DEFAULT_ADMIN_SESSION_TTL_SECONDS
    _sessions: dict[str, float] = field(default_factory=dict)

    def create(self) -> str:
        token = secrets.token_urlsafe(32)
        self._sessions[token] = time.time() + self.ttl_seconds
        return token

    def is_valid(self, token: str) -> bool:
        if not token:
            return False
        self.cleanup()
        expires_at = self._sessions.get(token)
        if expires_at is None:
            return False
        if expires_at < time.time():
            self._sessions.pop(token, None)
            return False
        return True

    def revoke(self, token: str) -> None:
        if token:
            self._sessions.pop(token, None)

    def cleanup(self) -> None:
        now = time.time()
        expired = [token for token, expires_at in self._sessions.items() if expires_at < now]
        for token in expired:
            self._sessions.pop(token, None)


@dataclass
class LoginAttemptState:
    failures: int = 0
    locked_until: float = 0.0
    last_seen: float = 0.0


@dataclass
class AdminLoginAttemptStore:
    max_failures: int = DEFAULT_ADMIN_LOGIN_MAX_FAILURES
    lock_seconds: int = DEFAULT_ADMIN_LOGIN_LOCK_SECONDS
    _attempts: dict[str, LoginAttemptState] = field(default_factory=dict)

    def is_locked(self, client_ip: str) -> int:
        self.cleanup()
        state = self._attempts.get(client_ip)
        if state is None:
            return 0
        remaining = int(state.locked_until - time.time())
        if remaining <= 0:
            return 0
        return remaining

    def record_failure(self, client_ip: str) -> int:
        now = time.time()
        state = self._attempts.setdefault(client_ip, LoginAttemptState())
        if state.locked_until > now:
            state.last_seen = now
            return int(state.locked_until - now)
        state.failures += 1
        state.last_seen = now
        if state.failures >= self.max_failures:
            state.failures = 0
            state.locked_until = now + self.lock_seconds
            return self.lock_seconds
        return 0

    def record_success(self, client_ip: str) -> None:
        self._attempts.pop(client_ip, None)

    def cleanup(self) -> None:
        now = time.time()
        stale_before = now - max(self.lock_seconds * 2, 3600)
        expired = [
            ip
            for ip, state in self._attempts.items()
            if state.locked_until <= now and state.last_seen < stale_before
        ]
        for ip in expired:
            self._attempts.pop(ip, None)


def _admin_store(request: Request) -> AdminSessionStore:
    store = getattr(request.app.state, "admin_sessions", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Admin session store is unavailable")
    return store


def _admin_login_attempt_store(request: Request) -> AdminLoginAttemptStore:
    store = getattr(request.app.state, "admin_login_attempts", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Login rate limiter is unavailable")
    return store


def client_ip_of(request: Request) -> str:
    client = getattr(request, "client", None)
    host = getattr(client, "host", None)
    return str(host or "unknown")


def check_admin_login_rate_limit(request: Request) -> None:
    remaining = _admin_login_attempt_store(request).is_locked(client_ip_of(request))
    if remaining > 0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed login attempts. Try again in {remaining} seconds.",
        )


def record_admin_login_failure(request: Request) -> int:
    return _admin_login_attempt_store(request).record_failure(client_ip_of(request))


def record_admin_login_success(request: Request) -> None:
    _admin_login_attempt_store(request).record_success(client_ip_of(request))


def admin_logged_in(request: Request) -> bool:
    if not config_login_enabled(request):
        return False
    token = (request.cookies.get(ADMIN_SESSION_COOKIE) or "").strip()
    return _admin_store(request).is_valid(token)


def require_config_login_enabled(request: Request | None = None) -> None:
    if not config_login_enabled(request):
        raise HTTPException(status_code=404, detail="Config dashboard is disabled")


def require_config_login(request: Request) -> None:
    require_config_login_enabled(request)
    if admin_logged_in(request):
        return
    raise HTTPException(status_code=401, detail="Please sign in to access the config dashboard")
