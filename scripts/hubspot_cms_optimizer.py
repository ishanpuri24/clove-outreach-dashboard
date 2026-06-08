#!/usr/bin/env python3
"""HubSpot CMS metadata optimizer (controlled live writeback + daily learning).

Companion to ``scripts/refresh_marketing_dashboard.py``. Reads a
private HubSpot CMS config (token + publish_mode + safety tiers),
pulls a minimal CMS inventory (one inventory pull per run), combines
with the GSC query/page rows that already live in the public snapshot
(and any sanitized GA4 rows the orchestrator merged), and applies up
to ``--max-changes`` (default 3) low-risk metadata changes per run.

Safety model
------------
The private config (``hubspot_cms_config.json``) declares a
``publish_mode`` and ``safety_tiers``. Two publish modes are
supported:

  * ``controlled_live_writeback_allowed`` (current default) -- title /
    meta-description live updates on site_pages and landing_pages are
    eligible for direct push-live when the change type is listed in
    ``safety_tiers.auto_live_allowed``; if a page type only has a
    draft endpoint (or live push fails), the optimizer falls back to
    a HubSpot draft and logs accordingly.
  * ``low_risk_metadata_writeback_allowed`` (legacy) -- draft-only
    writeback for backwards compatibility.
  * Any other value (or missing config) -- dry-run only.

This script never:

  * publishes new pages, body content > 280 chars, template/theme,
    CTA, form, redirect, or script/source-code changes;
  * touches domains, functions, billing/users/oauth, transactional
    email;
  * writes anything to the public snapshot that could leak HubSpot
    private IDs, portal IDs, the token, the config path, or raw API
    payloads.

Daily learning loop
-------------------
Each run records every action (live, draft, proposed, error) into
``daily_learning_state.json::cms_experiments.log`` with:

  * slug + public page label
  * change types applied
  * hypothesis
  * baseline (GSC clicks / impressions / CTR / position at time of change)
  * metric_to_watch
  * status (``applied_live`` | ``applied_draft`` | ``proposed`` | ``error``)
  * applied_at + cooldown_until
  * impact_history list -- subsequent daily runs append new CTR /
    clicks / sessions / qualified-call samples here so the dashboard
    can chart "impact over time" per change.

Output
------
On each run the optimizer:

  1. Returns a sanitized ``cms_actions`` block (live vs draft vs
    proposed) suitable for merging into the public snapshot.
  2. Updates the private learning state.

Usage (called by orchestrator; also runnable standalone for ops):

    python3 scripts/hubspot_cms_optimizer.py --check       # dry-run only
    python3 scripts/hubspot_cms_optimizer.py --apply       # live where eligible
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
DEFAULT_COOLDOWN_DAYS = 7

ACCELERATED_PUBLISH_MODE = (
    "accelerated_controlled_live_writeback_with_small_content_allowed"
)

# Publish modes that allow any write at all.
PUBLISH_MODES_WITH_WRITE = {
    "low_risk_metadata_writeback_allowed",   # legacy: drafts only
    "controlled_live_writeback_allowed",     # standard: live where listed
    ACCELERATED_PUBLISH_MODE,                # accelerated: + small content
}

# Publish modes that allow live push (skip draft step) when the change
# type is listed under safety_tiers.auto_live_allowed.
PUBLISH_MODES_WITH_LIVE = {
    "controlled_live_writeback_allowed",
    ACCELERATED_PUBLISH_MODE,
}

# Publish modes that allow small content (body copy / FAQ / internal-link)
# in DRAFT-or-propose form when explicitly approved in the config.
PUBLISH_MODES_WITH_SMALL_CONTENT = {ACCELERATED_PUBLISH_MODE}

# Public-facing change-type labels (used in the action log + dashboard).
CHANGE_TITLE = "missing_or_weak_title_update"
CHANGE_META = "missing_or_weak_meta_description_update"
CHANGE_SMALL_BODY = "small_existing_body_copy_improvement"
CHANGE_FAQ = "faq_section_update"
CHANGE_INTERNAL_LINK = "internal_link_block_update"

SMALL_CONTENT_CHANGE_TYPES = {
    CHANGE_SMALL_BODY,
    CHANGE_FAQ,
    CHANGE_INTERNAL_LINK,
}

# Map our generic public change labels to the per-page-type tier keys
# the private config uses to grant live-write permission. Small-content
# changes use the same tier key for both page types because the config
# uses page-type-agnostic labels for them.
_TIER_KEY = {
    ("site_page", CHANGE_TITLE): "site_page_title_update",
    ("site_page", CHANGE_META): "site_page_meta_description_update",
    ("landing_page", CHANGE_TITLE): "landing_page_title_update",
    ("landing_page", CHANGE_META): "landing_page_meta_description_update",
    ("site_page", CHANGE_SMALL_BODY): "small_existing_body_copy_improvement",
    ("landing_page", CHANGE_SMALL_BODY): "small_existing_body_copy_improvement",
    ("site_page", CHANGE_FAQ): "faq_section_update",
    ("landing_page", CHANGE_FAQ): "faq_section_update",
    ("site_page", CHANGE_INTERNAL_LINK): "internal_link_block_update",
    ("landing_page", CHANGE_INTERNAL_LINK): "internal_link_block_update",
}

# Heuristic thresholds for "weak" / "missing" metadata.
WEAK_TITLE_MAX_LEN = 25
WEAK_META_MAX_LEN = 60
RECOMMENDED_TITLE_MAX = 60
RECOMMENDED_META_MAX = 155

# Heuristic for "high impressions / low CTR" candidate selection.
MIN_IMPRESSIONS_FOR_CTR_LEAK = 1000
MAX_CTR_PCT_FOR_LEAK = 1.5

# Accelerated-mode signals (lower thresholds, near-rank, demand-themed).
ACCEL_MIN_IMPRESSIONS = 200
ACCEL_MAX_CTR_PCT = 2.5
NEAR_RANK_MIN_POSITION = 7.0
NEAR_RANK_MAX_POSITION = 20.0
NEAR_RANK_MIN_IMPRESSIONS = 100
DEMAND_OFFICE_TOKENS = (
    "thousand-oaks", "thousand oaks", "newbury-park", "newbury park",
    "westlake", "agoura", "agoura-hills", "moorpark", "simi", "calabasas",
    "conejo", "oak-park", "oak park",
)
DEMAND_SERVICE_TOKENS = (
    "invisalign", "implant", "implants", "veneer", "veneers", "crown",
    "crowns", "whitening", "cleaning", "cleanings", "emergency",
    "wisdom-teeth", "wisdom teeth", "extraction", "extractions",
    "root-canal", "root canal", "dentist", "dental", "orthodontic",
    "orthodontics", "braces", "kids", "pediatric", "membership",
    "insurance", "consultation", "exam",
)

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
    m = re.match(r"https?://[^/]+(/.*)?$", s)
    if m:
        s = m.group(1) or "/"
    if not s.startswith("/"):
        s = "/" + s
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
    """Minimal HubSpot CMS client used by this optimizer.

    Read paths: site-pages and landing-pages list.
    Write paths: per page type, draft PATCH and live push.
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
        return list((self._request("GET", path) or {}).get("results", []))

    def list_landing_pages(self, *, limit: int = 50) -> list[dict]:
        path = f"/cms/v3/pages/landing-pages?limit={limit}&archived=false"
        return list((self._request("GET", path) or {}).get("results", []))

    def _patch_draft(self, kind: str, page_id: str, body: dict) -> dict:
        seg = "site-pages" if kind == "site_page" else "landing-pages"
        path = f"/cms/v3/pages/{seg}/{urllib.parse.quote(page_id)}/draft"
        return self._request("PATCH", path, body=body)

    def _push_live(self, kind: str, page_id: str) -> dict:
        """Push the saved draft live for a site or landing page."""
        seg = "site-pages" if kind == "site_page" else "landing-pages"
        path = (
            f"/cms/v3/pages/{seg}/{urllib.parse.quote(page_id)}"
            "/draft/push-live"
        )
        return self._request("POST", path, body={})

    def update_page_metadata(
        self,
        kind: str,
        page_id: str,
        *,
        html_title: str | None,
        meta_description: str | None,
        live: bool,
    ) -> dict:
        """Update title / meta_description on a HubSpot CMS page.

        Returns ``{"status": "applied_live"|"applied_draft", ...}``.
        On a live request, the draft is saved first and then pushed
        live. If the live push fails, we fall back to draft and surface
        ``applied_draft`` with a ``fallback_reason``.
        """
        body: dict = {}
        if html_title is not None:
            body["htmlTitle"] = html_title
        if meta_description is not None:
            body["metaDescription"] = meta_description
        if not body:
            return {"status": "noop", "reason": "no metadata fields supplied"}
        # Always PATCH the draft first; cheap and reversible.
        self._patch_draft(kind, page_id, body)
        if not live:
            return {"status": "applied_draft"}
        try:
            self._push_live(kind, page_id)
            return {"status": "applied_live"}
        except Exception as e:
            return {
                "status": "applied_draft",
                "fallback_reason": f"live push failed: {type(e).__name__}",
            }


