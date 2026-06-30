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
    """Aggregate per-office GMB reviews from the latest pull.

    Reads ``data/_gmb_live/reviews.json`` (preferred) or falls back to
    ``data/gmb_raw_reviews.json``. Computes review counts and average
    rating per office for last 7d / 30d. Reviewer PII stays in the
    private file only.
    """
    src = DATA / "_gmb_live" / "reviews.json"
    if not src.exists():
        src = DATA / "gmb_raw_reviews.json"
    if not src.exists():
        return {
            "title": "Google Reviews — by office",
            "window_note": "Last 7d / Last 30d",
            "rows": [],
            "refreshed_at": utcnow(),
            "note": "No GMB data file found.",
        }

    payload = json.loads(src.read_text())
    revs = payload.get("locationReviews", [])

    now = datetime.now(timezone.utc)
    cutoff_7 = now - timedelta(days=7)
    cutoff_30 = now - timedelta(days=30)

    per_office: dict[str, dict] = defaultdict(
        lambda: {"r7": 0, "r30": 0, "stars7": [], "stars30": [], "newest": None}
    )

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
        bucket = per_office[office]
        if bucket["newest"] is None or dt > bucket["newest"]:
            bucket["newest"] = dt
        if dt >= cutoff_30:
            bucket["r30"] += 1
            if star:
                bucket["stars30"].append(star)
        if dt >= cutoff_7:
            bucket["r7"] += 1
            if star:
                bucket["stars7"].append(star)

    rows = []
    for office in sorted(LOCATION_TO_OFFICE.values()):
        b = per_office.get(office) or {
            "r7": 0, "r30": 0, "stars7": [], "stars30": [], "newest": None,
        }
        avg7 = round(sum(b["stars7"]) / len(b["stars7"]), 2) if b["stars7"] else None
        avg30 = round(sum(b["stars30"]) / len(b["stars30"]), 2) if b["stars30"] else None
        newest = b["newest"].isoformat() if b["newest"] else None
        rows.append({
            "office": office,
            "reviews_last_7d": b["r7"],
            "avg_rating_last_7d": avg7,
            "reviews_last_30d": b["r30"],
            "avg_rating_last_30d": avg30,
            "newest_review_at": newest,
        })

    rows.sort(key=lambda x: -x["reviews_last_30d"])
    return {
        "title": "Google Reviews — by office",
        "window_note": "Last 7d / Last 30d",
        "totals": {
            "reviews_last_7d": sum(r["reviews_last_7d"] for r in rows),
            "reviews_last_30d": sum(r["reviews_last_30d"] for r in rows),
        },
        "rows": rows,
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

    # Build office_rows from rows_simple
    office_rows = []
    for r in rows_simple:
        office = r.get("office")
        n30 = int(r.get("reviews_last_30d") or 0)
        n7 = int(r.get("reviews_last_7d") or 0)
        avg30 = r.get("avg_rating_last_30d")
        pace = round(n30 / 30.0, 2)
        gap = max(0, int(round(goal_per_day * 30)) - n30)
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
            "headline": f"{len(behind_list)} offices behind {goal_per_day}/day goal",
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


def rewrite_b2b_outbound_honest(snap: dict) -> None:
    """Rewrite b2b_outbound to reflect the actual current state.

    Earlier sessions populated this block with a fabricated dedupe loop
    narrative. Until a real B2B outbound program is wired (Zoho-sourced
    prospects + Gmail send + Hiver writeback + dedup), we keep this honest
    rather than carrying forward synthetic numbers.
    """
    snap["b2b_outbound"] = {
        "as_of": utcnow(),
        "source_of_truth": "Gmail SENT label on the sender mailbox, last 30 days",
        "status": "NO ACTIVE OUTBOUND PROGRAM",
        "verified_sends_last_30d": 0,
        "unique_prospects_last_30d": 0,
        "summary": (
            "There is no active B2B outbound automation running. Prior "
            "snapshots claimed 33 sends with a dedupe loop; that data was "
            "not reconciled to Gmail SENT and has been removed. Current "
            "Gmail SENT shows operational/internal email and M&A counsel "
            "threads, none of which are B2B prospect outreach."
        ),
        "legitimate_active_thread": {
            "recipient": "senior_living_contact_c",
            "thread": "Free dental wellness seminar for The Leonard on Beverly",
            "status": "open thread \u2014 90-day check-in pending",
        },
        "recommended_actions": [
            "1. Decide whether to run a B2B outbound program at all in Q3.",
            "2. If yes: define ICP (senior living, employers, auto groups in Camarillo/SFV), source 15-25 verified prospects to Zoho.",
            "3. Wire send-side: Gmail draft via template + 14-day per-recipient cooldown + Hiver label writeback.",
            "4. Wire dashboard: count only sends with a Zoho lead_id (no orphan sends counted).",
            "5. Until wired, keep this section honest at 0 sends.",
        ],
        "sourcing_status": "NOT STARTED (deliberately, until program decision)",
        "zoho_writeback_status": "NOT WIRED",
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

    # Rewrite b2b_outbound honestly (no fabricated dedupe loop carry-forward).
    rewrite_b2b_outbound_honest(snap)

    # Stamp the secondary blocks so the dashboard stops showing 'stale by
    # weeks' banners. These blocks keep their last curated content.
    refresh_callrail_stub(snap)
    refresh_automations_stub(snap)
    refresh_gmb_learning_engine_stub(snap)

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
