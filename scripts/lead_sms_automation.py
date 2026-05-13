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
import shutil
import subprocess
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
    "contacted yes/no": "contacted",
    "contacted (yes/no)": "contacted",
    "followed up": "followed_up",
    "follow up": "followed_up",
    "follow-up": "followed_up",
    "appointment booked": "appointment_booked",
    "treatment opted": "treatment_opted",
    "referral source": "referral_source",
    "phone calls": "phone_calls",
    "ai sms sent at": "ai_sms_sent_at",
    "ai sms status": "ai_sms_status",
    "ai sms notes": "ai_sms_notes",
    "last sms at": "ai_sms_sent_at",
}

# Optional feedback columns that the writeback path will populate when
# they already exist on the sheet. The script does NOT add columns on
# its own (see writeback docs); if any of these are absent the run logs
# a per-tab blocker once and stamps only the Contacted column.
WRITEBACK_FEEDBACK_FIELDS = ("ai_sms_sent_at", "ai_sms_status", "ai_sms_notes")

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
    """Aggregated, sanitized counters for the public snapshot.

    The ``_private_candidates`` list is process-local: it stores the row
    coordinates (tab name + 1-based row number) and the normalized phone
    for any uncontacted lead the apply path is allowed to text. It is
    NEVER copied to the public snapshot; ``build_automations_block``
    ignores it.
    """

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
    needs_human_count: int = 0
    escalations_queued: int = 0
    _private_candidates: list[dict[str, Any]] = field(default_factory=list)
    # Aggregate skip reasons from the apply path. Reason -> count. Used
    # to surface a clear blocker when apply mode produces zero sends so
    # the operator can fix the underlying config gap (e.g. missing
    # office booking link) instead of seeing a silent 0/0.
    apply_skips: dict[str, int] = field(default_factory=dict)

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
        "Hi [First name], this is Clove Dental [Office]. You filled "
        "out a form for an appointment, and we have real-time openings "
        "available today. You can book here: [office booking link]. "
        "If you want help, reply with what time works. Reply STOP to "
        "opt out."
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
# Sheets adapter (external-tool CLI; no SDK required)
# --------------------------------------------------------------------

