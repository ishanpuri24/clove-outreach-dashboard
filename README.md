# Clove Dental Outreach Dashboard (Public Mirror)

A static, mobile-first operations dashboard summarizing the Clove
Dental patient-acquisition outreach automation. This repository is
the **public, sanitized mirror** of the dashboard. It is sourced
from a private operations repository and contains only aggregate
metrics, non-sensitive category and status summaries, and operating
guardrails.

The dashboard is intentionally a single `index.html` file with
inline CSS, JavaScript, and SVG. It has no runtime dependencies, no
build step, no package manager, and no server. It can be hosted on
any static origin (GitHub Pages, Vercel, Netlify, Cloudflare Pages,
S3, an internal nginx) or opened directly from disk by
double-clicking `index.html`.

This repository is designed to be **self-deployable and
importable**. Anyone with read access to the repo can clone it and
publish the dashboard without coordinating with the original author.
See [`DEPLOYMENT.md`](./DEPLOYMENT.md) for step-by-step instructions.

## Purpose

- Provide a transparent, public-facing view of how the Clove Dental
  outreach campaign is performing in aggregate.
- Document the operating guardrails the campaign runs under so they
  are visible to every stakeholder, not buried in an internal sheet.
- Be portable enough that the dashboard survives loss of access to
  the original chat session, the original operator, or the private
  operations repository. Any future operator can fork or import this
  repository and continue publishing snapshots.

## Architecture

```
   private operations repo                public mirror (this repo)
   ----------------------                 -------------------------

   Google Sheet (source of truth)
            |
            v
   daily-outreach scheduled task
            |
            v
   private build_snapshot.py (raw)
            |
            v   sanitize_for_public()
            +-------------------------> data/snapshot.json
                                              |
                                              v
                                        index.html
                                        (inline embed)
                                              |
                                              v
                                  GitHub Pages / Vercel / static host
```

The Google Sheet remains the **private source of truth**. The
private operations repository owns the daily run, raw data, and
unsanitized outputs. This public mirror only stores the sanitized
aggregate snapshot.

## Files

```
/
  index.html                     single-file dashboard (inline JS/CSS/SVG)
  data/
    snapshot.json                machine-readable sanitized snapshot
  scripts/
    build_snapshot.py            sanitization-aware build / re-inject script
    validate_public_snapshot.py  pre-publish validator (PII / shape / parity)
  README.md                      this file (purpose, architecture, basics)
  DEPLOYMENT.md                  GitHub Pages, Vercel, local, and snapshot-update guides
```

The dashboard works in two complementary ways:

1. The same JSON is **embedded inline** in `index.html` between the
   `/* SNAPSHOT_START */` and `/* SNAPSHOT_END */` markers, so the
   page renders even when opened directly from the filesystem
   (`file://`) where `fetch()` against `data/snapshot.json` would
   otherwise be blocked.
2. When served over HTTP, `index.html` also tries to `fetch()`
   `data/snapshot.json` with `cache: "no-store"` so reload reflects
   the latest snapshot without a hard refresh.

The two copies must always match. `scripts/build_snapshot.py`
re-injects the JSON into `index.html` after every sanitization, and
`scripts/validate_public_snapshot.py` enforces the parity.

## Data flow

1. The daily outreach run inside the private operations repo writes
   the raw run state and reply log.
2. The private builder produces an **unsanitized** snapshot in the
   private repo.
3. `scripts/build_snapshot.py::sanitize_for_public()` (a copy of
   which is published here for transparency) is applied to that
   snapshot.
4. The sanitized result is written to `data/snapshot.json` and
   re-injected between the markers in `index.html`.
5. `scripts/validate_public_snapshot.py` is run before commit to
   reject any snapshot that still contains forbidden patterns.
6. The repo is committed and pushed; the deployment target (GitHub
   Pages or Vercel) picks up the new commit automatically.

## What this dashboard shows

- KPIs: total sends, weekdays run, latest cap usage, reply signals,
  positive replies, bounces, reply rate, positive rate.
- Daily trend: sends per weekday, replies per day, cumulative sends.
- Reply mix: distribution across Positive / Neutral / Bounce buckets.
- Channel mix: latest-batch and cumulative campaign category mix
  (senior living, home care, caregiver resources, health-adjacent,
  property, faith/community, schools, chambers, other).
- Channel scorecard: per-channel sends, replies by bucket, tier,
  confidence, and signal label, with a short qualitative note.
- Google Ads Waste Watch: aggregated 30-day spend, clicks,
  conversions, average CPC, and CPA across the linked office's paid
  search and Performance Max campaigns, with per-campaign risk
  flags and recommended actions. Manager-account and customer
  account identifiers are intentionally never exposed; offices
  appear under a mapping-pending placeholder until the remaining
  customer IDs are linked. A data-freshness timestamp is shown and
  the section is included in the consolidated weekday patient-
  acquisition operating loop, with dashboard-only recommendations
  and no Google Ads changes made without explicit confirmation.
- Experiment backlog and queue health with sourcing goals and
  warnings.
