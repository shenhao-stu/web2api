"""
统一的 YAML 配置加载。

优先级：
1. WEB2API_CONFIG_PATH 指定的路径
2. 项目根目录下的 config.local.yaml
3. 项目根目录下的 config.yaml

同时支持通过环境变量覆盖单个配置项：
- 通用规则：WEB2API_<SECTION>_<KEY>
- 额外兼容：server.host -> HOST，server.port -> PORT
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_CONFIG_ENV_KEY = "WEB2API_CONFIG_PATH"
_LOCAL_CONFIG_NAME = "config.local.yaml"
_DEFAULT_CONFIG_NAME = "config.yaml"
_ENV_MISSING = object()
_ENV_OVERRIDE_ALIASES: dict[tuple[str, str], tuple[str, ...]] = {
    ("server", "host"): ("HOST",),
    ("server", "port"): ("PORT",),
}
_DATABASE_URL_ENV_NAMES = ("WEB2API_DATABASE_URL", "DATABASE_URL")
_BOOL_TRUE_VALUES = {"1", "true", "yes", "on"}
_BOOL_FALSE_VALUES = {"0", "false", "no", "off"}


def _resolve_config_path() -> Path:
    configured = os.environ.get(_CONFIG_ENV_KEY, "").strip()
    if configured:
        return Path(configured).expanduser()
    local_config = _PROJECT_ROOT / _LOCAL_CONFIG_NAME
    if local_config.exists():
        return local_config
    return _PROJECT_ROOT / _DEFAULT_CONFIG_NAME


_CONFIG_PATH = _resolve_config_path()

_config_cache: dict[str, Any] | None = None


def _env_override_names(section: str, key: str) -> tuple[str, ...]:
    generic = f"WEB2API_{section}_{key}".upper().replace("-", "_")
    aliases = _ENV_OVERRIDE_ALIASES.get((section, key), ())
    ordered = [generic]
    ordered.extend(alias for alias in aliases if alias != generic)
    return tuple(ordered)


def _get_env_override(section: str, key: str) -> Any:
    for env_name in _env_override_names(section, key):
        if env_name in os.environ:
            return os.environ[env_name]
    return _ENV_MISSING


def has_env_override(section: str, key: str) -> bool:
    return _get_env_override(section, key) is not _ENV_MISSING


def get_config_path() -> Path:
    return _CONFIG_PATH


def reset_cache() -> None:
    global _config_cache
    _config_cache = None


def load_config() -> dict[str, Any]:
    """按优先级加载配置文件，不存在时返回空 dict。"""
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    if not _CONFIG_PATH.exists():
        _config_cache = {}
        return {}
    try:
        with _CONFIG_PATH.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            _config_cache = {}
        else:
            _config_cache = dict(data)
    except Exception:
        _config_cache = {}
    return _config_cache


def get(section: str, key: str, default: Any = None) -> Any:
    """从配置中读取 section.key，环境变量优先，其次 YAML，最后返回 default。"""
    env_override = _get_env_override(section, key)
    if env_override is not _ENV_MISSING:
        return env_override
    cfg = load_config().get(section) or {}
    if not isinstance(cfg, dict):
        return default
    val = cfg.get(key)
    return val if val is not None else default


def coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _BOOL_TRUE_VALUES:
            return True
        if normalized in _BOOL_FALSE_VALUES:
            return False
    return bool(default)


def get_bool(section: str, key: str, default: bool = False) -> bool:
    """从配置读取布尔值，兼容 true/false、1/0、yes/no、on/off。"""
    return coerce_bool(get(section, key, default), default)


def get_server_host(default: str = "127.0.0.1") -> str:
    return str(get("server", "host") or default).strip() or default


def get_server_port(default: int = 8001) -> int:
    try:
        return int(str(get("server", "port") or default).strip())
    except Exception:
        return default


def get_database_url(default: str = "") -> str:
    for env_name in _DATABASE_URL_ENV_NAMES:
        value = os.environ.get(env_name, "").strip()
        if value:
            return value
    return default
