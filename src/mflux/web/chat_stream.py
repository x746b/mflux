from collections.abc import AsyncIterator

import httpx


class BoundedEventStream:
    CHUNK_BYTES = 4096
    MAX_LINE_BYTES = 256 * 1024
    MAX_EVENT_BYTES = 256 * 1024
    MAX_RESPONSE_BYTES = 2 * 1024 * 1024

    @classmethod
    async def events(cls, response: httpx.Response) -> AsyncIterator[str]:
        if response.headers.get("content-encoding", "").strip().lower() not in ("", "identity"):
            raise ValueError("Compressed chat streams are not supported")
        data = []
        size = 0
        async for line in cls._lines(response):
            size += len(line.encode("utf-8")) + 1
            if size > cls.MAX_EVENT_BYTES:
                raise ValueError("Chat event exceeds size limit")
            if line.startswith("data:"):
                value = line[5:]
                data.append(value[1:] if value.startswith(" ") else value)
            elif not line:
                if data:
                    yield "\n".join(data)
                data, size = [], 0

    @classmethod
    async def _lines(cls, response: httpx.Response) -> AsyncIterator[str]:
        buffer = bytearray()
        total = 0
        skip_lf = False
        first = True
        # Do not ask HTTPX to coalesce chunks: short token events must arrive immediately.
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > cls.MAX_RESPONSE_BYTES:
                raise ValueError("Chat response exceeds size limit")
            for start in range(0, len(chunk), cls.CHUNK_BYTES):
                piece = chunk[start:start + cls.CHUNK_BYTES]
                if skip_lf:
                    if piece.startswith(b"\n"):
                        piece = piece[1:]
                    skip_lf = False
                buffer.extend(piece)
                while buffer:
                    cr, lf = buffer.find(b"\r"), buffer.find(b"\n")
                    positions = [position for position in (cr, lf) if position >= 0]
                    if not positions:
                        if len(buffer) > cls.MAX_LINE_BYTES:
                            raise ValueError("Chat line exceeds size limit")
                        break
                    end = min(positions)
                    if end > cls.MAX_LINE_BYTES:
                        raise ValueError("Chat line exceeds size limit")
                    line = bytes(buffer[:end])
                    is_cr = buffer[end] == 13
                    del buffer[:end + 1]
                    if is_cr:
                        if buffer.startswith(b"\n"):
                            del buffer[:1]
                        elif not buffer:
                            skip_lf = True
                    yield line.decode("utf-8-sig" if first else "utf-8")
                    first = False
        if buffer:
            yield bytes(buffer).decode("utf-8-sig" if first else "utf-8")
