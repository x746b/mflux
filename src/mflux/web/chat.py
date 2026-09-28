import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterator, Callable
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import HTTPException

from mflux.web.chat_stream import BoundedEventStream
from mflux.web.network import NetworkPolicy


class PromptChat:
    MODELS = {"gpt-6-luna": "GPT-6 Luna", "gpt-6-sol": "GPT-6 Sol"}
    DEFAULT_MODEL = "gpt-6-luna"
    MAX_MESSAGES = 24
    MAX_TEXT = 12000
    MAX_TOTAL = 48000
    MAX_INSTRUCTIONS = 4000
    RESPONSE_DEADLINES = {"openai": 120, "omlx": 300}
    MAX_BUFFERED_EVENTS = 8
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
        local_model = os.environ.get("OMLX_MFLUX_MODEL", "").strip()
        local_error = None
        try:
            PromptChat.endpoint("omlx")
        except HTTPException as exc:
            local_error = exc.detail
        if not local_model:
            local_error = "Set OMLX_MFLUX_MODEL to the model name shown in oMLX."
        elif len(local_model) > 256 or not local_model.isprintable():
            local_error = "OMLX_MFLUX_MODEL must be a printable model name of at most 256 characters."
            local_model = ""
        openai = {
            "name": "OpenAI",
            "configured": bool(os.environ.get("OPENAI_API_KEY", "").strip()),
            "default_model": PromptChat.DEFAULT_MODEL,
            "models": [{"id": key, "name": value} for key, value in PromptChat.MODELS.items()],
        }
        omlx = {
            "name": "oMLX",
            "configured": bool(os.environ.get("OMLX_API_KEY", "").strip()) and not local_error,
            "default_model": local_model,
            "models": [{"id": local_model, "name": local_model}] if local_model else [],
            "configuration_error": local_error,
        }
        return {
            **openai,
            "providers": {"openai": openai, "omlx": omlx},
            "max_messages": PromptChat.MAX_MESSAGES,
            "max_text": PromptChat.MAX_TEXT,
            "max_total": PromptChat.MAX_TOTAL,
            "max_instructions": PromptChat.MAX_INSTRUCTIONS,
        }

    @staticmethod
    def provider(body: dict) -> str:
        provider = body.get("provider", "openai")
        if provider not in ("openai", "omlx"):
            raise HTTPException(400, "Choose OpenAI or oMLX.")
        return provider

    @staticmethod
    def endpoint(provider: str) -> str:
        if provider == "openai":
            return "https://api.openai.com/v1/responses"
        try:
            base = os.environ.get("OMLX_BASE_URL", "http://127.0.0.1:8000/v1").strip()
            parsed = urlsplit(base)
            if (
                parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or (parsed.port is not None and not 1 <= parsed.port <= 65535)
            ):
                raise ValueError
            netloc = parsed.netloc
            if parsed.scheme == "http":
                if not NetworkPolicy.is_loopback_host(parsed.hostname):
                    raise HTTPException(503, "OMLX_BASE_URL must use HTTPS for servers outside this machine.")
                if parsed.hostname.lower().rstrip(".") == "localhost":
                    netloc = "127.0.0.1" + (f":{parsed.port}" if parsed.port is not None else "")
            path = parsed.path.rstrip("/") or "/v1"
            return urlunsplit((parsed.scheme, netloc, path + "/chat/completions", "", ""))
        except ValueError as exc:
            raise HTTPException(503, "OMLX_BASE_URL must be an HTTP(S) API base URL without credentials, query, or fragment.") from exc

    @staticmethod
    def validate(body: dict) -> dict:
        provider = PromptChat.provider(body)
        if provider == "omlx":
            configuration = PromptChat.status()["providers"]["omlx"]
            if configuration["configuration_error"]:
                raise HTTPException(503, configuration["configuration_error"])
            model = body.get("model", configuration["default_model"])
            if model != configuration["default_model"]:
                raise HTTPException(400, "Choose the model configured by OMLX_MFLUX_MODEL.")
        else:
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
        instructions = PromptChat.INSTRUCTIONS + ("\nUser style preferences:\n" + instructions if instructions else "")
        if provider == "omlx":
            return {
                "model": model,
                "messages": [{"role": "system", "content": instructions}, *cleaned],
                "max_tokens": 4096,
                "stream": True,
                "stream_options": {"include_usage": True},
                "chat_template_kwargs": {"enable_thinking": False},
            }
        return {
            "model": model,
            "instructions": instructions,
            "input": cleaned,
            "reasoning": {"effort": "low"},
            "max_output_tokens": 4096,
            "stream": True,
            "store": False,
        }

    async def stream(self, payload: dict, key: str, provider: str = "openai") -> AsyncIterator[str]:
        if self.lock.locked():
            yield self._event("error", message="Another chat reply is running. Please try again shortly.")
            return
        await self.lock.acquire()
        queue = asyncio.Queue(maxsize=self.MAX_BUFFERED_EVENTS)
        released = False

        def release_slot() -> None:
            nonlocal released
            if not released:
                released = True
                self.lock.release()

        producer = asyncio.create_task(self._produce(payload, key, provider, queue, release_slot))
        producer.add_done_callback(lambda task: release_slot())
        try:
            while True:
                if not queue.empty():
                    yield queue.get_nowait()
                    continue
                if producer.done():
                    producer.result()
                    break
                pending = asyncio.create_task(queue.get())
                try:
                    await asyncio.wait((pending, producer), return_when=asyncio.FIRST_COMPLETED)
                    if pending.done():
                        yield pending.result()
                finally:
                    pending.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await pending
        finally:
            producer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await producer
            release_slot()

    async def _produce(
        self, payload: dict, key: str, provider: str, queue: asyncio.Queue, release_slot: Callable[[], None]
    ) -> None:
        error = None
        try:
            await asyncio.wait_for(
                self._forward(payload, key, provider, queue), timeout=self.RESPONSE_DEADLINES[provider]
            )
        except asyncio.TimeoutError:
            error = self._event("error", message="The chat reply exceeded its time limit. Please try a shorter request.")
        except Exception:  # noqa: BLE001 -- never expose upstream diagnostics or leave a consumer waiting
            error = self._event("error", message="The chat response could not be completed. Please try again.")
        finally:
            release_slot()
        if error:
            # Discard queued partial output on failure; never wait on a stalled consumer.
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait(error)

    async def _forward(self, payload: dict, key: str, provider: str, queue: asyncio.Queue) -> None:
        async with contextlib.aclosing(self._stream_response(payload, key, provider)) as events:
            async for event in events:
                await queue.put(event)

    async def _stream_response(self, payload: dict, key: str, provider: str) -> AsyncIterator[str]:
        name = "oMLX" if provider == "omlx" else "OpenAI"
        try:
            timeout = 180 if provider == "omlx" else 60
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout, connect=10), follow_redirects=False, trust_env=provider == "openai",
            ) as client:
                async with client.stream(
                    "POST", self.endpoint(provider),
                    headers={"Authorization": f"Bearer {key}", "Accept-Encoding": "identity"}, json=payload,
                ) as response:
                    if response.status_code != 200:
                        yield self._event("error", message=self._http_error(response.status_code, provider))
                        return
                    output_size = 0
                    local_finished = False
                    local_usage = {"input_tokens": None, "output_tokens": None}
                    async for raw_event in BoundedEventStream.events(response):
                        if provider == "omlx" and raw_event == "[DONE]":
                            if local_finished:
                                yield self._event("done", usage=local_usage)
                            else:
                                yield self._event("error", message="oMLX ended without a complete reply.")
                            return
                        event = json.loads(raw_event)
                        if not isinstance(event, dict):
                            raise ValueError("Invalid event")
                        if provider == "omlx":
                            if "error" in event:
                                yield self._event("error", message="oMLX could not complete this reply. Check the model in oMLX.")
                                return
                            usage = event.get("usage")
                            if isinstance(usage, dict):
                                local_usage = {"input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens")}
                            choices = event.get("choices", [])
                            if not isinstance(choices, list):
                                raise ValueError("Invalid choices")
                            if choices:
                                choice = choices[0]
                                if not isinstance(choice, dict) or not isinstance(choice.get("delta", {}), dict):
                                    raise ValueError("Invalid choice")
                                delta = choice.get("delta", {}).get("content") or ""
                                if not isinstance(delta, str):
                                    raise ValueError("Invalid text")
                                output_size += len(delta)
                                if output_size > self.MAX_TEXT:
                                    yield self._event("error", message="The reply is too long. Ask for a shorter answer.")
                                    return
                                if delta:
                                    yield self._event("delta", text=delta)
                                finish = choice.get("finish_reason")
                                if finish is not None:
                                    if finish != "stop":
                                        yield self._event("error", message="oMLX stopped before completing the reply. Ask for a shorter answer.")
                                        return
                                    local_finished = True
                            continue
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
            yield self._event("error", message=f"{name} took too long to respond. Please try again.")
        except (httpx.HTTPError, ValueError, UnicodeError):
            yield self._event("error", message=f"Could not read the {name} response. Check the connection and try again.")

    @staticmethod
    def _event(kind: str, **data) -> str:
        return json.dumps({"type": kind, **data}) + "\n"

    @staticmethod
    def _http_error(status: int, provider: str = "openai") -> str:
        if provider == "omlx":
            return {
                401: "oMLX rejected the API key. Update OMLX_API_KEY and restart mflux-web.",
                403: "oMLX denied access. Check its API key and server settings.",
                404: "The oMLX model or API endpoint was not found. Check OMLX_MFLUX_MODEL and OMLX_BASE_URL.",
                429: "oMLX is busy. Please try again shortly.",
            }.get(status, "oMLX could not handle the request. Check the model and server status.")
        return {
            401: "OpenAI rejected the API key. Update OPENAI_API_KEY and restart mflux-web.",
            403: "This API project does not have access to the selected model.",
            404: "The selected model is unavailable for this API project. Try the other model.",
            429: "OpenAI rate or quota limit reached. Check your API billing or try again later.",
        }.get(status, "OpenAI is unavailable or rejected the request. Please try again later.")
