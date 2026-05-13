#!/usr/bin/env python3
"""Daily Clove patient-acquisition dashboard refresh orchestrator.

Self-deployable, fast-mode-safe routine refresh entry point for the
scheduled daily task and for local/GitHub checkouts. Replaces the
prior reliance on ``process_daily_run.py`` (an outreach payload
stager with no connector / refresh logic).

Behavior:

  * Reads private configs and last-known-good summaries from
    ``/home/user/workspace/cron_tracking/a3b9de2f`` when present.
  * In ``--fast`` mode (the default) it does not perform live
    connector calls. It re-stamps the public snapshot's
    ``generated_at`` and refreshes the routine-refresh status block,
    merges sanitized aggregates that already exist on disk, and
    leaves any unavailable metric marked ``pending`` or ``stale``
    rather than fabricating values.
  * Always runs ``--no-send`` by default. Outbound outreach is never
    triggered from this script. The script will refuse to send even
    if ``--no-send`` is removed unless an explicit ``--sender`` is
    supplied and a sender-bound connector is wired up, which by
    design is not part of this orchestrator.
  * Persists ``daily_learning_state.json`` (in the private tracking
    directory) with the last refresh status and suppressed-repeat
    recommendation tracking, when that file is present.
  * Strictly avoids publishing any private IDs, tokens, raw review
    IDs, patient/member/prospect data, phone numbers, GCLIDs,
    personal emails, config paths, scheduler IDs, or raw connector
    payloads. Office labels that are already public (e.g. "Thousand
    Oaks") are allowed in aggregate context.

CLI:

    python3 scripts/refresh_marketing_dashboard.py            # fast + no-send (default)
    python3 scripts/refresh_marketing_dashboard.py --fast     # explicit
    python3 scripts/refresh_marketing_dashboard.py --no-send  # explicit
    python3 scripts/refresh_marketing_dashboard.py --private-dir /path/to/cron_tracking/<id>
    python3 scripts/refresh_marketing_dashboard.py --check    # validate only, no write

Exit code is non-zero only on a structural failure (snapshot
unreadable / unwritable / sanitization invariants violated).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_SNAPSHOT = REPO_ROOT / "data" / "snapshot.json"

# Optional companion module: HubSpot CMS optimizer. Imported lazily
# so the refresh still works in environments where the optimizer's
# private config / network is unavailable.
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import hubspot_cms_optimizer as _cms_optimizer  # type: ignore
except Exception:  # pragma: no cover - companion is best-effort
    _cms_optimizer = None  # type: ignore

DEFAULT_PRIVATE_DIR = Path("/home/user/workspace/cron_tracking/a3b9de2f")

# Sanitized inputs we will consider merging from the private dir.
# Each is keyed by the public snapshot section it can refresh.
SANITIZED_INPUTS = {
    "callrail_live": {
        "7d": "callrail_7d_sanitized.json",
        "30d": "callrail_30d_sanitized.json",
    },
}

# Patterns that must never appear in the public snapshot. The
# validator covers more, but the orchestrator double-checks the
# delta it writes itself to keep failures local.
FORBIDDEN_PATTERNS = [
    re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),  # email
    re.compile(r"\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b"),                    # US phone
    re.compile(r"\bGCLID\b", re.IGNORECASE),                            # GCLID label
    re.compile(r"AIza[0-9A-Za-z_\-]{10,}"),                             # google api key
    re.compile(r"ya29\.[0-9A-Za-z_\-]{10,}"),                           # google oauth
    re.compile(r"\b[0-9]{3}-[0-9]{3}-[0-9]{4}\b"),                     # ads cust id
]

OFFICE_LABEL_ALLOWLIST = {
    "Thousand Oaks", "Camarillo", "Ventura", "Oxnard", "Beverly Hills",
    "Santa Monica", "Sherman Oaks", "Encino", "Los Angeles",
}

# GA4 key-event mapping state carried forward on every refresh. GA4
# itself is configured in the private analytics_config; this block
# only tracks which key events are mapped vs. pending site-side
# instrumentation, so the public dashboard can stay action-oriented.
# No GA4 property ID, measurement ID, or key-event ID is published.
GA4_KEY_EVENTS_PUBLIC = [
    {
        "name": "form_submit",
        "status": "mapped_as_key_event",
        "scope": "ONCE_PER_SESSION",
        "mapped_on": "2026-05-13",
        "impact_metric": "Organic form_submit conversions reported in GA4",
        "next_action": "Watch GA4 conversions report 24-48h for organic form_submit volume to appear.",
    },
    {
        "name": "call_click",
        "status": "instrumentation_pending",
        "scope": "ONCE_PER_SESSION (planned)",
        "impact_metric": "Organic call_click conversions reported in GA4",
        "next_action": "Add tel: link click event to site templates, then map as a GA4 key event.",
    },
    {
        "name": "appt_booked",
        "status": "instrumentation_pending",
        "scope": "ONCE_PER_SESSION (planned)",
        "impact_metric": "Organic appt_booked conversions reported in GA4",
        "next_action": "Fire appt_booked on booking-confirmation page (HubSpot/Subscribili flow), then map as a GA4 key event.",
    },
]


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json_atomic(path: Path, payload: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def scan_forbidden(node: Any, path: str = "$") -> list[str]:
    """Walk a JSON-like structure and flag forbidden patterns."""
    issues: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            issues.extend(scan_forbidden(v, f"{path}.{k}"))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            issues.extend(scan_forbidden(v, f"{path}[{i}]"))
    elif isinstance(node, str):
        for pat in FORBIDDEN_PATTERNS:
            if pat.search(node):
                # Allow public office labels appearing alone.
                if any(label in node for label in OFFICE_LABEL_ALLOWLIST):
                    continue
                issues.append(f"forbidden pattern at {path}: {pat.pattern}")
    return issues


def merge_callrail(snapshot: dict, private_dir: Path, status: dict) -> None:
    src_7d = read_json(private_dir / SANITIZED_INPUTS["callrail_live"]["7d"], None)
    src_30d = read_json(private_dir / SANITIZED_INPUTS["callrail_live"]["30d"], None)
    if not (src_7d or src_30d):
        status["callrail"] = "pending: no sanitized snapshot on disk"
        return
    live = snapshot.setdefault("callrail_live", {})
    if src_7d:
        prev_7d = live.get("last_7_days", {}) if isinstance(live.get("last_7_days"), dict) else {}
        merged_7d = dict(prev_7d)
        for k in (
            "total_calls", "answered", "missed", "first_time_callers",
            "callrail_qualified",
        ):
            if k in src_7d:
                merged_7d[k] = src_7d[k]
        ans = merged_7d.get("answered")
        tot = merged_7d.get("total_calls")
        if isinstance(ans, (int, float)) and isinstance(tot, (int, float)) and tot:
            merged_7d["answer_rate_pct"] = round(ans / tot * 100, 1)
        ql = merged_7d.get("callrail_qualified")
        if isinstance(ql, (int, float)) and isinstance(tot, (int, float)) and tot:
            merged_7d["qualified_rate_pct"] = round(ql / tot * 100, 1)
        live["last_7_days"] = merged_7d
    if src_30d:
        prev_30d = live.get("last_30_days", {}) if isinstance(live.get("last_30_days"), dict) else {}
        merged_30d = dict(prev_30d)
        for k in (
            "total_calls", "answered", "missed", "first_time_callers",
            "callrail_qualified",
        ):
            if k in src_30d:
                merged_30d[k] = src_30d[k]
        ans = merged_30d.get("answered")
        tot = merged_30d.get("total_calls")
        if isinstance(ans, (int, float)) and isinstance(tot, (int, float)) and tot:
            merged_30d["answer_rate_pct"] = round(ans / tot * 100, 1)
        ql = merged_30d.get("callrail_qualified")
        if isinstance(ql, (int, float)) and isinstance(tot, (int, float)) and tot:
            merged_30d["qualified_rate_pct"] = round(ql / tot * 100, 1)
        live["last_30_days"] = merged_30d
    # use the most recent pulled_at, never the raw connector payload path
    refreshed = max(
        (s.get("pulled_at") for s in (src_7d, src_30d) if isinstance(s, dict) and s.get("pulled_at")),
        default=None,
    )
    if refreshed:
        live["refreshed_at"] = refreshed
    status["callrail"] = "ok: merged sanitized aggregate"


def _ga4_action_for_organic() -> dict:
    return {
        "priority": "P1",
        "label": "GA4 instrumentation queue: call_click + appt_booked",
        "action": (
            "form_submit is mapped as a GA4 key event (done 2026-05-13). "
            "Add tel: link click + booking-confirmation events on site, "
            "then map each as a key event in GA4."
        ),
        "owner": "Marketing engineering",
        "status": "in_progress",
        "impact_metric": (
            "Organic conversions surfaced in GA4 (form_submit live; "
            "call_click + appt_booked when instrumented)"
        ),
    }


def update_ga4_status_block(snapshot: dict) -> None:
    """Stamp GA4 actionable status into organic_insights.

    GA4 is connected. The public mirror only states which key events
    are mapped vs. pending site-side instrumentation. No property ID,
    measurement ID, or key-event ID is published.
    """
    organic = snapshot.get("organic_insights")
    if not isinstance(organic, dict):
        return
    for c in organic.get("connector_status", []) or []:
        if not isinstance(c, dict):
            continue
        integ = str(c.get("integration", "")).lower()
        if integ.startswith("google analytics"):
            c["status"] = (
                "Connected; form_submit mapped, call_click/appt_booked "
                "pending instrumentation"
            )
            c["severity"] = "warn"
            c["action"] = (
                "form_submit mapped as a GA4 key event on 2026-05-13 "
                "(ONCE_PER_SESSION). call_click and appt_booked still "
                "need site-side instrumentation before they can be mapped."
            )
            c["key_events"] = list(GA4_KEY_EVENTS_PUBLIC)
    for r in organic.get("source_status_rows", []) or []:
        if not isinstance(r, dict):
            continue
        if str(r.get("source", "")).upper() == "GA4":
            r["status"] = "Connected; form_submit conversion mapped"
            r["note"] = (
                "form_submit mapped as a GA4 key event "
                "(ONCE_PER_SESSION) on 2026-05-13. call_click and "
                "appt_booked are queued for site-side instrumentation; "
                "once events fire they will be mapped the same way."
            )
    # Top-action: ensure GA4 instrumentation queue stays on the list
    # and any stale "map GA4 conversions" entry is removed.
    actions = organic.get("top_actions")
    if isinstance(actions, list):
        filtered = []
        for a in actions:
            if not isinstance(a, dict):
                continue
            label = (a.get("label") or a.get("action") or "").lower()
            if "ga4" in label and (
                "map" in label
                or "key event" in label
                or "conversion" in label
                or "instrumentation" in label
            ):
                continue
            filtered.append(a)
        organic["top_actions"] = [_ga4_action_for_organic()] + filtered


# ---------------------------------------------------------------------------
# Review-recovery weekly trend block
# ---------------------------------------------------------------------------
#
# Goal: turn the Reviews tab from a static low-rating list into a weekly
# trend view per office, with a clickable drilldown showing sanitized
# snippets, recurring themes, and aggregate staff-response signals.
#
# Inputs (read-only, never re-published as-is):
#   * snapshot["gmb_insights"]["office_rows"]           — current rolling counts
#   * snapshot["gmb_insights"]["negative_queue"]        — sanitized low reviews
#   * snapshot["gmb_insights"]["data_freshness"]        — anchor for "now"
#   * private_dir / "staff_review_reply_signals.json"   — aggregate counts only
#   * snapshot["gmb_insights"]["low_review_weekly_trends"] (prior, if any)
#
# Output (public, sanitized): gmb_insights.low_review_weekly_trends — see
# `_DEFAULT_TREND_SCHEMA_HINT` for the shape. The validator (check_review_
# weekly_trends) enforces it and refuses to publish any reviewer name,
# profile link, GBP ID, raw review ID, email body, private path, etc.

THEME_KEYWORD_MAP = {
    "Wait / scheduling": ["wait", "waiting", "appoint", "schedule", "late", "hour"],
    "Insurance / billing": ["insurance", "covered", "charge", "pricing", "billing", "cost", "paid"],
    "Communication": ["communic", "told", "never", "confirm", "call", "phone", "explain"],
    "Clinical experience": ["cleaning", "x-ray", "xray", "assistant", "pain", "specialist", "root", "crown", "extract"],
    "Staff professionalism": ["unprofessional", "rude", "manager", "front desk", "staff"],
    "Legacy transition": ["former owner", "sold", "chain", "takeover", "transition"],
}

# Token strippers for drilldown snippets. We already accept the public
# negative_queue copy that was sanitized upstream, but a second pass is
# cheap and makes the drilldown safe to expand.
_NAME_TOKEN_RE = re.compile(r"\b(?:Dr\.|Doctor|Mr\.|Mrs\.|Ms\.)\s+[A-Z][a-zA-Z]{1,30}\b")
_TITLED_NAME_RE = re.compile(
    r"\b(?:manager|nurse|hygienist|assistant|receptionist|specialist|dentist)\s+[A-Z][a-zA-Z]{1,30}\b",
    re.IGNORECASE,
)
_BARE_NAME_RE = re.compile(r"\(\s*[A-Z][a-zA-Z]{2,30}(?:\s+[A-Z][a-zA-Z]{2,30})?\s*\)")
_URL_RE = re.compile(r"https?://\S+|maps\.google\.\S+")
_GBP_ID_RE = re.compile(r"\b(?:accounts/\d+|locations/\d+|reviews/[A-Za-z0-9_-]+)\b")


def sanitize_snippet(text: str, limit: int = 180) -> str:
    """Strip reviewer/staff names, URLs, GBP IDs, and clamp length.

    The public snapshot already redacts most of this upstream; this is
    a belt-and-suspenders pass for the drilldown payload, which the
    user can click to expand.
    """
    if not isinstance(text, str) or not text.strip():
        return ""
    out = text
    out = _URL_RE.sub("[link removed]", out)
    out = _GBP_ID_RE.sub("[id removed]", out)
    out = _NAME_TOKEN_RE.sub("[name removed]", out)
    out = _TITLED_NAME_RE.sub(lambda m: m.group(0).split()[0] + " [name removed]", out)
    out = _BARE_NAME_RE.sub("([name removed])", out)
    out = re.sub(r"\s+", " ", out).strip()
    if len(out) > limit:
        out = out[: limit - 1].rstrip() + "…"
    return out


def _theme_tags(snippet: str) -> list[str]:
    s = (snippet or "").lower()
    tags = []
    for theme, needles in THEME_KEYWORD_MAP.items():
        if any(n in s for n in needles):
            tags.append(theme)
    return tags[:3]


def _parse_iso_date(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        if "T" in value:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _week_start(d: datetime) -> datetime:
    """Monday 00:00 UTC of d's week."""
    d_utc = d.astimezone(timezone.utc) if d.tzinfo else d.replace(tzinfo=timezone.utc)
    monday = d_utc - timedelta(days=d_utc.weekday())
    return datetime(monday.year, monday.month, monday.day, tzinfo=timezone.utc)


