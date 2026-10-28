"""Shared OpenAI-compatible streaming chat adapter."""
from __future__ import annotations

import base64
import json
import math
import random
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterator

import requests

from ..catalog import ModelInfo
from ..config import Settings
from ..errors import ProviderError

MAX_ATTEMPTS = 5
RETRY_BUDGET_SECONDS = 90.0
MAX_RETRY_AFTER = 60.0
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524, 529}


class CancelToken:
    """Thread-safe cancellation flag that can close an in-flight HTTP response."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._callbacks: list[Callable[[], None]] = []

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        with self._lock:
            self._event.set()
            callbacks, self._callbacks = self._callbacks, []
        for callback in callbacks:
            try:
                callback()
            except Exception:  # noqa: BLE001 - closing is best effort
                pass

    def on_cancel(self, callback: Callable[[], None]) -> None:
        with self._lock:
            if not self._event.is_set():
                self._callbacks.append(callback)
                return
        callback()

    def wait(self, seconds: float) -> bool:
        return self._event.wait(seconds)


@dataclass
class StreamEvent:
    kind: str  # text | reasoning | tool_call | usage | finish
    data: Any = None


@dataclass
class _ToolAccumulator:
    calls: dict[int, dict[str, str]] = field(default_factory=dict)

    def feed(self, fragments: list[dict]) -> None:
        for frag in fragments:
            slot = self.calls.setdefault(frag.get("index", len(self.calls)), {"id": "", "name": "", "arguments": ""})
            if frag.get("id"):
                slot["id"] = frag["id"]
            fn = frag.get("function") or {}
            if fn.get("name"):
                slot["name"] += fn["name"]
            if fn.get("arguments"):
                slot["arguments"] += fn["arguments"]

    def finish(self) -> list[dict[str, str]]:
        return [self.calls[i] for i in sorted(self.calls)]


def _decimal(value: Any) -> Decimal | None:
    """A finite, non-negative Decimal from a JSON number/string, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() and number >= 0 else None


def _text_of(content: list[dict]) -> str:
    return "\n".join(c["text"] for c in content if c["type"] == "text")


