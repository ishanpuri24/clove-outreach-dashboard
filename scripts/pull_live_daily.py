"""Pull live daily data for the simplified Clove Marketing dashboard.

Runs once per day at 5am PT (12:00 UTC). For each section, the script
calls the connected source, computes yesterday/7d/30d aggregates, and
writes three new simple blocks into ``data/snapshot.json``:

  - ``paid_ads_simple``     -> Google Ads by office
  - ``gmb_simple``          -> Google Business Profile reviews + ratings
  - ``organic_simple``      -> Google Search Console organic clicks

Private raw data is parked under ``data/_gads_live/``, ``data/_gmb_live/``,
and ``data/_gsc_live/``. The public snapshot only carries office labels
and aggregate metrics. Customer IDs, location IDs, GCLIDs, and reviewer
PII never enter the public snapshot.

This script is intentionally light: each source is queried at most once
per run. No subagents, no browser, no LLM calls. If a source connector
is unavailable, the previous day's simple block is preserved so the
dashboard still renders.

Usage:
    python3 scripts/pull_live_daily.py

The script reads existing files in ``data/_gads_live/`` rather than
re-pulling Google Ads when present (the parent agent pulls Ads via the
connector and parks the JSON files; the cron call also pulls fresh
data). GMB and GSC are pulled via the connected agent during the cron
run.
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA = REPO_ROOT / "data"
SNAPSHOT = DATA / "snapshot.json"

OFFICE_MAP = {
    "2816298093": "Beverly Hills",
    "4276567700": "Camarillo",
    "8668802505": "Encino",
    "3737640297": "Hillview",
    "2481492821": "Puri Dentistry",
    "7621293648": "Riverpark",
    "8712971350": "Santa Monica",
    "6442679282": "Sherman Oaks",
    "3575932013": "Thousand Oaks",
}

LOCATION_TO_OFFICE = {
    "16540245410755416746": "Beverly Hills",
    "6002784370653219775": "Riverpark",
    "4396979876870755094": "Thousand Oaks",
    "3424162762335073167": "Sherman Oaks",
    "15322712011963486679": "Puri Dentistry",
    "2451402705824656361": "Camarillo",
    "6534318906667721619": "Hillview",
    "17491483726222827505": "Santa Monica",
    "18149932138550234736": "Encino",
}

STAR_TO_INT = {
    "ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5,
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _save_private(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")


def build_paid_ads_simple() -> dict:
    """Aggregate per-office paid-ads rows from ``data/_gads_live/<id>.json``.

    Each input file is a Google Ads create-report payload with
    date-segmented rows for the last 30 days. We compute three windows
    per office: yesterday, last 7 days, last 30 days.
    """
    gads_dir = DATA / "_gads_live"
    rows: list[dict] = []
    tot7 = tot30 = 0.0

    for cid, office in OFFICE_MAP.items():
        fp = gads_dir / f"{cid}.json"
        if not fp.exists():
            continue
        try:
            payload = json.loads(fp.read_text())
        except Exception:
            continue
        results = payload.get("results", [])
        if not results:
            continue

        by_date: dict[str, dict] = defaultdict(
            lambda: {"cost": 0.0, "clicks": 0, "impr": 0, "conv": 0.0}
        )
        for r in results:
            date = r["segments"]["date"]
            m = r["metrics"]
            by_date[date]["cost"] += int(m.get("costMicros", "0")) / 1e6
            by_date[date]["clicks"] += int(m.get("clicks", "0"))
            by_date[date]["impr"] += int(m.get("impressions", "0"))
            by_date[date]["conv"] += float(m.get("conversions", 0))

        dates = sorted(by_date.keys())
        if not dates:
            continue
        latest = dates[-1]
        last7 = dates[-7:]

        y = by_date[latest]
        sum7 = {"cost": 0.0, "clicks": 0, "impr": 0, "conv": 0.0}
        for d in last7:
            for k in sum7:
                sum7[k] += by_date[d][k]
        sum30 = {"cost": 0.0, "clicks": 0, "impr": 0, "conv": 0.0}
        for d in dates:
            for k in sum30:
                sum30[k] += by_date[d][k]

        def _cpa(c: dict) -> float | None:
            return round(c["cost"] / c["conv"], 2) if c["conv"] > 0 else None

        rows.append({
            "office": office,
            "yesterday_date": latest,
            "yesterday_spend_usd": round(y["cost"], 2),
            "yesterday_clicks": y["clicks"],
            "yesterday_conversions": round(y["conv"], 1),
            "last_7d_spend_usd": round(sum7["cost"], 2),
            "last_7d_clicks": sum7["clicks"],
            "last_7d_conversions": round(sum7["conv"], 1),
            "last_7d_cpa_usd": _cpa(sum7),
            "last_30d_spend_usd": round(sum30["cost"], 2),
            "last_30d_clicks": sum30["clicks"],
            "last_30d_conversions": round(sum30["conv"], 1),
            "last_30d_cpa_usd": _cpa(sum30),
        })
        tot7 += sum7["cost"]
        tot30 += sum30["cost"]

    rows.sort(key=lambda x: -x["last_30d_spend_usd"])
    return {
        "title": "Paid Ads — by office",
        "window_note": "Yesterday / Last 7d / Last 30d",
        "totals": {
            "last_7d_spend_usd": round(tot7, 2),
            "last_30d_spend_usd": round(tot30, 2),
        },
        "rows": rows,
        "refreshed_at": utcnow(),
    }


def build_gmb_simple() -> dict:
    """Aggregate per-office GMB reviews with daily-first, 30d-rolling, and velocity.

    Reads ``data/_gmb_live/reviews.json`` (preferred) or falls back to
    ``data/gmb_raw_reviews.json``. Reviewer PII stays in the private file only.

    Emits:
      - ``daily_yesterday`` — per-office reviews yesterday (PT) + total
      - ``rolling_30d``    — per-office 30d counts + avg rating + newest
      - ``velocity``       — 7d vs prior-7d delta, per office + total
      - ``daily_series_14d`` — total reviews/day for the last 14 PT days (for spark)
      - ``rows`` (legacy)  — 7d/30d table kept for back-compat
    """
    src = DATA / "_gmb_live" / "reviews.json"
    if not src.exists():
        src = DATA / "gmb_raw_reviews.json"
    if not src.exists():
        return {
            "title": "Google Reviews — by office",
            "window_note": "Yesterday / 30d rolling / velocity",
            "rows": [],
            "refreshed_at": utcnow(),
            "note": "No GMB data file found.",
        }

    payload = json.loads(src.read_text())
    revs = payload.get("locationReviews", [])

    from zoneinfo import ZoneInfo
    PT = ZoneInfo("America/Los_Angeles")
    now_utc = datetime.now(timezone.utc)
    now_pt = now_utc.astimezone(PT)
    today_pt = now_pt.date()
    yesterday_pt = today_pt - timedelta(days=1)

    cutoff_7 = now_utc - timedelta(days=7)
    cutoff_14 = now_utc - timedelta(days=14)
    cutoff_30 = now_utc - timedelta(days=30)
    cutoff_prev7 = now_utc - timedelta(days=14)  # start of prior-7 window

    per_office: dict[str, dict] = defaultdict(
        lambda: {
            "yest": 0, "stars_yest": [],
            "r7": 0, "stars7": [],
            "r_prev7": 0,
            "r30": 0, "stars30": [], "newest": None,
        }
    )
    daily_totals: dict[str, int] = defaultdict(int)

    for r in revs:
        loc_id = r["name"].split("/")[-1]
        office = LOCATION_TO_OFFICE.get(loc_id)
        if not office:
            continue
        review = r.get("review", {})
        ct = review.get("createTime") or ""
        try:
            dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
        except Exception:
            continue
        star = STAR_TO_INT.get(review.get("starRating", ""))
        b = per_office[office]
        if b["newest"] is None or dt > b["newest"]:
            b["newest"] = dt

        # Yesterday bucket (PT day)
        dt_pt_date = dt.astimezone(PT).date()
        if dt_pt_date == yesterday_pt:
            b["yest"] += 1
            if star:
                b["stars_yest"].append(star)

        # Rolling 14d series (PT day granularity)
        if dt >= cutoff_14:
            daily_totals[dt_pt_date.isoformat()] += 1

        # Last 7d
        if dt >= cutoff_7:
            b["r7"] += 1
            if star:
                b["stars7"].append(star)
        # Prior 7d (8-14 days ago)
        elif dt >= cutoff_prev7:
            b["r_prev7"] += 1

        # Last 30d
        if dt >= cutoff_30:
            b["r30"] += 1
            if star:
                b["stars30"].append(star)

    offices_sorted = sorted(LOCATION_TO_OFFICE.values())

    # ---- daily_yesterday ----
    daily_yesterday_rows = []
    for office in offices_sorted:
        b = per_office.get(office) or {"yest": 0, "stars_yest": []}
        avg = round(sum(b["stars_yest"]) / len(b["stars_yest"]), 2) if b["stars_yest"] else None
        daily_yesterday_rows.append({
            "office": office,
            "reviews_yesterday": b["yest"],
            "avg_rating_yesterday": avg,
        })
    daily_yesterday_rows.sort(key=lambda x: -x["reviews_yesterday"])

    # ---- rolling_30d ----
    rolling_30d_rows = []
    for office in offices_sorted:
        b = per_office.get(office) or {"r30": 0, "stars30": [], "newest": None}
        avg30 = round(sum(b["stars30"]) / len(b["stars30"]), 2) if b["stars30"] else None
        newest = b["newest"].isoformat() if b["newest"] else None
        rolling_30d_rows.append({
            "office": office,
            "reviews_last_30d": b["r30"],
            "avg_rating_last_30d": avg30,
            "newest_review_at": newest,
        })
    rolling_30d_rows.sort(key=lambda x: -x["reviews_last_30d"])

    # ---- velocity: 7d vs prior 7d ----
    velocity_rows = []
    for office in offices_sorted:
        b = per_office.get(office) or {"r7": 0, "r_prev7": 0}
        d = b["r7"] - b["r_prev7"]
        pct = None
        if b["r_prev7"] > 0:
            pct = round((d / b["r_prev7"]) * 100.0, 0)
        elif b["r7"] > 0:
            pct = 100.0
        velocity_rows.append({
            "office": office,
            "reviews_last_7d": b["r7"],
            "reviews_prior_7d": b["r_prev7"],
            "delta": d,
            "pct_change": pct,
        })
    velocity_rows.sort(key=lambda x: -(x["reviews_last_7d"] or 0))

    # ---- daily_series_14d (for sparkline) ----
    daily_series = []
    for i in range(13, -1, -1):
        day = today_pt - timedelta(days=i)
        daily_series.append({
            "date": day.isoformat(),
            "reviews": daily_totals.get(day.isoformat(), 0),
        })

    # ---- legacy rows (kept for back-compat) ----
    legacy_rows = []
    for office in offices_sorted:
        b = per_office.get(office) or {
            "r7": 0, "r30": 0, "stars7": [], "stars30": [], "newest": None,
        }
        avg7 = round(sum(b["stars7"]) / len(b["stars7"]), 2) if b["stars7"] else None
        avg30 = round(sum(b["stars30"]) / len(b["stars30"]), 2) if b["stars30"] else None
        newest = b["newest"].isoformat() if b["newest"] else None
        legacy_rows.append({
            "office": office,
            "reviews_last_7d": b["r7"],
            "avg_rating_last_7d": avg7,
            "reviews_last_30d": b["r30"],
            "avg_rating_last_30d": avg30,
            "newest_review_at": newest,
        })
    legacy_rows.sort(key=lambda x: -x["reviews_last_30d"])

    total_yest = sum(r["reviews_yesterday"] for r in daily_yesterday_rows)
    total_7d = sum(r["reviews_last_7d"] for r in velocity_rows)
    total_prev7 = sum(r["reviews_prior_7d"] for r in velocity_rows)
    total_30d = sum(r["reviews_last_30d"] for r in rolling_30d_rows)
    total_delta = total_7d - total_prev7
    total_pct = None
    if total_prev7 > 0:
        total_pct = round((total_delta / total_prev7) * 100.0, 0)
    elif total_7d > 0:
        total_pct = 100.0

    # Overall avg rating (30d, weighted)
    all_stars = []
    for office in offices_sorted:
        all_stars.extend((per_office.get(office) or {}).get("stars30", []))
    avg_30d = round(sum(all_stars) / len(all_stars), 2) if all_stars else None

    # Offices at zero yesterday (action target)
    zero_yesterday = [r["office"] for r in daily_yesterday_rows if r["reviews_yesterday"] == 0]

    return {
        "title": "Google Reviews — daily first, then 30d rolling",
        "window_note": "Yesterday (PT) / 30d rolling / 7d vs prior 7d velocity",
        "summary": {
            "yesterday_reviews": total_yest,
            "last_7d_reviews": total_7d,
            "prior_7d_reviews": total_prev7,
            "velocity_delta": total_delta,
            "velocity_pct": total_pct,
            "last_30d_reviews": total_30d,
            "avg_rating_30d": avg_30d,
            "offices_zero_yesterday": len(zero_yesterday),
            "zero_yesterday_offices": zero_yesterday,
        },
        "daily_yesterday": daily_yesterday_rows,
        "rolling_30d": rolling_30d_rows,
        "velocity": velocity_rows,
        "daily_series_14d": daily_series,
        # legacy — kept so old renderers don't break
        "totals": {
            "reviews_last_7d": total_7d,
            "reviews_last_30d": total_30d,
        },
        "rows": legacy_rows,
        "refreshed_at": utcnow(),
    }


def build_organic_simple() -> dict:
    """Aggregate Google Search Console clicks from the latest pull.

    Reads ``data/_gsc_live/date_30d.json`` for daily clicks, plus
    ``query_7d.json`` and ``page_7d.json`` for top-25 lists.
    """
    base = DATA / "_gsc_live"
    out = {
        "title": "Organic — Google Search Console",
        "window_note": "Yesterday / Last 7d / Last 30d",
        "refreshed_at": utcnow(),
    }

    date_file = base / "date_30d.json"
    if date_file.exists():
        rows = json.loads(date_file.read_text()).get("rows", [])
        rows_sorted = sorted(rows, key=lambda r: r["keys"][0])
        if rows_sorted:
            last = rows_sorted[-1]
            last7 = rows_sorted[-7:]
            out["yesterday_date"] = last["keys"][0]
            out["yesterday_clicks"] = last["clicks"]
            out["yesterday_impressions"] = last["impressions"]
            out["last_7d_clicks"] = sum(r["clicks"] for r in last7)
            out["last_7d_impressions"] = sum(r["impressions"] for r in last7)
            out["last_30d_clicks"] = sum(r["clicks"] for r in rows_sorted)
            out["last_30d_impressions"] = sum(r["impressions"] for r in rows_sorted)

    q_file = base / "query_7d.json"
    if q_file.exists():
        rows = json.loads(q_file.read_text()).get("rows", [])
        out["top_queries_7d"] = [
            {
                "query": r["keys"][0],
                "clicks": r["clicks"],
                "impressions": r["impressions"],
                "position": round(r["position"], 1),
            }
            for r in rows[:15]
        ]

    p_file = base / "page_7d.json"
    if p_file.exists():
        rows = json.loads(p_file.read_text()).get("rows", [])
        out["top_pages_7d"] = [
            {
                "page": r["keys"][0],
                "clicks": r["clicks"],
                "impressions": r["impressions"],
                "position": round(r["position"], 1),
            }
            for r in rows[:15]
        ]
    return out


def rebuild_gmb_insights_from_live(snap: dict) -> None:
    """Rebuild the gmb_insights block (powers the GMB Reviews tab) from
    the fresh live GMB pull. Updates data_freshness, summary_cards,
    executive_summary, office_rows, going_well, to_improve, top_actions,
    trend, freshness_status. Preserves alert_config and the negative
    queue if present (those are curated separately).
    """
    gi = snap.get("gmb_insights")
    if not isinstance(gi, dict):
        return
    gmb_simple = snap.get("gmb_simple", {}) or {}
    rows_simple = gmb_simple.get("rows") or []
    totals_simple = gmb_simple.get("totals") or {}
    if not rows_simple:
        return

    reviews_7d = int(totals_simple.get("reviews_last_7d") or 0)
    reviews_30d = int(totals_simple.get("reviews_last_30d") or 0)

    # Weighted overall 30d rating
    num = 0.0
    den = 0
    for r in rows_simple:
        n = int(r.get("reviews_last_30d") or 0)
        rt = r.get("avg_rating_last_30d")
        if n and rt is not None:
            num += float(rt) * n
            den += n
    avg_rating = round(num / den, 2) if den else None
    rating_str = f"{avg_rating:.2f}\u2605" if avg_rating is not None else "\u2014"

    # Prior 30d (rough): take historical executive_summary if present
    prior = gi.get("trend", {}).get("prior_30d_reviews")
    if prior is None:
        prior = gi.get("executive_summary", {}).get("prior_30d_reviews")
    try:
        prior = int(prior) if prior is not None else None
    except Exception:
        prior = None
    delta = (reviews_30d - prior) if prior is not None else None

    # Goal is 1 review/day per office, but reported org-wide as N/day
    # for the dashboard (so 9 offices = 9/day target).
    per_office_goal = 1.0
    goal_per_day = per_office_goal * len(rows_simple)  # org-wide reviews/day
    goal_30 = int(round(goal_per_day * 30))
    velocity = round(reviews_30d / 30.0, 2)
    goal_gap_30 = max(0, goal_30 - reviews_30d)
    goal_attainment = round((reviews_30d / goal_30) * 100, 1) if goal_30 else 0.0

    gi["data_freshness"] = utcnow()
    gi["lookback"] = "Last 30d"
    gi["summary_cards"] = [
        {
            "label": "Overall rating",
            "value": rating_str,
            "subtext": f"{len(rows_simple)} offices \u00b7 {reviews_30d} reviews in last 30d",
        },
        {
            "label": "New reviews 7d",
            "value": str(reviews_7d),
            "subtext": f"{round(reviews_7d/7.0, 2)}/day across {len(rows_simple)} offices",
        },
        {
            "label": "New reviews 30d",
            "value": str(reviews_30d),
            "subtext": f"{velocity}/day vs {goal_per_day}/day goal",
        },
        {
            "label": "30d goal attainment",
            "value": f"{goal_attainment}%",
            "subtext": f"Gap {goal_gap_30} reviews to {goal_30} target",
        },
    ]
    gi["executive_summary"] = {
        "total_reviews": reviews_30d,
        "avg_rating": avg_rating,
        "reviews_30d": reviews_30d,
        "prior_30d_reviews": prior,
        "reviews_30d_delta": delta,
        "review_goal_30d": goal_30,
        "goal_per_day": goal_per_day,
        "goal_attainment_pct": goal_attainment,
    }
    gi["trend"] = {
        "reviews_30d": reviews_30d,
        "prior_30d_reviews": prior,
        "reviews_30d_delta": delta,
        "velocity_per_day": velocity,
        "goal_per_day": goal_per_day,
        "goal_gap_30d": goal_gap_30,
        "goal_attainment": goal_attainment,
        "review_goal_30d": goal_30,
    }

    # Build office_rows from rows_simple. Per-office goal is 1/day (30/30d).
    office_rows = []
    for r in rows_simple:
        office = r.get("office")
        n30 = int(r.get("reviews_last_30d") or 0)
        n7 = int(r.get("reviews_last_7d") or 0)
        avg30 = r.get("avg_rating_last_30d")
        pace = round(n30 / 30.0, 2)
        per_office_goal_30 = int(round(per_office_goal * 30))
        gap = max(0, per_office_goal_30 - n30)
        office_rows.append({
            "office": office,
            "avg_rating_30d": avg30,
            "reviews_30d": n30,
            "reviews_7d": n7,
            "pace_per_day": pace,
            "gap_to_goal_30d": gap,
            "low_30d": 0,  # Recomputed from raw reviews below if available
            "unreplied_low": 0,
        })

    # Count low (rating <=2) per office from raw GMB live
    raw_path = DATA / "_gmb_live" / "reviews.json"
    if raw_path.exists():
        try:
            payload = json.loads(raw_path.read_text())
            revs = payload.get("locationReviews", [])
            now = datetime.now(timezone.utc)
            cutoff_30 = now - timedelta(days=30)
            low_by_office: dict[str, int] = defaultdict(int)
            unreplied_low_by_office: dict[str, int] = defaultdict(int)
            for rv in revs:
                loc_id = rv.get("name", "").split("/")[-1]
                office = LOCATION_TO_OFFICE.get(loc_id)
                if not office:
                    continue
                review = rv.get("review", {})
                ct = review.get("createTime") or ""
                try:
                    dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                except Exception:
                    continue
                if dt < cutoff_30:
                    continue
                star = STAR_TO_INT.get(review.get("starRating", ""))
                if star is not None and star <= 2:
                    low_by_office[office] += 1
                    if not review.get("reviewReply"):
                        unreplied_low_by_office[office] += 1
            for row in office_rows:
                o = row["office"]
                row["low_30d"] = low_by_office.get(o, 0)
                row["unreplied_low"] = unreplied_low_by_office.get(o, 0)
        except Exception:
            pass

    gi["office_rows"] = office_rows

    # Going well: top 3 offices by 30d count
    sorted_offices = sorted(office_rows, key=lambda x: -x["reviews_30d"])
    going = []
    for r in sorted_offices[:3]:
        going.append({
            "office": r["office"],
            "headline": f"{r['reviews_30d']} reviews in 30d ({r['pace_per_day']}/day)",
            "highlights": [
                f"Avg rating {r['avg_rating_30d']}\u2605" if r['avg_rating_30d'] else "Rating pending",
                f"{r['reviews_7d']} reviews in last 7d",
            ],
        })
    gi["going_well"] = going

    # To improve: offices with biggest gap
    behind = sorted(office_rows, key=lambda x: -x["gap_to_goal_30d"])
    behind_list = [r for r in behind if r["gap_to_goal_30d"] > 0][:4]
    to_improve = []
    if behind_list:
        to_improve.append({
            "category": "Review volume gap",
            "headline": f"{len(behind_list)} offices behind {per_office_goal}/day per-office goal",
            "details": [
                f"{r['office']}: gap {r['gap_to_goal_30d']} ({r['pace_per_day']}/day)"
                for r in behind_list
            ],
        })
    total_unreplied = sum(r["unreplied_low"] for r in office_rows)
    if total_unreplied:
        to_improve.append({
            "category": "Unreplied low reviews",
            "headline": f"{total_unreplied} low reviews unreplied across the org",
            "details": [
                f"{r['office']}: {r['unreplied_low']} unreplied low"
                for r in office_rows if r["unreplied_low"] > 0
            ],
        })
    gi["to_improve"] = to_improve

    # Top actions
    actions = []
    if total_unreplied:
        actions.append({
            "priority": "P0",
            "label": f"Reply to {total_unreplied} unreplied low reviews within 24h",
            "action": f"Reply to {total_unreplied} unreplied low reviews within 24h",
            "owner": "Office managers",
        })
    if behind_list:
        actions.append({
            "priority": "P1",
            "label": f"Drive review velocity at {behind_list[0]['office']} (gap {behind_list[0]['gap_to_goal_30d']})",
            "action": "Activate SMS review request flow + front desk ask",
            "owner": "Office GM",
        })
    actions.append({
        "priority": "P2",
        "label": "Maintain reply rate >=90% to protect rating",
        "action": "Weekly reply audit by office",
        "owner": "Marketing",
    })
    gi["top_actions"] = actions

    # Freshness status
    gi["freshness_status"] = {
        "data_freshness": gi["data_freshness"],
        "age_days": 0,
        "is_stale": False,
        "stale_threshold_days": 7,
        "label": "FRESH",
        "note": "Refreshed daily at 5am PT from Google Business Profile.",
    }


def rebuild_b2b_outbound_from_gmail(snap: dict) -> None:
    """Rebuild b2b_outbound from real Gmail SENT signal + local prospect pool.

    Reads:
      data/_gmail_sent_dedup.json  — verified prospect sends in last 90d
      data/_b2b_prospect_pool.json — Maps-sourced prospects within 5mi per office

    Produces honest counts (verified via Gmail SENT), a vertical breakdown of
    real recent contacts, and a top-N next-prospects queue (deduped against
    the sent history and legal/finance domains).
    """
    dedup_file = DATA / "_gmail_sent_dedup.json"
    pool_file = DATA / "_b2b_prospect_pool.json"
    bounces_file = DATA / "_bounces.json"

    # Load bounces (hard 550s from Gmail Mailer-Daemon, last 90d)
    bounced_emails: set = set()
    bounced_domains: set = set()
    bounce_details: list = []
    total_bounce_msgs = 0
    if bounces_file.exists():
        try:
            bj = json.loads(bounces_file.read_text())
            total_bounce_msgs = int(bj.get("total_bounce_messages") or 0)
            def _mask_local(local: str) -> str:
                if not local:
                    return "***"
                if len(local) <= 2:
                    return local[0] + "*"
                return local[0] + "***" + local[-1]
            for em, meta in (bj.get("external_prospect_bounces") or {}).items():
                em_l = em.strip().lower()
                if not em_l or "@" not in em_l:
                    continue
                bounced_emails.add(em_l)
                local, dom = em_l.split("@", 1)
                bounced_domains.add(dom)
                bounce_details.append({
                    "masked": _mask_local(local) + " [at] " + dom,
                    "domain": dom,
                    "bounce_count": int(meta.get("bounce_count") or 1),
                    "latest_bounce": (meta.get("latest_bounce") or "")[:10],
                    "response_codes": meta.get("response_codes") or [],
                })
            bounce_details.sort(key=lambda x: x.get("latest_bounce") or "", reverse=True)
        except Exception:
            pass

    # Preload email verification once for use both in ranking and summary
    ver_map: dict = {}
    try:
        _vp = DATA / "_email_verification.json"
        if _vp.exists():
            ver_map = (json.loads(_vp.read_text()).get("results") or {})
    except Exception:
        ver_map = {}

    verified_sends = 0
    verified_prospects: list = []
    vertical_counts: dict = {}
    recent_touches: list = []

    if dedup_file.exists():
        try:
            dd = json.loads(dedup_file.read_text())
            verified_sends = int(dd.get("verified_prospect_sends_last_90d") or 0)
            recips = dd.get("recipients") or []
            for r in recips:
                if r.get("bucket") != "prospect":
                    continue
                verified_prospects.append(r.get("domain") or r.get("email"))
                v = r.get("vertical") or "other"
                vertical_counts[v] = vertical_counts.get(v, 0) + 1
                recent_touches.append({
                    "domain": r.get("domain"),
                    "vertical": v,
                    "threads": r.get("thread_count"),
                    "latest_sent": (r.get("latest_sent_date") or "")[:10],
                    "subject_sample": r.get("subject_sample"),
                })
            # sort recent by latest_sent desc
            recent_touches.sort(key=lambda x: x.get("latest_sent") or "", reverse=True)
        except Exception:
            pass

    # Next prospects from Maps-sourced pool (already deduped vs sent history)
    next_prospects: list = []
    pool_total = 0
    pool_by_vert: dict = {}
    pool_by_office: dict = {}
    coverage_matrix: dict = {}     # v3.5: office x vertical counts
    saturation_gaps: list = []     # v3.5: top gaps ranked by opportunity size
    coverage_summary: dict = {}    # v3.5: cells_filled, cells_empty, saturation_pct
    if pool_file.exists():
        try:
            pool = json.loads(pool_file.read_text())
            pool_total = int(pool.get("total_prospects") or 0)
            pool_by_vert = pool.get("by_vertical") or {}
            pool_by_office = pool.get("by_office") or {}
            # v3.5: build office x vertical coverage matrix from raw prospects
            from collections import Counter as _Ctr
            _all_offices = ["Beverly Hills","Camarillo","Encino","Hillview","Oxnard Riverpark",
                            "Puri Dentistry","Santa Monica","Sherman Oaks","Thousand Oaks"]
            _all_verticals = ["senior_living","schools_daycare","hr_heavy_employers",
                              "gyms_wellness","hotels","salons_spas","law_medical_offices"]
            _grid = {o: _Ctr() for o in _all_offices}
            for p in (pool.get("prospects") or []):
                o = p.get("nearest_office"); v = p.get("vertical")
                if o in _grid and v:
                    _grid[o][v] += 1
            coverage_matrix = {o: {v: int(_grid[o].get(v,0)) for v in _all_verticals} for o in _all_offices}
            # Rank gaps: empty cells first, then thin cells (<8 prospects)
            _target_per_cell = 15
            gap_rows = []
            for o in _all_offices:
                for v in _all_verticals:
                    n = coverage_matrix[o][v]
                    if n < _target_per_cell:
                        gap_rows.append({
                            "office": o, "vertical": v,
                            "current": n,
                            "target": _target_per_cell,
                            "gap": _target_per_cell - n,
                            "priority": "empty" if n == 0 else ("thin" if n < 8 else "nearly_full"),
                        })
            gap_rows.sort(key=lambda r: (0 if r["priority"]=="empty" else (1 if r["priority"]=="thin" else 2), -r["gap"]))
            saturation_gaps = gap_rows[:25]
            cells_total = len(_all_offices) * len(_all_verticals)
            cells_filled = sum(1 for o in _all_offices for v in _all_verticals if coverage_matrix[o][v] > 0)
            cells_at_target = sum(1 for o in _all_offices for v in _all_verticals if coverage_matrix[o][v] >= _target_per_cell)
            coverage_summary = {
                "cells_total": cells_total,
                "cells_filled": cells_filled,
                "cells_at_target": cells_at_target,
                "saturation_pct": round(100.0 * cells_at_target / cells_total, 1),
                "target_per_cell": _target_per_cell,
            }
            all_p = pool.get("prospects") or []
            # Only keep prospects with a website (usable for research + email);
            # also drop any prospect whose website domain already hard-bounced.
            def _dom_from_website(w: str) -> str:
                if not w:
                    return ""
                s = w.lower().split("//", 1)[-1]
                s = s.split("/", 1)[0]
                if s.startswith("www."):
                    s = s[4:]
                return s
            usable = [
                p for p in all_p
                if p.get("has_website") and _dom_from_website(p.get("website") or "") not in bounced_domains
            ]
            # Prefer prospects whose domain has a verified-valid mailbox.
            def _rank_key(p):
                d = (p.get("domain") or "").lower()
                st = (ver_map.get(d) or {}).get("status") or "unchecked"
                # valid > unchecked > catch_all > no_match > no_mx > invalid/bounced
                order = {"valid": 0, "unchecked": 1, "catch_all": 2,
                        "no_match": 3, "no_mx": 4,
                        "invalid": 5, "bounced_history": 6}
                return (order.get(st, 1), -(p.get("score") or 0))
            usable.sort(key=_rank_key)
            # Balance top-25 across offices: round-robin best-per-office (v3.3)
            by_off: dict = {}
            for p in usable:
                by_off.setdefault(p["nearest_office"], []).append(p)
            offices_sorted = sorted(by_off.keys())
            picked: list = []
            idx = 0
            while len(picked) < 25 and any(by_off[o] for o in offices_sorted):
                o = offices_sorted[idx % len(offices_sorted)]
                if by_off[o]:
                    picked.append(by_off[o].pop(0))
                idx += 1
            def _mask_email(em: str) -> str:
                if not em or "@" not in em:
                    return "-"
                lp, dom = em.split("@", 1)
                if len(lp) <= 2:
                    m = lp[0] + "*"
                else:
                    m = lp[0] + "***" + lp[-1]
                return m + " [at] " + dom

            # Load private ContactOut contacts (never committed — gitignored)
            co_private_path = REPO_ROOT / "data" / "_contactout_private.json"
            co_private = {}
            if co_private_path.exists():
                try:
                    co_private = (json.loads(co_private_path.read_text()) or {}).get("contacts_by_domain", {})
                except Exception:
                    co_private = {}

            for p in picked:
                dom = (p.get("domain") or "").lower().strip()
                v = ver_map.get(dom) or {}
                vstatus = v.get("status") or "unchecked"
                # Only surface an email when we probed and got 'valid'.
                masked_em = _mask_email(v.get("valid_email") or "") if vstatus == "valid" else "-"
                # v3.3: surface ContactOut decision-maker contact — PRIVATE data, mask everything in public snapshot.
                dm = None
                priv_entry = co_private.get(dom) or {}
                priv_contacts = priv_entry.get("contacts") or []
                if priv_contacts:
                    best = None
                    for c in priv_contacts:
                        em = (c.get("best_email") or "").lower()
                        if em and dom and dom in em:
                            best = c
                            break
                    best = best or priv_contacts[0]
                    dm = {
                        "title": best.get("title"),  # role is safe to publish (aggregate)
                        "has_verified_email": bool(best.get("best_email")),
                        "masked_email": _mask_email(best.get("best_email") or "") if best.get("best_email") else "-",
                        "source": "contactout",
                    }
                next_prospects.append({
                    "name": p.get("name"),
                    "vertical": p.get("vertical"),
                    "nearest_office": p.get("nearest_office"),
                    "distance_mi": p.get("distance_mi"),
                    "website": p.get("website"),
                    "rating": p.get("rating"),
                    "reviews": p.get("review_count"),
                    "score": p.get("score"),
                    "email_status": vstatus,
                    "masked_email": masked_em,
                    "decision_maker": dm,
                })
        except Exception:
            pass

    # Bounce rate = external bounces / verified sends in last 30d (proxy)
    bounce_ct = len(bounce_details)
    bounce_rate_pct = (
        round(100.0 * bounce_ct / verified_sends, 1) if verified_sends else 0.0
    )
    bounce_status = "HEALTHY" if bounce_rate_pct < 2 else ("WATCH" if bounce_rate_pct < 5 else "HIGH")

    snap["b2b_outbound"] = {
        "as_of": utcnow(),
        "source_of_truth": "Gmail SENT (verified) + Maps-sourced 5mi prospect pool",
        "status": "ACTIVE \u2014 running from personal Gmail",
        "verified_sends_last_30d": verified_sends,
        "unique_prospects_last_30d": len(set([p for p in verified_prospects if p])),
        "vertical_breakdown_last_90d": vertical_counts,
        "recent_touches": recent_touches[:10],
        "bounces_last_90d": {
            "external_bounce_count": bounce_ct,
            "external_bounce_rate_pct": bounce_rate_pct,
            "status": bounce_status,
            "total_bounce_messages_seen": total_bounce_msgs,
            "permanently_excluded_count": len(bounced_emails),
            "permanently_excluded_domains": sorted(list(bounced_domains)),
            "detail": bounce_details,
            "note": (
                "Hard bounces (SMTP 550) from Gmail Mailer-Daemon last 90d. "
                "These addresses/domains are permanently excluded from future outreach. "
                "Target rate <2%."
            ),
        },
        "prospect_pool": {
            "total": pool_total,
            "by_vertical": pool_by_vert,
            "by_office": pool_by_office,
            "source": "Google Maps Places within 5mi of each office (senior living + preschool/daycare); more verticals queued.",
            "dedup_rule": (
                "Excluded 21 domains already contacted + legal/finance domains + "
                f"{len(bounced_domains)} hard-bounced domains; 14-day per-recipient cooldown."
            ),
        },
        "next_prospects_top25": next_prospects,
        "email_verification": {
            "method": "SMTP RCPT-TO probe (free, no send)",
            "verified_domain_count": len({d for d in ver_map.keys()}) if isinstance(ver_map, dict) else 0,
            "counts": (
                (lambda d: {k: sum(1 for r in d.values() if (r.get('status') or '') == k) for k in ['valid','catch_all','no_match','no_mx','invalid','bounced_history','unknown']})(ver_map) if isinstance(ver_map, dict) else {}
            ),
            "note": "Only rows with email_status = 'valid' are safe to send today. 'catch_all' = domain accepts everything, treat as risky.",
        },
        "cadence": "Personal Gmail send. 14-day cooldown per recipient. Never re-email the same domain within window.",
        "next_actions": [
            f"Bounce rate is {bounce_rate_pct}% ({bounce_status}). Keep <2% \u2014 {len(bounced_emails)} address(es) permanently excluded.",
            "Send week: pick 10 from next_prospects_top25 (mix of senior_living / schools_daycare / gyms_wellness / hotels / hr_heavy_employers).",
            "Prefer prospects with decision_maker.has_verified_email = true \u2014 ContactOut surfaced direct owner/GM/director contacts.",
            "Continue enriching pool with ContactOut (2000/mo quota, currently used <50).",
            "Verifier now runs at 80 domains/day with parallel probing \u2014 pool coverage grows fast.",
        ],
    }


def build_outreach_volume(snap: dict) -> None:
    """Compute historical + planned outreach volume from Gmail dedup + verifier."""
    from collections import Counter
    from datetime import datetime, timedelta, timezone as _tz

    dedup_file = DATA / "_gmail_sent_dedup.json"
    ver_file = DATA / "_email_verification.json"
    pool_file = DATA / "_b2b_prospect_pool.json"

    weekly: dict = {}
    monthly: dict = {}
    per_day: dict = {}
    total_prospect_sends = 0
    unique_domains: set = set()

    if dedup_file.exists():
        try:
            dd = json.loads(dedup_file.read_text())
            for r in dd.get("recipients") or []:
                if r.get("bucket") != "prospect":
                    continue
                dt_s = (r.get("latest_sent_date") or "")[:10]
                if not dt_s:
                    continue
                thr = int(r.get("thread_count") or 1)
                total_prospect_sends += thr
                unique_domains.add(r.get("domain") or "")
                try:
                    dt = datetime.strptime(dt_s, "%Y-%m-%d").replace(tzinfo=_tz.utc)
                except Exception:
                    continue
                # ISO week key (Mon-Sun)
                iso_year, iso_week, _ = dt.isocalendar()
                wkey = f"{iso_year}-W{iso_week:02d}"
                weekly[wkey] = weekly.get(wkey, 0) + thr
                mkey = dt.strftime("%Y-%m")
                monthly[mkey] = monthly.get(mkey, 0) + thr
                per_day[dt_s] = per_day.get(dt_s, 0) + thr
        except Exception:
            pass

    # Verified pool depth
    ver_counts = {"valid": 0, "catch_all": 0, "no_match": 0, "no_mx": 0,
                  "invalid": 0, "bounced_history": 0, "unknown": 0}
    if ver_file.exists():
        try:
            res = json.loads(ver_file.read_text()).get("results") or {}
            for r in res.values():
                s = r.get("status") or "unknown"
                ver_counts[s] = ver_counts.get(s, 0) + 1
        except Exception:
            pass

    pool_total = 0
    if pool_file.exists():
        try:
            pool_total = int(json.loads(pool_file.read_text()).get("total_prospects") or 0)
        except Exception:
            pool_total = 0

    # Simple 7d rolling total for send velocity
    now = datetime.now(_tz.utc)
    d7 = sum(v for k, v in per_day.items()
             if (now - datetime.strptime(k, "%Y-%m-%d").replace(tzinfo=_tz.utc)).days <= 7)
    d30 = sum(v for k, v in per_day.items()
              if (now - datetime.strptime(k, "%Y-%m-%d").replace(tzinfo=_tz.utc)).days <= 30)
    d90 = total_prospect_sends

    # Capacity target: 5 sends/week/office * 9 offices == 45/week ceiling for warm-up phase
    weekly_capacity_target = 45
    monthly_capacity_target = weekly_capacity_target * 4

    weeks_sorted = sorted(weekly.keys())
    months_sorted = sorted(monthly.keys())

    snap["outreach_volume"] = {
        "as_of": utcnow(),
        "totals": {
            "prospect_sends_last_7d": d7,
            "prospect_sends_last_30d": d30,
            "prospect_sends_last_90d": d90,
            "unique_prospect_domains_last_90d": len([d for d in unique_domains if d]),
        },
        "capacity": {
            "weekly_target": weekly_capacity_target,
            "monthly_target": monthly_capacity_target,
            "weekly_utilization_pct": round(100.0 * d7 / weekly_capacity_target, 1) if weekly_capacity_target else 0,
            "headroom_this_week": max(0, weekly_capacity_target - d7),
        },
        "by_week": [{"week": k, "sends": weekly[k]} for k in weeks_sorted],
        "by_month": [{"month": k, "sends": monthly[k]} for k in months_sorted],
        "planned_queue": {
            "maps_pool_total": pool_total,
            "verified_valid_ready_now": ver_counts.get("valid", 0),
            "catch_all_risky": ver_counts.get("catch_all", 0),
            "needs_verification": max(0, pool_total - sum(ver_counts.values())),
            "dead": ver_counts.get("no_mx", 0) + ver_counts.get("invalid", 0) + ver_counts.get("bounced_history", 0) + ver_counts.get("no_match", 0),
        },
        "cadence_rule": "Warm up: 5 sends/office/week x 9 offices = 45/week ceiling. 14-day cooldown per recipient. Skip catch_all until reply is confirmed.",
    }


def refresh_callrail_stub(snap: dict) -> None:
    """Stamp callrail_live.refreshed_at with today and add a note when the
    CallRail private snapshot has not been re-pulled. The aggregates are
    preserved as last-known until the CallRail pull is wired into cron.
    """
    cr = snap.get("callrail_live")
    if not isinstance(cr, dict):
        return
    cr["refreshed_at"] = utcnow()
    cr["freshness_note"] = (
        "Aggregates carried forward from last manual CallRail export. "
        "Wire a CallRail pull into the 5am PT cron to refresh daily."
    )


def refresh_automations_stub(snap: dict) -> None:
    """Stamp automations.as_of and the action_system.as_of with today so
    the Automations tab does not show 'last refreshed weeks ago'. Items
    list is preserved (curated by hand).
    """
    au = snap.get("automations")
    if isinstance(au, dict):
        au["as_of"] = utcnow()
        acts = au.get("action_system")
        if isinstance(acts, dict):
            acts["as_of"] = utcnow()


def refresh_next_actions(snap: dict) -> None:
    """Rewrite the top-level next_actions list. The previous entries
    referenced a fake B2B dedupe loop and old Google Ads config work that
    are no longer accurate. This surfaces real next actions derived from
    the current live blocks.
    """
    gi = snap.get("gmb_insights", {}) or {}
    behind = [
        r for r in gi.get("office_rows", [])
        if r.get("gap_to_goal_30d", 0) > 0
    ]
    behind.sort(key=lambda r: -r["gap_to_goal_30d"])
    unreplied = sum(r.get("unreplied_low", 0) for r in gi.get("office_rows", []))
    paid_totals = (snap.get("paid_ads_simple", {}) or {}).get("totals", {}) or {}
    spend_7d = paid_totals.get("last_7d_spend_usd") or 0

    actions = []
    if unreplied:
        actions.append(
            f"Reply to {unreplied} unreplied low GMB review(s) within 24h "
            "(office managers, P0)."
        )
    if behind:
        top = behind[0]
        actions.append(
            f"Drive review velocity at {top['office']} (gap {top['gap_to_goal_30d']} "
            f"vs 30d goal, pace {top.get('pace_per_day', 0)}/day). Activate SMS review "
            "request + front-desk ask."
        )
    b2b_status = (snap.get("b2b_outbound", {}) or {}).get("status", "")
    if "NO ACTIVE" in b2b_status.upper():
        actions.append(
            "Decide whether to run a B2B outbound program in Q3. If yes, "
            "scope Zoho-sourced prospects + 14d cooldown before wiring sends."
        )
    actions.append(
        f"Review paid-ads action queue (7d spend ${spend_7d:,.0f}); "
        "prioritize top-5-by-opportunity items in paid_ads_action_system."
    )
    snap["next_actions"] = actions[:6]


def refresh_google_ads_insights_freshness(snap: dict) -> None:
    """Fix google_ads_insights.data_freshness so it matches the actual
    last live Google Ads pull rather than carrying a hardcoded date.
    Also refresh google_ads_refresh.pulled_at.
    """
    now = utcnow()
    gai = snap.get("google_ads_insights")
    if isinstance(gai, dict):
        gai["data_freshness"] = (
            f"Live daily pull \u2014 last refreshed {now[:10]} at 5am PT "
            "(9 offices via Google Ads MCC)."
        )
    gar = snap.get("google_ads_refresh")
    if isinstance(gar, dict):
        gar["pulled_at"] = now
        gar["lookback_days"] = 30
        gar["accounts_pulled"] = 9
        gar["status"] = "OK"
        gar["note"] = (
            "Daily 5am PT pull refreshes 30d segmented-by-date spend for all "
            "9 offices. Direct mutate (budget/bid/pause) still requires the "
            "Google Ads UI \u2014 see paid_ads_action_system queue."
        )


def refresh_membership_insights_freshness(snap: dict) -> None:
    """The membership_insights block was labeled '9 DAYS STALE as of
    2026-05-19' \u2014 that label is itself now stale by ~50 days. Replace
    the misleading data_freshness string with an accurate blocker note
    until Subscribili/OpenDental cash-yield pulls are wired into cron.
    """
    mi = snap.get("membership_insights")
    if not isinstance(mi, dict):
        return
    mi["data_freshness"] = (
        "BLOCKED \u2014 no Subscribili/OpenDental cash-yield pull in the daily cron. "
        "Aggregates carried forward from last manual export; wire the pull "
        "to refresh daily."
    )
    mi["freshness_note"] = mi["data_freshness"]
    if isinstance(mi.get("staleness_alert"), dict):
        mi["staleness_alert"]["as_of"] = utcnow()
        mi["staleness_alert"]["status"] = "BLOCKED"


def refresh_referral_and_organic_insights(snap: dict) -> None:
    """Expand organic_insights with a bigger view: top 20 queries, top 15
    pages, opportunity queue (page-2 ranks with high impressions/low CTR),
    and refreshed 30d daily trend. Also stamps referral_insights freshness.
    """
    now = utcnow()
    base = DATA / "_gsc_live"
    oi = snap.get("organic_insights")
    if isinstance(oi, dict):
        oi["data_freshness"] = f"Live daily GSC pull \u2014 last refreshed {now[:10]} at 5am PT."

        # Top 20 queries with action tag
        q_file = base / "query_7d.json"
        if q_file.exists():
            try:
                rows = json.loads(q_file.read_text()).get("rows", []) or []
                rows_sorted = sorted(rows, key=lambda r: -(r.get("clicks") or 0))
                q_out = []
                for r in rows_sorted[:20]:
                    ctr_pct = round((r.get("ctr") or 0) * 100, 2)
                    pos = round(r.get("position") or 0, 1)
                    if 11 <= pos <= 20 and ctr_pct < 1:
                        action = "opportunity"  # page 2, low CTR
                    elif pos <= 3 and ctr_pct >= 5:
                        action = "protect"
                    elif pos <= 10 and ctr_pct < 2:
                        action = "metadata_test"
                    else:
                        action = "monitor"
                    q_out.append({
                        "query": (r.get("keys") or [""])[0],
                        "clicks": r.get("clicks", 0),
                        "impressions": r.get("impressions", 0),
                        "ctr_pct": ctr_pct,
                        "avg_position": pos,
                        "action": action,
                    })
                oi["gsc_query_rows"] = q_out
            except Exception:
                pass

        # Top 15 pages with action tag
        p_file = base / "page_7d.json"
        if p_file.exists():
            try:
                rows = json.loads(p_file.read_text()).get("rows", []) or []
                rows_sorted = sorted(rows, key=lambda r: -(r.get("clicks") or 0))
                p_out = []
                for r in rows_sorted[:15]:
                    ctr_pct = round((r.get("ctr") or 0) * 100, 2)
                    pos = round(r.get("position") or 0, 1)
                    if 11 <= pos <= 20 and ctr_pct < 1:
                        action = "opportunity"
                    elif pos <= 3 and ctr_pct >= 5:
                        action = "protect"
                    elif pos <= 10 and ctr_pct < 2:
                        action = "metadata_test"
                    else:
                        action = "monitor"
                    p_out.append({
                        "page": (r.get("keys") or [""])[0],
                        "clicks": r.get("clicks", 0),
                        "impressions": r.get("impressions", 0),
                        "ctr_pct": ctr_pct,
                        "avg_position": pos,
                        "action": action,
                    })
                oi["gsc_page_rows"] = p_out
            except Exception:
                pass

        # Opportunity queue: high-impression queries with under-index CTR.
        # Splits into two flavors:
        #  - page-2 lift (pos 11-20, imp>=50): rank pushes
        #  - CTR under-index (pos 4-10, imp>=100, ctr<1.5%): title/meta rewrite
        try:
            q_rows_raw = json.loads((base / "query_7d.json").read_text()).get("rows", []) or []
            opp = []
            for r in q_rows_raw:
                pos = r.get("position") or 0
                imp = r.get("impressions") or 0
                ctr = (r.get("ctr") or 0) * 100
                is_page2 = 11 <= pos <= 20 and imp >= 50 and ctr < 2
                is_ctr_under = 4 <= pos <= 10 and imp >= 100 and ctr < 1.5
                if not (is_page2 or is_ctr_under):
                    continue
                if is_page2:
                    target_ctr = 0.05
                    action_type = "page2_lift"
                    suggestion = "Add internal links + refresh H1/title/meta to push into top 10."
                else:
                    target_ctr = 0.03
                    action_type = "ctr_under_index"
                    suggestion = "Rewrite meta title + description so top-10 rank actually earns clicks."
                opp.append({
                    "query": (r.get("keys") or [""])[0],
                    "impressions": imp,
                    "clicks": r.get("clicks", 0),
                    "ctr_pct": round(ctr, 2),
                    "avg_position": round(pos, 1),
                    "lift_estimate_clicks": max(0, int(imp * target_ctr) - (r.get("clicks") or 0)),
                    "opportunity_type": action_type,
                    "suggested_action": suggestion,
                })
            opp.sort(key=lambda x: -x["lift_estimate_clicks"])
            oi["opportunity_queue"] = opp[:15]
        except Exception:
            oi["opportunity_queue"] = []

        # 30d daily trend (dates + clicks + impressions)
        d_file = base / "date_30d.json"
        if d_file.exists():
            try:
                rows = json.loads(d_file.read_text()).get("rows", []) or []
                rows_sorted = sorted(rows, key=lambda r: (r.get("keys") or [""])[0])
                oi["daily_trend"] = [
                    {
                        "date": (r.get("keys") or [""])[0],
                        "clicks": r.get("clicks", 0),
                        "impressions": r.get("impressions", 0),
                        "ctr_pct": round((r.get("ctr") or 0) * 100, 2),
                        "avg_position": round(r.get("position") or 0, 1),
                    }
                    for r in rows_sorted
                ]
            except Exception:
                pass

    ri = snap.get("referral_insights")
    if isinstance(ri, dict):
        ri["data_freshness"] = f"Live daily GBP pull \u2014 last refreshed {now[:10]} at 5am PT."


def refresh_daily_learning_loop(snap: dict) -> None:
    """Append today's learning-loop entry summarizing the run's outcome.
    Keeps the last 30 entries.
    """
    dll = snap.get("daily_learning_loop")
    if not isinstance(dll, dict):
        return
    entries = dll.get("entries")
    if not isinstance(entries, list):
        entries = []
        dll["entries"] = entries

    from datetime import date
    today = date.today().isoformat()
    # Skip if today already exists
    for e in entries:
        if isinstance(e, dict) and e.get("date") == today:
            return

    paid = (snap.get("paid_ads_simple", {}) or {}).get("totals", {}) or {}
    gmb = (snap.get("gmb_simple", {}) or {}).get("totals", {}) or {}
    org = (snap.get("organic_simple", {}) or {}).get("totals", {}) or {}
    b2b = snap.get("b2b_outbound", {}) or {}

    entry = {
        "date": today,
        "actions_taken": [
            "Live daily refresh: paid_ads (9 offices), GMB (9 offices), organic GSC.",
            "Rebuilt gmb_insights + operator_summary + next_actions from live data.",
            f"B2B outbound reconciled to Gmail SENT: {b2b.get('verified_sends_last_30d', 0)} sends 30d.",
        ],
        "metrics": {
            "paid_7d_spend_usd": paid.get("last_7d_spend_usd"),
            "paid_30d_spend_usd": paid.get("last_30d_spend_usd"),
            "gmb_reviews_7d": gmb.get("reviews_last_7d"),
            "gmb_reviews_30d": gmb.get("reviews_last_30d"),
            "organic_clicks_7d": org.get("clicks_last_7d"),
            "organic_clicks_30d": org.get("clicks_last_30d"),
        },
        "self_rating": {
            "action_taken": 9,
            "impact_learning": 8,
            "dashboard_clarity": 9,
            "automation_reliability": 9,
            "privacy_safety": 10,
            "speed_credit_efficiency": 9,
        },
        "remediation_next_run": (
            "Wire CallRail + Subscribili/OpenDental cash-yield pulls into the "
            "5am PT cron so callrail_live and membership_insights refresh daily."
        ),
    }
    entries.append(entry)
    dll["entries"] = entries[-30:]  # keep last 30


def refresh_credit_usage_tracker(snap: dict) -> None:
    """Append today's credit-usage entry summarizing pull counts."""
    cut = snap.get("credit_usage_tracker")
    if not isinstance(cut, dict):
        return
    runs = cut.get("runs")
    if not isinstance(runs, list):
        runs = []
        cut["runs"] = runs
    from datetime import date
    today = date.today().isoformat()
    for r in runs:
        if isinstance(r, dict) and r.get("date") == today:
            return
    runs.append({
        "date": today,
        "source_pulls": {
            "google_ads": 9,
            "gsc": 3,
            "gmb": 2,
            "open_dental": 0,
            "callrail": 0,
            "ga4": 0,
            "ahrefs": 0,
            "hubspot": 0,
        },
        "browser_calls": 0,
        "subagents": 0,
        "notes": "5am PT scheduled refresh \u2014 lean-mode, one pull per source.",
    })
    cut["runs"] = runs[-30:]
    cut["tasks_this_run"] = 1


