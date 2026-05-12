#!/usr/bin/env python3
"""Minimal sanitization+shape check for the routine_refresh orchestrator.

Complements ``validate_public_snapshot.py``. This script is cheap to
run after every refresh and asserts the invariants the orchestrator
in ``refresh_marketing_dashboard.py`` is responsible for:

  1. ``data/snapshot.json`` parses as JSON.
  2. The ``routine_refresh`` block exists with sane shape.
  3. The block contains no email addresses, phone numbers, raw
     connector keys, or other forbidden tokens.
  4. ``generated_at`` is an ISO-8601 UTC timestamp.

Exit code is non-zero if any check fails. Safe to run in CI.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = REPO_ROOT / "data" / "snapshot.json"

FORBIDDEN = [
    (re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "email"),
    (re.compile(r"\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b"), "phone"),
    (re.compile(r"\bGCLID\b", re.IGNORECASE), "gclid"),
    (re.compile(r"AIza[0-9A-Za-z_\-]{10,}"), "google_api_key"),
    (re.compile(r"ya29\.[0-9A-Za-z_\-]{10,}"), "google_oauth"),
    (re.compile(r"\b[0-9]{3}-[0-9]{3}-[0-9]{4}\b"), "ads_customer_id"),
]


def _walk(node, path="$"):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}[{i}]")
    elif isinstance(node, str):
        yield path, node


def main() -> int:
    if not SNAPSHOT.exists():
        print(f"FAIL: missing {SNAPSHOT}", file=sys.stderr)
        return 1
    try:
        snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"FAIL: snapshot.json not valid JSON: {e}", file=sys.stderr)
        return 1
    if not isinstance(snap, dict):
        print("FAIL: snapshot.json root is not an object", file=sys.stderr)
        return 1

    gen = snap.get("generated_at")
    if not isinstance(gen, str):
        print("FAIL: generated_at missing", file=sys.stderr)
        return 1
    try:
        datetime.fromisoformat(gen.replace("Z", "+00:00"))
    except ValueError:
        print(f"FAIL: generated_at not ISO-8601: {gen}", file=sys.stderr)
        return 1

    rr = snap.get("routine_refresh")
    if rr is None:
        print("WARN: no routine_refresh block yet; orchestrator has not run.")
    else:
        if not isinstance(rr, dict):
            print("FAIL: routine_refresh must be an object", file=sys.stderr)
            return 1
        for key in ("last_run_at", "last_run_date", "mode", "sources"):
            if key not in rr:
                print(f"FAIL: routine_refresh missing {key}", file=sys.stderr)
                return 1
        if rr["mode"] not in {"fast", "full"}:
            print(f"FAIL: routine_refresh.mode invalid: {rr['mode']!r}", file=sys.stderr)
            return 1

    failures: list[str] = []
    for path, text in _walk(rr or {}, "$.routine_refresh"):
        for pat, label in FORBIDDEN:
            if pat.search(text):
                failures.append(f"{path}: forbidden token ({label})")
    for path, text in _walk(snap.get("callrail_live", {}), "$.callrail_live"):
        # office labels are allowed; reject phone/email/etc.
        for pat, label in FORBIDDEN:
            if label == "phone" and "Clove" in text:
                continue
            if pat.search(text):
                failures.append(f"{path}: forbidden token ({label})")

    if failures:
        print("FAIL: sanitization issues in refreshed sections:", file=sys.stderr)
        for f in failures:
            print("  -", f, file=sys.stderr)
        return 1

    print("OK: routine_refresh shape and sanitization look good.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
