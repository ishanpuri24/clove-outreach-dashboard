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
PRIV = REPO / "data" / "_contactout_private.json"
BOUNCES = REPO / "data" / "_bounces.json"
VERIF   = REPO / "data" / "_email_verification.json"

API = "https://api.contactout.com/v1"
HTTPX_KW = dict(timeout=30, headers={"Accept": "application/json", "Content-Type": "application/json"})


def _post_with_backoff(path, body, max_retries=4):
    """POST with exponential backoff on 429/5xx. Returns httpx.Response or None on network error.
    Also honors Retry-After header when present.
    """
    delay = 1.5
    for attempt in range(max_retries):
        try:
            r = httpx.post(f"{API}{path}", json=body, **HTTPX_KW)
        except Exception as e:
            # Network hiccup — brief pause and retry
            if attempt < max_retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            return e  # return the exception so caller can wrap
        if r.status_code == 429 or 500 <= r.status_code < 600:
            ra = r.headers.get("Retry-After")
            if ra and ra.isdigit():
                wait = min(int(ra), 20)
            else:
                wait = delay
            if attempt < max_retries - 1:
                time.sleep(wait)
                delay = min(delay * 2, 20)
                continue
        return r
    return r

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


def load_skip_domains():
    """Build the FULL skip set: already-enriched + bounced + verification-failed + blocklist.

    This is the single source of truth for "don’t hit this again".
    Returns (skip_domains_set, skip_emails_set, reason_by_domain_dict).
    """
    skip_domains = set(BLOCK_DOMAINS)
    skip_emails = set()
    reason = {}

    # 1) Already-enriched via ContactOut (private file)
    if PRIV.exists():
        try:
            priv = json.loads(PRIV.read_text())
            for d in (priv.get("contacts_by_domain") or {}):
                d = d.lower().strip()
                if d:
                    skip_domains.add(d)
                    reason.setdefault(d, "already_enriched")
        except Exception:
            pass

    # 2) Bounced-history file (external + internal)
    if BOUNCES.exists():
        try:
            b = json.loads(BOUNCES.read_text())
            for em in (b.get("external_prospect_bounces") or []) + (b.get("internal_bounces") or []):
                em = (em or "").lower().strip()
                if "@" in em:
                    skip_emails.add(em)
                    dom = em.split("@", 1)[1]
                    skip_domains.add(dom)
                    reason.setdefault(dom, "bounced_history")
        except Exception:
            pass

    # 3) Email-verification file — no_mx / no_match / invalid / bounced_history
    if VERIF.exists():
        try:
            ev = json.loads(VERIF.read_text())
            bad = {"no_mx", "invalid", "bounced_history"}
            for d, v in (ev.get("results") or {}).items():
                if isinstance(v, dict) and v.get("status") in bad:
                    d = d.lower().strip()
                    skip_domains.add(d)
                    reason.setdefault(d, f"verif:{v.get('status')}")
        except Exception:
            pass

    return skip_domains, skip_emails, reason


def blocked(p, skip_domains=None):
    d = (p.get("domain") or "").lower()
    n = (p.get("name") or "").lower()
    if not d: return True
    if skip_domains is not None and d in skip_domains: return True
    if d in BLOCK_DOMAINS: return True
    if any(b in n for b in NAME_BLOCK): return True
    if any(b in d for b in ("dental","dentist","orthodont")): return True
    return False


def _do_search(body):
    """Raw call — returns (status, total, list_of_(url, profile))."""
    r = _post_with_backoff("/people/search", body)
    if not hasattr(r, "status_code"):
        return (None, 0, [], f"net:{r}")
    if r.status_code != 200:
        return (r.status_code, 0, [], f"{r.status_code}:{r.text[:120]}")
    d = r.json()
    raw = d.get("profiles", {}) or {}
    if isinstance(raw, dict):
        pairs = list(raw.items())
    elif isinstance(raw, list):
        pairs = [(p.get("li_url") or p.get("linkedin_url") or f"idx{i}", p) for i,p in enumerate(raw)]
    else:
        pairs = []
    return (200, d.get("metadata",{}).get("total_results", 0), pairs, None)


