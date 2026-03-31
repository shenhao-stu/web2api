"""
ReAct 流式解析器：字符级 MarkerDetector + StateMachine

将 LLM 的 ReAct 格式文本实时转换为 OpenAI SSE 流式事件：

  Thought: xxx       → delta.content = "<think>xxx</think>"  (流式)
  Action: name       → 缓存工具名
  Action Input: {}   → delta.tool_calls[0].function.arguments (流式)
  Final Answer: xxx  → delta.content = "xxx"                 (流式)
  Observation: xxx   → delta.content = "xxx"                 (流式)
  无标记文本          → delta.content = "xxx"                 (直通)

核心设计：
  MarkerDetector：默认零延迟直通，仅在遇到 Marker 首字母时暂存等待确认。
  StateMachine：IDLE / IN_THOUGHT / IN_ACTION / IN_ACTION_INPUT /
                IN_OBSERVATION / IN_FINAL
"""

import json
import uuid
from enum import Enum, auto

# ─── Marker 定义 ──────────────────────────────────────────────────────────────

# 注意顺序：仅影响精确匹配时的遍历，不影响正确性（每个 marker 唯一）
_MARKERS: tuple[str, ...] = (
    "Thought:",
    "Action Input:",  # 必须比 "Action:" 先定义（_is_prefix 依赖全集）
    "Action:",
    "Observation:",
    "Final Answer:",
    "最终答案:",
)

_MARKER_FIRST_CHARS: frozenset[str] = frozenset(m[0] for m in _MARKERS)


# ─── 状态枚举 ─────────────────────────────────────────────────────────────────


class _State(Enum):
    IDLE = auto()
    IN_THOUGHT = auto()
    IN_ACTION = auto()
    IN_ACTION_INPUT = auto()
    IN_OBSERVATION = auto()
    IN_FINAL = auto()


# ─── 解析器主体 ───────────────────────────────────────────────────────────────


