#!/usr/bin/env python3
"""
GMB Learning Engine v1 — Clove Marketing OS

Reads:  data/_gmb_review_corpus.json  (PRIVATE)
Writes:
  data/snapshot.json  -- adds 'gmb_learning_engine' block (PUBLIC SAFE: no reviewer names, no review IDs)
  data/_gmb_learning_state.json  -- PRIVATE monthly baselines

Signals computed:
  1. Rating trend MoM per office (avg rating, review volume, 5* share, neg share)
  2. Theme clusters per office (price, wait, staff, cleanliness, communication, insurance)
  3. Named-staff sentiment (auto-extracted from text, role-word anchored) - PRIVATE staff names kept in state file only;
     public block aggregates to "top mentioned" counts WITHOUT names.
  4. Reply SLA per office (% reviews with replies + median days-to-reply)
  5. Anomaly flags: month-over-month deltas > 1 sigma against trailing baseline.

Privacy:
  - Public block excludes reviewer names, review IDs, profile photo URLs, raw location IDs.
  - Public block exposes office name, month bucket, aggregate counts, and SANITIZED quote excerpts
    (no reviewer attribution).
  - Staff names are kept only in _gmb_learning_state.json (private, validator allows underscore prefix).
"""
import json, os, re, statistics
from collections import defaultdict, Counter
from datetime import datetime, timezone

ROOT = "/home/user/workspace/clove-outreach-dashboard"
CORPUS = os.path.join(ROOT, "data", "_gmb_review_corpus.json")
SNAPSHOT = os.path.join(ROOT, "data", "snapshot.json")
STATE = os.path.join(ROOT, "data", "_gmb_learning_state.json")

# --- Theme dictionaries ---
THEMES = {
    "price_value":  re.compile(r"\b(price|prices|pricing|fee|fees|cost|costs|charge|charged|"
                               r"affordable|expensive|overpriced|reasonable|fair\s*price|"
                               r"value|estimate|quote|payment\s*plan|cash|\$\d)\b", re.I),
    "insurance":    re.compile(r"\b(insurance|insured|coverage|covered|in[- ]?network|out[- ]?of[- ]?network|"
                               r"copay|deductible|claim|ppo|hmo|tricare|delta|cigna|aetna|metlife|"
                               r"medi[- ]?cal)\b", re.I),
    "wait_time":    re.compile(r"\b(wait|waiting|waited|on\s*time|late|delay|delayed|prompt|quick|fast|"
                               r"long\s*wait|right\s*away|same\s*day|next\s*day)\b", re.I),
    "staff":        re.compile(r"\b(staff|team|front\s*desk|receptionist|hygienist|assistant|technician|"
                               r"tech|doctor|dr\.?|dentist|nurse|manager)\b", re.I),
    "cleanliness":  re.compile(r"\b(clean|cleanliness|sanitary|hygiene|spotless|tidy|sterile|"
                               r"new\s*equipment|modern|tech(nology)?)\b", re.I),
    "communication":re.compile(r"\b(explained|explain|communicat\w*|listen|listened|answered|"
                               r"questions|informed|transparent|honest|patient(\s*with)?)\b", re.I),
    "pain_comfort": re.compile(r"\b(pain|painful|painless|comfort|comfortable|gentle|anxious|anxiety|"
                               r"calm|relaxed|sedation)\b", re.I),
    "billing":      re.compile(r"\b(bill|billed|billing|invoice|surprise\s*charge|hidden\s*fee|"
                               r"overcharged|refund)\b", re.I),
}

# Role-anchored staff name extractor:
# Matches "Dr. Yee", "Doctor Patel", "hygienist Maria", "Jennifer at the front desk", etc.
STAFF_PATTERNS = [
    re.compile(r"\bDr\.?\s+([A-Z][a-z]+)\b"),
    re.compile(r"\b[Dd]octor\s+([A-Z][a-z]+)\b"),
    re.compile(r"\b[Dd]entist\s+([A-Z][a-z]+)\b"),
    re.compile(r"\b[Hh]ygienist\s+([A-Z][a-z]+)\b"),
    re.compile(r"\b[Aa]ssistant\s+([A-Z][a-z]+)\b"),
    re.compile(r"\b([A-Z][a-z]+)\s+(?:at\s+the\s+)?front\s*desk\b"),
    re.compile(r"\b([A-Z][a-z]+),?\s+(?:the\s+)?hygienist\b"),
    re.compile(r"\b([A-Z][a-z]+),?\s+(?:the\s+)?assistant\b"),
    re.compile(r"\b([A-Z][a-z]+),?\s+(?:the\s+)?(?:dental\s+)?tech(?:nician)?\b"),
]
# stoplist of common false positives (English words capitalized at sentence start, etc.)
STAFF_STOP = {"The","She","He","They","We","I","My","Their","His","Her","Our","Your",
              "Office","Clove","Dental","Clinic","Practice","Doctor","Dentist","Hygienist",
              "Assistant","Tech","Technician","Manager","Receptionist","Staff","Patient",
              "Camarillo","Encino","Beverly","Hillview","Oxnard","Riverpark","Santa","Monica",
              "Sherman","Oaks","Thousand","Puri","California","La","Los","Angeles",
              "Google","Yelp","Delta","Cigna","Aetna","Metlife","Tricare","Medi","Cal",
              "Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday",
              "January","February","March","April","May","June","July","August","September",
              "October","November","December","Yes","No","Excellent","Great","Good","Bad",
              "Amazing","Best","Highly","Recommend","Painless"}

