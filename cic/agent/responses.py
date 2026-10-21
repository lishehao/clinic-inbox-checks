"""Responses API client for ChatGPT plan usage (Sign in with ChatGPT).

Follows the plan-usage rules in OpenAI's documentation: POST /v1/responses with
"store": false and "stream": true, the system prompt sent as "instructions" (system
role items are rejected), no temperature, top_p or output-length fields, and the
reply read from the event stream until response.completed. A plan-usage error
stops the batch instead of being retried, and nothing switches to another billing
path. Tokens come from cic/agent/siwc.py and are sent only in the Authorization header.
"""

import http.client
import json
import os
import time
import urllib.error
import urllib.request

from cic.agent.llm import LLMError
from cic.agent.siwc import API_BASE, SignInError

STOP_EXPLANATIONS = {
    "subscription_sharing_usage_limit_exceeded":
        "The ChatGPT plan's usage limit for this app (or the plan's current window) is reached. "
        "See ChatGPT Settings > Usage. Continue later with --resume.",
    "subscription_sharing_user_not_eligible":
        "ChatGPT plan usage is not available for this account or workspace (it needs Plus or Pro).",
    "subscription_sharing_unsupported_capability":
        "This route does not support part of the request.",
    "subscription_sharing_route_not_supported":
        "This endpoint is not allowed for plan usage.",
    "subscription_sharing_invalid_user":
        "The signed-in account could not be validated.",
    "chatpass_v2_scope_not_authorized":
        "The sign-in does not carry the needed permission.",
    "chatpass_v2_invalid_authorization_context":
        "The client or grant configuration was not accepted.",
}
TRANSIENT_CODES = {"subscription_sharing_usage_unavailable", "subscription_sharing_user_unavailable"}


class StopBatch(LLMError):
    """An error after which no more requests should be sent in this batch."""


class _Transient(LLMError):
    pass


def _error_info(text):
    try:
        body = json.loads(text)
    except ValueError:
        return None, text[:300], None
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return error.get("code"), str(error.get("message") or "")[:300], error.get("param")
    if isinstance(body, dict) and body.get("detail"):
        return None, str(body["detail"])[:300], None
    return None, text[:300], None


def _explain(status, code, message, param=None, request_id=None):
    """Code and request id first, so they survive any later shortening of the message."""
    head = f"HTTP {status}" if status else "during the stream"
    parts = [head + (f" {code}" if code else "")]
    if request_id:
        parts.append(f"request id {request_id}")
    if param:
        parts.append(f"param: {param}")
    if STOP_EXPLANATIONS.get(code or ""):
        parts.append(STOP_EXPLANATIONS[code])
    if message:
        parts.append(f"server said: {message[:200]}")
    return " | ".join(parts)


def _output_text(response):
    chunks = []
    for item in response.get("output") or []:
        for part in item.get("content") or []:
            if part.get("type") in ("output_text", "text"):
                chunks.append(part.get("text", ""))
    return "".join(chunks)


def _stream_failure(error, request_id, status=None):
    code = error.get("code")
    message = str(error.get("message") or "")
    param = error.get("param")
    text = _explain(status, code, message, param, request_id)
    fields = {"status": status, "code": code, "request_id": request_id, "param": param}
    if code in TRANSIENT_CODES:
        return _Transient(text, **fields)
    if code in STOP_EXPLANATIONS or str(code or "").startswith(("subscription_sharing", "chatpass")):
        return StopBatch(text, **fields)
    return LLMError(text, **fields)


