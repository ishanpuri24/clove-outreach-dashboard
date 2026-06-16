#!/usr/bin/env python3
"""
Build a consolidated GMB review corpus across all Clove offices from:
  - 7 successful per-location pulls in current_session_context/tool_calls/call_external_tool/
  - data/_gmb_raw_full.json (fallback for Beverly Hills + Hillview which returned empty)

Writes:
  data/_gmb_review_corpus.json   (PRIVATE — underscore prefix, reviewers' names)
"""
import json, os, glob

ROOT = "/home/user/workspace/clove-outreach-dashboard"
TC_DIR = "/home/user/workspace/current_session_context/tool_calls/call_external_tool"

LOC_TO_OFFICE = {
    "16540245410755416746": "Beverly Hills",
    "6002784370653219775":  "Oxnard Riverpark",
    "4396979876870755094":  "Thousand Oaks",
    "3424162762335073167":  "Sherman Oaks",
    "15322712011963486679": "Puri Dentistry",
    "2451402705824656361":  "Camarillo",
    "6534318906667721619":  "Hillview",
    "17491483726222827505": "Santa Monica",
    "18149932138550234736": "Encino",
}

STAR_MAP = {"ONE":1,"TWO":2,"THREE":3,"FOUR":4,"FIVE":5}

def normalize(r, office):
    return {
        "office": office,
        "reviewer": (r.get("reviewer") or {}).get("displayName") or "Anonymous",
        "stars": STAR_MAP.get(r.get("starRating"), r.get("starRating")) if isinstance(r.get("starRating"), str) else r.get("starRating"),
        "comment": r.get("comment") or "",
        "createTime": r.get("createTime"),
        "updateTime": r.get("updateTime"),
        "reply": (r.get("reviewReply") or {}).get("comment") or "",
        "replyTime": (r.get("reviewReply") or {}).get("updateTime"),
        "name": r.get("name"),
    }

def main():
    # 1) Per-location pulls
    corpus = []
    seen_names = set()
    for inp in sorted(glob.glob(os.path.join(TC_DIR, "input_mqfvu*.json"))):
        with open(inp) as f:
            inp_data = json.load(f)
        args = inp_data.get("arguments") or inp_data
        loc = args.get("location")
        out_path = inp.replace("input_", "output_")
        if not os.path.exists(out_path): continue
        with open(out_path) as f:
            out = json.load(f)
        result = out.get("result") or out
        reviews = result.get("reviews") or []
        office = LOC_TO_OFFICE.get(str(loc), f"loc:{loc}")
        for r in reviews:
            if r.get("name") in seen_names: continue
            seen_names.add(r.get("name"))
            corpus.append(normalize(r, office))

    # 2) Fallback: _gmb_raw_full.json for offices with no rows yet
    offices_with_data = {c["office"] for c in corpus}
    missing = [o for o in LOC_TO_OFFICE.values() if o not in offices_with_data]
    print(f"[corpus] missing offices after per-loc pulls: {missing}")

    raw_full_path = os.path.join(ROOT, "data", "_gmb_raw_full.json")
    if os.path.exists(raw_full_path):
        with open(raw_full_path) as f:
            raw_full = json.load(f)
        # actual shape: {"result": {"locationReviews": [{"name":"accounts/.../locations/<id>", "review": {...}}]}}
        lr = (raw_full.get("result") or {}).get("locationReviews") or []
        for entry in lr:
            name_path = entry.get("name", "")
            review = entry.get("review") or {}
            rid = review.get("reviewId")
            uniq_name = f"{name_path}/reviews/{rid}" if rid else name_path
            if uniq_name in seen_names: continue
            # infer office from location path
            office = None
            parts = name_path.split("/")
            if "locations" in parts:
                idx = parts.index("locations")
                if idx+1 < len(parts):
                    office = LOC_TO_OFFICE.get(parts[idx+1])
            if office and office in missing:
                seen_names.add(uniq_name)
                # review dict lacks "name" — synthesize for traceability
                review.setdefault("name", uniq_name)
                corpus.append(normalize(review, office))

    # sort by office, then date desc
    corpus.sort(key=lambda x: (x["office"], x.get("createTime") or ""), reverse=False)

    out_path = os.path.join(ROOT, "data", "_gmb_review_corpus.json")
    with open(out_path, "w") as f:
        json.dump({
            "generated_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
            "office_count": len({c["office"] for c in corpus}),
            "review_count": len(corpus),
            "by_office": {o: sum(1 for c in corpus if c["office"]==o) for o in sorted({c["office"] for c in corpus})},
            "reviews": corpus,
        }, f, indent=2)
    print(f"[corpus] wrote {out_path}  reviews={len(corpus)}  offices={len({c['office'] for c in corpus})}")
    # simple summary
    counts = {}
    for c in corpus:
        counts[c["office"]] = counts.get(c["office"],0)+1
    for o in sorted(counts):
        print(f"  {o}: {counts[o]}")

if __name__ == "__main__":
    main()
