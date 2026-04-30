"""Reference build script for the public Clove outreach dashboard mirror.

This script is published in the public mirror repo for transparency.
The authoritative builder lives in the private operations repo and
reads the daily-outreach scheduled task's state files. The public
mirror is produced by:

  1. Running the private builder to generate the unsanitized snapshot.
  2. Passing the result through ``sanitize_for_public()`` below to
     strip recipient-level data, reply senders, free-text summaries,
     and the internal Google Sheet identifier.
  3. Writing the sanitized snapshot to ``data/snapshot.json`` and
     re-injecting the JSON between the ``/* SNAPSHOT_START */`` and
     ``/* SNAPSHOT_END */`` markers in ``index.html``.

The public dashboard never receives raw replies, recipient emails, or
the operations sheet URL. See ``README.md`` for the full list of
fields that are dropped.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DASHBOARD_DIR = Path(__file__).resolve().parents[1]
DATA_FILE = DASHBOARD_DIR / "data" / "snapshot.json"
INDEX_HTML = DASHBOARD_DIR / "index.html"


def sanitize_for_public(snap: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``snap`` with sensitive prospect/PII data removed.

    Public dashboard exposes only aggregate metrics, category and
    status summaries, and operating guardrails. Individual prospect
    emails, reply sender names and addresses, free-text summaries,
    the operations Google Sheet identifier, and recipient-level batch
    rows are removed.
    """
    out = json.loads(json.dumps(snap))

    sources = out.get("sources", {})
    sources["sheet_url"] = "(redacted - private operations sheet)"
    sources["sheet_id"] = "(redacted)"

    cleaned_replies = []
    for reply in out.get("replies", []):
        cleaned_replies.append({
            "Date": reply.get("Date", ""),
            "Organization": "(redacted)",
            "Category": reply.get("Category", ""),
            "Classification": reply.get("Classification", ""),
            "Status": reply.get("Status", ""),
            "Bucket": reply.get("Bucket", ""),
        })
    out["replies"] = cleaned_replies

    batch = out.pop("latest_batch", []) or []
    out["latest_batch_summary"] = {
        "size": len(batch),
        "note": (
            "Recipient-level details are not exposed in the public "
            "dashboard. See channel_mix_latest for category breakdown."
        ),
    }

    gh = out.get("github", {})
    gh.pop("latest_commit_before_dashboard", None)
    gh.pop("dashboard_build_commit", None)
    gh.pop("repo", None)
    gh["source_repo_visibility"] = "private"
    gh["dashboard_repo"] = "ishanpuri24/clove-outreach-dashboard"
    out["github"] = gh

    return out


def reinject_into_html(snap: dict[str, Any]) -> None:
    html = INDEX_HTML.read_text()
    start = html.index("/* SNAPSHOT_START */")
    end = html.index("/* SNAPSHOT_END */")
    new_block = (
        "/* SNAPSHOT_START */\n"
        f"window.__SNAPSHOT__ = {json.dumps(snap)};\n"
    )
    INDEX_HTML.write_text(html[:start] + new_block + html[end:])


if __name__ == "__main__":
    snap = json.loads(DATA_FILE.read_text())
    sanitized = sanitize_for_public(snap)
    DATA_FILE.write_text(json.dumps(sanitized, indent=2) + "\n")
    reinject_into_html(sanitized)
    print(f"Wrote sanitized {DATA_FILE} and re-injected into {INDEX_HTML}.")