class ReactStreamParser:
    """
    字符级 ReAct 流解析器，将 LLM 的 ReAct 格式输出转换为 OpenAI SSE chunks。

    用法::

        parser = ReactStreamParser(chat_id, model, created, has_tools=True)
        async for chunk in llm_stream:
            # 注意：不要对 chunk 做 strip_session_id_suffix，否则客户端收不到会话 ID，下一轮无法复用会话
            for sse in parser.feed(chunk):
                yield sse
        for sse in parser.finish():
            yield sse
    """

    def __init__(
        self,
        chat_id: str,
        model: str,
        created: int,
        *,
        has_tools: bool = True,
    ) -> None:
        self._chat_id = chat_id
        self._model = model
        self._created = created
        self._has_tools = has_tools

        # MarkerDetector 状态
        self._suspect_buf = ""
        self._skip_leading_ws = False  # 吃掉 Marker 冒号后的空白

        # StateMachine 状态
        self._state = _State.IDLE
        self._action_name_buf = ""  # 收集 Action 名称
        self._tool_call_id = ""
        self._tool_call_index = 0

        # 输出控制标志
        self._emitted_msg_start = False
        self._think_open = False  # 已发 <think>
        self._think_closed = False  # 已发 </think>
        self._tool_call_started = False  # 已发 function_call_start

    # ── 公开 API ──────────────────────────────────────────────────────────────

    def feed(self, chunk: str) -> list[str]:
        """处理一个文本 chunk，返回需要下发的 SSE 字符串列表（含 `data: ...\\n\\n`）。"""
        events: list[str] = []
        for char in chunk:
            events.extend(self._on_char(char))
        return events

    def finish(self) -> list[str]:
        """LLM 流结束时调用：flush 残留 suspect_buf，补发结束 SSE。"""
        events: list[str] = []
        if self._suspect_buf:
            buf, self._suspect_buf = self._suspect_buf, ""
            events.extend(self._dispatch(buf))
        events.extend(self._emit_end())
        return events

    # ── 字符级处理（MarkerDetector）──────────────────────────────────────────

    def _on_char(self, char: str) -> list[str]:
        # 吃掉 Marker 冒号后的单个/连续空格或制表符
        if self._skip_leading_ws:
            if char in (" ", "\t"):
                return []
            self._skip_leading_ws = False

        # 无工具：全部直通为纯文本
        if not self._has_tools:
            return self._dispatch(char)

        if not self._suspect_buf:
            if char in _MARKER_FIRST_CHARS:
                self._suspect_buf = char
                return []
            return self._dispatch(char)

        # 正在疑似 Marker
        self._suspect_buf += char

        matched = self._exact_match()
        if matched:
            events = self._on_marker(matched)
            self._suspect_buf = ""
            return events

        if self._is_prefix():
            return []  # 继续积累，等待确认

        # 排除歧义：flush suspect_buf 作为普通内容
        buf, self._suspect_buf = self._suspect_buf, ""
        return self._dispatch(buf)

    def _exact_match(self) -> str | None:
        for m in _MARKERS:
            if self._suspect_buf == m:
                return m
        return None

    def _is_prefix(self) -> bool:
        return any(m.startswith(self._suspect_buf) for m in _MARKERS)

    # ── Marker 触发（状态转换）────────────────────────────────────────────────

    def _on_marker(self, marker: str) -> list[str]:
        events: list[str] = []
        events.extend(self._exit_state())

        if marker == "Thought:":
            self._state = _State.IN_THOUGHT
            events.extend(self._enter_thought())

        elif marker == "Action:":
            self._state = _State.IN_ACTION
            self._action_name_buf = ""

        elif marker == "Action Input:":
            # 若 Action 名后没有 \n（罕见），在此兜底触发 function_call_start
            if not self._tool_call_started:
                events.extend(self._start_function_call())
            self._state = _State.IN_ACTION_INPUT

        elif marker == "Observation:":
            self._state = _State.IN_OBSERVATION

        elif marker in ("Final Answer:", "最终答案:"):
            self._state = _State.IN_FINAL
            events.extend(self._enter_final())

        self._skip_leading_ws = True  # 跳过 Marker 冒号后的空白
        return events

    def _exit_state(self) -> list[str]:
        """离开当前状态时的收尾动作。"""
        events: list[str] = []
        if self._state == _State.IN_THOUGHT:
            if self._think_open and not self._think_closed:
                self._think_closed = True
                events.extend(self._make_content("</think>"))
        return events

    # ── 状态进入 ──────────────────────────────────────────────────────────────

    def _enter_thought(self) -> list[str]:
        events: list[str] = []
        if not self._emitted_msg_start:
            events.extend(self._emit_msg_start())
        # 每次进入 IN_THOUGHT 都开一个新的 <think> 块（支持多轮）
        self._think_open = True
        self._think_closed = False
        events.extend(self._make_content("<think>"))
        return events

    def _enter_final(self) -> list[str]:
        events: list[str] = []
        if not self._emitted_msg_start:
            events.extend(self._emit_msg_start())
        return events

    def _start_function_call(self) -> list[str]:
        """Action 名收集完毕，发送 function_call_start。"""
        name = self._action_name_buf.strip()
        self._tool_call_id = f"call_{uuid.uuid4().hex[:8]}"
        self._tool_call_started = True
        events: list[str] = []
        if not self._emitted_msg_start:
            events.extend(self._emit_msg_start())
        events.extend(self._make_tool_call_start(name))
        return events

    # ── 内容分发（根据当前状态路由字符/字符串）──────────────────────────────────

    def _dispatch(self, text: str) -> list[str]:
        """将 text 按当前状态路由到对应的输出动作。"""
        s = self._state
        events: list[str] = []

        if s == _State.IDLE:
            if not self._emitted_msg_start:
                events.extend(self._emit_msg_start())
            events.extend(self._make_content(text))

        elif s == _State.IN_THOUGHT:
            if not self._think_open:
                # 安全兜底：进入 IN_THOUGHT 时通常已调用 _enter_thought，此处防御
                events.extend(self._enter_thought())
            events.extend(self._make_content(text))

        elif s == _State.IN_ACTION:
            # 逐字收集 action 名，遇换行触发 function_call_start
            for ch in text:
                if ch == "\n":
                    if self._action_name_buf.strip() and not self._tool_call_started:
                        events.extend(self._start_function_call())
                else:
                    self._action_name_buf += ch

        elif s == _State.IN_ACTION_INPUT:
            if self._tool_call_started:
                events.extend(self._make_tool_args(text))

        elif s == _State.IN_OBSERVATION:
            # Observation 内容作为普通文本流输出
            if not self._emitted_msg_start:
                events.extend(self._emit_msg_start())
            events.extend(self._make_content(text))

        elif s == _State.IN_FINAL:
            events.extend(self._make_content(text))

        return events

    # ── 流结束 ────────────────────────────────────────────────────────────────

    def _emit_end(self) -> list[str]:
        events: list[str] = []

        # 关闭未关闭的 <think>
        if self._think_open and not self._think_closed:
            self._think_closed = True
            events.extend(self._make_content("</think>"))

        if self._tool_call_started:
            events.extend(self._make_tool_calls_finish())
        elif self._emitted_msg_start:
            events.extend(self._make_stop())
        else:
            # 空响应：补齐最小合法 SSE 序列
            events.extend(self._emit_msg_start())
            events.extend(self._make_stop())

        events.append("data: [DONE]\n\n")
        return events

    # ── SSE chunk 构造 ─────────────────────────────────────────────────────────

    def _emit_msg_start(self) -> list[str]:
        """发送 role:assistant + content:"" 的首帧。"""
        self._emitted_msg_start = True
        return [
            self._sse(
                {
                    "id": self._chat_id,
                    "object": "chat.completion.chunk",
                    "created": self._created,
                    "model": self._model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": ""},
                            "logprobs": None,
                            "finish_reason": None,
                        }
                    ],
                }
            )
        ]

    def _make_content(self, text: str) -> list[str]:
        return [
            self._sse(
                {
                    "id": self._chat_id,
                    "object": "chat.completion.chunk",
                    "created": self._created,
                    "model": self._model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": text},
                            "logprobs": None,
                            "finish_reason": None,
                        }
                    ],
                }
            )
        ]

    def _make_tool_call_start(self, name: str) -> list[str]:
        """发送 function_call_start：携带 id、type、name 和空 arguments。"""
        return [
            self._sse(
                {
                    "id": self._chat_id,
                    "object": "chat.completion.chunk",
                    "created": self._created,
                    "model": self._model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": self._tool_call_index,
                                        "id": self._tool_call_id,
                                        "type": "function",
                                        "function": {"name": name, "arguments": ""},
                                    }
                                ]
                            },
                            "logprobs": None,
                            "finish_reason": None,
                        }
                    ],
                }
            )
        ]

    def _make_tool_args(self, delta: str) -> list[str]:
        """逐字发送 arguments 增量。"""
        return [
            self._sse(
                {
                    "id": self._chat_id,
                    "object": "chat.completion.chunk",
                    "created": self._created,
                    "model": self._model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": self._tool_call_index,
                                        "function": {"arguments": delta},
                                    }
                                ]
                            },
                            "logprobs": None,
                            "finish_reason": None,
                        }
                    ],
                }
            )
        ]

    def _make_tool_calls_finish(self) -> list[str]:
        return [
            self._sse(
                {
                    "id": self._chat_id,
                    "object": "chat.completion.chunk",
                    "created": self._created,
                    "model": self._model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "logprobs": None,
                            "finish_reason": "tool_calls",
                        }
                    ],
                }
            )
        ]

    def _make_stop(self) -> list[str]:
        return [
            self._sse(
                {
                    "id": self._chat_id,
                    "object": "chat.completion.chunk",
                    "created": self._created,
                    "model": self._model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "logprobs": None,
                            "finish_reason": "stop",
                        }
                    ],
                }
            )
        ]

    @staticmethod
    def _sse(obj: dict) -> str:
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"
