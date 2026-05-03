"""Apply a sanitized multi-office Google Ads payload into ``data/snapshot.json``.

This helper is called from the private operations pipeline and from
local development when refreshing the public dashboard's Google Ads
section against the latest sanitized export of the connected accounts.
It loads ``data/snapshot.json``, replaces ``google_ads_insights`` and
``google_ads_keyword_focus`` with the multi-office rollup, leaderboard,
campaign action list, and keyword themes from the payload, and then
re-runs ``build_snapshot.sanitize_for_public`` to guarantee the
public-mirror contract.

Usage::

    python3 scripts/apply_ads_payload.py /path/to/payload.json

The payload is the sanitized ``dashboard_multi_office_ads_payload.json``
shape produced by the private builder. Account identifiers and any
free-text PII are never read into the snapshot or the inline embed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from build_snapshot import (
    DATA_FILE,
    INDEX_HTML,
    sanitize_for_public,
    reinject_into_html,
)


def _round(value: float | int | None, ndigits: int = 2) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return round(float(value), ndigits)


def _campaign_priority(risk: str | None) -> str:
    r = (risk or "").strip().lower()
    if r == "high":
        return "High"
    if r == "protect":
        return "Protect"
    if r == "monitor":
        return "Monitor"
    if r == "medium":
        return "Monitor"
    return "Monitor"


def _decision_label(action: str, risk: str) -> str:
    pri = _campaign_priority(risk)
    if pri == "High":
        if "Pause" in action:
            return "Pause and audit"
        return "Tighten and audit"
    if pri == "Protect":
        if "model" in action:
            return "Protect and replicate"
        return "Protect and test scale"
    return "Monitor"


_SPECIFIC_REC_KEYS = (
    "google_ads_location",
    "intent_focus",
    "immediate_steps",
    "budget_bid_guidance",
    "negative_keyword_review_themes",
    "match_type_or_structure_guidance",
    "success_metric",
    "change_tracker_entry",
    "do_not_remove_note",
)


def _sanitize_specific_recommendation(
    rec: Any,
) -> dict[str, Any] | None:
    """Whitelist the public-safe keys from a specific_recommendation block.

    Any unexpected keys are dropped so we cannot accidentally leak fields
    that future payloads add (raw search-term lists, account labels, etc.).
    Lists are coerced to lists of strings; scalar fields to strings.
    """
    if not isinstance(rec, dict):
        return None
    out: dict[str, Any] = {}
    for key in _SPECIFIC_REC_KEYS:
        if key not in rec:
            continue
        val = rec.get(key)
        if val is None:
            continue
        if key in ("immediate_steps", "negative_keyword_review_themes"):
            if isinstance(val, list):
                out[key] = [str(x) for x in val if x is not None]
            elif isinstance(val, str):
                out[key] = [val]
            else:
                out[key] = []
        else:
            out[key] = str(val)
    return out or None


def _sanitize_action_queue_row(row: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(row)
    rec = cleaned.get("specific_recommendation")
    sanitized = _sanitize_specific_recommendation(rec)
    if sanitized:
        cleaned["specific_recommendation"] = sanitized
    elif "specific_recommendation" in cleaned:
        cleaned.pop("specific_recommendation", None)
    return cleaned


def _sanitize_campaign_trend_row(row: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(row)
    rec = cleaned.get("specific_recommendation")
    sanitized = _sanitize_specific_recommendation(rec)
    if sanitized:
        cleaned["specific_recommendation"] = sanitized
    elif "specific_recommendation" in cleaned:
        cleaned.pop("specific_recommendation", None)
    return cleaned


def _campaign_to_dashboard(c: dict[str, Any]) -> dict[str, Any]:
    risk = c.get("risk") or "Monitor"
    action = c.get("recommended_action") or ""
    pri = _campaign_priority(risk)
    flags: list[str] = []
    cpa = c.get("cpa")
    avg_cpc = c.get("avg_cpc")
    cvr = c.get("conversion_rate_pct")
    spend = c.get("spend") or 0
    if cpa is not None and cpa and cpa >= 150:
        flags.append("CPA elevated vs office target")
    if avg_cpc is not None and avg_cpc >= 9:
        flags.append("High CPC")
    if cvr is not None and cvr < 2 and spend >= 500:
        flags.append("Low conversion rate vs spend")
    if (c.get("phone_calls") or 0) == 0 and spend >= 500:
        flags.append("No tracked phone calls in window")
    why = (
        f"${_round(spend, 2)} spend, "
        f"{_round(c.get('clicks') or 0, 0)} clicks, "
        f"{_round(c.get('conversions') or 0, 2)} conversions, "
        f"{('$' + str(_round(avg_cpc, 2))) if avg_cpc is not None else '-'} CPC, "
        f"{(str(_round(cvr, 2)) + '%') if cvr is not None else '-'} CVR, "
        f"{('$' + str(_round(cpa, 2))) if cpa is not None else '-'} CPA."
    )
    next_steps = []
    if pri == "High":
        next_steps = [
            "Pull search-term and asset detail privately and pause ad groups "
            "with zero conversions and high CPC.",
            "Add negatives for plan-research, member-login, jobs, and broad "
            "provider-list searches.",
            "Verify call/form tracking and landing-page intent before any "
            "budget restoration.",
        ]
    elif pri == "Protect":
        next_steps = [
            "Hold current budget; run a small (10-15%) weekly increment only "
            "if call quality stays strong.",
            "Replicate the structure into the next office where lead quality "
            "is confirmed.",
        ]
    else:
        next_steps = [
            "Review weekly; revisit budget only after a quality check on the "
            "underlying queries and calls.",
        ]
    return {
        "office": c.get("office") or "",
        "campaign_name": c.get("campaign") or "",
        "channel": c.get("channel") or "",
        "status": c.get("primary_status") or c.get("status") or "",
        "cost_usd": _round(spend, 2),
        "clicks": _round(c.get("clicks") or 0, 0),
        "conversions": _round(c.get("conversions") or 0, 2),
        "impressions": _round(c.get("impressions") or 0, 0),
        "phone_calls": _round(c.get("phone_calls") or 0, 0),
        "avg_cpc_usd": _round(avg_cpc, 2),
        "cpa_usd": _round(cpa, 2) if cpa is not None else None,
        "ctr_pct": _round(c.get("ctr_pct"), 2),
        "conversion_rate_pct": _round(cvr, 2),
        "risk": risk,
        "flags": flags,
        "recommended_action": action,
        "decision_detail": {
            "priority": pri,
            "decision": _decision_label(action, risk),
            "what_to_change": action,
            "why": why,
            "next_steps": next_steps,
        },
    }


def _office_leaderboard(office_summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for o in office_summaries:
        rows.append({
            "office": o.get("office") or "",
            "status": o.get("status") or "",
            "campaign_count": _round(o.get("campaign_count") or 0, 0),
            "spend_usd": _round(o.get("spend") or 0, 2),
            "clicks": _round(o.get("clicks") or 0, 0),
            "conversions": _round(o.get("conversions") or 0, 2),
            "phone_calls": _round(o.get("phone_calls") or 0, 0),
            "avg_cpc_usd": _round(o.get("avg_cpc"), 2),
            "cpa_usd": _round(o.get("cpa"), 2) if o.get("cpa") is not None else None,
            "ctr_pct": _round(o.get("ctr_pct"), 2),
            "conversion_rate_pct": _round(o.get("conversion_rate_pct"), 2),
            "high_risk_spend_usd": _round(o.get("high_risk_spend") or 0, 2),
            "high_risk_campaign_count": _round(
                o.get("high_risk_campaign_count") or 0, 0),
            "high_risk_spend_share_pct": _round(
                o.get("high_risk_spend_share_pct") or 0, 2),
        })
    rows.sort(key=lambda r: r.get("spend_usd") or 0, reverse=True)
    return rows


def _campaign_groups(office_summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for o in office_summaries:
        groups.append({
            "group": o.get("office") or "",
            "office_label": o.get("office") or "",
            "office_hint": "Direct linked Google Ads account reporting.",
            "campaigns": _round(o.get("campaign_count") or 0, 0),
            "cost_usd": _round(o.get("spend") or 0, 2),
            "conversions": _round(o.get("conversions") or 0, 2),
            "high_risk_spend_usd": _round(o.get("high_risk_spend") or 0, 2),
            "high_risk_spend_share_pct": _round(
                o.get("high_risk_spend_share_pct") or 0, 2),
            "note": (
                "Direct linked office account. Office labels are the only "
                "identifier shown in the public mirror."
            ),
        })
    groups.sort(key=lambda r: r.get("cost_usd") or 0, reverse=True)
    return groups


def _account_coverage(coverage_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in coverage_rows:
        out.append({
            "office": row.get("office") or "",
            "status": row.get("status") or "",
            "campaigns": _round(row.get("campaigns") or 0, 0),
            "note": row.get("note") or "",
        })
    out.sort(key=lambda r: (-(r.get("campaigns") or 0), r.get("office") or ""))
    return out


def _build_recommended_actions(
    rollup: dict[str, Any],
    high_risk: list[dict[str, Any]],
    protect: list[dict[str, Any]],
) -> list[str]:
    actions: list[str] = []
    if high_risk:
        top_risk = high_risk[:3]
        names = ", ".join(
            f"{c.get('office')} - {c.get('campaign')}" for c in top_risk
        )
        actions.append(
            "Tighten or pause first: " + names +
            " - reduce budget/bids, sweep negatives, and verify "
            "tracking before any reload."
        )
    if protect:
        top_protect = protect[:3]
        names = ", ".join(
            f"{c.get('office')} - {c.get('campaign')}" for c in top_protect
        )
        actions.append(
            "Protect and replicate: " + names +
            " - hold budget, copy winning structure into the next office "
            "where lead quality is confirmed."
        )
    actions.append(
        "Audit zero-conversion search and PMax campaigns with no phone "
        "calls before any budget restoration."
    )
    actions.append(
        "Use the office leaderboard and high-risk spend share to choose "
        "where call/quality audits go this week."
    )
    actions.append(
        f"High-risk spend currently at "
        f"${_round(rollup.get('high_risk_spend') or 0, 2)} "
        f"({_round(rollup.get('high_risk_spend_share_pct') or 0, 2)}% of "
        f"30-day spend across "
        f"{rollup.get('reporting_offices') or 0} reporting offices)."
    )
    return actions


def _build_operator_notes() -> list[str]:
    return [
        "Direct linked account reporting is active for every connected "
        "office in this refresh; manager-account consolidation remains a "
        "private/internal step.",
        "No live Google Ads changes are made from this dashboard. The "
        "currently exposed connector supports reporting and limited "
        "writebacks (offline conversions / customer lists), not direct "
        "campaign, budget, bid, negative-keyword, or ad mutations.",
        "Office labels are the only identifier shown publicly. Manager "
        "and child customer IDs, dashed and undashed, are intentionally "
        "never exposed.",
    ]


def _build_keyword_focus(payload_kf: dict[str, Any]) -> dict[str, Any]:
    focus = []
    for row in payload_kf.get("protect_or_expand") or []:
        theme = row.get("keyword_or_theme") or row.get("theme") or ""
        office = row.get("office") or ""
        focus.append({
            "keyword": theme,
            "office": office,
            "focus_reason": row.get("why") or "",
            "recommended_action": "Protect or expand carefully.",
        })
    negative = []
    for row in payload_kf.get("tighten_or_pause") or []:
        theme = row.get("keyword_or_theme") or row.get("theme") or ""
        office = row.get("office") or ""
        negative.append({
            "keyword": theme,
            "office": office,
            "why_review_or_negative": row.get("why") or "",
        })
    return {
        "title": "Keyword and theme focus",
        "source_note": (
            "Themes derived from the multi-office Google Ads connector "
            "refresh. Search-term-level data is intentionally kept "
            "private; only protect/expand vs tighten/pause themes are "
            "shown publicly."
        ),
        "focus_keywords": focus,
        "negative_or_isolate_candidates": negative,
        "campaign_mapping": [],
        "api_writeback_capability": {
            "status": (
                "Reporting is supported across all linked office accounts. "
                "The exposed connector does not support direct campaign, "
                "budget, bid, negative-keyword, or ad mutations from this "
                "dashboard."
            ),
            "can_write_supported": [
                "Offline conversion uploads via the Google Ads API.",
                "Customer-list (audience) updates via the Google Ads API.",
                "Reporting reads across all linked office accounts.",
            ],
            "not_exposed_by_current_connector": [
                "Campaign create / pause / budget edits.",
                "Ad-group, ad, and asset edits.",
                "Keyword and negative-keyword edits.",
                "Bid-strategy and target-CPA / target-ROAS changes.",
            ],
            "required_for_live_changes": (
                "Add a Google Ads mutation tool/scope that can write "
                "campaign criteria, budgets, ad-group criteria, ads, and "
                "campaign status. Then require explicit per-change "
                "operator approval before any write."
            ),
        },
    }


def _build_account_linking_status(
    rollup: dict[str, Any],
    coverage_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    reporting_offices = rollup.get("reporting_offices") or len(coverage_rows)
    return {
        "last_checked": "",
        "manager_accounts_visible": "private",
        "enabled_managers_visible": "private",
        "metrics_source_summary": (
            f"Direct linked account reporting is active for "
            f"{reporting_offices} office account(s). "
            "Manager-account consolidation and any private hierarchy "
            "details remain internal."
        ),
        "new_manager_status": (
            "Manager-account enumeration is intentionally not surfaced "
            "publicly. The dashboard shows only the office accounts that "
            "are currently returning campaign metrics."
        ),
        "api_limitation": (
            "The connector enumerates linked office accounts and pulls "
            "their campaign metrics directly. Cross-account manager "
            "rollups and customer-client hierarchy details are private."
        ),
        "privacy_note": (
            "Public dashboard shows account state by office label only. "
            "Manager and child customer IDs, dashed and undashed, are "
            "never exposed."
        ),
        "public_state": (
            f"Reporting is live for {reporting_offices} linked office "
            "account(s) in this refresh, including the recently added "
            "Riverpark AI, Santa Monica, and Santa Monica AI accounts."
        ),
        "manager_metrics_note": (
            "Manager-level rollups are computed privately. Each office "
            "account is reported as its own row."
        ),
        "connector_limitation_note": (
            "Reporting is supported. Direct campaign/budget/bid/negative/"
            "ad mutations are not supported by the currently exposed "
            "connector. Live ad changes require a mutation-capable Google "
            "Ads tool/scope and explicit approval."
        ),
        "next_actions": [
            "Keep direct linked account reporting as the public source.",
            "Wire a mutation-capable Google Ads tool/scope before "
            "executing any live campaign, budget, bid, or negative "
            "changes from this dashboard.",
            "Continue keeping manager-account consolidation and any "
            "customer IDs private/internal.",
        ],
        "no_ids_disclaimer": (
            "Manager account IDs, customer IDs, and account labels are "
            "intentionally never displayed in the public mirror."
        ),
    }


def apply_payload(payload: dict[str, Any]) -> dict[str, Any]:
    snap = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    google_ads = payload.get("google_ads") or {}
    rollup = google_ads.get("rollup") or {}
    coverage_rows = google_ads.get("account_coverage") or []
    office_summaries = google_ads.get("office_summaries") or []
    decisions = google_ads.get("campaign_decisions") or []
    high_risk = google_ads.get("high_risk_campaigns") or []
    protect = google_ads.get("protect_or_scale_campaigns") or []
    keyword_focus = google_ads.get("keyword_focus") or {}
    api_writeback = google_ads.get("api_writeback_status") or {}
    manual_queue = [
        _sanitize_action_queue_row(r)
        for r in (google_ads.get("manual_action_queue") or [])
        if isinstance(r, dict)
    ]
    trend_summary = google_ads.get("trend_summary") or {}
    office_trends = google_ads.get("office_trends") or []
    campaign_trends = [
        _sanitize_campaign_trend_row(r)
        for r in (google_ads.get("campaign_trends") or [])
        if isinstance(r, dict)
    ]
    change_tracking = google_ads.get("change_tracking") or {}
    daily_update_note = google_ads.get("daily_update_note") or ""
    operator_review_order = google_ads.get("operator_review_order") or []
    if not isinstance(operator_review_order, list):
        operator_review_order = []
    operator_review_order = [
        str(x) for x in operator_review_order if x is not None
    ]
    recommendation_detail_note = (
        google_ads.get("recommendation_detail_note") or ""
    )
    dashboard_priorities = payload.get("dashboard_priorities") or []

    reporting_offices = rollup.get("reporting_offices") or len(coverage_rows)

    insights: dict[str, Any] = {
        "title": "Google Ads Multi-Office Watch",
        "subtitle": (
            f"Direct linked account reporting across {reporting_offices} "
            "office account(s) for the last 30 days. Recommendations are "
            "dashboard-only; no live Google Ads changes are pushed."
        ),
        "lookback": rollup.get("date_range") or "LAST_30_DAYS",
        "data_freshness": payload.get("generated_at") or "",
        "automation_status": (
            "Direct linked account reporting is active for every "
            f"connected office account ({reporting_offices} in this "
            "refresh). Recommendations are dashboard-only and require "
            "explicit operator approval before any live change."
        ),
        "coverage": {
            "linked_offices": reporting_offices,
            "offices_pending_link": "internal",
            "office_label_policy": (
                "Office labels are the only identifier shown publicly. "
                "Manager and child customer IDs are never exposed in the "
                "public mirror; office mapping is pending for any office "
                "not yet linked under the manager."
            ),
            "next_step": (
                "Continue private manager-account consolidation and keep "
                "office mapping internal."
            ),
        },
        "totals": {
            "campaigns": _round(
                sum((o.get("campaign_count") or 0) for o in office_summaries),
                0,
            ),
            "cost_usd": _round(rollup.get("spend") or 0, 2),
            "clicks": _round(rollup.get("clicks") or 0, 0),
            "conversions": _round(rollup.get("conversions") or 0, 2),
            "impressions": _round(rollup.get("impressions") or 0, 0),
            "phone_calls": _round(rollup.get("phone_calls") or 0, 0),
            "avg_cpc_usd": _round(rollup.get("avg_cpc"), 2),
            "cpa_usd": _round(rollup.get("cpa"), 2),
            "ctr_pct": _round(rollup.get("ctr_pct"), 2),
            "conversion_rate_pct": _round(rollup.get("conversion_rate_pct"), 2),
        },
        "risk_summary": {
            "high": sum(
                1 for c in decisions if (c.get("risk") or "").lower() == "high"
            ),
            "monitor": sum(
                1 for c in decisions
                if (c.get("risk") or "").lower() in ("monitor", "medium")
            ),
            "protect": sum(
                1 for c in decisions
                if (c.get("risk") or "").lower() == "protect"
            ),
            "low_volume_no_conversions": sum(
                1 for c in decisions
                if (c.get("conversions") or 0) == 0
                and (c.get("spend") or 0) > 0
            ),
            "high_priority_campaigns": _round(
                rollup.get("high_risk_campaign_count") or 0, 0),
            "protect_campaigns": sum(
                1 for c in decisions
                if (c.get("risk") or "").lower() == "protect"
            ),
            "monitor_campaigns": sum(
                1 for c in decisions
                if (c.get("risk") or "").lower() in ("monitor", "medium")
            ),
            "high_risk_spend_usd": _round(rollup.get("high_risk_spend") or 0, 2),
            "high_risk_spend_share_pct": _round(
                rollup.get("high_risk_spend_share_pct") or 0, 2),
            "high_cost_share_pct": _round(
                rollup.get("high_risk_spend_share_pct") or 0, 2),
            "note": (
                "High-risk spend is the share of 30-day spend coming from "
                "campaigns flagged for tightening or pausing. Prioritize "
                "search-query review and negative sweeps before any "
                "budget restoration."
            ),
        },
        "recommended_budget_shift": {
            "from": [
                f"{c.get('office')} - {c.get('campaign')}"
                for c in high_risk[:6]
            ],
            "to": [
                f"{c.get('office')} - {c.get('campaign')}"
                for c in protect[:6]
            ],
            "guardrail": (
                "Do not shift budget until call/booking quality is "
                "verified per office; then move in small weekly "
                "increments."
            ),
            "estimated_waste_to_review_usd": _round(
                rollup.get("high_risk_spend") or 0, 2),
            "estimated_waste_share_pct": _round(
                rollup.get("high_risk_spend_share_pct") or 0, 2),
        },
        "campaign_groups": _campaign_groups(office_summaries),
        "office_leaderboard": _office_leaderboard(office_summaries),
        "account_coverage": _account_coverage(coverage_rows),
        "campaigns": [_campaign_to_dashboard(c) for c in decisions],
        "ad_group_recommendations": [],
        "office_mapping_needed": [],
        "recommended_actions": _build_recommended_actions(
            rollup, high_risk, protect),
        "operator_notes": _build_operator_notes(),
        "account_linking_status": _build_account_linking_status(
            rollup, coverage_rows),
        "api_writeback_status": {
            "read_status": api_writeback.get("read_status") or "",
            "write_status": api_writeback.get("write_status") or "",
            "required_for_live_changes": api_writeback.get(
                "required_for_live_changes") or "",
        },
        "manual_action_queue": manual_queue,
        "trends": {
            "rollup": trend_summary,
            "by_office": office_trends,
            "by_campaign": campaign_trends,
        },
        "change_tracking": change_tracking,
        "daily_update_note": daily_update_note,
        "operator_review_order": operator_review_order,
        "recommendation_detail_note": recommendation_detail_note,
        "dashboard_priorities": dashboard_priorities,
    }

    snap["google_ads_insights"] = insights
    snap["google_ads_keyword_focus"] = _build_keyword_focus(keyword_focus)
    snap["generated_at"] = payload.get("generated_at") or snap.get(
        "generated_at", "")
    return snap


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: apply_ads_payload.py PAYLOAD.json", file=sys.stderr)
        return 2
    payload_path = Path(argv[1])
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    snap = apply_payload(payload)
    sanitized = sanitize_for_public(snap)
    DATA_FILE.write_text(json.dumps(sanitized, indent=2) + "\n",
                         encoding="utf-8")
    reinject_into_html(sanitized)
    print(f"Wrote {DATA_FILE} and re-injected into {INDEX_HTML}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
