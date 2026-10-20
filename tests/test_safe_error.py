"""Provider errors reach Result.trace (and endpoint callers) — only status + type."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from router.adapters.openai_compatible import _safe_error  # noqa: E402


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _exc(resp):
    e = RuntimeError("raw text with org-SECRET and ak-SECRET")
    e.response = resp
    return e


class TestSafeError(unittest.TestCase):
    def test_status_and_type_only(self):
        body = {"error": {"type": "exceeded_current_quota_error",
                          "message": "account org-SECRET <ak-SECRET> suspended"}}
        out = _safe_error(_exc(_Resp(429, body)))
        self.assertEqual(out, "HTTP 429 exceeded_current_quota_error")
        self.assertNotIn("SECRET", out)

    def test_non_json_body(self):
        self.assertEqual(_safe_error(_exc(_Resp(502, ValueError("html")))), "HTTP 502")

    def test_untrusted_type_value_dropped(self):
        body = {"error": {"type": "quota error; org-SECRET"}}
        self.assertEqual(_safe_error(_exc(_Resp(400, body))), "HTTP 400")

    def test_no_response_is_class_name(self):
        self.assertEqual(_safe_error(TimeoutError("org-SECRET")), "TimeoutError")


if __name__ == "__main__":
    unittest.main()