class OpenAICompatProvider:
    name = "base"

    def __init__(self, settings: Settings):
        self.settings = settings

    # -- subclass hooks -------------------------------------------------
    def endpoint(self) -> str:
        raise NotImplementedError

    def headers(self) -> dict[str, str]:
        raise NotImplementedError

    def extra_body(self, model: ModelInfo, options: dict) -> dict:
        return {}

    def configured(self) -> bool:
        raise NotImplementedError

    def extract_cost(self, usage: dict, model: ModelInfo) -> Decimal | None:
        """USD cost of one request: the provider-reported ``usage.cost`` if present,
        otherwise computed from token counts and the catalog's optional prices."""
        reported = _decimal(usage.get("cost"))
        if reported is not None:
            return reported
        price_in, price_out = _decimal(model.price_input_per_mtok), _decimal(model.price_output_per_mtok)
        prompt, completion = _decimal(usage.get("prompt_tokens")), _decimal(usage.get("completion_tokens"))
        if None in (price_in, price_out, prompt, completion):
            return None
        return (prompt * price_in + completion * price_out) / Decimal(1_000_000)

    # -- conversion -----------------------------------------------------
    def convert_messages(self, history: list[dict], load_image: Callable[[str], tuple[str, bytes]]) -> list[dict]:
        """Convert stored history into OpenAI format.

        Neither supported provider accepts images in tool results, so images from
        a tool batch are delivered in one synthetic user message after the batch.
        That message exists only in the upstream payload, never in history.
        """

        def image_part(hash_: str) -> dict:
            mime, data = load_image(hash_)
            return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode()}"}}

        out: list[dict] = []
        batch_images: list[str] = []

        def flush_images() -> None:
            if batch_images:
                parts: list[dict] = [{"type": "text", "text": "Image(s) returned by the tool call(s) above:"}]
                parts += [image_part(h) for h in batch_images]
                out.append({"role": "user", "content": parts})
                batch_images.clear()

        for msg in history:
            role = msg["role"]
            if role != "tool":
                flush_images()
            if role == "system":
                out.append({"role": "system", "content": _text_of(msg["content"])})
            elif role == "user":
                parts = [{"type": "text", "text": c["text"]} if c["type"] == "text" else image_part(c["hash"]) for c in msg["content"]]
                out.append({"role": "user", "content": parts})
            elif role == "assistant":
                text = msg["text"]["text"]
                calls = msg.get("tool_calls") or []
                if not text and not calls:
                    continue
                item: dict[str, Any] = {"role": "assistant", "content": text or None}
                if calls:
                    item["tool_calls"] = [
                        {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
                        for c in calls
                    ]
                out.append(item)
            elif role == "tool":
                images = [c["hash"] for c in msg["content"] if c["type"] == "image"]
                text = _text_of(msg["content"])
                if images:
                    text = (text + "\n" if text else "") + f"[{len(images)} image(s) attached in the following user message]"
                    batch_images.extend(images)
                out.append({"role": "tool", "tool_call_id": msg["tool_call_id"], "content": text})
        flush_images()
        return out

    def build_body(self, model: ModelInfo, messages: list[dict], tools: list[dict], options: dict) -> dict:
        body: dict[str, Any] = {"model": model.provider_tech_name, "messages": messages, "stream": True}
        if tools:
            body["tools"] = tools
            body["parallel_tool_calls"] = True
        if options.get("temperature") is not None:
            body["temperature"] = options["temperature"]
        if options.get("max_tokens") is not None:
            body["max_tokens"] = options["max_tokens"]
        body.update(self.extra_body(model, options))
        return body

    # -- transport ------------------------------------------------------
    def classify_http_error(self, response: requests.Response) -> ProviderError:
        status = response.status_code
        try:
            payload = response.json()
            err = payload.get("error", payload) if isinstance(payload, dict) else {}
            message = err.get("message") if isinstance(err, dict) else str(err)
        except ValueError:
            message = response.text[:500]
        message = (message or f"HTTP {status}")[:1000]
        retry_after = None
        header = response.headers.get("Retry-After")
        if header:
            try:
                retry_after = max(0.0, float(header))
            except ValueError:
                retry_after = None
        retryable = status in RETRYABLE_STATUS and not self.never_retry(status)
        code = "rate_limited" if status == 429 else "context_length_exceeded" if status in (400, 413) and "context" in message.lower() else f"http_{status}"
        return ProviderError(code, message, retryable=retryable, retry_after=retry_after)

    def never_retry(self, status: int) -> bool:
        return False

    def _open(self, body: dict, cancel: CancelToken) -> requests.Response:
        started = time.monotonic()
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if cancel.cancelled:
                raise _Cancelled()
            error: ProviderError
            try:
                response = requests.post(
                    self.endpoint(), headers=self.headers(), json=body, stream=True,
                    timeout=(self.settings.provider_connect_timeout, self.settings.provider_read_timeout),
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                if cancel.cancelled:
                    raise _Cancelled() from exc
                error = ProviderError("network_error", f"{type(exc).__name__}: {exc}"[:500], retryable=True)
            except requests.RequestException as exc:
                raise ProviderError("request_error", str(exc)[:500]) from exc
            else:
                if response.status_code < 400:
                    return response
                error = self.classify_http_error(response)
                response.close()
            delay = min(2 ** (attempt - 1), 16) * random.uniform(0.75, 1.25)
            if error.retry_after is not None:
                if error.retry_after > MAX_RETRY_AFTER:
                    raise error
                delay = error.retry_after
            if not error.retryable or attempt == MAX_ATTEMPTS or time.monotonic() - started + delay > RETRY_BUDGET_SECONDS:
                raise error
            if cancel.wait(delay):
                raise _Cancelled()
        raise AssertionError("unreachable")

    def stream(self, model: ModelInfo, history: list[dict], tools: list[dict], options: dict,
               load_image: Callable[[str], tuple[str, bytes]], cancel: CancelToken) -> Iterator[StreamEvent]:
        """Yield events. Retries are only attempted before the first event is yielded.

        Raises ``Cancelled`` (see ``is_cancelled``) or ProviderError.
        """
        body = self.build_body(model, self.convert_messages(history, load_image), tools, options)
        attempt = 0
        started = time.monotonic()
        while True:
            attempt += 1
            yielded = False
            try:
                response = self._open(body, cancel)
                cancel.on_cancel(response.close)
                try:
                    for event in self._parse(response, cancel):
                        yielded = True
                        yield event
                finally:
                    response.close()
                return
            except ProviderError as exc:
                if yielded or cancel.cancelled or not exc.retryable or attempt >= MAX_ATTEMPTS \
                        or time.monotonic() - started > RETRY_BUDGET_SECONDS:
                    raise
                if cancel.wait(min(2 ** (attempt - 1), 16) * random.uniform(0.75, 1.25)):
                    raise _Cancelled() from exc

    def _parse(self, response: requests.Response, cancel: CancelToken) -> Iterator[StreamEvent]:
        tools = _ToolAccumulator()
        finish_reason = None
        done = False
        try:
            # requests.iter_lines() uses a fixed read size, holding back tiny
            # SSE chunks on non-chunked responses. urllib3.read1 returns what is
            # currently available instead; no need to wait for a full buffer.
            buffer = b""
            def lines():
                nonlocal buffer
                while True:
                    data = response.raw.read1(65536)
                    if not data:
                        if buffer:
                            yield buffer.rstrip(b"\r")
                        return
                    buffer += data
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        yield line.rstrip(b"\r")

            for raw in lines():
                if cancel.cancelled:
                    raise _Cancelled()
                if not raw or not raw.startswith(b"data:"):
                    continue  # blank line, SSE comment (keep-alive) or other field
                data = raw[5:].strip()
                if data == b"[DONE]":
                    done = True
                    break
                try:
                    chunk = json.loads(data)
                except ValueError as exc:
                    raise ProviderError("bad_stream", "Provider sent invalid JSON in stream", retryable=True) from exc
                if chunk.get("error"):
                    err = chunk["error"]
                    message = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                    code = err.get("code") if isinstance(err, dict) else None
                    raise ProviderError("provider_error", str(message)[:1000],
                                        retryable=isinstance(code, int) and code in RETRYABLE_STATUS)
                if isinstance(chunk.get("usage"), dict):
                    # A final usage-only chunk often has no choices. Yield it now
                    # so cancellation/errors after it do not lose a reported cost.
                    yield StreamEvent("usage", chunk["usage"])
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        yield StreamEvent("text", delta["content"])
                    reasoning = delta.get("reasoning") or delta.get("reasoning_content")
                    if reasoning:
                        yield StreamEvent("reasoning", reasoning)
                    if delta.get("tool_calls"):
                        tools.feed(delta["tool_calls"])
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
        except (requests.RequestException, OSError, ValueError) as exc:
            if cancel.cancelled:
                raise _Cancelled() from exc
            raise ProviderError("stream_interrupted", f"{type(exc).__name__}: {exc}"[:500], retryable=True) from exc
        if cancel.cancelled:
            raise _Cancelled()
        if not done and finish_reason is None:
            raise ProviderError("stream_interrupted", "Stream ended before completion", retryable=True)
        for call in tools.finish():
            yield StreamEvent("tool_call", call)
        yield StreamEvent("finish", finish_reason or "stop")


class _Cancelled(Exception):
    pass


Cancelled = _Cancelled
