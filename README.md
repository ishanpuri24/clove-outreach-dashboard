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
                                              v   fetch() at load
                                        index.html
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
    build_snapshot.py            sanitization-aware build script (writes data/snapshot.json)
    validate_public_snapshot.py  pre-publish validator (PII / shape)
  config/
    callrail.example.yaml        private CallRail connector template
    organic.example.yaml         private GA4 / GSC / Ahrefs connector template
    referrals.example.yaml       private GBP + Open Dental referral template
    subscribili.example.yaml     private Subscribili / Clove Care plan template
  README.md                      this file (purpose, architecture, basics)
  DEPLOYMENT.md                  GitHub Pages, Vercel, local, and snapshot-update guides
```

The dashboard renders by fetching `data/snapshot.json` at load with
`cache: "no-store"`, so a reload always reflects the latest commit.
`index.html` no longer carries an inline copy of the snapshot, which
keeps the HTML small (~220 KB) and means daily refreshes only need
to commit `data/snapshot.json` (~750 KB) — the HTML diff stays at
zero, GitHub Pages republishes a much smaller change, and reviews
are easy to scan.

> **Note on `file://`**: because the dashboard fetches the snapshot,
> opening `index.html` directly from disk (`file://`) will not load
> data in browsers that block `fetch()` for `file://`. Serve the
> repo over any static origin (GitHub Pages, Vercel, `python3 -m
> http.server`) — see [`DEPLOYMENT.md`](./DEPLOYMENT.md).

## Faster daily refresh workflow

For a routine data refresh (no UI changes):

```bash
# 1. Replace data/snapshot.json with the freshly sanitized snapshot
#    produced by the private builder.
# 2. Confirm the build script is idempotent and the validator passes.
python3 scripts/build_snapshot.py
python3 scripts/validate_public_snapshot.py

# 3. Commit only the data file and push.
git add data/snapshot.json
git commit -m "Refresh public snapshot"
git push
```

GitHub Pages rebuilds with a small diff (one JSON file), and the
existing `index.html` picks up the new data on the next page load.
Touch `index.html` only when the dashboard UI itself changes.

### Scheduled refresh orchestrator (self-deployable)

For routine daily refreshes driven by a scheduled task or a fresh
clone, use the dedicated orchestrator instead of `build_snapshot.py`.
It does **not** require a private builder, does not stage or send
outreach, and is safe to run with no connector credentials present:

```bash
# Defaults: fast mode + no-send. Reads private inputs from
# /home/user/workspace/cron_tracking/a3b9de2f if present.
python3 scripts/refresh_marketing_dashboard.py

# Override the private tracking dir (e.g. on a different host):
python3 scripts/refresh_marketing_dashboard.py --private-dir /path/to/private

# Dry-run (validate inputs, write nothing):
python3 scripts/refresh_marketing_dashboard.py --check

# Then validate before committing:
python3 scripts/validate_refresh_block.py
python3 scripts/validate_public_snapshot.py
```

What the orchestrator does:

- Re-stamps `data/snapshot.json::generated_at` and writes a compact
  `routine_refresh` block (mode, last-run timestamps, per-source
  status, pending-source list).
- Merges sanitized aggregates from the private tracking directory
  into `callrail_live.last_7_days` / `last_30_days`. Sources without
  fresh inputs stay at their last-known-good values and are listed
  in `routine_refresh.pending_sources`.
- Persists `daily_learning_state.json` (in the private dir) with the
  last refresh status, suppresses repeated recommendations by hash,
  and stores a summarised previous-metrics block.
- Never writes private IDs, tokens, raw reviews, patient or member
  records, phone numbers, GCLIDs, personal email addresses, config
  paths, scheduler IDs, or raw connector payloads into the public
  snapshot. A built-in sanitization sweep runs before write.
- Always runs `--no-send`; outbound outreach is **not** wired here.
  Passing `--allow-send` is intentionally a no-op and is logged.