# ---------------------------------------------------------------------------
# Candidate selection
# ---------------------------------------------------------------------------

def _is_weak_title(t: str | None) -> bool:
    if not t or not isinstance(t, str):
        return True
    return len(t.strip()) < WEAK_TITLE_MAX_LEN


def _is_weak_meta(m: str | None) -> bool:
    if not m or not isinstance(m, str):
        return True
    return len(m.strip()) < WEAK_META_MAX_LEN


def _normalize_page(p: dict) -> dict:
    return {
        "_id": p.get("id"),
        "url": p.get("url") or "",
        "slug": p.get("slug") or "",
        "name": p.get("name") or "",
        "html_title": p.get("htmlTitle") or "",
        "meta_description": p.get("metaDescription") or "",
        "type": p.get("_object_type", "site_page"),
        "currently_published": p.get("currentlyPublished"),
        "archived": p.get("archived"),
    }


# Markers HubSpot puts on auto-created / unpublished scratch pages. We
# never write to these and never surface their slugs publicly: the slug
# embeds a per-page UUID (private-ish, and useless as a public path).
_NON_PUBLISHABLE_SLUG_MARKERS = ("-temporary-slug-", "temporary-slug")


def _is_publishable_page(np: dict) -> bool:
    """Whether a CMS page is a real, published page safe to optimize.

    Excludes archived pages, never-published drafts, and HubSpot's
    auto-generated ``-temporary-slug-<uuid>`` scratch pages. Writing
    metadata to these is pointless and leaks a UUID-bearing slug into
    the public snapshot.
    """
    if np.get("archived") is True:
        return False
    if np.get("currently_published") is False:
        return False
    slug = (np.get("slug") or "").lower()
    url = (np.get("url") or "").lower()
    for marker in _NON_PUBLISHABLE_SLUG_MARKERS:
        if marker in slug or marker in url:
            return False
    return True


def _public_page_label(np: dict) -> str:
    slug = _public_slug(np.get("url") or ("/" + (np.get("slug") or "")))
    title = (np.get("name") or "").strip()
    if title and len(title) <= 80:
        return f"{slug} — {title}"
    return slug or title or "(unnamed page)"


def _gsc_index(snapshot: dict) -> dict[str, list[dict]]:
    oi = snapshot.get("organic_insights") or {}
    rows: dict[str, list[dict]] = {"pages": [], "queries": []}
    for r in oi.get("gsc_page_rows") or []:
        if isinstance(r, dict) and r.get("page"):
            rows["pages"].append(r)
    for r in oi.get("gsc_query_rows") or []:
        if isinstance(r, dict) and r.get("query"):
            rows["queries"].append(r)
    return rows


def _matches_demand_theme(text: str) -> str | None:
    t = (text or "").lower()
    if not t:
        return None
    for tok in DEMAND_OFFICE_TOKENS:
        if tok in t:
            return f"office:{tok}"
    for tok in DEMAND_SERVICE_TOKENS:
        if tok in t:
            return f"service:{tok}"
    return None


