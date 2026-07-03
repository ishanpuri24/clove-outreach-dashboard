#!/usr/bin/env python3
"""Merge Google Maps Places search results (saved as JSON files) into the
_b2b_prospect_pool.json, with dedup + blocklist + scoring.

Reads a manifest JSONL where each line is:
  {"office": "Beverly Hills", "vertical": "senior_living", "office_lat": 34.06,
   "office_lng": -118.38, "path": "/absolute/path/to/output_xxx.json"}

Uses the same shape/score conventions as the existing pool.
"""
import json, math, os, sys, re, urllib.parse
from datetime import datetime, timezone

POOL_PATH = "/home/user/workspace/clove-outreach-dashboard/data/_b2b_prospect_pool.json"

BLOCK_KEYWORDS_NAME = re.compile(r"\b(dental|dentist|orthodont|attorney|lawfirm|law firm|bank(?!er))\b", re.I)
BLOCK_DOMAINS_EXACT = {
    "gmail.com","yahoo.com","outlook.com","hotmail.com","aol.com",
    "clovedds.com","clovedental.com","icloud.com","live.com","msn.com",
    "facebook.com","instagram.com","yelp.com","tripadvisor.com","google.com",
    # bounced domains
    "silverstarautos.com","divi.express",
}
BLOCK_DOMAIN_SUBSTRINGS = ["dental","dentist","orthodont"]

def haversine_mi(lat1, lng1, lat2, lng2):
    R = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * R * math.asin(math.sqrt(a))

def registrable_domain(url):
    if not url: return ""
    try:
        u = urllib.parse.urlparse(url if "://" in url else f"http://{url}")
        host = (u.hostname or "").lower().strip()
        if host.startswith("www."): host = host[4:]
        return host
    except Exception:
        return ""

def score_prospect(rating, review_count, distance_mi):
    r = rating or 0.0
    rc = review_count or 0
    # bounded scoring: rating quality * log(reviews) * distance decay
    quality = max(0.0, r - 3.0)  # 0-2
    review_boost = math.log10(1 + rc)  # 0-3.x
    dist_decay = max(0.2, 1.0 - min(distance_mi / 8.0, 0.8))
    return round(quality * 5 * review_boost * dist_decay, 2)

def load_places(path):
    d = json.load(open(path))
    r = d.get("result") if isinstance(d, dict) else None
    if isinstance(r, dict) and "places" in r:
        return r["places"]
    if isinstance(d, dict) and "places" in d:
        return d["places"]
    return []

def extract_prospect(place, vertical, office_name, office_lat, office_lng):
    name = (place.get("displayName") or {}).get("text") or ""
    website = place.get("websiteUri") or ""
    domain = registrable_domain(website)
    address = place.get("formattedAddress") or ""
    phone = place.get("nationalPhoneNumber") or ""
    rating = place.get("rating")
    review_count = place.get("userRatingCount") or 0
    loc = place.get("location") or {}
    lat = loc.get("latitude")
    lng = loc.get("longitude")
    if lat is None or lng is None:
        distance_mi = 99.0
    else:
        distance_mi = round(haversine_mi(office_lat, office_lng, lat, lng), 2)
    return {
        "name": name,
        "vertical": vertical,
        "nearest_office": office_name,
        "distance_mi": distance_mi,
        "address": address,
        "phone": phone,
        "website": website,
        "domain": domain,
        "rating": rating,
        "review_count": review_count,
        "score": score_prospect(rating, review_count, distance_mi),
        "has_website": bool(website),
    }

def is_blocked(p):
    name = (p.get("name") or "").lower()
    domain = (p.get("domain") or "").lower()
    if not name: return True
    if BLOCK_KEYWORDS_NAME.search(name): return True
    if domain and domain in BLOCK_DOMAINS_EXACT: return True
    for sub in BLOCK_DOMAIN_SUBSTRINGS:
        if sub in domain: return True
    return False

def main(manifest_path):
    # load current pool
    pool = json.load(open(POOL_PATH))
    existing = pool.get("prospects", [])
    seen_domains = {(p.get("domain") or "").lower() for p in existing if p.get("domain")}
    # track duplicates without website by (name, address)
    seen_no_domain = {(p.get("name",""), p.get("address","")) for p in existing if not p.get("domain")}

    added = 0
    blocked = 0
    dup = 0
    per_office = {}
    per_vertical = {}

    with open(manifest_path) as f:
        manifest = [json.loads(l) for l in f if l.strip()]

    for entry in manifest:
        path = entry["path"]
        if not os.path.exists(path):
            print(f"MISSING: {path}", file=sys.stderr)
            continue
        places = load_places(path)
        for place in places:
            pr = extract_prospect(place, entry["vertical"], entry["office"],
                                  entry["office_lat"], entry["office_lng"])
            if is_blocked(pr):
                blocked += 1
                continue
            d = (pr.get("domain") or "").lower()
            if d and d in seen_domains:
                dup += 1
                continue
            if not d:
                key = (pr["name"], pr["address"])
                if key in seen_no_domain:
                    dup += 1
                    continue
                seen_no_domain.add(key)
            else:
                seen_domains.add(d)
            existing.append(pr)
            added += 1

    # recompute aggregates
    for p in existing:
        per_office[p.get("nearest_office","?")] = per_office.get(p.get("nearest_office","?"),0)+1
        per_vertical[p.get("vertical","?")] = per_vertical.get(p.get("vertical","?"),0)+1

    pool["prospects"] = existing
    pool["total_prospects"] = len(existing)
    pool["total_with_website"] = sum(1 for p in existing if p.get("has_website"))
    pool["by_office"] = per_office
    pool["by_vertical"] = per_vertical
    pool["generated_at"] = datetime.now(timezone.utc).isoformat()

    with open(POOL_PATH, "w") as f:
        json.dump(pool, f, indent=2)

    print(f"added={added} blocked={blocked} dup={dup} total={len(existing)}")
    print(f"by_office={per_office}")
    print(f"by_vertical={per_vertical}")

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/home/user/workspace/clove-outreach-dashboard/data/_places_manifest.jsonl")
