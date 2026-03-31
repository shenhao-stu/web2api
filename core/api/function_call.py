"""
Function Call 层：解析模型输出的 <tool_call> 格式，转换为 OpenAI tool_calls；
将 tools 和 tool 结果拼入 prompt。对外统一使用 OpenAI 格式。
"""

import json
import re
import uuid
from collections.abc import Callable
from typing import Any

TOOL_CALL_PREFIX = "<tool_call>"
TOOL_CALL_PREFIX_LEN = len(TOOL_CALL_PREFIX)
TOOL_CALL_PATTERN = re.compile(
    r"<tool_call>\s*(.*?)\s*</tool_call>",
    re.DOTALL,
)


def parse_tool_calls(text: str) -> list[dict[str, Any]]:
    """
    从文本中解析所有 <tool_call>...</tool_call> 块。
    返回 [{"name": str, "arguments": dict | str}, ...]
    """
    if not text or not text.strip():
        return []
    matches = TOOL_CALL_PATTERN.findall(text)
    result: list[dict[str, Any]] = []
    for m in matches:
        try:
            obj = json.loads(m.strip())
            if isinstance(obj, dict) and "name" in obj:
                args = obj.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                result.append({"name": obj["name"], "arguments": args})
        except json.JSONDecodeError:
            pass
    return result


def detect_tool_call_mode(buffer: str, *, strip_session_id: bool = True) -> bool | None:
    """
    根据 buffer 内容判断是否为 tool_call 模式。
    None=尚未确定，True=tool_call，False=普通文本。
    strip_session_id: 若 True，先去掉开头的零宽 session_id 前缀再判断。
    """
    content = buffer
    if strip_session_id:
        from core.api.conv_parser import strip_session_id_suffix

        content = strip_session_id_suffix(buffer)
    stripped = content.lstrip()
    if stripped.startswith(TOOL_CALL_PREFIX):
        return True
    if len(stripped) > TOOL_CALL_PREFIX_LEN:
        return False
    return None


def format_tools_for_prompt(tools: list[dict[str, Any]]) -> str:
    """
    将 OpenAI 格式的 tools 转为可读文本，用于 prompt。
    兼容 OpenAI 格式 {type, function: {name, description, parameters}}
    和 Cursor 格式 {name, description, input_schema}。
    """
    if not tools:
        return ""
    lines: list[str] = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if t.get("type") == "function" else t
        if not isinstance(fn, dict):
            fn = t
        name = fn.get("name")
        if not name:
            continue
        desc = fn.get("description") or fn.get("summary") or ""
        params = fn.get("parameters") or fn.get("input_schema") or {}
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except json.JSONDecodeError:
                params = {}
        props = params.get("properties") or {}
        required = params.get("required") or []
        args_desc = ", ".join(
            f"{k}: {v.get('type', 'any')}" + (" (必填)" if k in required else "")
            for k, v in props.items()
        )
        lines.append(
            f"- {name}({args_desc}): {desc[:200]}" + ("..." if len(desc) > 200 else "")
        )
    return "\n".join(lines) if lines else ""


def build_tool_calls_response(
    tool_calls_list: list[dict[str, Any]],
    chat_id: str,
    model: str,
    created: int,
    *,
    text_content: str = "",
) -> dict[str, Any]:
    """返回 OpenAI 格式的 chat.completion（含 tool_calls）。
    message.content 为字符串（或空时 null），tool_calls 为 OpenAI 标准数组。
    """
    tool_calls: list[dict[str, Any]] = []
    for tc in tool_calls_list:
        name = tc.get("name", "")
        args = tc.get("arguments", {})
        if isinstance(args, dict):
            args_str = json.dumps(args, ensure_ascii=False)
        else:
            try:
                args_obj = json.loads(str(args)) if args else {}
                args_str = json.dumps(args_obj, ensure_ascii=False)
            except json.JSONDecodeError:
                args_str = "{}"
        call_id = f"call_{uuid.uuid4().hex[:24]}"
        tool_calls.append(
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": args_str},
            }
        )
    message: dict[str, Any] = {
        "role": "assistant",
        "content": text_content if text_content else None,
        "tool_calls": tool_calls,
    }
    return {
        "id": chat_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls",
            }
        ],
    }


def _openai_sse_chunk(
    chat_id: str,
    model: str,
    created: int,
    delta: dict,
    finish_reason: str | None = None,
) -> str:
    """构建 OpenAI 流式 SSE：data: <json>\\n\\n"""
    choice: dict[str, Any] = {"index": 0, "delta": delta}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    data = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [choice],
    }
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def build_openai_text_sse_events(
    chat_id: str,
    model: str,
    created: int,
) -> tuple[str, Callable[[str], str], Callable[[], str]]:
    """返回 OpenAI 流式事件的工厂。
    返回 (msg_start_sse, make_delta_sse, make_stop_sse)。
    msg_start 为带 role 的首 chunk。
    """

    def msg_start() -> str:
        return _openai_sse_chunk(
            chat_id,
            model,
            created,
            delta={"role": "assistant", "content": ""},
            finish_reason=None,
        )

    def make_delta_sse(text: str) -> str:
        return _openai_sse_chunk(
            chat_id,
            model,
            created,
            delta={
                "content": text,
            },
            finish_reason=None,
        )

    def make_stop_sse() -> str:
        return (
            _openai_sse_chunk(
                chat_id,
                model,
                created,
                delta={},
                finish_reason="stop",
            )
            + "data: [DONE]\n\n"
        )

    return msg_start(), make_delta_sse, make_stop_sse