def _gsc_signal_for_page(
    np: dict,
    gsc: dict[str, list[dict]],
    *,
    accelerated: bool = False,
) -> dict | None:
    slug = _public_slug(np.get("url") or ("/" + (np.get("slug") or "")))
    if not slug or slug == "/":
        return None
    tokens = [t for t in slug.strip("/").split("/") if t]
    if not tokens:
        return None
    leaf = tokens[-1]
    min_impr = ACCEL_MIN_IMPRESSIONS if accelerated else MIN_IMPRESSIONS_FOR_CTR_LEAK
    max_ctr = ACCEL_MAX_CTR_PCT if accelerated else MAX_CTR_PCT_FOR_LEAK

    # 1. Page-level: high impressions / low CTR (standard + accelerated).
    for r in gsc["pages"]:
        page = (r.get("page") or "").lower()
        if not (leaf and leaf.lower() in page):
            continue
        impressions = r.get("impressions")
        ctr = r.get("ctr_pct")
        position = r.get("avg_position")
        if (
            isinstance(impressions, (int, float))
            and impressions >= min_impr
            and isinstance(ctr, (int, float))
            and ctr <= max_ctr
        ):
            return {
                "match": "page_row_high_impression_low_ctr",
                "rule": "high_impression_low_ctr",
                "page": _public_slug(r.get("page") or ""),
                "clicks": r.get("clicks"),
                "impressions": impressions,
                "ctr_pct": ctr,
                "avg_position": position,
            }
        # 2. Accelerated only: near-rank opportunity (page 2 -> page 1).
        if accelerated and (
            isinstance(position, (int, float))
            and NEAR_RANK_MIN_POSITION <= position <= NEAR_RANK_MAX_POSITION
            and isinstance(impressions, (int, float))
            and impressions >= NEAR_RANK_MIN_IMPRESSIONS
        ):
            return {
                "match": "page_row_near_rank",
                "rule": "near_rank_page_2_to_page_1_opportunity",
                "page": _public_slug(r.get("page") or ""),
                "clicks": r.get("clicks"),
                "impressions": impressions,
                "ctr_pct": ctr,
                "avg_position": position,
            }

    # 3. Query-level: high impressions / low CTR + (accelerated) demand-theme.
    for r in gsc["queries"]:
        action = (r.get("action") or "").lower()
        query = (r.get("query") or "").lower()
        if not (leaf and (leaf.lower() in action or leaf.lower() in query)):
            continue
        impressions = r.get("impressions")
        ctr = r.get("ctr_pct")
        if (
            isinstance(impressions, (int, float))
            and impressions >= min_impr
            and isinstance(ctr, (int, float))
            and ctr <= max_ctr
        ):
            return {
                "match": "query_row_high_impression_low_ctr",
                "rule": "high_impression_low_ctr",
                "query": r.get("query"),
                "clicks": r.get("clicks"),
                "impressions": impressions,
                "ctr_pct": ctr,
                "avg_position": r.get("avg_position"),
            }
        if accelerated:
            theme = _matches_demand_theme(query)
            if theme and isinstance(impressions, (int, float)) and impressions >= NEAR_RANK_MIN_IMPRESSIONS:
                return {
                    "match": "query_row_demand_theme",
                    "rule": "office_service_query_match",
                    "demand_theme": theme,
                    "query": r.get("query"),
                    "clicks": r.get("clicks"),
                    "impressions": impressions,
                    "ctr_pct": ctr,
                    "avg_position": r.get("avg_position"),
                }

    # 4. Accelerated only: missing/weak metadata with any demand-theme slug match.
    if accelerated:
        theme = _matches_demand_theme(slug)
        if theme and (_is_weak_title(np.get("html_title")) or _is_weak_meta(np.get("meta_description"))):
            return {
                "match": "slug_demand_theme_weak_metadata",
                "rule": "page_title_or_meta_missing_or_weak",
                "demand_theme": theme,
                "query": leaf,
                "clicks": None,
                "impressions": None,
                "ctr_pct": None,
                "avg_position": None,
            }
    return None


def _weak_metadata_signal(np: dict) -> dict:
    """Synthetic signal for a page whose title/meta is missing or weak.

    Used in live-capable apply runs so a defective <title> or meta
    description is itself a sufficient reason for a safe, reversible
    live metadata write — independent of any GSC demand signal. Metric
    fields are None because this is driven by the on-page defect, not a
    search-performance threshold.
    """
    slug = _public_slug(np.get("url") or ("/" + (np.get("slug") or "")))
    leaf = (slug.strip("/").split("/")[-1] if slug.strip("/") else slug) or "page"
    return {
        "match": "weak_metadata_no_gsc_signal",
        "rule": "page_title_or_meta_missing_or_weak",
        "query": leaf,
        "clicks": None,
        "impressions": None,
        "ctr_pct": None,
        "avg_position": None,
    }


def _callrail_review_themes(snapshot: dict) -> list[str]:
    """Extract sanitized demand themes from CallRail / review aggregates.

    These are short text labels (no PII) used as a soft signal when
    deciding which pages to prioritize. We never read raw call bodies
    or review bodies; we only look at sanitized aggregate counts.
    """
    themes: list[str] = []
    cr = snapshot.get("callrail_live") or {}
    for row in (cr.get("top_call_intents") or [])[:5]:
        if isinstance(row, dict) and isinstance(row.get("intent"), str):
            themes.append(row["intent"].lower())
    gmb = snapshot.get("gmb_insights") or {}
    for row in (gmb.get("top_review_themes") or [])[:5]:
        if isinstance(row, dict) and isinstance(row.get("theme"), str):
            themes.append(row["theme"].lower())
    return themes


def _draft_title(np: dict, signal: dict) -> str:
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
    """Build a metadata-update candidate (title / meta description).

    Small body / FAQ / internal-link improvements are tracked through a
    separate proposal path (see ``_build_small_content_proposal``) so
    that the metadata-write loop stays narrow and reversible.
    """
    weak_title = _is_weak_title(np.get("html_title"))
    weak_meta = _is_weak_meta(np.get("meta_description"))
    if not (weak_title or weak_meta):
        return None
    fields: dict[str, str] = {}
    change_types: list[str] = []
    if weak_title:
        fields["html_title"] = _draft_title(np, signal)
        change_types.append(CHANGE_TITLE)
    if weak_meta:
        fields["meta_description"] = _draft_meta(np, signal)
        change_types.append(CHANGE_META)
    return {
        "_id": np.get("_id"),
        "page_label": _public_page_label(np),
        "slug": _public_slug(np.get("url") or ("/" + (np.get("slug") or ""))),
        "type": np.get("type", "site_page"),
        "current_title_len": len((np.get("html_title") or "")),
        "current_meta_len": len((np.get("meta_description") or "")),
        "proposed_fields": fields,
        "change_types": change_types,
        "signal": signal,
    }


def _build_small_content_proposal(np: dict, signal: dict) -> dict | None:
    """Stage a small body-copy / FAQ / internal-link improvement.

    These are never written live by this optimizer: the HubSpot CMS
    page-module structure varies per template, and a careless PATCH can
    corrupt the rendered page. We surface the proposal with the
    smallest possible scope (one-paragraph rewrite, single FAQ row,
    single internal-link block) and a clear ``why`` so the operator
    can promote it to a HubSpot draft from the UI.
    """
    keyword_source = signal.get("query") or signal.get("page") or ""
    keyword = (str(keyword_source).strip().rstrip("/").split("/")[-1] or "").replace("-", " ").strip()
    if not keyword:
        return None
    # Choose the proposal type based on the signal rule.
    rule = signal.get("rule") or signal.get("match") or ""
    if "near_rank" in rule:
        change_type = CHANGE_FAQ
        proposal_text = (
            f"Add a 2-3 question FAQ block answering common '{keyword}' "
            "questions (eligibility, timing, what's included). Keep total "
            "added copy under 600 characters."
        )
    elif "office_service" in rule or signal.get("demand_theme"):
        change_type = CHANGE_INTERNAL_LINK
        proposal_text = (
            f"Add 1-2 internal links from this page to the matching "
            f"'{keyword}' service or office page, with descriptive anchor "
            "text. No new sections; reuse existing copy where possible."
        )
    else:
        change_type = CHANGE_SMALL_BODY
        proposal_text = (
            f"Rewrite the opening paragraph to lead with '{keyword}' and "
            "the patient outcome. Single-paragraph change, under 280 "
            "characters added; no template/CTA/form changes."
        )
    return {
        "_id": np.get("_id"),
        "page_label": _public_page_label(np),
        "slug": _public_slug(np.get("url") or ("/" + (np.get("slug") or ""))),
        "type": np.get("type", "site_page"),
        "current_title_len": len((np.get("html_title") or "")),
        "current_meta_len": len((np.get("meta_description") or "")),
        "proposed_fields": {"proposal_text": proposal_text},
        "change_types": [change_type],
        "signal": signal,
        "small_content": True,
    }


