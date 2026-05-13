#!/usr/bin/env python3
"""Daily Clove patient-acquisition dashboard refresh orchestrator.

Self-deployable, fast-mode-safe routine refresh entry point for the
scheduled daily task and for local/GitHub checkouts. Replaces the
prior reliance on ``process_daily_run.py`` (an outreach payload
stager with no connector / refresh logic).

Behavior:

  * Reads private configs and last-known-good summaries from
    ``/home/user/workspace/cron_tracking/a3b9de2f`` when present.
  * In ``--fast`` mode (the default) it does not perform live
    connector calls. It re-stamps the public snapshot's
    ``generated_at`` and refreshes the routine-refresh status block,
    merges sanitized aggregates that already exist on disk, and
    leaves any unavailable metric marked ``pending`` or ``stale``
    rather than fabricating values.
  * Always runs ``--no-send`` by default. Outbound outreach is never
    triggered from this script. The script will refuse to send even
    if ``--no-send`` is removed unless an explicit ``--sender`` is
    supplied and a sender-bound connector is wired up, which by
    design is not part of this orchestrator.
  * Persists ``daily_learning_state.json`` (in the private tracking
    directory) with the last refresh status and suppressed-repeat
    recommendation tracking, when that file is present.
  * Strictly avoids publishing any private IDs, tokens, raw review
    IDs, patient/member/prospect data, phone numbers, GCLIDs,
    personal emails, config paths, scheduler IDs, or raw connector
    payloads. Office labels that are already public (e.g. "Thousand
    Oaks") are allowed in aggregate context.

CLI:

    python3 scripts/refresh_marketing_dashboard.py            # fast + no-send (default)
    python3 scripts/refresh_marketing_dashboard.py --fast     # explicit
    python3 scripts/refresh_marketing_dashboard.py --no-send  # explicit
    python3 scripts/refresh_marketing_dashboard.py --private-dir /path/to/cron_tracking/<id>
    python3 scripts/refresh_marketing_dashboard.py --check    # validate only, no write

Exit code is non-zero only on a structural failure (snapshot
unreadable / unwritable / sanitization invariants violated).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_SNAPSHOT = REPO_ROOT / "data" / "snapshot.json"

# Optional companion module: HubSpot CMS optimizer. Imported lazily
# so the refresh still works in environments where the optimizer's
# private config / network is unavailable.
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import hubspot_cms_optimizer as _cms_optimizer  # type: ignore
except Exception:  # pragma: no cover - companion is best-effort
    _cms_optimizer = None  # type: ignore

DEFAULT_PRIVATE_DIR = Path("/home/user/workspace/cron_tracking/a3b9de2f")

# Sanitized inputs we will consider merging from the private dir.
# Each is keyed by the public snapshot section it can refresh.
SANITIZED_INPUTS = {
    "callrail_live": {
        "7d": "callrail_7d_sanitized.json",
        "30d": "callrail_30d_sanitized.json",
    },
}

# Patterns that must never appear in the public snapshot. The
# validator covers more, but the orchestrator double-checks the
# delta it writes itself to keep failures local.
FORBIDDEN_PATTERNS = [
    re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),  # email
    re.compile(r"\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b"),                    # US phone
    re.compile(r"\bGCLID\b", re.IGNORECASE),                            # GCLID label
    re.compile(r"AIza[0-9A-Za-z_\-]{10,}"),                             # google api key
    re.compile(r"ya29\.[0-9A-Za-z_\-]{10,}"),                           # google oauth
    re.compile(r"\b[0-9]{3}-[0-9]{3}-[0-9]{4}\b"),                     # ads cust id
]

OFFICE_LABEL_ALLOWLIST = {
    "Thousand Oaks", "Camarillo", "Ventura", "Oxnard", "Beverly Hills",
    "Santa Monica", "Sherman Oaks", "Encino", "Los Angeles",
}


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json_atomic(path: Path, payload: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def scan_forbidden(node: Any, path: str = "$") -> list[str]:
    """Walk a JSON-like structure and flag forbidden patterns."""
    issues: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            issues.extend(scan_forbidden(v, f"{path}.{k}"))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            issues.extend(scan_forbidden(v, f"{path}[{i}]"))
    elif isinstance(node, str):
        for pat in FORBIDDEN_PATTERNS:
            if pat.search(node):
                # Allow public office labels appearing alone.
                if any(label in node for label in OFFICE_LABEL_ALLOWLIST):
                    continue
                issues.append(f"forbidden pattern at {path}: {pat.pattern}")
    return issues


def merge_callrail(snapshot: dict, private_dir: Path, status: dict) -> None:
    src_7d = read_json(private_dir / SANITIZED_INPUTS["callrail_live"]["7d"], None)
    src_30d = read_json(private_dir / SANITIZED_INPUTS["callrail_live"]["30d"], None)
    if not (src_7d or src_30d):
        status["callrail"] = "pending: no sanitized snapshot on disk"
        return
    live = snapshot.setdefault("callrail_live", {})
    if src_7d:
        prev_7d = live.get("last_7_days", {}) if isinstance(live.get("last_7_days"), dict) else {}
        merged_7d = dict(prev_7d)
        for k in (
            "total_calls", "answered", "missed", "first_time_callers",
            "callrail_qualified",
        ):
            if k in src_7d:
                merged_7d[k] = src_7d[k]
        ans = merged_7d.get("answered")
        tot = merged_7d.get("total_calls")
        if isinstance(ans, (int, float)) and isinstance(tot, (int, float)) and tot:
            merged_7d["answer_rate_pct"] = round(ans / tot * 100, 1)
        ql = merged_7d.get("callrail_qualified")
        if isinstance(ql, (int, float)) and isinstance(tot, (int, float)) and tot:
            merged_7d["qualified_rate_pct"] = round(ql / tot * 100, 1)
        live["last_7_days"] = merged_7d
    if src_30d:
        prev_30d = live.get("last_30_days", {}) if isinstance(live.get("last_30_days"), dict) else {}
        merged_30d = dict(prev_30d)
        for k in (
            "total_calls", "answered", "missed", "first_time_callers",
            "callrail_qualified",
        ):
            if k in src_30d:
                merged_30d[k] = src_30d[k]
        ans = merged_30d.get("answered")
        tot = merged_30d.get("total_calls")
        if isinstance(ans, (int, float)) and isinstance(tot, (int, float)) and tot:
            merged_30d["answer_rate_pct"] = round(ans / tot * 100, 1)
        ql = merged_30d.get("callrail_qualified")
        if isinstance(ql, (int, float)) and isinstance(tot, (int, float)) and tot:
            merged_30d["qualified_rate_pct"] = round(ql / tot * 100, 1)
        live["last_30_days"] = merged_30d
    # use the most recent pulled_at, never the raw connector payload path
    refreshed = max(
        (s.get("pulled_at") for s in (src_7d, src_30d) if isinstance(s, dict) and s.get("pulled_at")),
        default=None,
    )
    if refreshed:
        live["refreshed_at"] = refreshed
    status["callrail"] = "ok: merged sanitized aggregate"


def update_routine_refresh_block(snapshot: dict, status: dict, mode: str) -> None:
    """Stamp a compact, public-safe refresh status block on the snapshot."""
    refresh = snapshot.setdefault("routine_refresh", {})
    refresh["last_run_at"] = utcnow_iso()
    refresh["last_run_date"] = today_iso()
    refresh["mode"] = mode
    refresh["sources"] = status
    # Mark sources without fresh inputs as stale/pending in a compact way.
    pending = sorted([k for k, v in status.items() if str(v).startswith("pending")])
    refresh["pending_sources"] = pending


def recommendation_hash(rec: dict) -> str:
    raw = json.dumps(rec, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def update_learning_state(
    private_dir: Path,
    status: dict,
    snapshot_summary: dict,
    new_recommendations: list[dict] | None = None,
) -> str | None:
    state_path = private_dir / "daily_learning_state.json"
    state = read_json(state_path, None)
    if state is None:
        return None  # nothing to update -- file is optional
    recs = new_recommendations or []
    mem = state.setdefault("recommendation_memory", {
        "active_recommendations": [],
        "completed_actions": [],
        "suppressed_repeated_recommendations": [],
        "experiments_running": [],
        "last_recommendation_hashes": [],
    })
    seen_hashes = set(mem.get("last_recommendation_hashes", []))
    kept = []
    suppressed = list(mem.get("suppressed_repeated_recommendations", []))
    for rec in recs:
        h = recommendation_hash(rec)
        if h in seen_hashes:
            suppressed.append({"hash": h, "suppressed_at": utcnow_iso()})
        else:
            kept.append(rec)
            seen_hashes.add(h)
    mem["last_recommendation_hashes"] = sorted(seen_hashes)[-100:]
    mem["suppressed_repeated_recommendations"] = suppressed[-100:]
    mem["active_recommendations"] = kept

    metric_mem = state.setdefault("metric_memory", {
        "last_snapshot_date": None,
        "previous_metrics": {},
        "material_changes": [],
    })
    metric_mem["last_snapshot_date"] = today_iso()
    metric_mem["previous_metrics"] = snapshot_summary

    last_run = state.setdefault("last_run", {})
    last_run.update({
        "ran_at": utcnow_iso(),
        "status": "ok" if not any(str(v).startswith("pending") for v in status.values()) else "partial",
        "source_status": status,
    })
    write_json_atomic(state_path, state)
    return str(state_path)


def summarize_snapshot(snapshot: dict) -> dict:
    k = snapshot.get("kpis", {})
    return {
        "latest_date": k.get("latest_date"),
        "total_sends": k.get("total_sends"),
        "reply_rate_pct": k.get("reply_rate_pct"),
        "positive_rate_pct": k.get("positive_rate_pct"),
        "bounces": k.get("bounces"),
    }


def merge_cms_actions(
    snapshot: dict,
    private_dir: Path,
    status: dict,
    *,
    apply_changes: bool,
    max_changes: int,
    check_only: bool,
) -> dict | None:
    """Run the HubSpot CMS optimizer and merge its sanitized block.

    Returns the result dict on success, or ``None`` when the
    optimizer is unavailable / config is absent. Never raises into
    the orchestrator's main path.
    """
    if _cms_optimizer is None:
        status["hubspot_cms"] = "pending: optimizer module unavailable"
        return None
    cfg_path = private_dir / "hubspot_cms_config.json"
    if not cfg_path.exists():
        status["hubspot_cms"] = "pending: hubspot_cms_config not present"
        return None
    try:
        result = _cms_optimizer.run(
            private_dir=private_dir,
            apply_changes=apply_changes and not check_only,
            max_changes=max_changes,
            cooldown_days=_cms_optimizer.DEFAULT_COOLDOWN_DAYS,
            snapshot=snapshot,
        )
    except Exception as e:
        status["hubspot_cms"] = f"error: {type(e).__name__}"
        return None
    block = _cms_optimizer.build_public_block(result, private_dir=private_dir)
    issues = _cms_optimizer.assert_public_sanitized(block)
    issues += scan_forbidden(block)
    if issues:
        status["hubspot_cms"] = "error: sanitization invariant violated; cms_actions dropped"
        return result
    snapshot["organic_cms_actions"] = block
    parts = [
        f"inventory={result['inventory']['site_pages']}sp/{result['inventory']['landing_pages']}lp",
        f"considered={result['candidates_considered']}",
        f"actions={len(result['actions'])}",
        f"live={result.get('live_writes', 0)}",
        f"draft={result.get('draft_writes', 0)}",
        f"proposed={result.get('proposals', 0)}",
        f"impact_samples={result.get('impact_samples_updated', 0)}",
    ]
    if result.get("live_writes"):
        mode_note = " (live-writeback)"
    elif result.get("draft_writes"):
        mode_note = " (draft-writeback)"
    elif result.get("writeback_performed"):
        mode_note = " (writeback)"
    else:
        mode_note = " (dry-run)"
    status["hubspot_cms"] = "ok: " + ", ".join(parts) + mode_note
    return result


def refresh(
    private_dir: Path,
    fast: bool,
    no_send: bool,
    check_only: bool,
    *,
    cms_apply: bool = True,
    cms_max_changes: int = 3,
) -> int:
    if not PUBLIC_SNAPSHOT.exists():
        print(f"ERROR: missing public snapshot at {PUBLIC_SNAPSHOT}", file=sys.stderr)
        return 2

    snapshot = read_json(PUBLIC_SNAPSHOT, None)
    if not isinstance(snapshot, dict):
        print("ERROR: snapshot.json did not parse as a JSON object", file=sys.stderr)
        return 2

    status: dict[str, str] = {}
    mode_label = "fast" if fast else "full"

    if private_dir.exists():
        merge_callrail(snapshot, private_dir, status)
        merge_cms_actions(
            snapshot,
            private_dir,
            status,
            apply_changes=cms_apply,
            max_changes=cms_max_changes,
            check_only=check_only,
        )
    else:
        status["private_dir"] = "pending: tracking directory not present"

    # Outbound is always disabled here. We do not stage or send anything.
    if not no_send:
        # Still refuse: this orchestrator is intentionally not wired
        # for sending. The --no-send flag is the default and required
        # for correctness; we ignore attempts to disable it.
        status["outbound"] = "disabled: orchestrator does not stage or send outreach"
    else:
        status["outbound"] = "disabled: --no-send (default)"

    # Routine refresh stamp.
    snapshot["generated_at"] = utcnow_iso()
    update_routine_refresh_block(snapshot, status, mode_label)

    # Final sanitization safety check on the routine_refresh block we wrote.
    issues = scan_forbidden(snapshot.get("routine_refresh", {}))
    issues += scan_forbidden(snapshot.get("callrail_live", {}))
    issues += scan_forbidden(snapshot.get("organic_cms_actions", {}))
    if issues:
        print("ERROR: refresh would publish forbidden patterns:", file=sys.stderr)
        for i in issues:
            print("  -", i, file=sys.stderr)
        return 3

    learning_path = update_learning_state(
        private_dir,
        status,
        summarize_snapshot(snapshot),
        new_recommendations=[],
    )

    if check_only:
        print("CHECK: refresh dry-run; no files written.")
        print(json.dumps({"status": status, "learning_state": learning_path}, indent=2))
        return 0

    write_json_atomic(PUBLIC_SNAPSHOT, snapshot)
    print("OK: refreshed", PUBLIC_SNAPSHOT)
    print(json.dumps({"mode": mode_label, "status": status, "learning_state": learning_path}, indent=2))
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Refresh the public Clove patient-acquisition dashboard snapshot.")
    p.add_argument("--fast", action="store_true", default=True, help="Fast mode (default): no live connector calls; merge sanitized snapshots only.")
    p.add_argument("--full", dest="fast", action="store_false", help="Allow heavier merges (still no outbound, still no raw payload publish).")
    p.add_argument("--no-send", action="store_true", default=True, help="Disable outbound outreach (default and effectively required).")
    p.add_argument("--allow-send", dest="no_send", action="store_false", help="Attempt to enable outbound; orchestrator still refuses and logs.")
    p.add_argument("--private-dir", default=str(DEFAULT_PRIVATE_DIR), help="Path to the private cron tracking directory.")
    p.add_argument("--check", action="store_true", help="Validate inputs and exit without writing snapshot.json.")
    p.add_argument("--cms-apply", action="store_true", default=True, help="Allow HubSpot CMS low-risk metadata writeback if config permits (default).")
    p.add_argument("--cms-dry-run", dest="cms_apply", action="store_false", help="Force HubSpot CMS step to dry-run regardless of config.")
    p.add_argument("--cms-max-changes", type=int, default=3, help="Cap number of CMS metadata changes per run (default 3).")
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    private_dir = Path(args.private_dir)
    return refresh(
        private_dir=private_dir,
        fast=args.fast,
        no_send=args.no_send,
        check_only=args.check,
        cms_apply=args.cms_apply,
        cms_max_changes=args.cms_max_changes,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
