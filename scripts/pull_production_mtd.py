#!/usr/bin/env python3
"""Pull MTD (June 1 -> today) production per Clove clinic from Open Dental.

Critical learnings from live API (2026-06-10) vs the uploaded markdown:
- /procedurelogs IGNORES ProcDateStart/ProcDateEnd query params (they are not
  in the official docs at https://www.opendental.com/site/apiprocedurelogs.html
  and the server silently ignores unknown params).
- The supported filters are: PatNum, AptNum, ProcStatus, PlannedAptNum,
  ClinicNum, CodeNum, DateTStamp (entry timestamp >= ...), and pagination via
  Offset (page size = 100, no limit param).
- ProcStatus is a one-letter string: "C" Complete (production), "TP", "EC",
  "EO", "R", "D", "Cn", "TPi".
- ProcFee returns as a string; cast to float.
- Booleans return as strings.

Strategy:
  For each ClinicNum we care about, GET /procedurelogs?ProcStatus=C&ClinicNum=N
  paged by Offset. The server orders results most-recent-entry first. For each
  row, count ProcFee toward production if ProcDate is in [2026-06-01, today].
  Stop paging a clinic when we've seen MAX_OUT_OF_WINDOW consecutive rows whose
  ProcDate is BEFORE the window start (newer entries come first; once we're
  clearly past the window we exit) OR when we hit the per-clinic page cap.

Writes:
  data/od_production_mtd.json
"""
import json
import os
import sys
import time
from collections import defaultdict
from datetime import date

import httpx

BASE = "https://api.opendental.com/api/v1"
WINDOW_START = date(2026, 6, 1)
WINDOW_END = date.today()
PAGE = 100
SLEEP = 1.5  # be polite to the proxy (it 429'd at 0.2s)
TIMEOUT = 180
MAX_PAGES_PER_CLINIC = 80  # 8k rows safety cap
EXIT_AFTER_BEFORE_WINDOW_PAGES = 2  # 2 full pages with zero in-window rows

CLINIC_MAP = {
    3: "Hillview",
    4: "Camarillo",
    6: "Marin",
    8: "Riverpark",
    10: "Puri",
    12: "BH",
    14: "Encino",
    16: "SO",
    18: "SM",
}


_client = httpx.Client(
    timeout=TIMEOUT,
    headers={"Accept": "application/json"},
    trust_env=True,  # picks up HTTPS_PROXY and SSL_CERT_FILE
)


def http_get(url: str, retries: int = 5) -> list:
    last = None
    for attempt in range(retries):
        try:
            r = _client.get(url)
            if r.status_code == 429:
                wait = 5 * (attempt + 1)
                print(f"[od] 429, sleeping {wait}s", flush=True)
                time.sleep(wait)
                last = RuntimeError("429")
                continue
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list):
                raise RuntimeError(f"non-list response: {str(data)[:200]}")
            return data
        except httpx.ProxyError as e:
            wait = 8 * (attempt + 1)
            print(f"[od] proxy error ({e}), sleeping {wait}s", flush=True)
            time.sleep(wait)
            last = e
        except Exception as e:
            wait = 3 * (attempt + 1)
            print(f"[od] {type(e).__name__}: {e}; sleeping {wait}s", flush=True)
            time.sleep(wait)
            last = e
    raise last  # type: ignore


def parse_iso(s: str):
    try:
        y, m, d = s.split("-")
        return date(int(y), int(m), int(d))
    except Exception:
        return None


def fetch_clinic(cn: int) -> tuple[float, int, int]:
    """Returns (production_complete_usd, rows_counted_in_window, pages)."""
    production = 0.0
    in_window_rows = 0
    pages = 0
    offset = 0
    pages_with_no_in_window = 0

    while pages < MAX_PAGES_PER_CLINIC:
        url = (
            f"{BASE}/procedurelogs?ProcStatus=C&ClinicNum={cn}&Offset={offset}"
        )
        rows = http_get(url)
        pages += 1
        if not rows:
            break

        page_in_window = 0
        page_before_window = 0
        for r in rows:
            pd = parse_iso(str(r.get("ProcDate") or ""))
            if pd is None:
                continue
            if pd > WINDOW_END:
                # future-dated (shouldn't happen for Complete), skip
                continue
            if pd < WINDOW_START:
                page_before_window += 1
                continue
            # in window
            try:
                fee = float(r.get("ProcFee") or 0)
            except Exception:
                fee = 0.0
            production += fee
            in_window_rows += 1
            page_in_window += 1

        if page_in_window == 0:
            pages_with_no_in_window += 1
        else:
            pages_with_no_in_window = 0

        if pages_with_no_in_window >= EXIT_AFTER_BEFORE_WINDOW_PAGES:
            break

        if len(rows) < PAGE:
            break

        offset += PAGE
        time.sleep(SLEEP)

    print(
        f"[od] ClinicNum={cn:>2} ({CLINIC_MAP.get(cn,'?'):<10}): "
        f"${production:>10,.2f} | rows_in_window={in_window_rows} | pages={pages}",
        flush=True,
    )
    return production, in_window_rows, pages


OUT_PATH = "/home/user/workspace/clove-outreach-dashboard/data/od_production_mtd.json"


def write_snapshot(by_clinic, rows_by_clinic, pages_by_clinic, status="in_progress"):
    out = {
        "source": "open_dental",
        "endpoint": "/procedurelogs?ProcStatus=C&ClinicNum=<n> (paged by Offset)",
        "status": status,
        "window_start": WINDOW_START.isoformat(),
        "window_end": WINDOW_END.isoformat(),
        "production_mtd_usd_complete_by_office": by_clinic,
        "rows_in_window_by_office": rows_by_clinic,
        "pages_fetched_by_office": pages_by_clinic,
        "total_production_mtd_usd": round(sum(by_clinic.values()), 2),
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, indent=2)


def main() -> int:
    by_clinic = {}
    rows_by_clinic = {}
    pages_by_clinic = {}

    for cn in CLINIC_MAP.keys():
        prod, rows, pages = fetch_clinic(cn)
        by_clinic[CLINIC_MAP[cn]] = round(prod, 2)
        rows_by_clinic[CLINIC_MAP[cn]] = rows
        pages_by_clinic[CLINIC_MAP[cn]] = pages
        write_snapshot(by_clinic, rows_by_clinic, pages_by_clinic, status="in_progress")

    write_snapshot(by_clinic, rows_by_clinic, pages_by_clinic, status="complete")
    print(f"\n[od] wrote {OUT_PATH}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
