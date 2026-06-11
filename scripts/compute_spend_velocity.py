#!/usr/bin/env python3
"""Compute spend velocity windows (yesterday, trailing-7d, trailing-30d)
per Google Ads account and per office, then merge into snapshot.json
google_ads_insights.office_leaderboard + totals.

Reads raw 30d daily pulls from data/_gads_pulls/*.json (16 accounts).
"""
import json, pathlib, datetime, glob, re

ROOT = pathlib.Path(__file__).resolve().parents[1]
PULLS = ROOT / "data" / "_gads_pulls"
SNAP = ROOT / "data" / "snapshot.json"

# Customer ID -> office name (canonicalized for snapshot rollup)
# Maps each child account to an office bucket. *-AI accounts roll into their parent.
ACCT_TO_OFFICE = {
    "4276567700": "Camarillo",                  # Clove Dental Camarillo
    "2481492821": "Puri Dentistry",             # Puri Dentistry
    "3737640297": "Hillview",                   # Hillview
    "6442679282": "Sherman Oaks",               # Sherman Oaks
    "7621293648": "Oxnard Riverpark",           # Riverpark
    "8287478168": "All Offices / Ayur",         # All-offices (Ayur)
    "2816298093": "Beverly Hills",              # Beverly Hills
    "3575932013": "Thousand Oaks",              # Thousand Oaks (a.k.a. "Marin" elsewhere)
    "4787133203": "Ayur - Problem",             # Problem (Ayur)
    "8668802505": "Encino",                     # Encino
    "7341541088": "Encino",                     # Encino - AI -> roll into Encino
    "1181427688": "Beverly Hills",              # Beverly Hills - AI -> roll into Beverly Hills
    "9588043178": "Sherman Oaks",               # Sherman Oaks - AI -> roll into Sherman Oaks
    "7980265317": "Oxnard Riverpark",           # Riverpark - AI -> roll into Riverpark
    "8712971350": "Santa Monica",               # Santa Monica
    "9663587549": "Santa Monica",               # Santa Monica - Al -> roll into Santa Monica
}

def load_pulls():
    rows = []
    for path in sorted(PULLS.glob("output_*.json")):
        try:
            doc = json.loads(path.read_text())
        except Exception:
            continue
        # outputs from connector are wrapped — extract `results` list
        results = []
        if isinstance(doc, dict):
            if "results" in doc:
                results = doc["results"]
            elif "result" in doc and isinstance(doc["result"], dict):
                results = doc["result"].get("results", [])
            elif "value" in doc and isinstance(doc["value"], dict):
                results = doc["value"].get("results", [])
        for r in results:
            cust = r.get("customer") or {}
            mets = r.get("metrics") or {}
            segs = r.get("segments") or {}
            rows.append({
                "customer_id": str(cust.get("id","")),
                "descriptive_name": cust.get("descriptiveName",""),
                "date": segs.get("date",""),
                "cost_usd": int(mets.get("costMicros",0) or 0) / 1_000_000.0,
                "conversions": float(mets.get("conversions") or 0),
                "clicks": int(mets.get("clicks") or 0),
                "impressions": int(mets.get("impressions") or 0),
            })
    return rows

