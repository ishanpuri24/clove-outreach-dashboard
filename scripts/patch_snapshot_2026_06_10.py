"""
Ads tab v2 (2026-06-10): daily run-rate x30 vs 30d trailing, production from Open Dental,
% of production vs 4% target, clearer per-office actions.

Inputs:
- Yesterday daily spend pulled live from Google Ads (2026-06-09).
- 30-day trailing spend from prior snapshot (validated 2026-06-09).
- Production: PENDING (Open Dental credential not yet wired in this Space).
"""
import json
from datetime import datetime, timezone
from pathlib import Path

SNAP = Path(__file__).resolve().parent.parent / "data" / "snapshot.json"
d = json.loads(SNAP.read_text())

TARGET_PCT = 4.0  # operator-set ceiling: paid ads spend should be <=4% of production

# (office_label, customer_id, yesterday_cost_micros, yesterday_conv, trailing_30d_spend, trailing_30d_conv, trailing_30d_calls, prev_recoverable)
rows = [
    ("Hillview",         "3737640297", 199_683_208,  2.00, 6713,  188.9,  92, 2500),
    ("Sherman Oaks",     "6442679282", 184_880_641,  2.00, 12465, 217.3, 108, 5500),
    ("Riverpark+AI",     "7621293648", 224_322_172,  5.00, 8303,  148.0, 193, 2900),
    ("Beverly Hills+AI", "2816298093", 220_065_987,  1.00, 11506, 173.5, 199, 3200),
    ("Thousand Oaks",    "3575932013", 104_621_900,  0.00, 5498,  126.0, 159, 1700),
    ("Encino+AI",        "8668802505", 138_698_046,  1.00, 8780,  183.0, 185, 2400),
    ("Santa Monica+AI",  "8712971350", 145_284_484,  1.00, 13825, 218.0, 151, 5800),
    ("Camarillo",        "4276567700", 258_551_745,  3.00, 8097,  141.0, 149, 2000),
    ("Puri Dentistry",   "2481492821", 191_427_225, 11.00, 8624,  275.0, 358, 1500),
]

def status_for(pct):
    if pct is None:
        return "PENDING"
    if pct <= TARGET_PCT:
        return "ON TARGET"
    if pct <= TARGET_PCT * 1.5:  # up to 6%
        return "ELEVATED"
    return "OVER"

# Improvement narratives per office — operator-friendly, action-first
IMPROVEMENTS = {
    "Hillview":         "Pause JZ-Search-Ventura ($158 CPA waste). +50% Insurance ($21 CPA). Desktop -80%.",
    "Sherman Oaks":     "Pause JZ-Search-SO ($1,404), JZ-PMAX, JZ-Search-SO-Spanish. Consolidate 5 PMax to 2. Desktop -85%.",
    "Riverpark+AI":     "Pause Dentical + Spanish. +50% Insurance PMax. Wire AI call-tracking. Desktop -80%.",
    "Beverly Hills+AI": "Pause Dentical + Spanish. Consolidate PMax variants. Wire AI call-tracking. Desktop -85%.",
    "Thousand Oaks":    "Audit Marin geo ($1.6k at risk). Clone Spanish template. +40% Insurance.",
    "Encino+AI":        "Pause JZ-Search-Encino ($721 CPA). +50% PMX-encino ($13 CPA, org best). Wire AI call-tracking.",
    "Santa Monica+AI":  "Pause 3 zero-conv campaigns ($4.7k/mo). Investigate Pmax-dentical (only working Dentical). Desktop -90%.",
    "Camarillo":        "Pause Dentical ($586 CPA). Convert JZ-Search-Clove to Max Conv. +50% Insurance PMax ($18 CPA).",
    "Puri Dentistry":   "GROW: +75% Insurance PMax (org-best $10 CPA). Clone Spanish template org-wide.",
}