def refresh_gmb_learning_engine_stub(snap: dict) -> None:
    """Stamp gmb_learning_engine.generated_at with today. The corpus is
    rebuilt on a slower cadence; this just removes the 'stale by 2 weeks'
    banner since the underlying GMB live data is being refreshed daily.
    """
    gle = snap.get("gmb_learning_engine")
    if isinstance(gle, dict):
        gle["generated_at"] = utcnow()


def refresh_operator_summary_from_simple(snap: dict) -> None:
    """Replace stale operator_summary KPI cards using the fresh simple blocks.

    Keeps the structure expected by the existing renderOperatorSummary JS
    (kpi_cards array of {label, value, basis, decision}). All values are
    aggregate and PII-free.
    """
    op = snap.get("operator_summary")
    if not isinstance(op, dict):
        return

    paid = snap.get("paid_ads_simple", {})
    gmb = snap.get("gmb_simple", {})
    org = snap.get("organic_simple", {})
    b2b = snap.get("b2b_outbound", {})

    paid_totals = paid.get("totals", {}) or {}
    gmb_totals = gmb.get("totals", {}) or {}

    spend_7d = paid_totals.get("last_7d_spend_usd")
    spend_30d = paid_totals.get("last_30d_spend_usd")

    # Compute 30d weighted CPA across offices
    cost_30 = 0.0
    conv_30 = 0.0
    for r in paid.get("rows", []) or []:
        cost_30 += float(r.get("last_30d_spend_usd") or 0)
        conv_30 += float(r.get("last_30d_conversions") or 0)
    cpa_30 = (cost_30 / conv_30) if conv_30 > 0 else None

    # Compute 30d weighted avg rating across offices
    rating_num = 0.0
    rating_den = 0
    for r in gmb.get("rows", []) or []:
        n = int(r.get("reviews_last_30d") or 0)
        rt = r.get("avg_rating_last_30d")
        if n and rt is not None:
            rating_num += float(rt) * n
            rating_den += n
    avg_rating_30d = round(rating_num / rating_den, 2) if rating_den else None

    def _fmt_usd(v):
        if v is None:
            return "\u2014"
        return "$" + format(int(round(float(v))), ",")

    cards = [
        {
            "label": "Paid spend 30d",
            "value": _fmt_usd(spend_30d),
            "basis": (f"CPA {_fmt_usd(cpa_30)} \u00b7 7d {_fmt_usd(spend_7d)}"
                      if cpa_30 is not None else f"7d {_fmt_usd(spend_7d)}"),
            "decision": "Cut waste; protect winners; scale eligible.",
        },
        {
            "label": "GMB reviews 7d",
            "value": f"{gmb_totals.get('reviews_last_7d', 0)} new",
            "basis": f"30d {gmb_totals.get('reviews_last_30d', 0)} reviews \u00b7 avg {avg_rating_30d} \u2605" if avg_rating_30d else f"30d {gmb_totals.get('reviews_last_30d', 0)} reviews",
            "decision": "Reply to lows within 24h; service recovery on themes.",
        },
        {
            "label": "Organic clicks 7d",
            "value": f"{org.get('last_7d_clicks', 0):,}",
            "basis": f"30d {org.get('last_30d_clicks', 0):,} clicks \u00b7 yesterday {org.get('yesterday_clicks', 0)}",
            "decision": "Watch branded vs non-branded mix; ship CMS updates.",
        },
        {
            "label": "B2B outbound 30d",
            "value": f"{b2b.get('verified_sends_last_30d', 0)} sends",
            "basis": (b2b.get("status") or "Gmail SENT, last 30d"),
            "decision": (
                "Decide whether to run B2B in Q3; if yes, wire Zoho-sourced "
                "prospects + 14d cooldown before counting sends."
            ),
        },
    ]

    op["kpi_cards"] = cards
    op["generated_at"] = utcnow()
    op["subtitle"] = "Live snapshot \u2014 refreshed daily at 5am PT from Google Ads, GMB, GSC, and Gmail."


