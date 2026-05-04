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
  5. Neither file contains forbidden sensitive patterns. The default
     email whitelist is empty -- any email-shaped string anywhere in
     the public snapshot or rendered HTML fails validation. Operator
     inboxes are referred to with safe labels (``Connected Clove
     sender``, ``Internal follow-up only``). The validator also blocks
     Google Sheet IDs or URLs, Google Ads customer/manager identifiers
     (dashed and undashed), free-text reply bodies, internal commit
     hashes, common token shapes, and any ``mailto:`` recipient links.

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
    "google_ads_insights",
    "_sanitization",
]

REQUIRED_GOOGLE_ADS_FIELDS = [
    "title",
    "lookback",
    "data_freshness",
    "automation_status",
    "coverage",
    "totals",
    "risk_summary",
    "campaign_groups",
    "campaigns",
    "recommended_actions",
    "operator_notes",
    "manual_action_queue",
    "trends",
    "change_tracking",
    "operator_review_order",
    "recommendation_detail_note",
    "priority_playbooks",
    "paid_ads_top_summary",
    "conversion_rate_benchmarks",
    "ad_group_conversion_benchmarks",
    "daily_improvement_loop",
    "office_spend_opportunities",
]

REQUIRED_TREND_WINDOW_FIELDS = [
    "spend_per_day",
    "conversions_per_day",
    "avg_cpc",
    "cpa",
    "ctr_pct",
    "conversion_rate_pct",
]

REQUIRED_ACTION_QUEUE_FIELDS = [
    "priority",
    "office",
    "campaign",
    "issue",
    "evidence",
    "manual_change",
    "expected_impact",
    "check_after",
    "status",
    "specific_recommendation",
    "campaign_specific_points",
]

REQUIRED_SPECIFIC_RECOMMENDATION_FIELDS = [
    "google_ads_location",
    "intent_focus",
    "immediate_steps",
    "budget_bid_guidance",
    "negative_keyword_review_themes",
    "match_type_or_structure_guidance",
    "success_metric",
    "change_tracker_entry",
    "do_not_remove_note",
]

ALLOWED_SPECIFIC_RECOMMENDATION_FIELDS = set(
    REQUIRED_SPECIFIC_RECOMMENDATION_FIELDS
)

# Short, campaign-specific recommendation block. The visible action
# card surfaces these concise per-campaign fields; the longer
# specific_recommendation block stays as a hidden fallback. Every
# action-queue row must include a short_specific_recommendation so the
# operator never sees a generic, repeated long checklist.
REQUIRED_SHORT_SPECIFIC_RECOMMENDATION_FIELDS = [
    "headline",
    "why_this_campaign",
    "do_next",
    "inspect",
    "negative_keyword_focus",
    "structure_fix",
    "success_metric",
    "log_note",
]

ALLOWED_SHORT_SPECIFIC_RECOMMENDATION_FIELDS = set(
    REQUIRED_SHORT_SPECIFIC_RECOMMENDATION_FIELDS + ["metric_snapshot"]
)

# Compact per-campaign points block. Each visible card now renders
# only these unique campaign-specific decisions; the long P0/P1/P2
# guidance is consolidated once in priority_playbooks. The v5
# benchmarked payload swaps the legacy keys for benchmark-anchored
# ones, leading every card with how the campaign's conversion metrics
# compare to the benchmark before the operator decides what to change.
REQUIRED_CAMPAIGN_SPECIFIC_POINTS_FIELDS = [
    "conversion_benchmark",
    "ad_group_or_theme",
    "exact_change",
    "inspect",
    "keyword_focus",
    "success_metric",
    "daily_learning",
]

ALLOWED_CAMPAIGN_SPECIFIC_POINTS_FIELDS = set(
    REQUIRED_CAMPAIGN_SPECIFIC_POINTS_FIELDS
)

# Priority playbook block (P0 / P1 / P2). One small shared card per
# priority captures the repeated guidance that used to be duplicated
# inside every action card.
REQUIRED_PRIORITY_PLAYBOOK_LEVELS = ["P0", "P1", "P2"]
REQUIRED_PRIORITY_PLAYBOOK_FIELDS = [
    "label",
    "shared_action",
    "budget_rule",
    "review_window",
    "completion_rule",
]
ALLOWED_PRIORITY_PLAYBOOK_FIELDS = set(REQUIRED_PRIORITY_PLAYBOOK_FIELDS)

REQUIRED_CHANGE_TRACKING_FIELDS = [
    "purpose",
    "current_connector_limit",
    "manual_log_fields",
    "status_rules",
    "approval_rule",
]

# Top-of-Paid-Ads summary block. The v5 payload puts the blended
# CPA/CPC/CTR/CVR/conversions-per-day/spend-per-day/phone-calls-per-day
# numbers above everything else and benchmarks each one against last
# month plus the internal medians.
REQUIRED_PRIMARY_STAT_LABELS = {
    "Spend/day",
    "Conversions/day",
    "Conversion rate",
    "CPA",
    "CPC",
    "CTR",
    "Phone calls/day",
}
ALLOWED_PRIMARY_STAT_KEYS = {"label", "value", "benchmark", "delta"}
REQUIRED_PRIMARY_STAT_KEYS = ["label", "value"]
ALLOWED_TOP_SUMMARY_KEYS = {
    "title",
    "period",
    "primary_stats",
    "benchmark_rules",
    "internal_benchmarks",
}
ALLOWED_INTERNAL_BENCHMARK_KEYS = {
    "office_median_conversion_rate_pct",
    "campaign_median_conversion_rate_pct",
    "ad_group_median_conversion_rate_pct",
    "last_month_conversion_rate_pct",
}