The private inputs are **never committed** to this repo. Expected
files in the private tracking dir (all optional - missing files are
treated as `pending`):

| File | Used for |
| ---- | -------- |
| `callrail_7d_sanitized.json` | Live 7d CallRail aggregate |
| `callrail_30d_sanitized.json` | Live 30d CallRail aggregate |
| `daily_learning_state.json` | Repeat-recommendation suppression + CMS experiment log |
| `hubspot_cms_config.json` | HubSpot CMS token, publish_mode, safety tiers |
| `analytics_config.json`, `gmb_config.json`, `opendental_config.json`, `membership_config.json` | Connector status flags (read-only) |

The script is self-deployable: a fresh checkout on a new host needs
only Python 3.9+ and the public repo. If the private directory is
absent, the orchestrator still writes a valid public snapshot with
every source marked `pending` and exits 0.

### HubSpot CMS automation (low-risk, approval-policy controlled)

The orchestrator calls a companion script,
`scripts/hubspot_cms_optimizer.py`, once per run. It pulls a minimal
CMS inventory (site pages + landing pages, capped at 50 each — one
inventory call per run, low credit cost), cross-references the GSC
query/page rows already in the public snapshot, and proposes up to
3 low-risk metadata changes per daily run.

**Private config** lives at
`hubspot_cms_config.json` inside the private tracking directory.
The file is **never committed** and the token is never logged. It
declares two things the optimizer reads:

```jsonc
{
  "token": "pat-na1-...",                 // private; do not commit
  "publish_mode": "low_risk_metadata_writeback_allowed",
  "safety_tiers": {
    "auto_allowed": [
      "missing_or_weak_title_update",
      "missing_or_weak_meta_description_update",
      "private_experiment_log_update",
      "dashboard_sanitized_action_log_update"
    ],
    "approval_required": [
      "body_content_rewrite", "new_page_publish",
      "template_or_theme_change", "cta_or_form_change",
      "redirect_change", "domain_change",
      "script_or_source_code_change"
    ],
    "never_allowed_without_new_approval": [
      "billing_users_oauth_transactional_email",
      "functions_write", "domain_write"
    ]
  }
}
```

**Safety tiers**

| Tier | Examples | Behavior |
| ---- | -------- | -------- |
| Auto-allowed | Title or meta-description update on a page with weak metadata **and** a clear high-impression / low-CTR GSC signal | Saved as a HubSpot **draft** (unpublished) when `publish_mode = low_risk_metadata_writeback_allowed`. Otherwise dry-run only. |
| Approval-required | Body rewrites, template/theme, CTA/form, redirects, new-page publish | Never auto-applied. Optimizer skips them entirely in v1. |
| Never without new approval | Billing, users, OAuth, transactional email, functions, domain writes | Never touched, regardless of any flag. |

The optimizer enforces a per-page **cooldown** (default 14 days). A
page that received a change or a proposal does not re-enter the
candidate pool until the cooldown expires — this is what
prevents repeated metadata churn on the same slug.

**Run it**

```bash
# Dry-run only (does not call HubSpot writes):
python3 scripts/hubspot_cms_optimizer.py --check

# Apply low-risk metadata changes as drafts (default daily mode):
python3 scripts/hubspot_cms_optimizer.py --apply

# Cap to 1 change per run:
python3 scripts/hubspot_cms_optimizer.py --apply --max-changes 1

# The daily orchestrator invokes it automatically; force its CMS step
# into dry-run regardless of config:
python3 scripts/refresh_marketing_dashboard.py --cms-dry-run
```

**What lands in the public dashboard**

`data/snapshot.json::organic_cms_actions` is a compact, sanitized
action log. Each row carries only: a public slug + page title, the
change type, a short "why", a status (`applied_draft`, `proposed`,
or `error`), and a metric to watch (CTR / clicks / impressions for
28d). No HubSpot page IDs, portal IDs, tokens, config paths, or raw
API payloads ever reach the public mirror — both the optimizer and
the orchestrator scrub the block, and `validate_public_snapshot.py`
backstops every commit.