leaderboard = []
total_yesterday = 0.0
total_runrate = 0.0
total_trailing = 0.0
total_calls = 0
total_conv_trailing = 0.0

for label, cid, cost_micros, y_conv, t_spend, t_conv, t_calls, prev_rec in rows:
    yesterday = round(cost_micros / 1_000_000, 2)
    runrate30 = round(yesterday * 30, 0)
    # trend = runrate30 vs trailing 30d (negative = pacing down vs last 30d)
    trend_pct = round(100 * (runrate30 - t_spend) / t_spend, 1) if t_spend else None
    # production is unknown; show placeholders so the structure renders
    production_mtd = None
    pct_of_prod = None
    status = status_for(pct_of_prod)

    leaderboard.append({
        "office": label,
        # customer_id intentionally not emitted to public snapshot (validator strips ID-shaped values)
        "yesterday_spend_usd": yesterday,
        "yesterday_conversions": y_conv,
        "daily_runrate_x30_usd": runrate30,
        "trailing_30d_spend_usd": t_spend,
        "trend_vs_30d_pct": trend_pct,
        "production_mtd_usd": production_mtd,
        "spend_pct_of_production": pct_of_prod,
        "status_vs_4pct_target": status,
        "trailing_30d_conv": t_conv,
        "trailing_30d_calls": t_calls,
        "trailing_30d_cpa_usd": round(t_spend / t_conv, 1) if t_conv else None,
        "recoverable_per_month_usd": prev_rec,
        "operator_focus": IMPROVEMENTS.get(label, "-"),
        # Legacy aliases used by current renderer (so existing cells still populate):
        "spend_usd": runrate30,           # column now reads "Run-rate (daily x30)"
        "conversions": t_conv,
        "cpa_usd": round(t_spend / t_conv, 1) if t_conv else None,
        "phone_calls": t_calls,
        "dollar_per_call": round(t_spend / t_calls, 1) if t_calls else None,
        "high_risk_spend_usd": prev_rec,
        "high_risk_spend_share_pct": round(100 * prev_rec / t_spend, 1),
        "campaign_count": None,
        "status": "PROD PENDING",
    })

    total_yesterday += yesterday
    total_runrate += runrate30
    total_trailing += t_spend
    total_calls += t_calls
    total_conv_trailing += t_conv

# Sort by run-rate descending (largest spend at top — operator wants action priority)
leaderboard.sort(key=lambda r: r["daily_runrate_x30_usd"], reverse=True)

org_trend = round(100 * (total_runrate - total_trailing) / total_trailing, 1)
org_runrate_label = (
    f"pacing {'down' if org_trend < 0 else 'up'} "
    f"{abs(org_trend):.1f}% vs trailing 30d"
) if total_trailing else ""

ai = d.get("google_ads_insights", {})
ai["title"] = "Paid Ads · 9 offices · Daily run-rate x30 vs 30-day trailing"
ai["subtitle"] = (
    f"Yesterday spend ${total_yesterday:,.0f} -> "
    f"projected month ${total_runrate:,.0f} ({org_runrate_label}). "
    f"Target: paid ads <= {TARGET_PCT:.0f}% of office production (production pending Open Dental wire)."
)
ai["lookback"] = "Yesterday (2026-06-09) + trailing 30d"
ai["data_freshness"] = "Yesterday spend pulled live 2026-06-10 from Google Ads"
ai["headline"] = (
    f"Run-rate ${total_runrate:,.0f}/mo across 9 offices "
    f"({'-' if org_trend < 0 else '+'}{abs(org_trend):.1f}% vs trailing 30d {total_trailing:,.0f}). "
    f"Target: <={TARGET_PCT:.0f}% of office production. Production: pending Open Dental wire."
)