# Conversion-rate benchmarks (by office). Required so the dashboard
# always shows where each office sits versus last month and the median.
REQUIRED_CVR_OFFICE_FIELDS = [
    "office",
    "conversion_rate_pct",
    "last_month_conversion_rate_pct",
    "vs_office_median_pts",
    "conversions_per_day",
    "cpa",
    "status",
]
ALLOWED_CVR_OFFICE_KEYS = set(REQUIRED_CVR_OFFICE_FIELDS)

# Ad-group conversion benchmarks. One row per (office, campaign, ad
# group) so an operator can jump from the summary to the specific ad
# group and keyword theme.
REQUIRED_AD_GROUP_BENCHMARK_FIELDS = [
    "office",
    "campaign",
    "ad_group",
    "spend",
    "clicks",
    "conversions",
    "conversion_rate_pct",
    "cpc",
    "benchmark_status",
    "keyword_focus",
]
ALLOWED_AD_GROUP_BENCHMARK_KEYS = set(
    REQUIRED_AD_GROUP_BENCHMARK_FIELDS + ["cpa"]
)

# Daily improvement loop section, rendered at the bottom of the Paid
# Ads tab to explain how the system improves day over day.
REQUIRED_DAILY_LOOP_FIELDS = ["title", "steps", "decision_rule"]
ALLOWED_DAILY_LOOP_FIELDS = set(REQUIRED_DAILY_LOOP_FIELDS)

# Office spend and opportunities block, rendered immediately after the
# blended Paid Ads top summary so an operator can see, by office, where
# spend should grow vs where waste cleanup must happen first.
REQUIRED_OFFICE_SPEND_OPP_TOP_KEYS = [
    "title",
    "office_inference_note",
    "total_last_30_spend_usd",
    "total_high_risk_spend_usd",
    "top_spend_offices",
    "rows",
]
ALLOWED_OFFICE_SPEND_OPP_TOP_KEYS = set(
    REQUIRED_OFFICE_SPEND_OPP_TOP_KEYS + ["placement"]
)
REQUIRED_OFFICE_SPEND_OPP_TOP_OFFICE_KEYS = [
    "office",
    "last_30_spend_usd",
    "opportunity",
]
ALLOWED_OFFICE_SPEND_OPP_TOP_OFFICE_KEYS = set(
    REQUIRED_OFFICE_SPEND_OPP_TOP_OFFICE_KEYS
)
REQUIRED_OFFICE_SPEND_OPP_ROW_KEYS = [
    "office",
    "last_30_spend_usd",
    "high_risk_spend_usd",
    "high_risk_spend_share_pct",
    "last_7_conversion_rate_pct",
    "last_month_conversion_rate_pct",
    "vs_office_median_pts",
    "last_7_cpa_usd",
    "last_30_cpa_usd",
    "last_7_conversions_per_day",
    "p0_count",
    "p1_count",
    "p2_count",
    "opportunity",
    "budget_move",
    "top_ad_group_opportunity",
]
ALLOWED_OFFICE_SPEND_OPP_ROW_KEYS = set(
    REQUIRED_OFFICE_SPEND_OPP_ROW_KEYS + [
        "last_30_conversions",
        "last_30_phone_calls",
        "last_30_cpc_usd",
        "last_30_ctr_pct",
        "last_30_conversion_rate_pct",
        "campaign_count",
        "change_items",
        "top_issue",
        "why",
        "cvr_benchmark_status",
        "protect_or_scale_candidates",
    ]
)
ALLOWED_OFFICE_SPEND_OPP_PROTECT_KEYS = {
    "campaign",
    "conversions",
    "cpa_usd",
    "conversion_rate_pct",
}

REQUIRED_GOOGLE_ADS_TOTALS = [
    "campaigns",
    "cost_usd",
    "clicks",
    "conversions",
    "avg_cpc_usd",
    "cpa_usd",
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

# The public mirror does not whitelist any email addresses. Operator
# inboxes (sender, CC) are referred to by safe labels such as
# "Connected Clove sender" or "Internal follow-up only". An empty
# whitelist means any email-shaped string in the public snapshot or
# rendered HTML fails validation.
ALLOWED_PUBLIC_EMAIL_ADDRESSES: set[str] = set()

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
    # Google Ads customer / manager account IDs. The dashed shape
    # (NNN-NNN-NNNN) is the canonical Google Ads UI form; the
    # undashed 10-digit shape is the API form. Both must be redacted
    # in the public mirror.
    (r"\b\d{3}-\d{3}-\d{4}\b", "Google Ads dashed customer/manager id"),
    (
        r"(?<![\d-])(?:customers/)?\d{10}(?![\d-])",
        "Google Ads undashed 10-digit customer id",
    ),
    # Phone number shapes (NANP). Any reply-side phone leak is forbidden.
    (
        r"(?<![\w/-])\+?1?[\s.-]?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)",
        "Phone-number-shaped value",
    ),
    # Bare private commit hashes (full or short) that would identify
    # commits in the private operations repo.
    (
        r"(?:^|[^A-Za-z0-9])\b(?:[0-9a-f]{40}|[0-9a-f]{12})\b(?![A-Za-z0-9])",
        "Possible git commit hash",
    ),
    # Internal experiment / follow-up tracker IDs from the private
    # operations repo. The public mirror surfaces hypothesis / action
    # text instead of the tracker code, so any EXP-NN or FU-NN
    # occurrence is treated as a leak.
    (
        r"\bEXP-\d{2,}\b",
        "Internal experiment tracker id (EXP-NN)",
    ),
    (
        r"\bFU-\d{2,}\b",
        "Internal follow-up tracker id (FU-NN)",
    ),
]

# Internal scheduler/task identifier shape: a bare 8-character
# lowercase hex string. The private builder uses this shape for the
# scheduled-task id; the public mirror substitutes the safe label
# "daily-refresh".
SCHEDULER_TASK_ID_RE = re.compile(r"^[0-9a-f]{8}$")