**What lands in the private learning state**

`daily_learning_state.json::cms_experiments.log` keeps the longer
trail: hypothesis, page, change types, date applied, baseline GSC
metric, metric to watch, status. This is the source of truth the
cooldown reads.

**What never happens in v1** (regardless of flags):

- No HubSpot body / template / theme / source-code writes.
- No new-page publish, no redirect change, no CTA/form change.
- No domain, function, billing, user, OAuth, or transactional-email
  writes.
- No `--apply` ever publishes a page; HubSpot drafts only.

## Data flow

1. The daily outreach run inside the private operations repo writes
   the raw run state and reply log.
2. The private builder produces an **unsanitized** snapshot in the
   private repo.
3. `scripts/build_snapshot.py::sanitize_for_public()` (a copy of
   which is published here for transparency) is applied to that
   snapshot.
4. The sanitized result is written to `data/snapshot.json`. The
   dashboard `index.html` fetches that file at runtime, so no
   re-injection or HTML edit is required for a data refresh.
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
- Google Ads Multi-Office Watch: aggregated 30-day spend, clicks,
  conversions, average CPC, and CPA across every linked office's
  paid search and Performance Max campaigns, with per-campaign risk
  flags and recommended actions. Manager-account and customer
  account identifiers are intentionally never exposed; offices
  appear by office label only.
- Google Ads action queue: P0 / P1 / P2 manual changes to make
  inside Google Ads. Each card names the office, campaign, the
  issue, supporting evidence, the exact manual change, the expected
  impact, and when to check back. Live writeback is intentionally
  not wired - operators apply the change inside Google Ads.
- Google Ads trends: last 7 days vs last month, normalized per day,
  for the rollup, by office, and by campaign. Spend / day,
  conversions / day, phone calls / day, CPA, CPC, conversion rate,
  and Improving / Worsening / Needs review / Stable / Noisy badges.
- CallRail call quality: aggregated qualified calls (CallRail
  `lead_status = good_lead`), first-time callers, answered/missed
  counts, qualified-call CPA where paid spend is attributable, plus
  per-office, per-campaign, and per-ad-group call quality and a
  missed-call leakage view. Only counts, rates, and labels appear in
  the public mirror - raw call records, caller phone numbers, names,
  emails, CallRail account/company/tracker IDs, GCLIDs, and recordings
  or transcripts are blocked at sanitization and re-checked by the
  validator. See [`docs/integrations/callrail.md`](./docs/integrations/callrail.md)
  and [`config/callrail.example.yaml`](./config/callrail.example.yaml)
  for the private-side config template.
- Keyword theme focus: protect-or-expand and tighten-or-pause
  themes, listed by office and campaign so the next inspection is
  obvious.
- Change tracker: documents the manual change-log workflow and the
  fields each operator change should record so the next refresh can
  grade Working / Worsening / Noisy. The connector reads reports and
  does limited writebacks (offline conversions, audience lists);
  campaign, budget, bid, ad, search-term, and negative-keyword edits
  are manual until a mutation-capable Google Ads tool/scope is
  added, and any future writeback must be explicitly approved.
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
- No email addresses of any kind may appear anywhere in
  `data/snapshot.json` or `index.html`. Operator inboxes are referred
  to with safe labels (`Connected Clove sender`, `Internal follow-up
  only`); the validator's email whitelist is empty by default.
- No `docs.google.com/spreadsheets/d/...` URLs, no Google Sheet IDs,
  no GitHub PATs, no API keys, no JWTs, no AWS access keys, and no
  `mailto:` links may appear anywhere in those files.
- No Google Ads manager or customer account identifiers (dashed
  `NNN-NNN-NNNN` or undashed 10-digit API form) may appear anywhere
  in `data/snapshot.json` or `index.html`.