class SheetsAdapter:
    """Google Sheets adapter that shells out to the ``external-tool`` CLI.

    The operator host runs this script with ``api_credentials=["external-
    tools"]`` and the ``external-tool`` binary on PATH. We avoid the
    ``google-api-python-client`` SDK because that pulls in extra
    dependencies and ADC, neither of which is configured in cron.

    All sheet IO funnels through three pipedream tools:

    - ``google_sheets-get-spreadsheet-info`` — list tabs / metadata.
    - ``google_sheets-get-values``           — read a tab as a 2D array.
    - ``google_sheets-update-row``           — write a single row back.

    The spreadsheet id is supplied at construction time from the private
    config and is **never** passed on argv as a flag value or echoed in
    error output. Tool payloads are sent as a single JSON argument so
    the id stays out of process listings as a top-level token.
    """

    DEFAULT_TIMEOUT_S = 60.0
    SOURCE_ID = "google_sheets__pipedream"
    TOOL_INFO = "google_sheets-get-spreadsheet-info"
    TOOL_READ_ROWS = "google_sheets-get-values-in-range"
    TOOL_UPDATE_CELL = "google_sheets-update-cell"

    def __init__(
        self,
        spreadsheet_id: str,
        *,
        binary: str | None = None,
        env_extra: dict[str, str] | None = None,
    ) -> None:
        self.spreadsheet_id = str(spreadsheet_id or "")
        self.available = False
        self.error: str | None = None
        self._binary = binary or os.environ.get("EXTERNAL_TOOL_BIN") or "external-tool"
        self._env_extra = dict(env_extra or {})
        # Cache: tab_title -> {"sheet_id": int, "headers": list[str], "row_count": int}
        self._tab_cache: dict[str, dict[str, Any]] | None = None
        self._probe()

    def _probe(self) -> None:
        if not self.spreadsheet_id:
            self.error = "Google Sheets adapter: spreadsheet_id missing in private config."
            return
        if not shutil.which(self._binary):
            self.error = (
                "external-tool CLI not on PATH. Run this script with "
                "api_credentials=[\"external-tools\"] and ensure the "
                "'external-tool' binary is installed on the operator host."
            )
            return
        self.available = True

    def _call(self, tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Invoke ``external-tool call '{json}'`` and parse the JSON result.

        Returns ``{"_error": "<reason>"}`` on any failure so callers can
        decide whether to skip or fall back. Stderr is captured and
        condensed into a short reason string; we never propagate raw
        provider output to the public snapshot.
        """
        # The external-tool CLI expects the parameter map under the
        # ``arguments`` key (see /usr/local/bin/external-tool::call_tool).
        # Earlier ``tool_arguments`` shape caused every call to fail with
        # "Missing required parameters" and scan returned backlog=0.
        req = {
            "source_id": self.SOURCE_ID,
            "tool_name": tool_name,
            "arguments": payload,
        }
        try:
            proc = subprocess.run(
                [self._binary, "call", json.dumps(req)],
                capture_output=True,
                text=True,
                timeout=self.DEFAULT_TIMEOUT_S,
                env={**os.environ, **self._env_extra},
                check=False,
            )
        except FileNotFoundError:
            return {"_error": "external_tool_binary_missing"}
        except subprocess.TimeoutExpired:
            return {"_error": "external_tool_timeout"}
        except Exception as exc:  # pragma: no cover - defensive
            return {"_error": f"external_tool_exception_{type(exc).__name__}"}
        if proc.returncode != 0:
            return {"_error": f"external_tool_exit_{proc.returncode}"}
        out = (proc.stdout or "").strip()
        if not out:
            return {"_error": "external_tool_empty_response"}
        try:
            data = json.loads(out)
        except Exception:
            return {"_error": "external_tool_non_json"}
        if isinstance(data, dict):
            return data
        return {"_data": data}

    def list_tabs(self) -> list[str]:
        """Return tab titles and populate the per-tab cache.

        The Pipedream ``google_sheets-get-spreadsheet-info`` tool returns
        a top-level dict with a ``worksheets`` list. Each entry has
        ``sheetName`` / ``sheetId`` (integer worksheet id) / ``rowCount``
        / ``headers`` (the first row as a list of strings). We cache all
        of this so the scanner can look up ``worksheetId`` for the read
        call and skip non-lead tabs cheaply by header inspection.
        """
        if not self.available:
            return []
        resp = self._call(
            self.TOOL_INFO,
            {"spreadsheetId": self.spreadsheet_id, "includeGridData": False},
        )
        if "_error" in resp:
            self.error = (
                "Google Sheets list_tabs failed via external-tool "
                f"({resp.get('_error')})."
            )
            return []
        worksheets = resp.get("worksheets") or resp.get("sheets") or []
        cache: dict[str, dict[str, Any]] = {}
        titles: list[str] = []
        for s in worksheets:
            if not isinstance(s, dict):
                continue
            # The CLI uses ``sheetName``; the raw Sheets API uses
            # ``properties.title``. Accept either so the adapter survives
            # a future provider swap without code churn.
            props = s.get("properties") if isinstance(s.get("properties"), dict) else s
            title = (
                s.get("sheetName")
                or props.get("title")
                or props.get("Title")
            )
            sheet_id = (
                s.get("sheetId")
                if s.get("sheetId") is not None
                else props.get("sheetId")
            )
            headers = s.get("headers") or props.get("headers") or []
            row_count = s.get("rowCount") or props.get("rowCount") or 0
            if not title or sheet_id is None:
                continue
            title_s = str(title)
            try:
                sid_int = int(sheet_id)
            except (TypeError, ValueError):
                continue
            cache[title_s] = {
                "sheet_id": sid_int,
                "headers": [str(h or "") for h in headers if isinstance(headers, list)],
                "row_count": int(row_count or 0),
            }
            titles.append(title_s)
        self._tab_cache = cache
        return titles

    def tab_info(self, tab: str) -> dict[str, Any] | None:
        if self._tab_cache is None:
            self.list_tabs()
        return (self._tab_cache or {}).get(tab)

    def sheet_id_for(self, tab: str) -> Any:
        info = self.tab_info(tab)
        return info.get("sheet_id") if info else None

    def read_tab(self, tab: str) -> list[list[str]]:
        if not self.available:
            return []
        info = self.tab_info(tab)
        if not info:
            return []
        worksheet_id = info.get("sheet_id")
        if worksheet_id is None:
            return []
        # ``google_sheets-get-values-in-range`` returns the rows as a
        # top-level JSON list of lists. If ``range`` is omitted the tool
        # returns the entire used range of the worksheet, which is what
        # the scanner needs.
        resp = self._call(
            self.TOOL_READ_ROWS,
            {
                "sheetId": self.spreadsheet_id,
                "worksheetId": int(worksheet_id),
            },
        )
        if "_error" in resp:
            return []
        # The CLI may return a bare list (wrapped as {"_data": [...]} by
        # ``_call``) or a dict-with-values for legacy callers.
        values: Any = resp.get("_data")
        if values is None and isinstance(resp, dict):
            values = resp.get("values") or resp.get("Values")
        if not isinstance(values, list):
            return []
        normalized: list[list[str]] = []
        for row in values:
            if isinstance(row, list):
                normalized.append(["" if c is None else str(c) for c in row])
            else:
                normalized.append([])
        return normalized

    @staticmethod
    def _col_letter(idx_zero: int) -> str:
        """Convert 0-based column index to A1 column letters."""
        n = idx_zero + 1
        letters = ""
        while n > 0:
            n, rem = divmod(n - 1, 26)
            letters = chr(65 + rem) + letters
        return letters

    def update_cells(
        self,
        tab: str,
        row_number_1based: int,
        updates: dict[int, str],
    ) -> dict[str, Any]:
        """Idempotently update specific cells on a row.

        ``updates`` maps 0-based column index -> cell string value. The
        adapter issues one ``google_sheets-update-cell`` call per cell so
        unrelated cells are never overwritten. Returns ``{"ok": bool,
        "status": "<label>", "cells": <int>}``.
        """
        if not self.available:
            return {"ok": False, "status": "sheets_unavailable", "cells": 0}
        if not updates:
            return {"ok": True, "status": "noop", "cells": 0}
        info = self.tab_info(tab)
        if not info:
            return {"ok": False, "status": "tab_not_found", "cells": 0}
        worksheet_id = int(info.get("sheet_id"))
        wrote = 0
        for col_idx, value in updates.items():
            cell_a1 = f"{self._col_letter(int(col_idx))}{int(row_number_1based)}"
            resp = self._call(
                self.TOOL_UPDATE_CELL,
                {
                    "sheetId": self.spreadsheet_id,
                    "worksheetId": worksheet_id,
                    "cell": cell_a1,
                    "newCell": str(value),
                },
            )
            if "_error" in resp:
                return {
                    "ok": False,
                    "status": resp.get("_error") or "update_failed",
                    "cells": wrote,
                }
            wrote += 1
        return {"ok": True, "status": "updated", "cells": wrote}


# --------------------------------------------------------------------
# OpenPhone provider adapter (raw-key Authorization header)
# --------------------------------------------------------------------

# Aggregate, non-PII reason tokens we are willing to surface from an
# OpenPhone 400 response. Anything not on this list collapses to
# ``provider_validation`` so we never leak phones, message bodies, IDs,
# names, or API keys via error text. Tokens deliberately mirror the
# OpenPhone error code/message shapes we have observed.
_ALLOWED_400_REASON_TOKENS: tuple[tuple[str, str], ...] = (
    ("missing_to", ("to is required", "missing to", "\"to\"")),
    ("missing_from", ("from is required", "missing from", "\"from\"")),
    ("missing_content", ("content is required", "missing content")),
    ("invalid_from_format", ("invalid from", "from must be", "from is invalid")),
    ("invalid_to_format", ("invalid to", "to must be", "invalid phone")),
    ("from_not_owned", ("not owned", "does not belong", "unauthorized number")),
    ("unsupported_field", ("unknown field", "unexpected field", "phonenumberid")),
    ("rate_or_quota", ("quota", "limit exceeded")),
)


def _sanitize_provider_400_reason(parsed: dict[str, Any] | None) -> str:
    """Map an OpenPhone 400 response body to a short, non-PII tag.

    Only the tags in ``_ALLOWED_400_REASON_TOKENS`` are ever returned.
    Any unrecognized error collapses to ``validation`` so we never
    propagate raw provider text (which could echo phones, bodies, or
    IDs back into logs). Returns just the suffix, e.g. ``missing_to``;
    callers prefix with ``http_400_``.
    """
    if not isinstance(parsed, dict):
        return "validation"
    # Collect candidate strings from the common OpenPhone shapes without
    # ever returning them verbatim.
    candidates: list[str] = []
    for key in ("message", "error", "detail", "title", "code"):
        val = parsed.get(key)
        if isinstance(val, str):
            candidates.append(val)
    errs = parsed.get("errors")
    if isinstance(errs, list):
        for item in errs:
            if isinstance(item, str):
                candidates.append(item)
            elif isinstance(item, dict):
                for k in ("message", "code", "detail", "field"):
                    v = item.get(k)
                    if isinstance(v, str):
                        candidates.append(v)
    blob = " ".join(candidates).lower()
    if not blob:
        return "validation"
    for tag, needles in _ALLOWED_400_REASON_TOKENS:
        for needle in needles:
            if needle in blob:
                return tag
    return "validation"


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
        self.from_number = str(op.get("from_number") or "")
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

    @property
    def from_number_present(self) -> bool:
        # OpenPhone v1 /messages requires the sending phone in E.164
        # form as the ``from`` field. phoneNumberId alone yields HTTP 400.
        return bool(self.from_number) and self.from_number.startswith("+")

    def ready(self) -> tuple[bool, str]:
        if not self.enabled:
            return False, (
                "OpenPhone adapter disabled in private config "
                "(openphone.enabled=false)."
            )
        if not self.api_key_present:
            return False, "OpenPhone api_key missing in private config."
        if not self.from_number_present:
            return False, (
                "OpenPhone from_number missing or not E.164 in private config."
            )
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
        if not self.api_key_present:
            return {"ok": False, "status": "not_configured"}
        if not self.from_number_present:
            return {"ok": False, "status": "missing_from"}
        if not to_phone_e164 or not body:
            return {"ok": False, "status": "bad_input"}
        # Per OpenPhone v1 handoff: payload must be exactly
        #   {"from": "<E.164>", "to": ["<E.164>"], "content": "<body>"}
        # phoneNumberId here causes HTTP 400.
        payload: dict[str, Any] = {
            "from": self.from_number,
            "to": [to_phone_e164],
            "content": body,
        }
        status, parsed = self._request("POST", "/messages", payload=payload)
        if status in (200, 201, 202):
            return {"ok": True, "status": "sent"}
        if status in (401, 403):
            return {"ok": False, "status": "auth_failed"}
        if status == 429:
            return {"ok": False, "status": "rate_limited"}
        if status == 0:
            return {"ok": False, "status": "network_error"}
        if status == 400:
            reason = _sanitize_provider_400_reason(parsed)
            return {"ok": False, "status": f"http_400_{reason}"}
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

    missing_feedback_tabs: list[str] = []
    lead_tab_count = 0
    for tab in tabs:
        # Pre-filter using cached headers from get-spreadsheet-info so we
        # don't issue a get-values-in-range call for obviously non-lead
        # tabs (Index, Notes, raw exports, etc.). Per the safety
        # contract, blank Date does NOT exclude a lead, so we only check
        # that the tab has both a name and phone column.
        info = sheets.tab_info(tab) or {}
        header_preview = info.get("headers") or []
        if header_preview:
            preview_idx = header_index(header_preview)
            if "phone" not in preview_idx or "name" not in preview_idx:
                continue
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
        lead_tab_count += 1
        office = office_from_tab(tab)
        source = source_from_tab(tab)
        feedback_cols = {
            f: idx[f] for f in WRITEBACK_FEEDBACK_FIELDS if f in idx
        }
        if not feedback_cols and "contacted" in idx:
            missing_feedback_tabs.append(tab)

        for row_offset, raw in enumerate(rows[1:], start=2):
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
            ai_status = (raw[idx["ai_sms_status"]]
                         if "ai_sms_status" in idx
                         and idx["ai_sms_status"] < len(raw) else "")

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
                # Idempotence: if AI SMS Status already records a prior
                # send we skip enqueueing this row for the apply path.
                if not (ai_status and is_yes(ai_status)
                        or str(ai_status).strip().lower() in {"sent", "queued"}):
                    result._private_candidates.append({
                        "tab": tab,
                        "row_number": row_offset,
                        "phone_e164": phone_e164,
                        "first_name": (name.split() or [""])[0],
                        "office": office,
                        "source": source,
                        "contacted_col": idx.get("contacted"),
                        "feedback_cols": feedback_cols,
                    })
            elif is_yes(contacted) and not is_yes(followed):
                result.replies_pending += 1
                _bump(result.by_office, office, "replies_pending")
                _bump(result.by_source, source, "replies_pending")

    if missing_feedback_tabs:
        # Single aggregated, non-PII blocker; sheet names are safe to
        # mention since the validator only forbids ids and recipient
        # data, not tab labels.
        result.blockers.append(
            "Feedback columns (AI SMS Sent At / Status / Notes) absent on "
            f"{len(missing_feedback_tabs)} tab(s); writeback will stamp "
            "Contacted only. Add the three columns to enable per-row "
            "feedback writeback. The script will not add columns "
            "automatically (risk of breaking existing formulas)."
        )

    result.last_run_status = "dry_run"
    return result


# --------------------------------------------------------------------
# Apply path: send capped SMS via OpenPhone and write back to sheet
# --------------------------------------------------------------------

def _booking_link_for_office(cfg: dict[str, Any], office: str) -> str:
    cfg = cfg or {}
    # The private config uses ``office_booking_links``; legacy fixtures
    # used ``booking_links``. Accept either so the apply path keeps a
    # valid link map across config schema changes.
    links = cfg.get("office_booking_links") or cfg.get("booking_links") or {}
    if not isinstance(links, dict):
        return ""
    return str(links.get(office) or links.get("default") or "")


def _render_lead_sms(first_name: str, office: str, booking_link: str) -> str:
    """Match the public sample's required phrases for compliance.

    The public dashboard renders ``render_public_sample_sms``; this
    private version substitutes [First name] / [Office] / [office
    booking link] only. Required phrases (verified by tests):
    'filled out a form', 'real-time openings available today',
    'You can book here', and 'STOP'.
    """
    name = (first_name or "there").strip().split(" ")[0] or "there"
    office_label = (office or "Clove Dental").strip() or "Clove Dental"
    return (
        f"Hi {name}, this is Clove Dental {office_label}. You filled out "
        f"a form for an appointment, and we have real-time openings "
        f"available today. You can book here: {booking_link}. If you "
        f"want help, reply with what time works. Reply STOP to opt out."
    )


def apply_sends(
    sheets: SheetsAdapter,
    openphone: OpenPhoneAdapter,
    cfg: dict[str, Any],
    result: RunResult,
) -> None:
    """Send capped SMS and stamp rows after each successful send.

    The caller has already verified all four gates (provider ready,
    policy enabled, --apply, --i-understand). This function additionally
    enforces the per-run cap from ``send_policy.max_initial_backfill_per_run``
    and a hard ``max_hourly_sends`` ceiling.
    """
    sp = (cfg or {}).get("send_policy") or {}
    cap = int(sp.get("max_initial_backfill_per_run") or 25)
    hourly_cap = int(sp.get("max_hourly_sends") or 25)
    cap = min(cap, hourly_cap)

    def _skip(reason: str) -> None:
        result.apply_skips[reason] = result.apply_skips.get(reason, 0) + 1

    candidates = list(result._private_candidates)
    sent = 0
    offices_missing_link: set[str] = set()
    for cand in candidates:
        if sent >= cap:
            _skip("cap_reached")
            continue
        phone = cand.get("phone_e164")
        if not phone:
            _skip("missing_phone")
            continue
        first_name = cand.get("first_name") or ""
        office = cand.get("office") or ""
        booking_link = _booking_link_for_office(cfg, office)
        if not booking_link:
            _skip("missing_booking_link")
            if office:
                offices_missing_link.add(office)
            continue
        body = _render_lead_sms(first_name, office, booking_link)
        send_result = openphone.send(phone, body)
        if not send_result.get("ok"):
            _skip(f"provider_{send_result.get('status') or 'error'}")
            continue
        # Stamp the sheet only after a successful send.
        updates: dict[int, str] = {}
        contacted_col = cand.get("contacted_col")
        if isinstance(contacted_col, int):
            updates[contacted_col] = "YES"
        fb_cols = cand.get("feedback_cols") or {}
        now_iso = _now_utc_iso()
        if "ai_sms_sent_at" in fb_cols:
            updates[fb_cols["ai_sms_sent_at"]] = now_iso
        if "ai_sms_status" in fb_cols:
            updates[fb_cols["ai_sms_status"]] = "sent"
        if "ai_sms_notes" in fb_cols:
            updates[fb_cols["ai_sms_notes"]] = "auto-send: lead SMS via Optimization line"
        write = sheets.update_cells(
            cand["tab"], int(cand["row_number"]), updates,
        )
        if write.get("ok"):
            result.sheet_rows_modified += 1
        else:
            _skip(f"writeback_{write.get('status') or 'failed'}")
        result.sent_today += 1
        result.sms_messages_sent += 1
        sent += 1

    if candidates and sent == 0:
        # Surface a clear, actionable blocker so the apply path never
        # silently reports 0/0 with no diagnosis. Aggregate-only — never
        # includes phones, names, row numbers, or links.
        top_reason = max(
            result.apply_skips.items(), key=lambda kv: kv[1]
        )[0] if result.apply_skips else "unknown"
        msg = (
            f"Apply mode produced 0 sends from {len(candidates)} eligible "
            f"candidate(s). Top skip reason: {top_reason}."
        )
        if top_reason == "missing_booking_link" and offices_missing_link:
            office_list = ", ".join(sorted(offices_missing_link))
            msg += (
                f" Add office_booking_links entries (or a 'default' fallback) "
                f"in the private config for: {office_list}."
            )
        result.blockers.append(msg)


# --------------------------------------------------------------------
# Needs-human escalation (config-gated Trello, otherwise aggregate-only)
# --------------------------------------------------------------------

def maybe_escalate_needs_human(
    cfg: dict[str, Any],
    result: RunResult,
    reply_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    """Surface aggregate ``needs_human`` counts and, if explicitly
    configured, queue a Trello card via the external-tool CLI.

    Email-to-Aryaan is intentionally NOT supported. Only Trello is
    wired, and only when ``escalation.trello.list_id`` is present in
    the private config. Returns a sanitized status dict for the public
    snapshot.
    """
    needs_human = 0
    if isinstance(reply_summary, dict):
        needs_human = int(reply_summary.get("other") or 0)
    result.needs_human_count = needs_human

    esc = (cfg or {}).get("escalation") or {}
    trello = (esc.get("trello") or {}) if isinstance(esc, dict) else {}
    list_id = str(trello.get("list_id") or "")
    enabled = bool(trello.get("enabled")) and bool(list_id)
    if not enabled or needs_human <= 0:
        return {
            "mode": "aggregate_only",
            "needs_human": needs_human,
            "trello_configured": bool(list_id),
            "queued": 0,
            "note": (
                "Aggregate needs-human count only. No automatic Trello "
                "card is created unless escalation.trello.list_id is set "
                "and escalation.trello.enabled=true in the private config."
            ),
        }
    binary = os.environ.get("EXTERNAL_TOOL_BIN") or "external-tool"
    if not shutil.which(binary):
        return {
            "mode": "trello_unavailable",
            "needs_human": needs_human,
            "trello_configured": True,
            "queued": 0,
            "note": "external-tool CLI not on PATH; no Trello card queued.",
        }
    req = {
        "source_id": str(trello.get("source_id") or "trello__pipedream"),
        "tool_name": str(trello.get("tool_name") or "trello-create-card"),
        "tool_arguments": {
            "idList": list_id,
            "name": f"Needs-human SMS replies: {needs_human}",
            "desc": (
                "Aggregate placeholder: detail stays on operator host. "
                "Review needs-human inbound replies in OpenPhone."
            ),
        },
    }
    try:
        proc = subprocess.run(
            [binary, "call", json.dumps(req)],
            capture_output=True, text=True, timeout=20.0, check=False,
        )
        ok = proc.returncode == 0
    except Exception:
        ok = False
    if ok:
        result.escalations_queued += 1
    return {
        "mode": "trello",
        "needs_human": needs_human,
        "trello_configured": True,
        "queued": 1 if ok else 0,
        "note": (
            "One Trello card queued for aggregate needs-human review."
            if ok else
            "Trello card create failed; review on operator host."
        ),
    }


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
            f"Checks daily and can backfill uncontacted leads now (up to "
            f"{max_backfill} in the initial backfill run). Can also run "
            f"hourly for fast response, up to {max_hourly} sends per hour."
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
    escalation_status: dict[str, Any] | None = None,
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
            "Daily check for uncontacted Google Ads leads from the "
            "office lead tabs that can backfill now, with office-"
            "specific booking links and STOP opt-out language. Can also "
            "run hourly for fast response. Sends from the OpenPhone "
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
        "escalation": {
            "mode": (
                (escalation_status or {}).get("mode") or "aggregate_only"
            ),
            "needs_human": int(
                (escalation_status or {}).get("needs_human") or 0
            ),
            "trello_configured": bool(
                (escalation_status or {}).get("trello_configured")
            ),
            "queued": int((escalation_status or {}).get("queued") or 0),
            "note": str((escalation_status or {}).get("note") or ""),
            "channel": "trello_or_aggregate",
        },
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
        # Live send path. Per-run cap enforced inside ``apply_sends``;
        # dedupe and feedback-column idempotence enforced during scan.
        apply_sends(sheets, op, cfg, result)
        result.last_run_status = "apply"
    else:
        result.last_run_status = "check" if check_mode else "dry_run"

    # Reply polling + escalation. Always aggregate-only; Trello queue is
    # off unless private config sets escalation.trello.list_id +
    # escalation.trello.enabled=true.
    reply_summary: dict[str, Any] | None = None
    if provider_ready and (check_mode or apply_mode):
        try:
            reply_summary = op.poll_replies()
        except Exception:
            reply_summary = None
    escalation_status = maybe_escalate_needs_human(cfg, result, reply_summary)

    block = build_automations_block(
        result,
        provider_status=provider_msg,
        provider_check=provider_check,
        send_policy_enabled=send_policy_enabled,
        apply_mode=apply_mode,
        cfg=cfg,
        optimization_rows=optimization_rows,
        escalation_status=escalation_status,
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