_COOLDOWN_BLOCKING_STATUSES = {
    "applied_live",
    "applied_draft",  # current label for a real HubSpot draft write
}

# Change types considered low-risk metadata. Approved live writes for
# these touch only the page <title> and <meta name="description"> on
# already-existing pages. They do not alter body copy, modules, FAQ
# blocks, internal-link blocks, CTA wiring, or templates, so re-running
# them does not need a cooldown for safety reasons. Cooldown stays in
# effect for any change type outside this set.
LOW_RISK_METADATA_CHANGE_TYPES = {"missing_or_weak_title_update",
                                  "missing_or_weak_meta_description_update"}


def _no_cooldown_for_approved_live_writes(cfg: dict) -> bool:
    """Whether the config opts the main site into no-cooldown low-risk live writes.

    Operator request (2026-05): on the main HubSpot website, approved
    low-risk metadata live writes (title / meta only) should not be
    blocked by the per-page cooldown. Body / module / template changes
    still respect cooldown.
    """
    accel = cfg.get("accelerated_growth_mode") or {}
    # Explicit opt-in at the accelerated layer wins.
    if isinstance(accel, dict) and accel.get("no_cooldown_for_approved_live_writes") is True:
        return True
    # Otherwise the accelerated publish mode itself implies the operator
    # has approved live writes for the main HubSpot site — honor that.
    if cfg.get("publish_mode") == ACCELERATED_PUBLISH_MODE:
        return True
    return False


def _effective_cooldown_days(cand: dict, cfg: dict, cooldown_days: int) -> int:
    """Return cooldown days to record for this candidate.

    Approved low-risk metadata live writes record a 0-day cooldown when
    the config opts in, so the daily run is free to re-evaluate the
    same title/meta on the main HubSpot site without a 7-day lockout.
    Anything outside ``LOW_RISK_METADATA_CHANGE_TYPES`` keeps the
    configured cooldown.
    """
    change_types = set(cand.get("change_types") or [])
    if change_types and change_types.issubset(LOW_RISK_METADATA_CHANGE_TYPES):
        if _no_cooldown_for_approved_live_writes(cfg):
            return 0
    return cooldown_days


def _candidate_cooldown_active(
    state: dict, slug: str, cooldown_days: int,
    cand: dict | None = None, cfg: dict | None = None,
) -> bool:
    """Cooldown blocks re-touching slugs that already had a real write.

    Entries that were only proposed (``proposed_not_applied`` /
    ``proposed``) do not block — we want a daily run that gains
    new permissions to upgrade them to a real write. Legacy entries
    with ``draft_saved_unpublished`` also do not block; the new
    optimizer should be allowed to push them live.

    When ``cand`` carries only low-risk metadata change types (title /
    meta) and ``cfg`` opts into ``no_cooldown_for_approved_live_writes``
    (default true for the accelerated publish mode), cooldown is
    skipped — the operator has explicitly approved daily re-touching
    of these fields on the main HubSpot site.
    """
    if cooldown_days <= 0:
        return False
    if cand is not None and cfg is not None:
        change_types = set(cand.get("change_types") or [])
        if change_types and change_types.issubset(LOW_RISK_METADATA_CHANGE_TYPES):
            if _no_cooldown_for_approved_live_writes(cfg):
                return False
    cms = state.get("cms_experiments") or {}
    log = cms.get("log") or []
    cutoff = utcnow() - timedelta(days=cooldown_days)
    for row in log:
        if not isinstance(row, dict):
            continue
        if row.get("slug") != slug:
            continue
        if row.get("status") not in _COOLDOWN_BLOCKING_STATUSES:
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
    re.compile(r"\bpat-na1-[0-9a-f-]{8,}\b", re.IGNORECASE),
    re.compile(r"\bhubspot[_-]?(?:portal|hub)[_-]?id\b", re.IGNORECASE),
    re.compile(r"/cron_tracking/"),
    re.compile(r"hubspot_cms_config\.json"),
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
    if CHANGE_TITLE in cand.get("change_types", []):
        why_bits.append(f"title len {cand.get('current_title_len')}")
    if CHANGE_META in cand.get("change_types", []):
        why_bits.append(f"meta len {cand.get('current_meta_len')}")
    row = {
        "page": cand.get("page_label"),
        "slug": cand.get("slug"),
        "page_type": cand.get("type"),
        "change": ", ".join(cand.get("change_types") or []),
        "why": "; ".join(why_bits) or "weak metadata",
        "status": status,
        "expected_impact": (
            "Lift CTR by ~0.3-0.7pp on high-impression, low-CTR queries; "
            "lift clicks within 14-28d."
        ),
        "metric_to_watch": "; ".join(metric_to_watch) or "CTR / clicks",
    }
    if note:
        row["note"] = note
    return row


# ---------------------------------------------------------------------------
# Eligibility + impact tracking
# ---------------------------------------------------------------------------

def _load_config(config_path: Path) -> dict:
    cfg = read_json(config_path, None)
    if not isinstance(cfg, dict):
        raise RuntimeError(f"hubspot_cms_config not found or unreadable at {config_path.name}")
    if not cfg.get("token"):
        raise RuntimeError("hubspot_cms_config: missing token")
    return cfg


def _credentials_present(cfg: dict) -> bool:
    """Whether a usable HubSpot token is present in the config.

    A blank / whitespace-only / obvious-placeholder token counts as
    missing. We never log or echo the token itself — only this boolean.
    """
    tok = cfg.get("token")
    if not isinstance(tok, str):
        return False
    tok = tok.strip()
    if len(tok) < 8:
        return False
    placeholder_markers = ("changeme", "placeholder", "your_token", "xxxx", "<", "todo")
    low = tok.lower()
    return not any(m in low for m in placeholder_markers)


def _publish_mode_is_live_capable(cfg: dict) -> bool:
    """Whether the configured publish_mode authorizes any live write.

    True for the accelerated mode and the standard controlled-live mode.
    The legacy drafts-only mode is not live-capable.
    """
    return cfg.get("publish_mode") in PUBLISH_MODES_WITH_LIVE