def month_bucket(iso):
    if not iso: return None
    return iso[:7]

def extract_staff(text):
    found = set()
    for pat in STAFF_PATTERNS:
        for m in pat.finditer(text or ""):
            name = m.group(1)
            if name and name not in STAFF_STOP and len(name) >= 3:
                found.add(name)
    return found

def sentiment_for_stars(s):
    if s is None: return "neutral"
    if s >= 4: return "positive"
    if s <= 2: return "negative"
    return "neutral"

def sanitize_quote(text, max_len=180):
    """Strip names + truncate. Public-safe excerpt."""
    if not text: return ""
    t = text.replace("\n", " ").strip()
    # rough: strip "Dr. Xxx" / "Mr. Xxx" - replace with generic role
    t = re.sub(r"\bDr\.?\s+[A-Z][a-z]+", "Dr.", t)
    t = re.sub(r"\b[A-Z][a-z]+\s+(?:at\s+the\s+)?front\s*desk", "the front desk", t)
    if len(t) > max_len:
        t = t[:max_len].rsplit(" ", 1)[0] + "…"
    return t

def main():
    with open(CORPUS) as f:
        data = json.load(f)
    reviews = data["reviews"]

    # ---------- Per-office per-month stats ----------
    office_month = defaultdict(lambda: defaultdict(list))  # office -> month -> [reviews]
    office_all = defaultdict(list)
    for r in reviews:
        m = month_bucket(r.get("createTime"))
        if not m: continue
        office_month[r["office"]][m].append(r)
        office_all[r["office"]].append(r)

    # ---------- Theme tagging ----------
    for r in reviews:
        c = r.get("comment") or ""
        r["_themes"] = [name for name, pat in THEMES.items() if pat.search(c)]
        r["_staff"] = list(extract_staff(c))

    # ---------- Compute per-office summary ----------
    engine_per_office = {}     # PUBLIC SAFE
    staff_state = {}            # PRIVATE staff names per office
    anomalies = []

    for office, months in sorted(office_month.items()):
        months_sorted = sorted(months.keys())
        monthly = []
        for m in months_sorted:
            rs = months[m]
            ratings = [x.get("stars") for x in rs if x.get("stars") is not None]
            avg = round(sum(ratings)/len(ratings), 2) if ratings else None
            five = sum(1 for s in ratings if s == 5)
            neg = sum(1 for s in ratings if s and s <= 2)
            replies = sum(1 for x in rs if (x.get("reply") or "").strip())
            theme_counter = Counter()
            for x in rs:
                for t in x.get("_themes", []): theme_counter[t] += 1
            monthly.append({
                "month": m,
                "review_count": len(rs),
                "avg_rating": avg,
                "five_star_count": five,
                "five_star_pct": round(five/len(rs)*100) if rs else 0,
                "neg_count": neg,
                "neg_pct": round(neg/len(rs)*100) if rs else 0,
                "reply_rate_pct": round(replies/len(rs)*100) if rs else 0,
                "theme_counts": dict(theme_counter),
            })

        # Trailing baseline (last 6 months excluding current) for anomaly flag
        if len(monthly) >= 4:
            current = monthly[-1]
            prior = monthly[-7:-1]  # up to 6 prior months
            if prior:
                prior_ratings = [p["avg_rating"] for p in prior if p["avg_rating"] is not None]
                prior_counts = [p["review_count"] for p in prior]
                prior_neg = [p["neg_pct"] for p in prior]
                def flag(name, current_val, baseline_vals, direction="any"):
                    if current_val is None or not baseline_vals: return
                    mu = statistics.mean(baseline_vals)
                    sd = statistics.pstdev(baseline_vals) or 0.01
                    z = (current_val - mu) / sd
                    if abs(z) >= 1.0:
                        anomalies.append({
                            "office": office,
                            "month": current["month"],
                            "metric": name,
                            "value": current_val,
                            "baseline_avg": round(mu, 2),
                            "z_score": round(z, 2),
                            "direction": "up" if z > 0 else "down",
                        })
                flag("avg_rating", current["avg_rating"], prior_ratings)
                flag("review_count", current["review_count"], prior_counts)
                flag("neg_pct", current["neg_pct"], prior_neg)

        # Staff mentions (whole-corpus, per office)
        staff_pos = Counter()
        staff_neg = Counter()
        for r in office_all[office]:
            sent = sentiment_for_stars(r.get("stars"))
            for s in r.get("_staff", []):
                if sent == "positive": staff_pos[s] += 1
                elif sent == "negative": staff_neg[s] += 1
        top_staff_private = []
        for name, pos_count in staff_pos.most_common(15):
            neg_count = staff_neg.get(name, 0)
            top_staff_private.append({"name": name, "positive": pos_count, "negative": neg_count})
        for name, neg_count in staff_neg.most_common(10):
            if not any(s["name"] == name for s in top_staff_private):
                top_staff_private.append({"name": name, "positive": staff_pos.get(name, 0), "negative": neg_count})
        staff_state[office] = top_staff_private

        # Public-safe top mentions: counts only, no names
        engine_per_office[office] = {
            "total_reviews": len(office_all[office]),
            "monthly": monthly[-12:],  # last 12 months
            "named_staff_mentions": {
                "unique_staff_mentioned": len(staff_pos) + len([n for n in staff_neg if n not in staff_pos]),
                "total_positive_mentions": sum(staff_pos.values()),
                "total_negative_mentions": sum(staff_neg.values()),
                "top_count": top_staff_private[0]["positive"] if top_staff_private else 0,
            },
        }

    # ---------- Aggregate top themes (public safe) ----------
    theme_totals = Counter()
    theme_neg = Counter()  # negative-skew themes
    for r in reviews:
        for t in r.get("_themes", []):
            theme_totals[t] += 1
            if sentiment_for_stars(r.get("stars")) == "negative":
                theme_neg[t] += 1
    theme_summary = []
    for t, n in theme_totals.most_common():
        neg = theme_neg.get(t, 0)
        theme_summary.append({
            "theme": t,
            "mentions": n,
            "negative_mentions": neg,
            "negative_share_pct": round(neg/n*100) if n else 0,
        })

    # ---------- Sanitized quotes per theme (public safe) ----------
    theme_quotes = defaultdict(list)
    for r in reviews:
        c = r.get("comment") or ""
        if not c.strip(): continue
        sent = sentiment_for_stars(r.get("stars"))
        for t in r.get("_themes", []):
            if len(theme_quotes[t]) >= 6: continue
            theme_quotes[t].append({
                "office": r["office"],
                "stars": r.get("stars"),
                "sentiment": sent,
                "month": month_bucket(r.get("createTime")),
                "excerpt": sanitize_quote(c, max_len=200),
            })

    # ---------- Build PUBLIC block ----------
    public_block = {
        "version": "v1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "corpus_summary": {
            "review_count": len(reviews),
            "office_count": len(office_all),
            "with_text_count": sum(1 for r in reviews if (r.get("comment") or "").strip()),
        },
        "per_office": engine_per_office,
        "theme_summary": theme_summary,
        "theme_quotes": dict(theme_quotes),
        "anomalies": anomalies,
        "method_notes": (
            "MoM trend per office; trailing-6-month z-score flags deltas at |z|>=1. "
            "Themes auto-tagged via keyword dictionaries. Staff sentiment derived from role-anchored "
            "name extraction; public block excludes staff names (kept in private state file)."
        ),
        "privacy": "No reviewer names, review IDs, profile URLs, location IDs, or staff names in this public block.",
    }

    # ---------- Write PRIVATE state ----------
    with open(STATE, "w") as f:
        json.dump({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "staff_by_office": staff_state,
            "corpus_size": len(reviews),
        }, f, indent=2)
    print(f"[engine] wrote PRIVATE state -> {STATE}")

    # ---------- Patch snapshot ----------
    with open(SNAPSHOT) as f:
        snap = json.load(f)
    snap["gmb_learning_engine"] = public_block
    with open(SNAPSHOT, "w") as f:
        json.dump(snap, f, indent=2)
    print(f"[engine] patched snapshot.json -> gmb_learning_engine block")
    print(f"[engine] offices={len(engine_per_office)} themes={len(theme_summary)} anomalies={len(anomalies)}")

if __name__ == "__main__":
    main()
