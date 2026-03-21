"""会话指纹：基于 system prompt + 首条 user 消息计算 SHA-256 指纹。

同一逻辑对话（相同 system + 相同首条 user）的指纹恒定，
不同对话指纹不同，杜绝上下文污染。
"""

import hashlib

from core.api.schemas import OpenAIMessage


def _norm_content(content: str | list | None) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    # list[OpenAIContentPart]
    parts: list[str] = []
    for p in content:
        if hasattr(p, "type") and p.type == "text" and p.text:
            parts.append(p.text.strip())
    return " ".join(parts)


def compute_conversation_fingerprint(messages: list[OpenAIMessage]) -> str:
    """sha256(system_prompt + first_user_message)[:16]

    Returns empty string if no user message found.
    """
    system_text = ""
    first_user_text = ""
    for m in messages:
        if m.role == "system" and not system_text:
            system_text = _norm_content(m.content)
        elif m.role == "user" and not first_user_text:
            first_user_text = _norm_content(m.content)
            break
    if not first_user_text:
        return ""
    raw = f"{system_text}\n{first_user_text}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
