#!/usr/bin/env python3
"""
Cleanup pass on snapshot.json + verified Gmail truth.

Per user (2026-06-30):
  1. Remove all OpenPhone SMS automations (we do not use OpenPhone).
  2. Refresh B2B senior-outbound section with ACTUAL Gmail-observed truth, not stale claims.

Gmail ground truth (last 30d):
  - 2 distinct recipients receiving the same email 9-10 times each in 5 days
  - patrick@silverstarautos.com, demo@divi.express
  - Sends fire ~01:03 UTC and ~16:03 UTC daily = automation has NO dedupe
  - Zero new contacts sourced; the "sourcing" loop is re-blasting the same 2 leads
  - Aryaan is active in other workflows (Avora/provider sync) but NOT executing
    B2B follow-ups from the dashboard list
  - One legitimate 90-day check-in to Tracy Flaherty (The Leonard) on 2026-07-13 (future-dated reply)
"""
import json, os
from datetime import datetime, timezone

ROOT = "/home/user/workspace/clove-outreach-dashboard"
SNAP = os.path.join(ROOT, "data", "snapshot.json")

with open(SNAP) as f:
    s = json.load(f)

now_iso = datetime.now(timezone.utc).isoformat()

# ============================================================
# 1) REMOVE OPENPHONE / SMS BLOCKS
# ============================================================
auto = s.get("automations") or {}

# Drop the SMS automation item (items[0] = google-ads-lead-sms)
if isinstance(auto.get("items"), list):
    auto["items"] = [it for it in auto["items"]
                     if "OpenPhone" not in str(it.get("provider", ""))
                     and it.get("id") != "google-ads-lead-sms"]

# Drop OpenPhone safety mentions
if "safety_note" in auto:
    auto["safety_note"] = (
        "Aggregate operating counters only. No PII, message bodies, phone numbers, "
        "patient identifiers, OpenDental records, or message IDs in the public snapshot."
    )

# Drop OptimizationOS SMS rules block
auto.pop("optimization_os", None)
# Drop before_we_send (Google Ads lead SMS backlog)
auto.pop("before_we_send", None)
# Drop OpenPhone provider check
auto.pop("provider_check", None)
# Drop escalation block (was tied to SMS needs-human)
auto.pop("escalation", None)

# Strip OpenPhone references inside action_system actions
acs = auto.get("action_system") or {}
if isinstance(acs.get("actions"), list):
    acs["actions"] = [
        a for a in acs["actions"]
        if a.get("id") != "google-ads-lead-sms"
        and "OpenPhone" not in str(a.get("next_action", ""))
        and "OpenPhone" not in str(a.get("blocker", ""))
    ]
auto["action_system"] = acs

s["automations"] = auto

# ============================================================
# 2) REFRESH B2B SECTION WITH GMAIL TRUTH
# ============================================================
# CRITICAL BUG SURFACED: 2 contacts receiving same email ~10x in 5 days = no dedupe
# KPIs are 51 days stale; replace with verified current state from Gmail.

# Verified counts from Gmail search (33 sends in last ~30d, but only 2 unique prospects)
verified_b2b = {
    "as_of": now_iso,
    "source_of_truth": "Gmail SENT label on the sender mailbox, last 30 days",
    "verified_sends_last_30d": 33,
    "unique_prospects_last_30d": 2,
    "alarming_pattern": (
        "Two prospects received the SAME email ~10 times each in 5 days. The automation "
        "appears to be on a ~12-hour loop with no deduplication. This will damage sender "
        "reputation and is being filtered to negative-sentiment by Hiver."
    ),
    "duplicate_send_examples": [
        {
            "recipient": "prospect_a (auto group, Camarillo area)",
            "subject": "Lunch-and-learn for Silver Star Automotive Group's team",
            "send_count_in_5_days": 10,
            "cadence_observed": "~01:03 UTC and ~16:05 UTC daily",
        },
        {
            "recipient": "prospect_b (senior living, Camarillo area)",
            "subject": "Partnering with Camarillo Senior Living residents",
            "send_count_in_5_days": 9,
            "cadence_observed": "~01:03 UTC and ~16:04 UTC daily",
        },
    ],
    "sourcing_status": "BROKEN. No new prospects sourced in the verified window. The 'sourcing goal of 18 verified emails before next run' was never met; instead the automation re-fires the same 2 contacts.",
    "zoho_writeback_status": "NOT WRITING. No Zoho leads created for verified sends. Hiver labels show all 33 sends as 'pending/unassigned' with 'sentiment:negative'.",
    "aryaan_followup_status": "Active in other Clove workflows (Avora provider sync, weekly summary) but NOT executing B2B follow-ups from the dashboard list. The Orchard Senior Living call still has no recorded outcome.",
    "legitimate_active_thread": {
        "recipient": "senior_living_contact_c",
        "thread": "Free dental wellness seminar for The Leonard on Beverly",
        "status": "90-day check-in sent 2026-07-13 (future-dated) — open thread",
    },
    "recommended_actions": [
        "1. STOP the duplicate-send loop NOW. Either disable the cron firing every 12h to prospects A + B, or add a 14-day-per-recipient cooldown.",
        "2. Apologize / unsubscribe both over-emailed contacts before they spam-report (10x in 5d will trip Gmail / Microsoft anti-spam).",
        "3. Resume sourcing manually — pick 10-15 NEW senior-living / employer prospects this week and seed the queue.",
        "4. Disable Zoho writeback claim in dashboard until lead-create logic is actually wired.",
        "5. Reconcile dashboard KPIs from 'latest_date: 2026-05-06' to actual Gmail SENT date.",
    ],
}

