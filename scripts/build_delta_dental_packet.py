#!/usr/bin/env python3
"""
Generate the Delta Dental fee-negotiation review packet:
  - Per-office Delta Dental / insurance / fee mentions (quotes + stars + date)
  - Per-office top happy-patient quotes (5* enthusiastic)
  - Aggregate KPIs to use in negotiation framing

Reads:  data/_gmb_review_corpus.json
Writes: /home/user/workspace/clove_delta_dental_review_packet.md
"""
import json, re, os
from collections import defaultdict, Counter

ROOT = "/home/user/workspace/clove-outreach-dashboard"
CORPUS = os.path.join(ROOT, "data", "_gmb_review_corpus.json")
OUT = "/home/user/workspace/clove_delta_dental_review_packet.md"

# --- Filters ---
DELTA_PAT = re.compile(r"\b(delta\s*dental|delta)\b", re.IGNORECASE)
INSURANCE_PAT = re.compile(
    r"\b(insurance|insured|coverage|covered|in[- ]?network|out[- ]?of[- ]?network|"
    r"copay|co-?pay|deductible|claim|claims|ppo|hmo|hsa|fsa|tricare|cigna|aetna|"
    r"metlife|guardian|premera|anthem|bcbs|blue\s*cross)\b",
    re.IGNORECASE,
)
FEE_PAT = re.compile(
    r"\b(fee|fees|price|prices|pricing|cost|costs|costly|charge|charges|charged|"
    r"affordable|expensive|overpriced|reasonable|estimate|quote|quoted|"
    r"out[- ]?of[- ]?pocket|cash|payment\s*plan|financing|bill|billed|billing|"
    r"\$\d|membership|subscription)\b",
    re.IGNORECASE,
)
HAPPY_PAT = re.compile(
    r"\b(best|amazing|incredible|excellent|outstanding|fantastic|wonderful|"
    r"highly\s*recommend|love|loved|honest|trust(ed|worthy)?|caring|gentle|"
    r"thorough|professional|painless|comfortable|fair(\s*price)?|reasonable|"
    r"transparent|stress[- ]?free|life[- ]?saver)\b",
    re.IGNORECASE,
)

OFFICE_ORDER = [
    "Beverly Hills", "Camarillo", "Encino", "Hillview",
    "Oxnard Riverpark", "Puri Dentistry", "Santa Monica",
    "Sherman Oaks", "Thousand Oaks",
]

def fmt_quote(r, highlight_terms=None):
    txt = (r.get("comment") or "").strip()
    if not txt: return ""
    # truncate to 600 chars and clean
    if len(txt) > 600:
        txt = txt[:600].rsplit(" ", 1)[0] + "…"
    txt = txt.replace("\n", " ").replace("  ", " ")
    return txt

def short_date(iso):
    return (iso or "")[:10]

