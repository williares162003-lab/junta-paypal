"""Minimal client for any OpenAI-compatible chat API (default: Groq free tier). Standard library only.

Every call returns None on any final failure, so the organizer can fall back to its rule-based mode.
Free tiers have small limits (Groq: 8,000 tokens/minute), so the client:
  - waits and retries when the API answers 429 (rate limit),
  - retries without JSON mode when the API rejects a malformed JSON generation, and extracts the JSON itself.
"""
import json
import re
import time
import urllib.error
import urllib.request
import uuid


class AIClient:
    def __init__(self, base_url, api_key, model, timeout=30, max_retries=3, max_wait=20.0, fallback_models=()):
        self.base_url, self.api_key, self.model = base_url.rstrip("/"), api_key, model
        self.timeout, self.max_retries, self.max_wait = timeout, max_retries, max_wait
        self.fallback_models = [m for m in fallback_models if m and m != model]
        self.last_error, self.last_model = None, None

    @classmethod
    def from_env(cls, env, interactive=False):
        """interactive=True (the web app): give up fast and let the rules answer instead of making people wait.

        AI_FALLBACK_MODELS (comma-separated) are tried when the main model hits a long limit,
        such as the free tier's tokens-per-day cap. Each model has its own quota.
        """
        if not env.get("AI_API_KEY") or not env.get("AI_BASE_URL") or not env.get("AI_MODEL"):
            return None
        fallbacks = [m.strip() for m in env.get("AI_FALLBACK_MODELS", "").split(",")]
        if interactive:
            return cls(env["AI_BASE_URL"], env["AI_API_KEY"], env["AI_MODEL"], timeout=15, max_retries=1,
                       max_wait=3.0, fallback_models=fallbacks)
        return cls(env["AI_BASE_URL"], env["AI_API_KEY"], env["AI_MODEL"], fallback_models=fallbacks)

    def _retry_after(self, err):
        return float(err.headers.get("retry-after") or 0) or 6.0

    def _post(self, body):
        req = urllib.request.Request(f"{self.base_url}/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {self.api_key}",
                                              "Content-Type": "application/json", "User-Agent": "junta/0.1"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.load(r)["choices"][0]["message"]["content"]

    def chat(self, system, user, json_mode=False, max_tokens=500):
        self.last_error, self.last_model = None, None
        for model in [self.model] + self.fallback_models:
            text = self._chat_one_model(model, system, user, json_mode, max_tokens)
            if text is not None:
                self.last_model = model
                if not json_mode:
                    return text.strip()
                parsed = _extract_json(text)
                if parsed is not None:
                    return parsed
        return None

    def _chat_one_model(self, model, system, user, json_mode, max_tokens):
        """Returns the raw text, or None to try the next model (or give up)."""
        body = {"model": model, "temperature": 0.2, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        for attempt in range(self.max_retries + 1):
            try:
                return self._post(body)
            except urllib.error.HTTPError as err:
                detail = err.read()[:300].decode(errors="replace")
                self.last_error = f"{model}: HTTP {err.code}: {detail[:120]}"
                if err.code == 429:
                    wait = self._retry_after(err)
                    if wait > self.max_wait or attempt >= self.max_retries:
                        return None                   # long limit (e.g. tokens per day): switch model now
                    time.sleep(wait)
                    continue
                if err.code == 400 and "json" in detail.lower() and "response_format" in body:
                    body.pop("response_format")       # let the model answer as text; we extract the JSON
                    continue
                return None
            except (urllib.error.URLError, TimeoutError, KeyError, IndexError, ValueError) as err:
                self.last_error = f"{model}: {err!r}"
                return None
        return None


    def transcribe(self, audio, filename, content_type, model="whisper-large-v3-turbo"):
        """Speech to text through the OpenAI-compatible /audio/transcriptions endpoint. Returns text or None."""
        boundary = "junta" + uuid.uuid4().hex
        parts = [
            _form_field(boundary, "model", model),
            _form_field(boundary, "response_format", "json"),
            (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
             f"Content-Type: {content_type}\r\n\r\n").encode() + audio + b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
        req = urllib.request.Request(f"{self.base_url}/audio/transcriptions", data=b"".join(parts),
                                     headers={"Authorization": f"Bearer {self.api_key}", "User-Agent": "junta/0.1",
                                              "Content-Type": f"multipart/form-data; boundary={boundary}"})
        self.last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=max(self.timeout, 30)) as r:
                    return (json.load(r).get("text") or "").strip() or None
            except urllib.error.HTTPError as err:
                self.last_error = f"HTTP {err.code}: {err.read()[:120].decode(errors='replace')}"
                if err.code == 429 and attempt < self.max_retries:
                    time.sleep(min(self._retry_after(err), self.max_wait))
                    continue
                return None
            except (urllib.error.URLError, TimeoutError, ValueError) as err:
                self.last_error = repr(err)
                return None
        return None


def _form_field(boundary, name, value):
    return f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()


def _extract_json(text):
    try:
        return json.loads(text)
    except ValueError:
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            try:
                return json.loads(match.group(0))
            except ValueError:
                pass
    return None