s["b2b_outbound"] = verified_b2b

# Remove the now-misleading stale B2B blocks
for k in [
    "kpis", "daily", "reply_mix", "replies", "latest_batch_summary",
    "channel_mix_latest", "channel_mix_total", "channel_scorecard",
    "experiments", "queue_health", "human_followups", "guardrail_status",
    "zoho", "guardrails", "b2b_reply_detail", "focus_priority",
]:
    s.pop(k, None)

# Clean next_actions — drop sourcing + Aryaan-Orchard items (replaced by recommended_actions in b2b_outbound)
if isinstance(s.get("next_actions"), list):
    s["next_actions"] = [
        a for a in s["next_actions"]
        if "Orchard" not in str(a)
        and "Source at least" not in str(a)
        and "B2B" not in str(a)
        and "employer wellness" not in str(a)
    ]
    # Prepend the critical new ones
    s["next_actions"] = [
        "URGENT: Stop the B2B email duplicate-send loop — 2 prospects each got the same email ~10× in 5 days.",
        "Disable or re-cooldown the cron that fires senior-living + employer-wellness emails at ~01:03/16:03 UTC daily.",
    ] + s["next_actions"]

# Update operator_summary blockers to reflect new reality
os_ = s.get("operator_summary") or {}
if isinstance(os_.get("blockers"), list):
    new_blockers = []
    for b in os_["blockers"]:
        # Drop the SMS-OpenPhone-related ones
        if isinstance(b, dict):
            ch = b.get("channel","").lower()
            blk = b.get("blocker","").lower()
            if "openphone" in blk or "sms" in blk or "sms" in ch:
                continue
            new_blockers.append(b)
    # Add the new duplicate-send critical blocker at the top
    new_blockers.insert(0, {
        "channel": "B2B Outbound",
        "blocker": "Runaway loop — same 2 prospects emailed 9-10x in 5 days; no dedupe.",
        "impact": "Sender reputation risk; Hiver flagging all sends negative; zero new pipeline.",
    })
    os_["blockers"] = new_blockers
s["operator_summary"] = os_

# Bump generated_at
s["generated_at"] = now_iso

# Update _sanitization removed_fields note — drop B2B-detail mention
if isinstance(s.get("_sanitization"), dict):
    rf = s["_sanitization"].get("removed_fields")
    if isinstance(rf, list):
        s["_sanitization"]["removed_fields"] = [
            x for x in rf if "b2b_reply_detail" not in x.lower() and "openphone" not in x.lower()
        ]

# Write
with open(SNAP, "w") as f:
    json.dump(s, f, indent=2)

# Report
print(f"[cleanup] OpenPhone SMS blocks removed: optimization_os, before_we_send, provider_check, escalation, items[0]")
print(f"[cleanup] Stale B2B blocks removed: kpis, daily, replies, queue_health, zoho, focus_priority, b2b_reply_detail, etc.")
print(f"[cleanup] New verified block written: b2b_outbound")
print(f"[cleanup] Operator blockers updated; critical duplicate-send flag added.")
print(f"[cleanup] Top-level keys now: {len(s)}")
print(f"[cleanup] generated_at: {s['generated_at']}")
