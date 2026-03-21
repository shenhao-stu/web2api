"""
ReAct 模块：解析 LLM 纯文本输出（Thought/Action/Action Input），转换为 function_call 格式。
适用于不支持 function calling 的 LLM。提示词借鉴 Dify ReAct 结构与表述，保持行式格式。
"""

import json
import re
from typing import Any

# 复用 function_call 的工具描述格式化
from core.api.function_call import format_tools_for_prompt

# 固定 ReAct 提示词（借鉴 Dify ReAct 结构与表述，保持行式格式以兼容 parse_react_output）
REACT_PROMPT_FIXED = r"""Respond to the human as helpfully and accurately as possible.

You have access to the following tools (listed below under "## Available tools").

Use the following format:

Question: the input question you must answer
Thought: consider what you know and what to do next
Action: the tool name (exactly one of the tools listed below)
Action Input: a single-line JSON object as the tool input
Observation: the result of the action (injected by the system — do NOT output this yourself)
... (repeat Thought / Action / Action Input as needed; after each, the system adds Observation)
Thought: I know the final answer
Final Answer: your final response to the human

Provide only ONE action per response. Valid "Action" values: a tool name from the list, or (when done) output "Final Answer" / "最终答案" instead of Action + Action Input.

Rules:
- After "Action Input: {...}" you must STOP and wait for Observation. Do not add any text, code, or explanation after the JSON line.
- Action Input must be a single-line valid JSON. All double quotes `"` in JSON values must be escaped as `\"`. Do not output "Observation" yourself.
- Format is: Thought → Action → Action Input (or Final Answer when done). Then the system replies with Observation.

Begin. Always respond with a valid Thought then Action then Action Input (or Final Answer). Use tools when necessary; respond with Final Answer when appropriate.
"""


def format_react_prompt(
    tools: list[dict[str, Any]],
    tools_text: str | None = None,
) -> str:
    """用固定 ReAct 提示词构建系统前缀，并拼接可用工具列表。"""
    if tools_text is None:
        tools_text = format_tools_for_prompt(tools)
    return REACT_PROMPT_FIXED + "\n\n---\n\n## Available tools\n\n" + tools_text + "\n"


def parse_react_output(text: str) -> dict[str, Any] | None:
    """
    解析行式 ReAct 输出 (Thought / Action / Action Input)。
    返回 {"type": "final_answer", "content": str} 或
         {"type": "tool_call", "tool": str, "params": dict} 或 None（解析失败）。
    注意：优先解析 Action，若同时存在 Action 与 Final Answer，则返回 tool_call，
    以便正确下发 tool_calls 给客户端执行。
    """
    if not text or not text.strip():
        return None

    # 1. 优先提取 Action + Action Input（若存在则返回 tool_call，避免被 Final Answer 抢先）
    action_match = re.search(r"^\s*Action[:：]\s*(\w+)", text, re.MULTILINE)
    if action_match:
        tool_name = action_match.group(1).strip()

        # 2. 提取 Action Input（单行 JSON 或简单多行）
        input_match = re.search(r"Action Input[:：]\s*(\{[^\n]+\})", text)
        json_str: str | None = None
        if input_match:
            json_str = input_match.group(1).strip()
        else:
            # 多行 JSON：从 Action Input 到下一关键字
            start_m = re.search(r"Action Input[:：]\s*", text)
            if start_m:
                rest = text[start_m.end() :]
                end_m = re.search(
                    r"\n\s*(?:Thought|Action|Observation|Final)", rest, re.I
                )
                raw = rest[: end_m.start()].strip() if end_m else rest.strip()
                if raw.startswith("{") and "}" in raw:
                    depth = 0
                    for i, c in enumerate(raw):
                        if c == "{":
                            depth += 1
                        elif c == "}":
                            depth -= 1
                            if depth == 0:
                                json_str = raw[: i + 1]
                                break

        if not json_str:
            return {
                "type": "tool_call",
                "tool": tool_name,
                "params": {},
                "parse_error": "no_action_input",
            }

        try:
            params = json.loads(json_str)
        except json.JSONDecodeError as e:
            return {
                "type": "tool_call",
                "tool": tool_name,
                "params": {},
                "parse_error": str(e),
            }

        return {"type": "tool_call", "tool": tool_name, "params": params}

    # 3. 无 Action 时，检查 Final Answer
    m = re.search(
        r"(?:Final Answer|最终答案)[:：]\s*(.*)",
        text,
        re.DOTALL | re.I,
    )
    if m:
        content = m.group(1).strip()
        return {"type": "final_answer", "content": content}

    return None