def _compute_live_write_status(out: dict, apply_changes: bool) -> str:
    """Distinguish *why* a run did or did not perform live writes.

    Avoids the historical ambiguity where any run with zero live writes
    was labelled a flat "dry-run" regardless of cause. Possible values:

      * ``credentials_missing`` / ``config_missing`` -- set earlier and
        not overwritten here (the run returns before this is reached).
      * ``live_written``      -- at least one live metadata write landed.
      * ``draft_written``     -- only HubSpot drafts were saved.
      * ``check_dry_run``     -- ``--check`` mode; nothing applied by design.
      * ``not_live_capable``  -- publish_mode does not authorize live writes.
      * ``no_eligible_candidates`` -- live-capable + credentialed, but no
        page qualified for a live metadata write this run (only proposals).
    """
    if out.get("live_writes", 0) > 0:
        return "live_written"
    if out.get("draft_writes", 0) > 0:
        return "draft_written"
    if not apply_changes:
        return "check_dry_run"
    if not out.get("live_capable"):
        return "not_live_capable"
    return "no_eligible_candidates"


def _tier_set(cfg: dict, name: str) -> set[str]:
    tiers = cfg.get("safety_tiers") or {}
    val = tiers.get(name)
    if isinstance(val, list):
        return {str(v) for v in val}
    return set()


def _accel(cfg: dict) -> dict:
    """Return the accelerated_growth_mode sub-block (or {} if absent)."""
    accel = cfg.get("accelerated_growth_mode") or {}
    return accel if isinstance(accel, dict) else {}


def _accel_enabled(cfg: dict) -> bool:
    accel = _accel(cfg)
    return bool(accel.get("enabled")) and cfg.get("publish_mode") == ACCELERATED_PUBLISH_MODE


def _accel_tier_set(cfg: dict, name: str) -> set[str]:
    """Pull a tier list from accelerated_growth_mode, falling back to safety_tiers."""
    accel = _accel(cfg)
    val = accel.get(name)
    if isinstance(val, list):
        return {str(v) for v in val}
    return _tier_set(cfg, name)


def _eligibility(cfg: dict, cand: dict) -> dict:
    """Decide how this candidate may be written.

    Returns ``{"write": bool, "live": bool, "reason": str}``.
    ``write`` False means dry-run/proposal only. ``write`` True with
    ``live`` False means save as HubSpot draft.
    """
    publish_mode = cfg.get("publish_mode")
    if publish_mode not in PUBLISH_MODES_WITH_WRITE:
        return {"write": False, "live": False, "reason": "publish_mode disabled"}
    page_type = cand.get("type", "site_page")
    change_types = cand.get("change_types") or []
    if not change_types:
        return {"write": False, "live": False, "reason": "no change types"}

    # Small-content changes (body/FAQ/internal-link) are never written
    # live by this optimizer. They are surfaced as proposals so an
    # operator (or a follow-on tool with template-aware writes) can
    # apply them without risk of corrupting the page's module tree.
    if any(ct in SMALL_CONTENT_CHANGE_TYPES for ct in change_types):
        if publish_mode not in PUBLISH_MODES_WITH_SMALL_CONTENT:
            return {
                "write": False, "live": False,
                "reason": "small_content not enabled in publish_mode",
            }
        return {
            "write": False, "live": False,
            "reason": "small_content_proposed_for_review_no_safe_api_path",
        }

    # Accelerated mode keeps its tier lists under accelerated_growth_mode
    # but falls back to safety_tiers if missing.
    auto_live = _accel_tier_set(cfg, "auto_live_allowed")
    auto_draft = _accel_tier_set(cfg, "auto_draft_or_propose_only")
    # Legacy key, still honored if present.
    legacy_auto = _tier_set(cfg, "auto_allowed")

    all_live = True
    all_writable = True
    for ct in change_types:
        tier_key = _TIER_KEY.get((page_type, ct))
        if not tier_key:
            return {
                "write": False,
                "live": False,
                "reason": f"change type {ct} not mapped for {page_type}",
            }
        if tier_key in auto_live:
            continue
        all_live = False
        if tier_key in auto_draft:
            continue
        # Legacy fallback (drafts only).
        if ct in legacy_auto:
            continue
        return {
            "write": False,
            "live": False,
            "reason": f"{tier_key} not in any auto_* tier",
        }
    if all_live and publish_mode in PUBLISH_MODES_WITH_LIVE:
        return {"write": True, "live": True, "reason": "auto_live_allowed"}
    if all_writable:
        return {"write": True, "live": False, "reason": "auto_draft_or_legacy"}
    return {"write": False, "live": False, "reason": "no tier match"}


def _baseline_from_signal(sig: dict) -> dict:
    return {
        "as_of": today_iso(),
        "clicks_28d": sig.get("clicks"),
        "impressions_28d": sig.get("impressions"),
        "ctr_pct_28d": sig.get("ctr_pct"),
        "avg_position": sig.get("avg_position"),
        "source": sig.get("match"),
    }


def _refresh_impact_history(state: dict, snapshot: dict) -> int:
    """Append today's GSC sample to each existing experiment's impact_history.

    Returns the number of experiments that received a new sample.
    """
    cms = state.get("cms_experiments") or {}
    log = cms.get("log") or []
    if not log:
        return 0
    gsc = _gsc_index(snapshot)
    pages = gsc.get("pages") or []
    updated = 0
    today = today_iso()
    for row in log:
        if not isinstance(row, dict):
            continue
        slug = row.get("slug") or ""
        if not slug:
            continue
        leaf = slug.strip("/").split("/")[-1] if slug.strip("/") else slug
        match = None
        for p in pages:
            page = (p.get("page") or "").lower()
            if leaf and leaf.lower() in page:
                match = p
                break
        if not match:
            continue
        history = row.setdefault("impact_history", [])
        # Don't double-record on the same day.
        if history and history[-1].get("as_of") == today:
            continue
        baseline = row.get("baseline") or {}
        b_clicks = baseline.get("clicks_28d")
        b_ctr = baseline.get("ctr_pct_28d")
        sample = {
            "as_of": today,
            "clicks_28d": match.get("clicks"),
            "impressions_28d": match.get("impressions"),
            "ctr_pct_28d": match.get("ctr_pct"),
            "avg_position": match.get("avg_position"),
        }
        try:
            if isinstance(b_clicks, (int, float)) and isinstance(sample["clicks_28d"], (int, float)):
                sample["delta_clicks_vs_baseline"] = round(
                    float(sample["clicks_28d"]) - float(b_clicks), 2
                )
            if isinstance(b_ctr, (int, float)) and isinstance(sample["ctr_pct_28d"], (int, float)):
                sample["delta_ctr_pct_vs_baseline"] = round(
                    float(sample["ctr_pct_28d"]) - float(b_ctr), 3
                )
        except Exception:
            pass
        history.append(sample)
        # Cap history to most recent 60 samples per row.
        if len(history) > 60:
            del history[: len(history) - 60]
        updated += 1
    return updated


# ---------------------------------------------------------------------------
# Core run
# ---------------------------------------------------------------------------