def _load_staff_reply_signals(private_dir: Path) -> dict:
    raw = read_json(private_dir / "staff_review_reply_signals.json", None)
    if not isinstance(raw, dict):
        return {}
    # Strip anything other than aggregate counts before we even consider
    # using these values downstream. We never read or republish bodies.
    out: dict[str, Any] = {
        "checked_at": raw.get("checked_at"),
        "total_matches": int(raw.get("total_matches") or 0),
        "review_related_signals": int(raw.get("review_related_signals") or 0),
        "non_review_noise": int(raw.get("non_review_noise") or 0),
        "office_reply_signals": {},
    }
    src = raw.get("office_reply_signals") or {}
    if isinstance(src, dict):
        for office, val in src.items():
            if not isinstance(office, str) or not isinstance(val, dict):
                continue
            out["office_reply_signals"][office] = {
                "matches": int(val.get("matches") or 0),
                "review_related": int(val.get("review_related") or 0),
                "latest_date": val.get("latest_date"),
            }
    return out


def _office_weekly_buckets(office: str, queue: list[dict], anchor: datetime, weeks: int = 4) -> list[dict]:
    buckets: list[dict] = []
    for offset in range(weeks):
        end = _week_start(anchor) - timedelta(days=7 * offset)
        start = end - timedelta(days=7)
        in_bucket = [
            r for r in queue
            if (r.get("office") == office)
            and (start <= (_parse_iso_date(r.get("date")) or anchor) < end)
        ]
        ratings = [int(r.get("rating") or 0) for r in in_bucket if isinstance(r.get("rating"), (int, float))]
        avg = round(sum(ratings) / len(ratings), 2) if ratings else None
        buckets.append({
            "week_start": start.date().isoformat(),
            "week_end": (end - timedelta(seconds=1)).date().isoformat(),
            "low_count": len(in_bucket),
            "avg_rating": avg,
        })
    buckets.reverse()  # oldest -> newest
    return buckets


