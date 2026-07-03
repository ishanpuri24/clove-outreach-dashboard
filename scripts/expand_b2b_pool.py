#!/usr/bin/env python3
"""Expand the B2B prospect pool via Google Maps Places API.

Sweeps every office x every vertical, dedupes by domain, merges into
data/_b2b_prospect_pool.json. Idempotent — safe to re-run.

Data source: `google_maps_platform-search-places` via the Pipedream MCP
connector. This script calls the connector through the same mechanism as
pull_live_daily.py (using pplx-tool call_external_tool).

Usage:
    python3 scripts/expand_b2b_pool.py
"""
from __future__ import annotations
import json
import math
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
POOL_PATH = DATA / "_b2b_prospect_pool.json"

# 9 offices with confirmed GMB coordinates
OFFICES = [
    ("Beverly Hills",   34.0673977, -118.3879952),
    ("Camarillo",       34.2264,    -119.0378),
    ("Encino",          34.1595,    -118.5013),
    ("Hillview",        34.2771289, -119.24772),      # Ventura
    ("Oxnard Riverpark",34.243141,  -119.1819866),
    ("Puri Dentistry",  34.2038366, -119.1783887),
    ("Santa Monica",    34.0183169, -118.4920713),
    ("Sherman Oaks",    34.1514364, -118.452697),
    ("Thousand Oaks",   34.1847735, -118.8828329),
]

# Vertical -> list of text queries. Each query is one Places API call.
VERTICALS = {
    "senior_living": [
        "senior living", "assisted living facility", "memory care",
    ],
    "schools_daycare": [
        "preschool", "daycare", "montessori school", "childcare center",
    ],
    "hr_heavy_employers": [
        "corporate office", "large employer", "manufacturing plant",
        "distribution center", "warehouse company",
    ],
    "gyms_wellness": [
        "gym", "fitness center", "yoga studio", "pilates studio",
    ],
    "salons_spas": [
        "hair salon", "day spa", "nail salon", "barbershop",
    ],
    "hotels": [
        "hotel", "boutique hotel", "extended stay",
    ],
    "real_estate": [
        "real estate office", "property management",
    ],
    "law_medical_offices": [
        "law firm", "chiropractor", "physical therapy clinic",
        "medical office", "veterinary clinic",
    ],
    "auto_home_services": [
        "auto dealership", "home services company",
    ],
    "restaurants_local": [
        "coffee shop", "restaurant group",
    ],
}

RADIUS_METERS = 8046  # 5 miles


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def haversine_mi(lat1, lon1, lat2, lon2):
    R = 3958.7613
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def domain_of(url: str) -> str:
    if not url:
        return ""
    s = url.lower().split("//", 1)[-1]
    s = s.split("/", 1)[0]
    if s.startswith("www."):
        s = s[4:]
    return s


def load_pool() -> dict:
    if POOL_PATH.exists():
        try:
            return json.loads(POOL_PATH.read_text())
        except Exception:
            pass
    return {
        "generated_at": utcnow(),
        "prospects": [],
        "radius_miles_per_office": 5,
        "dedup_source": "domain",
    }


def call_places(text_query: str, lat: float, lng: float) -> list:
    """Call Places Text Search via pplx-tool CLI. Returns raw places list."""
    args = {
        "textQuery": text_query,
        "locationBias": {"circle": {"center": {"latitude": lat, "longitude": lng},
                                      "radius": RADIUS_METERS}},
        "rankPreference": "RELEVANCE",
    }
    payload = json.dumps({
        "tool_name": "google_maps_platform-search-places",
        "source_id": "google_maps_platform__pipedream",
        "arguments": args,
    })
    try:
        r = subprocess.run(
            ["pplx-tool", "call_external_tool"],
            input=payload, capture_output=True, text=True, timeout=45,
            env={**os.environ, "PPLX_TOOL_API_CREDENTIALS": "pplx-tool:call_external_tool"},
        )
        if r.returncode != 0:
            print(f"    [warn] places call failed: {r.stderr[:200]}")
            return []
        # pplx-tool prints JSON to stdout
        out = r.stdout.strip()
        # find last json object in output
        start = out.rfind("{")
        if start < 0:
            return []
        try:
            j = json.loads(out[start:])
        except Exception:
            return []
        # Response envelope: {"result": {...}} or direct
        res = j.get("result", j)
        return res.get("places") or []
    except Exception as e:
        print(f"    [warn] {e}")
        return []