def build_tool_calls_with_ids(
    tool_calls_list: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """从 name+arguments 的 tool_calls_list 构建带 id 的 OpenAI 格式 tool_calls。
    用于流式下发与 debug 保存共用同一批 id，保证下一轮 request 的 tool_call_id 一致。
    """
    tool_calls: list[dict[str, Any]] = []
    for i, tc in enumerate(tool_calls_list):
        name = tc.get("name", "")
        args = tc.get("arguments", {})
        if isinstance(args, dict):
            args_str = json.dumps(args, ensure_ascii=False)
        else:
            try:
                args_obj = json.loads(str(args)) if args else {}
                args_str = json.dumps(args_obj, ensure_ascii=False)
            except json.JSONDecodeError:
                args_str = "{}"
        tool_calls.append(
            {
                "index": i,
                "id": f"call_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {"name": name, "arguments": args_str},
            }
        )
    return tool_calls


def build_openai_tool_use_sse_events(
    tool_calls_list: list[dict[str, Any]],
    chat_id: str,
    model: str,
    created: int,
    *,
    text_content: str = "",
    tool_calls_with_ids: list[dict[str, Any]] | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """构建 OpenAI 流式 SSE 事件，用于 tool_calls 场景。
    有 text_content（如 thinking）时：先发 content chunk，再发 tool_calls chunk，便于客户端先展示思考再展示工具调用。
    无 text_content 时：单 chunk 发 role + tool_calls。
    tool_calls 场景最后只发 finish_reason，不发 data: [DONE]（think 之后不跟 [DONE]）。
    """
    if tool_calls_with_ids is not None:
        tool_calls = tool_calls_with_ids
    else:
        tool_calls = build_tool_calls_with_ids(tool_calls_list)
    sse_list: list[str] = []
    if text_content:
        # 先发 content（thinking），再发 tool_calls，同一条消息内顺序展示
        sse_list.append(
            _openai_sse_chunk(
                chat_id,
                model,
                created,
                {"role": "assistant", "content": text_content},
                None,
            )
        )
        sse_list.append(
            _openai_sse_chunk(chat_id, model, created, {"tool_calls": tool_calls}, None)
        )
    else:
        sse_list.append(
            _openai_sse_chunk(
                chat_id,
                model,
                created,
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": tool_calls,
                },
                None,
            )
        )
    sse_list.append(_openai_sse_chunk(chat_id, model, created, {}, "tool_calls"))
    return (sse_list, tool_calls)


def stream_openai_tool_use_sse_events(
    tool_calls_list: list[dict[str, Any]],
    chat_id: str,
    model: str,
    created: int,
    *,
    tool_calls_with_ids: list[dict[str, Any]] | None = None,
) -> list[str]:
    """
    流式下发 tool_calls：先发每个 tool 的 id/name（arguments 为空），
    再逐个发 arguments 分片，最后发 finish_reason。便于客户端逐步展示。
    content（如 <think>）由调用方已通过 delta 流式发完，此处只发 tool_calls 相关 chunk。
    """
    if tool_calls_with_ids is not None:
        tool_calls = tool_calls_with_ids
    else:
        tool_calls = build_tool_calls_with_ids(tool_calls_list)
    sse_list: list[str] = []
    # 第一块：仅 id + type + name，arguments 为空，让客户端先展示“正在调用 xxx”
    tool_calls_heads: list[dict[str, Any]] = []
    for tc in tool_calls:
        tool_calls_heads.append(
            {
                "index": tc["index"],
                "id": tc["id"],
                "type": "function",
                "function": {"name": tc["function"]["name"], "arguments": ""},
            }
        )
    sse_list.append(
        _openai_sse_chunk(
            chat_id, model, created, {"tool_calls": tool_calls_heads}, None
        )
    )
    # 后续每块：只带 index + function.arguments，可整段发或分片发，这里按 tool 逐个发
    for tc in tool_calls:
        args = tc.get("function", {}).get("arguments", "") or ""
        if not args:
            continue
        sse_list.append(
            _openai_sse_chunk(
                chat_id,
                model,
                created,
                {
                    "tool_calls": [
                        {"index": tc["index"], "function": {"arguments": args}}
                    ]
                },
                None,
            )
        )
    sse_list.append(_openai_sse_chunk(chat_id, model, created, {}, "tool_calls"))
    return sse_list