_t = dict(ai.get("totals") or {})
_t["yesterday_spend_usd"] = round(total_yesterday, 0)
_t["daily_runrate_x30_usd"] = round(total_runrate, 0)
_t["trailing_30d_spend_usd"] = round(total_trailing, 0)
_t["trend_vs_30d_pct"] = org_trend
_t["monthly_spend_usd"] = round(total_runrate, 0)  # treat run-rate as monthly going forward
_t["monthly_calls"] = total_calls
_t["target_pct_of_production"] = TARGET_PCT
_t["production_mtd_usd"] = None
_t["spend_pct_of_production"] = None
_t["blended_status_vs_target"] = "PENDING"
ai["totals"] = _t

ai["office_leaderboard"] = leaderboard

ai["focus_areas_org_level"] = [
    f"Wire Open Dental MTD production per office -> dashboard auto-computes % of spend vs {TARGET_PCT:.0f}% target",
    "Daily run-rate (yesterday x30) is the new pacing signal; trailing 30d is the trailing baseline",
    "Scale Puri Insurance PMax ($10 CPA, org-best) — largest single ROI lever",
    "Pause Santa Monica zero-conv trio ($4.7k/mo bleeding) and Sherman Oaks JZ campaigns",
    "Apply Desktop -80% to -90% account-level (mobile carries 88-97% of conversions)",
    "Wire call-tracking on 4 AI subaccounts (currently under-attributing 40-80 calls/mo)",
]

ai["how_to_read_2026_06_10"] = {
    "yesterday_spend": "Actual spend yesterday (a complete day). Most current signal.",
    "run_rate_x30": "Yesterday x 30 = projected monthly if you do nothing. Compare to 30d trailing to see if you are pacing up or down.",
    "trailing_30d_spend": "What you actually spent the last 30 days.",
    "trend_vs_30d_pct": "Negative = pacing below recent average (cooling); positive = pacing above (heating up).",
    "spend_pct_of_production": "Daily run-rate as % of office MTD production from Open Dental. Target: <=4%.",
    "status": "ON TARGET (<=4%), ELEVATED (4-6%), OVER (>6%), PENDING (production not yet wired).",
}

ai["operator_priorities_2026_06_10"] = [
    {
        "rank": 1,
        "office": "Camarillo",
        "signal": "Yesterday $259 -> run-rate $7,757/mo. Largest single daily spend.",
        "action": "Confirm budget is intentional. Pause Dentical ($586 CPA). Verify Insurance PMax ramp.",
    },
    {
        "rank": 2,
        "office": "Riverpark+AI",
        "signal": "Yesterday $224 -> run-rate $6,730/mo. Trending well above its $8.3k trailing if you scale.",
        "action": "Pause Dentical + Spanish (~$1.1k bleed). Reallocate to Insurance PMax.",
    },
    {
        "rank": 3,
        "office": "Beverly Hills+AI",
        "signal": "Yesterday $220 -> run-rate $6,602/mo (-43% vs trailing $11.5k).",
        "action": "Yesterday's pacing is healthier than last 30d average. Maintain. Wire AI call-tracking.",
    },
    {
        "rank": 4,
        "office": "Hillview",
        "signal": "Yesterday $200 -> run-rate $5,990/mo. Insurance still budget-capped.",
        "action": "Pause JZ-Search-Ventura. +50% Insurance budget today.",
    },
    {
        "rank": 5,
        "office": "Puri Dentistry",
        "signal": "Yesterday $191 / 11 conv = $17 CPA. Best daily efficiency in org.",
        "action": "GROW. +75% on Insurance PMax. This office should not have a pause queue.",
    },
    {
        "rank": 6,
        "office": "Sherman Oaks",
        "signal": "Yesterday $185 -> run-rate $5,546/mo (-55% vs trailing $12.5k). Already cooling.",
        "action": "Good direction. Execute the 5 pauses to lock in savings.",
    },
    {
        "rank": 7,
        "office": "Santa Monica+AI",
        "signal": "Yesterday $145 -> run-rate $4,359/mo (-68% vs trailing $13.8k). Big drop.",
        "action": "Confirm the zero-conv trio is paused (that explains the drop). If not, pause now.",
    },
    {
        "rank": 8,
        "office": "Encino+AI",
        "signal": "Yesterday $139 -> run-rate $4,161/mo (-53% vs trailing $8.8k).",
        "action": "Pause JZ-Search-Encino. +50% PMX-encino. Wire AI call-tracking.",
    },
    {
        "rank": 9,
        "office": "Thousand Oaks",
        "signal": "Yesterday $105 / 0 conv. Lowest spend, but zero conv yesterday.",
        "action": "Check if Marin geo or Dentical campaign drove yesterday's $105. Audit before more days.",
    },
]

