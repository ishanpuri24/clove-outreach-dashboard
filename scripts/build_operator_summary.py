"""Compute operator-summary + freshness diagnostics on the public snapshot.

This is a derivative, deterministic transform: it reads
``data/snapshot.json``, computes ``operator_summary``, refreshes
``gmb_insights.freshness_status``, and writes the file back. It does
not pull any new data. It is safe to run before validators and is
idempotent.

The script intentionally does not modify the underlying source blocks
(``gmb_insights.office_rows``, ``paid_ads_action_system.queue``, etc.).
It only adds aggregate, decision-focused views computed from them, so
the existing daily refresh pipeline keeps owning the source data.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_PATH = REPO_ROOT / "data" / "snapshot.json"

# Offices the operator explicitly tracks first when triaging paid ads.
PRIORITY_OFFICES = ("Camarillo", "Sherman Oaks", "Puri Dentistry")

GMB_STALE_DAYS = 2  # daily cadence; flag if > 2d behind snapshot


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    # Extract leading ISO date or datetime even if value carries prose
    # ("Pulled 2026-05-19 from Google Ads via Pipedream").
    m = re.search(r"(\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?)", value)
    if not m:
        return None
    txt = m.group(1).replace(" ", "T")
    if "T" not in txt:
        txt += "T00:00:00"
    try:
        dt = datetime.fromisoformat(txt.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _age_days(value: Any, anchor: datetime) -> int | None:
    dt = _parse_iso(value)
    if dt is None:
        return None
    return max(0, int((anchor - dt).total_seconds() // 86400))


def _trend_arrow(curr: float | int | None, prior: float | int | None,
                 *, prefer_lower: bool = False) -> str:
    if curr is None or prior is None:
        return "→"
    if curr == prior:
        return "→"
    up = curr > prior
    if prefer_lower:
        return "↓" if up is False else "↑"
    return "↑" if up else "↓"


def _freshness_status(data_freshness: Any, snapshot_anchor: datetime) -> dict:
    age = _age_days(data_freshness, snapshot_anchor)
    if age is None:
        return {
            "data_freshness": data_freshness or None,
            "age_days": None,
            "is_stale": True,
            "stale_threshold_days": GMB_STALE_DAYS,
            "label": "unknown",
            "note": (
                "data_freshness missing or unparseable; treat as stale until "
                "next refresh writes a parseable timestamp."
            ),
        }
    is_stale = age > GMB_STALE_DAYS
    return {
        "data_freshness": data_freshness,
        "age_days": age,
        "is_stale": is_stale,
        "stale_threshold_days": GMB_STALE_DAYS,
        "label": "stale" if is_stale else "fresh",
        "note": (
            f"GMB data is {age}d behind snapshot generation; expected daily."
            if is_stale else
            f"GMB refreshed {age}d ago, within daily cadence."
        ),
    }


def _gmb_summary(gmb: dict, fresh: dict) -> dict:
    trend = gmb.get("trend") or {}
    cards = gmb.get("summary_cards") or []
    avg = next((c.get("value") for c in cards if c.get("label") == "Avg rating"), None)
    unreplied = trend.get("unreplied")
    if unreplied is None:
        unreplied = next(
            (c.get("value") for c in cards if c.get("label") == "Unreplied"),
            "—",
        )
    last_7d = trend.get("reviews_7d")
    prior_7d_low = None
    cur_7d_low = None
    lrw = gmb.get("low_review_weekly_trends") or {}
    totals = lrw.get("totals") or {}
    cur_7d_low = totals.get("last_7d_low")
    prior_7d_low = totals.get("prior_7d_low")
    return {
        "avg_rating": avg or "—",
        "reviews_7d": last_7d if last_7d is not None else "—",
        "goal_attainment_pct": trend.get("goal_attainment"),
        "low_last_7d": cur_7d_low,
        "low_prior_7d": prior_7d_low,
        "low_trend": _trend_arrow(cur_7d_low, prior_7d_low, prefer_lower=True),
        "unreplied": unreplied,
        "freshness_label": fresh.get("label"),
        "freshness_age_days": fresh.get("age_days"),
        "is_stale": fresh.get("is_stale"),
    }


def _paid_ads_summary(snapshot: dict) -> dict:
    ga = snapshot.get("google_ads_insights") or {}
    pa = snapshot.get("paid_ads_action_system") or {}
    totals = ga.get("totals") or {}
    tc = pa.get("tier_counts") or {}
    queue = pa.get("queue") or []
    # Office rollup: opportunity USD per office, category counts
    office: dict[str, dict] = {}
    for q in queue:
        o = q.get("office") or "—"
        slot = office.setdefault(o, {
            "office": o,
            "queued": 0,
            "executable_now": 0,
            "blocked_on_mutate": 0,
            "opportunity_usd": 0.0,
            "cut": 0, "protect": 0, "scale": 0, "other": 0,
        })
        slot["queued"] += 1
        slot["opportunity_usd"] += float(q.get("estimated_opportunity_usd") or 0)
        if q.get("can_execute_now"):
            slot["executable_now"] += 1
        else:
            slot["blocked_on_mutate"] += 1
        cat = (q.get("category") or "").lower()
        if "waste" in cat or "cut" in cat or "pause" in cat:
            slot["cut"] += 1
        elif "scale" in cat or "expand" in cat or "lift" in cat:
            slot["scale"] += 1
        elif "protect" in cat or "hold" in cat:
            slot["protect"] += 1
        else:
            slot["other"] += 1
    office_rows = sorted(
        office.values(), key=lambda r: -r["opportunity_usd"]
    )
    return {
        "queue_total": len(queue),
        "executable_now": tc.get("executable_now", 0),
        "needs_mutate_access": tc.get("mutation_ready_when_write_access_available", 0),
        "needs_approval": tc.get("approval_required_higher_risk", 0),
        "spend_30d_usd": totals.get("cost_usd"),
        "cpa_usd": totals.get("cpa_usd"),
        "recoverable_per_month_usd": totals.get("recoverable_per_month_usd"),
        "office_rollup": office_rows[:12],
        "priority_offices": [
            o for o in office_rows if o["office"] in PRIORITY_OFFICES
        ],
        "blocker": pa.get("blocker_for_direct_writes") or "",
    }


def _organic_summary(snapshot: dict) -> dict:
    org = snapshot.get("organic_insights") or {}
    cms = snapshot.get("organic_cms_actions") or {}
    actions = cms.get("actions") or []
    live = sum(1 for a in actions if a.get("status") == "applied_live")
    draft = sum(1 for a in actions if a.get("status") == "applied_draft")
    proposed = sum(
        1 for a in actions
        if a.get("status") in ("proposed", "proposed_not_applied")
    )
    return {
        "publish_mode": cms.get("publish_mode"),
        "growth_mode": cms.get("growth_mode"),
        "accelerated": cms.get("accelerated"),
        "live_writes": live,
        "draft_writes": draft,
        "proposals": proposed,
        "cms_actions": len(actions),
        "cooldown_days": cms.get("cooldown_days"),
        "summary": cms.get("summary") or "",
        "freshness": org.get("data_freshness"),
    }


def _calls_summary(snapshot: dict) -> dict:
    cr = snapshot.get("callrail_live") or {}
    l7 = cr.get("last_7_days") or {}
    l30 = cr.get("last_30_days") or {}

    def _n(d: dict, k: str) -> Any:
        v = d.get(k)
        return v if v is not None else "—"

    return {
        "qualified_calls_7d": _n(l7, "qualified_calls"),
        "qualified_calls_30d": _n(l30, "qualified_calls"),
        "total_calls_7d": _n(l7, "total_calls"),
        "total_calls_30d": _n(l30, "total_calls"),
        "qualification_rule": cr.get("qualification_rule"),
        "refreshed_at": cr.get("refreshed_at"),
    }


def _stringify_alert(alert: Any) -> str:
    """Coerce a staleness_alert (str | dict | None) into a concise string."""
    if alert is None:
        return ""
    if isinstance(alert, str):
        return alert.strip()
    if isinstance(alert, dict):
        days = alert.get("days_stale")
        thr = alert.get("threshold_days")
        nxt = alert.get("next_action") or alert.get("blocker") or ""
        head = []
        if isinstance(days, (int, float)):
            head.append(f"{int(days)}d stale")
        if isinstance(thr, (int, float)):
            head.append(f"thr {int(thr)}d")
        prefix = " · ".join(head)
        nxt = str(nxt).strip()
        if prefix and nxt:
            # Trim long next-action prose to one short clause.
            short = nxt.split(".")[0].strip()
            if len(short) > 90:
                short = short[:87].rstrip() + "…"
            return f"{prefix} — {short}"
        return prefix or (nxt.split(".")[0].strip()[:90] if nxt else "stale")
    return ""


def _membership_summary(snapshot: dict) -> dict:
    m = snapshot.get("membership_insights") or {}
    snap = m.get("snapshot") or {}
    return {
        "headline": m.get("headline") or "",
        "freshness": m.get("data_freshness"),
        "staleness_alert": _stringify_alert(m.get("staleness_alert")),
        "cash_7d_usd": snap.get("cash_7d_usd"),
        "cash_prior_7d_usd": snap.get("cash_prior_7d_usd"),
        "cash_delta_share_pct": snap.get("cash_delta_share_pct"),
        "active_members": snap.get("active_members"),
        "new_signups_period": snap.get("new_signups_period"),
        "overdue": snap.get("overdue"),
    }


def _sms_summary(snapshot: dict) -> dict:
    autos = snapshot.get("automations") or {}
    items = autos.get("items") or []
    sms = next((i for i in items if "sms" in str(i.get("id") or "").lower()
                or "sms" in str(i.get("name") or "").lower()), None) or {}
    c = sms.get("counters") or {}
    return {
        "automation": sms.get("name") or sms.get("id") or "Lead SMS",
        "status": sms.get("status"),
        "backlog": c.get("backlog"),
        "eligible": c.get("eligible"),
        "sent_today": c.get("sent_today"),
        "booked": c.get("booked"),
        "apply_mode": "apply" if sms.get("apply_mode") else "dry-run",
        "last_run_at_utc": sms.get("last_run_at_utc"),
    }


def _top_actions(snapshot: dict, paid: dict, gmb: dict, organic: dict) -> list[dict]:
    """Return top 5 cross-channel actions, primarily ranked by $ opportunity.

    Ranking rules:
      1. Sort by ``opportunity_usd`` desc — dollar-quantified items dominate.
      2. Items without a $ figure are admitted only when explicitly P0
         (operator-flagged urgent) and tagged ``priority_reason`` so the UI
         can label them "Priority — no $ estimate" rather than mixing them
         silently above paid-waste items.
      3. Executable-now breaks ties at equal $.
    """
    pool: list[dict] = []

    # Paid ads: every queue item participates; they carry real $ figures.
    pa = snapshot.get("paid_ads_action_system") or {}
    for q in pa.get("queue") or []:
        opp = q.get("estimated_opportunity_usd")
        pool.append({
            "channel": "Paid Ads",
            "priority": q.get("priority") or "P1",
            "office": q.get("office"),
            "label": q.get("label") or q.get("action"),
            "action": q.get("action"),
            "opportunity_usd": float(opp) if isinstance(opp, (int, float)) else None,
            "executable_now": bool(q.get("can_execute_now")),
            "blocker": q.get("blocker") or "",
            "metric": q.get("impact_metric"),
            "priority_reason": None,
        })

    # GMB: admitted only when P0 (urgent reputational), tagged for the UI.
    for a in (gmb.get("top_actions") or []):
        pri = a.get("priority") or "P1"
        if pri != "P0":
            continue
        pool.append({
            "channel": "GMB Reviews",
            "priority": pri,
            "office": None,
            "label": a.get("label") or a.get("action"),
            "action": a.get("action"),
            "opportunity_usd": None,
            "executable_now": True,
            "blocker": "",
            "metric": "Star avg · low-review count · time-to-reply",
            "priority_reason": "P0 reputational — no $ estimate",
        })

    # Organic / CMS: only the top proposal, only if it's an executable live
    # write OR there is something to gate on (proposals + no credentials).
    cms_actions = (snapshot.get("organic_cms_actions") or {}).get("actions") or []
    if cms_actions:
        cand = cms_actions[0]
        status = cand.get("status")
        executable = status in ("applied_live", "applied_draft")
        pool.append({
            "channel": "Organic / CMS",
            "priority": "P1",
            "office": None,
            "label": cand.get("page") or "CMS metadata",
            "action": cand.get("change") or "Title/meta refresh",
            "opportunity_usd": None,
            "executable_now": executable,
            "blocker": ("" if executable
                        else "Pending HubSpot live-write credentials"),
            "metric": cand.get("metric_to_watch"),
            "priority_reason": ("Live metadata write ready"
                                if executable else None),
        })

    def _sort_key(r: dict) -> tuple:
        # Bucket A: has $ opportunity — rank by $ desc.
        # Bucket B: P0 priority with no $ — comes after bucket A unless $0.
        # Bucket C: everything else — lowest.
        opp = r.get("opportunity_usd")
        has_money = isinstance(opp, (int, float)) and opp > 0
        is_p0 = r.get("priority") == "P0"
        if has_money:
            bucket = 0
        elif is_p0:
            bucket = 1
        else:
            bucket = 2
        return (
            bucket,
            -(opp or 0),
            0 if r.get("executable_now") else 1,
        )

    pool.sort(key=_sort_key)
    return pool[:5]


def _blockers(snapshot: dict, paid: dict, organic: dict, gmb_fresh: dict,
              membership: dict) -> list[dict]:
    out: list[dict] = []
    if paid.get("needs_mutate_access"):
        out.append({
            "channel": "Paid Ads",
            "blocker": "Google Ads mutate access not connected",
            "impact": f"{paid['needs_mutate_access']} actions held",
        })
    if gmb_fresh.get("is_stale"):
        out.append({
            "channel": "GMB Reviews",
            "blocker": "GMB data older than daily cadence",
            "impact": gmb_fresh.get("note") or "Refresh pipeline behind",
        })
    if membership.get("staleness_alert"):
        out.append({
            "channel": "Membership",
            "blocker": "Subscribili export stale",
            "impact": membership.get("staleness_alert"),
        })
    if not organic.get("live_writes"):
        out.append({
            "channel": "Organic / CMS",
            "blocker": "HubSpot live-write credentials not connected",
            "impact": f"{organic.get('proposals', 0)} metadata proposals pending",
        })
    return out


def _last_action(snapshot: dict) -> dict:
    cr_refresh = (snapshot.get("callrail_live") or {}).get("refreshed_at")
    ga_refresh = (snapshot.get("google_ads_insights") or {}).get("data_freshness")
    gmb_refresh = (snapshot.get("gmb_insights") or {}).get("data_freshness")
    org_refresh = (snapshot.get("organic_insights") or {}).get("data_freshness")
    return {
        "snapshot_generated_at": snapshot.get("generated_at"),
        "by_channel": {
            "paid_ads": ga_refresh,
            "calls": cr_refresh,
            "gmb": gmb_refresh,
            "organic": org_refresh,
        },
    }


def build_operator_summary(snapshot: dict) -> dict:
    anchor = _parse_iso(snapshot.get("generated_at")) or datetime.now(timezone.utc)
    gmb = snapshot.get("gmb_insights") or {}
    gmb_fresh = _freshness_status(gmb.get("data_freshness"), anchor)
    paid = _paid_ads_summary(snapshot)
    organic = _organic_summary(snapshot)
    membership = _membership_summary(snapshot)
    calls = _calls_summary(snapshot)
    sms = _sms_summary(snapshot)
    gmb_sum = _gmb_summary(gmb, gmb_fresh)
    actions = _top_actions(snapshot, paid, gmb, organic)
    blockers = _blockers(snapshot, paid, organic, gmb_fresh, membership)

    return {
        "title": "Operator Summary",
        "subtitle": "Cross-initiative status, decisions, and blockers — no prose.",
        "generated_at": snapshot.get("generated_at"),
        "kpi_cards": [
            {"label": "Paid spend 30d",
             "value": (f"${paid['spend_30d_usd']:,.0f}"
                       if isinstance(paid["spend_30d_usd"], (int, float)) else "—"),
             "basis": f"CPA ${paid['cpa_usd']}" if paid.get('cpa_usd') else "—",
             "decision": (
                 f"Recover ~${paid['recoverable_per_month_usd']:,.0f}/mo"
                 if isinstance(paid.get("recoverable_per_month_usd"), (int, float))
                 else "Tighten waste"
             )},
            {"label": "Paid action queue",
             "value": f"{paid['queue_total']} queued",
             "basis": f"{paid['executable_now']} executable · "
                      f"{paid['needs_mutate_access']} need mutate",
             "decision": "Cut waste; protect winners; scale eligible."},
            {"label": "GMB avg rating",
             "value": str(gmb_sum["avg_rating"]),
             "basis": f"{gmb_sum['unreplied']} unreplied low reviews",
             "decision": (
                 "Refresh GMB pipeline" if gmb_fresh.get("is_stale")
                 else "Reply within 24h"
             )},
            {"label": "GMB low last 7d",
             "value": (f"{gmb_sum['low_last_7d']} {gmb_sum['low_trend']}"
                       if gmb_sum["low_last_7d"] is not None else "—"),
             "basis": (f"prior 7d {gmb_sum['low_prior_7d']}"
                       if gmb_sum["low_prior_7d"] is not None else "—"),
             "decision": "Huddle on recurring themes; service recovery."},
            {"label": "Qualified calls 7d",
             "value": str(calls["qualified_calls_7d"]),
             "basis": f"{calls['total_calls_7d']} total · {calls['qualification_rule'] or '—'}",
             "decision": "Watch qualified-call CPA per office."},
            {"label": "CMS writes",
             "value": (f"{organic['live_writes']} live / "
                       f"{organic['draft_writes']} draft / "
                       f"{organic['proposals']} proposed"),
             "basis": (f"mode: {organic['growth_mode'] or organic['publish_mode'] or '—'}"),
             "decision": (
                 "Approve live credentials" if not organic["live_writes"]
                 else "Watch CTR/clicks 14-28d"
             )},
            {"label": "Lead SMS",
             "value": (f"{sms['sent_today'] or 0} sent today"),
             "basis": (f"backlog {sms.get('backlog') or 0} · "
                       f"eligible {sms.get('eligible') or 0} · "
                       f"{sms.get('apply_mode')}"),
             "decision": "Move to apply once provider + dedupe verified."},
            {"label": "Membership cash 7d",
             "value": (f"${membership['cash_7d_usd']:,.0f}"
                       if isinstance(membership.get('cash_7d_usd'), (int, float))
                       else (f"{membership['active_members']} active members"
                             if isinstance(membership.get('active_members'),
                                           (int, float))
                             else "—")),
             "basis": (f"prior 7d ${membership['cash_prior_7d_usd']:,.0f}"
                       if isinstance(membership.get('cash_prior_7d_usd'),
                                     (int, float))
                       else (f"{membership['new_signups_period'] or 0} new · "
                             f"{membership['overdue'] or 0} overdue"
                             if membership.get('active_members') is not None
                             else "—")),
             "decision": (membership["staleness_alert"]
                          if membership.get("staleness_alert")
                          else "Hold cadence")},
        ],
        "trend_badges": [
            {"label": "GMB low 7d", "trend": gmb_sum["low_trend"],
             "current": gmb_sum["low_last_7d"], "prior": gmb_sum["low_prior_7d"],
             "good_direction": "down"},
        ],
        "top_actions": actions,
        "blockers": blockers,
        "last_action": _last_action(snapshot),
        "gmb_freshness": gmb_fresh,
        "paid_ads_office_rollup": paid["office_rollup"],
        "paid_ads_priority_offices": paid["priority_offices"],
        "channel_freshness": {
            "paid_ads": snapshot.get("google_ads_insights", {}).get("data_freshness"),
            "calls": snapshot.get("callrail_live", {}).get("refreshed_at"),
            "gmb": gmb.get("data_freshness"),
            "gmb_age_days": gmb_fresh.get("age_days"),
            "gmb_is_stale": gmb_fresh.get("is_stale"),
            "organic": snapshot.get("organic_insights", {}).get("data_freshness"),
            "membership": snapshot.get("membership_insights", {}).get(
                "data_freshness"
            ),
        },
    }


def _annotate_cms_cooldown_policy(snapshot: dict) -> None:
    """Surface the new no-cooldown-for-approved-live-writes policy.

    The optimizer encodes the policy in its public block, but for any
    snapshot generated before the optimizer ships the new fields we
    backfill the announcement here so the dashboard reflects current
    reality. This does NOT cause a write or change credentials — it
    only labels the policy the operator approved.
    """
    oca = snapshot.get("organic_cms_actions")
    if not isinstance(oca, dict):
        return
    accelerated = bool(oca.get("accelerated")) or oca.get("publish_mode") == (
        "accelerated_controlled_live_writeback_with_small_content_allowed"
    )
    oca["no_cooldown_for_approved_live_writes"] = bool(accelerated)
    oca["low_risk_metadata_change_types"] = [
        "missing_or_weak_title_update",
        "missing_or_weak_meta_description_update",
    ]
    oca["cooldown_policy_note"] = (
        "Approved low-risk metadata live writes (title/meta only) on the "
        "main HubSpot website have no cooldown. Body / module / FAQ / "
        "internal-link changes keep the standard cooldown."
    )
    # Surface live-write readiness honestly. The optimizer still gates
    # actual writes on credentials being present; we never claim a live
    # write occurred unless the underlying log row shows applied_live.
    actions = oca.get("actions") or []
    live = sum(1 for a in actions if a.get("status") == "applied_live")
    oca["live_write_status_note"] = (
        f"{live} live write(s) recorded in this run. "
        "Live writes only occur when HubSpot credentials are connected "
        "and the change type is on the approved low-risk metadata list."
    )


def main() -> int:
    snapshot = json.loads(SNAPSHOT_PATH.read_text())
    _annotate_cms_cooldown_policy(snapshot)
    summary = build_operator_summary(snapshot)
    snapshot["operator_summary"] = summary
    gmb = snapshot.setdefault("gmb_insights", {})
    gmb["freshness_status"] = summary["gmb_freshness"]
    SNAPSHOT_PATH.write_text(json.dumps(snapshot, indent=2) + "\n")
    print(json.dumps({
        "wrote": str(SNAPSHOT_PATH.relative_to(REPO_ROOT)),
        "gmb_freshness": summary["gmb_freshness"],
        "kpi_cards": len(summary["kpi_cards"]),
        "top_actions": len(summary["top_actions"]),
        "blockers": len(summary["blockers"]),
        "priority_offices": [r["office"] for r in summary["paid_ads_priority_offices"]],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
