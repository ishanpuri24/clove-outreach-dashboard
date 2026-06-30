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
            "basis": f"{b2b.get('unique_prospects_last_30d', 0)} unique prospects (Gmail SENT)",
            "decision": "Stop dedupe-broken loop; rebuild on Zoho with safety guards.",
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

    # Refresh operator_summary KPI cards from the new simple blocks so the
    # Operator Summary tab stops showing stale June-11 numbers.
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