ai["api_writeback_status"] = "Read-only Google Ads connector. Pauses/budget changes require manual UI execution."
ai["operator_notes"] = (
    f"v2 dashboard — daily run-rate (yesterday x30) replaces the static 30d spend column. "
    f"Open Dental production wire is pending; once a credential is saved here, the "
    f"'% of {TARGET_PCT:.0f}% target' column auto-fills."
)
d["google_ads_insights"] = ai

# --- Daily learning loop entry ---
_learning = d.get("daily_learning_loop", {"entries": []})
if not isinstance(_learning, dict):
    _learning = {"entries": []}
entries = _learning.get("entries", [])
entries.insert(0, {
    "date": "2026-06-10",
    "actions_taken": [
        "Pulled yesterday spend (2026-06-09) for all 9 Google Ads accounts in one parallel batch",
        "Added daily_runrate_x30_usd, trailing_30d_spend_usd, trend_vs_30d_pct per office",
        "Added production_mtd_usd + spend_pct_of_production columns (pending Open Dental wire)",
        "Replaced static '30d spend' with run-rate; added operator_priorities_2026_06_10 with concrete next action per office",
        "Added how_to_read legend so dashboard explains itself",
    ],
    "impact_learning": [
        "Org pacing significantly below trailing 30d — yesterday $1.67k -> $50k/mo run-rate vs $83.8k trailing. Either intentional cooldown or under-pacing winners.",
        "Camarillo is now the largest single-day spend ($259). Puri is best efficiency ($17 daily CPA).",
        "Santa Monica down 68% vs trailing — pauses may already be in flight.",
    ],
    "self_rating": {
        "action_taken": 9,
        "impact_learning": 8,
        "dashboard_clarity": 9,
        "automation_reliability": 6,
        "privacy_and_safety": 9,
        "speed_and_credit_efficiency": 9,
    },
    "remediations_next_run": [
        "Add Open Dental credential to this Space so production_mtd_usd auto-fills",
        "Consider 7-day rolling average instead of single yesterday — one slow day distorts run-rate",
    ],
    "blockers": [
        "Open Dental credential not saved in this Space (custom credentials are thread-scoped)",
    ],
})
_learning["entries"] = entries[:30]
d["daily_learning_loop"] = _learning

# --- Credit tracker ---
d["credit_usage_tracker"] = {
    "mode": "lean",
    "tasks_this_run": [
        {"task": "Google Ads daily pull (9 accounts in parallel)", "cost_class": "low", "justified": True},
        {"task": "Snapshot patch + validate", "cost_class": "low", "justified": True},
        {"task": "index.html column changes", "cost_class": "low", "justified": True},
    ],
    "credit_notes": "Single parallel batch of 9 reports; no subagents; no browser. Run cost minimal.",
}

d["generated_at"] = datetime.now(timezone.utc).isoformat()
SNAP.write_text(json.dumps(d, indent=2) + "\n")
print(f"Patched {SNAP}")
print(f"  Yesterday total: ${total_yesterday:,.0f}")
print(f"  Run-rate x30:    ${total_runrate:,.0f}/mo")
print(f"  Trailing 30d:    ${total_trailing:,.0f}/mo")
print(f"  Trend:           {org_trend:+.1f}%")
print(f"  Production: PENDING Open Dental wire")
