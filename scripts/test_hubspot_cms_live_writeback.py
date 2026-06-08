"""Unit tests for the HubSpot CMS optimizer live-writeback decision path.

These tests pin the behaviour that production run 43 got wrong: a
live-capable, credentialed accelerated config that performed zero live
writes and reported a flat ``accelerated (dry-run)``.

What is covered:

  * ``credentials_missing`` is reported explicitly (not a generic
    dry-run) and the run short-circuits before any network call, with
    proposals left staged.
  * ``--check`` mode reports ``check_dry_run``.
  * A live-capable + credentialed apply run that finds weak title/meta
    pages produces *live* metadata writes even when those pages have no
    GSC demand signal (the structural starvation bug).
  * Small-content (body/FAQ/internal-link) changes are never written
    live; they stay proposals.
  * Low-risk metadata change types are exempt from the cooldown when the
    accelerated publish mode is active.

No real network IO is performed; the HubSpot client is replaced with a
fake that records draft/live calls and never opens a socket.

Run with:

    python3 scripts/test_hubspot_cms_live_writeback.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hubspot_cms_optimizer as o  # noqa: E402

ACCEL = o.ACCELERATED_PUBLISH_MODE
# Fake token, never a real secret; built at runtime so no fixed literal.
_FAKE_TOKEN = "tok_" + "a" * 40


def _accel_config(token: str | None = _FAKE_TOKEN) -> dict:
    tier = [
        "site_page_title_update",
        "site_page_meta_description_update",
        "landing_page_title_update",
        "landing_page_meta_description_update",
    ]
    cfg: dict = {
        "publish_mode": ACCEL,
        "accelerated_growth_mode": {
            "enabled": True,
            "auto_live_allowed": tier,
            "max_live_metadata_changes_per_run": 10,
            "cooldown_days_per_page": 7,
            "max_body_or_section_changes_per_run": 3,
        },
        "safety_tiers": {"auto_live_allowed": tier},
    }
    if token is not None:
        cfg["token"] = token
    return cfg


class _FakeClient:
    """Stand-in for HubSpotClient. Records writes; opens no sockets."""

    def __init__(self, pages_site, pages_landing):
        self._site = pages_site
        self._landing = pages_landing
        self.draft_calls: list[dict] = []
        self.live_calls: list[dict] = []

    def list_site_pages(self, *, limit=50):
        return self._site

    def list_landing_pages(self, *, limit=50):
        return self._landing

    def update_page_metadata(self, kind, page_id, *, html_title, meta_description, live):
        rec = {"kind": kind, "id": page_id, "live": live,
               "html_title": html_title, "meta_description": meta_description}
        if live:
            self.live_calls.append(rec)
            return {"status": "applied_live"}
        self.draft_calls.append(rec)
        return {"status": "applied_draft"}


def _weak_page(pid: str) -> dict:
    # Short title + short meta -> both flagged weak.
    return {"id": pid, "url": f"https://example.com/p/{pid}", "slug": pid,
            "name": pid, "htmlTitle": "x", "metaDescription": "y"}


def _strong_page(pid: str) -> dict:
    return {"id": pid, "url": f"https://example.com/p/{pid}", "slug": pid,
            "name": pid,
            "htmlTitle": "A sufficiently long and descriptive page title here",
            "metaDescription": (
                "A meta description that is comfortably longer than the weak "
                "threshold so it is not flagged as missing or weak at all.")}


def _run(monkey_cfg, client, *, apply_changes, tmp_dir, snapshot=None):
    """Invoke o.run with config + client patched, isolated state dir."""
    orig_load = o._load_config
    orig_client = o.HubSpotClient
    orig_read = o.read_json

    def fake_read(path, default=None):
        # Config path returns our dict; everything else (state) uses real IO.
        if str(path).endswith(o.DEFAULT_CONFIG_NAME):
            return monkey_cfg
        return orig_read(path, default)

    o.read_json = fake_read  # type: ignore
    o._load_config = lambda p: monkey_cfg  # type: ignore
    o.HubSpotClient = lambda token, **kw: client  # type: ignore
    try:
        return o.run(
            private_dir=tmp_dir,
            apply_changes=apply_changes,
            max_changes=10,
            cooldown_days=o.DEFAULT_COOLDOWN_DAYS,
            snapshot=snapshot or {},
        )
    finally:
        o.read_json = orig_read  # type: ignore
        o._load_config = orig_load  # type: ignore
        o.HubSpotClient = orig_client  # type: ignore


def test_credentials_present_helper():
    assert o._credentials_present(_accel_config()) is True
    assert o._credentials_present(_accel_config(token=None)) is False
    assert o._credentials_present({"token": ""}) is False
    assert o._credentials_present({"token": "   "}) is False
    assert o._credentials_present({"token": "your_token_here_changeme"}) is False
    assert o._credentials_present({"token": "<paste>"}) is False
    print("OK: test_credentials_present_helper")


def test_publish_mode_live_capable():
    assert o._publish_mode_is_live_capable(_accel_config()) is True
    assert o._publish_mode_is_live_capable(
        {"publish_mode": "controlled_live_writeback_allowed"}) is True
    assert o._publish_mode_is_live_capable(
        {"publish_mode": "low_risk_metadata_writeback_allowed"}) is False
    print("OK: test_publish_mode_live_capable")


def test_missing_credentials_yields_explicit_status(tmp_path):
    cfg = _accel_config(token=None)

    class _Boom:
        def list_site_pages(self, **k):
            raise AssertionError("network call attempted with no credentials")

        list_landing_pages = list_site_pages

    out = _run(cfg, _Boom(), apply_changes=True, tmp_dir=tmp_path)
    assert out["live_write_status"] == "credentials_missing", out["live_write_status"]
    assert out["credentials_present"] is False
    assert out["live_capable"] is True
    assert out["live_writes"] == 0
    assert any("credentials_missing" in e for e in out["errors"])
    print("OK: test_missing_credentials_yields_explicit_status")


def test_check_mode_is_dry_run(tmp_path):
    cfg = _accel_config()
    client = _FakeClient([_weak_page("a")], [])
    out = _run(cfg, client, apply_changes=False, tmp_dir=tmp_path)
    assert out["live_write_status"] == "check_dry_run", out["live_write_status"]
    assert out["live_writes"] == 0
    assert client.live_calls == []
    print("OK: test_check_mode_is_dry_run")


def test_weak_metadata_writes_live_without_gsc_signal(tmp_path):
    # No GSC rows at all -> previously zero live writes. Now the weak
    # title/meta defect alone makes the page live-eligible.
    cfg = _accel_config()
    client = _FakeClient([_weak_page("alpha"), _strong_page("beta")], [])
    out = _run(cfg, client, apply_changes=True, tmp_dir=tmp_path)
    assert out["live_write_status"] == "live_written", out["live_write_status"]
    assert out["live_writes"] >= 1, out
    # Only the weak page should be touched.
    touched = {c["id"] for c in client.live_calls}
    assert "alpha" in touched
    assert "beta" not in touched
    print("OK: test_weak_metadata_writes_live_without_gsc_signal")


def test_no_eligible_candidates_is_not_credentials_missing(tmp_path):
    # All pages strong -> nothing to write, but config is live-capable
    # and credentialed: must NOT be labelled a flat dry-run / missing.
    cfg = _accel_config()
    client = _FakeClient([_strong_page("s1"), _strong_page("s2")], [])
    out = _run(cfg, client, apply_changes=True, tmp_dir=tmp_path)
    assert out["live_write_status"] == "no_eligible_candidates", out["live_write_status"]
    assert out["credentials_present"] is True
    assert client.live_calls == []
    print("OK: test_no_eligible_candidates_is_not_credentials_missing")


def test_small_content_never_live():
    # A small-content (FAQ/body/internal-link) candidate is proposal-only.
    cand = {
        "type": "site_page",
        "change_types": [o.CHANGE_FAQ],
    }
    elig = o._eligibility(_accel_config(), cand)
    assert elig["write"] is False
    assert elig["live"] is False
    print("OK: test_small_content_never_live")


def test_low_risk_metadata_exempt_from_cooldown():
    cfg = _accel_config()
    cand = {"change_types": [o.CHANGE_TITLE, o.CHANGE_META]}
    # Even with a populated blocking log, low-risk metadata is exempt.
    state = {"cms_experiments": {"log": [{
        "slug": "/p/alpha", "status": "applied_live",
        "applied_at": o.utcnow_iso(),
    }]}}
    blocked = o._candidate_cooldown_active(
        state, "/p/alpha", o.DEFAULT_COOLDOWN_DAYS, cand=cand, cfg=cfg)
    assert blocked is False
    # A non-metadata change on the same slug IS blocked.
    cand2 = {"change_types": [o.CHANGE_FAQ]}
    blocked2 = o._candidate_cooldown_active(
        state, "/p/alpha", o.DEFAULT_COOLDOWN_DAYS, cand=cand2, cfg=cfg)
    assert blocked2 is True
    print("OK: test_low_risk_metadata_exempt_from_cooldown")


def test_temporary_slug_pages_excluded(tmp_path):
    # A HubSpot auto-created scratch page (-temporary-slug-<uuid>) is weak
    # but must never be written or surfaced — its slug carries a UUID.
    cfg = _accel_config()
    temp_page = {"id": "tmp1",
                 "url": "https://example.com/-temporary-slug-abcdef012345",
                 "slug": "-temporary-slug-abcdef012345",
                 "name": "scratch", "htmlTitle": "x", "metaDescription": "y"}
    client = _FakeClient([temp_page, _weak_page("real")], [])
    out = _run(cfg, client, apply_changes=True, tmp_dir=tmp_path)
    touched = {c["id"] for c in client.live_calls}
    assert "tmp1" not in touched, "temporary-slug page must not be written"
    assert "real" in touched
    import json as _json
    assert "temporary-slug" not in _json.dumps(out["actions"])
    print("OK: test_temporary_slug_pages_excluded")


def test_is_publishable_page_helper():
    assert o._is_publishable_page(
        {"slug": "thousand-oaks", "currently_published": True}) is True
    assert o._is_publishable_page(
        {"slug": "-temporary-slug-deadbeef0000"}) is False
    assert o._is_publishable_page(
        {"slug": "ok", "archived": True}) is False
    assert o._is_publishable_page(
        {"slug": "ok", "currently_published": False}) is False
    print("OK: test_is_publishable_page_helper")


def test_preflight_no_network(tmp_path):
    # Ready: credentials + live-capable publish_mode.
    cfg = _accel_config()
    (tmp_path / o.DEFAULT_CONFIG_NAME).write_text(__import__("json").dumps(cfg))
    pf = o.preflight(tmp_path)
    assert pf["config_present"] is True
    assert pf["credentials_present"] is True
    assert pf["live_capable"] is True
    assert pf["expected_live_write_status"] == "live_capable_ready"
    assert pf["blocker"] is None

    # Missing token -> credentials_missing, no network attempted.
    cfg2 = _accel_config(token=None)
    (tmp_path / o.DEFAULT_CONFIG_NAME).write_text(__import__("json").dumps(cfg2))
    pf2 = o.preflight(tmp_path)
    assert pf2["credentials_present"] is False
    assert pf2["expected_live_write_status"] == "credentials_missing"
    assert "credentials_missing" in (pf2["blocker"] or "")

    # No config file at all.
    import tempfile
    empty = Path(tempfile.mkdtemp(prefix="cms_test_empty_"))
    pf3 = o.preflight(empty)
    assert pf3["config_present"] is False
    assert pf3["expected_live_write_status"] == "config_missing"
    print("OK: test_preflight_no_network")


def _mk_tmp(name: str) -> Path:
    import tempfile
    d = Path(tempfile.mkdtemp(prefix=f"cms_test_{name}_"))
    return d


if __name__ == "__main__":
    test_credentials_present_helper()
    test_publish_mode_live_capable()
    test_small_content_never_live()
    test_low_risk_metadata_exempt_from_cooldown()
    test_missing_credentials_yields_explicit_status(_mk_tmp("creds"))
    test_check_mode_is_dry_run(_mk_tmp("check"))
    test_weak_metadata_writes_live_without_gsc_signal(_mk_tmp("weaklive"))
    test_no_eligible_candidates_is_not_credentials_missing(_mk_tmp("noelig"))
    test_is_publishable_page_helper()
    test_temporary_slug_pages_excluded(_mk_tmp("tempslug"))
    test_preflight_no_network(_mk_tmp("preflight"))
    print("\nAll HubSpot CMS live-writeback tests passed.")