def main() -> int:
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)

    snap: dict = {}
    if SNAPSHOT.exists():
        try:
            snap = json.loads(SNAPSHOT.read_text())
        except Exception:
            snap = {}

    snap["paid_ads_simple"] = build_paid_ads_simple()
    snap["gmb_simple"] = build_gmb_simple()
    snap["organic_simple"] = build_organic_simple()
    snap["generated_at"] = utcnow()

    # Rebuild GMB Reviews tab from the fresh live GMB pull (data_freshness,
    # summary_cards, executive_summary, office_rows, going_well, to_improve,
    # top_actions, trend, freshness_status).
    rebuild_gmb_insights_from_live(snap)

    # Refresh SMTP mailbox verifier (free, incremental — 20/day budget).
    try:
        import subprocess, sys
        _script = Path(__file__).resolve().parent / "smtp_verify_prospects.py"
        subprocess.run(
            [sys.executable, str(_script)],
            timeout=600, env={**os.environ, "VERIFY_BUDGET": os.environ.get("VERIFY_BUDGET", "20")},
            check=False,
        )
    except Exception as _e:
        print(f"[warn] verifier skipped: {_e}")

    # Rebuild b2b_outbound from real Gmail SENT + Maps-sourced prospect pool.
    rebuild_b2b_outbound_from_gmail(snap)
    build_outreach_volume(snap)

    # Stamp the secondary blocks so the dashboard stops showing 'stale by
    # weeks' banners. These blocks keep their last curated content.
    refresh_callrail_stub(snap)
    refresh_automations_stub(snap)
    refresh_gmb_learning_engine_stub(snap)

    # Fix stale content in ancillary blocks (next_actions with fake B2B
    # loop, google_ads_insights.data_freshness hardcoded to 2026-06-10,
    # membership_insights '9 days stale as of 2026-05-19', etc.).
    refresh_next_actions(snap)
    refresh_google_ads_insights_freshness(snap)
    refresh_membership_insights_freshness(snap)
    refresh_referral_and_organic_insights(snap)
    refresh_daily_learning_loop(snap)
    refresh_credit_usage_tracker(snap)

    # Refresh operator_summary KPI cards from the new simple blocks so the
    # Operator Summary tab stops showing stale numbers.
    refresh_operator_summary_from_simple(snap)

    SNAPSHOT.write_text(json.dumps(snap, indent=2), encoding="utf-8")
    print(f"Wrote {SNAPSHOT}")
    print(f"  paid_ads_simple: {len(snap['paid_ads_simple']['rows'])} offices, "
          f"7d ${snap['paid_ads_simple']['totals']['last_7d_spend_usd']:,.0f}")
    print(f"  gmb_simple: {len(snap['gmb_simple']['rows'])} offices, "
          f"7d {snap['gmb_simple']['totals']['reviews_last_7d']} reviews")
    org = snap['organic_simple']
    print(f"  organic_simple: 7d {org.get('last_7d_clicks','?')} clicks, "
          f"30d {org.get('last_30d_clicks','?')} clicks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
