"""
会话缓存：session_id 全局唯一，映射到 (proxy_key, type, account_id)。

当前架构下 session 绑定到某个 tab/account：

- tab 被关闭或切号时，需要批量失效该 tab 下的 session
- 单个 session 失效时，需要从缓存中移除，后续按完整历史重建
- 超过 TTL 的 session 在维护循环中被自动清理
"""

from dataclasses import dataclass
import time

from core.runtime.keys import ProxyKey

# Sessions older than this are eligible for eviction during maintenance.
SESSION_TTL_SECONDS = 1800.0  # 30 minutes


@dataclass
class SessionEntry:
    """单条会话：用于通过 session_id 反查 context/page 与账号。"""

    proxy_key: ProxyKey
    type_name: str
    account_id: str
    last_used_at: float


class SessionCache:
    """进程内会话缓存，不持久化、不跨进程。"""

    def __init__(self) -> None:
        self._store: dict[str, SessionEntry] = {}

    def get(self, session_id: str) -> SessionEntry | None:
        return self._store.get(session_id)

    def put(
        self,
        session_id: str,
        proxy_key: ProxyKey,
        type_name: str,
        account_id: str,
    ) -> None:
        self._store[session_id] = SessionEntry(
            proxy_key=proxy_key,
            type_name=type_name,
            account_id=account_id,
            last_used_at=time.time(),
        )

    def touch(self, session_id: str) -> None:
        entry = self._store.get(session_id)
        if entry is not None:
            entry.last_used_at = time.time()

    def delete(self, session_id: str) -> None:
        self._store.pop(session_id, None)

    def delete_many(self, session_ids: list[str] | set[str]) -> None:
        for session_id in session_ids:
            self._store.pop(session_id, None)

    def evict_stale(self, ttl: float = SESSION_TTL_SECONDS) -> list[str]:
        """Remove sessions older than *ttl* seconds. Returns evicted IDs."""
        now = time.time()
        stale = [
            sid
            for sid, entry in self._store.items()
            if (now - entry.last_used_at) > ttl
        ]
        for sid in stale:
            del self._store[sid]
        return stale

    def __contains__(self, session_id: str) -> bool:
        return session_id in self._store

    def __len__(self) -> int:
        return len(self._store)