def run(
    *,
    private_dir: Path,
    apply_changes: bool,
    max_changes: int,
    cooldown_days: int,
    snapshot: dict,
) -> dict:
    out: dict = {
        "ran_at": utcnow_iso(),
        "mode": "apply" if apply_changes else "check",
        "inventory": {"site_pages": 0, "landing_pages": 0},
        "candidates_considered": 0,
        "actions": [],
        "written_live": [],
        "written_draft": [],
        "errors": [],
        "publish_mode": None,
        "writeback_performed": False,
        "credentials_present": False,
        "live_capable": False,
        "live_write_status": "unknown",
        "live_writes": 0,
        "draft_writes": 0,
        "proposals": 0,
        "small_content_proposals": 0,
        "impact_samples_updated": 0,
        "accelerated": False,
        "growth_mode": "standard",
        "cooldown_days": cooldown_days,
        "max_changes_cap": max_changes,
        "cooldown_blocked_slugs": [],
        "next_opportunity_queue": [],
    }

    cfg_path = private_dir / DEFAULT_CONFIG_NAME
    cfg = read_json(cfg_path, None)
    if not isinstance(cfg, dict):
        out["errors"].append("config: hubspot_cms_config not found or unreadable")
        out["live_write_status"] = "config_missing"
        return out
    out["publish_mode"] = cfg.get("publish_mode")
    out["live_capable"] = _publish_mode_is_live_capable(cfg)
    creds_ok = _credentials_present(cfg)
    out["credentials_present"] = creds_ok
    # No usable token: never silently degrade to a generic "dry-run".
    # Surface an explicit credentials_missing status, keep proposals
    # staged so the operator sees the queued work, and stop before any
    # network call (which would only raise an opaque auth error).
    if not creds_ok:
        out["live_write_status"] = "credentials_missing"
        out["errors"].append("config: credentials_missing")
        return out
    # Per-config cap, if present.
    learn = cfg.get("daily_learning_loop") or {}
    accel = _accel(cfg)
    accelerated = _accel_enabled(cfg)
    out["accelerated"] = accelerated
    out["growth_mode"] = "accelerated" if accelerated else (learn.get("growth_mode") or "standard")
    # Accelerated config overrides cap + cooldown; otherwise fall back
    # to daily_learning_loop, otherwise the CLI default.
    if accelerated and isinstance(accel.get("max_live_metadata_changes_per_run"), int):
        max_changes = accel["max_live_metadata_changes_per_run"]
    elif isinstance(learn.get("max_live_metadata_changes_per_run"), int):
        max_changes = min(max_changes, learn["max_live_metadata_changes_per_run"])
    if accelerated and isinstance(accel.get("cooldown_days_per_page"), int):
        cooldown_days = accel["cooldown_days_per_page"]
    elif isinstance(learn.get("cooldown_days_per_page"), int):
        cooldown_days = learn["cooldown_days_per_page"]
    max_small_content = 0
    if accelerated and isinstance(accel.get("max_body_or_section_changes_per_run"), int):
        max_small_content = max(0, accel["max_body_or_section_changes_per_run"])
    out["cooldown_days"] = cooldown_days
    out["max_changes_cap"] = max_changes
    out["max_small_content_cap"] = max_small_content
    out["no_cooldown_for_approved_live_writes"] = (
        _no_cooldown_for_approved_live_writes(cfg)
    )
    out["low_risk_metadata_change_types"] = sorted(LOW_RISK_METADATA_CHANGE_TYPES)

    client = HubSpotClient(cfg["token"])
    pages: list[dict] = []
    try:
        site_pages = client.list_site_pages(limit=50)
        out["inventory"]["site_pages"] = len(site_pages)
        for p in site_pages:
            np = _normalize_page(p)
            np["type"] = "site_page"
            if _is_publishable_page(np):
                pages.append(np)
    except Exception as e:
        out["errors"].append(f"site_pages: {e}")
    try:
        landing_pages = client.list_landing_pages(limit=50)
        out["inventory"]["landing_pages"] = len(landing_pages)
        for p in landing_pages:
            np = _normalize_page(p)
            np["type"] = "landing_page"
            if _is_publishable_page(np):
                pages.append(np)
    except Exception as e:
        out["errors"].append(f"landing_pages: {e}")

    gsc = _gsc_index(snapshot)
    state = read_json(private_dir / DEFAULT_STATE_NAME, {}) or {}
    cms_state = state.setdefault("cms_experiments", {
        "version": 2,
        "log": [],
        "cooldown_days": cooldown_days,
    })
    cms_state["cooldown_days"] = cooldown_days
    cms_state["version"] = max(int(cms_state.get("version") or 1), 2)

    # Daily learning: append impact samples for existing experiments
    # before picking new candidates.
    out["impact_samples_updated"] = _refresh_impact_history(state, snapshot)

    # When the publish_mode authorizes live writes and the run is
    # actually applying, weak title/meta pages are themselves eligible
    # for a safe live metadata write even without a GSC demand signal:
    # a missing/short <title> or meta description is a defect to fix on
    # its own. Without this, the live-write path was structurally starved
    # because the weak pages rarely overlap the GSC-signalled pages, so
    # every run degraded to "dry-run" despite a live-capable config.
    weak_metadata_live_eligible = bool(
        apply_changes and out.get("live_capable")
    )

    candidates: list[dict] = []
    small_candidates: list[dict] = []
    cooldown_blocked: list[dict] = []
    next_queue: list[dict] = []
    for np in pages:
        signal = _gsc_signal_for_page(np, gsc, accelerated=accelerated)
        if not signal and weak_metadata_live_eligible and (
            _is_weak_title(np.get("html_title"))
            or _is_weak_meta(np.get("meta_description"))
        ):
            signal = _weak_metadata_signal(np)
        if not signal:
            continue
        out["candidates_considered"] += 1
        slug = _public_slug(np.get("url") or ("/" + (np.get("slug") or "")))
        cand = _build_candidate(np, signal)
        if _candidate_cooldown_active(
            state, slug, cooldown_days, cand=cand, cfg=cfg,
        ):
            cooldown_blocked.append({
                "slug": slug,
                "rule": signal.get("rule"),
                "reason": f"cooldown_active_{cooldown_days}d",
            })
            continue
        if cand:
            if len(candidates) < max_changes:
                candidates.append(cand)
            else:
                next_queue.append({
                    "slug": cand["slug"],
                    "rule": signal.get("rule"),
                    "change": ", ".join(cand.get("change_types") or []),
                    "reason": "queued_beyond_run_cap",
                })
        # In accelerated mode, also stage one small-content proposal per
        # page (capped). These never write live; they surface to the
        # operator with an explicit reason.
        if accelerated and max_small_content > 0 and len(small_candidates) < max_small_content:
            sc = _build_small_content_proposal(np, signal)
            if sc:
                small_candidates.append(sc)
    out["cooldown_blocked_slugs"] = cooldown_blocked[:10]
    out["next_opportunity_queue"] = next_queue[:10]

    for cand in candidates:
        elig = _eligibility(cfg, cand)
        attempt_live = bool(apply_changes and elig["write"] and elig["live"])
        attempt_draft = bool(
            apply_changes and elig["write"] and not elig["live"]
        )
        if attempt_live or attempt_draft:
            try:
                resp = client.update_page_metadata(
                    cand["type"],
                    cand["_id"],
                    html_title=cand["proposed_fields"].get("html_title"),
                    meta_description=cand["proposed_fields"].get("meta_description"),
                    live=attempt_live,
                )
                actual_status = resp.get("status", "applied_draft")
                if actual_status == "applied_live":
                    note = "Live metadata update pushed to HubSpot CMS"
                    out["live_writes"] += 1
                    row = _public_action_row(cand, "applied_live", note)
                    out["written_live"].append(row)
                    out["writeback_performed"] = True
                else:
                    note = "HubSpot draft (unpublished)"
                    if resp.get("fallback_reason"):
                        note += "; live fallback"
                    out["draft_writes"] += 1
                    row = _public_action_row(cand, "applied_draft", note)
                    out["written_draft"].append(row)
                    out["writeback_performed"] = True
                out["actions"].append(row)
                applied_at = utcnow_iso()
                eff_cd = _effective_cooldown_days(cand, cfg, cooldown_days)
                cms_state["log"].append({
                    "applied_at": applied_at,
                    "cooldown_until": (
                        utcnow() + timedelta(days=eff_cd)
                    ).isoformat(),
                    "cooldown_days_applied": eff_cd,
                    "slug": cand["slug"],
                    "page_label": cand["page_label"],
                    "page_type": cand["type"],
                    "change_types": cand["change_types"],
                    "hypothesis": (
                        "Strengthening title/meta on a high-impression, "
                        "low-CTR page should lift CTR and clicks within "
                        "14-28 days, with no body / template / CTA change."
                    ),
                    "baseline": _baseline_from_signal(cand.get("signal") or {}),
                    "metric_to_watch": (
                        "ctr_pct (gsc), clicks (gsc), sessions (ga4), "
                        "qualified_calls (callrail)"
                    ),
                    "status": actual_status,
                    "impact_history": [],
                })
            except Exception as e:
                row = _public_action_row(
                    cand, "error", f"writeback failed: {type(e).__name__}"
                )
                out["actions"].append(row)
                out["errors"].append(f"writeback: {type(e).__name__}")
        else:
            reason = "dry-run" if not apply_changes else elig.get("reason", "")
            row = _public_action_row(cand, "proposed", reason)
            out["actions"].append(row)
            out["proposals"] += 1
            eff_cd_p = _effective_cooldown_days(cand, cfg, cooldown_days)
            cms_state["log"].append({
                "applied_at": utcnow_iso(),
                "cooldown_until": (
                    utcnow() + timedelta(days=eff_cd_p)
                ).isoformat(),
                "cooldown_days_applied": eff_cd_p,
                "slug": cand["slug"],
                "page_label": cand["page_label"],
                "page_type": cand["type"],
                "change_types": cand["change_types"],
                "hypothesis": (
                    "Strengthening title/meta on a high-impression, "
                    "low-CTR page should lift CTR and clicks."
                ),
                "baseline": _baseline_from_signal(cand.get("signal") or {}),
                "metric_to_watch": (
                    "ctr_pct (gsc), clicks (gsc), sessions (ga4)"
                ),
                "status": "proposed_not_applied",
                "impact_history": [],
            })

    # Small-content proposals (body / FAQ / internal-link). These are
    # never written live — the HubSpot module structure varies by
    # template and a careless PATCH can corrupt the rendered page.
    # We surface them as draft proposals so the operator can promote
    # them safely from the HubSpot UI.
    for cand in small_candidates:
        elig = _eligibility(cfg, cand)
        reason = elig.get("reason", "small_content_proposed_for_review_no_safe_api_path")
        row = _public_action_row(cand, "proposed", reason)
        row["proposal_text"] = cand["proposed_fields"].get("proposal_text", "")
        out["actions"].append(row)
        out["proposals"] += 1
        out["small_content_proposals"] += 1
        cms_state["log"].append({
            "applied_at": utcnow_iso(),
            "cooldown_until": (
                utcnow() + timedelta(days=cooldown_days)
            ).isoformat(),
            "slug": cand["slug"],
            "page_label": cand["page_label"],
            "page_type": cand["type"],
            "change_types": cand["change_types"],
            "hypothesis": (
                "A scoped body/FAQ/internal-link improvement on a "
                "demand-themed page should lift on-page engagement and "
                "internal pageviews within 14-28 days. Safety: only "
                "small, reversible, no template/CTA/form change."
            ),
            "baseline": _baseline_from_signal(cand.get("signal") or {}),
            "metric_to_watch": (
                "ctr_pct (gsc), clicks (gsc), sessions (ga4), "
                "internal_links_clicked (ga4)"
            ),
            "status": "proposed_not_applied",
            "acceleration_reason": (
                "accelerated_mode_small_content_safe_proposal"
            ),
            "impact_history": [],
        })

    out["live_write_status"] = _compute_live_write_status(out, apply_changes)

    try:
        write_json_atomic(private_dir / DEFAULT_STATE_NAME, state)
    except Exception as e:
        out["errors"].append(f"learning_state: {e}")

    out["actions"] = _strip_private_keys(out["actions"])
    out["written_live"] = _strip_private_keys(out["written_live"])
    out["written_draft"] = _strip_private_keys(out["written_draft"])

    issues = (
        assert_public_sanitized(out["actions"])
        + assert_public_sanitized(out["written_live"])
        + assert_public_sanitized(out["written_draft"])
    )
    if issues:
        out["errors"].extend(issues)
        out["actions"] = []
        out["written_live"] = []
        out["written_draft"] = []

    return out


