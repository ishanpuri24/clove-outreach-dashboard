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
from pathlib import Path
from typing import Any

DASHBOARD_DIR = Path(__file__).resolve().parents[1]
DATA_FILE = DASHBOARD_DIR / "data" / "snapshot.json"
INDEX_HTML = DASHBOARD_DIR / "index.html"


def sanitize_for_public(snap: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``snap`` with sensitive prospect/PII data removed.

    Public dashboard exposes only aggregate metrics, category and
    status summaries, and operating guardrails. Individual prospect
    emails, reply sender names and addresses, free-text summaries,
    the operations Google Sheet identifier, and recipient-level batch
    rows are removed.
    """
    out = json.loads(json.dumps(snap))

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

    followups = out.get("human_followups") or []
    cleaned_followups = []
    for f in followups:
        cleaned_followups.append({
            "id": f.get("id", ""),
            "priority": f.get("priority", ""),
            "due": f.get("due", ""),
            "channel": f.get("channel", ""),
            "action": f.get("action", ""),
            "status": f.get("status", ""),
            "note": f.get("note", ""),
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