def _office_drilldown(office: str, queue: list[dict], anchor: datetime, weeks: int = 4) -> list[dict]:
    """Per-week sanitized drilldown payload for a single office."""
    out: list[dict] = []
    for offset in range(weeks):
        end = _week_start(anchor) - timedelta(days=7 * offset)
        start = end - timedelta(days=7)
        rows = [
            r for r in queue
            if (r.get("office") == office)
            and (start <= (_parse_iso_date(r.get("date")) or anchor) < end)
        ]
        theme_counts: dict[str, int] = {}
        snippets: list[dict] = []
        replied_count = 0
        for r in sorted(rows, key=lambda x: x.get("date") or "", reverse=True):
            snip = sanitize_snippet(r.get("snippet") or "")
            tags = _theme_tags(snip)
            for t in tags:
                theme_counts[t] = theme_counts.get(t, 0) + 1
            replied = bool(r.get("replied"))
            if replied:
                replied_count += 1
            snippets.append({
                "date": r.get("date") or "—",
                "rating": int(r.get("rating") or 0),
                "snippet": snip,
                "replied": replied,
                "themes": tags,
            })
        n = len(snippets)
        if n == 0:
            action_status = "No low reviews this week"
        elif replied_count == n:
            action_status = "All replied — log recovery"
        elif replied_count == 0:
            action_status = f"Reply within 24h ({n} unreplied)"
        else:
            action_status = f"Reply within 24h ({n - replied_count} of {n} unreplied)"
        out.append({
            "week_start": start.date().isoformat(),
            "week_end": (end - timedelta(seconds=1)).date().isoformat(),
            "low_count": n,
            "replied_count": replied_count,
            "themes": sorted(
                ({"theme": k, "count": v} for k, v in theme_counts.items()),
                key=lambda t: -t["count"],
            )[:3],
            "sanitized_snippets": snippets[:5],
            "action_status": action_status,
        })
    out.reverse()  # oldest -> newest
    return out


