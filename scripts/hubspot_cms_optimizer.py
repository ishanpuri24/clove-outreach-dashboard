#!/usr/bin/env python3
"""HubSpot CMS metadata optimizer (low-risk, approval-policy controlled).

Companion to ``scripts/refresh_marketing_dashboard.py``. Reads a
private HubSpot CMS config (token + safety tiers), pulls a minimal
CMS inventory (one inventory pull per run), combines with the GSC
query/page rows that already live in the public snapshot (and any
sanitized GA4 rows the orchestrator merged), and proposes up to
``--max-changes`` (default 3) low-risk metadata changes per run.

Safety model
------------
The private config (``hubspot_cms_config.json``) declares a
``publish_mode`` and ``safety_tiers``. Only changes that fall in
``safety_tiers.auto_allowed`` are eligible to be written
automatically; everything else stays as a dry-run proposal regardless
of CLI flags.

  * ``publish_mode = "low_risk_metadata_writeback_allowed"`` →
    title/meta description updates eligible for live writeback.
  * Any other ``publish_mode`` (or missing) → dry-run only.

This script never:

  * publishes new pages, body content, template/theme, CTA, form,
    redirect, or script/source-code changes;
  * touches domains, functions, billing/users/oauth, transactional
    email;
  * writes anything to the public snapshot that could leak HubSpot
    private IDs, portal IDs, the token, the config path, or raw API
    payloads.

Output
------
On each run the optimizer:

  1. Returns a sanitized ``cms_actions`` block suitable for merging
     into the public snapshot (the orchestrator does the merge).
  2. Updates the private ``daily_learning_state.json`` with
     ``cms_experiments`` entries: hypothesis, page (public-safe slug
     and title), change type, date applied, baseline metric,
     metric-to-watch, status. Repeats to the same page within a
     cooldown window are suppressed.

Usage (called by orchestrator; also runnable standalone for ops):

    python3 scripts/hubspot_cms_optimizer.py --check       # dry-run only
    python3 scripts/hubspot_cms_optimizer.py --apply       # apply low-risk drafts
    python3 scripts/hubspot_cms_optimizer.py --max-changes 1
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_SNAPSHOT = REPO_ROOT / "data" / "snapshot.json"

DEFAULT_PRIVATE_DIR = Path("/home/user/workspace/cron_tracking/a3b9de2f")
DEFAULT_CONFIG_NAME = "hubspot_cms_config.json"
DEFAULT_STATE_NAME = "daily_learning_state.json"
DEFAULT_COOLDOWN_DAYS = 14

# Auto-applicable change types. The config must also list the type
# under safety_tiers.auto_allowed AND publish_mode must allow writes.
AUTO_CHANGE_TYPES = {
    "missing_or_weak_title_update",
    "missing_or_weak_meta_description_update",
}

# Heuristic thresholds for "weak" / "missing" metadata.
WEAK_TITLE_MAX_LEN = 25
WEAK_META_MAX_LEN = 60
RECOMMENDED_TITLE_MAX = 60
RECOMMENDED_META_MAX = 155

# Heuristic for "high impressions / low CTR" candidate selection.
MIN_IMPRESSIONS_FOR_CTR_LEAK = 1000
MAX_CTR_PCT_FOR_LEAK = 1.5

HUBSPOT_API_BASE = "https://api.hubapi.com"

# Tokens / IDs / payload shapes the sanitizer scrubs on the way out.
PRIVATE_KEY_HINTS = (
    "token", "access_token", "refresh_token", "portal_id", "hub_id",
    "id", "objectId", "object_id", "internal_id", "_id",
    "archivedAt", "currentlyPublished", "publishDate",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    return utcnow().isoformat()


def today_iso() -> str:
    return utcnow().date().isoformat()


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


def _public_slug(url_or_slug: str) -> str:
    """Reduce a CMS URL or slug to a public, path-only slug."""
    if not isinstance(url_or_slug, str) or not url_or_slug:
        return ""
    s = url_or_slug.strip()
    # If it's a full URL, keep only the path.
    m = re.match(r"https?://[^/]+(/.*)?$", s)
    if m:
        s = m.group(1) or "/"
    if not s.startswith("/"):
        s = "/" + s
    # Hard-cap and strip query/fragment.
    s = s.split("?", 1)[0].split("#", 1)[0]
    return s[:120]


def _strip_private_keys(node: Any) -> Any:
    """Defensive scrub: never let private HubSpot keys/payload escape."""
    if isinstance(node, dict):
        return {
            k: _strip_private_keys(v)
            for k, v in node.items()
            if k not in PRIVATE_KEY_HINTS
        }
    if isinstance(node, list):
        return [_strip_private_keys(v) for v in node]
    return node


# ---------------------------------------------------------------------------
# HubSpot API helpers
# ---------------------------------------------------------------------------

class HubSpotClient:
    """Very small HubSpot CMS client.

    Only the endpoints we need are wrapped. Read-only by default;
    the write path is gated behind ``allow_write=True`` AND the
    caller passing an explicit change type in ``AUTO_CHANGE_TYPES``.
    """

    def __init__(self, token: str, *, timeout: int = 20):
        self._token = token
        self._timeout = timeout

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        url = HUBSPOT_API_BASE + path
        data = None
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
                if not raw:
                    return {}
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                body_txt = e.read().decode("utf-8", errors="replace")
            except Exception:
                body_txt = ""
            # Never echo the URL (it may include private filters) and
            # never the body (HubSpot error payloads can echo IDs).
            raise RuntimeError(
                f"HubSpot HTTP {e.code} on {method} {path.split('?',1)[0]}: "
                f"{body_txt[:200]}"
            ) from None
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"HubSpot transport error on {method} {path.split('?',1)[0]}: {e.reason}"
            ) from None

    def list_site_pages(self, *, limit: int = 50) -> list[dict]:
        path = f"/cms/v3/pages/site-pages?limit={limit}&archived=false"
        out = self._request("GET", path)
        return list(out.get("results", []))

    def list_landing_pages(self, *, limit: int = 50) -> list[dict]:
        path = f"/cms/v3/pages/landing-pages?limit={limit}&archived=false"
        out = self._request("GET", path)
        return list(out.get("results", []))

    def update_site_page_metadata_draft(
        self, page_id: str, *, html_title: str | None, meta_description: str | None
    ) -> dict:
        body: dict = {}
        if html_title is not None:
            body["htmlTitle"] = html_title
        if meta_description is not None:
            body["metaDescription"] = meta_description
        if not body:
            return {"skipped": "no metadata fields supplied"}
        # PATCH on the buffer (draft) endpoint -- does NOT publish.
        path = f"/cms/v3/pages/site-pages/{urllib.parse.quote(page_id)}/draft"
        return self._request("PATCH", path, body=body)


# ---------------------------------------------------------------------------
# Candidate selection
# ---------------------------------------------------------------------------

def _is_weak_title(t: str | None) -> bool:
    if not t or not isinstance(t, str):
        return True
    t = t.strip()
    if len(t) < WEAK_TITLE_MAX_LEN:
        return True
    return False


def _is_weak_meta(m: str | None) -> bool:
    if not m or not isinstance(m, str):
        return True
    m = m.strip()
    if len(m) < WEAK_META_MAX_LEN:
        return True
    return False


def _normalize_page(p: dict) -> dict:
    """Pull the small handful of fields we use; drop everything else."""
    return {
        "_id": p.get("id"),  # PRIVATE; never written to public output.
        "url": p.get("url") or "",
        "slug": p.get("slug") or "",
        "name": p.get("name") or "",
        "html_title": p.get("htmlTitle") or "",
        "meta_description": p.get("metaDescription") or "",
        "type": p.get("_object_type", "site_page"),
    }


def _public_page_label(np: dict) -> str:
    """A public-safe label for a HubSpot page. Slug or name only."""
    slug = _public_slug(np.get("url") or ("/" + (np.get("slug") or "")))
    title = (np.get("name") or "").strip()
    if title and len(title) <= 80:
        return f"{slug} — {title}"
    return slug or title or "(unnamed page)"


def _gsc_index(snapshot: dict) -> dict[str, list[dict]]:
    """Index GSC query rows by candidate page path tokens.

    The public snapshot has both ``gsc_query_rows`` and
    ``gsc_page_rows``. We use page rows directly when present, and
    fall back to query rows where the action text references a page
    family (e.g. "insurance page", "blog/mouth-sloughing").
    """
    oi = snapshot.get("organic_insights") or {}
    rows: dict[str, list[dict]] = {"pages": [], "queries": []}
    for r in oi.get("gsc_page_rows") or []:
        if isinstance(r, dict) and r.get("page"):
            rows["pages"].append(r)
    for r in oi.get("gsc_query_rows") or []:
        if isinstance(r, dict) and r.get("query"):
            rows["queries"].append(r)
    return rows


def _gsc_signal_for_page(np: dict, gsc: dict[str, list[dict]]) -> dict | None:
    """Return the strongest GSC row whose page path matches this page.

    Strict path-token matching: we never echo the HubSpot id, only
    the public slug. ``None`` means "no clear GSC justification" and
    the optimizer will skip the candidate.
    """
    slug = _public_slug(np.get("url") or ("/" + (np.get("slug") or "")))
    if not slug or slug == "/":
        # The homepage match is intentionally not auto-touched: it's
        # too high-blast-radius for the first version.
        return None
    tokens = [t for t in slug.strip("/").split("/") if t]
    if not tokens:
        return None
    leaf = tokens[-1]
    for r in gsc["pages"]:
        page = (r.get("page") or "").lower()
        if leaf and leaf.lower() in page:
            if (
                isinstance(r.get("impressions"), (int, float))
                and r["impressions"] >= MIN_IMPRESSIONS_FOR_CTR_LEAK
                and isinstance(r.get("ctr_pct"), (int, float))
                and r["ctr_pct"] <= MAX_CTR_PCT_FOR_LEAK
            ):
                return {
                    "match": "page_row",
                    "page": _public_slug(r.get("page") or ""),
                    "clicks": r.get("clicks"),
                    "impressions": r.get("impressions"),
                    "ctr_pct": r.get("ctr_pct"),
                    "avg_position": r.get("avg_position"),
                }
    # Fall back to query rows whose action text references the slug leaf.
    for r in gsc["queries"]:
        action = (r.get("action") or "").lower()
        query = (r.get("query") or "").lower()
        if leaf and (leaf.lower() in action or leaf.lower() in query):
            if (
                isinstance(r.get("impressions"), (int, float))
                and r["impressions"] >= MIN_IMPRESSIONS_FOR_CTR_LEAK
                and isinstance(r.get("ctr_pct"), (int, float))
                and r["ctr_pct"] <= MAX_CTR_PCT_FOR_LEAK
            ):
                return {
                    "match": "query_row",
                    "query": r.get("query"),
                    "clicks": r.get("clicks"),
                    "impressions": r.get("impressions"),
                    "ctr_pct": r.get("ctr_pct"),
                    "avg_position": r.get("avg_position"),
                }
    return None


def _draft_title(np: dict, signal: dict) -> str:
    """Conservative title draft: keeps brand, surfaces query intent."""
    current = (np.get("html_title") or np.get("name") or "").strip()
    keyword_source = signal.get("query") or signal.get("page") or ""
    keyword = (str(keyword_source).strip().rstrip("/").split("/")[-1] or "").replace("-", " ")
    keyword = keyword.strip().title()
    brand = "Clove Dental"
    if current and keyword and keyword.lower() not in current.lower():
        proposal = f"{keyword} | {brand}"
    elif keyword:
        proposal = f"{keyword} | {brand}"
    else:
        proposal = (current or brand)[:RECOMMENDED_TITLE_MAX]
    return proposal[:RECOMMENDED_TITLE_MAX]


def _draft_meta(np: dict, signal: dict) -> str:
    """Conservative meta draft: factual, no claims, no prices."""
    keyword_source = signal.get("query") or signal.get("page") or ""
    keyword = (str(keyword_source).strip().rstrip("/").split("/")[-1] or "").replace("-", " ")
    keyword = keyword.strip()
    base = (
        f"Learn more about {keyword} at Clove Dental. Care across the "
        f"Conejo Valley. Schedule a consultation to discuss your options."
    ) if keyword else (
        "Clove Dental serves the Conejo Valley with patient-focused care. "
        "Schedule a consultation to discuss your options."
    )
    return base[:RECOMMENDED_META_MAX]


def _build_candidate(np: dict, signal: dict) -> dict | None:
    """Return an internal candidate record (still has private ``_id``).

    The sanitizer downstream strips ``_id`` before anything is
    written to the public snapshot.
    """
    weak_title = _is_weak_title(np.get("html_title"))
    weak_meta = _is_weak_meta(np.get("meta_description"))
    if not (weak_title or weak_meta):
        return None
    fields: dict[str, str] = {}
    change_types: list[str] = []
    if weak_title:
        fields["html_title"] = _draft_title(np, signal)
        change_types.append("missing_or_weak_title_update")
    if weak_meta:
        fields["meta_description"] = _draft_meta(np, signal)
        change_types.append("missing_or_weak_meta_description_update")
    return {
        "_id": np.get("_id"),  # PRIVATE
        "page_label": _public_page_label(np),
        "slug": _public_slug(np.get("url") or ("/" + (np.get("slug") or ""))),
        "type": np.get("type", "site_page"),
        "current_title_len": len((np.get("html_title") or "")),
        "current_meta_len": len((np.get("meta_description") or "")),
        "proposed_fields": fields,
        "change_types": change_types,
        "signal": signal,
    }


def _candidate_cooldown_active(
    state: dict, slug: str, cooldown_days: int
) -> bool:
    cms = state.get("cms_experiments") or {}
    log = cms.get("log") or []
    cutoff = utcnow() - timedelta(days=cooldown_days)
    for row in log:
        if not isinstance(row, dict):
            continue
        if row.get("slug") != slug:
            continue
        applied = row.get("applied_at")
        if not applied:
            continue
        try:
            ts = datetime.fromisoformat(applied.replace("Z", "+00:00"))
        except Exception:
            continue
        if ts > cutoff:
            return True
    return False


# ---------------------------------------------------------------------------
# Sanitization invariants
# ---------------------------------------------------------------------------

FORBIDDEN_IN_PUBLIC = [
    re.compile(r"\bpat-na1-[0-9a-f-]{8,}\b", re.IGNORECASE),  # HubSpot PAT
    re.compile(r"\bhubspot[_-]?(?:portal|hub)[_-]?id\b", re.IGNORECASE),
    re.compile(r"/cron_tracking/"),
    re.compile(r"hubspot_cms_config\.json"),
    # 32+ hex strings (HubSpot object ids are 8-12 digits, but we also
    # block any longer hex-looking blob just in case).
    re.compile(r"\b[0-9a-f]{32,}\b"),
]


def assert_public_sanitized(node: Any) -> list[str]:
    issues: list[str] = []
    try:
        text = json.dumps(node, ensure_ascii=False)
    except Exception:
        return ["cms_actions block is not JSON serializable"]
    for pat in FORBIDDEN_IN_PUBLIC:
        m = pat.search(text)
        if m:
            issues.append(f"forbidden pattern in cms_actions: {pat.pattern}")
    return issues


def _public_action_row(cand: dict, status: str, note: str = "") -> dict:
    sig = cand.get("signal") or {}
    metric_to_watch = []
    if isinstance(sig.get("ctr_pct"), (int, float)):
        metric_to_watch.append(f"CTR {sig['ctr_pct']}%")
    if isinstance(sig.get("clicks"), (int, float)):
        metric_to_watch.append(f"clicks {sig['clicks']}/28d")
    if isinstance(sig.get("impressions"), (int, float)):
        metric_to_watch.append(f"impressions {sig['impressions']}/28d")
    why_bits = []
    if "missing_or_weak_title_update" in cand.get("change_types", []):
        why_bits.append(f"title len {cand.get('current_title_len')}")
    if "missing_or_weak_meta_description_update" in cand.get("change_types", []):
        why_bits.append(f"meta len {cand.get('current_meta_len')}")
    row = {
        "page": cand.get("page_label"),
        "slug": cand.get("slug"),
        "change": ", ".join(cand.get("change_types") or []),
        "why": "; ".join(why_bits) or "weak metadata",
        "status": status,
        "metric_to_watch": "; ".join(metric_to_watch) or "CTR / clicks",
    }
    if note:
        row["note"] = note
    return row


# ---------------------------------------------------------------------------
# Core run
# ---------------------------------------------------------------------------

def _load_config(config_path: Path) -> dict:
    cfg = read_json(config_path, None)
    if not isinstance(cfg, dict):
        raise RuntimeError(f"hubspot_cms_config not found or unreadable at {config_path.name}")
    if not cfg.get("token"):
        raise RuntimeError("hubspot_cms_config: missing token")
    return cfg


def _eligible_to_write(cfg: dict, change_types: list[str]) -> bool:
    if cfg.get("publish_mode") != "low_risk_metadata_writeback_allowed":
        return False
    tiers = cfg.get("safety_tiers") or {}
    auto_allowed = set(tiers.get("auto_allowed") or [])
    for ct in change_types:
        if ct not in AUTO_CHANGE_TYPES:
            return False
        if ct not in auto_allowed:
            return False
    return True


def run(
    *,
    private_dir: Path,
    apply_changes: bool,
    max_changes: int,
    cooldown_days: int,
    snapshot: dict,
) -> dict:
    """Return a result dict the orchestrator can act on."""
    out: dict = {
        "ran_at": utcnow_iso(),
        "mode": "apply" if apply_changes else "check",
        "inventory": {"site_pages": 0, "landing_pages": 0},
        "candidates_considered": 0,
        "actions": [],          # sanitized, public-safe rows
        "written": [],          # sanitized, public-safe rows
        "errors": [],
        "publish_mode": None,
        "writeback_performed": False,
    }

    cfg_path = private_dir / DEFAULT_CONFIG_NAME
    try:
        cfg = _load_config(cfg_path)
    except Exception as e:
        out["errors"].append(f"config: {e}")
        return out
    out["publish_mode"] = cfg.get("publish_mode")

    client = HubSpotClient(cfg["token"])
    pages: list[dict] = []
    try:
        site_pages = client.list_site_pages(limit=50)
        out["inventory"]["site_pages"] = len(site_pages)
        for p in site_pages:
            np = _normalize_page(p)
            np["type"] = "site_page"
            pages.append(np)
    except Exception as e:
        out["errors"].append(f"site_pages: {e}")
    try:
        landing_pages = client.list_landing_pages(limit=50)
        out["inventory"]["landing_pages"] = len(landing_pages)
        for p in landing_pages:
            np = _normalize_page(p)
            np["type"] = "landing_page"
            pages.append(np)
    except Exception as e:
        out["errors"].append(f"landing_pages: {e}")

    gsc = _gsc_index(snapshot)
    state = read_json(private_dir / DEFAULT_STATE_NAME, {}) or {}
    cms_state = state.setdefault("cms_experiments", {
        "version": 1,
        "log": [],
        "cooldown_days": cooldown_days,
    })

    candidates: list[dict] = []
    for np in pages:
        signal = _gsc_signal_for_page(np, gsc)
        if not signal:
            continue
        out["candidates_considered"] += 1
        slug = _public_slug(np.get("url") or ("/" + (np.get("slug") or "")))
        if _candidate_cooldown_active(state, slug, cooldown_days):
            continue
        cand = _build_candidate(np, signal)
        if not cand:
            continue
        candidates.append(cand)
        if len(candidates) >= max_changes:
            break

    for cand in candidates:
        eligible = _eligible_to_write(cfg, cand.get("change_types", []))
        if apply_changes and eligible and cand["type"] == "site_page":
            try:
                client.update_site_page_metadata_draft(
                    cand["_id"],
                    html_title=cand["proposed_fields"].get("html_title"),
                    meta_description=cand["proposed_fields"].get("meta_description"),
                )
                row = _public_action_row(cand, status="applied_draft", note="HubSpot draft (unpublished)")
                out["written"].append(row)
                out["actions"].append(row)
                out["writeback_performed"] = True
                cms_state["log"].append({
                    "applied_at": utcnow_iso(),
                    "slug": cand["slug"],
                    "page_label": cand["page_label"],
                    "change_types": cand["change_types"],
                    "hypothesis": (
                        "Strengthening title/meta on a high-impression, "
                        "low-CTR page should lift CTR and clicks."
                    ),
                    "baseline": cand.get("signal"),
                    "metric_to_watch": "ctr_pct, clicks (28d)",
                    "status": "draft_saved_unpublished",
                })
            except Exception as e:
                row = _public_action_row(cand, status="error", note=f"writeback failed: {type(e).__name__}")
                out["actions"].append(row)
                out["errors"].append(f"writeback: {type(e).__name__}")
        else:
            reason = "dry-run" if not apply_changes else (
                "publish_mode does not permit writeback" if not eligible
                else "change type not auto-allowed for this page type"
            )
            row = _public_action_row(cand, status="proposed", note=reason)
            out["actions"].append(row)
            cms_state["log"].append({
                "applied_at": utcnow_iso(),
                "slug": cand["slug"],
                "page_label": cand["page_label"],
                "change_types": cand["change_types"],
                "hypothesis": (
                    "Strengthening title/meta on a high-impression, "
                    "low-CTR page should lift CTR and clicks."
                ),
                "baseline": cand.get("signal"),
                "metric_to_watch": "ctr_pct, clicks (28d)",
                "status": "proposed_not_applied",
            })

    # Persist learning state.
    try:
        write_json_atomic(private_dir / DEFAULT_STATE_NAME, state)
    except Exception as e:
        out["errors"].append(f"learning_state: {e}")

    # Last-mile sanitization. We never publish ``_id`` or any private
    # blob. The strip is defense in depth -- the rows we appended are
    # already constructed from public-safe fields only.
    out["actions"] = _strip_private_keys(out["actions"])
    out["written"] = _strip_private_keys(out["written"])

    issues = assert_public_sanitized(out["actions"]) + assert_public_sanitized(out["written"])
    if issues:
        out["errors"].extend(issues)
        out["actions"] = []
        out["written"] = []

    return out


def build_public_block(result: dict) -> dict:
    """Construct the ``organic_cms_actions`` block for the snapshot."""
    actions = result.get("actions") or []
    return {
        "title": "Organic / CMS metadata action log",
        "last_run_at": result.get("ran_at"),
        "mode": result.get("mode"),
        "publish_mode": result.get("publish_mode"),
        "writeback_performed": bool(result.get("writeback_performed")),
        "summary": (
            f"{len(actions)} CMS metadata action(s) considered; "
            f"{len(result.get('written') or [])} applied as draft."
        ),
        "actions": actions,
        "errors": [
            e for e in (result.get("errors") or [])
            if "config" not in e.lower()  # do not leak config name
        ][:5],
    }


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="HubSpot CMS metadata optimizer (low-risk, approval-policy controlled).",
    )
    p.add_argument("--private-dir", default=str(DEFAULT_PRIVATE_DIR))
    p.add_argument("--check", action="store_true",
                   help="Dry-run only: propose changes, write nothing to HubSpot.")
    p.add_argument("--apply", action="store_true",
                   help="Apply low-risk metadata changes as drafts where eligible.")
    p.add_argument("--max-changes", type=int, default=3)
    p.add_argument("--cooldown-days", type=int, default=DEFAULT_COOLDOWN_DAYS)
    p.add_argument("--snapshot", default=str(PUBLIC_SNAPSHOT))
    p.add_argument("--write-block", default=None,
                   help="If set, also writes the sanitized cms_actions block to this JSON file.")
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    apply_changes = args.apply and not args.check
    private_dir = Path(args.private_dir)
    snapshot_path = Path(args.snapshot)
    snapshot = read_json(snapshot_path, {}) or {}
    if not private_dir.exists():
        print(f"ERROR: private dir missing: {private_dir.name}", file=sys.stderr)
        return 2
    try:
        result = run(
            private_dir=private_dir,
            apply_changes=apply_changes,
            max_changes=max(0, int(args.max_changes)),
            cooldown_days=max(1, int(args.cooldown_days)),
            snapshot=snapshot,
        )
    except Exception as e:
        print(f"ERROR: optimizer failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 3
    block = build_public_block(result)
    if args.write_block:
        write_json_atomic(Path(args.write_block), block)
    print(json.dumps({
        "mode": result["mode"],
        "inventory": result["inventory"],
        "candidates_considered": result["candidates_considered"],
        "actions": len(result["actions"]),
        "written": len(result["written"]),
        "writeback_performed": result["writeback_performed"],
        "errors": result["errors"],
    }, indent=2))
    return 0 if not result.get("errors") else 0  # non-fatal errors do not fail run


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