def search_people(company, titles, limit=3):
    """Search strategy (ContactOut AND of company+title is too strict for small orgs):
      1) Try company + job_title arrays. If >0 results, use them.
      2) Fall back to company-only search, then filter client-side by title keyword match.
    """
    # Attempt 1: strict AND
    status, tot, pairs, err = _do_search({
        "company": [company],
        "job_title": titles,
        "page": 1,
        "reveal_info": False,
    })
    if err and status != 200:
        return {"error": err, "profiles": []}

    if tot == 0 or not pairs:
        # Attempt 2: company-only, filter client-side
        status, tot, pairs, err = _do_search({
            "company": [company],
            "page": 1,
            "reveal_info": False,
        })
        if err and status != 200:
            return {"error": err, "profiles": []}
        # Client-side title filter: any target-title token appears in profile.title
        title_tokens = set()
        for t in titles:
            for tok in t.lower().split():
                if len(tok) > 2:
                    title_tokens.add(tok)
        filtered = []
        for url, p in pairs:
            t = (p.get("title") or "").lower()
            if any(tok in t for tok in title_tokens):
                filtered.append((url, p))
        # If title filter kills everything, keep top pairs unfiltered (better than nothing)
        pairs = filtered or pairs[:limit]

    out = []
    for url, p in pairs[:limit]:
        co = p.get("company") or {}
        co_name = co.get("name") if isinstance(co, dict) else co
        # Confirm company match (case-insensitive first-token substring)
        if co_name and company.lower().split()[0] in (co_name or "").lower():
            out.append({
                "linkedin_url": url,
                "full_name": p.get("full_name"),
                "title": p.get("title"),
                "company": co_name,
                "location": p.get("location"),
            })
    return {"error": None, "profiles": out, "total": tot}


