"""会话指纹索引：替代 sticky session，通过指纹精确匹配同一逻辑对话。

指纹 = sha256(system_prompt + first_user_message)[:16]，
同一对话的指纹恒定，不同对话指纹不同，杜绝上下文污染。
"""

import time
from dataclasses import dataclass, field


@dataclass
class ConversationEntry:
    session_id: str
    fingerprint: str
    message_count: int
    account_id: str
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)


class ConversationIndex:
    """进程内指纹索引，不持久化。"""

    def __init__(self) -> None:
        self._by_fingerprint: dict[str, ConversationEntry] = {}
        self._by_session_id: dict[str, ConversationEntry] = {}

    def register(
        self,
        fingerprint: str,
        session_id: str,
        message_count: int,
        account_id: str,
    ) -> None:
        # Remove old entry for this fingerprint if exists
        old = self._by_fingerprint.pop(fingerprint, None)
        if old is not None:
            self._by_session_id.pop(old.session_id, None)
        entry = ConversationEntry(
            session_id=session_id,
            fingerprint=fingerprint,
            message_count=message_count,
            account_id=account_id,
        )
        self._by_fingerprint[fingerprint] = entry
        self._by_session_id[session_id] = entry

    def lookup(self, fingerprint: str) -> ConversationEntry | None:
        entry = self._by_fingerprint.get(fingerprint)
        if entry is not None:
            entry.last_used_at = time.time()
        return entry

    def remove_session(self, session_id: str) -> None:
        entry = self._by_session_id.pop(session_id, None)
        if entry is not None:
            self._by_fingerprint.pop(entry.fingerprint, None)

    def evict_stale(self, ttl: float) -> list[str]:
        """Remove entries older than *ttl* seconds. Returns evicted session IDs."""
        now = time.time()
        stale = [
            e.session_id
            for e in self._by_fingerprint.values()
            if (now - e.last_used_at) > ttl
        ]
        for sid in stale:
            self.remove_session(sid)
        return stale

    def __len__(self) -> int:
        return len(self._by_fingerprint)
