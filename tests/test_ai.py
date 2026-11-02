"""Tests for the AI client's resilience: long rate limits switch model, short ones wait, malformed JSON retries."""
import email.message
import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from junta.ai import AIClient  # noqa: E402


def http_error(code, retry_after=None, message="limit"):
    headers = email.message.Message()
    if retry_after is not None:
        headers["retry-after"] = str(retry_after)
    body = json.dumps({"error": {"message": message}}).encode()
    return urllib.error.HTTPError("https://api", code, "error", headers, io.BytesIO(body))


class ScriptedClient(AIClient):
    """AIClient whose HTTP layer replays a script: each item is a text answer or an exception to raise."""

    def __init__(self, script, **kw):
        super().__init__("https://api", "key", "big-model", fallback_models=["small-model"], **kw)
        self.script, self.calls = list(script), []

    def _post(self, body):
        self.calls.append(body.get("model"))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class AIClientTest(unittest.TestCase):
    def test_daily_limit_switches_to_fallback_model(self):
        ai = ScriptedClient([http_error(429, retry_after=104), '{"intent": "greeting"}'], max_wait=3.0)
        self.assertEqual(ai.chat("s", "u", json_mode=True), {"intent": "greeting"})
        self.assertEqual(ai.calls, ["big-model", "small-model"])
        self.assertEqual(ai.last_model, "small-model")

    def test_short_limit_waits_and_retries_same_model(self):
        ai = ScriptedClient([http_error(429, retry_after=0.01), "hola"], max_wait=3.0)
        self.assertEqual(ai.chat("s", "u"), "hola")
        self.assertEqual(ai.calls, ["big-model", "big-model"])

    def test_malformed_json_retries_without_json_mode(self):
        ai = ScriptedClient([http_error(400, message="Failed to validate JSON"), 'Sure: {"intent": "question"}'])
        self.assertEqual(ai.chat("s", "u", json_mode=True), {"intent": "question"})

    def test_everything_fails_returns_none(self):
        ai = ScriptedClient([http_error(429, retry_after=500), http_error(429, retry_after=500)], max_wait=3.0)
        self.assertIsNone(ai.chat("s", "u"))
        self.assertIn("small-model", ai.last_error)


if __name__ == "__main__":
    unittest.main(verbosity=2)
