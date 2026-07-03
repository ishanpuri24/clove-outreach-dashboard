#!/usr/bin/env python3
"""
ContactOut enrichment for B2B prospect pool.

Usage:
    python3 scripts/contactout_enrich.py [--budget N]

Reads /data/_b2b_prospect_pool.json, picks the top-N prospects that:
  - have a domain
  - are NOT already enriched (no `co_contacts` on the prospect)
  - are NOT in the blocklist
Then, for each prospect:
  1) POST /v1/people/search filtered by company name + vertical-appropriate job_title array
  2) Pick top profile (or top 1-2 by seniority)
  3) POST /v1/people/enrich by full_name + company_domain (array), include=["work_email"]
  4) Save contact details INTO the prospect record as `co_contacts: [{name,title,email,linkedin,confidence}]`
Writes back _b2b_prospect_pool.json in place.

Budget-conscious: ContactOut quota is 2000 people-enrichments / month; we default to 40 per run.
Auth: uses saved custom credential handle 'custom-cred:api.contactout.com' (via HTTPS_PROXY).
"""
import argparse, json, os, sys, time, httpx
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
POOL = REPO / "data" / "_b2b_prospect_pool.json"
LOG  = REPO / "data" / "_contactout_enrich_log.jsonl"

API = "https://api.contactout.com/v1"
HTTPX_KW = dict(timeout=30, headers={"Accept": "application/json", "Content-Type": "application/json"})

# Vertical → prioritized job titles (search AND enrich hints)
TITLES_BY_VERTICAL = {
    "senior_living":       ["Executive Director", "Administrator", "Director of Wellness", "Community Relations Director", "General Manager"],
    "schools_daycare":     ["Director", "Owner", "Head of School", "Principal", "Administrator"],
    "hr_heavy_employers":  ["HR Director", "Head of HR", "People Operations", "Benefits Manager", "VP HR"],
    "hotels":              ["General Manager", "Director of HR", "Director of Operations", "Front Office Manager"],
    "gyms_wellness":       ["Owner", "General Manager", "Studio Manager", "Franchisee", "Regional Manager"],
    "salons_spas":         ["Owner", "General Manager", "Spa Director"],
    "law_medical_offices": ["Managing Partner", "Office Administrator", "Practice Manager"],
}

# Domain-level blocklist (bounced) + name blocklist substrings
BLOCK_DOMAINS = {"gmail.com","yahoo.com","outlook.com","hotmail.com","clovedds.com","silverstarautos.com","divi.express"}
NAME_BLOCK = ("dental","dentist","orthodont","attorney","lawfirm","bank")


def blocked(p):
    d = (p.get("domain") or "").lower()
    n = (p.get("name") or "").lower()
    if not d or d in BLOCK_DOMAINS: return True
    if any(b in n for b in NAME_BLOCK): return True
    if any(b in d for b in ("dental","dentist","orthodont")): return True
    return False


def search_people(company, titles, limit=3):
    """POST /v1/people/search with company (array) + job_title (array). Returns top profiles."""
    body = {
        "company": [company],
        "job_title": titles,
        "page": 1,
        "reveal_info": False,
    }
    try:
        r = httpx.post(f"{API}/people/search", json=body, **HTTPX_KW)
    except Exception as e:
        return {"error": f"net:{e}", "profiles": []}
    if r.status_code != 200:
        return {"error": f"{r.status_code}:{r.text[:120]}", "profiles": []}
    d = r.json()
    raw_profiles = d.get("profiles", {}) or {}
    if isinstance(raw_profiles, dict):
        profiles = list(raw_profiles.items())[:limit]
    elif isinstance(raw_profiles, list):
        profiles = [(p.get("li_url") or p.get("linkedin_url") or f"idx{i}", p) for i,p in enumerate(raw_profiles)][:limit]
    else:
        profiles = []
    out = []
    for url, p in profiles:
        co = p.get("company") or {}
        co_name = co.get("name") if isinstance(co, dict) else co
        # Only keep matches where the profile's company matches the target company (case-insensitive substring)
        if co_name and company.lower().split()[0] in (co_name or "").lower():
            out.append({
                "linkedin_url": url,
                "full_name": p.get("full_name"),
                "title": p.get("title"),
                "company": co_name,
                "location": p.get("location"),
            })
    return {"error": None, "profiles": out, "total": d.get("metadata",{}).get("total_results")}