def _trend_direction(buckets: list[dict]) -> str:
    """Direction of the *latest* week vs the prior week.

    Buckets are oldest -> newest. We compare the most recent week to
    the one before it: a fresh low review or a fresh dry week is what
    the user actually wants to see, not a smoothed average.
    """
    counts = [b.get("low_count", 0) or 0 for b in buckets]
    if len(counts) < 2:
        return "flat"
    last, prev = counts[-1], counts[-2]
    if last > prev:
        return "up"
    if last < prev:
        return "down"
    return "flat"


def _prior_action_effect(office: str, prior_trends: dict | None) -> dict | None:
    """Return how the prior weekly bucket compares to the current one."""
    if not isinstance(prior_trends, dict):
        return None
    for o in prior_trends.get("office_trends", []) or []:
        if o.get("office") != office:
            continue
        action = o.get("next_action") or o.get("prior_action", {}).get("action")
        prior_low = o.get("last_7d_low")
        prior_week = o.get("current_week_start") or prior_trends.get("current_week_start")
        if action is None or prior_low is None:
            return None
        return {
            "week_start": prior_week,
            "action": action,
            "low_then": prior_low,
        }
    return None


def _office_response_signals(office: str, signals: dict) -> dict:
    block = (signals.get("office_reply_signals") or {}).get(office) or {}
    matches = int(block.get("review_related") or 0)
    label = "no reply signals detected"
    if matches >= 2:
        label = f"{matches} response signals (last 28d)"
    elif matches == 1:
        label = "1 response signal (last 28d)"
    return {
        "matches_28d": matches,
        "latest_signal_date": block.get("latest_date"),
        "label": label,
    }


def _open_followups(office: str, queue: list[dict], anchor: datetime) -> tuple[int, int | None]:
    """Aggregate unresolved/open low reviews for this office.

    "Open" = low-rating review with replied=false. Oldest open age is
    in days. No raw IDs surface here.
    """
    opens = [
        r for r in queue
        if r.get("office") == office and not bool(r.get("replied"))
    ]
    if not opens:
        return 0, None
    ages = []
    for r in opens:
        d = _parse_iso_date(r.get("date"))
        if d:
            ages.append((anchor - d).days)
    oldest = max(ages) if ages else None
    return len(opens), oldest


def build_review_weekly_trends(
    snapshot: dict, private_dir: Path, prior: dict | None
) -> dict | None:
    gmb = snapshot.get("gmb_insights") or {}
    rows = gmb.get("office_rows") or []
    queue = gmb.get("negative_queue") or []
    if not rows:
        return None
    anchor = _parse_iso_date(gmb.get("data_freshness")) or datetime.now(timezone.utc)
    signals = _load_staff_reply_signals(private_dir)

    office_trends: list[dict] = []
    for o in rows:
        office = o.get("office")
        if not office:
            continue
        buckets = _office_weekly_buckets(office, queue, anchor)
        last_7d_low = buckets[-1]["low_count"] if buckets else 0
        prior_7d_low = buckets[-2]["low_count"] if len(buckets) >= 2 else 0
        last_7d_avg = buckets[-1]["avg_rating"]
        last_28d_ratings = [b["avg_rating"] for b in buckets if b["avg_rating"] is not None]
        last_28d_avg = round(sum(last_28d_ratings) / len(last_28d_ratings), 2) if last_28d_ratings else None
        opens, oldest = _open_followups(office, queue, anchor)
        drilldown = _office_drilldown(office, queue, anchor)
        # recurring themes = themes that show up in 2+ of the last 4 weeks
        theme_week_count: dict[str, int] = {}
        for wk in drilldown:
            for t in wk.get("themes", []):
                theme_week_count[t["theme"]] = theme_week_count.get(t["theme"], 0) + 1
        recurring_themes = [
            {"theme": k, "weeks_seen": v, "recurring": v >= 2}
            for k, v in sorted(theme_week_count.items(), key=lambda kv: -kv[1])
        ]
        direction = _trend_direction(buckets)
        prior_action = _prior_action_effect(office, prior)
        if prior_action is not None:
            prior_action["low_now"] = last_7d_low
            prior_action["improved"] = (last_7d_low < prior_action.get("low_then", last_7d_low))
        next_action = o.get("action") or "Reply within 24h, log recovery call"
        if direction == "up" and recurring_themes:
            next_action = (
                f"Huddle on {recurring_themes[0]['theme']} — recurring "
                f"{recurring_themes[0]['weeks_seen']}/4 weeks; reply on any open low review"
            )
        elif last_7d_low == 0 and prior_7d_low == 0:
            next_action = "Hold cadence; keep asking 2 happy patients/day"
        office_trends.append({
            "office": office,
            "last_7d_low": last_7d_low,
            "prior_7d_low": prior_7d_low,
            "delta": last_7d_low - prior_7d_low,
            "trend_direction": direction,
            "last_7d_avg": last_7d_avg,
            "last_28d_avg": last_28d_avg,
            "weekly_buckets": buckets,
            "common_themes": recurring_themes[:5],
            "response_signals": _office_response_signals(office, signals),
            "open_followups": opens,
            "oldest_open_age_days": oldest,
            "prior_action": prior_action,
            "next_action": next_action,
            "drilldown": drilldown,
        })

    last_7d_low_total = sum(t["last_7d_low"] for t in office_trends)
    prior_7d_low_total = sum(t["prior_7d_low"] for t in office_trends)
    opens_total = sum(t["open_followups"] for t in office_trends)
    oldest_age = max(
        (t["oldest_open_age_days"] for t in office_trends if t["oldest_open_age_days"] is not None),
        default=None,
    )

    # Cross-office weekly history (sum over offices, oldest -> newest)
    weekly_buckets_all: list[dict] = []
    for i in range(4):
        weekly_buckets_all.append({
            "week_start": office_trends[0]["weekly_buckets"][i]["week_start"] if office_trends else "",
            "week_end": office_trends[0]["weekly_buckets"][i]["week_end"] if office_trends else "",
            "low_count": sum(t["weekly_buckets"][i]["low_count"] for t in office_trends),
            "total_offices_with_low": sum(
                1 for t in office_trends if t["weekly_buckets"][i]["low_count"] > 0
            ),
        })

    # Action queue: rank recurring themes + offices trending up
    action_queue: list[dict] = []
    for t in office_trends:
        if t["trend_direction"] == "up" and t["last_7d_low"] > 0:
            recurring = [c for c in t["common_themes"] if c.get("recurring")]
            theme_label = recurring[0]["theme"] if recurring else "Service recovery"
            prior_effect = "improved" if (t.get("prior_action") and t["prior_action"].get("improved")) else "no improvement yet" if t.get("prior_action") else "first cycle"
            action_queue.append({
                "priority": "P0" if t["last_7d_low"] >= 2 else "P1",
                "office": t["office"],
                "theme": theme_label,
                "weeks_seen": recurring[0]["weeks_seen"] if recurring else 1,
                "trend_direction": "up",
                "prior_action_effect": prior_effect,
                "action": t["next_action"],
            })
    # global recurring theme across offices
    cross_theme: dict[str, dict] = {}
    for t in office_trends:
        for c in t["common_themes"]:
            if not c.get("recurring"):
                continue
            slot = cross_theme.setdefault(c["theme"], {"offices": [], "weeks_seen": 0})
            slot["offices"].append(t["office"])
            slot["weeks_seen"] = max(slot["weeks_seen"], c["weeks_seen"])
    for theme, data in cross_theme.items():
        if len(data["offices"]) >= 2:
            action_queue.append({
                "priority": "P1",
                "office": "Multi-office",
                "theme": theme,
                "weeks_seen": data["weeks_seen"],
                "trend_direction": "up",
                "prior_action_effect": "system-wide",
                "action": f"Coach on {theme} at {', '.join(sorted(set(data['offices']))[:4])} this week",
            })
    action_queue.sort(key=lambda a: (a["priority"], -a["weeks_seen"]))

    response_tracking = {
        "label": "response signals",
        "basis": "aggregate counts only — no email bodies, no staff names, no patient names",
        "total_signals_28d": signals.get("review_related_signals", 0),
        "offices_with_signals": len(signals.get("office_reply_signals") or {}),
        "checked_at": signals.get("checked_at"),
        "next_action": (
            "Improve tracking: have office managers tag review reply emails so we "
            "can move from 'response signals' to a definitive reply rate."
        ),
    }

    avg_last_7d = [t["last_7d_avg"] for t in office_trends if t["last_7d_avg"] is not None]
    last_7d_avg_global = round(sum(avg_last_7d) / len(avg_last_7d), 2) if avg_last_7d else None

    return {
        "title": "Weekly low-review trend",
        "generated_at": utcnow_iso(),
        "anchor_date": anchor.date().isoformat(),
        "current_week_start": _week_start(anchor).date().isoformat(),
        "windows": {"last_7d": 7, "prior_7d": 7, "last_28d": 28},
        "totals": {
            "last_7d_low": last_7d_low_total,
            "prior_7d_low": prior_7d_low_total,
            "delta_low": last_7d_low_total - prior_7d_low_total,
            "last_7d_avg_rating": last_7d_avg_global,
            "unresolved_open": opens_total,
            "oldest_open_age_days": oldest_age,
        },
        "weekly_buckets": weekly_buckets_all,
        "office_trends": office_trends,
        "action_queue": action_queue,
        "response_tracking": response_tracking,
        "privacy_note": (
            "Aggregate counts and theme labels only. Snippets are sanitized — "
            "no reviewer or staff names, no profile links, no GBP IDs, no "
            "raw review IDs, no email bodies."
        ),
    }


