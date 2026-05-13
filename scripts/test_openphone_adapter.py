"""Unit tests for OpenPhoneAdapter HTTP contract.

These tests verify the wire-level shape of ``OpenPhoneAdapter.send``
against the OpenPhone v1 ``/messages`` handoff:

- Authorization header is the raw API key (NOT ``Bearer <key>``).
- Content-Type is ``application/json``.
- Endpoint is POST ``/v1/messages``.
- Payload is ``{"from": <E.164>, "to": [<E.164>], "content": <body>}``.
- ``to`` is always a list of exactly one E.164 phone.
- A real ``phoneNumberId`` field is NOT present in the payload (its
  presence is what caused the production HTTP 400 storm).
- HTTP 400 responses surface a non-PII aggregate reason tag.

No real network IO is performed; ``urllib.request.urlopen`` is patched
to a fake that captures the outgoing request. Run with:

    python3 scripts/test_openphone_adapter.py
"""

from __future__ import annotations

import io
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lead_sms_automation as lsa  # noqa: E402
from lead_sms_automation import (  # noqa: E402
    OpenPhoneAdapter,
    _sanitize_provider_400_reason,
)


# Fake API key. Not a real secret. Constructed at runtime so the literal
# string never appears as a fixed token in the source tree.
_FAKE_KEY = "test_" + "x" * 16
_FAKE_FROM = "+18055550100"
_FAKE_TO = "+13105551111"


def _adapter(**overrides):
    op = {
        "enabled": True,
        "api_key": _FAKE_KEY,
        "from_number": _FAKE_FROM,
        "phone_number_id": "PNtest123",
        "base_url": "https://api.openphone.com/v1",
    }
    op.update(overrides)
    return OpenPhoneAdapter({"openphone": op})


class _FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Capture:
    """urlopen replacement that records the outgoing Request."""

    def __init__(self, status: int = 202, body: dict | None = None) -> None:
        self.status = status
        self.body = json.dumps(body or {"id": "ignored"}).encode("utf-8")
        self.last_request: urllib.request.Request | None = None

    def __call__(self, req, timeout=None):  # noqa: D401 - urlopen shim
        self.last_request = req
        return _FakeResponse(self.status, self.body)


def _install_capture(monkeyfn) -> _Capture:
    cap = monkeyfn()
    # Patch the symbol the adapter actually calls.
    urllib.request.urlopen = cap  # type: ignore[assignment]
    return cap


def _restore_urlopen(orig) -> None:
    urllib.request.urlopen = orig  # type: ignore[assignment]


def _with_capture(status=202, body=None):
    orig = urllib.request.urlopen
    cap = _Capture(status=status, body=body)
    urllib.request.urlopen = cap  # type: ignore[assignment]
    return cap, orig


def test_send_uses_raw_authorization_header_not_bearer() -> None:
    cap, orig = _with_capture()
    try:
        op = _adapter()
        r = op.send(_FAKE_TO, "hello world")
    finally:
        _restore_urlopen(orig)
    assert r == {"ok": True, "status": "sent"}, r
    req = cap.last_request
    assert req is not None
    # urllib lower-cases header names for lookup.
    auth = req.get_header("Authorization")
    assert auth == _FAKE_KEY, f"Authorization header must be raw key, got {auth!r}"
    assert not auth.lower().startswith("bearer"), (
        f"Authorization must NOT use Bearer prefix; got {auth!r}"
    )
    ctype = req.get_header("Content-type")
    assert ctype == "application/json", f"Content-Type must be JSON, got {ctype!r}"
    print("OK: send_uses_raw_authorization_header_not_bearer")


def test_send_payload_shape_matches_handoff() -> None:
    cap, orig = _with_capture()
    try:
        op = _adapter()
        op.send(_FAKE_TO, "hello")
    finally:
        _restore_urlopen(orig)
    req = cap.last_request
    assert req is not None
    assert req.method == "POST"
    assert req.full_url == "https://api.openphone.com/v1/messages", req.full_url
    payload = json.loads(req.data.decode("utf-8"))
    assert isinstance(payload, dict)
    # from = configured E.164.
    assert payload["from"] == _FAKE_FROM, payload
    # to = array of exactly one E.164 phone.
    assert isinstance(payload["to"], list), payload
    assert len(payload["to"]) == 1, payload
    assert payload["to"][0] == _FAKE_TO, payload
    assert payload["to"][0].startswith("+1"), payload
    # content populated, non-empty string.
    assert isinstance(payload["content"], str) and payload["content"], payload
    # phoneNumberId MUST NOT appear — its presence was the prod 400 cause.
    assert "phoneNumberId" not in payload, (
        "phoneNumberId must not be in the payload — it caused HTTP 400 in prod."
    )
    print("OK: send_payload_shape_matches_handoff")