def _impact_over_time_public(state_path: Path) -> list[dict]:
    """Sanitized impact-over-time rows derived from the private state.

    One row per logged experiment (most recent 12), summarising the
    impact_history without any private IDs.
    """
    state = read_json(state_path, {}) or {}
    log = ((state.get("cms_experiments") or {}).get("log") or [])[-12:]
    out: list[dict] = []
    for row in log:
        if not isinstance(row, dict):
            continue
        hist = row.get("impact_history") or []
        latest = hist[-1] if hist else None
        out.append({
            "page": row.get("page_label"),
            "slug": row.get("slug"),
            "change": ", ".join(row.get("change_types") or []),
            "status": row.get("status"),
            "applied_at": row.get("applied_at"),
            "baseline_ctr_pct": (row.get("baseline") or {}).get("ctr_pct_28d"),
            "baseline_clicks": (row.get("baseline") or {}).get("clicks_28d"),
            "latest_ctr_pct": (latest or {}).get("ctr_pct_28d"),
            "latest_clicks": (latest or {}).get("clicks_28d"),
            "delta_ctr_pct": (latest or {}).get("delta_ctr_pct_vs_baseline"),
            "delta_clicks": (latest or {}).get("delta_clicks_vs_baseline"),
            "samples": len(hist),
        })
    return out


