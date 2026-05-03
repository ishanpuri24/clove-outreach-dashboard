"""Reference build script for the public Clove outreach dashboard mirror.

This script is published in the public mirror repo for transparency.
The authoritative builder lives in the private operations repo and
reads the daily-outreach scheduled task's state files. The public
mirror is produced by:

  1. Running the private builder to generate the unsanitized snapshot.
  2. Passing the result through ``sanitize_for_public()`` below to
     strip recipient-level data, reply senders, free-text summaries,
     the internal Google Sheet identifier, and Google Ads
     manager/customer account identifiers.
  3. Writing the sanitized snapshot to ``data/snapshot.json`` and
     re-injecting the JSON between the ``/* SNAPSHOT_START */`` and
     ``/* SNAPSHOT_END */`` markers in ``index.html``.

The public dashboard never receives raw replies, recipient emails,
the operations sheet URL, or any Google Ads account/customer IDs.
See ``README.md`` for the full list of fields that are dropped.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

DASHBOARD_DIR = Path(__file__).resolve().parents[1]
DATA_FILE = DASHBOARD_DIR / "data" / "snapshot.json"
INDEX_HTML = DASHBOARD_DIR / "index.html"

EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)
SENDER_LABEL = "Connected Clove sender"
INTERNAL_FOLLOWUP_LABEL = "Internal follow-up only"

# Public label used in place of the internal scheduled-task identifier.
PUBLIC_TASK_LABEL = "daily-refresh"

# Internal scheduler/task identifier shape (8-char lowercase hex). Any
# value of this shape in the public mirror is treated as an internal
# task ID leak and replaced with PUBLIC_TASK_LABEL.
SCHEDULER_TASK_ID_RE = re.compile(r"^[0-9a-f]{8}$")

# Internal experiment / follow-up tracking IDs. The public mirror
# renders the title/action/hypothesis instead of the tracker code.
EXPERIMENT_ID_RE = re.compile(r"\bEXP-\d{2,}\b")
FOLLOWUP_ID_RE = re.compile(r"\bFU-\d{2,}\b")


def _strip_internal_tracker_ids(text: Any) -> Any:
    """Remove EXP-XX / FU-XX style internal tracker IDs from prose."""
    if not isinstance(text, str):
        return text
    # Collapse comma/and-joined runs of tracker IDs first so prose like
    # "after EXP-01 and EXP-03 complete" does not become "after a
    # related experiment and a related experiment complete".
    multi_exp = re.compile(
        r"\bEXP-\d{2,}(?:\s*(?:,|and)\s*EXP-\d{2,})+\b"
    )
    multi_fu = re.compile(
        r"\bFU-\d{2,}(?:\s*(?:,|and)\s*FU-\d{2,})+\b"
    )
    cleaned = multi_exp.sub("the related experiments", text)
    cleaned = multi_fu.sub("the related follow-ups", cleaned)
    cleaned = EXPERIMENT_ID_RE.sub("a related experiment", cleaned)
    cleaned = FOLLOWUP_ID_RE.sub("a related follow-up", cleaned)
    return cleaned


def _strip_emails(text: str) -> str:
    """Replace any email address in ``text`` with a safe label.

    The public mirror must never expose operator or prospect email
    addresses. Operator inboxes used for outreach are folded under the
    "Connected Clove sender" label; CC/follow-up inboxes become
    "Internal follow-up only". Any other email-shaped match is replaced
    with "(redacted)" so prose lines that referenced raw addresses
    remain readable.
    """
    if not isinstance(text, str) or "@" not in text:
        return text

    def _replace(match: re.Match[str]) -> str:
        addr = match.group(0).lower()
        # Heuristic: the operator's outbound sender vs internal CC.
        # Both fall into safe labels; anything else is redacted.
        return "(redacted)"

    return EMAIL_RE.sub(_replace, text)


def _sanitize_guardrail_line(text: Any) -> Any:
    if not isinstance(text, str) or "@" not in text:
        return text
    cleaned = text
    # Replace inboxes used for initial outreach.
    cleaned = re.sub(
        r"(?:from\s+)[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
        f"from the {SENDER_LABEL.lower()}",
        cleaned,
    )
    # Replace inboxes used as CC / internal follow-up.
    cleaned = re.sub(
        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\s+is\s+CC'?d",
        f"{INTERNAL_FOLLOWUP_LABEL} is used",
        cleaned,
    )
    # Final pass: if any email survived, redact it.
    cleaned = EMAIL_RE.sub("(redacted)", cleaned)
    return cleaned


def sanitize_for_public(snap: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``snap`` with sensitive prospect/PII data removed.

    Public dashboard exposes only aggregate metrics, category and
    status summaries, and operating guardrails. Individual prospect
    emails, reply sender names and addresses, free-text summaries,
    the operations Google Sheet identifier, and recipient-level batch
    rows are removed.
    """
    out = json.loads(json.dumps(snap))

    task = out.get("task")
    if isinstance(task, dict):
        if "sender" in task:
            sender_val = task.get("sender") or ""
            if EMAIL_RE.search(sender_val):
                task["sender"] = SENDER_LABEL
        # The private builder ships the scheduler task id as an 8-char
        # hex string. The public mirror replaces it with a stable safe
        # label so the dashboard still names what generated it without
        # leaking the internal scheduler/task identifier.
        task_id = task.get("id")
        if isinstance(task_id, str) and SCHEDULER_TASK_ID_RE.match(task_id):
            task["id"] = PUBLIC_TASK_LABEL

    guardrails = out.get("guardrails")
    if isinstance(guardrails, list):
        out["guardrails"] = [
            _sanitize_guardrail_line(line) for line in guardrails
        ]
    guardrail_status = out.get("guardrail_status")
    if isinstance(guardrail_status, list):
        for row in guardrail_status:
            if isinstance(row, dict):
                for key in ("rule", "evidence"):
                    if key in row:
                        row[key] = _sanitize_guardrail_line(row.get(key))

    sources = out.get("sources", {})
    sources["sheet_url"] = "(redacted - private operations sheet)"
    sources["sheet_id"] = "(redacted)"

    cleaned_replies = []
    for reply in out.get("replies", []):
        cleaned_replies.append({
            "Date": reply.get("Date", ""),
            "Organization": "(redacted)",
            "Category": reply.get("Category", ""),
            "Classification": reply.get("Classification", ""),
            "Status": reply.get("Status", ""),
            "Bucket": reply.get("Bucket", ""),
        })
    out["replies"] = cleaned_replies

    batch = out.pop("latest_batch", None)
    if batch is not None:
        out["latest_batch_summary"] = {
            "size": len(batch),
            "note": (
                "Recipient-level details are not exposed in the public "
                "dashboard. See channel_mix_latest for category breakdown."
            ),
        }

    experiments = out.get("experiments") or []
    cleaned_experiments = []
    for e in experiments:
        if not isinstance(e, dict):
            continue
        cleaned_experiments.append({
            "title": e.get("title", ""),
            "hypothesis": _strip_internal_tracker_ids(e.get("hypothesis", "")),
            "channel": e.get("channel", ""),
            "status": e.get("status", ""),
            "next_step": _strip_internal_tracker_ids(e.get("next_step", "")),
        })
    if experiments:
        out["experiments"] = cleaned_experiments

    followups = out.get("human_followups") or []
    cleaned_followups = []
    for f in followups:
        if not isinstance(f, dict):
            continue
        cleaned_followups.append({
            "priority": f.get("priority", ""),
            "due": f.get("due", ""),
            "channel": f.get("channel", ""),
            "action": _strip_internal_tracker_ids(f.get("action", "")),
            "status": f.get("status", ""),
            "note": _strip_internal_tracker_ids(f.get("note", "")),
        })
    if followups:
        out["human_followups"] = cleaned_followups

    gh = out.get("github", {})
    gh.pop("latest_commit_before_dashboard", None)
    gh.pop("dashboard_build_commit", None)
    gh.pop("repo", None)
    gh["source_repo_visibility"] = "private"
    gh["dashboard_repo"] = "ishanpuri24/clove-outreach-dashboard"
    out["github"] = gh

    allowed_specific_rec_keys = {
        "google_ads_location",
        "intent_focus",
        "immediate_steps",
        "budget_bid_guidance",
        "negative_keyword_review_themes",
        "match_type_or_structure_guidance",
        "success_metric",
        "change_tracker_entry",
        "do_not_remove_note",
    }

    allowed_short_specific_rec_keys = {
        "headline",
        "why_this_campaign",
        "do_next",
        "inspect",
        "negative_keyword_focus",
        "structure_fix",
        "success_metric",
        "metric_snapshot",
        "log_note",
    }

    # Compact per-campaign points block: this is what each visible
    # action card now renders. The v5 benchmarked payload swaps the
    # legacy keys for benchmark-anchored ones so each card leads with
    # how the campaign's conversion metrics compare to the benchmark.
    allowed_campaign_specific_points_keys = {
        "conversion_benchmark",
        "ad_group_or_theme",
        "exact_change",
        "inspect",
        "keyword_focus",
        "success_metric",
        "daily_learning",
    }

    allowed_priority_playbook_keys = {
        "label",
        "shared_action",
        "budget_rule",
        "review_window",
        "completion_rule",
    }
    allowed_priority_playbook_levels = {"P0", "P1", "P2"}

    def _scrub_specific_recommendation(row: Any) -> None:
        """Drop any unexpected keys from a specific_recommendation block.

        Future payloads may add fields. The public mirror only ships the
        whitelisted keys; everything else is stripped. The long
        ``specific_recommendation``, the short
        ``short_specific_recommendation``, and the compact
        ``campaign_specific_points`` blocks are all scrubbed.
        """
        if not isinstance(row, dict):
            return
        rec = row.get("specific_recommendation")
        if isinstance(rec, dict):
            for key in list(rec.keys()):
                if key not in allowed_specific_rec_keys:
                    rec.pop(key, None)
        short = row.get("short_specific_recommendation")
        if isinstance(short, dict):
            for key in list(short.keys()):
                if key not in allowed_short_specific_rec_keys:
                    short.pop(key, None)
        pts = row.get("campaign_specific_points")
        if isinstance(pts, dict):
            for key in list(pts.keys()):
                if key not in allowed_campaign_specific_points_keys:
                    pts.pop(key, None)

    ads = out.get("google_ads_insights")
    if isinstance(ads, dict):
        forbidden_account_keys = (
            "manager_customer_id",
            "manager_account_id",
            "customer_id",
            "customer_ids",
            "account_id",
            "account_ids",
            "login_customer_id",
            "account_label",
        )
        for key in forbidden_account_keys:
            ads.pop(key, None)
        for group in ads.get("campaign_groups") or []:
            if isinstance(group, dict):
                for key in forbidden_account_keys:
                    group.pop(key, None)
        for row in ads.get("office_leaderboard") or []:
            if isinstance(row, dict):
                for key in forbidden_account_keys:
                    row.pop(key, None)
        for row in ads.get("account_coverage") or []:
            if isinstance(row, dict):
                for key in forbidden_account_keys:
                    row.pop(key, None)
        for camp in ads.get("campaigns") or []:
            if isinstance(camp, dict):
                for key in forbidden_account_keys:
                    camp.pop(key, None)
        for row in ads.get("manual_action_queue") or []:
            if isinstance(row, dict):
                for key in forbidden_account_keys:
                    row.pop(key, None)
                _scrub_specific_recommendation(row)
        playbooks = ads.get("priority_playbooks")
        if isinstance(playbooks, dict):
            for level in list(playbooks.keys()):
                if level not in allowed_priority_playbook_levels:
                    playbooks.pop(level, None)
                    continue
                block = playbooks.get(level)
                if not isinstance(block, dict):
                    playbooks.pop(level, None)
                    continue
                for key in list(block.keys()):
                    if key not in allowed_priority_playbook_keys:
                        block.pop(key, None)

        # ---- Top-of-Paid-Ads summary ----
        allowed_top_summary_keys = {
            "title",
            "period",
            "primary_stats",
            "benchmark_rules",
            "internal_benchmarks",
        }
        allowed_primary_stat_keys = {
            "label", "value", "benchmark", "delta",
        }
        allowed_internal_benchmark_keys = {
            "office_median_conversion_rate_pct",
            "campaign_median_conversion_rate_pct",
            "ad_group_median_conversion_rate_pct",
            "last_month_conversion_rate_pct",
        }
        top_summary = ads.get("paid_ads_top_summary")
        if isinstance(top_summary, dict):
            for k in list(top_summary.keys()):
                if k not in allowed_top_summary_keys:
                    top_summary.pop(k, None)
            stats = top_summary.get("primary_stats")
            if isinstance(stats, list):
                for stat in stats:
                    if not isinstance(stat, dict):
                        continue
                    for k in list(stat.keys()):
                        if k not in allowed_primary_stat_keys:
                            stat.pop(k, None)
            bench = top_summary.get("internal_benchmarks")
            if isinstance(bench, dict):
                for k in list(bench.keys()):
                    if k not in allowed_internal_benchmark_keys:
                        bench.pop(k, None)

        # ---- Conversion-rate benchmarks (by office) ----
        allowed_cvr_office_keys = {
            "office",
            "conversion_rate_pct",
            "last_month_conversion_rate_pct",
            "vs_office_median_pts",
            "conversions_per_day",
            "cpa",
            "status",
        }
        cvr_block = ads.get("conversion_rate_benchmarks")
        if isinstance(cvr_block, dict):
            rows = cvr_block.get("by_office")
            if isinstance(rows, list):
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    for k in list(row.keys()):
                        if k not in allowed_cvr_office_keys:
                            row.pop(k, None)

        # ---- Ad-group conversion benchmarks ----
        allowed_ad_group_benchmark_keys = {
            "office",
            "campaign",
            "ad_group",
            "spend",
            "clicks",
            "conversions",
            "conversion_rate_pct",
            "cpa",
            "cpc",
            "benchmark_status",
            "keyword_focus",
        }
        ag_rows = ads.get("ad_group_conversion_benchmarks")
        if isinstance(ag_rows, list):
            for row in ag_rows:
                if not isinstance(row, dict):
                    continue
                for k in list(row.keys()):
                    if k not in allowed_ad_group_benchmark_keys:
                        row.pop(k, None)
                for key in forbidden_account_keys:
                    row.pop(key, None)

        # ---- Daily improvement loop ----
        allowed_daily_loop_keys = {"title", "steps", "decision_rule"}
        loop = ads.get("daily_improvement_loop")
        if isinstance(loop, dict):
            for k in list(loop.keys()):
                if k not in allowed_daily_loop_keys:
                    loop.pop(k, None)
        trends = ads.get("trends") or {}
        if isinstance(trends, dict):
            for row in trends.get("by_office") or []:
                if isinstance(row, dict):
                    for key in forbidden_account_keys:
                        row.pop(key, None)
            for row in trends.get("by_campaign") or []:
                if isinstance(row, dict):
                    for key in forbidden_account_keys:
                        row.pop(key, None)
                    _scrub_specific_recommendation(row)
        out["google_ads_insights"] = ads

    kf = out.get("google_ads_keyword_focus")
    if isinstance(kf, dict):
        for key in (
            "manager_customer_id",
            "customer_id",
            "customer_ids",
            "account_id",
            "search_terms",
            "search_term_view",
        ):
            kf.pop(key, None)
        out["google_ads_keyword_focus"] = kf

    rd = out.get("b2b_reply_detail")
    if isinstance(rd, dict):
        forbidden_reply_keys = (
            "Email From",
            "email_from",
            "sender",
            "sender_name",
            "sender_email",
            "from",
            "From",
            "Organization",
            "organization",
            "phone",
            "Phone",
            "Body",
            "body",
            "raw_text",
            "raw_reply",
            "Summary",
            "summary",
            "Suggested Next Action",
        )
        for key in forbidden_reply_keys:
            rd.pop(key, None)
        timeline = rd.get("reply_timeline") or []
        cleaned_timeline = []
        for row in timeline:
            if not isinstance(row, dict):
                continue
            cleaned_timeline.append({
                "date": row.get("date", ""),
                "category": row.get("category", ""),
                "classification": row.get("classification", ""),
                "public_theme": row.get("public_theme", ""),
                "status": row.get("status", ""),
                "suggested_next_action_public": row.get(
                    "suggested_next_action_public", ""
                ),
            })
        rd["reply_timeline"] = cleaned_timeline
        out["b2b_reply_detail"] = rd

    return out


def reinject_into_html(snap: dict[str, Any]) -> None:
    html = INDEX_HTML.read_text()
    start = html.index("/* SNAPSHOT_START */")
    end = html.index("/* SNAPSHOT_END */")
    new_block = (
        "/* SNAPSHOT_START */\n"
        f"window.__SNAPSHOT__ = {json.dumps(snap)};\n"
    )
    INDEX_HTML.write_text(html[:start] + new_block + html[end:])


if __name__ == "__main__":
    snap = json.loads(DATA_FILE.read_text())
    sanitized = sanitize_for_public(snap)
    DATA_FILE.write_text(json.dumps(sanitized, indent=2) + "\n")
    reinject_into_html(sanitized)
    print(f"Wrote sanitized {DATA_FILE} and re-injected into {INDEX_HTML}.")
