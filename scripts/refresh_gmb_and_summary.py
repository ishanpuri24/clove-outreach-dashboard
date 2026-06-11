#!/usr/bin/env python3
"""Refresh data/snapshot.json from the freshly pulled GMB reviews payload.

Reads:  data/gmb_raw_reviews.json  (locationReviews list from connector)
        data/gmb_location_map.json (office -> 'locations/<id>')
        data/snapshot.json
Writes: data/snapshot.json (in-place)

Updates:
  - gmb_insights.data_freshness, freshness_status (recomputed against today)
  - gmb_insights.summary_cards (overall rating, velocity, vs prior, recovery load)
  - gmb_insights.office_rows (per-office: rating/30d, low-30d, unreplied, themes)
  - gmb_insights.low_review_weekly_trends.totals (last_7d, prior_7d, delta)
  - gmb_insights.low_review_weekly_trends.weekly_buckets (last 4 full weeks)
  - operator_summary.generated_at -> today (UTC, ISO)

Privacy: no reviewer names, review IDs, profile URLs, location IDs in output.
Snippet sanitization: truncate to 160 chars, no PII heuristics.
"""
import json
import re
from collections import defaultdict, Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path("/home/user/workspace/clove-outreach-dashboard")
SNAP = ROOT / "data" / "snapshot.json"
RAW = ROOT / "data" / "gmb_raw_reviews.json"
MAP = ROOT / "data" / "gmb_location_map.json"

STAR_TO_INT = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}
LOW_THRESHOLD = 3  # 1-3 stars = "low"

# Theme keyword buckets — applied to comment text (case-insensitive)
THEME_KEYWORDS = {
    "Wait / scheduling": [
        "wait", "late", "delay", "schedule", "appointment", "reschedul",
        "no show", "no-show",
    ],
    "Clinical experience": [
        "pain", "hurt", "rough", "rushed", "extraction", "root canal",
        "filling", "crown", "anesth", "numb",
    ],
    "Insurance / billing": [
        "insurance", "bill", "charge", "cost", "price", "denial",
        "covered", "deductible", "estimate", "claim",
    ],
    "Communication": [
        "communic", "explain", "told me", "didn't tell", "did not tell",
        "follow up", "follow-up", "callback", "respond",
    ],
    "Staff professionalism": [
        "rude", "unprofessional", "attitude", "disrespect", "argument",
        "yelled",
    ],
    "Friendly staff": [
        "friendly", "kind", "welcoming", "warm", "nice", "wonderful",
        "amazing staff", "great staff",
    ],
    "Doctor quality": [
        "dr.", "doctor", "dentist", "hygienist", "skilled", "professional",
        "knowledg",
    ],
    "Comfort / anxiety": [
        "comfortable", "anxiety", "anxious", "scared", "afraid", "gentle",
        "painless", "pain-free", "pain free",
    ],
    "Clean office": [
        "clean", "modern", "beautiful", "sanitary", "tidy",
    ],
    "Clear explanation": [
        "explain", "thorough", "informed", "honest", "transparent",
    ],
}

POSITIVE_THEMES = {
    "Friendly staff", "Doctor quality", "Comfort / anxiety",
    "Clean office", "Clear explanation",
}
NEGATIVE_THEMES = {
    "Wait / scheduling", "Clinical experience", "Insurance / billing",
    "Communication", "Staff professionalism",
}


def sanitize(text: str, max_len: int = 160) -> str:
    if not text:
        return ""
    t = re.sub(r"\s+", " ", text).strip()
    if len(t) > max_len:
        t = t[: max_len - 1].rstrip() + "…"
    return t


def themes_for(text: str) -> list[str]:
    if not text:
        return []
    low = text.lower()
    hits = []
    for theme, kws in THEME_KEYWORDS.items():
        if any(k in low for k in kws):
            hits.append(theme)
    return hits