def react_output_to_tool_calls(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    """
    将 parse_react_output 的 tool_call 结果转为 function_call 的 tool_calls_list 格式。
    供 build_tool_calls_response / build_tool_calls_chunk 使用。
    """
    if parsed.get("type") != "tool_call":
        return []
    return [
        {
            "name": parsed.get("tool", ""),
            "arguments": parsed.get("params", {}),
        }
    ]


def format_react_final_answer_content(text: str) -> str:
    """
    若 text 为 ReAct 的 Thought + Final Answer 格式，则将 Thought 用 <think> 包裹，
    便于客户端识别为思考内容；否则返回原文本。
    """
    if not text or not text.strip():
        return text
    # 匹配 Thought: ... 与 Final Answer: / 最终答案: ...
    thought_m = re.search(
        r"Thought[:：]\s*(.+?)(?=\s*(?:Final Answer|最终答案)[:：]|\Z)",
        text,
        re.DOTALL | re.I,
    )
    answer_m = re.search(
        r"(?:Final Answer|最终答案)[:：]\s*(.*)",
        text,
        re.DOTALL | re.I,
    )
    if thought_m and answer_m:
        thought = (thought_m.group(1) or "").strip()
        answer = (answer_m.group(1) or "").strip()
        return f"<think>{thought}</think>\n\n{answer}"
    return text


def extract_thought_so_far(buffer: str) -> tuple[str | None, bool]:
    """
    从流式 buffer 中增量解析当前 Thought 内容（Thought: 到 Action:/Final Answer:/结尾）。
    返回 (thought_content, thought_ended)。
    - thought_content: 当前可确定的 Thought 正文（不含 "Thought:" 前缀），未出现 Thought: 则为 None。
    - thought_ended: 是否已出现 Action: 或 Final Answer:，即 Thought 段已结束。
    """
    content = buffer.lstrip()
    if not content:
        return (None, False)
    # 必须已有 Thought:
    thought_start = re.search(r"Thought[:：]\s*", content, re.I)
    if not thought_start:
        return (None, False)
    start = thought_start.end()
    rest = content[start:]
    # 先找完整结尾：Action: 或 Final Answer:（一出现就截断，不要求后面已有工具名）
    action_m = re.search(r"Action[:：]\s*", rest, re.I)
    final_m = re.search(r"(?:Final Answer|最终答案)[:：]\s*", rest, re.I)
    end_pos: int | None = None
    if action_m and (final_m is None or action_m.start() <= final_m.start()):
        end_pos = action_m.start()
    if final_m and (end_pos is None or final_m.start() < end_pos):
        end_pos = final_m.start()
    if end_pos is not None:
        thought_content = rest[:end_pos].rstrip()
        return (thought_content, True)
    # 未出现完整关键字时，去掉末尾「可能是关键字前缀」的片段，避免把 "\nAc"、"tion:"、"r:"、" [完整回答]" 等当 thought 流式发出
    thought_content = rest.rstrip()
    for kw in ("Action:", "Final Answer:", "最终答案:"):
        for i in range(len(kw), 0, -1):
            if thought_content.lower().endswith(kw[:i].lower()):
                thought_content = thought_content[:-i].rstrip()
                break
    # 再剥 "Final Answer:" 的尾部片段（流式时先收到 "Answer:"、"r:" 等），避免 [完整回答] 被算进 think
    for suffix in (
        " Final Answer:",
        " Final Answer",
        " Answer:",
        " Answer",
        "Answer:",
        "Answer",
        "nswer:",
        "nswer",
        "swer:",
        "swer",
        "wer:",
        "wer",
        "er:",
        "er",
        "r:",
        "r",
    ):
        if thought_content.endswith(suffix):
            thought_content = thought_content[: -len(suffix)].rstrip()
            break
    return (thought_content, False)


def detect_react_mode(buffer: str) -> bool | None:
    """
    判断 buffer 是否为 ReAct 工具调用模式（规范格式：Thought:/Action:/Action Input:）。
    仅当出现该格式时才识别为 ReAct；未按规范返回一律视为纯文本。
    None=尚未确定，True=ReAct 工具调用，False=普通文本或 Final Answer。
    """
    stripped = buffer.lstrip()
    if re.search(r"^\s*Action[:：]\s*\w+", stripped, re.MULTILINE):
        return True
    if re.search(r"(?:Final Answer|最终答案)[:：]", stripped, re.I):
        return False
    # 流式可能只传 Thought/Action 的前半段（如 "Th"、"Tho"），视为尚未确定，继续缓冲
    lower = stripped.lower()
    if lower and ("thought:".startswith(lower) or "action:".startswith(lower)):
        return None
    # 若 buffer 中已出现 Thought:，可能为前导语 + Thought 格式（第二轮常见），保持 None 等待 Action
    if re.search(r"Thought[:：]\s*", stripped, re.I):
        return None
    # 未按规范：首行不是 Thought:/Action: 开头则视为纯文本
    if stripped and not re.match(r"^\s*(?:Thought|Action)[:：]", stripped, re.I):
        return False
    return None
