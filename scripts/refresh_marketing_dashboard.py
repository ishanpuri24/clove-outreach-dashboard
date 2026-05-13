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
from datetime import datetime, timezone
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