def main():
    snap = json.loads(SNAP.read_text())
    raw = json.loads(RAW.read_text())
    loc_map = json.loads(MAP.read_text())

    # invert: locations/<id> -> office name
    loc_to_office = {}
    for office, loc_path in loc_map.items():
        loc_id = loc_path.split("/")[-1]
        loc_to_office[loc_id] = office

    today = date.today()
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    last_7d_cutoff = today - timedelta(days=7)
    prior_7d_cutoff = today - timedelta(days=14)
    last_30d_cutoff = today - timedelta(days=30)
    prior_30d_cutoff = today - timedelta(days=60)

    reviews_by_office = defaultdict(list)
    skipped = 0
    for entry in raw.get("locationReviews", []):
        # entry.name = "accounts/<a>/locations/<id>"
        loc_id = entry.get("name", "").split("/locations/")[-1].split("/")[0]
        office = loc_to_office.get(loc_id)
        if not office:
            skipped += 1
            continue
        r = entry.get("review", {})
        rating = STAR_TO_INT.get(r.get("starRating"), 0)
        if rating == 0:
            continue
        ct = r.get("createTime", "")
        try:
            d = datetime.fromisoformat(ct.replace("Z", "+00:00")).date()
        except Exception:
            continue
        replied = bool(r.get("reviewReply"))
        comment = r.get("comment") or ""
        reviews_by_office[office].append({
            "date": d.isoformat(),
            "rating": rating,
            "replied": replied,
            "comment": comment,
            "themes": themes_for(comment),
        })

    # ---- Summary cards (aggregate across offices) ----
    all_30d = []
    all_prior_30d = []
    all_7d = []
    low_30d_total = 0
    unreplied_low_total = 0
    low_30d_by_office = Counter()
    unreplied_low_by_office = Counter()
    rev_30d_by_office = Counter()
    rev_7d_by_office = Counter()
    sum_rating_30d_by_office = defaultdict(float)
    cnt_rating_30d_by_office = Counter()

    # Lifetime + low review entries for the historical section
    historical_low_entries = []

    for office, lst in reviews_by_office.items():
        for r in lst:
            d = date.fromisoformat(r["date"])
            if d > today:
                continue
            if d >= last_30d_cutoff:
                all_30d.append(r)
                rev_30d_by_office[office] += 1
                sum_rating_30d_by_office[office] += r["rating"]
                cnt_rating_30d_by_office[office] += 1
                if r["rating"] <= LOW_THRESHOLD:
                    low_30d_total += 1
                    low_30d_by_office[office] += 1
                    if not r["replied"]:
                        unreplied_low_total += 1
                        unreplied_low_by_office[office] += 1
                    historical_low_entries.append({
                        "office": office,
                        "date": r["date"],
                        "rating": r["rating"],
                        "replied": r["replied"],
                        "snippet": sanitize(r["comment"]),
                        "themes": r["themes"],
                        "action": (
                            "Reply within 24h, call patient, log recovery"
                            if not r["replied"]
                            else "Replied — log recovery outcome"
                        ),
                    })
            if prior_30d_cutoff <= d < last_30d_cutoff:
                all_prior_30d.append(r)
            if d >= last_7d_cutoff:
                all_7d.append(r)
                rev_7d_by_office[office] += 1

    total_30d = len(all_30d)
    avg_rating_30d = (
        sum(r["rating"] for r in all_30d) / total_30d if total_30d else 0
    )
    velocity_per_day = total_30d / 30 if total_30d else 0
    goal_per_day = 9.0
    attainment_pct = (
        (velocity_per_day / goal_per_day * 100) if goal_per_day else 0
    )
    gap_30d = max(0, int(round(goal_per_day * 30 - total_30d)))

    summary_cards = [
        {
            "label": "Overall rating",
            "value": f"{avg_rating_30d:.2f}★",
            "subtext": f"9 offices · {total_30d} reviews in last 30d",
        },
        {
            "label": "Review velocity",
            "value": f"{velocity_per_day:.2f}/day",
            "subtext": (
                f"Goal {goal_per_day:.0f}/day; attainment "
                f"{attainment_pct:.1f}%, gap {gap_30d}/30d"
            ),
        },
        {
            "label": "30d reviews vs prior",
            "value": (
                f"{total_30d} "
                f"({'+' if total_30d - len(all_prior_30d) >= 0 else ''}"
                f"{total_30d - len(all_prior_30d)})"
            ),
            "subtext": (
                f"Prior 30d: {len(all_prior_30d)}; Last 7d: {len(all_7d)}"
            ),
        },
        {
            "label": "Service recovery load",
            "value": f"{low_30d_total} low / {unreplied_low_total} unreplied",
            "subtext": (
                "Reply discipline OK"
                if unreplied_low_total == 0
                else f"{unreplied_low_total} need reply within 24h"
            ),
        },
    ]

    # ---- Per-office rows ----
    office_rows = []
    for office in sorted(loc_map.keys()):
        cnt30 = rev_30d_by_office.get(office, 0)
        avg30 = (
            sum_rating_30d_by_office[office] / cnt_rating_30d_by_office[office]
            if cnt_rating_30d_by_office[office] else 0
        )
        # collect positive + negative themes
        pos_counter = Counter()
        neg_counter = Counter()
        for r in reviews_by_office.get(office, []):
            d = date.fromisoformat(r["date"])
            if d < last_30d_cutoff:
                continue
            for th in r["themes"]:
                if th in POSITIVE_THEMES:
                    pos_counter[th] += 1
                if th in NEGATIVE_THEMES:
                    neg_counter[th] += 1

        gap = max(0, int(round(goal_per_day * 30 - cnt30)))
        office_rows.append({
            "office": office,
            "avg_rating_30d": round(avg30, 2),
            "reviews_30d": cnt30,
            "reviews_7d": rev_7d_by_office.get(office, 0),
            "pace_per_day": round(cnt30 / 30, 2),
            "gap_to_goal_30d": gap,
            "low_30d": low_30d_by_office.get(office, 0),
            "unreplied_low": unreplied_low_by_office.get(office, 0),
            "patients_like": [t for t, _ in pos_counter.most_common(3)],
            "patients_flag": [t for t, _ in neg_counter.most_common(3)],
            "do_next": (
                f"Reply to {unreplied_low_by_office[office]} unreplied low review(s) within 24h"
                if unreplied_low_by_office.get(office, 0) > 0
                else f"Ask 2 happy patients/day to close gap of {gap}"
            ),
        })

    # ---- Weekly low-review trend (last 4 full ISO weeks) ----
    # Week start = Monday
    def monday(d: date) -> date:
        return d - timedelta(days=d.weekday())

    current_week_start = monday(today)
    weekly_buckets = []
    for i in range(4, 0, -1):
        wstart = current_week_start - timedelta(weeks=i)
        wend = wstart + timedelta(days=6)
        low_count = 0
        offices_with_low = set()
        for office, lst in reviews_by_office.items():
            for r in lst:
                d = date.fromisoformat(r["date"])
                if wstart <= d <= wend and r["rating"] <= LOW_THRESHOLD:
                    low_count += 1
                    offices_with_low.add(office)
        weekly_buckets.append({
            "week_start": wstart.isoformat(),
            "week_end": wend.isoformat(),
            "low_count": low_count,
            "total_offices_with_low": len(offices_with_low),
        })

    last_7d_low = sum(
        1
        for office, lst in reviews_by_office.items()
        for r in lst
        if date.fromisoformat(r["date"]) >= last_7d_cutoff
        and r["rating"] <= LOW_THRESHOLD
    )
    prior_7d_low = sum(
        1
        for office, lst in reviews_by_office.items()
        for r in lst
        if prior_7d_cutoff <= date.fromisoformat(r["date"]) < last_7d_cutoff
        and r["rating"] <= LOW_THRESHOLD
    )
    last_7d_low_avg = (
        sum(
            r["rating"]
            for office, lst in reviews_by_office.items()
            for r in lst
            if date.fromisoformat(r["date"]) >= last_7d_cutoff
            and r["rating"] <= LOW_THRESHOLD
        )
        / last_7d_low
        if last_7d_low
        else None
    )

    # ---- Stamp gmb_insights ----
    gmb = snap.setdefault("gmb_insights", {})
    gmb["data_freshness"] = now_iso
    gmb["lookback"] = "Last 30d"
    gmb["freshness_status"] = {
        "data_freshness": now_iso,
        "age_days": 0,
        "is_stale": False,
        "stale_threshold_days": 2,
        "label": "fresh",
        "note": "GMB data pulled today via Google Business Profile API.",
    }
    gmb["summary_cards"] = summary_cards
    gmb["office_rows"] = office_rows

    # Update weekly trend totals + buckets but preserve office_trends/action_queue structure
    wt = gmb.setdefault("low_review_weekly_trends", {})
    wt["title"] = "Weekly low-review trend"
    wt["generated_at"] = now_iso
    wt["anchor_date"] = today.isoformat()
    wt["current_week_start"] = current_week_start.isoformat()
    wt["windows"] = {"last_7d": 7, "prior_7d": 7, "last_28d": 28}
    wt["totals"] = {
        "last_7d_low": last_7d_low,
        "prior_7d_low": prior_7d_low,
        "delta_low": last_7d_low - prior_7d_low,
        "last_7d_avg_rating": (
            round(last_7d_low_avg, 2) if last_7d_low_avg is not None else None
        ),
        "unresolved_open": unreplied_low_total,
        "oldest_open_age_days": 0,
    }
    wt["weekly_buckets"] = weekly_buckets

    # Per-office trends for the weekly trend table
    office_trends = []
    for office in sorted(loc_map.keys()):
        lst = reviews_by_office.get(office, [])
        o_last_7d_low = sum(
            1 for r in lst
            if date.fromisoformat(r["date"]) >= last_7d_cutoff
            and r["rating"] <= LOW_THRESHOLD
        )
        o_prior_7d_low = sum(
            1 for r in lst
            if prior_7d_cutoff <= date.fromisoformat(r["date"]) < last_7d_cutoff
            and r["rating"] <= LOW_THRESHOLD
        )
        delta = o_last_7d_low - o_prior_7d_low
        if delta > 0:
            direction = "up"
        elif delta < 0:
            direction = "down"
        else:
            direction = "flat"
        # Per-week buckets
        o_buckets = []
        for i in range(4, 0, -1):
            wstart = current_week_start - timedelta(weeks=i)
            wend = wstart + timedelta(days=6)
            wk_low = [
                r for r in lst
                if wstart <= date.fromisoformat(r["date"]) <= wend
                and r["rating"] <= LOW_THRESHOLD
            ]
            wk_avg = (
                sum(r["rating"] for r in wk_low) / len(wk_low)
                if wk_low else None
            )
            o_buckets.append({
                "week_start": wstart.isoformat(),
                "week_end": wend.isoformat(),
                "low_count": len(wk_low),
                "avg_rating": round(wk_avg, 2) if wk_avg is not None else None,
            })
        last_28d_low = [
            r for r in lst
            if date.fromisoformat(r["date"]) >= today - timedelta(days=28)
            and r["rating"] <= LOW_THRESHOLD
        ]
        last_28d_avg = (
            round(sum(r["rating"] for r in last_28d_low) / len(last_28d_low), 2)
            if last_28d_low else None
        )
        open_fu = unreplied_low_by_office.get(office, 0)
        office_trends.append({
            "office": office,
            "last_7d_low": o_last_7d_low,
            "prior_7d_low": o_prior_7d_low,
            "delta": delta,
            "trend_direction": direction,
            "last_7d_avg": None,
            "last_28d_avg": last_28d_avg,
            "weekly_buckets": o_buckets,
            "common_themes": [],
            "response_signals": {
                "matches_28d": 0,
                "latest_signal_date": None,
                "label": (
                    "no reply signals detected"
                    if open_fu == 0
                    else f"{open_fu} open follow-up(s)"
                ),
            },
            "open_followups": open_fu,
            "oldest_open_age_days": 0,
            "next_action": (
                f"Reply to {open_fu} unreplied low review(s)"
                if open_fu > 0
                else (
                    "Huddle on recurring themes"
                    if direction == "up"
                    else "Maintain reply cadence"
                )
            ),
            "drilldown": [],
        })
    wt["office_trends"] = office_trends

    # Refresh action queue based on offices trending up
    aq = []
    for ot in office_trends:
        if ot["trend_direction"] == "up" or ot["open_followups"] > 0:
            aq.append({
                "priority": "P1" if ot["open_followups"] > 0 else "P2",
                "office": ot["office"],
                "theme": "Service recovery",
                "weeks_seen": 1,
                "trend_direction": ot["trend_direction"],
                "prior_action_effect": "new this week",
                "action": ot["next_action"],
            })
    wt["action_queue"] = aq

    # Historical low entries (sanitized)
    gmb["prior_negative_queue"] = sorted(
        historical_low_entries, key=lambda x: x["date"], reverse=True
    )[:12]

    # ---- Operator summary timestamp refresh ----
    os_ = snap.setdefault("operator_summary", {})
    os_["generated_at"] = now_iso

    # Keep snapshot.generated_at fresh as well
    snap["generated_at"] = now_iso

    SNAP.write_text(json.dumps(snap, indent=2))
    print(f"updated gmb data_freshness -> {now_iso}")
    print(f"offices: {len(office_rows)}")
    print(f"total reviews in last 30d: {total_30d}")
    print(f"low reviews 30d: {low_30d_total} / unreplied: {unreplied_low_total}")
    print(f"weekly buckets: {[b['low_count'] for b in weekly_buckets]}")
    print(f"last 7d low: {last_7d_low} (prior 7d: {prior_7d_low})")
    if skipped:
        print(f"skipped {skipped} reviews from unmapped locations")


if __name__ == "__main__":
    main()
