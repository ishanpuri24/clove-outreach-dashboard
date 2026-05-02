"""Validation harness for the public Clove outreach dashboard mirror.

This script enforces the public-mirror contract before each commit
or deploy. It is intentionally dependency-free and safe to run from
any Python 3.9+ interpreter with no virtualenv:

    python3 scripts/validate_public_snapshot.py

Exit code is non-zero if any check fails. The script verifies that:

  1. ``data/snapshot.json`` parses as JSON.
  2. The required top-level operator sections are present
     (KPIs, daily trend, reply mix, channel mixes, channel scorecard,
     experiments, queue health, human follow-ups, guardrail status,
     focus priority, sanitization policy).
  3. Key KPI fields exist and are numeric.
  4. The inline embedded snapshot in ``index.html`` (between the
     ``SNAPSHOT_START`` and ``SNAPSHOT_END`` markers) parses and
     matches ``data/snapshot.json`` exactly.
  5. Neither file contains forbidden sensitive patterns
     (raw clovedds.com prospect addresses other than the documented
     sender accounts, Google Sheet IDs or URLs, free-text reply
     bodies, internal commit hashes, common token shapes, or any
     ``mailto:`` recipient links pointing at private prospects).

Run this before publishing a new snapshot. The dashboard is meant to
remain a static, package-free site that any operator can deploy by
pushing this repo to GitHub Pages, importing it into Vercel, or
serving it from any static origin. Failing this script means the
public mirror is not safe to publish.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = REPO_ROOT / "data" / "snapshot.json"
INDEX_HTML = REPO_ROOT / "index.html"

REQUIRED_TOP_LEVEL_SECTIONS = [
    "generated_at",
    "task",
    "sources",
    "kpis",
    "daily",
    "reply_mix",
    "replies",
    "latest_batch_summary",
    "channel_mix_latest",
    "channel_mix_total",
    "channel_scorecard",
    "experiments",
    "queue_health",
    "human_followups",
    "guardrail_status",
    "guardrails",
    "next_actions",
    "focus_priority",
    "_sanitization",
]

REQUIRED_KPI_FIELDS = [
    "total_sends",
    "weekdays_run",
    "latest_date",
    "latest_sends",
    "latest_cap_usage_pct",
    "total_reply_signals",
    "positive_warm_replies",
    "bounces",
    "ccs_used_on_initial",
    "reply_rate_pct",
    "positive_rate_pct",
]

ALLOWED_SENDER_ADDRESSES = {
    "ip@clovedds.com",
    "aryaan@clovedds.com",
}

# Patterns that must never appear in either file. These are deliberately
# broad on purpose: false positives are cheaper than a leak.
FORBIDDEN_PATTERNS: list[tuple[str, str]] = [
    # Google Sheet identifiers and edit URLs.
    (
        r"docs\.google\.com/spreadsheets/d/[A-Za-z0-9_-]{20,}",
        "Google Sheet URL leaked",
    ),
    (
        r"\bsheet[_-]?id\s*[:=]\s*['\"]?[A-Za-z0-9_-]{20,}",
        "Google Sheet ID assignment leaked",
    ),
    # Common token shapes. These match GitHub PATs, generic API keys,
    # and JWTs. They are not exhaustive, but they catch the typical
    # paste-by-accident cases.
    (r"\bghp_[A-Za-z0-9]{30,}", "GitHub personal access token"),
    (r"\bgithub_pat_[A-Za-z0-9_]{60,}", "GitHub fine-grained token"),
    (r"\bsk-[A-Za-z0-9]{32,}", "API secret key"),
    (
        r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
        "JWT-shaped credential",
    ),
    # mailto links almost certainly leak prospect addresses.
    (r"mailto:", "mailto link in public mirror"),
    # AWS access keys.
    (r"\bAKIA[0-9A-Z]{16}\b", "AWS access key id"),
]


class ValidationError(Exception):
    pass


def _fail(message: str) -> None:
    raise ValidationError(message)


def load_snapshot_json() -> dict[str, Any]:
    if not DATA_FILE.exists():
        _fail(f"Missing required file: {DATA_FILE}")
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _fail(f"data/snapshot.json is not valid JSON: {exc}")
        return {}


def extract_inline_snapshot(html: str) -> dict[str, Any]:
    start_marker = "/* SNAPSHOT_START */"
    end_marker = "/* SNAPSHOT_END */"
    if start_marker not in html or end_marker not in html:
        _fail(
            "index.html is missing the SNAPSHOT_START / SNAPSHOT_END "
            "markers; the inline embedded snapshot cannot be verified."
        )
    block = html.split(start_marker, 1)[1].split(end_marker, 1)[0]
    match = re.search(
        r"window\.__SNAPSHOT__\s*=\s*(?P<json>\{.*\})\s*;",
        block,
        re.DOTALL,
    )
    if not match:
        _fail(
            "Could not find a `window.__SNAPSHOT__ = {...};` assignment "
            "between the SNAPSHOT_START and SNAPSHOT_END markers in "
            "index.html."
        )
    payload = match.group("json")
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        _fail(f"Inline embedded snapshot is not valid JSON: {exc}")
        return {}


def check_required_sections(snap: dict[str, Any]) -> list[str]:
    missing = [k for k in REQUIRED_TOP_LEVEL_SECTIONS if k not in snap]
    if missing:
        _fail(f"snapshot.json missing required sections: {missing}")
    return REQUIRED_TOP_LEVEL_SECTIONS


def check_kpis(snap: dict[str, Any]) -> None:
    kpis = snap.get("kpis") or {}
    missing = [k for k in REQUIRED_KPI_FIELDS if k not in kpis]
    if missing:
        _fail(f"snapshot.json kpis missing required fields: {missing}")
    numeric_fields = [
        "total_sends",
        "weekdays_run",
        "latest_sends",
        "latest_cap_usage_pct",
        "total_reply_signals",
        "positive_warm_replies",
        "bounces",
        "ccs_used_on_initial",
        "reply_rate_pct",
        "positive_rate_pct",
    ]
    for field in numeric_fields:
        if not isinstance(kpis.get(field), (int, float)):
            _fail(
                f"snapshot.json kpis['{field}'] must be numeric, got "
                f"{type(kpis.get(field)).__name__}"
            )


def check_sources_redacted(snap: dict[str, Any]) -> None:
    sources = snap.get("sources") or {}
    sheet_url = (sources.get("sheet_url") or "").lower()
    sheet_id = (sources.get("sheet_id") or "").lower()
    if "docs.google.com" in sheet_url or sheet_url.startswith("http"):
        _fail("sources.sheet_url appears to be a real URL; must be redacted.")
    if sheet_id and "redact" not in sheet_id and len(sheet_id) > 6:
        _fail(
            "sources.sheet_id appears to be a real Google Sheet id; "
            "must be redacted."
        )


def check_replies_redacted(snap: dict[str, Any]) -> None:
    forbidden_keys = {
        "Email From",
        "Summary",
        "Suggested Next Action",
        "Owner",
        "Body",
        "Reply Body",
    }
    for idx, reply in enumerate(snap.get("replies") or []):
        if not isinstance(reply, dict):
            _fail(f"replies[{idx}] is not an object")
        leaked = forbidden_keys.intersection(reply.keys())
        if leaked:
            _fail(
                f"replies[{idx}] exposes forbidden fields "
                f"{sorted(leaked)} in the public mirror."
            )
        org = (reply.get("Organization") or "").strip().lower()
        if org and "redact" not in org:
            _fail(
                f"replies[{idx}].Organization is not redacted: "
                f"{reply.get('Organization')!r}"
            )


def check_latest_batch_redacted(snap: dict[str, Any]) -> None:
    if "latest_batch" in snap:
        _fail(
            "snapshot.json must not include latest_batch (recipient-level "
            "rows). Use latest_batch_summary with size + note only."
        )
    summary = snap.get("latest_batch_summary") or {}
    if "size" not in summary:
        _fail("latest_batch_summary.size is required.")


def check_github_section_redacted(snap: dict[str, Any]) -> None:
    gh = snap.get("github") or {}
    forbidden = {
        "latest_commit_before_dashboard",
        "dashboard_build_commit",
        "repo",
    }
    leaked = forbidden.intersection(gh.keys())
    if leaked:
        _fail(
            f"github section exposes forbidden fields {sorted(leaked)} "
            "in the public mirror."
        )


def _scan_text_for_secrets(label: str, text: str) -> list[str]:
    findings: list[str] = []
    for pattern, description in FORBIDDEN_PATTERNS:
        for m in re.finditer(pattern, text):
            findings.append(
                f"{label}: forbidden pattern matched ({description}): "
                f"{m.group(0)[:80]!r}"
            )
    # Email scan: any clovedds.com address that is not the documented
    # operator sender accounts is treated as a leak. Any non-clovedds
    # email that is not a redaction placeholder is also flagged.
    email_re = re.compile(
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
    )
    for m in email_re.finditer(text):
        addr = m.group(0).lower()
        if addr in ALLOWED_SENDER_ADDRESSES:
            continue
        if addr.endswith("@clovedds.com"):
            findings.append(
                f"{label}: unexpected clovedds.com address leaked: {addr}"
            )
            continue
        # Non-clovedds emails are not allowed at all in the public
        # mirror; the snapshot should redact prospect/reply senders.
        findings.append(
            f"{label}: non-operator email address leaked: {addr}"
        )
    return findings


def check_no_forbidden_patterns(
    snapshot_text: str, html_text: str
) -> None:
    findings = []
    findings.extend(_scan_text_for_secrets("data/snapshot.json", snapshot_text))
    findings.extend(_scan_text_for_secrets("index.html", html_text))
    if findings:
        joined = "\n  - ".join(findings)
        _fail(
            "Forbidden sensitive patterns detected. Resolve before "
            f"publishing:\n  - {joined}"
        )


def check_inline_matches_data_file(
    snap_from_data: dict[str, Any], snap_from_html: dict[str, Any]
) -> None:
    a = json.dumps(snap_from_data, sort_keys=True)
    b = json.dumps(snap_from_html, sort_keys=True)
    if a != b:
        _fail(
            "Inline embedded snapshot in index.html does not match "
            "data/snapshot.json. Re-run scripts/build_snapshot.py to "
            "re-inject the sanitized snapshot."
        )


def main() -> int:
    print("Validating public snapshot ...")
    try:
        snap = load_snapshot_json()
        check_required_sections(snap)
        check_kpis(snap)
        check_sources_redacted(snap)
        check_replies_redacted(snap)
        check_latest_batch_redacted(snap)
        check_github_section_redacted(snap)

        snapshot_text = DATA_FILE.read_text(encoding="utf-8")
        if not INDEX_HTML.exists():
            _fail(f"Missing required file: {INDEX_HTML}")
        html_text = INDEX_HTML.read_text(encoding="utf-8")

        inline_snap = extract_inline_snapshot(html_text)
        check_inline_matches_data_file(snap, inline_snap)

        check_no_forbidden_patterns(snapshot_text, html_text)
    except ValidationError as exc:
        print(f"FAIL: {exc}")
        return 1

    print("OK: snapshot.json parses and contains all required sections.")
    print("OK: inline embedded snapshot in index.html matches data/snapshot.json.")
    print("OK: no forbidden sensitive patterns detected.")
    print("Public snapshot is safe to publish.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