def enrich(full_name, company_domain, linkedin_url=None):
    """POST /v1/people/enrich to reveal work_email. Prefer LinkedIn URL if we have it."""
    body = {"include": ["work_email"]}
    if linkedin_url:
        body["linkedin_url"] = linkedin_url
    else:
        body["full_name"] = full_name
        body["company_domain"] = [company_domain]
    try:
        r = httpx.post(f"{API}/people/enrich", json=body, **HTTPX_KW)
    except Exception as e:
        return {"error": f"net:{e}"}
    if r.status_code != 200:
        return {"error": f"{r.status_code}:{r.text[:200]}"}
    d = r.json()
    prof = d.get("profile") or d.get("data") or {}
    emails = prof.get("work_email") or prof.get("emails") or []
    if isinstance(emails, str):
        emails = [emails]
    return {"error": None, "emails": emails, "raw": prof}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=int, default=40, help="Max ContactOut enrich calls this run")
    ap.add_argument("--dry", action="store_true", help="Print what would be enriched, don't call API")
    args = ap.parse_args()

    pool = json.loads(POOL.read_text())
    prospects = pool["prospects"]

    # Rank: has domain, not enriched yet, not blocked, sort by score desc
    ranked = [p for p in prospects
              if p.get("has_website") and p.get("domain")
              and not p.get("co_contacts")
              and not blocked(p)]
    ranked.sort(key=lambda x: -x.get("score", 0))
    todo = ranked[:args.budget]

    print(f"pool={len(prospects)}  candidates={len(ranked)}  budget={args.budget}  will_process={len(todo)}")
    if args.dry:
        for p in todo[:15]:
            print(f"  - {p['name'][:44]:44}  {p['domain']:30}  {p['vertical']}")
        return

    LOG.parent.mkdir(parents=True, exist_ok=True)
    log_fh = LOG.open("a")

    enrich_calls = 0
    contacts_found = 0
    for i, p in enumerate(todo, 1):
        titles = TITLES_BY_VERTICAL.get(p["vertical"], ["Owner","Manager","Director"])
        print(f"[{i}/{len(todo)}] {p['name'][:44]:44}  {p['domain']}")

        # 1) search — 0 cost (only counts against search_quota)
        s = search_people(p["name"], titles, limit=2)
        if s["error"]:
            log_fh.write(json.dumps({"prospect": p["name"], "stage":"search", "err":s["error"]}) + "\n")
            continue
        if not s["profiles"]:
            log_fh.write(json.dumps({"prospect": p["name"], "stage":"search", "err":"no_match", "total":s.get("total")}) + "\n")
            continue

        # 2) enrich the top profile
        top = s["profiles"][0]
        e = enrich(top["full_name"], p["domain"], linkedin_url=top["linkedin_url"])
        enrich_calls += 1
        if e["error"]:
            log_fh.write(json.dumps({"prospect": p["name"], "stage":"enrich", "err":e["error"], "target":top}) + "\n")
            continue

        emails = e["emails"] or []
        contact = {
            "name": top["full_name"],
            "title": top["title"],
            "linkedin_url": top["linkedin_url"],
            "emails": emails,
            "source": "contactout",
        }
        p.setdefault("co_contacts", []).append(contact)
        if emails:
            contacts_found += 1
            print(f"      -> {top['full_name']} ({top['title']}) : {emails[0]}")
        else:
            print(f"      -> {top['full_name']} ({top['title']}) : (no email revealed)")
        log_fh.write(json.dumps({"prospect": p["name"], "domain": p["domain"], "stage":"ok", "contact": contact}) + "\n")

        # Rate limit hygiene
        time.sleep(0.25)

    log_fh.close()

    # Save back
    POOL.write_text(json.dumps(pool, indent=2))
    print(f"\ndone. enrich_calls={enrich_calls}  contacts_found={contacts_found}")


if __name__ == "__main__":
    main()
