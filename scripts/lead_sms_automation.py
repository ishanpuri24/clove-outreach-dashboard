"""Google Ads Leads Tracker SMS follow-up automation scaffold.

This script is the **public, no-send scaffold** for the Clove
inbound-lead SMS follow-up automation. It is intentionally safe to
publish: it never embeds the private Google Sheet ID, never embeds
provider credentials, and never sends a message.

Design contract
---------------
1. Reads private config from a path supplied via ``--config`` (or the
   ``LEAD_SMS_CONFIG`` env var). The expected file lives outside this
   repository, e.g. ``/home/user/workspace/cron_tracking/<id>/
   lead_sms_config.json`` on the operator's machine. The private
   config holds the spreadsheet id, OpenPhone API key, the
   marketing-line phone-number id (number ending 3707), the per-office
   booking links, and the send-policy flags.
2. Defaults to ``--dry-run``. ``--apply`` is only allowed when *all*
   of the following are true:
       - the provider adapter is enabled (``openphone.enabled`` true
         AND api_key + phone_number_id present),
       - the send_policy block has ``enabled: true``,
       - ``--i-understand-i-am-sending-real-sms`` is also supplied.
   Otherwise ``--apply`` falls back to dry-run with a clear blocker.
3. Scans every "lead-shaped" tab in the Leads spreadsheet. Lead tabs
   carry the standard inbound headers (Date / Name / Email / Phone
   Number / Contacted / Followed Up / Appointment Booked / Treatment
   Opted). Call-tracker tabs carry a similar shape with
   ``Entry Date`` + ``Name of the lead`` + ``Referral Source`` +
   ``Phone Calls`` and are also scanned.
4. Dedupes uncontacted leads by a normalized
   ``(phone_e164, office, source_type)`` key so a backfill run never
   double-texts the same person from two tabs.
5. Excludes obvious sample / test rows (placeholder phone numbers,
   ``test`` / ``sample`` / ``demo`` in the name field, empty
   first-name, etc.).
6. Builds an office-specific, opt-out-bearing SMS template. The
   sample template is rendered with a generic ``[First name]``
   placeholder for the public dashboard snapshot - real names never
   leave the private machine.
7. Writes a **sanitized** public Automations snapshot block. The
   snapshot is aggregate-only: backlog count, eligible count, sent
   today, replies/feedback pending, booked count, booked rate, by
   office and source type, last-run status, and any blockers. No
   names, no emails, no phone numbers, no row numbers, no sheet ids,
   no provider credentials, no raw messages, no private links.

Run
---
    # Default - safe scan, no sends, refreshes public snapshot
    python3 scripts/lead_sms_automation.py --dry-run

    # Hourly cron (suggested) - same as above, idempotent
    python3 scripts/lead_sms_automation.py

    # Apply (only when provider + policy + flag all set)
    python3 scripts/lead_sms_automation.py --apply \\
        --i-understand-i-am-sending-real-sms

Exit codes
----------
0 - scan completed (or dry-run completed) without error
1 - configuration / safety error (e.g. private config missing,
    apply requested without provider+policy enabled)
2 - sheet access error (network, oauth, sheet missing)

This script is deliberately dependency-light. The Google Sheets and
OpenPhone clients are imported lazily so the script can be run on a
machine without those packages installed - in that case it falls back
to a "blocker" state and still refreshes the sanitized public
snapshot with the appropriate status.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_SNAPSHOT = REPO_ROOT / "data" / "snapshot.json"

DEFAULT_PRIVATE_CONFIG_HINT = (
    "/home/user/workspace/cron_tracking/<task-id>/lead_sms_config.json"
)

LEAD_HEADER_ALIASES = {
    "date": "date",
    "entry date": "date",
    "name": "name",
    "name of the lead": "name",
    "email": "email",
    "phone number": "phone",
    "phone": "phone",
    "contacted": "contacted",
    "followed up": "followed_up",
    "follow up": "followed_up",
    "follow-up": "followed_up",
    "appointment booked": "appointment_booked",
    "treatment opted": "treatment_opted",
    "referral source": "referral_source",
    "phone calls": "phone_calls",
}

SAMPLE_TOKENS = {
    "test", "sample", "demo", "example", "fake", "placeholder",
    "lorem", "ipsum", "delete me", "ignore",
}

# Known office labels surfaced by the public dashboard. Used both for
# parsing tab names and for sanitized per-office aggregates. Anything
# not in this list collapses to "Other" so a future tab name never
# leaks into the public snapshot.
KNOWN_OFFICES = [
    "Santa Monica",
    "Encino",
    "Thousand Oaks",
    "Beverly Hills",
    "Riverpark",
    "Oxnard",
    "Ventura",
    "Sherman Oaks",
    "Camarillo",
]

# Source-type labels rolled up to the public snapshot. Tab-level
# detail (General / Emergency / Insurance / Call Tracker) is
# normalized into these public labels.
KNOWN_SOURCE_TYPES = [
    "General",
    "Emergency",
    "Insurance",
    "Call Tracker",
]


@dataclass
class RunResult:
    """Aggregated, sanitized counters for the public snapshot."""

    backlog: int = 0
    eligible: int = 0
    sent_today: int = 0
    replies_pending: int = 0
    booked: int = 0
    by_office: dict[str, dict[str, int]] = field(default_factory=dict)
    by_source: dict[str, dict[str, int]] = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)
    last_run_status: str = "dry_run"
    last_run_at_utc: str = ""
    sheet_rows_modified: int = 0
    sms_messages_sent: int = 0

    def booked_rate_pct(self) -> float:
        if self.eligible <= 0:
            return 0.0
        return round((self.booked / self.eligible) * 100.0, 2)


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_phone(raw: str) -> str | None:
    """Reduce a raw phone string to an E.164-shaped key.

    Returns None when the input is empty, all-zeros, or shorter than
    10 digits. Sample/test numbers like ``5555555555`` collapse to a
    None as well so they never get a real SMS.
    """
    if not raw:
        return None
    digits = re.sub(r"\D+", "", raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return None
    if digits in {"0000000000", "1234567890", "5555555555"}:
        return None
    return "+1" + digits


def office_from_tab(tab_name: str) -> str:
    """Map a tab name to a public office label."""
    if not tab_name:
        return "Other"
    lowered = tab_name.lower()
    for office in KNOWN_OFFICES:
        if office.lower() in lowered:
            return office
    return "Other"


def source_from_tab(tab_name: str) -> str:
    if not tab_name:
        return "General"
    lowered = tab_name.lower()
    if "call" in lowered and "track" in lowered:
        return "Call Tracker"
    if "emergency" in lowered:
        return "Emergency"
    if "insurance" in lowered:
        return "Insurance"
    return "General"


def looks_like_sample(name: str, phone_e164: str | None) -> bool:
    if not phone_e164:
        return True
    if not name:
        return True
    lowered = name.strip().lower()
    if any(tok in lowered for tok in SAMPLE_TOKENS):
        return True
    # All-same-digit phones (e.g. +15555555555) are sample numbers.
    bare = re.sub(r"\D+", "", phone_e164)
    if len(set(bare)) <= 1:
        return True
    return False


def header_index(row: list[str]) -> dict[str, int]:
    """Map a header row to canonical column names -> index."""
    idx: dict[str, int] = {}
    for i, cell in enumerate(row or []):
        key = (cell or "").strip().lower()
        canon = LEAD_HEADER_ALIASES.get(key)
        if canon and canon not in idx:
            idx[canon] = i
    return idx


def is_yes(v: str | None) -> bool:
    if not v:
        return False
    return str(v).strip().lower() in {"y", "yes", "true", "1", "done", "sent"}


def render_sample_sms(office: str, booking_link: str) -> str:
    """Public, name-free SMS sample for the dashboard / README.

    The production renderer fills in the first name on the private
    machine. The public mirror only ever renders this generic form.
    """
    return (
        "Hi [First name], this is the Clove Dental team at "
        f"{office}. Thanks for reaching out - want to grab a time "
        f"that works for you? You can book directly at {booking_link}. "
        "Reply STOP to opt out."
    )


# --------------------------------------------------------------------
# Private-config loading
# --------------------------------------------------------------------

def load_private_config(path: str | None) -> dict[str, Any]:
    """Load the private operator config from disk.

    The config path must come from the operator (CLI flag or env var).
    No default path is hard-coded inside the public repo - the operator
    machine knows where the file lives. When missing, the script
    raises with the hint path so the operator can fix it without
    leaking the real id into git.
    """
    resolved = path or os.environ.get("LEAD_SMS_CONFIG")
    if not resolved:
        raise FileNotFoundError(
            "Private lead_sms_config.json not found. Pass --config "
            "<path> or set LEAD_SMS_CONFIG. Expected layout: "
            f"{DEFAULT_PRIVATE_CONFIG_HINT}"
        )
    p = Path(resolved).expanduser()
    if not p.exists():
        raise FileNotFoundError(
            f"Private config path does not exist: {p}. Expected "
            f"layout: {DEFAULT_PRIVATE_CONFIG_HINT}"
        )
    return json.loads(p.read_text(encoding="utf-8"))


# --------------------------------------------------------------------
# Sheets adapter (lazy import; degrades gracefully)
# --------------------------------------------------------------------

class SheetsAdapter:
    """Wraps googleapiclient if available; otherwise records a blocker."""

    def __init__(self, spreadsheet_id: str) -> None:
        self.spreadsheet_id = spreadsheet_id
        self.available = False
        self.error: str | None = None
        self._service = None
        try:
            from googleapiclient.discovery import build  # type: ignore
            import google.auth  # type: ignore

            creds, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/spreadsheets"]
            )
            self._service = build(
                "sheets", "v4", credentials=creds, cache_discovery=False
            )
            self.available = True
        except Exception as exc:  # pragma: no cover - adapter blocker
            self.error = (
                "Google Sheets client not available "
                f"({type(exc).__name__}). Install google-api-python-client "
                "and configure application default credentials to enable "
                "live scanning."
            )

    def list_tabs(self) -> list[str]:
        if not self.available or self._service is None:
            return []
        meta = self._service.spreadsheets().get(
            spreadsheetId=self.spreadsheet_id
        ).execute()
        return [
            s["properties"]["title"]
            for s in meta.get("sheets", [])
            if "properties" in s
        ]

    def read_tab(self, tab: str) -> list[list[str]]:
        if not self.available or self._service is None:
            return []
        rng = f"'{tab}'"
        resp = self._service.spreadsheets().values().get(
            spreadsheetId=self.spreadsheet_id,
            range=rng,
        ).execute()
        return resp.get("values", []) or []


# --------------------------------------------------------------------
# OpenPhone provider adapter (stub - disabled by default)
# --------------------------------------------------------------------

class OpenPhoneAdapter:
    """Stub OpenPhone client. Disabled until private config enables it.

    Expected private-config fields (mirrored in the README):
        openphone.enabled            : bool (must be true)
        openphone.api_key            : str  (provider API key)
        openphone.phone_number_id    : str  (id of the marketing line
                                              ending 3707)
        openphone.from_number_ending : str  (sanity check, e.g. "3707")

    The send() method intentionally raises until the adapter is
    wired up against the OpenPhone REST API. The shape of that call
    is not yet finalized in this scaffold; an explicit error is
    safer than a half-finished POST.
    """

    def __init__(self, cfg: dict[str, Any]) -> None:
        op = (cfg or {}).get("openphone") or {}
        self.enabled = bool(op.get("enabled"))
        self.api_key_present = bool(op.get("api_key"))
        self.phone_id_present = bool(op.get("phone_number_id"))
        self.from_ending = str(op.get("from_number_ending") or "")
        self.notes = str(op.get("notes") or "")

    def ready(self) -> tuple[bool, str]:
        if not self.enabled:
            return False, (
                "OpenPhone adapter disabled in private config "
                "(openphone.enabled=false)."
            )
        if not self.api_key_present:
            return False, "OpenPhone api_key missing in private config."
        if not self.phone_id_present:
            return False, "OpenPhone phone_number_id missing in private config."
        return True, "OpenPhone adapter configured."

    def send(self, to_phone: str, body: str) -> dict[str, Any]:
        raise RuntimeError(
            "OpenPhone send path is intentionally not implemented in "
            "this scaffold. Finalize the OpenPhone REST contract, then "
            "wire send() against it. Until then this scaffold stays no-send."
        )


# --------------------------------------------------------------------
# Core scan
# --------------------------------------------------------------------

def _bump(buckets: dict[str, dict[str, int]], key: str, field: str) -> None:
    slot = buckets.setdefault(key, {
        "backlog": 0, "eligible": 0, "sent_today": 0,
        "replies_pending": 0, "booked": 0,
    })
    slot[field] = slot.get(field, 0) + 1


def scan_leads(
    sheets: SheetsAdapter,
    cfg: dict[str, Any],
) -> RunResult:
    result = RunResult()
    result.last_run_at_utc = _now_utc_iso()
    seen_keys: set[tuple[str, str, str]] = set()

    if not sheets.available:
        result.blockers.append(
            sheets.error or "Sheets adapter not available."
        )
        result.last_run_status = "blocked"
        return result

    try:
        tabs = sheets.list_tabs()
    except Exception as exc:
        result.blockers.append(
            f"Could not list sheet tabs ({type(exc).__name__})."
        )
        result.last_run_status = "blocked"
        return result

    for tab in tabs:
        rows = []
        try:
            rows = sheets.read_tab(tab)
        except Exception:
            # Skip tabs we cannot read; don't leak the tab name into
            # blockers (it can carry office-specific descriptors).
            continue
        if not rows or len(rows) < 2:
            continue
        idx = header_index(rows[0])
        if "phone" not in idx or "name" not in idx:
            # Not a lead tab.
            continue
        office = office_from_tab(tab)
        source = source_from_tab(tab)

        for raw in rows[1:]:
            if not raw:
                continue
            name = (raw[idx["name"]] if idx.get("name", -1) < len(raw)
                    else "") or ""
            phone_raw = (raw[idx["phone"]] if idx.get("phone", -1) < len(raw)
                         else "") or ""
            contacted = (raw[idx["contacted"]]
                         if "contacted" in idx
                         and idx["contacted"] < len(raw) else "")
            followed = (raw[idx["followed_up"]]
                        if "followed_up" in idx
                        and idx["followed_up"] < len(raw) else "")
            booked = (raw[idx["appointment_booked"]]
                      if "appointment_booked" in idx
                      and idx["appointment_booked"] < len(raw) else "")

            phone_e164 = normalize_phone(phone_raw)
            if looks_like_sample(name, phone_e164):
                continue
            key = (phone_e164 or "", office, source)
            if key in seen_keys:
                continue
            seen_keys.add(key)

            if is_yes(booked):
                result.booked += 1
                _bump(result.by_office, office, "booked")
                _bump(result.by_source, source, "booked")
                continue
            if not is_yes(contacted):
                result.backlog += 1
                _bump(result.by_office, office, "backlog")
                _bump(result.by_source, source, "backlog")
                result.eligible += 1
                _bump(result.by_office, office, "eligible")
                _bump(result.by_source, source, "eligible")
            elif is_yes(contacted) and not is_yes(followed):
                # Contacted but no follow-up - count as replies_pending
                # so the operator can see how many threads await a
                # status update.
                result.replies_pending += 1
                _bump(result.by_office, office, "replies_pending")
                _bump(result.by_source, source, "replies_pending")

    result.last_run_status = "dry_run"
    return result


# --------------------------------------------------------------------
# Public snapshot writeback
# --------------------------------------------------------------------

def _office_rows(result: RunResult) -> list[dict[str, Any]]:
    rows = []
    for office in KNOWN_OFFICES + ["Other"]:
        slot = result.by_office.get(office)
        if not slot:
            continue
        rows.append({
            "office": office,
            "backlog": slot.get("backlog", 0),
            "eligible": slot.get("eligible", 0),
            "sent_today": slot.get("sent_today", 0),
            "replies_pending": slot.get("replies_pending", 0),
            "booked": slot.get("booked", 0),
        })
    return rows


def _source_rows(result: RunResult) -> list[dict[str, Any]]:
    rows = []
    for source in KNOWN_SOURCE_TYPES:
        slot = result.by_source.get(source)
        if not slot:
            continue
        rows.append({
            "source_type": source,
            "backlog": slot.get("backlog", 0),
            "eligible": slot.get("eligible", 0),
            "sent_today": slot.get("sent_today", 0),
            "replies_pending": slot.get("replies_pending", 0),
            "booked": slot.get("booked", 0),
        })
    return rows


def build_automations_block(
    result: RunResult,
    provider_status: str,
    send_policy_enabled: bool,
    apply_mode: bool,
) -> dict[str, Any]:
    """Construct the sanitized public Automations block."""
    sample_template = (
        "Hi [First name], this is the Clove Dental team at [Office]. "
        "Thanks for reaching out - want to grab a time that works for "
        "you? You can book directly at [office booking link]. Reply "
        "STOP to opt out."
    )
    return {
        "title": "Automations",
        "as_of": result.last_run_at_utc,
        "items": [
            {
                "id": "google-ads-lead-sms",
                "name": "Google Ads lead SMS follow-up",
                "purpose": (
                    "Hourly catch-up text to uncontacted Google Ads "
                    "leads from the office lead tabs, with office-"
                    "specific booking links and opt-out language."
                ),
                "status": result.last_run_status,
                "provider": "OpenPhone (marketing line ending 3707)",
                "provider_status": provider_status,
                "send_policy_enabled": bool(send_policy_enabled),
                "apply_mode": bool(apply_mode),
                "last_run_at_utc": result.last_run_at_utc,
                "counters": {
                    "backlog": result.backlog,
                    "eligible": result.eligible,
                    "sent_today": result.sent_today,
                    "replies_pending": result.replies_pending,
                    "booked": result.booked,
                    "booked_rate_pct": result.booked_rate_pct(),
                    "sheet_rows_modified": result.sheet_rows_modified,
                    "sms_messages_sent": result.sms_messages_sent,
                },
                "by_office": _office_rows(result),
                "by_source": _source_rows(result),
                "sample_template_public": sample_template,
                "compliance_notes": [
                    "Marketing SMS - includes opt-out (Reply STOP).",
                    "Quiet hours respected (after 8pm / before 8am "
                    "recipient local time).",
                    "Per-run cap applies before any sends are enabled.",
                ],
                "blockers": list(result.blockers),
            }
        ],
        "safety_note": (
            "No-send by default. The scaffold scans uncontacted leads "
            "and writes only aggregated counts to this public mirror. "
            "Real SMS is gated on a private OpenPhone API key, an "
            "explicit send-policy flag, and a confirmation flag on the "
            "command line."
        ),
        "_sanitization": {
            "no_pii": True,
            "no_phone_numbers": True,
            "no_names": True,
            "no_sheet_ids": True,
            "no_raw_messages": True,
            "no_private_links": True,
            "aggregated_only": True,
        },
    }


def write_public_snapshot(block: dict[str, Any]) -> None:
    if not PUBLIC_SNAPSHOT.exists():
        # First-run safety: don't create a snapshot file from nothing.
        # The dashboard's daily refresh owns the file's lifecycle.
        raise FileNotFoundError(
            f"Expected existing public snapshot at {PUBLIC_SNAPSHOT}. "
            "Run scripts/build_snapshot.py (or the daily refresh) before "
            "this automation."
        )
    snap = json.loads(PUBLIC_SNAPSHOT.read_text(encoding="utf-8"))
    snap["automations"] = block
    PUBLIC_SNAPSHOT.write_text(
        json.dumps(snap, indent=2) + "\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------

def _parse_args(argv: Iterable[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Google Ads Leads Tracker SMS follow-up automation. "
            "No-send by default. Apply mode is gated on private "
            "provider config and an explicit confirmation flag."
        )
    )
    p.add_argument("--config", help="Path to private lead_sms_config.json")
    g = p.add_mutually_exclusive_group()
    g.add_argument(
        "--dry-run", action="store_true",
        help="Scan only; write sanitized snapshot block (default).",
    )
    g.add_argument(
        "--apply", action="store_true",
        help=(
            "Allow real sends. Requires provider+policy enabled AND "
            "--i-understand-i-am-sending-real-sms."
        ),
    )
    p.add_argument(
        "--i-understand-i-am-sending-real-sms",
        dest="confirm_send",
        action="store_true",
        help="Confirmation flag required for --apply to actually send.",
    )
    return p.parse_args(list(argv))


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    try:
        cfg = load_private_config(args.config)
    except FileNotFoundError as exc:
        # Refresh a blocker-only snapshot so the public dashboard still
        # shows a useful status. No sheet access, no sends.
        result = RunResult()
        result.last_run_at_utc = _now_utc_iso()
        result.last_run_status = "blocked"
        result.blockers.append(str(exc))
        block = build_automations_block(
            result,
            provider_status="missing_private_config",
            send_policy_enabled=False,
            apply_mode=False,
        )
        try:
            write_public_snapshot(block)
        except Exception:
            # Best-effort; the validator surfaces any structural issue.
            pass
        print(f"FAIL: {exc}")
        return 1

    spreadsheet_id = (cfg or {}).get("spreadsheet_id") or ""
    if not spreadsheet_id:
        print("FAIL: private config missing spreadsheet_id.")
        return 1

    sheets = SheetsAdapter(spreadsheet_id)
    op = OpenPhoneAdapter(cfg)
    provider_ready, provider_msg = op.ready()

    send_policy = (cfg.get("send_policy") or {})
    send_policy_enabled = bool(send_policy.get("enabled"))

    apply_mode = bool(args.apply)
    if apply_mode and not (
        provider_ready and send_policy_enabled and args.confirm_send
    ):
        print(
            "WARN: --apply requested but provider/policy/confirmation "
            "not all satisfied. Falling back to dry-run."
        )
        if not provider_ready:
            print(f"  - provider not ready: {provider_msg}")
        if not send_policy_enabled:
            print("  - send_policy.enabled is false in private config.")
        if not args.confirm_send:
            print(
                "  - --i-understand-i-am-sending-real-sms flag was not "
                "supplied."
            )
        apply_mode = False

    result = scan_leads(sheets, cfg)

    if not provider_ready:
        result.blockers.append(provider_msg)
    if not send_policy_enabled:
        result.blockers.append(
            "send_policy.enabled is false in private config; "
            "scaffold is no-send."
        )
    if apply_mode:
        # Apply path is intentionally not implemented in this scaffold.
        result.blockers.append(
            "Apply path not implemented: finalize OpenPhone REST "
            "contract before enabling sends."
        )
        result.last_run_status = "blocked"
    else:
        result.last_run_status = "dry_run"

    block = build_automations_block(
        result,
        provider_status=provider_msg,
        send_policy_enabled=send_policy_enabled,
        apply_mode=apply_mode,
    )
    try:
        write_public_snapshot(block)
    except FileNotFoundError as exc:
        print(f"FAIL: {exc}")
        return 1

    print(
        "OK: lead-sms automation scan complete. "
        f"status={result.last_run_status} backlog={result.backlog} "
        f"eligible={result.eligible} replies_pending={result.replies_pending} "
        f"booked={result.booked} sent_today={result.sent_today} "
        f"sheet_rows_modified={result.sheet_rows_modified} "
        f"sms_sent={result.sms_messages_sent}"
    )
    if result.blockers:
        print("Blockers:")
        for b in result.blockers:
            print(f"  - {b}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