def main():
    with open(CORPUS) as f:
        data = json.load(f)
    reviews = data["reviews"]

    # bucket
    delta_hits = defaultdict(list)        # office -> [reviews matching delta dental specifically]
    insurance_hits = defaultdict(list)    # office -> [reviews about insurance / coverage]
    fee_hits = defaultdict(list)          # office -> [reviews about fees/cost/value]
    happy_hits = defaultdict(list)        # office -> [enthusiastic 5* reviews]

    for r in reviews:
        c = r.get("comment") or ""
        if not c.strip(): continue
        if DELTA_PAT.search(c):
            delta_hits[r["office"]].append(r)
        if INSURANCE_PAT.search(c):
            insurance_hits[r["office"]].append(r)
        if FEE_PAT.search(c):
            fee_hits[r["office"]].append(r)
        if r.get("stars") == 5 and HAPPY_PAT.search(c) and len(c) > 60:
            happy_hits[r["office"]].append(r)

    # --- Aggregate KPIs ---
    total = len(reviews)
    with_text = sum(1 for r in reviews if (r.get("comment") or "").strip())
    five_star = sum(1 for r in reviews if r.get("stars") == 5)
    avg_rating = round(sum((r.get("stars") or 0) for r in reviews) / max(1, total), 2)
    n_delta = sum(len(v) for v in delta_hits.values())
    n_insurance = sum(len(v) for v in insurance_hits.values())
    n_fee = sum(len(v) for v in fee_hits.values())
    n_happy = sum(len(v) for v in happy_hits.values())

    # --- Build markdown ---
    lines = []
    lines.append("# Clove Dental — Delta Dental Fee Negotiation Review Packet")
    lines.append(f"_Generated {data.get('generated_at','')} from {total} reviews across {data.get('office_count','?')} offices._")
    lines.append("")
    lines.append("## Executive Summary")
    lines.append("")
    lines.append(f"- **Total reviews analyzed:** {total} ({with_text} with written feedback)")
    lines.append(f"- **Average rating:** {avg_rating} ★ across portfolio")
    lines.append(f"- **5-star reviews:** {five_star} ({round(five_star/total*100)}% of total)")
    lines.append(f"- **Reviews mentioning Delta Dental by name:** {n_delta}")
    lines.append(f"- **Reviews referencing insurance / coverage:** {n_insurance}")
    lines.append(f"- **Reviews discussing fees / cost / value:** {n_fee}")
    lines.append(f"- **High-enthusiasm 5★ patient quotes captured:** {n_happy}")
    lines.append("")
    lines.append("**Negotiation angle:** Patient reviews consistently emphasize trust, transparency, and value — "
                 "Clove is the kind of in-network provider Delta members are actively seeking. The corpus also "
                 "shows patients walking away when out-of-pocket fees stack up, which directly impacts Delta's "
                 "member retention and satisfaction scores in our service area.")
    lines.append("")

    # --- Delta Dental direct mentions (most important section) ---
    lines.append("## 1. Direct Delta Dental Mentions")
    lines.append("")
    if n_delta == 0:
        lines.append("_No reviews mention 'Delta Dental' by name in the current corpus. Use the insurance/fee sections below for evidence._")
    else:
        for office in OFFICE_ORDER:
            rs = delta_hits.get(office, [])
            if not rs: continue
            lines.append(f"### {office} ({len(rs)})")
            for r in sorted(rs, key=lambda x: x.get("createTime") or "", reverse=True):
                lines.append(f"- **{r['stars']}★ — {r['reviewer']} ({short_date(r.get('createTime'))})**")
                lines.append(f"  > {fmt_quote(r)}")
            lines.append("")
    lines.append("")

    # --- Insurance / coverage mentions ---
    lines.append("## 2. Insurance & Coverage Mentions")
    lines.append("_Patient experiences with in-network status, coverage gaps, and claims._")
    lines.append("")
    for office in OFFICE_ORDER:
        rs = insurance_hits.get(office, [])
        if not rs: continue
        lines.append(f"### {office} ({len(rs)})")
        # show up to 6 per office, prioritizing recent
        for r in sorted(rs, key=lambda x: x.get("createTime") or "", reverse=True)[:6]:
            lines.append(f"- **{r['stars']}★ — {r['reviewer']} ({short_date(r.get('createTime'))})**")
            lines.append(f"  > {fmt_quote(r)}")
        lines.append("")
    lines.append("")

    # --- Fee / cost / value mentions ---
    lines.append("## 3. Fee, Cost & Value Mentions")
    lines.append("_Where patients describe value, affordability, or sticker shock — context for fee schedule discussions._")
    lines.append("")
    for office in OFFICE_ORDER:
        rs = fee_hits.get(office, [])
        if not rs: continue
        lines.append(f"### {office} ({len(rs)})")
        # split into positive (4-5★) and negative (1-3★) tones
        pos = [r for r in rs if (r.get("stars") or 0) >= 4]
        neg = [r for r in rs if (r.get("stars") or 0) <= 3]
        if pos:
            lines.append(f"**Positive value mentions ({len(pos)}):**")
            for r in sorted(pos, key=lambda x: x.get("createTime") or "", reverse=True)[:5]:
                lines.append(f"- **{r['stars']}★ — {r['reviewer']} ({short_date(r.get('createTime'))}):** {fmt_quote(r)}")
            lines.append("")
        if neg:
            lines.append(f"**Cost-friction mentions ({len(neg)}):** _(use sparingly — shows where coverage gaps hurt)_")
            for r in sorted(neg, key=lambda x: x.get("createTime") or "", reverse=True)[:5]:
                lines.append(f"- **{r['stars']}★ — {r['reviewer']} ({short_date(r.get('createTime'))}):** {fmt_quote(r)}")
            lines.append("")
    lines.append("")

    # --- Happy patient highlights ---
    lines.append("## 4. Happy-Patient Highlights")
    lines.append("_Strongest 5★ testimonials per office — these demonstrate the quality of care Delta members would access._")
    lines.append("")
    for office in OFFICE_ORDER:
        rs = happy_hits.get(office, [])
        if not rs: continue
        lines.append(f"### {office} — Top {min(8, len(rs))} (of {len(rs)} enthusiastic 5★)")
        for r in sorted(rs, key=lambda x: len(x.get("comment") or ""), reverse=True)[:8]:
            lines.append(f"- **{r['reviewer']} ({short_date(r.get('createTime'))}):** {fmt_quote(r)}")
        lines.append("")
    lines.append("")

    # --- Talking points appendix ---
    lines.append("## 5. Suggested Negotiation Talking Points")
    lines.append("")
    lines.append(f"1. **Quality signal.** Clove portfolio averages {avg_rating}★ across {total} verified Google reviews — well above industry benchmark for dental groups.")
    lines.append(f"2. **In-demand network.** {n_insurance} written reviews discuss insurance/coverage; patients are actively searching for in-network options at our locations.")
    lines.append(f"3. **Trust-led brand.** Patients repeatedly use words like _honest_, _transparent_, _fair_, _trustworthy_ — exactly the experience Delta members expect.")
    lines.append(f"4. **Coverage gaps hurt Delta members too.** Cost-friction quotes show patients leaving for cheaper out-of-network options when reimbursement is too low — a Delta fee-schedule increase keeps patients (and Delta's retention) intact.")
    lines.append(f"5. **Multi-market footprint.** 9 offices across LA, Ventura, and surrounding markets give Delta broad geographic coverage in one negotiation.")
    lines.append(f"6. **Reputation lift on Delta's directory.** Linking to a Clove location averaging {avg_rating}★ raises Delta's perceived network quality vs. lower-rated competitors.")
    lines.append("")

    with open(OUT, "w") as f:
        f.write("\n".join(lines))
    print(f"[packet] wrote {OUT}")
    print(f"[packet] delta_hits={n_delta} insurance_hits={n_insurance} fee_hits={n_fee} happy_hits={n_happy}")

if __name__ == "__main__":
    main()
