"""Canonical 请求桥接到 OpenAI 语义事件流（唯一中间态）。"""

from __future__ import annotations

from collections.abc import AsyncIterator

from core.api.chat_handler import ChatHandler
from core.api.schemas import (
    InputAttachment,
    OpenAIChatRequest,
    OpenAIContentPart,
    OpenAIMessage,
)
from core.protocol.images import (
    MAX_IMAGE_COUNT,
    download_remote_image,
    parse_base64_image,
    parse_data_url,
)
from core.hub.schemas import OpenAIStreamEvent
from core.protocol.schemas import CanonicalChatRequest, CanonicalContentBlock, CanonicalMessage


class CanonicalChatService:
    def __init__(self, handler: ChatHandler) -> None:
        self._handler = handler

    async def stream_raw(
        self, req: CanonicalChatRequest
    ) -> AsyncIterator[OpenAIStreamEvent]:
        openai_req = await self._to_openai_request(req)
        async for event in self._handler.stream_openai_events(req.provider, openai_req):
            yield event

    async def collect_raw(self, req: CanonicalChatRequest) -> list[OpenAIStreamEvent]:
        events: list[OpenAIStreamEvent] = []
        async for event in self.stream_raw(req):
            events.append(event)
        return events

    async def _to_openai_request(self, req: CanonicalChatRequest) -> OpenAIChatRequest:
        messages: list[OpenAIMessage] = []
        if req.system:
            messages.append(
                OpenAIMessage(
                    role="system",
                    content=self._to_openai_content(req.system),
                )
            )
        for msg in req.messages:
            messages.append(
                OpenAIMessage(
                    role=msg.role,
                    content=self._to_openai_content(msg.content),
                    tool_call_id=msg.content[0].tool_use_id
                    if msg.role == "tool" and msg.content
                    else None,
                )
            )

        openai_tools = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                    "strict": tool.strict,
                },
            }
            for tool in req.tools
        ]
        last_user_attachments, all_attachments = await self._resolve_attachments(req)
        return OpenAIChatRequest(
            model=req.model,
            messages=messages,
            stream=req.stream,
            tools=openai_tools or None,
            tool_choice=req.tool_choice,
            resume_session_id=req.resume_session_id,
            upstream_model=str(req.metadata.get("upstream_model") or "") or None,
            # 由 ChatHandler 根据是否 full_history 选择实际赋值给 attachment_files
            attachment_files=[],
            attachment_files_last_user=last_user_attachments,
            attachment_files_all_users=all_attachments,
        )

    async def _resolve_attachments(
        self, req: CanonicalChatRequest
    ) -> tuple[list[InputAttachment], list[InputAttachment]]:
        """
        解析图片附件，返回 (last_user_attachments, all_user_attachments)：

        - 复用会话（full_history=False）时，仅需最后一条 user 的图片；
        - 重建会话（full_history=True）时，需要把所有历史 user 的图片一并补上。
        """
        last_user: CanonicalMessage | None = None
        for msg in reversed(req.messages):
            if msg.role == "user":
                last_user = msg
                break

        # 所有 user 消息里的图片（用于重建会话补历史）
        all_image_blocks: list[CanonicalContentBlock] = []
        for msg in req.messages:
            if msg.role != "user":
                continue
            all_image_blocks.extend(
                block for block in msg.content if block.type == "image"
            )

        last_user_blocks: list[CanonicalContentBlock] = []
        if last_user is not None:
            last_user_blocks = [
                block for block in last_user.content if block.type == "image"
            ]

        if len(all_image_blocks) > MAX_IMAGE_COUNT:
            raise ValueError(f"单次最多上传 {MAX_IMAGE_COUNT} 张图片")

        async def _prepare(
            blocks: list[CanonicalContentBlock],
        ) -> list[InputAttachment]:
            attachments: list[InputAttachment] = []
            for idx, block in enumerate(blocks, start=1):
                if block.url:
                    prepared = await download_remote_image(
                        block.url, prefix=f"message_image_{idx}"
                    )
                elif block.data and block.data.startswith("data:"):
                    prepared = parse_data_url(block.data, prefix=f"message_image_{idx}")
                elif block.data and block.mime_type:
                    prepared = parse_base64_image(
                        block.data,
                        block.mime_type,
                        prefix=f"message_image_{idx}",
                    )
                else:
                    raise ValueError("图片块缺少可用数据")
                attachments.append(
                    InputAttachment(
                        filename=prepared.filename,
                        mime_type=prepared.mime_type,
                        data=prepared.data,
                    )
                )
            return attachments

        last_attachments = await _prepare(last_user_blocks)
        all_attachments = await _prepare(all_image_blocks)
        return last_attachments, all_attachments

    @staticmethod
    def _to_openai_content(
        blocks: list[CanonicalContentBlock],
    ) -> str | list[OpenAIContentPart]:
        if not blocks:
            return ""
        parts: list[OpenAIContentPart] = []
        for block in blocks:
            if block.type in {"text", "thinking", "tool_result"}:
                parts.append(OpenAIContentPart(type="text", text=block.text or ""))
            elif block.type == "image":
                url = block.url or block.data or ""
                parts.append(
                    OpenAIContentPart(
                        type="image_url",
                        image_url={"url": url},
                    )
                )
        if not parts:
            return ""
        if len(parts) == 1 and parts[0].type == "text":
            return parts[0].text or ""
        return parts
