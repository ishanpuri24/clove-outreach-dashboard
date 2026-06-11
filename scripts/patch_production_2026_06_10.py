#!/usr/bin/env python3
"""Patch data/snapshot.json with MTD production from Open Dental.

For each row in google_ads_insights.office_leaderboard:
  - Set production_mtd_usd from data/od_production_mtd.json (map office -> value).
  - Recompute spend_pct_of_production = (daily_runrate_x30_usd / production_mtd_usd) * 100.
  - Set status_vs_4pct_target:
        <=4.0   -> "ON TARGET"
        4.0-6.0 -> "ELEVATED"
        >6.0    -> "OVER 4% CAP"

Also stamps:
  google_ads_insights.production_source = {window, total, fetched_at}
  daily_learning_loop appended entry for 2026-06-10 production wire
  credit_usage_tracker bumped (one OD pull this run)
"""
import json
from datetime import date, datetime

ROOT = "/home/user/workspace/clove-outreach-dashboard"
SNAP = f"{ROOT}/data/snapshot.json"
PROD = f"{ROOT}/data/od_production_mtd.json"

# office key in leaderboard -> office key in OD production map
# leaderboard offices: Camarillo, Riverpark+AI, BH+AI, Hillview, Puri, SO, SM+AI, Encino+AI, TO
OFFICE_TO_OD = {
    "Camarillo": "Camarillo",
    "Riverpark+AI": "Riverpark",
    "Beverly Hills+AI": "BH",
    "Hillview": "Hillview",
    "Puri Dentistry": "Puri",
    "Sherman Oaks": "SO",
    "Santa Monica+AI": "SM",
    "Encino+AI": "Encino",
    "Thousand Oaks": "Marin",  # TO campaign = Marin clinic
}


def status_label(pct: float) -> str:
    if pct is None:
        return "—"
    if pct <= 4.0:
        return "ON TARGET"
    if pct <= 6.0:
        return "ELEVATED"
    return "OVER 4% CAP"


def main():
    snap = json.load(open(SNAP))
    prod = json.load(open(PROD))
    od_by_office = prod["production_mtd_usd_complete_by_office"]
    window_end = prod["window_end"]
    window_start = prod["window_start"]

    leaderboard = snap.setdefault("google_ads_insights", {}).setdefault(
        "office_leaderboard", []
    )

    updated = []
    for row in leaderboard:
        office = row.get("office")
        od_key = OFFICE_TO_OD.get(office)
        if od_key is None or od_key not in od_by_office:
            row["production_mtd_usd"] = None
            row["spend_pct_of_production"] = None
            row["status_vs_4pct_target"] = "—"
            updated.append(office)
            continue
        prod_mtd = float(od_by_office[od_key])
        row["production_mtd_usd"] = round(prod_mtd, 2)
        runrate = float(row.get("daily_runrate_x30_usd") or 0)
        if prod_mtd > 0:
            pct = (runrate / prod_mtd) * 100
            row["spend_pct_of_production"] = round(pct, 2)
            row["status_vs_4pct_target"] = status_label(pct)
        else:
            row["spend_pct_of_production"] = None
            row["status_vs_4pct_target"] = "—"
        updated.append(office)

    # Source stamp
    snap["google_ads_insights"]["production_source"] = {
        "system": "Open Dental",
        "endpoint": "/procedurelogs?ProcStatus=C&ClinicNum=<n>",
        "window_start": window_start,
        "window_end": window_end,
        "total_mtd_usd": round(prod["total_production_mtd_usd"], 2),
        "fetched_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "rows_aggregated": sum(prod["rows_in_window_by_office"].values()),
    }

    # Daily learning loop entry (idempotent: drop any prior entry for today first)
    today = date.today().isoformat()
    loop = snap.setdefault("daily_learning_loop", {})
    entries = loop.setdefault("entries", [])
    entries[:] = [e for e in entries if e.get("date") != today or e.get("actions_taken", [None])[0] != "Wired Open Dental procedurelogs (ProcStatus=C) into the Ads tab"]
    entries.append({
        "date": today,
        "actions_taken": [
            "Wired Open Dental procedurelogs (ProcStatus=C) into the Ads tab",
            "Set production_mtd_usd per office and recomputed spend % of production",
            "Applied 4% ceiling status (ON TARGET / ELEVATED / OVER 4% CAP)",
        ],
        "impact_learning": [
            f"Total MTD production = ${prod['total_production_mtd_usd']:,.0f} across 9 offices",
            "Run-rate (×30) vs MTD production exposes which offices are over the 4% cap",
            "Open Dental /procedurelogs ignores ProcDate filter; must page Offset and filter ProcDate in code",
        ],
        "self_rating": {
            "action_taken": 9,
            "impact_learning": 9,
            "dashboard_clarity": 9,
            "automation_reliability": 8,
            "privacy_and_safety": 10,
            "speed_and_credit_efficiency": 7,
        },
        "remediations_next_run": [
            "Cache OD production per day to a delta file so daily refreshes only pull today's procs",
            "Add async parallel clinic pulls to compress runtime",
        ],
        "blockers": [],
    })

    # Credit usage tracker bump (idempotent for today's OD wire run)
    cu = snap.setdefault("credit_usage_tracker", {})
    runs = cu.setdefault("runs", [])
    runs[:] = [r for r in runs if not (r.get("date") == today and r.get("notes", "").startswith("OD wire run"))]
    runs.append({
        "date": today,
        "source_pulls": {
            "open_dental": 1,
            "google_ads": 0,  # reused this morning's pull
            "callrail": 0,
            "ga4": 0,
            "gsc": 0,
            "ahrefs": 0,
            "gmb": 0,
            "hubspot": 0,
        },
        "browser_calls": 0,
        "subagents": 0,
        "notes": "OD wire run; one paged pull across 9 clinics filtered by ProcStatus=C.",
    })

    with open(SNAP, "w") as f:
        json.dump(snap, f, indent=2)

    print(f"updated {len(updated)} leaderboard rows: {', '.join(updated)}")
    print(f"total MTD production: ${prod['total_production_mtd_usd']:,.2f}")
    for row in leaderboard:
        print(
            f"  {row['office']:<14} run-rate ${row.get('daily_runrate_x30_usd',0):>7,.0f} | "
            f"prod ${row.get('production_mtd_usd') or 0:>9,.0f} | "
            f"{row.get('spend_pct_of_production')}% | {row.get('status_vs_4pct_target')}"
        )


if __name__ == "__main__":
    main()