def test_send_returns_disabled_when_provider_disabled() -> None:
    op = _adapter(enabled=False)
    r = op.send(_FAKE_TO, "hi")
    assert r == {"ok": False, "status": "disabled"}
    print("OK: send_returns_disabled_when_provider_disabled")


def test_send_returns_missing_from_when_from_number_absent() -> None:
    op = _adapter(from_number="")
    r = op.send(_FAKE_TO, "hi")
    assert r == {"ok": False, "status": "missing_from"}, r
    print("OK: send_returns_missing_from_when_from_number_absent")


def test_send_returns_bad_input_for_empty_phone_or_body() -> None:
    op = _adapter()
    assert op.send("", "hi") == {"ok": False, "status": "bad_input"}
    assert op.send(_FAKE_TO, "") == {"ok": False, "status": "bad_input"}
    print("OK: send_returns_bad_input_for_empty_phone_or_body")


def test_send_surfaces_sanitized_400_reason() -> None:
    # Provider replies 400 with a generic OpenPhone validation message.
    body = {"message": "to is required", "code": "validation_error"}
    cap, orig = _with_capture(status=400, body=body)
    try:
        op = _adapter()
        r = op.send(_FAKE_TO, "hi")
    finally:
        _restore_urlopen(orig)
    assert r["ok"] is False
    assert r["status"] == "http_400_missing_to", r
    # Status string must NOT contain phone, body, key, or phone_number_id.
    s = r["status"]
    assert _FAKE_TO not in s and _FAKE_KEY not in s
    assert _FAKE_FROM not in s
    assert "PNtest123" not in s
    assert "hi" != s and "hello" not in s
    print("OK: send_surfaces_sanitized_400_reason")


def test_send_collapses_unknown_400_to_validation() -> None:
    body = {"message": "something weird happened with id abc-123"}
    cap, orig = _with_capture(status=400, body=body)
    try:
        op = _adapter()
        r = op.send(_FAKE_TO, "hi")
    finally:
        _restore_urlopen(orig)
    assert r == {"ok": False, "status": "http_400_validation"}, r
    print("OK: send_collapses_unknown_400_to_validation")


def test_sanitize_400_reason_known_tokens() -> None:
    cases = [
        ({"message": "from is required"}, "missing_from"),
        ({"message": "content is required"}, "missing_content"),
        ({"errors": [{"message": "invalid from format"}]}, "invalid_from_format"),
        ({"errors": [{"field": "to", "message": "invalid phone"}]},
         "invalid_to_format"),
        ({"message": "This number is not owned by your workspace"},
         "from_not_owned"),
        ({"message": "Unknown field phoneNumberId"}, "unsupported_field"),
        ({"message": "quota exceeded"}, "rate_or_quota"),
        ({}, "validation"),
        ({"message": ""}, "validation"),
    ]
    for body, expected in cases:
        got = _sanitize_provider_400_reason(body)
        assert got == expected, f"{body!r} -> {got!r}, expected {expected!r}"
    print("OK: sanitize_400_reason_known_tokens")


def test_real_adapter_short_circuits_when_disabled_no_network() -> None:
    """Defense in depth — disabled adapter must not even attempt urlopen."""
    called = {"n": 0}

    def boom(*a, **k):
        called["n"] += 1
        raise AssertionError("urlopen must not be called when disabled")

    orig = urllib.request.urlopen
    urllib.request.urlopen = boom  # type: ignore[assignment]
    try:
        op = OpenPhoneAdapter({"openphone": {"enabled": False}})
        r = op.send(_FAKE_TO, "hi")
    finally:
        urllib.request.urlopen = orig  # type: ignore[assignment]
    assert r == {"ok": False, "status": "disabled"}
    assert called["n"] == 0
    print("OK: real_adapter_short_circuits_when_disabled_no_network")


if __name__ == "__main__":
    test_send_uses_raw_authorization_header_not_bearer()
    test_send_payload_shape_matches_handoff()
    test_send_returns_disabled_when_provider_disabled()
    test_send_returns_missing_from_when_from_number_absent()
    test_send_returns_bad_input_for_empty_phone_or_body()
    test_send_surfaces_sanitized_400_reason()
    test_send_collapses_unknown_400_to_validation()
    test_sanitize_400_reason_known_tokens()
    test_real_adapter_short_circuits_when_disabled_no_network()
    print("\nAll OpenPhone adapter contract tests passed.")