- `google_ads_insights` is required and must include `title`,
  `lookback`, `data_freshness`, `automation_status`, `coverage`,
  `totals`, `risk_summary`, `campaign_groups`, `campaigns`,
  `recommended_actions`, `operator_notes`, `manual_action_queue`,
  `trends`, and `change_tracking`. Account-id keys
  (`manager_customer_id`, `customer_id`, `account_id`, etc.) are
  rejected at both the top level and inside `campaign_groups[]`,
  `manual_action_queue[]`, and `trends.by_office[]` /
  `trends.by_campaign[]`. `coverage.office_label_policy` must
  explicitly state that office mapping is pending until the
  remaining customer IDs are linked. Each `manual_action_queue[]`
  row must carry `priority` (P0/P1/P2/P3), `office`, `campaign`,
  `issue`, a non-empty `evidence` list, `manual_change`,
  `expected_impact`, `check_after`, and `status`. `trends.rollup`
  must contain `last_7_days` and `last_month` with per-day metrics
  (`spend_per_day`, `conversions_per_day`, `cpa`, `avg_cpc`,
  `ctr_pct`, `conversion_rate_pct`).

The validator (see below) enforces all of the above on every run.

## Local validation

The validator has no third-party dependencies. From the repo root:

```sh
python3 scripts/validate_public_snapshot.py
```

### Minimal local refresh