def update_review_weekly_trends(
    snapshot: dict, private_dir: Path, status: dict
) -> None:
    gmb = snapshot.setdefault("gmb_insights", {})
    prior = gmb.get("low_review_weekly_trends") if isinstance(gmb.get("low_review_weekly_trends"), dict) else None
    trends = build_review_weekly_trends(snapshot, private_dir, prior)
    if trends is None:
        status["review_weekly_trends"] = "pending: no gmb_insights.office_rows yet"
        return
    gmb["low_review_weekly_trends"] = trends
    status["review_weekly_trends"] = (
        f"ok: {len(trends['office_trends'])} offices, "
        f"{trends['totals']['last_7d_low']} low last 7d "
        f"(prior {trends['totals']['prior_7d_low']}), "
        f"{len(trends['action_queue'])} action(s) queued"
    )


def persist_review_trends_in_learning_state(
    private_dir: Path, trends: dict | None
) -> None:
    if trends is None:
        return
    state_path = private_dir / "daily_learning_state.json"
    state = read_json(state_path, None)
    if not isinstance(state, dict):
        return
    mem = state.setdefault("review_recovery_memory", {
        "weekly_history": [],
        "office_action_history": {},
    })
    # Append weekly trend snapshot (cap at 12 weeks of history)
    entry = {
        "captured_at": trends.get("generated_at"),
        "current_week_start": trends.get("current_week_start"),
        "totals": trends.get("totals"),
        "office_summary": [
            {
                "office": o["office"],
                "last_7d_low": o["last_7d_low"],
                "prior_7d_low": o["prior_7d_low"],
                "trend_direction": o["trend_direction"],
                "open_followups": o["open_followups"],
            }
            for o in trends.get("office_trends", [])
        ],
    }
    history = mem.get("weekly_history") or []
    # de-dupe by current_week_start so multiple daily refreshes overwrite
    history = [h for h in history if h.get("current_week_start") != entry["current_week_start"]]
    history.append(entry)
    mem["weekly_history"] = history[-12:]
    # Record next_action per office so we can attribute improvement next week
    oah = mem.setdefault("office_action_history", {})
    for o in trends.get("office_trends", []):
        slot = oah.setdefault(o["office"], [])
        slot.append({
            "week_start": trends.get("current_week_start"),
            "action": o.get("next_action"),
            "low_then": o.get("last_7d_low"),
        })
        oah[o["office"]] = slot[-8:]
    write_json_atomic(state_path, state)