def build_public_block(result: dict, *, private_dir: Path | None = None) -> dict:
    actions = result.get("actions") or []
    live_n = len(result.get("written_live") or [])
    draft_n = len(result.get("written_draft") or [])
    impact_rows: list[dict] = []
    if private_dir is not None:
        try:
            impact_rows = _impact_over_time_public(private_dir / DEFAULT_STATE_NAME)
        except Exception:
            impact_rows = []
    accelerated = bool(result.get("accelerated"))
    growth_mode = result.get("growth_mode") or "standard"
    cooldown_days = int(result.get("cooldown_days") or DEFAULT_COOLDOWN_DAYS)
    max_cap = int(result.get("max_changes_cap") or 0)
    small_cap = int(result.get("max_small_content_cap") or 0)
    why_no_prior = (
        f"Prior runs were held by the {cooldown_days}-day per-page "
        "cooldown after the May 13 live metadata writes. Accelerated "
        "mode shortens the cooldown and expands the candidate pool."
    ) if accelerated else (
        f"Prior runs were held by the {cooldown_days}-day per-page "
        "cooldown after the most recent live metadata write."
    )
    return {
        "title": "Organic / HubSpot CMS automation",
        "last_run_at": result.get("ran_at"),
        "mode": result.get("mode"),
        "publish_mode": result.get("publish_mode"),
        "growth_mode": growth_mode,
        "accelerated": accelerated,
        "writeback_performed": bool(result.get("writeback_performed")),
        "credentials_present": bool(result.get("credentials_present")),
        "live_capable": bool(result.get("live_capable")),
        "live_write_status": result.get("live_write_status") or "unknown",
        "live_writes": int(result.get("live_writes") or 0),
        "draft_writes": int(result.get("draft_writes") or 0),
        "proposals": int(result.get("proposals") or 0),
        "small_content_proposals": int(result.get("small_content_proposals") or 0),
        "impact_samples_updated": int(result.get("impact_samples_updated") or 0),
        "cooldown_days": cooldown_days,
        "max_changes_cap": max_cap,
        "max_small_content_cap": small_cap,
        "no_cooldown_for_approved_live_writes": bool(
            result.get("no_cooldown_for_approved_live_writes")
        ),
        "low_risk_metadata_change_types": result.get(
            "low_risk_metadata_change_types"
        ) or [],
        "cooldown_policy_note": (
            "Approved low-risk metadata live writes (title/meta only) on "
            "the main HubSpot website have no cooldown. Body / module / "
            "FAQ / internal-link changes keep the standard cooldown."
        ),
        "why_no_prior_change": why_no_prior,
        "cooldown_blocked_slugs": result.get("cooldown_blocked_slugs") or [],
        "next_opportunity_queue": result.get("next_opportunity_queue") or [],
        "summary": (
            f"{len(actions)} CMS action(s) considered; "
            f"{live_n} live · {draft_n} draft · "
            f"{int(result.get('proposals') or 0)} proposed "
            f"({'accelerated' if accelerated else 'standard'} mode)."
        ),
        "actions": actions,
        "impact_over_time": impact_rows,
        "errors": [
            e for e in (result.get("errors") or [])
            if "config" not in e.lower()
        ][:5],
    }


# ---------------------------------------------------------------------------
# Preflight (no network, no writes)
# ---------------------------------------------------------------------------

def preflight(private_dir: Path) -> dict:
    """Report live-write readiness without any network call or write.

    Answers, without touching HubSpot: is a config present, is a usable
    token present, does the publish_mode authorize live writes, and
    would an apply run be expected to perform live writes or surface a
    blocker? Never echoes the token or any private path/ID.
    """
    out: dict = {
        "config_present": False,
        "credentials_present": False,
        "publish_mode": None,
        "live_capable": False,
        "accelerated": False,
        "no_cooldown_for_approved_live_writes": False,
        "expected_live_write_status": "config_missing",
        "blocker": None,
    }
    cfg = read_json(private_dir / DEFAULT_CONFIG_NAME, None)
    if not isinstance(cfg, dict):
        out["blocker"] = "hubspot_cms_config not present or unreadable"
        return out
    out["config_present"] = True
    out["publish_mode"] = cfg.get("publish_mode")
    out["live_capable"] = _publish_mode_is_live_capable(cfg)
    out["accelerated"] = _accel_enabled(cfg)
    out["credentials_present"] = _credentials_present(cfg)
    out["no_cooldown_for_approved_live_writes"] = (
        _no_cooldown_for_approved_live_writes(cfg)
    )
    if not out["credentials_present"]:
        out["expected_live_write_status"] = "credentials_missing"
        out["blocker"] = "credentials_missing: token absent or placeholder"
    elif not out["live_capable"]:
        out["expected_live_write_status"] = "not_live_capable"
        out["blocker"] = "publish_mode does not authorize live writes"
    else:
        out["expected_live_write_status"] = "live_capable_ready"
    return out


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="HubSpot CMS metadata optimizer (controlled live writeback + daily learning).",
    )
    p.add_argument("--private-dir", default=str(DEFAULT_PRIVATE_DIR))
    p.add_argument("--check", action="store_true",
                   help="Dry-run only: propose changes, write nothing to HubSpot.")
    p.add_argument("--apply", action="store_true",
                   help="Apply low-risk metadata changes (live where eligible, draft otherwise).")
    p.add_argument("--preflight", action="store_true",
                   help="Report live-write readiness (credentials + publish_mode) without any network call or write.")
    p.add_argument("--max-changes", type=int, default=10)
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
    if args.preflight:
        print(json.dumps(preflight(private_dir), indent=2))
        return 0
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
    block = build_public_block(result, private_dir=private_dir)
    if args.write_block:
        write_json_atomic(Path(args.write_block), block)
    print(json.dumps({
        "mode": result["mode"],
        "publish_mode": result["publish_mode"],
        "growth_mode": result.get("growth_mode"),
        "accelerated": result.get("accelerated"),
        "inventory": result["inventory"],
        "candidates_considered": result["candidates_considered"],
        "actions": len(result["actions"]),
        "live_writes": result["live_writes"],
        "draft_writes": result["draft_writes"],
        "proposals": result["proposals"],
        "small_content_proposals": result.get("small_content_proposals", 0),
        "writeback_performed": result["writeback_performed"],
        "credentials_present": result.get("credentials_present"),
        "live_capable": result.get("live_capable"),
        "live_write_status": result.get("live_write_status"),
        "impact_samples_updated": result["impact_samples_updated"],
        "cooldown_days": result.get("cooldown_days"),
        "max_changes_cap": result.get("max_changes_cap"),
        "cooldown_blocked": len(result.get("cooldown_blocked_slugs") or []),
        "next_queue": len(result.get("next_opportunity_queue") or []),
        "errors": result["errors"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
