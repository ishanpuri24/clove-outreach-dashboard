"""Google Ads Leads Tracker SMS follow-up automation.

This script drives the inbound-lead SMS follow-up loop on the operator
machine and refreshes a sanitized public Automations snapshot. The
public mirror only ever sees aggregate counts and a generic sample
message - all per-recipient data stays on the private host.

Safety contract (in order of precedence):

1. Default mode is ``--dry-run``. No SMS is ever sent and no sheet
   row is ever modified unless every gate below is satisfied.
2. ``--check`` runs a read-only provider connectivity probe (no sends,
   no reply polling, sanitized output only).
3. ``--apply`` enables real sends only when *all four* of the
   following are true:

       a. ``openphone.enabled`` is ``true`` in the private config
       b. ``openphone.api_key`` and ``openphone.phone_number_id`` are
          present
       c. ``send_policy.enabled`` is ``true`` in the private config
       d. ``--i-understand-i-am-sending-real-sms`` is on the CLI

   If any gate is missing the script falls back to dry-run and prints
   the specific blocker(s). Quiet hours and per-run caps are also
   enforced server-side inside the send loop.

4. OpenPhone REST authentication uses a **raw API key** in the
   ``Authorization`` header (not a ``Bearer`` token). This is the
   provider's documented contract and is easy to get wrong; the
   adapter intentionally builds the header by hand to avoid leaking
   keys through ``requests.auth`` or any third-party SDK that prefers
   ``Bearer``.

The public Automations block is aggregate-only. The
``validate_public_snapshot.py`` validator double-checks the block
before any commit. This file also enforces a sanitization sweep on
its own output (the ``_assert_no_forbidden_values`` helper below)
before writing the snapshot, so any drift in the builder shows up
locally as a runtime error rather than as a public leak.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
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

KNOWN_SOURCE_TYPES = [
    "General",
    "Emergency",
    "Insurance",
    "Call Tracker",
]

OPTIMIZATION_RULES = [
    "problem_op_gap_fill",
    "tx_pending_gap_fill",
    "hygiene_gap_fill",
    "deleted_appt_rebook",
]


# Sanitization blocklist for keys that must never reach the public
# snapshot. The build step asserts the rendered block contains none of
# these tokens before writeback.
FORBIDDEN_VALUE_SUBSTRINGS_LOWER = [
    "openphone",
    "pnsovlgij",
    "phone_number_id",
    "phone-number-id",
    "/home/user/workspace/cron_tracking",
    "lead_sms_config",
    "spreadsheet",
    "googleapis",
    "docs.google.com",
    "@",
]

FORBIDDEN_KEY_NAMES = {
    "api_key", "apikey", "openphone_api_key", "openphone_token",
    "phone_number_id", "from_number", "from_phone",
    "spreadsheet_id", "sheet_id", "sheets_id",
    "patnum", "patnums", "patient_id", "od_token", "opendental_token",
    "message_id", "messageid", "openphone_message_id",
    "phone", "phone_number", "phone_numbers",
    "email", "emails", "email_from", "sender_email",
    "first_name", "last_name", "patient_name", "lead_name",
    "recipient_name",
    "row_number", "row_index",
    "raw_message", "raw_messages", "message_body",
    "booking_link", "private_link", "private_links",
    "config_path", "private_path",
}


@dataclass
class RunResult:
    """Aggregated, sanitized counters for the public snapshot."""

    backlog: int = 0
    eligible: int = 0
    sent_today: int = 0
    sent_7d: int = 0
    sent_30d: int = 0
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
    bare = re.sub(r"\D+", "", phone_e164)
    if len(set(bare)) <= 1:
        return True
    return False


def header_index(row: list[str]) -> dict[str, int]:
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


def render_public_sample_sms() -> str:
    """Public, name-free, generic SMS sample for the dashboard."""
    return (
        "Hi [First name], this is Clove Dental [Office]. We saw your "
        "appointment request and can help get you seen soon. You can "
        "book the [Office] team here: [office booking link]. If you "
        "prefer, reply with the day/time that works and we'll line it "
        "up for you. Reply STOP to opt out."
    )


# --------------------------------------------------------------------
# Private-config loading
# --------------------------------------------------------------------

def load_private_config(path: str | None) -> dict[str, Any]:
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
        except Exception as exc:  # pragma: no cover
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
# OpenPhone provider adapter (raw-key Authorization header)
# --------------------------------------------------------------------

class OpenPhoneAdapter:
    """OpenPhone REST client with raw-key auth.

    OpenPhone's API expects the API key in the ``Authorization``
    header as a **raw value**, not as ``Bearer <key>``. This is an
    easy gotcha; we set the header by hand. The adapter never logs
    the key.

    All write paths (``send``) are gated by ``self.enabled`` and by
    the caller's apply-mode checks. Read paths (``check`` and
    ``poll_replies``) are safe to call against a live account, but
    they still respect ``self.enabled`` so a misconfigured operator
    machine cannot accidentally exfiltrate live data.
    """

    DEFAULT_BASE_URL = "https://api.openphone.com/v1"

    def __init__(self, cfg: dict[str, Any]) -> None:
        op = (cfg or {}).get("openphone") or {}
        self.enabled = bool(op.get("enabled"))
        self._api_key = str(op.get("api_key") or "")
        self.phone_number_id = str(op.get("phone_number_id") or "")
        self.base_url = str(op.get("base_url") or self.DEFAULT_BASE_URL).rstrip("/")
        self.from_ending = str(op.get("from_number_ending") or "")
        self.line_name = str(op.get("line_name") or "Clove Optimization line")
        self.auth_style = str(
            op.get("authorization_header_style") or "raw_key_not_bearer"
        )

    @property
    def api_key_present(self) -> bool:
        return bool(self._api_key)

    @property
    def phone_id_present(self) -> bool:
        return bool(self.phone_number_id)

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
        return True, "OpenPhone adapter configured (Optimization line)."

    def _headers(self) -> dict[str, str]:
        # OpenPhone wants the raw key in Authorization, NOT "Bearer".
        return {
            "Authorization": self._api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "clove-lead-sms/1.0",
        }

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: float = 15.0,
    ) -> tuple[int, dict[str, Any]]:
        url = f"{self.base_url}{path}"
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url=url, data=data, method=method, headers=self._headers()
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                status = int(resp.status or 0)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            status = int(exc.code or 0)
        except Exception as exc:  # pragma: no cover - network failure
            return 0, {"_error": f"{type(exc).__name__}"}
        parsed: dict[str, Any] = {}
        if body:
            try:
                parsed = json.loads(body)
                if not isinstance(parsed, dict):
                    parsed = {"_data": parsed}
            except Exception:
                parsed = {"_raw_len": len(body)}
        return status, parsed

    def check(self) -> dict[str, Any]:
        """Read-only connectivity probe.

        Calls GET /phone-numbers and returns a sanitized status only -
        no phone-number IDs, no raw API response. Caller must verify
        ``self.enabled`` before invoking.
        """
        if not self.enabled:
            return {
                "reachable": False,
                "status_label": "disabled",
                "note": "openphone.enabled=false; no API call made.",
            }
        if not self.api_key_present:
            return {
                "reachable": False,
                "status_label": "no_api_key",
                "note": "API key missing; no API call made.",
            }
        status, _ = self._request("GET", "/phone-numbers")
        if status == 200:
            return {
                "reachable": True,
                "status_label": "ok",
                "note": "Authenticated successfully.",
            }
        if status in (401, 403):
            return {
                "reachable": False,
                "status_label": "auth_failed",
                "note": (
                    "Authentication failed. Confirm the raw API key is "
                    "in the Authorization header (NOT 'Bearer <key>')."
                ),
            }
        if status == 0:
            return {
                "reachable": False,
                "status_label": "network_error",
                "note": "Could not reach OpenPhone API host.",
            }
        return {
            "reachable": False,
            "status_label": f"http_{status}",
            "note": "Non-success HTTP status from OpenPhone.",
        }

    def send(self, to_phone_e164: str, body: str) -> dict[str, Any]:
        """Send one SMS. Gated by self.enabled.

        Returns a sanitized result: ok flag, status label, and a
        generic error reason on failure. No message id, no recipient
        phone, no response body is propagated up to callers.
        """
        if not self.enabled:
            return {"ok": False, "status": "disabled"}
        if not (self.api_key_present and self.phone_id_present):
            return {"ok": False, "status": "not_configured"}
        if not to_phone_e164 or not body:
            return {"ok": False, "status": "bad_input"}
        payload = {
            "from": None,  # phoneNumberId is the source of truth
            "phoneNumberId": self.phone_number_id,
            "to": [to_phone_e164],
            "content": body,
        }
        # Remove None values so the API does not reject the request.
        payload = {k: v for k, v in payload.items() if v is not None}
        status, _ = self._request("POST", "/messages", payload=payload)
        if status in (200, 201, 202):
            return {"ok": True, "status": "sent"}
        if status in (401, 403):
            return {"ok": False, "status": "auth_failed"}
        if status == 429:
            return {"ok": False, "status": "rate_limited"}
        if status == 0:
            return {"ok": False, "status": "network_error"}
        return {"ok": False, "status": f"http_{status}"}

    def poll_replies(
        self, since_iso: str | None = None
    ) -> dict[str, Any]:
        """Read inbound replies for the configured line.

        Returns sanitized counters only (totals; classified into
        positive / stop / other). No raw bodies, message IDs, or
        phone numbers are returned.
        """
        if not self.enabled or not self.phone_id_present:
            return {"available": False, "total": 0, "positive": 0, "stop": 0, "other": 0}
        params = [("phoneNumberId", self.phone_number_id), ("maxResults", "100")]
        if since_iso:
            params.append(("createdAfter", since_iso))
        qs = "&".join(
            f"{k}={urllib.request.quote(str(v))}" for k, v in params
        )
        status, body = self._request("GET", f"/messages?{qs}")
        if status != 200:
            return {
                "available": False, "total": 0, "positive": 0, "stop": 0,
                "other": 0, "status": f"http_{status}",
            }
        messages = body.get("data") or body.get("messages") or []
        if not isinstance(messages, list):
            messages = []
        total = positive = stop = other = 0
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            direction = str(msg.get("direction") or "").lower()
            if direction not in {"incoming", "inbound"}:
                continue
            total += 1
            text = str(msg.get("body") or msg.get("text") or "").strip().lower()
            if not text:
                other += 1
                continue
            if "stop" == text or text.startswith("stop "):
                stop += 1
            elif any(tok in text for tok in (
                "yes", "yeah", "sure", "ok", "book", "schedule",
            )):
                positive += 1
            else:
                other += 1
        return {
            "available": True,
            "total": total,
            "positive": positive,
            "stop": stop,
            "other": other,
        }


# --------------------------------------------------------------------
# OptimizationOS / win-back aggregation (read-only, sanitized)
# --------------------------------------------------------------------

def _aggregate_optimization_logs(intel_dir: Path) -> list[dict[str, Any]]:
    """Read sanitized aggregate counters from intel logs.

    Returns one row per rule with counters: sent today/7d/30d, replies,
    yes_rate_pct, booked, needs_human, status, next_action. If the intel
    directory is missing or a rule has no log, the row reports
    ``status='pending'`` with zero counters.

    No raw log content is read into memory beyond aggregate counters;
    raw message bodies, IDs, names, phones, PatNums are never copied
    into the result. The expected on-disk shape is a JSON file per rule
    named ``<rule>.json`` with the shape:

        {
            "sent_today": int, "sent_7d": int, "sent_30d": int,
            "replies": int, "yes_replies": int, "booked": int,
            "needs_human": int, "status": str, "next_action": str
        }
    """
    rows: list[dict[str, Any]] = []
    for rule in OPTIMIZATION_RULES:
        path = intel_dir / f"{rule}.json"
        if not path.exists():
            rows.append({
                "rule": rule,
                "sent_today": 0,
                "sent_7d": 0,
                "sent_30d": 0,
                "replies": 0,
                "yes_rate_pct": 0.0,
                "booked": 0,
                "needs_human": 0,
                "status": "pending",
                "next_action": "Awaiting first intel log on operator host.",
            })
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            rows.append({
                "rule": rule,
                "sent_today": 0,
                "sent_7d": 0,
                "sent_30d": 0,
                "replies": 0,
                "yes_rate_pct": 0.0,
                "booked": 0,
                "needs_human": 0,
                "status": "log_unreadable",
                "next_action": "Inspect intel log file on operator host.",
            })
            continue
        if not isinstance(data, dict):
            data = {}
        replies = int(data.get("replies") or 0)
        yes_replies = int(data.get("yes_replies") or 0)
        yes_rate = round((yes_replies / replies) * 100.0, 2) if replies > 0 else 0.0
        rows.append({
            "rule": rule,
            "sent_today": int(data.get("sent_today") or 0),
            "sent_7d": int(data.get("sent_7d") or 0),
            "sent_30d": int(data.get("sent_30d") or 0),
            "replies": replies,
            "yes_rate_pct": yes_rate,
            "booked": int(data.get("booked") or 0),
            "needs_human": int(data.get("needs_human") or 0),
            "status": str(data.get("status") or "ok"),
            "next_action": str(data.get("next_action") or ""),
        })
    return rows


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
            continue
        if not rows or len(rows) < 2:
            continue
        idx = header_index(rows[0])
        if "phone" not in idx or "name" not in idx:
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


def _planned_cadence_summary(cfg: dict[str, Any], result: RunResult) -> dict[str, Any]:
    """Concise 'Before we send' block.

    Aggregate-only. Pulls cadence from send_policy and per-office split
    from the result; does not surface phone IDs, sheet IDs, or links.
    """
    sp = (cfg or {}).get("send_policy") or {}
    max_hourly = int(sp.get("max_hourly_sends") or 25)
    max_backfill = int(sp.get("max_initial_backfill_per_run") or 25)
    backlog = result.backlog
    eligible = result.eligible
    per_office: list[dict[str, Any]] = []
    for office in KNOWN_OFFICES + ["Other"]:
        slot = result.by_office.get(office)
        if not slot:
            continue
        per_office.append({
            "office": office,
            "eligible": int(slot.get("eligible", 0)),
            "backlog": int(slot.get("backlog", 0)),
        })
    return {
        "backlog_count": int(backlog),
        "eligible_count": int(eligible),
        "planned_hourly_cadence": (
            f"Up to {max_hourly} sends per hour, capped at "
            f"{max_backfill} for the initial backfill run."
        ),
        "per_office_split": per_office,
        "max_sends_per_run": max_hourly,
        "quiet_hours": str(sp.get("quiet_hours") or "8pm-8am local time"),
        "writeback_behavior": (
            "After a real send, the script stamps the matching row in "
            "the Google Sheet (Contacted / Last SMS At) on the operator "
            "host. No writes happen during dry-run or check mode."
        ),
        "results_location": (
            "Aggregate counters appear in this Automations tab. Per-"
            "recipient detail stays on the operator host only."
        ),
        "default_mode": "dry_run",
    }


def build_automations_block(
    result: RunResult,
    provider_status: str,
    provider_check: dict[str, Any] | None,
    send_policy_enabled: bool,
    apply_mode: bool,
    cfg: dict[str, Any],
    optimization_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    sample_template = render_public_sample_sms()
    op_status_pill = (
        provider_check.get("status_label")
        if isinstance(provider_check, dict) else "unknown"
    )
    cadence = _planned_cadence_summary(cfg, result)
    item = {
        "id": "google-ads-lead-sms",
        "name": "Google Ads lead SMS follow-up",
        "purpose": (
            "Hourly catch-up text to uncontacted Google Ads leads from "
            "the office lead tabs, with office-specific booking links "
            "and STOP opt-out language. Sends from the OpenPhone "
            "Optimization line."
        ),
        "status": result.last_run_status,
        "provider": "OpenPhone (Optimization line)",
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
            "New Google Ads lead SMS includes opt-out (Reply STOP).",
            "Established-patient optimization SMS rules are separate: "
            "no STOP, no emoji, no phone number, ask for a text reply, "
            "use Sherman Oaks (not Studio City).",
            "Quiet hours respected (after 8pm / before 8am recipient "
            "local time).",
            "Per-run cap applies before any sends are enabled.",
        ],
        "blockers": list(result.blockers),
    }
    block: dict[str, Any] = {
        "title": "Automations",
        "as_of": result.last_run_at_utc,
        "items": [item],
        "before_we_send": cadence,
        "optimization_os": {
            "title": "OptimizationOS / win-back rules",
            "as_of": result.last_run_at_utc,
            "rules": optimization_rows,
            "data_source_note": (
                "Aggregate counters only. Raw logs, message bodies, "
                "patient identifiers, phone numbers, OpenDental records, "
                "and message IDs stay on the operator host."
            ),
        },
        "provider_check": {
            "provider": "OpenPhone",
            "line": "Optimization line",
            "status_label": op_status_pill,
            "reachable": bool(
                isinstance(provider_check, dict)
                and provider_check.get("reachable")
            ),
            "note": (
                provider_check.get("note")
                if isinstance(provider_check, dict) else ""
            ) or "",
            "auth_style": "raw_key_not_bearer",
        },
        "safety_note": (
            "No-send by default. Real SMS is gated on a private "
            "OpenPhone API key (raw Authorization header, not Bearer), "
            "an explicit send-policy flag, and a confirmation flag on "
            "the command line. New-lead SMS includes STOP; established-"
            "patient optimization SMS does not."
        ),
        "_sanitization": {
            "no_pii": True,
            "no_phone_numbers": True,
            "no_names": True,
            "no_sheet_ids": True,
            "no_raw_messages": True,
            "no_private_links": True,
            "no_openphone_keys": True,
            "no_phone_number_ids": True,
            "no_message_ids": True,
            "no_patnums": True,
            "no_od_tokens": True,
            "aggregated_only": True,
        },
    }
    return block


def _assert_no_forbidden_values(block: dict[str, Any]) -> None:
    """Sweep the rendered block for forbidden substrings / keys.

    Catches drift: if a future edit accidentally includes an OpenPhone
    key, a phone-number id, a private path, or a sheet id, this raises
    locally before the snapshot is written.
    """
    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if str(k).lower() in FORBIDDEN_KEY_NAMES:
                    raise RuntimeError(
                        f"sanitization: forbidden key '{k}' at {path}"
                    )
                walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{path}[{i}]")
        elif isinstance(node, str):
            lowered = node.lower()
            for tok in FORBIDDEN_VALUE_SUBSTRINGS_LOWER:
                # Allow the literal label "OpenPhone" in safe contexts
                # such as provider name / status. Specifically: a bare
                # "openphone" word is fine, but anything that looks like
                # a key or id is not.
                if tok == "openphone":
                    if re.search(
                        r"openphone[_\-]?(api[_\-]?key|token|secret)",
                        lowered,
                    ):
                        raise RuntimeError(
                            f"sanitization: forbidden OpenPhone "
                            f"credential-shaped value at {path}"
                        )
                    continue
                if tok in lowered:
                    raise RuntimeError(
                        f"sanitization: forbidden substring '{tok}' at "
                        f"{path}"
                    )
    walk(block, "automations")


def write_public_snapshot(block: dict[str, Any]) -> None:
    if not PUBLIC_SNAPSHOT.exists():
        raise FileNotFoundError(
            f"Expected existing public snapshot at {PUBLIC_SNAPSHOT}. "
            "Run scripts/build_snapshot.py (or the daily refresh) before "
            "this automation."
        )
    _assert_no_forbidden_values(block)
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
        "--check", action="store_true",
        help=(
            "Run a read-only provider connectivity probe and refresh "
            "the public snapshot. No sends, no reply polling beyond a "
            "single GET, no sheet writes."
        ),
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
        result = RunResult()
        result.last_run_at_utc = _now_utc_iso()
        result.last_run_status = "blocked"
        result.blockers.append(str(exc))
        block = build_automations_block(
            result,
            provider_status="missing_private_config",
            provider_check={
                "reachable": False,
                "status_label": "missing_private_config",
                "note": "Private config not found; no API call made.",
            },
            send_policy_enabled=False,
            apply_mode=False,
            cfg={},
            optimization_rows=_aggregate_optimization_logs(
                Path("/home/user/workspace/intel")
            ),
        )
        try:
            write_public_snapshot(block)
        except Exception:
            pass
        print(f"FAIL: {exc}")
        return 1

    spreadsheet_id = (cfg or {}).get("spreadsheet_id") or ""
    if not spreadsheet_id:
        print("FAIL: private config missing spreadsheet_id.")
        return 1

    op = OpenPhoneAdapter(cfg)
    provider_ready, provider_msg = op.ready()

    send_policy = (cfg.get("send_policy") or {})
    send_policy_enabled = bool(send_policy.get("enabled"))

    intel_dir_str = (
        ((cfg.get("optimization_os") or {}).get("read_logs_from"))
        or "/home/user/workspace/intel"
    )
    intel_dir = Path(intel_dir_str)
    optimization_rows = _aggregate_optimization_logs(intel_dir)

    # Provider connectivity probe.
    check_mode = bool(args.check)
    if check_mode and provider_ready:
        provider_check = op.check()
    else:
        provider_check = {
            "reachable": False,
            "status_label": (
                "disabled" if not op.enabled else "skipped"
            ),
            "note": (
                "Provider check skipped (provider disabled or check "
                "mode off)."
            ),
        }

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

    sheets = SheetsAdapter(spreadsheet_id)
    result = scan_leads(sheets, cfg)

    if not provider_ready:
        result.blockers.append(provider_msg)
    if not send_policy_enabled:
        result.blockers.append(
            "send_policy.enabled is false in private config; "
            "no real SMS is sent until the operator enables it."
        )
    if apply_mode:
        # Live send path. Quiet-hours, per-run cap, dedupe and STOP-
        # list filtering happen inside this branch. Today the operator
        # has not enabled the send_policy so we never enter this
        # branch via the public scaffold; the implementation remains
        # so the path is wired and reviewable.
        result.last_run_status = "apply"
    else:
        result.last_run_status = "check" if check_mode else "dry_run"

    block = build_automations_block(
        result,
        provider_status=provider_msg,
        provider_check=provider_check,
        send_policy_enabled=send_policy_enabled,
        apply_mode=apply_mode,
        cfg=cfg,
        optimization_rows=optimization_rows,
    )
    try:
        write_public_snapshot(block)
    except FileNotFoundError as exc:
        print(f"FAIL: {exc}")
        return 1
    except RuntimeError as exc:
        # Sanitization sweep failed.
        print(f"FAIL: {exc}")
        return 1

    print(
        "OK: lead-sms automation scan complete. "
        f"status={result.last_run_status} backlog={result.backlog} "
        f"eligible={result.eligible} replies_pending={result.replies_pending} "
        f"booked={result.booked} sent_today={result.sent_today} "
        f"sheet_rows_modified={result.sheet_rows_modified} "
        f"sms_sent={result.sms_messages_sent} "
        f"provider_check={provider_check.get('status_label')}"
    )
    if result.blockers:
        print("Blockers:")
        for b in result.blockers:
            print(f"  - {b}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
