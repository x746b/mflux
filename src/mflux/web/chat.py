import asyncio
import json
import os
from collections.abc import AsyncIterator

import httpx
from fastapi import HTTPException


class PromptChat:
    MODELS = {"gpt-6-luna": "GPT-6 Luna", "gpt-6-sol": "GPT-6 Sol"}
    DEFAULT_MODEL = "gpt-6-luna"
    MAX_MESSAGES = 24
    MAX_TEXT = 12000
    MAX_TOTAL = 48000
    MAX_INSTRUCTIONS = 4000
    INSTRUCTIONS = (
        "You are an image-prompt collaborator for mflux. Discuss ideas naturally and concisely. "
        "Help with composition, lighting, mood and style while preserving the user's intent. "
        "Do not invent image-model features or claim you have generated or seen an image. "
        "When proposing a ready-to-use prompt, put only the prompt text in a fenced code block "
        "labelled prompt, like ```prompt followed by a newline, the prompt, a newline and ```. "
        "Put each variation in its own prompt block. Keep explanations outside those blocks. "
        "For a request to shorten or expand, return one revised prompt unless asked otherwise."
    )

    def __init__(self) -> None:
        self.lock = asyncio.Lock()

    @staticmethod
    def status() -> dict:
        return {
            "configured": bool(os.environ.get("OPENAI_API_KEY", "").strip()),
            "default_model": PromptChat.DEFAULT_MODEL,
            "models": [{"id": key, "name": value} for key, value in PromptChat.MODELS.items()],
            "max_messages": PromptChat.MAX_MESSAGES,
            "max_text": PromptChat.MAX_TEXT,
            "max_total": PromptChat.MAX_TOTAL,
            "max_instructions": PromptChat.MAX_INSTRUCTIONS,
        }

    @staticmethod
    def validate(body: dict) -> dict:
        model = body.get("model", PromptChat.DEFAULT_MODEL)
        if not isinstance(model, str) or model not in PromptChat.MODELS:
            raise HTTPException(400, "Choose GPT-6 Luna or GPT-6 Sol.")
        instructions = body.get("instructions", "")
        if not isinstance(instructions, str) or len(instructions) > PromptChat.MAX_INSTRUCTIONS:
            raise HTTPException(400, "Assistant instructions must be at most 4,000 characters.")
        messages = body.get("messages")
        if not isinstance(messages, list) or not 1 <= len(messages) <= PromptChat.MAX_MESSAGES:
            raise HTTPException(400, "Chat supports up to 24 messages. Clear chat to start a new conversation.")
        cleaned = []
        total = len(instructions)
        for index, message in enumerate(messages):
            role = "user" if index % 2 == 0 else "assistant"
            if not isinstance(message, dict) or message.get("role") != role:
                raise HTTPException(400, "Chat messages must alternate between user and assistant.")
            content = message.get("content")
            if not isinstance(content, str) or not content.strip() or len(content) > PromptChat.MAX_TEXT:
                raise HTTPException(400, "Each chat message must contain 1–12,000 characters.")
            total += len(content)
            cleaned.append({"role": role, "content": content})
        if cleaned[-1]["role"] != "user" or total > PromptChat.MAX_TOTAL:
            raise HTTPException(400, "Send a user message and keep the conversation under 48,000 characters.")
        return {
            "model": model,
            "instructions": PromptChat.INSTRUCTIONS + ("\nUser style preferences:\n" + instructions if instructions else ""),
            "input": cleaned,
            "reasoning": {"effort": "low"},
            "max_output_tokens": 4096,
            "stream": True,
            "store": False,
        }

    async def stream(self, payload: dict, key: str) -> AsyncIterator[str]:
        if self.lock.locked():
            yield self._event("error", message="Another chat reply is running. Please try again shortly.")
            return
        async with self.lock:
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10), follow_redirects=False) as client:
                    async with client.stream(
                        "POST", "https://api.openai.com/v1/responses",
                        headers={"Authorization": f"Bearer {key}"}, json=payload,
                    ) as response:
                        if response.status_code != 200:
                            yield self._event("error", message=self._http_error(response.status_code))
                            return
                        data = []
                        size = 0
                        output_size = 0
                        async for line in response.aiter_lines():
                            if line.startswith("data:"):
                                data.append(line[5:].lstrip())
                                size += len(line)
                                if size > 262144:
                                    raise ValueError("Oversized event")
                            elif not line and data:
                                event = json.loads("\n".join(data))
                                data, size = [], 0
                                if not isinstance(event, dict):
                                    raise ValueError("Invalid event")
                                kind = event.get("type")
                                if kind in ("response.output_text.delta", "response.refusal.delta"):
                                    delta = event.get("delta", "")
                                    if not isinstance(delta, str):
                                        raise ValueError("Invalid text")
                                    output_size += len(delta)
                                    if output_size > self.MAX_TEXT:
                                        yield self._event("error", message="The reply is too long. Ask for a shorter answer.")
                                        return
                                    yield self._event("delta", text=delta)
                                elif kind == "response.completed":
                                    result = event.get("response")
                                    if not isinstance(result, dict):
                                        raise ValueError("Invalid response")
                                    usage = result.get("usage") or {}
                                    if not isinstance(usage, dict):
                                        raise ValueError("Invalid usage")
                                    yield self._event("done", usage={
                                        key: usage.get(key, 0) for key in ("input_tokens", "output_tokens")
                                    })
                                    return
                                elif kind in ("response.failed", "response.incomplete", "error"):
                                    yield self._event("error", message="OpenAI could not complete this reply. Try a shorter request or another model.")
                                    return
                        yield self._event("error", message="The reply ended unexpectedly. Please try again.")
            except httpx.TimeoutException:
                yield self._event("error", message="OpenAI took too long to respond. Please try again.")
            except (httpx.HTTPError, ValueError, UnicodeError):
                yield self._event("error", message="Could not read the OpenAI response. Check the connection and try again.")

    @staticmethod
    def _event(kind: str, **data) -> str:
        return json.dumps({"type": kind, **data}) + "\n"

    @staticmethod
    def _http_error(status: int) -> str:
        return {
            401: "OpenAI rejected the API key. Update OPENAI_API_KEY and restart mflux-web.",
            403: "This API project does not have access to the selected model.",
            404: "The selected model is unavailable for this API project. Try the other model.",
            429: "OpenAI rate or quota limit reached. Check your API billing or try again later.",
        }.get(status, "OpenAI is unavailable or rejected the request. Please try again later.")