def read_stream(lines, request_id=None):
    """Read server-sent events until response.completed. Returns (text, usage)."""
    text_parts, data_lines = [], []

    def handle(data):
        if data.strip() == "[DONE]":
            return None
        try:
            event = json.loads(data)
        except ValueError:
            raise _Transient("An event in the stream could not be read.", request_id=request_id) from None
        kind = event.get("type")
        if kind == "response.output_text.delta":
            text_parts.append(event.get("delta", ""))
        elif kind == "response.completed":
            response = event.get("response") or {}
            usage = response.get("usage") or {}
            text = "".join(text_parts) or _output_text(response)
            return text, {"prompt_tokens": int(usage.get("input_tokens", 0) or 0),
                          "completion_tokens": int(usage.get("output_tokens", 0) or 0)}
        elif kind == "response.failed":
            raise _stream_failure((event.get("response") or {}).get("error") or {}, request_id)
        elif kind == "error":
            nested = event.get("error")
            raise _stream_failure(nested if isinstance(nested, dict) else event, request_id)
        elif kind == "response.incomplete":
            details = (event.get("response") or {}).get("incomplete_details") or {}
            raise LLMError(f"The response stopped early (incomplete: {details.get('reason', 'unknown')}).",
                           request_id=request_id)
        return None

    for raw in lines:
        line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        line = line.rstrip("\r\n")
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
            continue
        if line == "" and data_lines:
            result = handle("\n".join(data_lines))
            data_lines = []
            if result is not None:
                return result
    if data_lines:
        result = handle("\n".join(data_lines))
        if result is not None:
            return result
    raise _Transient("The event stream ended before response.completed.", request_id=request_id)


class ResponsesClient:
    def __init__(self, model, token_provider=None, api_base=API_BASE, timeout=180, backoff=5.0,
                 reasoning_effort=None):
        if not model:
            raise LLMError("No model chosen: run python -m cic models and set CIC_CHATGPT_MODEL in .env.")
        if token_provider is None:
            from cic.agent.siwc import access_token as token_provider
        self.model = model
        self.api_base = api_base.rstrip("/")
        self._token = token_provider
        self.timeout = timeout
        self.backoff = backoff
        self.reasoning_effort = reasoning_effort if reasoning_effort is not None else (
            os.environ.get("CIC_CHATGPT_REASONING") or None)

    def describe(self):
        info = {"base_url": self.api_base, "model": self.model, "temperature": None, "billing": "chatgpt_plan"}
        if self.reasoning_effort:
            info["reasoning_effort"] = self.reasoning_effort
        return info

    def body(self, messages):
        instructions = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        items = [{"role": "assistant" if m["role"] == "assistant" else ("developer" if m["role"] == "developer"
                                                                         else "user"),
                  "content": m["content"]} for m in messages if m["role"] != "system"]
        body = {"model": self.model, "input": items, "store": False, "stream": True}
        if instructions:
            body["instructions"] = instructions
        if self.reasoning_effort:
            body["reasoning"] = {"effort": self.reasoning_effort}
        return body

    def _wait(self, transient):
        time.sleep(self.backoff * 2 ** transient)

    def complete(self, messages, max_tokens=None):  # max_tokens is not sent: the route omits length limits
        body = json.dumps(self.body(messages)).encode("utf-8")
        refreshed, force_refresh, transient = False, False, 0
        while True:
            try:
                token = self._token(force_refresh=force_refresh)
            except SignInError as error:
                raise StopBatch(str(error)) from None
            force_refresh = False
            request = urllib.request.Request(self.api_base + "/responses", data=body, method="POST", headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "Authorization": f"Bearer {token}",
            })
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return read_stream(response, response.headers.get("x-request-id"))
            except urllib.error.HTTPError as error:
                status = error.code
                code, message, param = _error_info(error.read().decode("utf-8", errors="replace"))
                request_id = error.headers.get("x-request-id") if error.headers else None
                if status == 401 and not refreshed and code != "subscription_sharing_invalid_user":
                    refreshed = force_refresh = True  # one refresh per request, then give up
                    continue
                if (status == 503 or code in TRANSIENT_CODES) and transient < 3:
                    transient += 1
                    self._wait(transient)
                    continue
                raise StopBatch(_explain(status, code, message, param, request_id), status=status, code=code,
                                request_id=request_id, param=param) from None
            except _Transient as error:
                if transient < 3:
                    transient += 1
                    self._wait(transient)
                    continue
                raise LLMError(f"Gave up after retries: {error}", code=error.code,
                               request_id=error.request_id) from None
            except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
                if transient < 3:
                    transient += 1
                    self._wait(transient)
                    continue
                raise LLMError(f"Network error after retries: {error}") from None
