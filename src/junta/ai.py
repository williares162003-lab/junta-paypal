"""Minimal client for any OpenAI-compatible chat API (default: Groq free tier). Standard library only.

Every call returns None on any failure, so the organizer can fall back to its rule-based mode.
"""
import json
import urllib.error
import urllib.request


class AIClient:
    def __init__(self, base_url, api_key, model, timeout=30):
        self.base_url, self.api_key, self.model, self.timeout = base_url.rstrip("/"), api_key, model, timeout

    @classmethod
    def from_env(cls, env):
        if not env.get("AI_API_KEY") or not env.get("AI_BASE_URL") or not env.get("AI_MODEL"):
            return None
        return cls(env["AI_BASE_URL"], env["AI_API_KEY"], env["AI_MODEL"])

    def chat(self, system, user, json_mode=False, max_tokens=500):
        body = {"model": self.model, "temperature": 0.2, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(f"{self.base_url}/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {self.api_key}",
                                              "Content-Type": "application/json", "User-Agent": "junta/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                text = json.load(r)["choices"][0]["message"]["content"]
        except (urllib.error.URLError, TimeoutError, KeyError, IndexError, ValueError):
            return None
        if not json_mode:
            return text.strip()
        try:
            return json.loads(text)
        except ValueError:
            return None