# Keys that must never appear anywhere inside b2b_reply_detail.
FORBIDDEN_REPLY_DETAIL_KEYS = {
    "Email From",
    "email_from",
    "sender",
    "sender_name",
    "sender_email",
    "from",
    "From",
    "Organization",
    "organization",
    "org_name",
    "phone",
    "Phone",
    "Body",
    "body",
    "Reply Body",
    "raw_text",
    "raw_reply",
    "summary",
    "Summary",
    "Suggested Next Action",
}


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


def check_task_id_redacted(snap: dict[str, Any]) -> None:
    """The scheduled-task id must not be the raw internal hex value.

    The private builder ships an 8-character lowercase hex
    scheduler/task identifier. The public mirror is required to
    substitute a safe label (for example ``daily-refresh``). Any value
    that still matches the internal scheduler-id shape fails
    validation.
    """
    task = snap.get("task")
    if not isinstance(task, dict):
        return
    task_id = task.get("id")
    if not isinstance(task_id, str):
        return
    if SCHEDULER_TASK_ID_RE.match(task_id):
        _fail(
            "task.id looks like a raw internal scheduler/task id "
            f"({task_id!r}); replace with a safe label such as "
            "'daily-refresh' before publishing."
        )


def check_experiments_redacted(snap: dict[str, Any]) -> None:
    """Experiments and human follow-ups must not carry tracker IDs.

    The private operations repo tracks experiments and follow-ups by
    EXP-NN / FU-NN codes. Those codes are internal: the public mirror
    renders the title/action text instead. This check enforces that no
    experiment or follow-up row ships an ``id`` field, and that no row
    field contains an EXP-NN or FU-NN substring.
    """
    experiments = snap.get("experiments") or []
    if not isinstance(experiments, list):
        _fail("experiments must be a list when present.")
    for idx, row in enumerate(experiments):
        if not isinstance(row, dict):
            _fail(f"experiments[{idx}] is not an object")
        if "id" in row:
            _fail(
                f"experiments[{idx}] still carries an internal tracker "
                "id field; remove before publishing."
            )
        for key, val in row.items():
            if isinstance(val, str) and (
                re.search(r"\bEXP-\d{2,}\b", val)
                or re.search(r"\bFU-\d{2,}\b", val)
            ):
                _fail(
                    f"experiments[{idx}].{key} references an internal "
                    f"tracker id ({val!r})."
                )

    followups = snap.get("human_followups") or []
    if not isinstance(followups, list):
        _fail("human_followups must be a list when present.")
    for idx, row in enumerate(followups):
        if not isinstance(row, dict):
            _fail(f"human_followups[{idx}] is not an object")
        if "id" in row:
            _fail(
                f"human_followups[{idx}] still carries an internal "
                "tracker id field; remove before publishing."
            )
        for key, val in row.items():
            if isinstance(val, str) and (
                re.search(r"\bEXP-\d{2,}\b", val)
                or re.search(r"\bFU-\d{2,}\b", val)
            ):
                _fail(
                    f"human_followups[{idx}].{key} references an "
                    f"internal tracker id ({val!r})."
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


def check_google_ads_insights(snap: dict[str, Any]) -> None:
    ads = snap.get("google_ads_insights")
    if not isinstance(ads, dict):
        _fail("snapshot.json google_ads_insights must be an object.")
    missing = [k for k in REQUIRED_GOOGLE_ADS_FIELDS if k not in ads]
    if missing:
        _fail(
            "snapshot.json google_ads_insights missing required "
            f"fields: {missing}"
        )

    forbidden_account_keys = {
        "manager_customer_id",
        "manager_account_id",
        "customer_id",
        "customer_ids",
        "account_id",
        "account_ids",
        "login_customer_id",
    }
    leaked = forbidden_account_keys.intersection(ads.keys())
    if leaked:
        _fail(
            "google_ads_insights exposes forbidden account-id fields "
            f"{sorted(leaked)}; account identifiers must never appear "
            "in the public mirror."
        )
    for idx, group in enumerate(ads.get("campaign_groups") or []):
        if not isinstance(group, dict):
            _fail(f"google_ads_insights.campaign_groups[{idx}] is not an object")
        leaked_g = forbidden_account_keys.intersection(group.keys())
        if leaked_g:
            _fail(
                f"google_ads_insights.campaign_groups[{idx}] exposes "
                f"forbidden account-id fields {sorted(leaked_g)}."
            )

    totals = ads.get("totals") or {}
    missing_totals = [k for k in REQUIRED_GOOGLE_ADS_TOTALS if k not in totals]
    if missing_totals:
        _fail(
            "google_ads_insights.totals missing required fields: "
            f"{missing_totals}"
        )
    numeric_totals = [
        "campaigns",
        "cost_usd",
        "clicks",
        "conversions",
        "avg_cpc_usd",
    ]
    for field in numeric_totals:
        if not isinstance(totals.get(field), (int, float)):
            _fail(
                f"google_ads_insights.totals['{field}'] must be numeric."
            )
    cpa = totals.get("cpa_usd")
    if cpa is not None and not isinstance(cpa, (int, float)):
        _fail("google_ads_insights.totals['cpa_usd'] must be numeric or null.")

    campaigns = ads.get("campaigns")
    if not isinstance(campaigns, list) or not campaigns:
        _fail("google_ads_insights.campaigns must be a non-empty list.")
    required_campaign_fields = {
        "campaign_name",
        "channel",
        "cost_usd",
        "clicks",
        "conversions",
        "avg_cpc_usd",
        "conversion_rate_pct",
        "risk",
        "recommended_action",
    }
    for idx, c in enumerate(campaigns):
        if not isinstance(c, dict):
            _fail(f"google_ads_insights.campaigns[{idx}] is not an object")
        missing_c = required_campaign_fields - c.keys()
        if missing_c:
            _fail(
                f"google_ads_insights.campaigns[{idx}] missing required "
                f"fields: {sorted(missing_c)}"
            )

    coverage = ads.get("coverage") or {}
    label_policy = (coverage.get("office_label_policy") or "").lower()
    if "mapping pending" not in label_policy and "pending" not in label_policy:
        _fail(
            "google_ads_insights.coverage.office_label_policy must "
            "explicitly state that office mapping is pending until "
            "remaining customer IDs are linked."
        )

    queue = ads.get("manual_action_queue")
    if not isinstance(queue, list):
        _fail("google_ads_insights.manual_action_queue must be a list.")
    for idx, row in enumerate(queue):
        if not isinstance(row, dict):
            _fail(
                f"google_ads_insights.manual_action_queue[{idx}] is not "
                "an object"
            )
        missing_q = [k for k in REQUIRED_ACTION_QUEUE_FIELDS if k not in row]
        if missing_q:
            _fail(
                f"google_ads_insights.manual_action_queue[{idx}] missing "
                f"required fields: {missing_q}"
            )
        evidence = row.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            _fail(
                f"google_ads_insights.manual_action_queue[{idx}].evidence "
                "must be a non-empty list of strings."
            )
        priority = (row.get("priority") or "").upper()
        if priority not in {"P0", "P1", "P2", "P3"}:
            _fail(
                f"google_ads_insights.manual_action_queue[{idx}].priority "
                f"must be one of P0/P1/P2/P3, got {priority!r}."
            )
        rec = row.get("specific_recommendation")
        if not isinstance(rec, dict):
            _fail(
                f"google_ads_insights.manual_action_queue[{idx}]."
                "specific_recommendation must be an object."
            )
        missing_rec = [
            k for k in REQUIRED_SPECIFIC_RECOMMENDATION_FIELDS
            if k not in rec
        ]
        if missing_rec:
            _fail(
                f"google_ads_insights.manual_action_queue[{idx}]."
                f"specific_recommendation missing required fields: "
                f"{missing_rec}"
            )
        extra_rec = set(rec.keys()) - ALLOWED_SPECIFIC_RECOMMENDATION_FIELDS
        if extra_rec:
            _fail(
                f"google_ads_insights.manual_action_queue[{idx}]."
                f"specific_recommendation has unexpected keys: "
                f"{sorted(extra_rec)}"
            )
        for list_field in (
            "immediate_steps",
            "negative_keyword_review_themes",
        ):
            val = rec.get(list_field)
            if not isinstance(val, list) or not val:
                _fail(
                    f"google_ads_insights.manual_action_queue[{idx}]."
                    f"specific_recommendation['{list_field}'] must be a "
                    "non-empty list of strings."
                )
            for s in val:
                if not isinstance(s, str):
                    _fail(
                        "google_ads_insights.manual_action_queue"
                        f"[{idx}].specific_recommendation['{list_field}'] "
                        "items must be strings."
                    )
        short = row.get("short_specific_recommendation")
        if not isinstance(short, dict):
            _fail(
                f"google_ads_insights.manual_action_queue[{idx}]."
                "short_specific_recommendation must be an object so the "
                "visible card stays short and campaign-specific."
            )
        missing_short = [
            k for k in REQUIRED_SHORT_SPECIFIC_RECOMMENDATION_FIELDS
            if k not in short
        ]
        if missing_short:
            _fail(
                f"google_ads_insights.manual_action_queue[{idx}]."
                f"short_specific_recommendation missing required "
                f"fields: {missing_short}"
            )
        extra_short = (
            set(short.keys()) - ALLOWED_SHORT_SPECIFIC_RECOMMENDATION_FIELDS
        )
        if extra_short:
            _fail(
                f"google_ads_insights.manual_action_queue[{idx}]."
                f"short_specific_recommendation has unexpected keys: "
                f"{sorted(extra_short)}"
            )
        for s_key, s_val in short.items():
            if not isinstance(s_val, str) or not s_val.strip():
                _fail(
                    f"google_ads_insights.manual_action_queue[{idx}]."
                    f"short_specific_recommendation['{s_key}'] must be "
                    "a non-empty string."
                )
        pts = row.get("campaign_specific_points")
        if not isinstance(pts, dict):
            _fail(
                f"google_ads_insights.manual_action_queue[{idx}]."
                "campaign_specific_points must be an object so each card "
                "renders only the unique campaign-specific decisions."
            )
        missing_pts = [
            k for k in REQUIRED_CAMPAIGN_SPECIFIC_POINTS_FIELDS
            if k not in pts
        ]
        if missing_pts:
            _fail(
                f"google_ads_insights.manual_action_queue[{idx}]."
                f"campaign_specific_points missing required fields: "
                f"{missing_pts}"
            )
        extra_pts = (
            set(pts.keys()) - ALLOWED_CAMPAIGN_SPECIFIC_POINTS_FIELDS
        )
        if extra_pts:
            _fail(
                f"google_ads_insights.manual_action_queue[{idx}]."
                f"campaign_specific_points has unexpected keys: "
                f"{sorted(extra_pts)}"
            )
        for p_key, p_val in pts.items():
            if not isinstance(p_val, str) or not p_val.strip():
                _fail(
                    f"google_ads_insights.manual_action_queue[{idx}]."
                    f"campaign_specific_points['{p_key}'] must be a "
                    "non-empty string."
                )

    playbooks = ads.get("priority_playbooks")
    if not isinstance(playbooks, dict):
        _fail(
            "google_ads_insights.priority_playbooks must be an object "
            "containing the shared P0/P1/P2 cards."
        )
    missing_levels = [
        lvl for lvl in REQUIRED_PRIORITY_PLAYBOOK_LEVELS
        if lvl not in playbooks
    ]
    if missing_levels:
        _fail(
            "google_ads_insights.priority_playbooks missing required "
            f"priority levels: {missing_levels}"
        )
    for lvl in REQUIRED_PRIORITY_PLAYBOOK_LEVELS:
        block = playbooks.get(lvl)
        if not isinstance(block, dict):
            _fail(
                f"google_ads_insights.priority_playbooks['{lvl}'] must "
                "be an object."
            )
        missing_pb = [
            k for k in REQUIRED_PRIORITY_PLAYBOOK_FIELDS if k not in block
        ]
        if missing_pb:
            _fail(
                f"google_ads_insights.priority_playbooks['{lvl}'] "
                f"missing required fields: {missing_pb}"
            )
        extra_pb = set(block.keys()) - ALLOWED_PRIORITY_PLAYBOOK_FIELDS
        if extra_pb:
            _fail(
                f"google_ads_insights.priority_playbooks['{lvl}'] has "
                f"unexpected keys: {sorted(extra_pb)}"
            )
        for pb_key, pb_val in block.items():
            if not isinstance(pb_val, str) or not pb_val.strip():
                _fail(
                    f"google_ads_insights.priority_playbooks['{lvl}']"
                    f"['{pb_key}'] must be a non-empty string."
                )

    trends = ads.get("trends")
    if not isinstance(trends, dict):
        _fail("google_ads_insights.trends must be an object.")
    rollup_trend = trends.get("rollup") or {}
    if not isinstance(rollup_trend, dict):
        _fail("google_ads_insights.trends.rollup must be an object.")
    for window_key in ("last_7_days", "last_month"):
        window = rollup_trend.get(window_key) or {}
        if not isinstance(window, dict):
            _fail(
                "google_ads_insights.trends.rollup."
                f"{window_key} must be an object."
            )
        missing_w = [
            k for k in REQUIRED_TREND_WINDOW_FIELDS if k not in window
        ]
        if missing_w:
            _fail(
                "google_ads_insights.trends.rollup."
                f"{window_key} missing required fields: {missing_w}"
            )
    if not isinstance(trends.get("by_office"), list):
        _fail("google_ads_insights.trends.by_office must be a list.")
    by_campaign = trends.get("by_campaign")
    if not isinstance(by_campaign, list):
        _fail("google_ads_insights.trends.by_campaign must be a list.")
    for idx, row in enumerate(by_campaign):
        if not isinstance(row, dict):
            continue
        rec = row.get("specific_recommendation")
        if rec is not None:
            if not isinstance(rec, dict):
                _fail(
                    f"google_ads_insights.trends.by_campaign[{idx}]."
                    "specific_recommendation must be an object when "
                    "present."
                )
            extra = set(rec.keys()) - ALLOWED_SPECIFIC_RECOMMENDATION_FIELDS
            if extra:
                _fail(
                    f"google_ads_insights.trends.by_campaign[{idx}]."
                    f"specific_recommendation has unexpected keys: "
                    f"{sorted(extra)}"
                )
        short = row.get("short_specific_recommendation")
        if short is None:
            continue
        if not isinstance(short, dict):
            _fail(
                f"google_ads_insights.trends.by_campaign[{idx}]."
                "short_specific_recommendation must be an object when "
                "present."
            )
        extra_short = (
            set(short.keys()) - ALLOWED_SHORT_SPECIFIC_RECOMMENDATION_FIELDS
        )
        if extra_short:
            _fail(
                f"google_ads_insights.trends.by_campaign[{idx}]."
                f"short_specific_recommendation has unexpected keys: "
                f"{sorted(extra_short)}"
            )
        for s_key, s_val in short.items():
            if not isinstance(s_val, str) or not s_val.strip():
                _fail(
                    f"google_ads_insights.trends.by_campaign[{idx}]."
                    f"short_specific_recommendation['{s_key}'] must be "
                    "a non-empty string."
                )
        pts = row.get("campaign_specific_points")
        if pts is None:
            continue
        if not isinstance(pts, dict):
            _fail(
                f"google_ads_insights.trends.by_campaign[{idx}]."
                "campaign_specific_points must be an object when present."
            )
        extra_pts = (
            set(pts.keys()) - ALLOWED_CAMPAIGN_SPECIFIC_POINTS_FIELDS
        )
        if extra_pts:
            _fail(
                f"google_ads_insights.trends.by_campaign[{idx}]."
                f"campaign_specific_points has unexpected keys: "
                f"{sorted(extra_pts)}"
            )
        for p_key, p_val in pts.items():
            if not isinstance(p_val, str) or not p_val.strip():
                _fail(
                    f"google_ads_insights.trends.by_campaign[{idx}]."
                    f"campaign_specific_points['{p_key}'] must be a "
                    "non-empty string."
                )

    review_order = ads.get("operator_review_order")
    if not isinstance(review_order, list) or not review_order:
        _fail(
            "google_ads_insights.operator_review_order must be a "
            "non-empty list of strings telling the operator how to "
            "work P0 then P1 then P2 and how to log changes."
        )
    for idx_r, item in enumerate(review_order):
        if not isinstance(item, str) or not item.strip():
            _fail(
                f"google_ads_insights.operator_review_order[{idx_r}] "
                "must be a non-empty string."
            )

    rec_note = ads.get("recommendation_detail_note")
    if not isinstance(rec_note, str) or not rec_note.strip():
        _fail(
            "google_ads_insights.recommendation_detail_note must be a "
            "non-empty string explaining the do-not-remove rule for "
            "action cards."
        )

    ct = ads.get("change_tracking")
    if not isinstance(ct, dict):
        _fail("google_ads_insights.change_tracking must be an object.")
    missing_ct = [k for k in REQUIRED_CHANGE_TRACKING_FIELDS if k not in ct]
    if missing_ct:
        _fail(
            "google_ads_insights.change_tracking missing required "
            f"fields: {missing_ct}"
        )
    limit_text = (ct.get("current_connector_limit") or "").lower()
    if not limit_text:
        _fail(
            "google_ads_insights.change_tracking.current_connector_limit "
            "must describe what the connector cannot mutate today."
        )

    # ---- paid_ads_top_summary ----
    top_summary = ads.get("paid_ads_top_summary")
    if not isinstance(top_summary, dict):
        _fail(
            "google_ads_insights.paid_ads_top_summary must be an object "
            "(blended CPA/CPC/CTR/CVR/spend-per-day/conversions-per-day "
            "headline numbers go here)."
        )
    extra_top = set(top_summary.keys()) - ALLOWED_TOP_SUMMARY_KEYS
    if extra_top:
        _fail(
            "google_ads_insights.paid_ads_top_summary has unexpected "
            f"keys: {sorted(extra_top)}"
        )
    primary_stats = top_summary.get("primary_stats")
    if not isinstance(primary_stats, list) or not primary_stats:
        _fail(
            "google_ads_insights.paid_ads_top_summary.primary_stats must "
            "be a non-empty list of {label, value, benchmark, delta} "
            "rows."
        )
    seen_labels: set[str] = set()
    for idx, stat in enumerate(primary_stats):
        if not isinstance(stat, dict):
            _fail(
                f"google_ads_insights.paid_ads_top_summary.primary_stats"
                f"[{idx}] must be an object."
            )
        missing_keys = [k for k in REQUIRED_PRIMARY_STAT_KEYS if k not in stat]
        if missing_keys:
            _fail(
                f"google_ads_insights.paid_ads_top_summary.primary_stats"
                f"[{idx}] missing required keys: {missing_keys}"
            )
        extra = set(stat.keys()) - ALLOWED_PRIMARY_STAT_KEYS
        if extra:
            _fail(
                f"google_ads_insights.paid_ads_top_summary.primary_stats"
                f"[{idx}] has unexpected keys: {sorted(extra)}"
            )
        for k, v in stat.items():
            if not isinstance(v, str) or not v.strip():
                _fail(
                    "google_ads_insights.paid_ads_top_summary."
                    f"primary_stats[{idx}]['{k}'] must be a non-empty "
                    "string."
                )
        seen_labels.add(stat.get("label", ""))
    missing_labels = REQUIRED_PRIMARY_STAT_LABELS - seen_labels
    if missing_labels:
        _fail(
            "google_ads_insights.paid_ads_top_summary.primary_stats "
            f"missing required blended stat labels: {sorted(missing_labels)}"
        )
    rules = top_summary.get("benchmark_rules")
    if rules is not None:
        if not isinstance(rules, list) or not rules:
            _fail(
                "google_ads_insights.paid_ads_top_summary.benchmark_rules "
                "must be a non-empty list of strings when present."
            )
        for idx_r, r in enumerate(rules):
            if not isinstance(r, str) or not r.strip():
                _fail(
                    "google_ads_insights.paid_ads_top_summary."
                    f"benchmark_rules[{idx_r}] must be a non-empty string."
                )
    bench = top_summary.get("internal_benchmarks")
    if bench is not None:
        if not isinstance(bench, dict):
            _fail(
                "google_ads_insights.paid_ads_top_summary."
                "internal_benchmarks must be an object when present."
            )
        extra_bench = set(bench.keys()) - ALLOWED_INTERNAL_BENCHMARK_KEYS
        if extra_bench:
            _fail(
                "google_ads_insights.paid_ads_top_summary."
                f"internal_benchmarks has unexpected keys: "
                f"{sorted(extra_bench)}"
            )
        for k, v in bench.items():
            if not isinstance(v, (int, float)):
                _fail(
                    "google_ads_insights.paid_ads_top_summary."
                    f"internal_benchmarks['{k}'] must be numeric."
                )

    # ---- conversion_rate_benchmarks ----
    cvr_block = ads.get("conversion_rate_benchmarks")
    if not isinstance(cvr_block, dict):
        _fail(
            "google_ads_insights.conversion_rate_benchmarks must be an "
            "object with a 'by_office' list of office-level CVR rows."
        )
    rows = cvr_block.get("by_office")
    if not isinstance(rows, list) or not rows:
        _fail(
            "google_ads_insights.conversion_rate_benchmarks.by_office "
            "must be a non-empty list."
        )
    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            _fail(
                f"google_ads_insights.conversion_rate_benchmarks."
                f"by_office[{idx}] must be an object."
            )
        missing_cvr = [k for k in REQUIRED_CVR_OFFICE_FIELDS if k not in row]
        if missing_cvr:
            _fail(
                "google_ads_insights.conversion_rate_benchmarks."
                f"by_office[{idx}] missing required fields: {missing_cvr}"
            )
        extra_cvr = set(row.keys()) - ALLOWED_CVR_OFFICE_KEYS
        if extra_cvr:
            _fail(
                "google_ads_insights.conversion_rate_benchmarks."
                f"by_office[{idx}] has unexpected keys: {sorted(extra_cvr)}"
            )
        if not isinstance(row.get("office"), str) or not row["office"].strip():
            _fail(
                "google_ads_insights.conversion_rate_benchmarks."
                f"by_office[{idx}]['office'] must be a non-empty string."
            )
        if not isinstance(row.get("status"), str) or not row["status"].strip():
            _fail(
                "google_ads_insights.conversion_rate_benchmarks."
                f"by_office[{idx}]['status'] must be a non-empty string."
            )

    # ---- ad_group_conversion_benchmarks ----
    ag = ads.get("ad_group_conversion_benchmarks")
    if not isinstance(ag, list) or not ag:
        _fail(
            "google_ads_insights.ad_group_conversion_benchmarks must be "
            "a non-empty list of ad-group-level rows."
        )
    for idx, row in enumerate(ag):
        if not isinstance(row, dict):
            _fail(
                "google_ads_insights.ad_group_conversion_benchmarks"
                f"[{idx}] must be an object."
            )
        missing_ag = [
            k for k in REQUIRED_AD_GROUP_BENCHMARK_FIELDS if k not in row
        ]
        if missing_ag:
            _fail(
                "google_ads_insights.ad_group_conversion_benchmarks"
                f"[{idx}] missing required fields: {missing_ag}"
            )
        extra_ag = set(row.keys()) - ALLOWED_AD_GROUP_BENCHMARK_KEYS
        if extra_ag:
            _fail(
                "google_ads_insights.ad_group_conversion_benchmarks"
                f"[{idx}] has unexpected keys: {sorted(extra_ag)}"
            )
        for str_field in (
            "office", "campaign", "ad_group", "benchmark_status",
            "keyword_focus",
        ):
            v = row.get(str_field)
            if not isinstance(v, str) or not v.strip():
                _fail(
                    "google_ads_insights.ad_group_conversion_benchmarks"
                    f"[{idx}]['{str_field}'] must be a non-empty string."
                )

    # ---- daily_improvement_loop ----
    loop = ads.get("daily_improvement_loop")
    if not isinstance(loop, dict):
        _fail(
            "google_ads_insights.daily_improvement_loop must be an object "
            "rendered at the end of the Paid Ads tab."
        )
    missing_loop = [k for k in REQUIRED_DAILY_LOOP_FIELDS if k not in loop]
    if missing_loop:
        _fail(
            "google_ads_insights.daily_improvement_loop missing required "
            f"fields: {missing_loop}"
        )
    extra_loop = set(loop.keys()) - ALLOWED_DAILY_LOOP_FIELDS
    if extra_loop:
        _fail(
            "google_ads_insights.daily_improvement_loop has unexpected "
            f"keys: {sorted(extra_loop)}"
        )
    if not isinstance(loop.get("title"), str) or not loop["title"].strip():
        _fail(
            "google_ads_insights.daily_improvement_loop.title must be a "
            "non-empty string."
        )
    if (
        not isinstance(loop.get("decision_rule"), str)
        or not loop["decision_rule"].strip()
    ):
        _fail(
            "google_ads_insights.daily_improvement_loop.decision_rule "
            "must be a non-empty string."
        )
    steps = loop.get("steps")
    if not isinstance(steps, list) or not steps:
        _fail(
            "google_ads_insights.daily_improvement_loop.steps must be a "
            "non-empty list of strings."
        )
    for idx_s, s in enumerate(steps):
        if not isinstance(s, str) or not s.strip():
            _fail(
                "google_ads_insights.daily_improvement_loop.steps"
                f"[{idx_s}] must be a non-empty string."
            )

    # ---- office_spend_opportunities ----
    oso = ads.get("office_spend_opportunities")
    if not isinstance(oso, dict):
        _fail(
            "google_ads_insights.office_spend_opportunities must be an "
            "object rendered immediately after the Paid Ads top summary "
            "so an operator can see spend vs opportunities by office."
        )
    missing_oso = [
        k for k in REQUIRED_OFFICE_SPEND_OPP_TOP_KEYS if k not in oso
    ]
    if missing_oso:
        _fail(
            "google_ads_insights.office_spend_opportunities missing "
            f"required fields: {missing_oso}"
        )
    extra_oso = set(oso.keys()) - ALLOWED_OFFICE_SPEND_OPP_TOP_KEYS
    if extra_oso:
        _fail(
            "google_ads_insights.office_spend_opportunities has "
            f"unexpected keys: {sorted(extra_oso)}"
        )
    for num_key in (
        "total_last_30_spend_usd", "total_high_risk_spend_usd",
    ):
        if not isinstance(oso.get(num_key), (int, float)):
            _fail(
                "google_ads_insights.office_spend_opportunities"
                f"['{num_key}'] must be numeric."
            )
    if not isinstance(oso.get("office_inference_note"), str) or not oso[
        "office_inference_note"
    ].strip():
        _fail(
            "google_ads_insights.office_spend_opportunities."
            "office_inference_note must be a non-empty string explaining "
            "how office is inferred from campaign names when missing."
        )
    top_offices = oso.get("top_spend_offices")
    if not isinstance(top_offices, list) or not top_offices:
        _fail(
            "google_ads_insights.office_spend_opportunities."
            "top_spend_offices must be a non-empty list."
        )
    for idx, row in enumerate(top_offices):
        if not isinstance(row, dict):
            _fail(
                "google_ads_insights.office_spend_opportunities."
                f"top_spend_offices[{idx}] must be an object."
            )
        missing = [
            k for k in REQUIRED_OFFICE_SPEND_OPP_TOP_OFFICE_KEYS
            if k not in row
        ]
        if missing:
            _fail(
                "google_ads_insights.office_spend_opportunities."
                f"top_spend_offices[{idx}] missing required fields: {missing}"
            )
        extra = set(row.keys()) - ALLOWED_OFFICE_SPEND_OPP_TOP_OFFICE_KEYS
        if extra:
            _fail(
                "google_ads_insights.office_spend_opportunities."
                f"top_spend_offices[{idx}] has unexpected keys: {sorted(extra)}"
            )
    rows = oso.get("rows")
    if not isinstance(rows, list) or not rows:
        _fail(
            "google_ads_insights.office_spend_opportunities.rows must "
            "be a non-empty list of office rows."
        )
    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            _fail(
                "google_ads_insights.office_spend_opportunities."
                f"rows[{idx}] must be an object."
            )
        missing = [
            k for k in REQUIRED_OFFICE_SPEND_OPP_ROW_KEYS if k not in row
        ]
        if missing:
            _fail(
                "google_ads_insights.office_spend_opportunities."
                f"rows[{idx}] missing required fields: {missing}"
            )
        extra = set(row.keys()) - ALLOWED_OFFICE_SPEND_OPP_ROW_KEYS
        if extra:
            _fail(
                "google_ads_insights.office_spend_opportunities."
                f"rows[{idx}] has unexpected keys: {sorted(extra)}"
            )
        if not isinstance(row.get("office"), str) or not row["office"].strip():
            _fail(
                "google_ads_insights.office_spend_opportunities."
                f"rows[{idx}]['office'] must be a non-empty string."
            )
        for str_field in ("opportunity", "budget_move"):
            v = row.get(str_field)
            if not isinstance(v, str) or not v.strip():
                _fail(
                    "google_ads_insights.office_spend_opportunities."
                    f"rows[{idx}]['{str_field}'] must be a non-empty string."
                )
        # top_ad_group_opportunity is allowed to be an empty string for
        # offices where no specific ad-group opportunity has been
        # identified yet; the dashboard renders "-" in that case.
        v = row.get("top_ad_group_opportunity")
        if not isinstance(v, str):
            _fail(
                "google_ads_insights.office_spend_opportunities."
                f"rows[{idx}]['top_ad_group_opportunity'] must be a string."
            )
        protect = row.get("protect_or_scale_candidates")
        if protect is not None:
            if not isinstance(protect, list):
                _fail(
                    "google_ads_insights.office_spend_opportunities."
                    f"rows[{idx}].protect_or_scale_candidates must be a "
                    "list when present."
                )
            for pidx, p in enumerate(protect):
                if not isinstance(p, dict):
                    _fail(
                        "google_ads_insights.office_spend_opportunities."
                        f"rows[{idx}].protect_or_scale_candidates[{pidx}] "
                        "must be an object."
                    )
                extra_p = set(p.keys()) - ALLOWED_OFFICE_SPEND_OPP_PROTECT_KEYS
                if extra_p:
                    _fail(
                        "google_ads_insights.office_spend_opportunities."
                        f"rows[{idx}].protect_or_scale_candidates[{pidx}] "
                        f"has unexpected keys: {sorted(extra_p)}"
                    )


def check_b2b_reply_detail(snap: dict[str, Any]) -> None:
    rd = snap.get("b2b_reply_detail")
    if rd is None:
        return
    if not isinstance(rd, dict):
        _fail("b2b_reply_detail must be an object")
    leaked = FORBIDDEN_REPLY_DETAIL_KEYS.intersection(rd.keys())
    if leaked:
        _fail(
            "b2b_reply_detail exposes forbidden top-level keys "
            f"{sorted(leaked)}; sender, organization, and raw text "
            "fields must never appear in the public mirror."
        )
    timeline = rd.get("reply_timeline") or []
    if not isinstance(timeline, list):
        _fail("b2b_reply_detail.reply_timeline must be a list")
    allowed = {
        "date",
        "category",
        "classification",
        "public_theme",
        "status",
        "suggested_next_action_public",
    }
    for idx, row in enumerate(timeline):
        if not isinstance(row, dict):
            _fail(f"b2b_reply_detail.reply_timeline[{idx}] is not an object")
        leaked_row = FORBIDDEN_REPLY_DETAIL_KEYS.intersection(row.keys())
        if leaked_row:
            _fail(
                f"b2b_reply_detail.reply_timeline[{idx}] exposes forbidden "
                f"keys {sorted(leaked_row)}."
            )
        extra = set(row.keys()) - allowed
        if extra:
            _fail(
                f"b2b_reply_detail.reply_timeline[{idx}] contains "
                f"unrecognized keys {sorted(extra)}; only "
                f"{sorted(allowed)} are allowed."
            )


def check_keyword_focus(snap: dict[str, Any]) -> None:
    kf = snap.get("google_ads_keyword_focus")
    if kf is None:
        return
    if not isinstance(kf, dict):
        _fail("google_ads_keyword_focus must be an object")
    forbidden_keys = {
        "manager_customer_id",
        "manager_account_id",
        "customer_id",
        "customer_ids",
        "account_id",
        "account_ids",
        "login_customer_id",
        "search_terms",
        "search_term_view",
    }
    leaked = forbidden_keys.intersection(kf.keys())
    if leaked:
        _fail(
            "google_ads_keyword_focus exposes forbidden keys "
            f"{sorted(leaked)}."
        )
    api = kf.get("api_writeback_capability") or {}
    if api and not isinstance(api, dict):
        _fail("api_writeback_capability must be an object when present")


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
    # Email scan: every email-shaped match is a leak unless it appears in
    # ALLOWED_PUBLIC_EMAIL_ADDRESSES (empty by default). The public
    # mirror substitutes safe labels like "Connected Clove sender" and
    # "Internal follow-up only" for operator inboxes; prospect senders
    # are redacted. Any address that slips through is treated as a leak.
    email_re = re.compile(
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
    )
    for m in email_re.finditer(text):
        addr = m.group(0).lower()
        if addr in ALLOWED_PUBLIC_EMAIL_ADDRESSES:
            continue
        findings.append(
            f"{label}: email address leaked in public mirror: {addr}"
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
        check_task_id_redacted(snap)
        check_experiments_redacted(snap)
        check_sources_redacted(snap)
        check_replies_redacted(snap)
        check_latest_batch_redacted(snap)
        check_github_section_redacted(snap)
        check_google_ads_insights(snap)
        check_b2b_reply_detail(snap)
        check_keyword_focus(snap)

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
