"""Unit tests for lead_sms_automation apply/send loop.

Verifies the apply path:

- Calls the provider and writeback exactly once per eligible candidate
  when all gates are satisfied.
- Does not call the provider or writeback when the provider is disabled.
- Surfaces a clear blocker when no sends happen (e.g. missing booking
  link) instead of silently reporting 0/0.

These tests never touch the network and never read the private config;
they wire fake provider + sheets adapters and call ``apply_sends``
directly. Run with ``python3 scripts/test_lead_sms_apply.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lead_sms_automation import (  # noqa: E402
    OpenPhoneAdapter,
    RunResult,
    apply_sends,
)


class FakeSheets:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, dict[int, str]]] = []
        self.available = True

    def update_cells(self, tab, row_number_1based, updates):
        self.calls.append((tab, int(row_number_1based), dict(updates)))
        return {"ok": True, "status": "updated", "cells": len(updates)}


class FakeOpenPhone:
    """Stand-in for ``OpenPhoneAdapter`` with no network IO.

    Implements ``send`` only; the apply path never calls anything else.
    """

    def __init__(self, *, enabled: bool = True, always_ok: bool = True) -> None:
        self.enabled = enabled
        self.calls: list[tuple[str, str]] = []
        self._always_ok = always_ok

    def send(self, to_phone_e164, body):
        if not self.enabled:
            return {"ok": False, "status": "disabled"}
        self.calls.append((to_phone_e164, body))
        if self._always_ok:
            return {"ok": True, "status": "sent"}
        return {"ok": False, "status": "http_500"}


def _candidate(office: str = "Encino", phone: str = "+13105551111") -> dict:
    return {
        "tab": f"{office} Leads",
        "row_number": 5,
        "phone_e164": phone,
        "first_name": "Alex",
        "office": office,
        "source": "General",
        "contacted_col": 4,
        "feedback_cols": {"ai_sms_sent_at": 7, "ai_sms_status": 8, "ai_sms_notes": 9},
    }


def _cfg(**overrides):
    cfg = {
        "send_policy": {
            "enabled": True,
            "max_initial_backfill_per_run": 25,
            "max_hourly_sends": 25,
        },
        "office_booking_links": {
            "Encino": "https://clovedental.com/book/encino",
            "default": "https://clovedental.com/book",
        },
    }
    cfg.update(overrides)
    return cfg


def test_apply_calls_provider_and_writeback_exactly_once_per_candidate() -> None:
    sheets = FakeSheets()
    op = FakeOpenPhone(enabled=True, always_ok=True)
    result = RunResult()
    result._private_candidates = [_candidate()]

    apply_sends(sheets, op, _cfg(), result)

    assert len(op.calls) == 1, f"expected 1 send, got {len(op.calls)}"
    assert len(sheets.calls) == 1, f"expected 1 writeback, got {len(sheets.calls)}"
    assert result.sms_messages_sent == 1
    assert result.sheet_rows_modified == 1
    assert result.sent_today == 1
    tab, row, updates = sheets.calls[0]
    assert tab == "Encino Leads"
    assert row == 5
    # Contacted column + 3 feedback cells.
    assert updates[4] == "YES"
    assert updates[8] == "sent"
    print("OK: apply_calls_provider_and_writeback_exactly_once_per_candidate")


def test_apply_sends_zero_when_provider_disabled() -> None:
    sheets = FakeSheets()
    op = FakeOpenPhone(enabled=False)
    result = RunResult()
    result._private_candidates = [_candidate()]

    apply_sends(sheets, op, _cfg(), result)

    assert len(op.calls) == 0, "provider must not be called when disabled"
    assert len(sheets.calls) == 0, "writeback must not happen when send fails"
    assert result.sms_messages_sent == 0
    assert result.sheet_rows_modified == 0
    # Blocker surfaced.
    assert result.blockers, "expected a blocker when 0 sends from N candidates"
    assert "0 sends" in result.blockers[0]
    print("OK: apply_sends_zero_when_provider_disabled")


def test_missing_booking_link_surfaces_blocker() -> None:
    sheets = FakeSheets()
    op = FakeOpenPhone(enabled=True, always_ok=True)
    result = RunResult()
    # Office "Ventura" with no booking link AND no default.
    result._private_candidates = [_candidate(office="Ventura")]
    cfg = {
        "send_policy": {"enabled": True, "max_initial_backfill_per_run": 25,
                        "max_hourly_sends": 25},
        "office_booking_links": {"Encino": "https://x.example/encino"},
    }

    apply_sends(sheets, op, cfg, result)

    assert len(op.calls) == 0
    assert result.sms_messages_sent == 0
    assert result.apply_skips.get("missing_booking_link") == 1
    assert any("Ventura" in b for b in result.blockers), result.blockers
    print("OK: missing_booking_link_surfaces_blocker")


def test_cap_enforced() -> None:
    sheets = FakeSheets()
    op = FakeOpenPhone(enabled=True, always_ok=True)
    result = RunResult()
    result._private_candidates = [
        _candidate(phone=f"+131055511{i:02d}") for i in range(30)
    ]
    cfg = _cfg()
    cfg["send_policy"]["max_initial_backfill_per_run"] = 25

    apply_sends(sheets, op, cfg, result)

    assert len(op.calls) == 25, f"cap 25 not enforced, got {len(op.calls)}"
    assert result.sms_messages_sent == 25
    print("OK: cap_enforced")


def test_provider_failure_does_not_writeback() -> None:
    sheets = FakeSheets()
    op = FakeOpenPhone(enabled=True, always_ok=False)
    result = RunResult()
    result._private_candidates = [_candidate()]

    apply_sends(sheets, op, _cfg(), result)

    assert len(op.calls) == 1
    assert len(sheets.calls) == 0, "writeback must not happen on send failure"
    assert result.sms_messages_sent == 0
    assert result.sheet_rows_modified == 0
    print("OK: provider_failure_does_not_writeback")


def test_real_adapter_send_short_circuits_when_disabled() -> None:
    """Defense in depth: even the real OpenPhoneAdapter.send must
    short-circuit and return ok=False when enabled=false, so disabling
    the provider in config is sufficient to guarantee no network IO.
    """
    op = OpenPhoneAdapter({"openphone": {"enabled": False}})
    r = op.send("+13105551111", "hello")
    assert r == {"ok": False, "status": "disabled"}
    print("OK: real_adapter_send_short_circuits_when_disabled")


if __name__ == "__main__":
    test_apply_calls_provider_and_writeback_exactly_once_per_candidate()
    test_apply_sends_zero_when_provider_disabled()
    test_missing_booking_link_surfaces_blocker()
    test_cap_enforced()
    test_provider_failure_does_not_writeback()
    test_real_adapter_send_short_circuits_when_disabled()
    print("\nAll lead-SMS apply-path tests passed.")