def _review_recovery_action_entry(gmb: dict) -> dict:
    trends = gmb.get("low_review_weekly_trends") or {}
    totals = trends.get("totals") or {}
    queue = trends.get("action_queue") or []
    unresolved = totals.get("unresolved_open", 0) or 0
    oldest = totals.get("oldest_open_age_days")
    p0_offices = [a["office"] for a in queue if a.get("priority") == "P0"]
    if unresolved > 0:
        status = "active_live"
        last_action = (
            f"{unresolved} open low-review follow-up(s); oldest "
            f"{oldest} day(s) old."
        )
    elif queue:
        status = "active_live"
        last_action = f"All low reviews replied; {len(queue)} themes still trending up."
    else:
        status = "idle" if trends else "pending"
        last_action = (
            "No open follow-ups detected this refresh."
            if trends else "Awaiting first weekly-trend computation."
        )
    next_action = (
        "Reply within 24h and log recovery call for: "
        + ", ".join(p0_offices[:4])
    ) if p0_offices else (
        "Hold cadence; coach recurring themes in next huddle."
    )
    return {
        "id": "gmb-review-recovery",
        "name": "Review recovery (low-review reply + follow-up)",
        "status": status,
        "next_action": next_action,
        "last_action": last_action,
        "last_action_at": trends.get("generated_at") or gmb.get("data_freshness") or "—",
        "impact_metric": (
            "Open low-review follow-ups, oldest-open age, response-signal count"
        ),
        "blocker": None,
    }


def _review_weekly_trend_action_entry(gmb: dict) -> dict:
    trends = gmb.get("low_review_weekly_trends") or {}
    totals = trends.get("totals") or {}
    direction = "flat"
    delta = totals.get("delta_low")
    if isinstance(delta, (int, float)):
        if delta > 0:
            direction = "up"
        elif delta < 0:
            direction = "down"
    last_7d = totals.get("last_7d_low")
    prior_7d = totals.get("prior_7d_low")
    if trends:
        status = "active_live"
        last_action = (
            f"Last 7d: {last_7d} low reviews "
            f"(prior 7d: {prior_7d}, direction: {direction})."
        )
    else:
        status = "pending"
        last_action = "Awaiting first weekly trend computation."
    queue = trends.get("action_queue") or []
    if queue:
        top = queue[0]
        next_action = (
            f"{top.get('priority','P1')}: {top.get('office','—')} · "
            f"{top.get('theme','—')} ({top.get('weeks_seen',1)}/4 weeks) — "
            f"{top.get('action','review and act')}"
        )
    else:
        next_action = (
            "Maintain weekly trend tracking; surface any office trending up "
            "with recurring themes next refresh."
        )
    return {
        "id": "gmb-review-weekly-trend",
        "name": "GMB weekly low-review trend tracker",
        "status": status,
        "next_action": next_action,
        "last_action": last_action,
        "last_action_at": trends.get("generated_at") or "—",
        "impact_metric": (
            "Week-over-week low-review delta, recurring-theme count, "
            "avg rating trend, prior-action improvement rate"
        ),
        "blocker": None,
    }


def build_action_system(snapshot: dict, prior_action_system: dict | None) -> dict:
    """Rebuild the automations.action_system block from current state.

    Carries forward each action's prior `last_action_at` if the new
    refresh has no fresher info, so the timeline is preserved across
    runs. Aggregate only — no PII, no private IDs.
    """
    prior_by_id: dict[str, dict] = {}
    if isinstance(prior_action_system, dict):
        for a in prior_action_system.get("actions", []) or []:
            if isinstance(a, dict) and a.get("id"):
                prior_by_id[str(a["id"])] = a

    cms = snapshot.get("organic_cms_actions") or {}
    auto = snapshot.get("automations") or {}
    items = auto.get("items") or []
    lead_sms = items[0] if items else {}
    lead_sms_counters = lead_sms.get("counters") or {}
    lead_sms_blockers = lead_sms.get("blockers") or []
    callrail = snapshot.get("callrail_live") or {}
    callrail_30d = callrail.get("last_30_days") or {}
    gmb = snapshot.get("gmb_insights") or {}
    new_neg = gmb.get("new_negative_alerts") or {}

    if cms.get("live_writes"):
        cms_status = "active_live"
    elif cms.get("draft_writes"):
        cms_status = "active_draft"
    elif cms.get("writeback_performed"):
        cms_status = "active_dry_run"
    else:
        cms_status = "idle"

    def merge_prior(entry: dict) -> dict:
        prev = prior_by_id.get(entry["id"]) or {}
        # carry forward last_action_at when current run has no fresh
        # timestamp ("—" or empty)
        if (entry.get("last_action_at") in (None, "", "—")) and prev.get("last_action_at"):
            entry["last_action_at"] = prev["last_action_at"]
        return entry

    actions = [
        merge_prior({
            "id": "hubspot-cms-metadata",
            "name": "HubSpot CMS metadata writeback",
            "status": cms_status,
            "next_action": (
                "Continue daily learning loop: append impact samples and "
                "promote draft changes to live when CTR uplift confirmed."
            ),
            "last_action": cms.get("summary") or "Awaiting first CMS optimizer run.",
            "last_action_at": cms.get("last_run_at") or "—",
            "impact_metric": "GSC clicks / CTR on edited pages (impact_over_time)",
            "impact_samples": cms.get("impact_samples_updated", 0),
            "live_writes": cms.get("live_writes", 0),
            "draft_writes": cms.get("draft_writes", 0),
            "blocker": None,
        }),
        merge_prior({
            "id": "ga4-form-submit-mapping",
            "name": "GA4 form_submit conversion mapping",
            "status": "active_live",
            "next_action": (
                "Watch GA4 conversions report 24-48h for organic "
                "form_submit volume; queue call_click + appt_booked for "
                "site instrumentation."
            ),
            "last_action": "form_submit mapped as a GA4 key event (ONCE_PER_SESSION)",
            "last_action_at": "2026-05-13",
            "impact_metric": "GA4 key-event conversions for organic form_submit",
            "key_events": list(GA4_KEY_EVENTS_PUBLIC),
            "blocker": None,
        }),
        merge_prior({
            "id": "google-ads-lead-sms",
            "name": "Google Ads lead SMS backfill",
            "status": "blocked",
            "next_action": (
                "Refresh the OpenPhone API key (raw Authorization header) "
                "in the private config so apply mode can clear the lead "
                "backlog from the office tabs."
            ),
            "last_action": (
                f"{lead_sms_counters.get('sent_today', 0)} SMS sent today; "
                f"{lead_sms_counters.get('backlog', 0)} eligible leads waiting."
            ),
            "last_action_at": lead_sms.get("last_run_at_utc") or "—",
            "impact_metric": "Backlog cleared / first-time bookings from Google Ads leads",
            "blocker": "OpenPhone provider auth failed (provider_auth_failed)",
            "blockers_detail": list(lead_sms_blockers),
        }),
        merge_prior({
            "id": "tracking-stack",
            "name": "CallRail / Open Dental / Subscribili tracking",
            "status": "active_live" if callrail_30d else "pending",
            "next_action": (
                "Daily refresh merges sanitized CallRail aggregates; "
                "Open Dental and Subscribili pulls run on the operator host."
            ),
            "last_action": (
                f"CallRail 30d: {callrail_30d.get('total_calls', '—')} calls, "
                f"answer rate {callrail_30d.get('answer_rate_pct', '—')}%"
            ) if callrail_30d else "Awaiting first CallRail sanitized aggregate.",
            "last_action_at": callrail.get("refreshed_at") or "—",
            "impact_metric": "Answer rate, qualified calls, first-time callers",
            "blocker": None,
        }),
        merge_prior({
            "id": "gmb-new-negative-alerts",
            "name": "GMB new-negative-review alerts",
            "status": "active_live" if new_neg else "pending",
            "next_action": (
                "On each refresh, surface any new <=3-star reviews in the "
                "new-negative queue and route the office owner to respond."
            ),
            "last_action": (
                f"{new_neg.get('count', 0)} new negative review(s) detected since last run."
            ) if new_neg else "Awaiting first GMB refresh with prior-state comparison.",
            "last_action_at": new_neg.get("checked_at") or gmb.get("data_freshness") or "—",
            "impact_metric": "Time-to-first-response on negative GMB reviews; star average",
            "blocker": None,
        }),
        merge_prior(_review_recovery_action_entry(gmb)),
        merge_prior(_review_weekly_trend_action_entry(gmb)),
    ]

    return {
        "title": "Action system",
        "as_of": utcnow_iso(),
        "description": (
            "Active marketing automations and tracked actions. Aggregate "
            "only — no PII, no private IDs, no tokens. Each entry shows "
            "status, next action, last action, and the impact metric to watch."
        ),
        "actions": actions,
    }


