"""
配置持久化：默认使用 SQLite；提供 DATABASE_URL / WEB2API_DATABASE_URL 时切换到 PostgreSQL。
表结构：proxy_group, account（含 name, type, auth JSON），以及 app_setting。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from core.config.schema import AccountConfig, ProxyGroupConfig, account_from_row
from core.config.settings import coerce_bool, get_database_url


DB_FILENAME = "db.sqlite3"
DB_PATH_ENV_KEY = "WEB2API_DB_PATH"
APP_SETTING_AUTH_API_KEY = "auth.api_key"
APP_SETTING_AUTH_CONFIG_SECRET_HASH = "auth.config_secret_hash"


def _get_db_path() -> Path:
    """SQLite 文件路径。"""
    configured = os.environ.get(DB_PATH_ENV_KEY, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parent.parent.parent / DB_FILENAME


def create_config_repository(
    db_path: Path | None = None,
    database_url: str | None = None,
) -> "ConfigRepository":
    resolved_database_url = (
        get_database_url().strip() if database_url is None else database_url.strip()
    )
    return ConfigRepository(
        _PostgresConfigRepository(resolved_database_url)
        if resolved_database_url
        else _SqliteConfigRepository(db_path or _get_db_path())
    )


class _RepositoryBase:
    def init_schema(self) -> None:
        raise NotImplementedError

    def load_groups(self) -> list[ProxyGroupConfig]:
        raise NotImplementedError

    def save_groups(self, groups: list[ProxyGroupConfig]) -> None:
        raise NotImplementedError

    def update_account_unfreeze_at(
        self,
        fingerprint_id: str,
        account_name: str,
        unfreeze_at: int | None,
    ) -> None:
        raise NotImplementedError

    def load_raw(self) -> list[dict[str, Any]]:
        """与前端/API 一致的原始列表格式。"""
        groups = self.load_groups()
        return [
            {
                "proxy_host": g.proxy_host,
                "proxy_user": g.proxy_user,
                "proxy_pass": g.proxy_pass,
                "fingerprint_id": g.fingerprint_id,
                "use_proxy": g.use_proxy,
                "timezone": g.timezone,
                "accounts": [
                    {
                        "name": a.name,
                        "type": a.type,
                        "auth": a.auth,
                        "enabled": a.enabled,
                        "unfreeze_at": a.unfreeze_at,
                    }
                    for a in g.accounts
                ],
            }
            for g in groups
        ]

    def load_app_settings(self) -> dict[str, str]:
        raise NotImplementedError

    def get_app_setting(self, key: str) -> str | None:
        value = self.load_app_settings().get(key)
        return value if value is not None else None

    def set_app_setting(self, key: str, value: str | None) -> None:
        raise NotImplementedError

    def save_raw(self, raw: list[dict[str, Any]]) -> None:
        """从 API/前端原始格式写入并保存。"""
        groups = _raw_to_groups(raw)
        self.save_groups(groups)


class _SqliteConfigRepository(_RepositoryBase):
    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or _get_db_path()
        self._schema_initialized = False

    def _conn(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(self._db_path)

    def _init_tables(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS proxy_group (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                proxy_host TEXT NOT NULL,
                proxy_user TEXT NOT NULL,
                proxy_pass TEXT NOT NULL,
                fingerprint_id TEXT NOT NULL DEFAULT '',
                use_proxy INTEGER NOT NULL DEFAULT 1,
                timezone TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS account (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                proxy_group_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                auth TEXT NOT NULL DEFAULT '{}',
                enabled INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY (proxy_group_id) REFERENCES proxy_group(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_account_proxy_group_id ON account(proxy_group_id)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS ix_account_type ON account(type)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_setting (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            )
            """
        )
        try:
            conn.execute("ALTER TABLE account ADD COLUMN unfreeze_at INTEGER")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute(
                "ALTER TABLE account ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"
            )
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute(
                "ALTER TABLE proxy_group ADD COLUMN use_proxy INTEGER NOT NULL DEFAULT 1"
            )
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE proxy_group ADD COLUMN timezone TEXT")
        except sqlite3.OperationalError:
            pass
        conn.commit()

    def _ensure_schema(self) -> None:
        if self._schema_initialized:
            return
        conn = self._conn()
        try:
            self._init_tables(conn)
            self._schema_initialized = True
        finally:
            conn.close()

    def init_schema(self) -> None:
        self._ensure_schema()

    def load_groups(self) -> list[ProxyGroupConfig]:
        self._ensure_schema()
        conn = self._conn()
        try:
            groups: list[ProxyGroupConfig] = []
            group_rows = conn.execute(
                """
                SELECT id, proxy_host, proxy_user, proxy_pass, fingerprint_id, use_proxy, timezone
                FROM proxy_group ORDER BY id ASC
                """
            ).fetchall()
            accounts_by_group: dict[int, list[AccountConfig]] = {}
            for gid, name, type_, auth_json, enabled, unfreeze_at in conn.execute(
                """
                SELECT proxy_group_id, name, type, auth, enabled, unfreeze_at
                FROM account ORDER BY proxy_group_id ASC, id ASC
                """
            ).fetchall():
                accounts_by_group.setdefault(int(gid), []).append(
                    account_from_row(
                        name,
                        type_,
                        auth_json or "{}",
                        enabled=bool(enabled) if enabled is not None else True,
                        unfreeze_at=unfreeze_at,
                    )
                )
            for gid, proxy_host, proxy_user, proxy_pass, fingerprint_id, use_proxy, timezone in group_rows:
                groups.append(
                    ProxyGroupConfig(
                        proxy_host=proxy_host,
                        proxy_user=proxy_user,
                        proxy_pass=proxy_pass,
                        fingerprint_id=fingerprint_id or "",
                        use_proxy=bool(use_proxy),
                        timezone=timezone,
                        accounts=accounts_by_group.get(int(gid), []),
                    )
                )
            return groups
        finally:
            conn.close()

    def save_groups(self, groups: list[ProxyGroupConfig]) -> None:
        self._ensure_schema()
        conn = self._conn()
        try:
            conn.execute("DELETE FROM account")
            conn.execute("DELETE FROM proxy_group")
            for group in groups:
                cur = conn.execute(
                    """
                    INSERT INTO proxy_group (proxy_host, proxy_user, proxy_pass, fingerprint_id, use_proxy, timezone)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        group.proxy_host,
                        group.proxy_user,
                        group.proxy_pass,
                        group.fingerprint_id,
                        1 if group.use_proxy else 0,
                        group.timezone,
                    ),
                )
                gid = cur.lastrowid
                for account in group.accounts:
                    conn.execute(
                        """
                        INSERT INTO account (proxy_group_id, name, type, auth, enabled, unfreeze_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            gid,
                            account.name,
                            account.type,
                            account.auth_json(),
                            1 if account.enabled else 0,
                            account.unfreeze_at,
                        ),
                    )
            conn.commit()
        finally:
            conn.close()

    def update_account_unfreeze_at(
        self,
        fingerprint_id: str,
        account_name: str,
        unfreeze_at: int | None,
    ) -> None:
        self._ensure_schema()
        conn = self._conn()
        try:
            conn.execute(
                """
                UPDATE account SET unfreeze_at = ?
                WHERE proxy_group_id = (SELECT id FROM proxy_group WHERE fingerprint_id = ?)
                  AND name = ?
                """,
                (unfreeze_at, fingerprint_id, account_name),
            )
            conn.commit()
        finally:
            conn.close()

    def load_app_settings(self) -> dict[str, str]:
        self._ensure_schema()
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT key, value FROM app_setting ORDER BY key ASC"
            ).fetchall()
            return {str(key): str(value) for key, value in rows}
        finally:
            conn.close()

    def set_app_setting(self, key: str, value: str | None) -> None:
        self._ensure_schema()
        conn = self._conn()
        try:
            if value is None:
                conn.execute("DELETE FROM app_setting WHERE key = ?", (key,))
            else:
                conn.execute(
                    """
                    INSERT INTO app_setting (key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (key, value),
                )
            conn.commit()
        finally:
            conn.close()


class _PostgresConfigRepository(_RepositoryBase):
    def __init__(self, database_url: str) -> None:
        self._database_url = database_url

    def _conn(self) -> Any:
        import psycopg

        return psycopg.connect(self._database_url)

    def init_schema(self) -> None:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS proxy_group (
                        id BIGSERIAL PRIMARY KEY,
                        proxy_host TEXT NOT NULL,
                        proxy_user TEXT NOT NULL,
                        proxy_pass TEXT NOT NULL,
                        fingerprint_id TEXT NOT NULL DEFAULT '',
                        use_proxy BOOLEAN NOT NULL DEFAULT TRUE,
                        timezone TEXT
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS account (
                        id BIGSERIAL PRIMARY KEY,
                        proxy_group_id BIGINT NOT NULL REFERENCES proxy_group(id) ON DELETE CASCADE,
                        name TEXT NOT NULL,
                        type TEXT NOT NULL,
                        auth TEXT NOT NULL DEFAULT '{}',
                        enabled BOOLEAN NOT NULL DEFAULT TRUE,
                        unfreeze_at BIGINT
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS ix_account_proxy_group_id ON account(proxy_group_id)"
                )
                cur.execute("CREATE INDEX IF NOT EXISTS ix_account_type ON account(type)")
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS app_setting (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL DEFAULT ''
                    )
                    """
                )

    def load_groups(self) -> list[ProxyGroupConfig]:
        groups: list[ProxyGroupConfig] = []
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, proxy_host, proxy_user, proxy_pass, fingerprint_id, use_proxy, timezone
                    FROM proxy_group ORDER BY id ASC
                    """
                )
                group_rows = cur.fetchall()
                cur.execute(
                    """
                    SELECT proxy_group_id, name, type, auth, enabled, unfreeze_at
                    FROM account ORDER BY proxy_group_id ASC, id ASC
                    """
                )
                accounts_by_group: dict[int, list[AccountConfig]] = {}
                for gid, name, type_, auth_json, enabled, unfreeze_at in cur.fetchall():
                    accounts_by_group.setdefault(int(gid), []).append(
                        account_from_row(
                            name,
                            type_,
                            auth_json or "{}",
                            enabled=bool(enabled) if enabled is not None else True,
                            unfreeze_at=unfreeze_at,
                        )
                    )
                for row in group_rows:
                    (
                        gid,
                        proxy_host,
                        proxy_user,
                        proxy_pass,
                        fingerprint_id,
                        use_proxy,
                        timezone,
                    ) = row
                    groups.append(
                        ProxyGroupConfig(
                            proxy_host=proxy_host,
                            proxy_user=proxy_user,
                            proxy_pass=proxy_pass,
                            fingerprint_id=fingerprint_id or "",
                            use_proxy=bool(use_proxy),
                            timezone=timezone,
                            accounts=accounts_by_group.get(int(gid), []),
                        )
                    )
        return groups

    def save_groups(self, groups: list[ProxyGroupConfig]) -> None:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM account")
                cur.execute("DELETE FROM proxy_group")
                for group in groups:
                    cur.execute(
                        """
                        INSERT INTO proxy_group (proxy_host, proxy_user, proxy_pass, fingerprint_id, use_proxy, timezone)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        (
                            group.proxy_host,
                            group.proxy_user,
                            group.proxy_pass,
                            group.fingerprint_id,
                            group.use_proxy,
                            group.timezone,
                        ),
                    )
                    gid = cur.fetchone()[0]
                    for account in group.accounts:
                        cur.execute(
                            """
                            INSERT INTO account (proxy_group_id, name, type, auth, enabled, unfreeze_at)
                            VALUES (%s, %s, %s, %s, %s, %s)
                            """,
                            (
                                gid,
                                account.name,
                                account.type,
                                account.auth_json(),
                                account.enabled,
                                account.unfreeze_at,
                            ),
                        )

    def update_account_unfreeze_at(
        self,
        fingerprint_id: str,
        account_name: str,
        unfreeze_at: int | None,
    ) -> None:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE account SET unfreeze_at = %s
                    WHERE proxy_group_id = (
                        SELECT id FROM proxy_group WHERE fingerprint_id = %s ORDER BY id ASC LIMIT 1
                    )
                      AND name = %s
                    """,
                    (unfreeze_at, fingerprint_id, account_name),
                )

    def load_app_settings(self) -> dict[str, str]:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT key, value FROM app_setting ORDER BY key ASC")
                return {str(key): str(value) for key, value in cur.fetchall()}

    def set_app_setting(self, key: str, value: str | None) -> None:
        with self._conn() as conn:
            with conn.cursor() as cur:
                if value is None:
                    cur.execute("DELETE FROM app_setting WHERE key = %s", (key,))
                else:
                    cur.execute(
                        """
                        INSERT INTO app_setting (key, value) VALUES (%s, %s)
                        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                        """,
                        (key, value),
                    )


class ConfigRepository(_RepositoryBase):
    """配置读写入口。"""

    def __init__(self, backend: _RepositoryBase) -> None:
        self._backend = backend

    def init_schema(self) -> None:
        self._backend.init_schema()

    def load_groups(self) -> list[ProxyGroupConfig]:
        return self._backend.load_groups()

    def save_groups(self, groups: list[ProxyGroupConfig]) -> None:
        self._backend.save_groups(groups)

    def load_raw(self) -> list[dict[str, Any]]:
        return self._backend.load_raw()

    def load_app_settings(self) -> dict[str, str]:
        return self._backend.load_app_settings()

    def get_app_setting(self, key: str) -> str | None:
        return self._backend.get_app_setting(key)

    def set_app_setting(self, key: str, value: str | None) -> None:
        self._backend.set_app_setting(key, value)

    def save_raw(self, raw: list[dict[str, Any]]) -> None:
        self._backend.save_raw(raw)

    def update_account_unfreeze_at(
        self,
        fingerprint_id: str,
        account_name: str,
        unfreeze_at: int | None,
    ) -> None:
        self._backend.update_account_unfreeze_at(
            fingerprint_id,
            account_name,
            unfreeze_at,
        )


def _raw_to_groups(raw: list[dict[str, Any]]) -> list[ProxyGroupConfig]:
    """将 API 原始列表转为 ProxyGroupConfig 列表。"""
    groups: list[ProxyGroupConfig] = []
    for group in raw:
        accounts: list[AccountConfig] = []
        for account in group.get("accounts", []):
            name = str(account.get("name", "")).strip()
            type_ = str(account.get("type", "")).strip() or "claude"
            auth = account.get("auth")
            if isinstance(auth, dict):
                pass
            elif isinstance(auth, str):
                try:
                    import json

                    auth = json.loads(auth) or {}
                except Exception:
                    auth = {}
            else:
                auth = {}
            if name:
                enabled = coerce_bool(account.get("enabled", True), True)
                unfreeze_at = account.get("unfreeze_at")
                if isinstance(unfreeze_at, (int, float)):
                    unfreeze_at = int(unfreeze_at)
                else:
                    unfreeze_at = None
                accounts.append(
                    AccountConfig(
                        name=name,
                        type=type_,
                        auth=auth,
                        enabled=enabled,
                        unfreeze_at=unfreeze_at,
                    )
                )
        groups.append(
            ProxyGroupConfig(
                proxy_host=str(group.get("proxy_host", "")),
                proxy_user=str(group.get("proxy_user", "")),
                proxy_pass=str(group.get("proxy_pass", "")),
                fingerprint_id=str(group.get("fingerprint_id", "")),
                use_proxy=coerce_bool(group.get("use_proxy", True), True),
                timezone=group.get("timezone"),
                accounts=accounts,
            )
        )
    return groups