```sh
python3 scripts/build_snapshot.py        # re-inject sanitized snapshot into index.html
python3 scripts/validate_public_snapshot.py  # confirm safe to publish
python3 -m http.server 8000              # spot-check rendered page at http://localhost:8000
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

## Material-change workflow (mandatory)

Any **material** dashboard or data change - new snapshot, copy
edits to operator-facing language, layout changes that move actions
above or below the fold, schema additions, sanitization changes -
must end in a commit pushed to `main`. Conversation-only edits do
not count as durable; if it is not in git, it is not deployed.

The contract is:

1. Edit `data/snapshot.json`, `index.html`, `scripts/`, or any
   docs file as needed.
2. Run `python3 scripts/build_snapshot.py` to re-inject the
   sanitized snapshot into `index.html`.
3. Run `python3 scripts/validate_public_snapshot.py`. A non-zero
   exit code means the change is **not safe to ship**: fix the
   sanitization upstream, do not silence the validator.
4. Spot-check the rendered page locally (`python3 -m http.server
   8000`) before pushing.
5. Commit with a short imperative message and push to `origin
   main`. GitHub Pages and any other static origin redeploy
   automatically on push.

A GitHub Actions workflow at
`.github/workflows/validate.yml` runs the validator on every push
and pull request to `main`. The workflow has no secrets, no
external network calls, and no deploy step; it only verifies that
the public mirror is still safe to publish. If the workflow fails,
do not merge or deploy.

## Deploying

See [`DEPLOYMENT.md`](./DEPLOYMENT.md) for full instructions for:

- GitHub Pages (recommended for the canonical public URL).
- Vercel (one-click import).
- Local browsing with no server.
- Any other static host (Netlify, Cloudflare Pages, S3, nginx).

## Operating rules surfaced in the UI

- Maximum 12 one-to-one new emails per weekday.
- Initial outreach sent only from the connected Clove sender (the
  specific operator inbox is redacted in the public mirror).
- No CC and no BCC on initial outreach. An internal follow-up inbox
  is used only after a positive reply or scheduling/ops handoff; that
  address is redacted in the public mirror.
- No auto-reply to interested prospects; the operator drafts each
  reply.
- Suppress bounces, opt-outs, and not-interested permanently.
- Public verified emails only; never invent or guess address
  patterns.
- Google Sheet remains the private source of truth. Zoho writeback
  is staged behind dedupe and a dry-run period.

## Automations: Marketing dashboard

The Marketing dashboard's **Automations** tab is the first/default
tab. It surfaces a sanitized, aggregate-only view of operator-side
automations: a Send readiness summary (backlog, eligible, cadence,
per-office split, writeback behavior, where results appear), the
Google Ads lead SMS follow-up status, an OptimizationOS / win-back
reporting table, and a provider connectivity card. The lead SMS
loop checks daily and can backfill uncontacted leads now; it can
also run hourly for fast response.

### Google Ads lead SMS follow-up

`scripts/lead_sms_automation.py` drives the loop. It:

- Reads a **private** operator config (path supplied via `--config`
  or `LEAD_SMS_CONFIG`). The private config holds the spreadsheet
  id, OpenPhone API credentials for the **Optimization line**, the
  marketing-line `phone_number_id`, per-office booking links, and
  send-policy flags. The path lives outside this public repo.
- Scans every lead-shaped tab in the Google Ads Leads Tracker.
- Dedupes uncontacted leads by `(normalized phone, office,
  source_type)`.
- Excludes obvious sample/test rows.
- Refreshes the public Automations snapshot block with aggregate
  counts only.
- **Never sends an SMS by default.** Real sends require *all four*
  gates: `openphone.enabled=true`, `send_policy.enabled=true`,
  `--apply`, and `--i-understand-i-am-sending-real-sms`. If any
  gate is missing, the script falls back to dry-run.

### OpenPhone raw-auth gotcha

OpenPhone's REST API expects the API key as a **raw value** in the
`Authorization` header, *not* as `Bearer <key>`. The adapter in
`scripts/lead_sms_automation.py` builds the header by hand to avoid
any SDK or `requests.auth` helper that prefers `Bearer`. Watch for
this when swapping in a new credential: a 401/403 with the message
"authentication failed" usually means a `Bearer` prefix slipped in.

### Google Sheets access via the external-tool CLI

`scripts/lead_sms_automation.py` does **not** depend on
`google-api-python-client`. The operator host runs the script with
`api_credentials=["external-tools"]` and shells out to the
`external-tool` CLI for every Sheets call. The three tools used are:

- `google_sheets-get-spreadsheet-info` — list lead-shaped tabs.
- `google_sheets-get-values`           — read a tab as a 2D array.
- `google_sheets-update-row`           — write one cell on writeback.

All three invocations pass the spreadsheet id as part of a single
JSON tool-arguments blob, e.g.

```sh
external-tool call '{
  "source_id":"google_sheets__pipedream",
  "tool_name":"google_sheets-get-spreadsheet-info",
  "tool_arguments":{"spreadsheetId":"<from private config>"}
}'
```

The spreadsheet id is loaded from the private `lead_sms_config.json`
(or the `LEAD_SMS_CONFIG` env var). It is **never** hard-coded or
committed, and never echoed to the public snapshot.

### Modes

```sh
# Default - safe scan, no sends, refreshes public snapshot.
# Requires api_credentials=["external-tools"] in the harness/cron.
python3 scripts/lead_sms_automation.py --dry-run \
    --config /path/to/private/lead_sms_config.json

# Read-only provider connectivity probe (GET only, sanitized output)
python3 scripts/lead_sms_automation.py --check \
    --config /path/to/private/lead_sms_config.json

# Apply (only when provider + policy + flag all set).
# Sends capped SMS via OpenPhone Optimization line and stamps the
# matching sheet row (Contacted = YES; AI SMS Sent At / Status /
# Notes if those columns already exist on the tab).
python3 scripts/lead_sms_automation.py --apply \
    --i-understand-i-am-sending-real-sms \
    --config /path/to/private/lead_sms_config.json
```

The loop checks daily and can backfill uncontacted leads now. It
can also run hourly for fast response. Example scheduled task on
the operator machine (replace placeholder path with your private
config location). The cron environment must have `api_credentials`
including `"external-tools"` and the `external-tool` binary on
`PATH`:

```cron
17 * * * * cd /path/to/clove-outreach-dashboard && \
    LEAD_SMS_CONFIG=/path/to/private/lead_sms_config.json \
    python3 scripts/lead_sms_automation.py --dry-run >> \
    /var/log/clove-lead-sms.log 2>&1