def update_action_system_block(snapshot: dict) -> None:
    auto = snapshot.setdefault("automations", {})
    prior = auto.get("action_system") if isinstance(auto.get("action_system"), dict) else None
    auto["action_system"] = build_action_system(snapshot, prior)


def update_routine_refresh_block(snapshot: dict, status: dict, mode: str) -> None:
    """Stamp a compact, public-safe refresh status block on the snapshot."""
    refresh = snapshot.setdefault("routine_refresh", {})
    refresh["last_run_at"] = utcnow_iso()
    refresh["last_run_date"] = today_iso()
    refresh["mode"] = mode
    refresh["sources"] = status
    # Mark sources without fresh inputs as stale/pending in a compact way.
    pending = sorted([k for k, v in status.items() if str(v).startswith("pending")])
    refresh["pending_sources"] = pending


def recommendation_hash(rec: dict) -> str:
    raw = json.dumps(rec, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def update_learning_state(
    private_dir: Path,
    status: dict,
    snapshot_summary: dict,
    new_recommendations: list[dict] | None = None,
) -> str | None:
    state_path = private_dir / "daily_learning_state.json"
    state = read_json(state_path, None)
    if state is None:
        return None  # nothing to update -- file is optional
    recs = new_recommendations or []
    mem = state.setdefault("recommendation_memory", {
        "active_recommendations": [],
        "completed_actions": [],
        "suppressed_repeated_recommendations": [],
        "experiments_running": [],
        "last_recommendation_hashes": [],
    })
    seen_hashes = set(mem.get("last_recommendation_hashes", []))
    kept = []
    suppressed = list(mem.get("suppressed_repeated_recommendations", []))
    for rec in recs:
        h = recommendation_hash(rec)
        if h in seen_hashes:
            suppressed.append({"hash": h, "suppressed_at": utcnow_iso()})
        else:
            kept.append(rec)
            seen_hashes.add(h)
    mem["last_recommendation_hashes"] = sorted(seen_hashes)[-100:]
    mem["suppressed_repeated_recommendations"] = suppressed[-100:]
    mem["active_recommendations"] = kept

    metric_mem = state.setdefault("metric_memory", {
        "last_snapshot_date": None,
        "previous_metrics": {},
        "material_changes": [],
    })
    metric_mem["last_snapshot_date"] = today_iso()
    metric_mem["previous_metrics"] = snapshot_summary

    last_run = state.setdefault("last_run", {})
    last_run.update({
        "ran_at": utcnow_iso(),
        "status": "ok" if not any(str(v).startswith("pending") for v in status.values()) else "partial",
        "source_status": status,
    })
    write_json_atomic(state_path, state)
    return str(state_path)


def summarize_snapshot(snapshot: dict) -> dict:
    k = snapshot.get("kpis", {})
    return {
        "latest_date": k.get("latest_date"),
        "total_sends": k.get("total_sends"),
        "reply_rate_pct": k.get("reply_rate_pct"),
        "positive_rate_pct": k.get("positive_rate_pct"),
        "bounces": k.get("bounces"),
    }


def merge_cms_actions(
    snapshot: dict,
    private_dir: Path,
    status: dict,
    *,
    apply_changes: bool,
    max_changes: int,
    check_only: bool,
) -> dict | None:
    """Run the HubSpot CMS optimizer and merge its sanitized block.

    Returns the result dict on success, or ``None`` when the
    optimizer is unavailable / config is absent. Never raises into
    the orchestrator's main path.
    """
    if _cms_optimizer is None:
        status["hubspot_cms"] = "pending: optimizer module unavailable"
        return None
    cfg_path = private_dir / "hubspot_cms_config.json"
    if not cfg_path.exists():
        status["hubspot_cms"] = "pending: hubspot_cms_config not present"
        return None
    try:
        result = _cms_optimizer.run(
            private_dir=private_dir,
            apply_changes=apply_changes and not check_only,
            max_changes=max_changes,
            cooldown_days=_cms_optimizer.DEFAULT_COOLDOWN_DAYS,
            snapshot=snapshot,
        )
    except Exception as e:
        status["hubspot_cms"] = f"error: {type(e).__name__}"
        return None
    block = _cms_optimizer.build_public_block(result, private_dir=private_dir)
    issues = _cms_optimizer.assert_public_sanitized(block)
    issues += scan_forbidden(block)
    if issues:
        status["hubspot_cms"] = "error: sanitization invariant violated; cms_actions dropped"
        return result
    snapshot["organic_cms_actions"] = block
    parts = [
        f"inventory={result['inventory']['site_pages']}sp/{result['inventory']['landing_pages']}lp",
        f"considered={result['candidates_considered']}",
        f"actions={len(result['actions'])}",
        f"live={result.get('live_writes', 0)}",
        f"draft={result.get('draft_writes', 0)}",
        f"proposed={result.get('proposals', 0)}",
        f"impact_samples={result.get('impact_samples_updated', 0)}",
    ]
    if result.get("live_writes"):
        mode_note = " (live-writeback)"
    elif result.get("draft_writes"):
        mode_note = " (draft-writeback)"
    elif result.get("writeback_performed"):
        mode_note = " (writeback)"
    else:
        mode_note = " (dry-run)"
    status["hubspot_cms"] = "ok: " + ", ".join(parts) + mode_note
    return result


def refresh(
    private_dir: Path,
    fast: bool,
    no_send: bool,
    check_only: bool,
    *,
    cms_apply: bool = True,
    cms_max_changes: int = 3,
) -> int:
    if not PUBLIC_SNAPSHOT.exists():
        print(f"ERROR: missing public snapshot at {PUBLIC_SNAPSHOT}", file=sys.stderr)
        return 2

    snapshot = read_json(PUBLIC_SNAPSHOT, None)
    if not isinstance(snapshot, dict):
        print("ERROR: snapshot.json did not parse as a JSON object", file=sys.stderr)
        return 2

    status: dict[str, str] = {}
    mode_label = "fast" if fast else "full"

    if private_dir.exists():
        merge_callrail(snapshot, private_dir, status)
        merge_cms_actions(
            snapshot,
            private_dir,
            status,
            apply_changes=cms_apply,
            max_changes=cms_max_changes,
            check_only=check_only,
        )
    else:
        status["private_dir"] = "pending: tracking directory not present"

    # Outbound is always disabled here. We do not stage or send anything.
    if not no_send:
        # Still refuse: this orchestrator is intentionally not wired
        # for sending. The --no-send flag is the default and required
        # for correctness; we ignore attempts to disable it.
        status["outbound"] = "disabled: orchestrator does not stage or send outreach"
    else:
        status["outbound"] = "disabled: --no-send (default)"

    # Action-oriented blocks carried forward every run. These are
    # rebuilt from current snapshot state and prior action_system
    # entries so the timeline is preserved and stale setup copy is
    # blocked at the source.
    update_ga4_status_block(snapshot)
    update_review_weekly_trends(snapshot, private_dir, status)
    persist_review_trends_in_learning_state(
        private_dir,
        snapshot.get("gmb_insights", {}).get("low_review_weekly_trends"),
    )
    update_action_system_block(snapshot)
    status["ga4_key_events"] = "ok: form_submit mapped; call_click+appt_booked instrumentation pending"
    status["action_system"] = (
        f"ok: {len(snapshot['automations']['action_system']['actions'])} actions tracked"
    )

    # Routine refresh stamp.
    snapshot["generated_at"] = utcnow_iso()
    update_routine_refresh_block(snapshot, status, mode_label)

    # Final sanitization safety check on the routine_refresh block we wrote.
    issues = scan_forbidden(snapshot.get("routine_refresh", {}))
    issues += scan_forbidden(snapshot.get("callrail_live", {}))
    issues += scan_forbidden(snapshot.get("organic_cms_actions", {}))
    issues += scan_forbidden(snapshot.get("automations", {}).get("action_system", {}))
    issues += scan_forbidden(snapshot.get("organic_insights", {}).get("connector_status", []))
    issues += scan_forbidden(snapshot.get("organic_insights", {}).get("source_status_rows", []))
    issues += scan_forbidden(snapshot.get("gmb_insights", {}).get("low_review_weekly_trends", {}))
    if issues:
        print("ERROR: refresh would publish forbidden patterns:", file=sys.stderr)
        for i in issues:
            print("  -", i, file=sys.stderr)
        return 3

    learning_path = update_learning_state(
        private_dir,
        status,
        summarize_snapshot(snapshot),
        new_recommendations=[],
    )

    if check_only:
        print("CHECK: refresh dry-run; no files written.")
        print(json.dumps({"status": status, "learning_state": learning_path}, indent=2))
        return 0

    write_json_atomic(PUBLIC_SNAPSHOT, snapshot)
    print("OK: refreshed", PUBLIC_SNAPSHOT)
    print(json.dumps({"mode": mode_label, "status": status, "learning_state": learning_path}, indent=2))
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Refresh the public Clove patient-acquisition dashboard snapshot.")
    p.add_argument("--fast", action="store_true", default=True, help="Fast mode (default): no live connector calls; merge sanitized snapshots only.")
    p.add_argument("--full", dest="fast", action="store_false", help="Allow heavier merges (still no outbound, still no raw payload publish).")
    p.add_argument("--no-send", action="store_true", default=True, help="Disable outbound outreach (default and effectively required).")
    p.add_argument("--allow-send", dest="no_send", action="store_false", help="Attempt to enable outbound; orchestrator still refuses and logs.")
    p.add_argument("--private-dir", default=str(DEFAULT_PRIVATE_DIR), help="Path to the private cron tracking directory.")
    p.add_argument("--check", action="store_true", help="Validate inputs and exit without writing snapshot.json.")
    p.add_argument("--cms-apply", action="store_true", default=True, help="Allow HubSpot CMS low-risk metadata writeback if config permits (default).")
    p.add_argument("--cms-dry-run", dest="cms_apply", action="store_false", help="Force HubSpot CMS step to dry-run regardless of config.")
    p.add_argument("--cms-max-changes", type=int, default=3, help="Cap number of CMS metadata changes per run (default 3).")
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    private_dir = Path(args.private_dir)
    return refresh(
        private_dir=private_dir,
        fast=args.fast,
        no_send=args.no_send,
        check_only=args.check,
        cms_apply=args.cms_apply,
        cms_max_changes=args.cms_max_changes,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