def compute_windows(rows):
    """Given daily rows, compute per-office windows."""
    today = datetime.date(2026, 6, 11)  # snapshot generation date
    yesterday = today - datetime.timedelta(days=1)
    last7_start = today - datetime.timedelta(days=7)   # last 7 complete days: 6/4..6/10
    last30_start = today - datetime.timedelta(days=30) # last 30 complete days: 5/12..6/10

    # per office aggregates
    per_office = {}
    for r in rows:
        office = ACCT_TO_OFFICE.get(r["customer_id"])
        if not office: continue
        try:
            d = datetime.date.fromisoformat(r["date"])
        except Exception:
            continue
        bucket = per_office.setdefault(office, {
            "yesterday_spend_usd": 0.0, "yesterday_conv": 0.0,
            "last7_spend_usd": 0.0, "last7_conv": 0.0, "last7_days": 0,
            "last30_spend_usd": 0.0, "last30_conv": 0.0, "last30_days": 0,
            "daily_series": [],  # for sparkline
            "child_accounts": set(),
        })
        bucket["child_accounts"].add(r["customer_id"])
        bucket["daily_series"].append({"date": r["date"], "spend": r["cost_usd"], "conv": r["conversions"]})
        if d == yesterday:
            bucket["yesterday_spend_usd"] += r["cost_usd"]
            bucket["yesterday_conv"] += r["conversions"]
        if last7_start <= d <= yesterday:
            bucket["last7_spend_usd"] += r["cost_usd"]
            bucket["last7_conv"] += r["conversions"]
            bucket["last7_days"] = max(bucket["last7_days"], (yesterday - last7_start).days + 1)
        if last30_start <= d <= yesterday:
            bucket["last30_spend_usd"] += r["cost_usd"]
            bucket["last30_conv"] += r["conversions"]
            bucket["last30_days"] = max(bucket["last30_days"], (yesterday - last30_start).days + 1)

    # derive run-rates
    for office, b in per_office.items():
        # yesterday × 30
        b["yesterday_x30_usd"] = round(b["yesterday_spend_usd"] * 30, 2)
        # last 7d avg × 30 (smoother run-rate)
        avg7 = b["last7_spend_usd"] / 7.0 if b["last7_days"] >= 7 else (b["last7_spend_usd"] / max(b["last7_days"],1))
        b["last7_avg_daily_usd"] = round(avg7, 2)
        b["last7_runrate_x30_usd"] = round(avg7 * 30, 2)
        # last 30d total = projected monthly
        b["last30_total_usd"] = round(b["last30_spend_usd"], 2)
        b["last30_avg_daily_usd"] = round(b["last30_spend_usd"] / max(b["last30_days"],1), 2)
        # velocity: how does last-7d pace compare to last-30d pace
        avg30 = b["last30_avg_daily_usd"]
        b["velocity_pct_vs_30d"] = round((avg7 - avg30) / avg30 * 100.0, 1) if avg30 > 0 else None
        # cpa windows
        b["last7_cpa_usd"] = round(b["last7_spend_usd"] / b["last7_conv"], 2) if b["last7_conv"] > 0 else None
        b["last30_cpa_usd"] = round(b["last30_spend_usd"] / b["last30_conv"], 2) if b["last30_conv"] > 0 else None
        b["child_accounts"] = sorted(b["child_accounts"])
        # round
        for k in ("yesterday_spend_usd","yesterday_conv","last7_spend_usd","last7_conv","last30_spend_usd","last30_conv"):
            b[k] = round(b[k], 2)
        # sparkline: sort daily series by date
        b["daily_series"] = sorted(b["daily_series"], key=lambda x: x["date"])
    return per_office

# Some leaderboard rows use legacy names with +AI suffix or other variants.
# Map those to the canonical office buckets we computed.
LEADERBOARD_ALIAS = {
    "Riverpark+AI": "Oxnard Riverpark",
    "Beverly Hills+AI": "Beverly Hills",
    "Santa Monica+AI": "Santa Monica",
    "Encino+AI": "Encino",
    "Sherman Oaks+AI": "Sherman Oaks",
}