- Operator follow-up queue, redacted to action and channel only.
- Guardrail status with evidence per rule.
- Focus priority tiers (tier-1 senior/caregiver, tier-2
  health-adjacent, deprioritized).

## What this dashboard does NOT show

The public mirror redacts everything that could expose individual
prospects or internal operational data:

- No raw email bodies.
- No prospect or recipient email addresses.
- No prospect names.
- No reply-sender names or addresses.
- No free-text reply summaries or per-reply suggested next actions.
- No internal Google Sheet ID or URL.
- No private operations-repo commit hashes.
- No tokens, API keys, or other credentials.
- No `mailto:` links to private prospects.
- No Google Ads manager-account or customer-account identifiers
  (neither the dashed `NNN-NNN-NNNN` form nor the undashed 10-digit
  API form). Office labels surface only as a mapping-pending
  placeholder; campaign names are the safest grouping shown in the
  Google Ads Waste Watch section.

Each `replies` entry in `data/snapshot.json` is reduced to its date,
category, classification bucket, and status. The latest batch is
exposed only as a size and an aggregated channel mix.

## Sanitization rules

The contract enforced by the validator is:

- `sources.sheet_url` and `sources.sheet_id` must be redacted
  placeholders, never real values.
- `replies[]` may contain only `Date`, `Organization` (set to the
  redacted placeholder), `Category`, `Classification`, `Status`, and
  `Bucket`. Fields named `Email From`, `Summary`,
  `Suggested Next Action`, `Owner`, `Body`, or `Reply Body` are
  rejected.
- `latest_batch` (recipient-level rows) must not appear; only
  `latest_batch_summary` with `size` and an aggregate `note`.
- `github` must not include `latest_commit_before_dashboard`,
  `dashboard_build_commit`, or `repo`.
- No email addresses other than the documented operator senders
  (`ip@clovedds.com`, `aryaan@clovedds.com`) may appear anywhere in
  `data/snapshot.json` or `index.html`.
- No `docs.google.com/spreadsheets/d/...` URLs, no Google Sheet IDs,
  no GitHub PATs, no API keys, no JWTs, no AWS access keys, and no
  `mailto:` links may appear anywhere in those files.
- No Google Ads manager or customer account identifiers (dashed
  `NNN-NNN-NNNN` or undashed 10-digit API form) may appear anywhere
  in `data/snapshot.json` or `index.html`.
- `google_ads_insights` is required and must include `title`,
  `lookback`, `data_freshness`, `automation_status`, `coverage`,
  `totals`, `risk_summary`, `campaign_groups`, `campaigns`,
  `recommended_actions`, and `operator_notes`. Account-id keys
  (`manager_customer_id`, `customer_id`, `account_id`, etc.) are
  rejected at both the top level and inside `campaign_groups[]`.
  `coverage.office_label_policy` must explicitly state that office
  mapping is pending until the remaining customer IDs are linked.

The validator (see below) enforces all of the above on every run.

## Local validation

The validator has no third-party dependencies. From the repo root:

```sh
python3 scripts/validate_public_snapshot.py
```

It checks that:

- `data/snapshot.json` parses as JSON.
- All required operator sections and KPI fields are present and
  KPI metrics are numeric.
- The inline embedded snapshot in `index.html` parses and matches
  `data/snapshot.json` byte-for-byte (after JSON normalization).
- None of the forbidden sensitive patterns above appear in either
  file.

A non-zero exit code means the snapshot is **not safe to publish**.
Resolve every reported finding, re-run the build script, and
re-validate before committing.

## Updating the snapshot safely

The full procedure is in [`DEPLOYMENT.md`](./DEPLOYMENT.md). The
short version:

1. Produce a new sanitized snapshot in the private operations repo.
2. Copy the sanitized JSON into `data/snapshot.json` in this repo.
3. Run `python3 scripts/build_snapshot.py` to re-inject the JSON
   into `index.html` between the `SNAPSHOT_START` / `SNAPSHOT_END`
   markers.
4. Run `python3 scripts/validate_public_snapshot.py`. Do not commit
   if it fails.
5. Open `index.html` in a browser and visually confirm the page
   renders correctly with the new data.
6. Commit and push. The deployment target redeploys automatically.

## Deploying

See [`DEPLOYMENT.md`](./DEPLOYMENT.md) for full instructions for:

- GitHub Pages (recommended for the canonical public URL).
- Vercel (one-click import).
- Local browsing with no server.
- Any other static host (Netlify, Cloudflare Pages, S3, nginx).

## Operating rules surfaced in the UI

- Maximum 12 one-to-one new emails per weekday.
- Initial outreach sent only from `ip@clovedds.com`.
- No CC and no BCC on initial outreach. `aryaan@clovedds.com` is
  CC'd only after a positive reply or scheduling/ops handoff.
- No auto-reply to interested prospects; the operator drafts each
  reply.
- Suppress bounces, opt-outs, and not-interested permanently.
- Public verified emails only; never invent or guess address
  patterns.
- Google Sheet remains the private source of truth. Zoho writeback
  is staged behind dedupe and a dry-run period.

## License

This mirror is published for transparency around the campaign's
guardrails and aggregate performance. The dashboard code is provided
as-is for reference. Source data is private.
