#!/usr/bin/env python3
"""Stub for CallRail live pull.

Purpose: The 5am PT cron references `scripts/pull_callrail_live.py`. Until a
credential-backed CallRail integration is wired in, this stub exists to
prevent the cron from escalating on FileNotFoundError.

Behavior:
- If /data/_callrail_live/summary.json already exists, leave it untouched
  (pull_live_daily.py carries forward last-known aggregates).
- If it does NOT exist, write an empty summary so downstream code has
  something to read.

The user has explicitly denied re-prompting for the CallRail credential,
so this stub is the correct behavior until they add it via the credentials
pane themselves.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "data" / "_callrail_live"
OUT.mkdir(parents=True, exist_ok=True)
SUMMARY = OUT / "summary.json"


def main() -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if SUMMARY.exists():
        # Preserve last-known aggregates; just stamp the check
        try:
            d = json.loads(SUMMARY.read_text())
        except Exception:
            d = {}
        d.setdefault("_stub_note", "No live CallRail credential yet — last-known preserved.")
        d["_last_stub_check"] = now
        SUMMARY.write_text(json.dumps(d, indent=2))
        print(f"[callrail-stub] no credential; preserved last-known summary at {SUMMARY}")
        return

    # No prior summary — write an empty scaffold so pull_live_daily.py can read it
    empty = {
        "generated_at": now,
        "_stub_note": (
            "CallRail credential not configured. This is a stub; wire a real "
            "CallRail pull when credentials are available."
        ),
        "windows": {
            "last_7d": {"calls": 0, "answered": 0, "missed": 0, "first_time": 0, "qualified": 0},
            "last_30d": {"calls": 0, "answered": 0, "missed": 0, "first_time": 0, "qualified": 0},
        },
        "by_office": [],
    }
    SUMMARY.write_text(json.dumps(empty, indent=2))
    print(f"[callrail-stub] wrote empty scaffold to {SUMMARY}")


if __name__ == "__main__":
    main()