def enrich(full_name, company_domain, linkedin_url=None):
    """POST /v1/people/enrich to reveal work_email. Prefer LinkedIn URL if we have it."""
    body = {"include": ["work_email", "personal_email"]}
    if linkedin_url:
        body["linkedin_url"] = linkedin_url
    else:
        body["full_name"] = full_name
        body["company_domain"] = [company_domain]
    r = _post_with_backoff("/people/enrich", body)
    if not hasattr(r, "status_code"):
        return {"error": f"net:{r}"}
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
    ap.add_argument("--budget", type=int, default=100, help="Max prospects to attempt this run")
    ap.add_argument("--sleep", type=float, default=0.6, help="Base sleep between prospects (s)")
    ap.add_argument("--per-prospect", type=int, default=2, help="Max profiles to enrich per prospect (multi-DM)")
    ap.add_argument("--time-limit", type=int, default=480, help="Max seconds before checkpoint-and-stop (default 480 = 8min)")
    ap.add_argument("--dry", action="store_true", help="Print what would be enriched, don't call API")
    args = ap.parse_args()

    pool = json.loads(POOL.read_text())
    prospects = pool["prospects"]

    # Build the FULL skip set so we never re-hit an already-touched or bounced domain
    skip_domains, skip_emails, skip_reason = load_skip_domains()
    print(f"skip_domains={len(skip_domains)}  skip_emails={len(skip_emails)}")

    # Rank: has domain, not enriched yet, not in skip set, sort by score desc
    ranked = [p for p in prospects
              if p.get("has_website") and p.get("domain")
              and not p.get("co_contacts")
              and not blocked(p, skip_domains)]
    ranked.sort(key=lambda x: -x.get("score", 0))
    todo = ranked[:args.budget]

    # Report why we skipped what we skipped (top 10)
    skipped_here = [p for p in prospects
                    if p.get("has_website") and p.get("domain")
                    and (p.get("domain") or "").lower() in skip_domains]
    reasons = {}
    for p in skipped_here:
        r = skip_reason.get((p.get("domain") or "").lower(), "blocklist")
        reasons[r] = reasons.get(r, 0) + 1

    print(f"pool={len(prospects)}  candidates={len(ranked)}  budget={args.budget}  will_process={len(todo)}  skipped_pool={len(skipped_here)}  skip_reasons={reasons}")
    if args.dry:
        print("\n-- SKIP SAMPLE (why we WON'T hit them) --")
        for p in skipped_here[:10]:
            r = skip_reason.get((p.get("domain") or "").lower(), "blocklist")
            print(f"  SKIP [{r}]  {p['name'][:38]:38}  {p['domain']}")
        print("\n-- WILL ENRICH --")
        for p in todo[:15]:
            print(f"  - {p['name'][:44]:44}  {p['domain']:30}  {p['vertical']}")
        return

    LOG.parent.mkdir(parents=True, exist_ok=True)
    log_fh = LOG.open("a")

    # Load prior private file so we merge rather than clobber prior domains
    priv = {"generated_at": None, "contacts_by_domain": {}}
    if PRIV.exists():
        try:
            priv = json.loads(PRIV.read_text())
            priv.setdefault("contacts_by_domain", {})
        except Exception:
            pass
    contacts_by_domain = priv["contacts_by_domain"]

    enrich_calls = 0
    contacts_found = 0
    consec_429 = 0  # circuit-breaker if the API is unhappy
    from datetime import datetime, timezone
    start_ts = time.time()
    def _save_progress(reason=""):
        POOL.write_text(json.dumps(pool, indent=2))
        priv["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        priv["contacts_by_domain"] = contacts_by_domain
        PRIV.write_text(json.dumps(priv, indent=2))
        if reason:
            print(f"      .. progress saved ({reason})")
    for i, p in enumerate(todo, 1):
        # Time cap so we never spin for 10min without checkpointing
        if args.time_limit and (time.time() - start_ts) > args.time_limit:
            print(f"      !! time_limit ({args.time_limit}s) reached after {i-1} prospects — stopping")
            _save_progress("time_limit")
            break
        # Checkpoint every 10 prospects so we never lose work again
        if i > 1 and (i-1) % 10 == 0:
            _save_progress(f"checkpoint @ {i-1}")
        titles = TITLES_BY_VERTICAL.get(p["vertical"], ["Owner","Manager","Director"])
        print(f"[{i}/{len(todo)}] {p['name'][:44]:44}  {p['domain']}")

        # 1) search — costs against search_quota
        s = search_people(p["name"], titles, limit=max(2, args.per_prospect))
        if s["error"]:
            log_fh.write(json.dumps({"prospect": p["name"], "stage":"search", "err":s["error"]}) + "\n")
            if "429" in str(s["error"]):
                consec_429 += 1
                if consec_429 >= 3:
                    print("      !! 3 consecutive 429s — cooling down 30s")
                    time.sleep(30)
                    consec_429 = 0
            continue
        consec_429 = 0
        if not s["profiles"]:
            log_fh.write(json.dumps({"prospect": p["name"], "stage":"search", "err":"no_match", "total":s.get("total")}) + "\n")
            continue

        # 2) enrich up to per_prospect profiles
        for top in s["profiles"][:args.per_prospect]:
            e = enrich(top["full_name"], p["domain"], linkedin_url=top["linkedin_url"])
            enrich_calls += 1
            if e["error"]:
                log_fh.write(json.dumps({"prospect": p["name"], "stage":"enrich", "err":e["error"], "target":top}) + "\n")
                if "429" in str(e["error"]):
                    consec_429 += 1
                    if consec_429 >= 3:
                        time.sleep(30)
                        consec_429 = 0
                continue
            consec_429 = 0

            emails = e["emails"] or []
            best_email = next((em for em in emails if isinstance(em, str) and "@" in em), "")
            contact = {
                "name": top["full_name"],
                "title": top["title"],
                "linkedin_url": top["linkedin_url"],
                "emails": emails,
                "best_email": best_email,
                "source": "contactout",
            }
            # Write into prospect pool (public-safe fields already stripped downstream)
            p.setdefault("co_contacts", []).append(contact)
            # Write into PRIVATE file (gitignored) so pull_live_daily can render decision-maker card
            dom_key = (p.get("domain") or "").lower().strip()
            if dom_key:
                bucket = contacts_by_domain.setdefault(dom_key, {
                    "prospect_name": p.get("name"),
                    "office": p.get("nearest_office"),
                    "vertical": p.get("vertical"),
                    "contacts": [],
                })
                # Dedup by linkedin_url
                if not any((c.get("linkedin_url") == contact["linkedin_url"]) for c in bucket["contacts"]):
                    bucket["contacts"].append(contact)

            if emails:
                contacts_found += 1
                print(f"      -> {top['full_name']} ({top['title']}) : {emails[0]}")
            else:
                print(f"      -> {top['full_name']} ({top['title']}) : (no email revealed)")
            log_fh.write(json.dumps({"prospect": p["name"], "domain": p["domain"], "stage":"ok", "contact": contact}) + "\n")

        # Rate limit hygiene between prospects
        time.sleep(args.sleep)

    log_fh.close()

    # PII SCRUB: _b2b_prospect_pool.json is committed to a PUBLIC repo. Strip PII from co_contacts
    # before saving. Keep aggregate flags only (title + has_verified_email + source). Raw PII
    # (name, linkedin, emails, best_email) stays exclusively in gitignored _contactout_private.json.
    for pr in pool["prospects"]:
        raw = pr.get("co_contacts") or []
        if raw:
            pr["co_contacts"] = [
                {
                    "title": c.get("title"),
                    "has_verified_email": bool((c.get("best_email") or "").strip()),
                    "source": c.get("source") or "contactout",
                }
                for c in raw
            ]

    # Save prospect pool (public-safe now)
    POOL.write_text(json.dumps(pool, indent=2))

    # Save private file (gitignored) — pull_live_daily.py reads this for decision-maker cards
    priv["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    priv["contacts_by_domain"] = contacts_by_domain
    PRIV.write_text(json.dumps(priv, indent=2))
    total_domains = len(contacts_by_domain)
    total_dms = sum(len(v.get("contacts", [])) for v in contacts_by_domain.values())
    print(f"\ndone. enrich_calls={enrich_calls}  contacts_found={contacts_found}  domains_in_priv={total_domains}  total_dms={total_dms}")


if __name__ == "__main__":
    main()
