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
  4. ``index.html`` exists and contains no forbidden sensitive
     patterns. (The dashboard fetches ``data/snapshot.json`` at
     runtime, so there is no inline snapshot to parity-check.)
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
    "b2b_outbound",
    "next_actions",
    "google_ads_insights",
    "_sanitization",
    # v6 live-daily simple blocks (added 2026-06-30). These power
    # the top-of-page "Live daily — refreshed 5am PT" section and
    # are written by scripts/pull_live_daily.py.
    "paid_ads_simple",
    "gmb_simple",
    "organic_simple",
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
    "weekly_marketing_run_rate",
    "callrail_call_quality",
]

# CallRail call-quality section. Aggregated counts and rates sourced
# from the CallRail Calls API v3 (lead_status, answered, first_call)
# joined to the paid-ads campaign/ad-group layer. Every key in this
# section is a label, count, or rate - raw call records, caller
# numbers/names/emails, and CallRail account/company IDs are rejected
# outright by FORBIDDEN_CALLRAIL_KEYS below.
REQUIRED_CALLRAIL_TOP_KEYS = [
    "title",
    "period",
    "source_note",
    "qualification_note",
    "lead_status_legend",
    "summary_cards",
    "call_outcome_breakdown",
    "office_call_quality",
    "campaign_call_quality",
    "ad_group_call_quality",
    "missed_call_leakage",
    "integration_status",
]
# Optional, additive sections rendered alongside the existing
# CallRail call-quality block: a first-time-caller grid by office, a
# week-over-week and month-over-month trend table by office, and a
# JZ / Joe campaign-cluster focus row.
ALLOWED_CALLRAIL_TOP_KEYS = set(REQUIRED_CALLRAIL_TOP_KEYS + [
    "placement",
    "office_first_time_caller_grid",
    "office_trends",
    "jz_joe_focus",
])

ALLOWED_CALLRAIL_FT_GRID_TOP_KEYS = {"title", "rule", "period", "rows"}
ALLOWED_CALLRAIL_FT_GRID_ROW_KEYS = {
    "office",
    "first_time_callers_last_7d",
    "first_time_callers_prior_7d",
    "wow_delta",
    "first_time_callers_last_30d",
    "first_time_callers_prior_30d",
    "mom_delta",
    "share_of_calls_pct",
    "note",
}

ALLOWED_CALLRAIL_OFFICE_TREND_TOP_KEYS = {"title", "rule", "rows"}
ALLOWED_CALLRAIL_OFFICE_TREND_ROW_KEYS = {
    "office",
    "calls_last_7d",
    "calls_prior_7d",
    "wow_calls_delta_pct",
    "calls_last_30d",
    "calls_prior_30d",
    "mom_calls_delta_pct",
    "qualified_last_7d",
    "qualified_prior_7d",
    "wow_qualified_delta",
    "qualified_last_30d",
    "qualified_prior_30d",
    "mom_qualified_delta",
    "missed_last_7d",
    "missed_prior_7d",
    "wow_missed_delta",
    "trend_call",
}

ALLOWED_CALLRAIL_JZ_JOE_TOP_KEYS = {"title", "rule", "rows"}
ALLOWED_CALLRAIL_JZ_JOE_ROW_KEYS = {
    "label",
    "office",
    "spend_last_7d_usd",
    "calls_last_7d",
    "first_time_callers_last_7d",
    "qualified_calls_last_7d",
    "qualified_rate_pct",
    "missed_calls_last_7d",
    "calls_last_30d",
    "qualified_calls_last_30d",
    "qualified_rate_30d_pct",
    "google_ads_conversions_last_7d",
    "match_basis",
    "recommended_action",
    "scale_hold_fix",
}

REQUIRED_CALLRAIL_SUMMARY_LABELS = {
    "Qualified calls (last 7d)",
    "Qualified-call rate",
    "First-time callers",
    "Answered calls",
    "Missed calls",
    "Qualified-call CPA",
}
ALLOWED_CALLRAIL_SUMMARY_CARD_KEYS = {"label", "value", "basis", "decision"}
REQUIRED_CALLRAIL_SUMMARY_CARD_KEYS = ["label", "value", "basis", "decision"]

ALLOWED_CALLRAIL_LEGEND_KEYS = {"lead_status", "label", "meaning"}
REQUIRED_CALLRAIL_LEGEND_KEYS = list(ALLOWED_CALLRAIL_LEGEND_KEYS)

ALLOWED_CALLRAIL_OUTCOME_TOP_KEYS = {"title", "rule", "rows"}
REQUIRED_CALLRAIL_OUTCOME_TOP_KEYS = list(ALLOWED_CALLRAIL_OUTCOME_TOP_KEYS)
ALLOWED_CALLRAIL_OUTCOME_ROW_KEYS = {"outcome", "count", "share_pct", "note"}
REQUIRED_CALLRAIL_OUTCOME_ROW_KEYS = list(ALLOWED_CALLRAIL_OUTCOME_ROW_KEYS)

ALLOWED_CALLRAIL_OFFICE_TOP_KEYS = {"title", "rule", "rows"}
REQUIRED_CALLRAIL_OFFICE_TOP_KEYS = list(ALLOWED_CALLRAIL_OFFICE_TOP_KEYS)
ALLOWED_CALLRAIL_OFFICE_ROW_KEYS = {
    "office",
    "total_calls",
    "answered_calls",
    "answered_rate_pct",
    "qualified_calls",
    "qualified_rate_pct",
    "first_time_callers",
    "missed_calls",
    "missed_rate_pct",
    "qualified_cpa_usd",
    "status",
    "note",
}
REQUIRED_CALLRAIL_OFFICE_ROW_KEYS = [
    "office",
    "total_calls",
    "qualified_calls",
    "qualified_rate_pct",
    "first_time_callers",
    "missed_calls",
    "status",
]

ALLOWED_CALLRAIL_CAMPAIGN_TOP_KEYS = {"title", "rule", "rows"}
REQUIRED_CALLRAIL_CAMPAIGN_TOP_KEYS = list(ALLOWED_CALLRAIL_CAMPAIGN_TOP_KEYS)
ALLOWED_CALLRAIL_CAMPAIGN_ROW_KEYS = {
    "office",
    "campaign",
    "channel",
    "total_calls",
    "qualified_calls",
    "qualified_rate_pct",
    "first_time_callers",
    "missed_calls",
    "qualified_cpa_usd",
    "recommended_action",
}
REQUIRED_CALLRAIL_CAMPAIGN_ROW_KEYS = [
    "office",
    "campaign",
    "total_calls",
    "qualified_calls",
    "qualified_rate_pct",
    "recommended_action",
]

ALLOWED_CALLRAIL_AD_GROUP_TOP_KEYS = {"title", "rule", "rows"}
REQUIRED_CALLRAIL_AD_GROUP_TOP_KEYS = list(ALLOWED_CALLRAIL_AD_GROUP_TOP_KEYS)
ALLOWED_CALLRAIL_AD_GROUP_ROW_KEYS = {
    "office",
    "campaign",
    "ad_group",
    "qualified_calls",
    "qualified_rate_pct",
    "missed_calls",
    "keyword_focus",
}
REQUIRED_CALLRAIL_AD_GROUP_ROW_KEYS = list(ALLOWED_CALLRAIL_AD_GROUP_ROW_KEYS)

ALLOWED_CALLRAIL_LEAKAGE_TOP_KEYS = {"title", "rule", "totals", "rows"}
REQUIRED_CALLRAIL_LEAKAGE_TOP_KEYS = list(ALLOWED_CALLRAIL_LEAKAGE_TOP_KEYS)
ALLOWED_CALLRAIL_LEAKAGE_TOTALS_KEYS = {
    "missed_calls_last_7d",
    "paid_clicks_lost_estimate",
    "estimated_spend_lost_usd",
    "basis",
}
REQUIRED_CALLRAIL_LEAKAGE_TOTALS_KEYS = list(
    ALLOWED_CALLRAIL_LEAKAGE_TOTALS_KEYS
)
ALLOWED_CALLRAIL_LEAKAGE_ROW_KEYS = {
    "office",
    "missed_calls",
    "peak_window",
    "estimated_spend_lost_usd",
    "next_step",
}
REQUIRED_CALLRAIL_LEAKAGE_ROW_KEYS = list(ALLOWED_CALLRAIL_LEAKAGE_ROW_KEYS)

ALLOWED_CALLRAIL_INTEGRATION_KEYS = {
    "integration",
    "status",
    "public_exposure",
    "private_config_fields",
    "docs_reference",
}
REQUIRED_CALLRAIL_INTEGRATION_KEYS = list(ALLOWED_CALLRAIL_INTEGRATION_KEYS)