def merge_into_snapshot(per_office):
    s = json.loads(SNAP.read_text())
    ads = s.setdefault("google_ads_insights", {})
    leaderboard = ads.get("office_leaderboard", [])

    # for each leaderboard row, patch in new velocity fields
    for row in leaderboard:
        off = row.get("office")
        canonical = LEADERBOARD_ALIAS.get(off, off)
        if canonical in per_office:
            off = canonical
            row["office"] = canonical  # normalize display name
            b = per_office[off]
            row["yesterday_spend_usd"] = b["yesterday_spend_usd"]
            row["yesterday_x30_usd"] = b["yesterday_x30_usd"]
            row["last7d_spend_usd"] = b["last7_spend_usd"]
            row["last7d_avg_daily_usd"] = b["last7_avg_daily_usd"]
            row["last7d_runrate_x30_usd"] = b["last7_runrate_x30_usd"]
            row["last30d_spend_usd"] = b["last30_total_usd"]
            row["last30d_avg_daily_usd"] = b["last30_avg_daily_usd"]
            row["velocity_pct_vs_30d"] = b["velocity_pct_vs_30d"]
            row["last7d_cpa_usd"] = b["last7_cpa_usd"]
            row["last30d_cpa_usd"] = b["last30_cpa_usd"]
            # keep old fields fresh too
            row["trailing_30d_spend_usd"] = b["last30_total_usd"]
            row["daily_runrate_x30_usd"] = b["last7_runrate_x30_usd"]  # use 7d-smoothed run rate now
            row["data_freshness"] = "2026-06-11"

    # Add a new compact roll-up block (totals across all offices)
    tot = {
        "yesterday_spend_usd": round(sum(b["yesterday_spend_usd"] for b in per_office.values()), 2),
        "yesterday_x30_usd":   round(sum(b["yesterday_x30_usd"]   for b in per_office.values()), 2),
        "last7d_spend_usd":    round(sum(b["last7_spend_usd"]     for b in per_office.values()), 2),
        "last7d_avg_daily_usd":round(sum(b["last7_avg_daily_usd"] for b in per_office.values()), 2),
        "last7d_runrate_x30_usd": round(sum(b["last7_runrate_x30_usd"] for b in per_office.values()), 2),
        "last30d_spend_usd":   round(sum(b["last30_total_usd"]    for b in per_office.values()), 2),
        "last30d_avg_daily_usd":round(sum(b["last30_avg_daily_usd"] for b in per_office.values()), 2),
        "offices_count": len(per_office),
        "data_freshness": "2026-06-11",
    }
    # Velocity vs 30d (org-wide)
    if tot["last30d_avg_daily_usd"] > 0:
        tot["velocity_pct_vs_30d"] = round((tot["last7d_avg_daily_usd"] - tot["last30d_avg_daily_usd"]) / tot["last30d_avg_daily_usd"] * 100.0, 1)
    else:
        tot["velocity_pct_vs_30d"] = None

    ads["spend_velocity"] = {
        "as_of": "2026-06-11",
        "windows": {
            "yesterday": "2026-06-10",
            "trailing_7d": "2026-06-04 .. 2026-06-10",
            "trailing_30d": "2026-05-12 .. 2026-06-10",
        },
        "org_totals": tot,
        "by_office": [
            {
                "office": off,
                "yesterday_spend_usd": b["yesterday_spend_usd"],
                "yesterday_x30_usd": b["yesterday_x30_usd"],
                "last7d_spend_usd": b["last7_spend_usd"],
                "last7d_avg_daily_usd": b["last7_avg_daily_usd"],
                "last7d_runrate_x30_usd": b["last7_runrate_x30_usd"],
                "last30d_spend_usd": b["last30_total_usd"],
                "last30d_avg_daily_usd": b["last30_avg_daily_usd"],
                "velocity_pct_vs_30d": b["velocity_pct_vs_30d"],
                "last7d_cpa_usd": b["last7_cpa_usd"],
                "last30d_cpa_usd": b["last30_cpa_usd"],
                "child_account_count": len(b["child_accounts"]),
                "daily_spend_series": [{"date":d["date"], "spend": round(d["spend"],2)} for d in b["daily_series"]],
            }
            for off, b in sorted(per_office.items(), key=lambda kv: -kv[1]["last30_total_usd"])
        ],
    }

    # Also update top-level totals
    t = ads.setdefault("totals", {})
    t["yesterday_spend_usd"] = tot["yesterday_spend_usd"]
    t["trailing_7d_spend_usd"] = tot["last7d_spend_usd"]
    t["trailing_7d_runrate_x30_usd"] = tot["last7d_runrate_x30_usd"]
    t["trailing_30d_spend_usd"] = tot["last30d_spend_usd"]
    t["daily_runrate_x30_usd"] = tot["last7d_runrate_x30_usd"]  # smoothed
    t["velocity_pct_vs_30d"] = tot["velocity_pct_vs_30d"]

    SNAP.write_text(json.dumps(s, indent=2))
    return tot

if __name__ == "__main__":
    rows = load_pulls()
    print("rows loaded:", len(rows))
    per_office = compute_windows(rows)
    tot = merge_into_snapshot(per_office)
    print("\n=== Org-wide spend velocity ===")
    print(f"  Yesterday      : ${tot['yesterday_spend_usd']:,.2f}  (x30 = ${tot['yesterday_x30_usd']:,.0f})")
    print(f"  Trailing 7d    : ${tot['last7d_spend_usd']:,.2f}  (avg ${tot['last7d_avg_daily_usd']:,.2f}/d, x30 = ${tot['last7d_runrate_x30_usd']:,.0f})")
    print(f"  Trailing 30d   : ${tot['last30d_spend_usd']:,.2f}  (avg ${tot['last30d_avg_daily_usd']:,.2f}/d)")
    print(f"  Velocity vs 30d: {tot['velocity_pct_vs_30d']:+.1f}%")
    print(f"  Offices       : {tot['offices_count']}")
    print("\n=== Per-office (sorted by 30d spend) ===")
    print(f"{'Office':<22} {'Yest':>10} {'Yx30':>10} {'7d':>10} {'7d/day':>9} {'7d×30':>10} {'30d':>10} {'30/day':>9} {'Δ%':>7}")
    for off, b in sorted(per_office.items(), key=lambda kv: -kv[1]['last30_total_usd']):
        v = b['velocity_pct_vs_30d']
        v_str = f"{v:+.1f}%" if v is not None else "—"
        print(f"{off:<22} ${b['yesterday_spend_usd']:>8,.0f} ${b['yesterday_x30_usd']:>8,.0f} ${b['last7_spend_usd']:>8,.0f} ${b['last7_avg_daily_usd']:>7,.0f} ${b['last7_runrate_x30_usd']:>8,.0f} ${b['last30_total_usd']:>8,.0f} ${b['last30_avg_daily_usd']:>7,.0f} {v_str:>7}")