def normalise_place(p: dict, office_name: str, office_lat: float, office_lng: float,
                     vertical: str) -> dict | None:
    name = (p.get("displayName") or {}).get("text") or ""
    website = p.get("websiteUri") or ""
    dom = domain_of(website)
    loc = p.get("location") or {}
    lat, lng = loc.get("latitude"), loc.get("longitude")
    rating = p.get("rating")
    reviews = p.get("userRatingCount") or 0
    if not (name and dom and lat and lng):
        return None
    dist = haversine_mi(office_lat, office_lng, lat, lng)
    if dist > 6:  # allow slight overrun beyond 5mi bias
        return None
    score = round((rating or 0) * math.log(max(1, reviews)), 2)
    return {
        "name": name,
        "vertical": vertical,
        "nearest_office": office_name,
        "distance_mi": round(dist, 2),
        "address": p.get("formattedAddress") or "",
        "phone": p.get("nationalPhoneNumber") or "",
        "website": website,
        "domain": dom,
        "rating": rating,
        "review_count": reviews,
        "score": score,
        "has_website": True,
    }


# Blocklist: legal/finance/dental competitors + gmail.com etc
BLOCK_DOMAINS = {
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com",
    "clovedds.com", "clove-dental.com",
}
BLOCK_KEYWORDS = ("dentist", "dental", "orthodontist", "endodontic",
                  "periodont", "attorney", "lawfirm", "bank")


def is_blocked(p: dict) -> bool:
    d = p.get("domain") or ""
    n = (p.get("name") or "").lower()
    if d in BLOCK_DOMAINS:
        return True
    if any(k in d for k in ("dental", "dentist", "orthodont")):
        return True
    if any(k in n for k in BLOCK_KEYWORDS):
        return True
    return False


def main() -> None:
    pool = load_pool()
    existing = {(p.get("domain") or "").lower(): p for p in pool.get("prospects") or []}
    original_ct = len(existing)
    print(f"Loaded existing pool: {original_ct} prospects")

    added = 0
    calls = 0
    for office_name, lat, lng in OFFICES:
        print(f"\n=== {office_name} ({lat:.4f},{lng:.4f}) ===")
        for vertical, queries in VERTICALS.items():
            for q in queries:
                calls += 1
                places = call_places(q, lat, lng)
                new_here = 0
                for p in places:
                    row = normalise_place(p, office_name, lat, lng, vertical)
                    if not row:
                        continue
                    if is_blocked(row):
                        continue
                    d = row["domain"]
                    if d in existing:
                        continue
                    existing[d] = row
                    added += 1
                    new_here += 1
                if new_here:
                    print(f"  [{vertical:22s}] '{q}' -> +{new_here}")

    prospects = list(existing.values())
    by_vert: dict = {}
    by_off: dict = {}
    for p in prospects:
        by_vert[p["vertical"]] = by_vert.get(p["vertical"], 0) + 1
        by_off[p["nearest_office"]] = by_off.get(p["nearest_office"], 0) + 1

    pool["prospects"] = prospects
    pool["total_prospects"] = len(prospects)
    pool["total_with_website"] = len(prospects)
    pool["by_vertical"] = by_vert
    pool["by_office"] = by_off
    pool["generated_at"] = utcnow()
    pool["last_expansion_calls"] = calls
    pool["radius_miles_per_office"] = 5

    POOL_PATH.write_text(json.dumps(pool, indent=2))
    print(f"\nPool: {original_ct} -> {len(prospects)}  (+{added} new, {calls} API calls)")
    print("By office:")
    for k, v in sorted(by_off.items()):
        print(f"  {k:20s} {v}")
    print("By vertical:")
    for k, v in sorted(by_vert.items(), key=lambda x: -x[1]):
        print(f"  {k:22s} {v}")


if __name__ == "__main__":
    main()