# Keys that must never appear anywhere inside callrail_call_quality -
# these are the CallRail-specific identifiers and raw-record fields
# that would re-introduce PII or private account identifiers if they
# ever leaked into the public mirror.
FORBIDDEN_CALLRAIL_KEYS = {
    "account_id",
    "account_ids",
    "callrail_account_id",
    "company_id",
    "company_ids",
    "callrail_company_id",
    "tracker_id",
    "tracker_ids",
    "api_key",
    "api_token",
    "token",
    "api_secret",
    "customer_phone_number",
    "tracking_phone_number",
    "business_phone_number",
    "customer_name",
    "caller_name",
    "customer_email",
    "caller_email",
    "caller_country",
    "caller_city",
    "caller_state",
    "caller_zip",
    "caller_postal_code",
    "recording",
    "recording_url",
    "recording_duration",
    "transcription",
    "transcription_text",
    "transcript",
    "call_highlights",
    "conversation_intelligence",
    "agent_email",
    "agent_name",
    "gclid",
    "gbraid",
    "wbraid",
    "fbclid",
    "calls",
    "call_records",
    "raw_calls",
    "raw_call",
    "raw_call_records",
}

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

# Weekly marketing run-rate. Rendered after the office spend block to
# show projected weekly spend/conversions/calls and the blended
# CPA/CVR/CTR cards, the run-rate decision rules, the office budget
# focus (reduce vs protect/scale), and the daily change review (top
# action rows + log fields + baseline note).
REQUIRED_WMRR_TOP_KEYS = [
    "title",
    "summary_cards",
    "run_rate_rules",
    "office_budget_focus",
    "daily_change_review",
]
ALLOWED_WMRR_TOP_KEYS = set(REQUIRED_WMRR_TOP_KEYS + [
    "period",
    "run_rate_trends",
])
ALLOWED_WMRR_RUN_RATE_TRENDS_TOP_KEYS = {"title", "rule", "rows"}
ALLOWED_WMRR_RUN_RATE_TRENDS_ROW_KEYS = {
    "metric",
    "last_7d",
    "prior_7d",
    "wow_delta",
    "last_30d",
    "prior_30d",
    "mom_delta",
    "basis",
    "decision",
}
REQUIRED_WMRR_SUMMARY_LABELS = {
    "Projected weekly spend",
    "Projected weekly conversions",
    "Projected weekly calls",
    "Blended CPA",
    "Blended CVR",
    "Blended CTR",
}
ALLOWED_WMRR_SUMMARY_CARD_KEYS = {"label", "value", "basis", "decision"}
REQUIRED_WMRR_SUMMARY_CARD_KEYS = ["label", "value"]
REQUIRED_WMRR_OFFICE_FOCUS_KEYS = [
    "reduce_or_reallocate_first",
    "protect_or_scale_after_quality_check",
    "rule",
]
ALLOWED_WMRR_OFFICE_FOCUS_KEYS = set(REQUIRED_WMRR_OFFICE_FOCUS_KEYS)
ALLOWED_WMRR_OFFICE_ROW_KEYS = {
    "office",
    "current_spend_30d",
    "high_risk_spend_30d",
    "last_7_cvr_pct",
    "last_7_cpa_usd",
    "p0_p1_p2",
    "today_change_needed",
    "why",
    "top_ad_group_opportunity",
    "run_rate_call",
}
REQUIRED_WMRR_DCR_KEYS = [
    "title",
    "today_should_do",
    "fields_to_log_each_day",
    "status_note",
]
ALLOWED_WMRR_DCR_KEYS = set(REQUIRED_WMRR_DCR_KEYS)
ALLOWED_WMRR_DCR_ROW_KEYS = {
    "priority",
    "office",
    "campaign",
    "change_to_make",
    "benchmark_reason",
    "keyword_or_ad_group_focus",
    "success_check",
    "tomorrow_learning",
}
REQUIRED_WMRR_DCR_ROW_KEYS = list(ALLOWED_WMRR_DCR_ROW_KEYS)

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
    # HubSpot CMS / private config artifacts.
    (
        r"\bpat-na1-[0-9a-f-]{8,}",
        "HubSpot private app token (pat-na1-...)",
    ),
    (
        r"/cron_tracking/",
        "Private cron_tracking directory path leaked",
    ),
    (
        r"hubspot_cms_config\.json",
        "Private HubSpot CMS config filename leaked",
    ),
    (
        r"\bhubspot[_-]?(?:portal|hub)[_-]?id\b",
        "HubSpot portal/hub id reference leaked",
    ),
    # GA4 measurement / property ids should never appear in the public mirror.
    (
        r"\bG-[A-Z0-9]{8,}\b",
        "GA4 measurement id (G-XXXXXXXX)",
    ),
    (
        r"\bproperties/\d{6,}\b",
        "GA4 property id (properties/NNN...)",
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

    # ---- weekly_marketing_run_rate ----
    wmrr = ads.get("weekly_marketing_run_rate")
    if not isinstance(wmrr, dict):
        _fail(
            "google_ads_insights.weekly_marketing_run_rate must be an "
            "object rendered after the office spend block (projected "
            "weekly spend/conversions/calls + run-rate rules + office "
            "budget focus + daily change review)."
        )
    missing_wmrr = [k for k in REQUIRED_WMRR_TOP_KEYS if k not in wmrr]
    if missing_wmrr:
        _fail(
            "google_ads_insights.weekly_marketing_run_rate missing "
            f"required fields: {missing_wmrr}"
        )
    extra_wmrr = set(wmrr.keys()) - ALLOWED_WMRR_TOP_KEYS
    if extra_wmrr:
        _fail(
            "google_ads_insights.weekly_marketing_run_rate has "
            f"unexpected keys: {sorted(extra_wmrr)}"
        )
    cards = wmrr.get("summary_cards")
    if not isinstance(cards, list) or not cards:
        _fail(
            "google_ads_insights.weekly_marketing_run_rate.summary_cards "
            "must be a non-empty list of {label, value, basis, decision} "
            "rows covering the projected weekly spend/conversions/calls "
            "and the blended CPA/CVR/CTR."
        )
    seen_card_labels: set[str] = set()
    for idx, card in enumerate(cards):
        if not isinstance(card, dict):
            _fail(
                "google_ads_insights.weekly_marketing_run_rate."
                f"summary_cards[{idx}] must be an object."
            )
        missing_keys = [
            k for k in REQUIRED_WMRR_SUMMARY_CARD_KEYS if k not in card
        ]
        if missing_keys:
            _fail(
                "google_ads_insights.weekly_marketing_run_rate."
                f"summary_cards[{idx}] missing required keys: {missing_keys}"
            )
        extra = set(card.keys()) - ALLOWED_WMRR_SUMMARY_CARD_KEYS
        if extra:
            _fail(
                "google_ads_insights.weekly_marketing_run_rate."
                f"summary_cards[{idx}] has unexpected keys: {sorted(extra)}"
            )
        for k, v in card.items():
            if not isinstance(v, str) or not v.strip():
                _fail(
                    "google_ads_insights.weekly_marketing_run_rate."
                    f"summary_cards[{idx}]['{k}'] must be a non-empty "
                    "string."
                )
        seen_card_labels.add(card.get("label", ""))
    missing_labels = REQUIRED_WMRR_SUMMARY_LABELS - seen_card_labels
    if missing_labels:
        _fail(
            "google_ads_insights.weekly_marketing_run_rate.summary_cards "
            f"missing required labels: {sorted(missing_labels)}"
        )
    rules = wmrr.get("run_rate_rules")
    if not isinstance(rules, list) or not rules:
        _fail(
            "google_ads_insights.weekly_marketing_run_rate.run_rate_rules "
            "must be a non-empty list of strings explaining the "
            "conversions/CVR/CPA/booked-call decision rules."
        )
    for idx_r, r in enumerate(rules):
        if not isinstance(r, str) or not r.strip():
            _fail(
                "google_ads_insights.weekly_marketing_run_rate."
                f"run_rate_rules[{idx_r}] must be a non-empty string."
            )
    ofb = wmrr.get("office_budget_focus")
    if not isinstance(ofb, dict):
        _fail(
            "google_ads_insights.weekly_marketing_run_rate."
            "office_budget_focus must be an object."
        )
    missing_ofb = [
        k for k in REQUIRED_WMRR_OFFICE_FOCUS_KEYS if k not in ofb
    ]
    if missing_ofb:
        _fail(
            "google_ads_insights.weekly_marketing_run_rate."
            f"office_budget_focus missing required fields: {missing_ofb}"
        )
    extra_ofb = set(ofb.keys()) - ALLOWED_WMRR_OFFICE_FOCUS_KEYS
    if extra_ofb:
        _fail(
            "google_ads_insights.weekly_marketing_run_rate."
            f"office_budget_focus has unexpected keys: {sorted(extra_ofb)}"
        )
    if not isinstance(ofb.get("rule"), str) or not ofb["rule"].strip():
        _fail(
            "google_ads_insights.weekly_marketing_run_rate."
            "office_budget_focus.rule must be a non-empty string."
        )
    for list_key in (
        "reduce_or_reallocate_first",
        "protect_or_scale_after_quality_check",
    ):
        rows = ofb.get(list_key)
        if not isinstance(rows, list):
            _fail(
                "google_ads_insights.weekly_marketing_run_rate."
                f"office_budget_focus['{list_key}'] must be a list."
            )
        for idx, row in enumerate(rows):
            if not isinstance(row, dict):
                _fail(
                    "google_ads_insights.weekly_marketing_run_rate."
                    f"office_budget_focus['{list_key}'][{idx}] must be "
                    "an object."
                )
            extra = set(row.keys()) - ALLOWED_WMRR_OFFICE_ROW_KEYS
            if extra:
                _fail(
                    "google_ads_insights.weekly_marketing_run_rate."
                    f"office_budget_focus['{list_key}'][{idx}] has "
                    f"unexpected keys: {sorted(extra)}"
                )
            if (
                not isinstance(row.get("office"), str)
                or not row["office"].strip()
            ):
                _fail(
                    "google_ads_insights.weekly_marketing_run_rate."
                    f"office_budget_focus['{list_key}'][{idx}]['office'] "
                    "must be a non-empty string."
                )
    dcr = wmrr.get("daily_change_review")
    if not isinstance(dcr, dict):
        _fail(
            "google_ads_insights.weekly_marketing_run_rate."
            "daily_change_review must be an object."
        )
    missing_dcr = [k for k in REQUIRED_WMRR_DCR_KEYS if k not in dcr]
    if missing_dcr:
        _fail(
            "google_ads_insights.weekly_marketing_run_rate."
            f"daily_change_review missing required fields: {missing_dcr}"
        )
    extra_dcr = set(dcr.keys()) - ALLOWED_WMRR_DCR_KEYS
    if extra_dcr:
        _fail(
            "google_ads_insights.weekly_marketing_run_rate."
            f"daily_change_review has unexpected keys: {sorted(extra_dcr)}"
        )
    today = dcr.get("today_should_do")
    if not isinstance(today, list) or not today:
        _fail(
            "google_ads_insights.weekly_marketing_run_rate."
            "daily_change_review.today_should_do must be a non-empty "
            "list of priority/office/campaign/change rows."
        )
    for idx, row in enumerate(today):
        if not isinstance(row, dict):
            _fail(
                "google_ads_insights.weekly_marketing_run_rate."
                f"daily_change_review.today_should_do[{idx}] must be an "
                "object."
            )
        missing = [
            k for k in REQUIRED_WMRR_DCR_ROW_KEYS if k not in row
        ]
        if missing:
            _fail(
                "google_ads_insights.weekly_marketing_run_rate."
                f"daily_change_review.today_should_do[{idx}] missing "
                f"required fields: {sorted(missing)}"
            )
        extra = set(row.keys()) - ALLOWED_WMRR_DCR_ROW_KEYS
        if extra:
            _fail(
                "google_ads_insights.weekly_marketing_run_rate."
                f"daily_change_review.today_should_do[{idx}] has "
                f"unexpected keys: {sorted(extra)}"
            )
        for k, v in row.items():
            if not isinstance(v, str) or not v.strip():
                _fail(
                    "google_ads_insights.weekly_marketing_run_rate."
                    f"daily_change_review.today_should_do[{idx}]['{k}'] "
                    "must be a non-empty string."
                )
    fields = dcr.get("fields_to_log_each_day")
    if not isinstance(fields, list) or not fields:
        _fail(
            "google_ads_insights.weekly_marketing_run_rate."
            "daily_change_review.fields_to_log_each_day must be a "
            "non-empty list of strings."
        )
    for idx_f, f in enumerate(fields):
        if not isinstance(f, str) or not f.strip():
            _fail(
                "google_ads_insights.weekly_marketing_run_rate."
                f"daily_change_review.fields_to_log_each_day[{idx_f}] "
                "must be a non-empty string."
            )
    if (
        not isinstance(dcr.get("status_note"), str)
        or not dcr["status_note"].strip()
    ):
        _fail(
            "google_ads_insights.weekly_marketing_run_rate."
            "daily_change_review.status_note must be a non-empty string "
            "explaining the dated-snapshot baseline."
        )

    # ---- run_rate_trends (optional) ----
    rrt = wmrr.get("run_rate_trends")
    if rrt is not None:
        if not isinstance(rrt, dict):
            _fail(
                "google_ads_insights.weekly_marketing_run_rate."
                "run_rate_trends must be an object when present."
            )
        extra_rrt = set(rrt.keys()) - ALLOWED_WMRR_RUN_RATE_TRENDS_TOP_KEYS
        if extra_rrt:
            _fail(
                "google_ads_insights.weekly_marketing_run_rate."
                f"run_rate_trends has unexpected keys: {sorted(extra_rrt)}"
            )
        rrt_rows = rrt.get("rows")
        if rrt_rows is not None:
            if not isinstance(rrt_rows, list):
                _fail(
                    "google_ads_insights.weekly_marketing_run_rate."
                    "run_rate_trends.rows must be a list when present."
                )
            for idx, row in enumerate(rrt_rows):
                if not isinstance(row, dict):
                    _fail(
                        "google_ads_insights.weekly_marketing_run_rate."
                        f"run_rate_trends.rows[{idx}] must be an object."
                    )
                extra_r = (
                    set(row.keys()) - ALLOWED_WMRR_RUN_RATE_TRENDS_ROW_KEYS
                )
                if extra_r:
                    _fail(
                        "google_ads_insights.weekly_marketing_run_rate."
                        f"run_rate_trends.rows[{idx}] has unexpected keys: "
                        f"{sorted(extra_r)}"
                    )

    # ---- callrail_call_quality ----
    check_callrail_call_quality(ads)


def _scan_callrail_for_forbidden(node: Any, path: str) -> None:
    """Walk the callrail block and reject CallRail-specific identifier
    or raw-record keys at any depth."""
    if isinstance(node, dict):
        leaked = FORBIDDEN_CALLRAIL_KEYS.intersection(node.keys())
        if leaked:
            _fail(
                f"google_ads_insights.callrail_call_quality{path} exposes "
                f"forbidden CallRail keys {sorted(leaked)}; raw call "
                "records, account/company IDs, tokens, caller PII, and "
                "Google Ads click IDs must never appear in the public "
                "mirror."
            )
        for k, v in node.items():
            _scan_callrail_for_forbidden(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, item in enumerate(node):
            _scan_callrail_for_forbidden(item, f"{path}[{i}]")


def check_callrail_call_quality(ads: dict[str, Any]) -> None:
    cr = ads.get("callrail_call_quality")
    if not isinstance(cr, dict):
        _fail(
            "google_ads_insights.callrail_call_quality must be an object "
            "rendered under the Paid Ads tab. Aggregated CallRail call "
            "quality (qualified by lead_status, first-time callers, "
            "answered/missed, qualified-call CPA) is required so the "
            "public dashboard can surface call-conversion enrichment "
            "without exposing raw call records."
        )
    missing = [k for k in REQUIRED_CALLRAIL_TOP_KEYS if k not in cr]
    if missing:
        _fail(
            "google_ads_insights.callrail_call_quality missing required "
            f"fields: {missing}"
        )
    extra = set(cr.keys()) - ALLOWED_CALLRAIL_TOP_KEYS
    if extra:
        _fail(
            "google_ads_insights.callrail_call_quality has unexpected "
            f"keys: {sorted(extra)}"
        )

    # Reject CallRail-specific identifiers / raw-record keys anywhere
    # inside this section.
    _scan_callrail_for_forbidden(cr, "")

    legend = cr.get("lead_status_legend")
    if not isinstance(legend, list) or not legend:
        _fail(
            "google_ads_insights.callrail_call_quality.lead_status_legend "
            "must be a non-empty list explaining the CallRail lead_status "
            "values that count as qualified."
        )
    for idx, row in enumerate(legend):
        if not isinstance(row, dict):
            _fail(
                "google_ads_insights.callrail_call_quality."
                f"lead_status_legend[{idx}] must be an object."
            )
        missing = [
            k for k in REQUIRED_CALLRAIL_LEGEND_KEYS if k not in row
        ]
        if missing:
            _fail(
                "google_ads_insights.callrail_call_quality."
                f"lead_status_legend[{idx}] missing required fields: "
                f"{missing}"
            )
        extra = set(row.keys()) - ALLOWED_CALLRAIL_LEGEND_KEYS
        if extra:
            _fail(
                "google_ads_insights.callrail_call_quality."
                f"lead_status_legend[{idx}] has unexpected keys: "
                f"{sorted(extra)}"
            )

    cards = cr.get("summary_cards")
    if not isinstance(cards, list) or not cards:
        _fail(
            "google_ads_insights.callrail_call_quality.summary_cards must "
            "be a non-empty list of {label, value, basis, decision} rows."
        )
    seen_labels: set[str] = set()
    for idx, card in enumerate(cards):
        if not isinstance(card, dict):
            _fail(
                "google_ads_insights.callrail_call_quality."
                f"summary_cards[{idx}] must be an object."
            )
        missing = [
            k for k in REQUIRED_CALLRAIL_SUMMARY_CARD_KEYS if k not in card
        ]
        if missing:
            _fail(
                "google_ads_insights.callrail_call_quality."
                f"summary_cards[{idx}] missing required keys: {missing}"
            )
        extra = set(card.keys()) - ALLOWED_CALLRAIL_SUMMARY_CARD_KEYS
        if extra:
            _fail(
                "google_ads_insights.callrail_call_quality."
                f"summary_cards[{idx}] has unexpected keys: {sorted(extra)}"
            )
        for k, v in card.items():
            if not isinstance(v, str) or not v.strip():
                _fail(
                    "google_ads_insights.callrail_call_quality."
                    f"summary_cards[{idx}]['{k}'] must be a non-empty string."
                )
        seen_labels.add(card.get("label", ""))
    missing_labels = REQUIRED_CALLRAIL_SUMMARY_LABELS - seen_labels
    if missing_labels:
        _fail(
            "google_ads_insights.callrail_call_quality.summary_cards "
            f"missing required labels: {sorted(missing_labels)}"
        )

    outcome = cr.get("call_outcome_breakdown")
    if not isinstance(outcome, dict):
        _fail(
            "google_ads_insights.callrail_call_quality."
            "call_outcome_breakdown must be an object."
        )
    missing = [
        k for k in REQUIRED_CALLRAIL_OUTCOME_TOP_KEYS if k not in outcome
    ]
    if missing:
        _fail(
            "google_ads_insights.callrail_call_quality."
            f"call_outcome_breakdown missing required fields: {missing}"
        )
    extra = set(outcome.keys()) - ALLOWED_CALLRAIL_OUTCOME_TOP_KEYS
    if extra:
        _fail(
            "google_ads_insights.callrail_call_quality."
            f"call_outcome_breakdown has unexpected keys: {sorted(extra)}"
        )
    for idx, row in enumerate(outcome.get("rows") or []):
        if not isinstance(row, dict):
            _fail(
                "google_ads_insights.callrail_call_quality."
                f"call_outcome_breakdown.rows[{idx}] must be an object."
            )
        missing = [
            k for k in REQUIRED_CALLRAIL_OUTCOME_ROW_KEYS if k not in row
        ]
        if missing:
            _fail(
                "google_ads_insights.callrail_call_quality."
                f"call_outcome_breakdown.rows[{idx}] missing required "
                f"fields: {missing}"
            )
        extra = set(row.keys()) - ALLOWED_CALLRAIL_OUTCOME_ROW_KEYS
        if extra:
            _fail(
                "google_ads_insights.callrail_call_quality."
                f"call_outcome_breakdown.rows[{idx}] has unexpected keys: "
                f"{sorted(extra)}"
            )

    def _check_table(
        section_key: str,
        required_top: list[str],
        allowed_top: set[str],
        required_row: list[str],
        allowed_row: set[str],
    ) -> None:
        block = cr.get(section_key)
        if not isinstance(block, dict):
            _fail(
                f"google_ads_insights.callrail_call_quality.{section_key} "
                "must be an object."
            )
        missing = [k for k in required_top if k not in block]
        if missing:
            _fail(
                f"google_ads_insights.callrail_call_quality.{section_key} "
                f"missing required fields: {missing}"
            )
        extra = set(block.keys()) - allowed_top
        if extra:
            _fail(
                f"google_ads_insights.callrail_call_quality.{section_key} "
                f"has unexpected keys: {sorted(extra)}"
            )
        rows = block.get("rows")
        if not isinstance(rows, list) or not rows:
            _fail(
                f"google_ads_insights.callrail_call_quality.{section_key}"
                ".rows must be a non-empty list."
            )
        for idx, row in enumerate(rows):
            if not isinstance(row, dict):
                _fail(
                    f"google_ads_insights.callrail_call_quality."
                    f"{section_key}.rows[{idx}] must be an object."
                )
            missing_r = [k for k in required_row if k not in row]
            if missing_r:
                _fail(
                    f"google_ads_insights.callrail_call_quality."
                    f"{section_key}.rows[{idx}] missing required fields: "
                    f"{missing_r}"
                )
            extra_r = set(row.keys()) - allowed_row
            if extra_r:
                _fail(
                    f"google_ads_insights.callrail_call_quality."
                    f"{section_key}.rows[{idx}] has unexpected keys: "
                    f"{sorted(extra_r)}"
                )

    _check_table(
        "office_call_quality",
        REQUIRED_CALLRAIL_OFFICE_TOP_KEYS,
        ALLOWED_CALLRAIL_OFFICE_TOP_KEYS,
        REQUIRED_CALLRAIL_OFFICE_ROW_KEYS,
        ALLOWED_CALLRAIL_OFFICE_ROW_KEYS,
    )
    _check_table(
        "campaign_call_quality",
        REQUIRED_CALLRAIL_CAMPAIGN_TOP_KEYS,
        ALLOWED_CALLRAIL_CAMPAIGN_TOP_KEYS,
        REQUIRED_CALLRAIL_CAMPAIGN_ROW_KEYS,
        ALLOWED_CALLRAIL_CAMPAIGN_ROW_KEYS,
    )
    _check_table(
        "ad_group_call_quality",
        REQUIRED_CALLRAIL_AD_GROUP_TOP_KEYS,
        ALLOWED_CALLRAIL_AD_GROUP_TOP_KEYS,
        REQUIRED_CALLRAIL_AD_GROUP_ROW_KEYS,
        ALLOWED_CALLRAIL_AD_GROUP_ROW_KEYS,
    )

    leakage = cr.get("missed_call_leakage")
    if not isinstance(leakage, dict):
        _fail(
            "google_ads_insights.callrail_call_quality.missed_call_leakage "
            "must be an object."
        )
    missing = [
        k for k in REQUIRED_CALLRAIL_LEAKAGE_TOP_KEYS if k not in leakage
    ]
    if missing:
        _fail(
            "google_ads_insights.callrail_call_quality.missed_call_leakage "
            f"missing required fields: {missing}"
        )
    extra = set(leakage.keys()) - ALLOWED_CALLRAIL_LEAKAGE_TOP_KEYS
    if extra:
        _fail(
            "google_ads_insights.callrail_call_quality.missed_call_leakage "
            f"has unexpected keys: {sorted(extra)}"
        )
    totals = leakage.get("totals")
    if not isinstance(totals, dict):
        _fail(
            "google_ads_insights.callrail_call_quality.missed_call_leakage"
            ".totals must be an object."
        )
    missing_t = [
        k for k in REQUIRED_CALLRAIL_LEAKAGE_TOTALS_KEYS if k not in totals
    ]
    if missing_t:
        _fail(
            "google_ads_insights.callrail_call_quality.missed_call_leakage"
            f".totals missing required fields: {missing_t}"
        )
    extra_t = set(totals.keys()) - ALLOWED_CALLRAIL_LEAKAGE_TOTALS_KEYS
    if extra_t:
        _fail(
            "google_ads_insights.callrail_call_quality.missed_call_leakage"
            f".totals has unexpected keys: {sorted(extra_t)}"
        )
    for idx, row in enumerate(leakage.get("rows") or []):
        if not isinstance(row, dict):
            _fail(
                "google_ads_insights.callrail_call_quality."
                f"missed_call_leakage.rows[{idx}] must be an object."
            )
        missing_r = [
            k for k in REQUIRED_CALLRAIL_LEAKAGE_ROW_KEYS if k not in row
        ]
        if missing_r:
            _fail(
                "google_ads_insights.callrail_call_quality."
                f"missed_call_leakage.rows[{idx}] missing required fields: "
                f"{missing_r}"
            )
        extra_r = set(row.keys()) - ALLOWED_CALLRAIL_LEAKAGE_ROW_KEYS
        if extra_r:
            _fail(
                "google_ads_insights.callrail_call_quality."
                f"missed_call_leakage.rows[{idx}] has unexpected keys: "
                f"{sorted(extra_r)}"
            )

    integ = cr.get("integration_status")
    if not isinstance(integ, dict):
        _fail(
            "google_ads_insights.callrail_call_quality.integration_status "
            "must be an object."
        )
    missing = [
        k for k in REQUIRED_CALLRAIL_INTEGRATION_KEYS if k not in integ
    ]
    if missing:
        _fail(
            "google_ads_insights.callrail_call_quality.integration_status "
            f"missing required fields: {missing}"
        )
    extra = set(integ.keys()) - ALLOWED_CALLRAIL_INTEGRATION_KEYS
    if extra:
        _fail(
            "google_ads_insights.callrail_call_quality.integration_status "
            f"has unexpected keys: {sorted(extra)}"
        )
    cfg = integ.get("private_config_fields")
    if not isinstance(cfg, list) or not cfg:
        _fail(
            "google_ads_insights.callrail_call_quality.integration_status"
            ".private_config_fields must be a non-empty list of placeholder "
            "config field names. Real credentials must never appear here."
        )
    for f in cfg:
        if not isinstance(f, str) or not f.strip():
            _fail(
                "google_ads_insights.callrail_call_quality."
                "integration_status.private_config_fields entries must be "
                "non-empty strings."
            )

    # Optional, additive blocks. If present, validate they only carry
    # allowed shapes - aggregated counts/rates and labels.
    def _check_optional_block(
        section_key: str,
        allowed_top: set[str],
        allowed_row: set[str],
    ) -> None:
        block = cr.get(section_key)
        if block is None:
            return
        if not isinstance(block, dict):
            _fail(
                f"google_ads_insights.callrail_call_quality.{section_key} "
                "must be an object when present."
            )
        extra = set(block.keys()) - allowed_top
        if extra:
            _fail(
                f"google_ads_insights.callrail_call_quality.{section_key} "
                f"has unexpected keys: {sorted(extra)}"
            )
        rows = block.get("rows")
        if rows is not None:
            if not isinstance(rows, list):
                _fail(
                    f"google_ads_insights.callrail_call_quality."
                    f"{section_key}.rows must be a list when present."
                )
            for idx, row in enumerate(rows):
                if not isinstance(row, dict):
                    _fail(
                        f"google_ads_insights.callrail_call_quality."
                        f"{section_key}.rows[{idx}] must be an object."
                    )
                extra_r = set(row.keys()) - allowed_row
                if extra_r:
                    _fail(
                        f"google_ads_insights.callrail_call_quality."
                        f"{section_key}.rows[{idx}] has unexpected keys: "
                        f"{sorted(extra_r)}"
                    )

    _check_optional_block(
        "office_first_time_caller_grid",
        ALLOWED_CALLRAIL_FT_GRID_TOP_KEYS,
        ALLOWED_CALLRAIL_FT_GRID_ROW_KEYS,
    )
    _check_optional_block(
        "office_trends",
        ALLOWED_CALLRAIL_OFFICE_TREND_TOP_KEYS,
        ALLOWED_CALLRAIL_OFFICE_TREND_ROW_KEYS,
    )
    _check_optional_block(
        "jz_joe_focus",
        ALLOWED_CALLRAIL_JZ_JOE_TOP_KEYS,
        ALLOWED_CALLRAIL_JZ_JOE_ROW_KEYS,
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


ALLOWED_AUTOMATION_ITEM_KEYS = {
    "id",
    "name",
    "purpose",
    "status",
    "provider",
    "provider_status",
    "send_policy_enabled",
    "apply_mode",
    "last_run_at_utc",
    "counters",
    "by_office",
    "by_source",
    "sample_template_public",
    "compliance_notes",
    "blockers",
}
REQUIRED_AUTOMATION_ITEM_KEYS = [
    "id",
    "name",
    "status",
    "provider",
    "provider_status",
    "send_policy_enabled",
    "apply_mode",
    "counters",
    "blockers",
]
ALLOWED_AUTOMATION_COUNTER_KEYS = {
    "backlog",
    "eligible",
    "sent_today",
    "replies_pending",
    "booked",
    "booked_rate_pct",
    "sheet_rows_modified",
    "sms_messages_sent",
}
REQUIRED_AUTOMATION_COUNTER_KEYS = list(ALLOWED_AUTOMATION_COUNTER_KEYS)
ALLOWED_AUTOMATION_OFFICE_ROW_KEYS = {
    "office",
    "backlog",
    "eligible",
    "sent_today",
    "replies_pending",
    "booked",
}
ALLOWED_AUTOMATION_SOURCE_ROW_KEYS = {
    "source_type",
    "backlog",
    "eligible",
    "sent_today",
    "replies_pending",
    "booked",
}

# Public Automations block must never carry recipient PII or private
# identifiers. This list mirrors the public-snapshot contract for the
# automations area: aggregate counts only.
FORBIDDEN_AUTOMATION_KEYS = {
    "phone",
    "phone_number",
    "phone_numbers",
    "email",
    "emails",
    "first_name",
    "last_name",
    "patient_name",
    "lead_name",
    "recipient_name",
    "row",
    "row_number",
    "row_index",
    "sheet_id",
    "spreadsheet_id",
    "sheets_id",
    "raw_message",
    "raw_messages",
    "message_body",
    "api_key",
    "api_token",
    "openphone_api_key",
    "openphone_token",
    "phone_number_id",
    "message_id",
    "messageid",
    "openphone_message_id",
    "patnum",
    "patnums",
    "patient_id",
    "od_token",
    "opendental_token",
    "booking_link",
    "private_link",
    "private_links",
    "config_path",
    "private_path",
}

# Forbidden substrings that must not appear anywhere in the rendered
# automations block as values. These catch raw OpenPhone keys, the
# Optimization line phone_number_id shape, private paths, and the
# spreadsheet/openphone host names.
FORBIDDEN_AUTOMATION_VALUE_SUBSTRINGS = [
    "/home/user/workspace/cron_tracking",
    "lead_sms_config",
    "docs.google.com",
    "spreadsheets/d/",
    "api.openphone.com",
]


def _scan_automations_for_forbidden(node: Any, path: str) -> None:
    if isinstance(node, dict):
        leaked = FORBIDDEN_AUTOMATION_KEYS.intersection(node.keys())
        if leaked:
            _fail(
                f"automations{path} exposes forbidden keys "
                f"{sorted(leaked)}; aggregate-only counters allowed."
            )
        for k, v in node.items():
            _scan_automations_for_forbidden(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, item in enumerate(node):
            _scan_automations_for_forbidden(item, f"{path}[{i}]")
    elif isinstance(node, str):
        lowered = node.lower()
        for tok in FORBIDDEN_AUTOMATION_VALUE_SUBSTRINGS:
            if tok in lowered:
                _fail(
                    f"automations{path} contains forbidden substring "
                    f"'{tok}'."
                )


def check_automations(snap: dict[str, Any]) -> None:
    block = snap.get("automations")
    if block is None:
        # Automations is optional today; the dashboard tolerates its
        # absence. If present, it must be fully sanitized.
        return
    if not isinstance(block, dict):
        _fail("automations must be an object when present.")
    items = block.get("items")
    if not isinstance(items, list):
        _fail("automations.items must be a list.")
    _scan_automations_for_forbidden(block, "")
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            _fail(f"automations.items[{idx}] must be an object.")
        missing = [
            k for k in REQUIRED_AUTOMATION_ITEM_KEYS if k not in item
        ]
        if missing:
            _fail(
                f"automations.items[{idx}] missing required keys: {missing}"
            )
        extra = set(item.keys()) - ALLOWED_AUTOMATION_ITEM_KEYS
        if extra:
            _fail(
                f"automations.items[{idx}] has unexpected keys: "
                f"{sorted(extra)}"
            )
        counters = item.get("counters") or {}
        if not isinstance(counters, dict):
            _fail(f"automations.items[{idx}].counters must be an object.")
        missing_c = [
            k for k in REQUIRED_AUTOMATION_COUNTER_KEYS if k not in counters
        ]
        if missing_c:
            _fail(
                f"automations.items[{idx}].counters missing keys: "
                f"{missing_c}"
            )
        extra_c = set(counters.keys()) - ALLOWED_AUTOMATION_COUNTER_KEYS
        if extra_c:
            _fail(
                f"automations.items[{idx}].counters has unexpected keys: "
                f"{sorted(extra_c)}"
            )
        for ck, cv in counters.items():
            if not isinstance(cv, (int, float)):
                _fail(
                    f"automations.items[{idx}].counters['{ck}'] must be "
                    f"numeric, got {type(cv).__name__}."
                )
        for list_key, allowed in (
            ("by_office", ALLOWED_AUTOMATION_OFFICE_ROW_KEYS),
            ("by_source", ALLOWED_AUTOMATION_SOURCE_ROW_KEYS),
        ):
            rows = item.get(list_key) or []
            if not isinstance(rows, list):
                _fail(
                    f"automations.items[{idx}].{list_key} must be a list."
                )
            for ridx, r in enumerate(rows):
                if not isinstance(r, dict):
                    _fail(
                        f"automations.items[{idx}].{list_key}[{ridx}] "
                        "must be an object."
                    )
                extra_r = set(r.keys()) - allowed
                if extra_r:
                    _fail(
                        f"automations.items[{idx}].{list_key}[{ridx}] "
                        f"has unexpected keys: {sorted(extra_r)}"
                    )


# GA4 is configured in the private analytics_config. These stale setup
# phrases must not appear in the public mirror — replace them with the
# specific action ("key events not firing", "conversion tracking needs
# mapping") so operators see what to do next, not "we need GA4".
# Paid-ads dynamic action system. Validates the shape of the new
# prioritized action queue, writeback tiers, and daily learning fields.
# Aggregate only — no Google Ads customer IDs, login customer IDs,
# raw search terms, raw call records, GCLIDs, tokens, sheet IDs,
# CallRail IDs, or private paths.

ALLOWED_PAID_ADS_ACTION_TOP_KEYS = {
    "title", "as_of", "summary", "writeback_tiers", "tier_counts",
    "top_5_by_opportunity", "queue", "daily_learning",
    "blocker_for_direct_writes",
}

ALLOWED_PAID_ADS_QUEUE_ENTRY_KEYS = {
    "category", "priority", "office", "label", "action",
    "owner", "status", "writeback_tier", "can_execute_now",
    "blocker", "impact_metric", "estimated_opportunity_usd",
    "estimated_waste_share_pct",
}

ALLOWED_PAID_ADS_WRITEBACK_TIERS = {
    "executable_now",
    "mutation_ready_when_write_access_available",
    "approval_required_higher_risk",
}

ALLOWED_PAID_ADS_QUEUE_STATUSES = {
    "queued", "monitoring", "active_live", "pending", "blocked",
    "in_progress", "completed",
}

ALLOWED_PAID_ADS_CATEGORIES = {
    "waste_to_cut",
    "budget_to_protect_or_scale",
    "keyword_focus",
    "negative_keyword_candidate",
    "office_or_campaign_opportunity",
    "tracking_offline_conversions",
}

REQUIRED_PAID_ADS_LEARNING_FIELDS = {
    "metric_before", "metric_after", "metric_deltas",
    "suppressed_repeats_this_run", "self_rating_note",
    "previous_actions_recorded",
}


def check_paid_ads_action_system(snap: dict[str, Any]) -> None:
    block = snap.get("paid_ads_action_system")
    if not isinstance(block, dict):
        _fail(
            "snapshot.json paid_ads_action_system must be an object "
            "containing the dynamic action queue."
        )
    extra = set(block.keys()) - ALLOWED_PAID_ADS_ACTION_TOP_KEYS
    if extra:
        _fail(
            "paid_ads_action_system has unexpected keys: "
            f"{sorted(extra)}"
        )
    for required in (
        "title", "as_of", "summary", "writeback_tiers",
        "tier_counts", "top_5_by_opportunity", "queue",
        "daily_learning", "blocker_for_direct_writes",
    ):
        if required not in block:
            _fail(
                f"paid_ads_action_system missing required field: "
                f"{required!r}"
            )
    tiers = block.get("writeback_tiers")
    if not isinstance(tiers, dict):
        _fail("paid_ads_action_system.writeback_tiers must be an object.")
    missing_tiers = ALLOWED_PAID_ADS_WRITEBACK_TIERS - set(tiers.keys())
    if missing_tiers:
        _fail(
            "paid_ads_action_system.writeback_tiers missing required "
            f"tier keys: {sorted(missing_tiers)}"
        )
    counts = block.get("tier_counts")
    if not isinstance(counts, dict):
        _fail("paid_ads_action_system.tier_counts must be an object.")
    for tk in ALLOWED_PAID_ADS_WRITEBACK_TIERS:
        if not isinstance(counts.get(tk), int):
            _fail(
                f"paid_ads_action_system.tier_counts[{tk!r}] must be an integer."
            )
    queue = block.get("queue")
    if not isinstance(queue, list):
        _fail("paid_ads_action_system.queue must be a list.")
    for idx, q in enumerate(queue):
        if not isinstance(q, dict):
            _fail(
                f"paid_ads_action_system.queue[{idx}] must be an object."
            )
        extra_q = set(q.keys()) - ALLOWED_PAID_ADS_QUEUE_ENTRY_KEYS
        if extra_q:
            _fail(
                f"paid_ads_action_system.queue[{idx}] has unexpected "
                f"keys: {sorted(extra_q)}"
            )
        if q.get("category") not in ALLOWED_PAID_ADS_CATEGORIES:
            _fail(
                f"paid_ads_action_system.queue[{idx}].category "
                f"{q.get('category')!r} not in allowlist."
            )
        if (q.get("priority") or "").upper() not in {"P0", "P1", "P2", "P3"}:
            _fail(
                f"paid_ads_action_system.queue[{idx}].priority must be "
                "one of P0/P1/P2/P3."
            )
        if q.get("status") not in ALLOWED_PAID_ADS_QUEUE_STATUSES:
            _fail(
                f"paid_ads_action_system.queue[{idx}].status "
                f"{q.get('status')!r} not in allowlist."
            )
        if q.get("writeback_tier") not in ALLOWED_PAID_ADS_WRITEBACK_TIERS:
            _fail(
                f"paid_ads_action_system.queue[{idx}].writeback_tier "
                f"{q.get('writeback_tier')!r} not in allowlist."
            )
        if not isinstance(q.get("can_execute_now"), bool):
            _fail(
                f"paid_ads_action_system.queue[{idx}].can_execute_now "
                "must be a boolean."
            )
        for required in ("office", "label", "action", "owner", "impact_metric"):
            if not q.get(required) or not isinstance(q.get(required), str):
                _fail(
                    f"paid_ads_action_system.queue[{idx}] missing required "
                    f"string field: {required!r}"
                )
    top5 = block.get("top_5_by_opportunity")
    if not isinstance(top5, list):
        _fail("paid_ads_action_system.top_5_by_opportunity must be a list.")
    if len(top5) > 5:
        _fail(
            "paid_ads_action_system.top_5_by_opportunity must contain at "
            "most 5 entries."
        )
    learning = block.get("daily_learning")
    if not isinstance(learning, dict):
        _fail("paid_ads_action_system.daily_learning must be an object.")
    missing_l = REQUIRED_PAID_ADS_LEARNING_FIELDS - set(learning.keys())
    if missing_l:
        _fail(
            "paid_ads_action_system.daily_learning missing required "
            f"fields: {sorted(missing_l)}"
        )


# Stale paid-ads boilerplate that must not appear in the public mirror.
# Replace blanket "connect Google Ads" / "set up tracking" copy with the
# specific action queue tier the user should look at.
STALE_PAID_ADS_PHRASES = [
    "Connect Google Ads",
    "connect Google Ads",
    "Google Ads not connected",
    "Set up Google Ads",
    "set up Google Ads",
    "Google Ads needs setup",
    "Google Ads pending setup",
    "Static recommendation list",
    "Manual recommendation list",
]


def check_no_stale_paid_ads_copy(
    snapshot_text: str, html_text: str
) -> None:
    for phrase in STALE_PAID_ADS_PHRASES:
        if phrase in snapshot_text:
            _fail(
                f"data/snapshot.json: stale paid-ads boilerplate: {phrase!r}"
            )


# Private Google Ads / CallRail identifier shapes that must never appear
# in the public mirror.
PAID_ADS_PRIVATE_ID_PATTERNS = [
    (r"\bcustomers/\d{6,}\b", "Google Ads customer resource path"),
    (r"\blogin[_-]?customer[_-]?id\s*[:=]?\s*['\"]?\d{6,}",
     "Google Ads login_customer_id assignment"),
    (r"\bmanager[_-]?customer[_-]?id\s*[:=]?\s*['\"]?\d{6,}",
     "Google Ads manager_customer_id assignment"),
    (r"\bcompany[_-]?id\s*[:=]?\s*['\"]?\d{6,}",
     "CallRail company_id assignment"),
    (r"\baccount[_-]?id\s*[:=]?\s*['\"]?\d{6,}",
     "CallRail account_id assignment"),
    (r"\bsheet[_-]?id\s*[:=]?\s*['\"]?[A-Za-z0-9_\-]{20,}",
     "Sheet ID assignment"),
]


def check_paid_ads_private_ids(snapshot_text: str) -> None:
    for pat, desc in PAID_ADS_PRIVATE_ID_PATTERNS:
        m = re.search(pat, snapshot_text)
        if m:
            _fail(
                f"data/snapshot.json contains private paid-ads/CallRail/"
                f"sheet identifier ({desc}): {m.group(0)!r}"
            )


STALE_GA4_SETUP_PHRASES = [
    "GA4 property needed",
    "Provide GA4 property ID",
    "Provide GA4 property id",
    "awaiting property ID",
    "awaiting property id",
    "GA4 not configured",
    "GA4 missing",
    "GA4 needed",
    "Connect GA4",
    "connect GA4",
    "Need GA4",
    "need GA4",
    # GA4 is connected and form_submit is mapped as a key event. The
    # remaining work is site-side instrumentation for call_click and
    # appt_booked, not setup. Block stale "key events not firing"
    # blanket copy too.
    "GA4 property ID",
    "GA4 measurement ID",
    "property ID needed",
    "connect Google Analytics",
    "Connect Google Analytics",
    "Google Analytics not configured",
    "key events not firing",
    "conversion tracking needs mapping",
]


# Private analytics-config / GA4 identifier shapes that must never
# appear in the public mirror. The dashboard exposes only that
# form_submit is mapped; the property ID / key-event ID / measurement
# ID stay private.
GA4_PRIVATE_ID_PATTERNS = [
    (r"\bG-[A-Z0-9]{8,}\b", "GA4 measurement ID (G-XXXX)"),
    (r"\bproperties/\d{6,}\b", "GA4 property path (properties/NNNNNN)"),
    (
        r"\bproperty[_-]?id\s*[:=]\s*['\"]?\d{6,}",
        "GA4 property ID assignment",
    ),
    (
        r"\bkey[_-]?event[_-]?id\s*[:=]\s*['\"]?\d{6,}",
        "GA4 key-event ID assignment",
    ),
]


REQUIRED_ACTION_SYSTEM_IDS = {
    "hubspot-cms-metadata",
    "ga4-form-submit-mapping",
    "tracking-stack",
    "gmb-new-negative-alerts",
    "gmb-review-recovery",
    "gmb-review-weekly-trend",
    "google-ads-dynamic-optimization",
}

ALLOWED_ACTION_SYSTEM_ENTRY_KEYS = {
    "id", "name", "status", "next_action", "last_action",
    "last_action_at", "impact_metric", "impact_samples",
    "live_writes", "draft_writes", "key_events", "blocker",
    "blockers_detail",
    # Accelerated organic SEO context on the hubspot-cms-metadata row.
    "growth_mode", "accelerated", "proposals", "small_content_proposals",
    "cooldown_days", "why_no_prior_change",
    # Paid-ads dynamic optimization tier counts.
    "tier_counts",
}

ALLOWED_ACTION_SYSTEM_STATUSES = {
    "active_live", "active_draft", "active_dry_run",
    "blocked", "pending", "idle", "ok",
}

ALLOWED_GA4_KEY_EVENT_KEYS = {
    "name", "status", "scope", "mapped_on",
    "impact_metric", "next_action",
}

ALLOWED_GA4_KEY_EVENT_STATUSES = {
    "mapped_as_key_event",
    "instrumentation_pending",
}


def check_action_system(snap: dict[str, Any]) -> None:
    auto = snap.get("automations")
    if not isinstance(auto, dict):
        return
    asys = auto.get("action_system")
    if asys is None:
        _fail(
            "automations.action_system is required so the dashboard "
            "can render the action-oriented summary."
        )
    if not isinstance(asys, dict):
        _fail("automations.action_system must be an object.")
    actions = asys.get("actions")
    if not isinstance(actions, list) or not actions:
        _fail(
            "automations.action_system.actions must be a non-empty list."
        )
    seen_ids: set[str] = set()
    for idx, a in enumerate(actions):
        if not isinstance(a, dict):
            _fail(
                f"automations.action_system.actions[{idx}] must be an object."
            )
        extra = set(a.keys()) - ALLOWED_ACTION_SYSTEM_ENTRY_KEYS
        if extra:
            _fail(
                f"automations.action_system.actions[{idx}] has unexpected "
                f"keys: {sorted(extra)}"
            )
        for required in (
            "id", "name", "status", "next_action",
            "last_action", "impact_metric",
        ):
            if not a.get(required):
                _fail(
                    f"automations.action_system.actions[{idx}] missing "
                    f"required field: {required!r}"
                )
        if a["status"] not in ALLOWED_ACTION_SYSTEM_STATUSES:
            _fail(
                f"automations.action_system.actions[{idx}].status "
                f"{a['status']!r} not in allowlist."
            )
        seen_ids.add(a["id"])
        kes = a.get("key_events")
        if kes is not None:
            if not isinstance(kes, list):
                _fail(
                    f"automations.action_system.actions[{idx}].key_events "
                    "must be a list."
                )
            for kidx, k in enumerate(kes):
                if not isinstance(k, dict):
                    _fail(
                        f"automations.action_system.actions[{idx}]"
                        f".key_events[{kidx}] must be an object."
                    )
                extra_k = set(k.keys()) - ALLOWED_GA4_KEY_EVENT_KEYS
                if extra_k:
                    _fail(
                        f"automations.action_system.actions[{idx}]"
                        f".key_events[{kidx}] has unexpected keys: "
                        f"{sorted(extra_k)}"
                    )
                if k.get("status") not in ALLOWED_GA4_KEY_EVENT_STATUSES:
                    _fail(
                        f"automations.action_system.actions[{idx}]"
                        f".key_events[{kidx}].status not in allowlist."
                    )
    missing = REQUIRED_ACTION_SYSTEM_IDS - seen_ids
    if missing:
        _fail(
            "automations.action_system.actions is missing required "
            f"entries: {sorted(missing)}"
        )


# -----------------------------------------------------------------------
# Review weekly trend block
#
# The Reviews tab now ships a weekly low-review trend section that the
# user can click to drill into per-office, per-week details. The trend
# block has strict shape + no-PII rules:
#   * No reviewer names / staff names / patient names / profile links
#   * No GBP IDs / raw review IDs / account/location IDs
#   * No email bodies / private paths / connector tokens
#   * Office labels only from the public allowlist
#   * Sanitized snippets only (<= 240 chars), stripped of names/URLs/IDs
# -----------------------------------------------------------------------

REQUIRED_TREND_TOP_KEYS = {
    "title", "generated_at", "anchor_date", "current_week_start",
    "windows", "totals", "weekly_buckets", "office_trends",
    "action_queue", "response_tracking", "privacy_note",
}
REQUIRED_TREND_TOTALS_KEYS = {
    "last_7d_low", "prior_7d_low", "delta_low", "last_7d_avg_rating",
    "unresolved_open", "oldest_open_age_days",
}
ALLOWED_TREND_BUCKET_KEYS = {
    "week_start", "week_end", "low_count", "total_offices_with_low",
    "avg_rating",
}
ALLOWED_OFFICE_TREND_KEYS = {
    "office", "last_7d_low", "prior_7d_low", "delta", "trend_direction",
    "last_7d_avg", "last_28d_avg", "weekly_buckets", "common_themes",
    "response_signals", "open_followups", "oldest_open_age_days",
    "prior_action", "next_action", "drilldown",
}
ALLOWED_DRILL_WEEK_KEYS = {
    "week_start", "week_end", "low_count", "replied_count",
    "themes", "sanitized_snippets", "action_status",
}
ALLOWED_DRILL_SNIPPET_KEYS = {
    "date", "rating", "snippet", "replied", "themes",
}
ALLOWED_TREND_DIRECTIONS = {"up", "down", "flat"}

# Keys that must never appear inside the weekly-trend block. These are
# the shapes that would leak reviewer identity, raw connector state, or
# private routing config into the public mirror.
FORBIDDEN_TREND_KEYS = {
    "reviewer", "reviewer_name", "reviewer_id", "reviewerId",
    "profile_url", "profile_link", "profileLink", "profilePhotoUrl",
    "review_id", "reviewId", "name_id", "nameId",
    "account", "accounts", "location", "locations", "location_id",
    "locationId", "place_id", "placeId",
    "office_email", "cc", "recipients", "recipient_email",
    "email_body", "raw_text", "body", "Body", "raw_review",
    "private_path", "config_path",
}

# Patterns that would betray names/profile URLs even if the keys are
# innocuous. Tested against every string we encounter in the trend
# block.
TREND_FORBIDDEN_VALUE_PATTERNS: list[tuple[str, str]] = [
    (r"https?://(?:maps|business)\.google\.\S+", "Google Maps/Business URL in trend"),
    (r"\baccounts/\d+\b", "GBP account ID in trend"),
    (r"\blocations/\d+\b", "GBP location ID in trend"),
    (r"\breviews/[A-Za-z0-9_-]{6,}\b", "GBP review ID in trend"),
    (r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b", "Email address in trend"),
    (r"/home/user/workspace/[A-Za-z0-9_\-/]+", "Private filesystem path in trend"),
    (r"\bcron_tracking/[A-Za-z0-9_\-]+", "Private cron_tracking path in trend"),
]


def _scan_trend_keys(node: Any, path: str, issues: list[str]) -> None:
    if isinstance(node, dict):
        leaked = FORBIDDEN_TREND_KEYS.intersection(node.keys())
        if leaked:
            issues.append(
                f"{path}: forbidden key(s) in weekly trend block: "
                f"{sorted(leaked)}"
            )
        for k, v in node.items():
            _scan_trend_keys(v, f"{path}.{k}", issues)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _scan_trend_keys(v, f"{path}[{i}]", issues)
    elif isinstance(node, str):
        for pat, desc in TREND_FORBIDDEN_VALUE_PATTERNS:
            if re.search(pat, node):
                issues.append(f"{path}: {desc} (value contained: {pat!r})")


def check_review_weekly_trends(snap: dict[str, Any]) -> None:
    gmb = snap.get("gmb_insights")
    if not isinstance(gmb, dict):
        return
    trends = gmb.get("low_review_weekly_trends")
    if trends is None:
        # Optional during first run; refresh writes it when office_rows
        # are present. Don't fail just because it's missing.
        return
    if not isinstance(trends, dict):
        _fail(
            "gmb_insights.low_review_weekly_trends must be an object."
        )
    missing = REQUIRED_TREND_TOP_KEYS - set(trends.keys())
    if missing:
        _fail(
            "gmb_insights.low_review_weekly_trends missing required "
            f"fields: {sorted(missing)}"
        )
    totals = trends.get("totals") or {}
    miss_t = REQUIRED_TREND_TOTALS_KEYS - set(totals.keys())
    if miss_t:
        _fail(
            "gmb_insights.low_review_weekly_trends.totals missing "
            f"fields: {sorted(miss_t)}"
        )
    buckets = trends.get("weekly_buckets") or []
    if not isinstance(buckets, list) or len(buckets) < 1:
        _fail(
            "gmb_insights.low_review_weekly_trends.weekly_buckets must "
            "be a non-empty list."
        )
    for idx, b in enumerate(buckets):
        if not isinstance(b, dict):
            _fail(
                f"low_review_weekly_trends.weekly_buckets[{idx}] must "
                "be an object."
            )
        extra = set(b.keys()) - ALLOWED_TREND_BUCKET_KEYS
        if extra:
            _fail(
                f"low_review_weekly_trends.weekly_buckets[{idx}] has "
                f"unexpected keys: {sorted(extra)}"
            )
    office_trends = trends.get("office_trends") or []
    if not isinstance(office_trends, list):
        _fail(
            "gmb_insights.low_review_weekly_trends.office_trends must "
            "be a list."
        )
    for idx, ot in enumerate(office_trends):
        if not isinstance(ot, dict):
            _fail(
                f"low_review_weekly_trends.office_trends[{idx}] must "
                "be an object."
            )
        extra = set(ot.keys()) - ALLOWED_OFFICE_TREND_KEYS
        if extra:
            _fail(
                f"low_review_weekly_trends.office_trends[{idx}] has "
                f"unexpected keys: {sorted(extra)}"
            )
        if ot.get("trend_direction") not in ALLOWED_TREND_DIRECTIONS:
            _fail(
                f"low_review_weekly_trends.office_trends[{idx}]"
                f".trend_direction not in allowlist."
            )
        drilldown = ot.get("drilldown") or []
        if not isinstance(drilldown, list):
            _fail(
                f"low_review_weekly_trends.office_trends[{idx}]"
                ".drilldown must be a list."
            )
        for dx, wk in enumerate(drilldown):
            if not isinstance(wk, dict):
                _fail(
                    f"low_review_weekly_trends.office_trends[{idx}]"
                    f".drilldown[{dx}] must be an object."
                )
            extra = set(wk.keys()) - ALLOWED_DRILL_WEEK_KEYS
            if extra:
                _fail(
                    f"low_review_weekly_trends.office_trends[{idx}]"
                    f".drilldown[{dx}] has unexpected keys: "
                    f"{sorted(extra)}"
                )
            snippets = wk.get("sanitized_snippets") or []
            if not isinstance(snippets, list):
                _fail(
                    "low_review_weekly_trends.*.drilldown[*]"
                    ".sanitized_snippets must be a list."
                )
            for sx, s in enumerate(snippets):
                if not isinstance(s, dict):
                    _fail(
                        "low_review_weekly_trends drilldown snippet "
                        "entries must be objects."
                    )
                extra = set(s.keys()) - ALLOWED_DRILL_SNIPPET_KEYS
                if extra:
                    _fail(
                        "low_review_weekly_trends drilldown snippet "
                        f"has unexpected keys: {sorted(extra)}"
                    )
                snip_text = s.get("snippet")
                if not isinstance(snip_text, str):
                    _fail(
                        "low_review_weekly_trends drilldown snippet "
                        "must have a string snippet field."
                    )
                if len(snip_text) > 240:
                    _fail(
                        "low_review_weekly_trends drilldown snippet "
                        f"too long ({len(snip_text)} chars; max 240)."
                    )
    rt = trends.get("response_tracking") or {}
    if not isinstance(rt, dict) or rt.get("label") != "response signals":
        _fail(
            "low_review_weekly_trends.response_tracking must declare "
            "label='response signals' (we never claim a definitive reply rate)."
        )
    if "no email bodies" not in (rt.get("basis") or ""):
        _fail(
            "low_review_weekly_trends.response_tracking.basis must "
            "state that no email bodies are used."
        )

    issues: list[str] = []
    _scan_trend_keys(trends, "low_review_weekly_trends", issues)
    if issues:
        _fail(
            "low_review_weekly_trends contains forbidden content:\n  - "
            + "\n  - ".join(issues)
        )


def check_ga4_form_submit_mapped(snap: dict[str, Any]) -> None:
    """GA4 form_submit must be marked as a mapped key event."""
    auto = snap.get("automations", {}) or {}
    asys = auto.get("action_system", {}) or {}
    found = False
    for a in asys.get("actions", []) or []:
        if a.get("id") == "ga4-form-submit-mapping":
            for k in a.get("key_events", []) or []:
                if (
                    k.get("name") == "form_submit"
                    and k.get("status") == "mapped_as_key_event"
                ):
                    found = True
                    break
    if not found:
        _fail(
            "automations.action_system must declare form_submit as a "
            "mapped GA4 key event."
        )
    organic = snap.get("organic_insights", {}) or {}
    ga4_row = None
    for c in organic.get("connector_status", []) or []:
        if str(c.get("integration", "")).lower().startswith(
            "google analytics"
        ):
            ga4_row = c
            break
    if ga4_row is None:
        _fail(
            "organic_insights.connector_status missing Google Analytics row."
        )
    status_text = str(ga4_row.get("status", ""))
    action_text = str(ga4_row.get("action", ""))
    if (
        "form_submit" not in status_text
        and "form_submit" not in action_text
    ):
        _fail(
            "organic_insights GA4 connector_status must reference "
            "form_submit (mapped key event)."
        )


def check_ga4_private_ids(snapshot_text: str) -> None:
    findings: list[str] = []
    for pattern, description in GA4_PRIVATE_ID_PATTERNS:
        for m in re.finditer(pattern, snapshot_text):
            findings.append(
                f"data/snapshot.json: {description} "
                f"(matched: {m.group(0)!r})"
            )
    if findings:
        joined = "\n  - ".join(findings)
        _fail(
            "GA4 private identifier leak detected. Property ID, "
            "measurement ID, and key-event IDs must stay private.\n"
            f"  - {joined}"
        )


def check_no_stale_ga4_setup_copy(
    snapshot_text: str, html_text: str
) -> None:
    findings: list[str] = []
    for phrase in STALE_GA4_SETUP_PHRASES:
        if phrase in snapshot_text:
            findings.append(
                f"data/snapshot.json: stale GA4 setup phrase: {phrase!r}"
            )
        if phrase in html_text:
            findings.append(
                f"index.html: stale GA4 setup phrase: {phrase!r}"
            )
    if findings:
        joined = "\n  - ".join(findings)
        _fail(
            "Stale GA4 setup copy detected. GA4 is configured; use "
            "action-oriented copy like 'key events not firing' or "
            "'conversion tracking needs mapping' instead.\n"
            f"  - {joined}"
        )


REQUIRED_ACCELERATED_ORGANIC_KEYS = [
    "title",
    "growth_mode",
    "accelerated",
    "publish_mode_pretty",
    "cooldown_days",
    "max_changes_cap",
    "why_no_prior_change",
    "what_changed_now",
    "next_opportunity_queue",
    "impact_metrics_watched",
    "live_writes",
    "draft_writes",
    "proposals",
    "small_content_proposals",
    "actions",
    "safety_tiers",
]

# Keys that must NEVER appear inside accelerated_organic (private IDs,
# tokens, raw API payloads). This is a belt-and-suspenders check on top
# of the forbidden-pattern scan that runs against the full snapshot.
FORBIDDEN_ACCELERATED_ORGANIC_KEYS = {
    "token", "access_token", "refresh_token", "portal_id", "portalId",
    "hub_id", "hubId", "objectId", "object_id", "internal_id",
    "currentlyPublished", "publishDate", "archivedAt",
}


def _scan_for_forbidden_keys(node: Any, forbidden: set[str], path: str) -> list[str]:
    issues: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k in forbidden:
                issues.append(f"{path}.{k}: forbidden key in public block")
            issues.extend(_scan_for_forbidden_keys(v, forbidden, f"{path}.{k}"))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            issues.extend(_scan_for_forbidden_keys(v, forbidden, f"{path}[{i}]"))
    return issues


def check_accelerated_organic(snap: dict[str, Any]) -> None:
    """Validate shape + sanitization of the accelerated_organic block.

    The block is optional (older snapshots may not have it), but when
    present it must have the documented shape and contain no private
    HubSpot identifiers, tokens, or raw API payload keys.
    """
    block = snap.get("accelerated_organic")
    if block is None:
        return
    if not isinstance(block, dict):
        _fail("accelerated_organic must be an object")
    missing = [k for k in REQUIRED_ACCELERATED_ORGANIC_KEYS if k not in block]
    if missing:
        _fail(f"accelerated_organic missing required keys: {missing}")
    if not isinstance(block.get("accelerated"), bool):
        _fail("accelerated_organic.accelerated must be a boolean")
    for nk in ("cooldown_days", "max_changes_cap", "live_writes",
               "draft_writes", "proposals", "small_content_proposals"):
        if not isinstance(block.get(nk), int):
            _fail(f"accelerated_organic.{nk} must be int")
    for lk in ("next_opportunity_queue", "impact_metrics_watched",
               "actions"):
        if not isinstance(block.get(lk), list):
            _fail(f"accelerated_organic.{lk} must be a list")
    if not isinstance(block.get("safety_tiers"), dict):
        _fail("accelerated_organic.safety_tiers must be an object")
    issues = _scan_for_forbidden_keys(
        block, FORBIDDEN_ACCELERATED_ORGANIC_KEYS, "$.accelerated_organic"
    )
    if issues:
        _fail("accelerated_organic contains forbidden keys: " + "; ".join(issues))


def main() -> int:
    print("Validating public snapshot ...")
    try:
        snap = load_snapshot_json()
        check_required_sections(snap)
        check_task_id_redacted(snap)
        check_sources_redacted(snap)
        check_github_section_redacted(snap)
        check_google_ads_insights(snap)
        check_keyword_focus(snap)
        check_automations(snap)
        check_action_system(snap)
        check_paid_ads_action_system(snap)
        check_review_weekly_trends(snap)
        check_ga4_form_submit_mapped(snap)
        check_accelerated_organic(snap)

        snapshot_text = DATA_FILE.read_text(encoding="utf-8")
        if not INDEX_HTML.exists():
            _fail(f"Missing required file: {INDEX_HTML}")
        html_text = INDEX_HTML.read_text(encoding="utf-8")

        check_no_forbidden_patterns(snapshot_text, html_text)
        check_no_stale_ga4_setup_copy(snapshot_text, html_text)
        check_no_stale_paid_ads_copy(snapshot_text, html_text)
        check_ga4_private_ids(snapshot_text)
        check_paid_ads_private_ids(snapshot_text)
    except ValidationError as exc:
        print(f"FAIL: {exc}")
        return 1

    print("OK: snapshot.json parses and contains all required sections.")
    print("OK: index.html present and free of forbidden patterns.")
    print("OK: no forbidden sensitive patterns detected.")
    print("Public snapshot is safe to publish.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
