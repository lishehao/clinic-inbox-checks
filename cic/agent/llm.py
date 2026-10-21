"""Minimal client for any OpenAI-compatible /chat/completions endpoint.

Works with OpenRouter, Alibaba Cloud Model Studio (DashScope compatible mode),
OpenAI, or a gateway that speaks the same API. The key is sent only in the
Authorization header and is never logged.
"""

import http.client
import json
import os
import time
import urllib.error
import urllib.request


class LLMError(Exception):
    """A model or API failure. status, code, request_id and param are kept for the run record."""

    def __init__(self, message, status=None, code=None, request_id=None, param=None):
        super().__init__(message)
        self.status, self.code, self.request_id, self.param = status, code, request_id, param


class ChatClient:
    def __init__(self, base_url, api_key, model, temperature=0.7, json_mode=False, timeout=120):
        if not api_key:
            raise LLMError("No API key: set CIC_API_KEY in .env (see .env.example).")
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.model = model
        self.temperature = None if temperature in (None, "", "none") else float(temperature)
        self.json_mode = json_mode
        self.timeout = timeout

    def describe(self):
        return {"base_url": self.base_url, "model": self.model, "temperature": self.temperature}

    def complete(self, messages, max_tokens=None):
        # Generous cap: reasoning models count their reasoning against it. Tokens are billed as used.
        if max_tokens is None:
            max_tokens = int(os.environ.get("CIC_MAX_TOKENS", "4000"))
        body = {"model": self.model, "messages": messages, "max_tokens": max_tokens}
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self._api_key}"}
        if "openrouter.ai" in self.base_url:
            headers["X-Title"] = "Clinic Inbox Checks"
        last_error = None
        for attempt in range(5):
            request = urllib.request.Request(
                self.base_url + "/chat/completions",
                data=json.dumps(body).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as error:
                detail = error.read().decode("utf-8", errors="replace")[:400]
                if error.code == 400 and "temperature" in detail.lower() and "temperature" in body:
                    body.pop("temperature")
                    self.temperature = None
                    continue
                if error.code == 400 and "response_format" in detail.lower() and "response_format" in body:
                    body.pop("response_format")
                    self.json_mode = False
                    continue
                last_error = f"HTTP {error.code}: {detail}"
                if error.code in (408, 409, 429, 500, 502, 503, 504):
                    time.sleep(min(30, 2 * 2 ** attempt))
                    continue
                raise LLMError(last_error, status=error.code,
                               request_id=error.headers.get("x-request-id") if error.headers else None) from None
            except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
                last_error = str(error)
                time.sleep(min(30, 2 * 2 ** attempt))
                continue
            except ValueError:
                last_error = "the endpoint returned a reply that is not JSON"
                time.sleep(min(30, 2 * 2 ** attempt))
                continue
            if "error" in payload and not payload.get("choices"):
                raise LLMError(f"API error: {str(payload['error'])[:400]}")
            message = payload["choices"][0]["message"]
            content = message.get("content") or ""
            if isinstance(content, list):
                content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
            return content, payload.get("usage") or {}
        raise LLMError(f"Gave up after retries: {last_error}")


def extract_json(text, want=dict):
    """Pull the first JSON object (or array) out of a model reply."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    opener, closer = ("{", "}") if want is dict else ("[", "]")
    start = text.find(opener)
    if start < 0:
        raise ValueError("no JSON found in reply")
    depth, in_string, escaped = 0, False, False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return json.loads(text[start:index + 1])
    raise ValueError("unterminated JSON in reply")