```

The scheduled task stays in dry-run until the operator explicitly
flips `send_policy.enabled=true` and `openphone.enabled=true` in
the private config and switches the cron command to `--apply
--i-understand-i-am-sending-real-sms`.

### Needs-human reply escalation

Inbound replies that the classifier cannot route automatically are
surfaced as an aggregate-only `needs_human` count on the
Automations dashboard. There is **no email-to-Aryaan path**. A
Trello card is queued only when the private config contains
`escalation.trello.enabled=true` and `escalation.trello.list_id`;
otherwise the count is the only output. The Trello call also goes
through the `external-tool` CLI.

### Sample SMS templates

**Google Ads lead (new contact, STOP required):**

```
Hi [First name], this is Clove Dental [Office]. You filled out a
form for an appointment, and we have real-time openings available
today. You can book here: [office booking link]. If you want help,
reply with what time works. Reply STOP to opt out.
```

**Established-patient optimization SMS (separate rules):**
no STOP keyword, no emoji, no phone number in the body, ask the
patient to reply by text (not call), and refer to Sherman Oaks
(never Studio City).

### Sheet writeback behavior

When a send actually happens (apply mode, all gates satisfied), the
script writes back to the same Google Sheet row via the
`google_sheets-update-row` external tool:

- `Contacted Yes/No` (existing header) → `YES`.
- `AI SMS Sent At`, `AI SMS Status`, `AI SMS Notes` → stamped if the
  three columns exist on the tab. The script does **not** add
  columns automatically — that would risk breaking the existing
  multiline `Contacted / Followed Up / Booked / Treatment` formula
  layout. If any of the three columns is missing the run logs a
  single aggregate blocker on the dashboard and writes only the
  `Contacted` column. Add the three columns to enable per-row
  feedback writeback.

Writes are idempotent: a row whose `AI SMS Status` already reads
`sent`/`yes` is skipped on subsequent runs. No writes happen during
`--dry-run` or `--check`.

If apply mode runs and produces zero sends from a non-empty eligible
backlog, the script surfaces an aggregate-only blocker naming the top
skip reason (e.g. `missing_booking_link` for offices without an entry
in `office_booking_links`, or `provider_<status>` for an OpenPhone
non-success response). The blocker never includes phone numbers,
names, or row numbers — it points the operator at the underlying
private-config gap.

### Safety summary

| Guard | State |
|-------|-------|
| Default mode | `--dry-run` (no SMS sent, no sheet writes) |
| Provider adapter | OpenPhone stub, `enabled=false` in private config |
| Send-policy flag | `send_policy.enabled=false` until operator flips it |
| Apply mode confirmation | Requires `--i-understand-i-am-sending-real-sms` |
| Public snapshot | Aggregate-only; validator rejects PII keys |
| Spreadsheet id | Read from private config; never embedded in the public repo |
| Booking links | Read from private config; never embedded in the public mirror |

### Public snapshot contract for automations

The validator (`scripts/validate_public_snapshot.py`) enforces the
following on the `automations` block, in addition to the global
forbidden-pattern checks (emails, phones, Sheet IDs, Google Ads
account ids, etc.):

- Only the allowed keys per item are permitted (`id`, `name`,
  `purpose`, `status`, `provider`, `provider_status`,
  `send_policy_enabled`, `apply_mode`, `last_run_at_utc`,
  `counters`, `by_office`, `by_source`, `sample_template_public`,
  `compliance_notes`, `blockers`).
- `counters` must be numeric on every field.
- Person-name keys (`first_name`, `last_name`, `patient_name`,
  `lead_name`), phone fields, email fields, row numbers, sheet
  ids, raw messages, provider credentials, and booking links are
  rejected at any depth inside the block.

## License

This mirror is published for transparency around the campaign's
guardrails and aggregate performance. The dashboard code is provided
as-is for reference. Source data is private.
