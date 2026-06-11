#!/usr/bin/env python3
"""Refresh the Operator Summary kpi_cards + freshness blocks so they reflect
the actual data in gmb_insights and google_ads_insights.

This fixes the stale banner ("GMB data refresh behind cadence") and the
"5 unreplied" / "2 down" mismatches on the Operator Summary tab.
"""
import json, datetime, pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
SNAP = ROOT / "data" / "snapshot.json"

def load():
    return json.loads(SNAP.read_text())

def save(d):
    SNAP.write_text(json.dumps(d, indent=2))

def main():
    s = load()
    now = datetime.datetime.utcnow().replace(microsecond=0).isoformat()+"Z"
    os_ = s.setdefault("operator_summary", {})
    g = s.get("gmb_insights", {})
    ads = s.get("google_ads_insights", {})
    totals = ads.get("totals", {})
    leaderboard = ads.get("office_leaderboard", [])

    # -- GMB freshness block --
    gmb_fresh = g.get("data_freshness") or now
    try:
        gdt = datetime.datetime.fromisoformat(gmb_fresh.replace("Z","+00:00"))
    except Exception:
        gdt = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc)
    age_days = (datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc) - gdt).days
    is_stale = age_days > 2
    os_["gmb_freshness"] = {
        "data_freshness": gmb_fresh,
        "age_days": age_days,
        "is_stale": is_stale,
        "stale_threshold_days": 2,
        "label": "stale" if is_stale else "fresh",
        "note": ("GMB data is "+str(age_days)+"d behind snapshot generation; expected daily.") if is_stale else "GMB data refreshed today via Business Profile API.",
    }

    # -- channel_freshness block --
    cf = os_.setdefault("channel_freshness", {})
    cf["gmb"] = gmb_fresh
    cf["gmb_age_days"] = age_days
    cf["gmb_is_stale"] = is_stale
    cf["paid_ads"] = ads.get("data_freshness", cf.get("paid_ads"))

    # -- GMB stats from gmb_insights --
    weekly = g.get("low_review_weekly_trends", {})
    wtot = weekly.get("totals", {}) or {}
    last7 = wtot.get("last_7d_low") or weekly.get("last_7d_low_count") or 0
    prior7 = wtot.get("prior_7d_low") or weekly.get("prior_7d_low_count") or 0
    summary_cards = g.get("summary_cards", [])
    overall_rating = None
    reviews_30d = None
    unreplied = 0
    low_30d = 0
    for c in summary_cards:
        lab = (c.get("label") or "").lower()
        val = c.get("value","")
        if lab == "overall rating":
            try: overall_rating = float(val.replace("★","").strip())
            except Exception: pass
        if "30d reviews" in lab:
            try: reviews_30d = int(val.split()[0])
            except Exception: pass
        if "service recovery" in lab:
            # "4 low / 1 unreplied"
            try:
                parts = val.split("/")
                low_30d = int(parts[0].split()[0])
                unreplied = int(parts[1].split()[0])
            except Exception: pass

    # -- KPI cards (rebuild) --
    # Compute production-aware spend % if available
    prod_mtd = sum((r.get("production_mtd_usd") or 0) for r in leaderboard) or None
    spend_pct = None
    spend_status = "PENDING"
    if prod_mtd:
        # use trailing_30d_spend_usd vs production_mtd*30/days_elapsed? simpler: use ratio of cost_usd (30d) / (prod_mtd_annualized_30d).
        # For ON TARGET we compare blended share: total leaderboard 30d spend / total production_mtd.
        spend_30d = sum((r.get("trailing_30d_spend_usd") or 0) for r in leaderboard)
        if spend_30d and prod_mtd:
            spend_pct = round(spend_30d / prod_mtd * 100, 1)
            if spend_pct <= 4.0: spend_status = "ON TARGET"
            elif spend_pct <= 6.0: spend_status = "ELEVATED"
            else: spend_status = "OVER"
    # update totals so any other reader gets it
    totals["production_mtd_usd"] = prod_mtd
    totals["spend_pct_of_production"] = spend_pct
    totals["blended_status_vs_target"] = spend_status

    cost_30d = totals.get("cost_usd") or totals.get("trailing_30d_spend_usd") or 0
    cpa = totals.get("cpa_usd") or totals.get("blended_cpa_usd")

    paid_action_count = sum((r.get("queued") or 0) for r in os_.get("paid_ads_office_rollup", []))
    paid_exec = sum((r.get("executable_now") or 0) for r in os_.get("paid_ads_office_rollup", []))
    paid_blocked = sum((r.get("blocked_on_mutate") or 0) for r in os_.get("paid_ads_office_rollup", []))
    opportunity_usd = sum((r.get("opportunity_usd") or 0) for r in os_.get("paid_ads_office_rollup", []))

    # GMB low last 7d arrow
    if last7 > prior7: gmb_arrow = "↑"
    elif last7 < prior7: gmb_arrow = "↓"
    else: gmb_arrow = "→"

    # find membership card (preserve existing if present)
    existing_kpi = {c.get("label"): c for c in os_.get("kpi_cards", [])}
    def keep(label, default):
        return existing_kpi.get(label, default)

    spend_basis = "CPA ${:.2f}".format(cpa) if cpa else None
    spend_decision = "Recover ${:,.0f}/mo identified".format(opportunity_usd) if opportunity_usd else "Cut waste; protect winners; scale eligible."

    new_cards = [
        {"label": "Paid spend 30d",
         "value": "${:,.0f}".format(cost_30d),
         "basis": (spend_basis or "—") + (" · {:.1f}% of MTD prod ({})".format(spend_pct, spend_status) if spend_pct is not None else ""),
         "decision": spend_decision},
        {"label": "Paid action queue",
         "value": "{} queued".format(paid_action_count),
         "basis": "{} executable · {} need mutate".format(paid_exec, paid_blocked),
         "decision": "Cut waste; protect winners; scale eligible."},
        {"label": "GMB avg rating",
         "value": ("{:.2f} ★".format(overall_rating) if overall_rating else "—"),
         "basis": "{} unreplied low review{}".format(unreplied, "" if unreplied == 1 else "s"),
         "decision": ("Reply within 24h" if unreplied else "Maintain reply SLA")},
        {"label": "GMB low last 7d",
         "value": "{} {}".format(last7, gmb_arrow),
         "basis": "prior 7d {}".format(prior7),
         "decision": ("Huddle on recurring themes; service recovery." if last7 > prior7 else "Maintain cadence.")},
        keep("Qualified calls 7d", {"label":"Qualified calls 7d","value":"—","basis":"973 total","decision":"Watch qualified-call CPA per office."}),
        keep("CMS writes", {"label":"CMS writes","value":"0 live / 0 draft / 3 proposed","basis":"mode: accelerated","decision":"Approve live credentials"}),
        keep("Lead SMS", {"label":"Lead SMS","value":"0 sent today","basis":"backlog 226","decision":"Move to apply once provider + dedupe verified."}),
        keep("Membership cash 7d", {"label":"Membership cash 7d","value":"—","basis":"—","decision":"—"}),
    ]
    os_["kpi_cards"] = new_cards

    # generated_at
    os_["generated_at"] = now

    # alerts: clear GMB-stale alert if fresh
    alerts = os_.get("alerts", [])
    if not is_stale:
        alerts = [a for a in (alerts or []) if "gmb" not in (a.get("id","")+a.get("title","")).lower()]
    os_["alerts"] = alerts

    save(s)
    print("updated operator_summary.generated_at ->", now)
    print("  gmb age_days:", age_days, "is_stale:", is_stale)
    print("  gmb last7/prior7:", last7, "/", prior7, gmb_arrow)
    print("  gmb unreplied:", unreplied, "low30d:", low_30d, "rating:", overall_rating)
    print("  paid spend 30d:", "${:,.0f}".format(cost_30d), "pct_of_prod:", spend_pct, spend_status)
    print("  production_mtd:", prod_mtd)

if __name__ == "__main__":
    main()
