# CallRail call-conversion enrichment

This document describes how the **private** operations repo populates
the `google_ads_insights.callrail_call_quality` section of the public
snapshot. The public mirror itself stores only sanitized aggregates;
the API token, account scope, and raw call records never enter this
repo.

The dashboard ships a scaffolded `callrail_call_quality` block (see
`data/snapshot.json`) so the UI renders out-of-the-box. A private
operator replaces the fixture rows with real aggregates after running
the private builder against the CallRail Calls API v3.

## What the public mirror exposes

Only **counts, rates, and labels**. Specifically:

- Per-office, per-campaign, and per-ad-group call quality:
  total calls, qualified calls, qualified rate, first-time callers,
  missed calls, and (where attributable) qualified-call CPA.
- Aggregate summary cards for the last 7 days (qualified calls,
  qualified rate, first-time callers, answered calls, missed calls,
  qualified-call CPA).
- A call-outcome breakdown (qualified / not-a-lead / pending /
  missed) with counts and shares.
- A missed-call leakage view with totals and per-office peak windows.
- A `lead_status_legend` explaining which CallRail `lead_status`
  values count as qualified.

## What the public mirror MUST NEVER expose

The validator (`scripts/validate_public_snapshot.py`) rejects any of
the following in `data/snapshot.json` or `index.html`:

- Raw phone numbers in any format (NANP regex enforced).
- Caller names, emails, country/city/state/zip, or any other
  caller-side PII.
- CallRail `account_id`, `company_id`, `tracker_id`, or any other
  CallRail private identifier.
- Google Ads `customer_id` / `manager_customer_id` (already enforced).
- `gclid`, `gbraid`, `wbraid`, `fbclid` or other click identifiers.
- Recording URLs, recording bytes, transcripts, or Conversation
  Intelligence text.
- Tokens, API keys, JWTs, or AWS access keys.
- Raw call records (anything that looks like a CallRail `Call`
  resource - see API reference below).

The validator scans the snapshot and the rendered HTML for these
patterns; any match aborts the publish.

## CallRail API v3 reference

Base URL: `https://api.callrail.com/v3/a/{account_id}/`

Authentication: `Authorization: Token token="..."` header. The token
must come from a CallRail user with read access to the relevant
companies. **Never commit the token to this repo.**

Primary endpoint used: `GET /a/{account_id}/calls.json`

Useful query parameters:

- `start_date` / `end_date` (e.g. `2026-04-29` ... `2026-05-05`)
- `company_id` (scope to one office or business unit)
- `fields` (comma-separated extra fields - see below)
- `per_page` (max 250)
- `answered=true|false`
- `direction=inbound|outbound`

Selected `Call` resource fields that the **private** builder reads
(none of these reach the public mirror as raw values):

| Field | Public mirror surfaces it as |
| --- | --- |
| `answered` | `answered_calls` count, `answered_rate_pct` |
| `duration` | dropped (not aggregated in public scaffold) |
| `direction` | filter only - inbound calls counted |
| `company_id` | mapped to `office` label |
| `source` / `campaign` / `medium` | mapped to channel/campaign label |
| `utm_*` | filter only - identifies paid traffic |
| `gclid` | filter only - never written to public mirror |
| `keyword` / `keyword_id` | aggregated to `keyword_focus` themes |
| `first_call` | `first_time_callers` count |
| `prior_calls` / `total_calls` | filter only - identifies repeats |
| `lead_status` | qualification - **only `good_lead` qualifies** |
| `tags` | aggregated to `recommended_action` strings |
| `call_highlights` (CI) | dropped - never written to public mirror |
| `customer_phone_number` | dropped - never written to public mirror |
| `customer_name` | dropped - never written to public mirror |

The full schema is documented at
[apidocs.callrail.com](https://apidocs.callrail.com/).

## Lead-status qualification rule

A call counts as **qualified** in this dashboard only when CallRail
`lead_status` is `good_lead`. Calls with `lead_status` of
`not_a_lead`, `unknown`, or any other value are excluded from the
qualified totals. Calls awaiting an operator decision in CallRail
sit in the `unknown` bucket and are counted as **pending**, not
qualified, until tagged.

This matches CallRail's own qualified-lead definition; tighten or
relax it inside the **private** builder, never inside the public
mirror.

## Private config fields (placeholders only)

The private operations repo must define the following environment
variables. None of them - and none of the values they resolve to -
should ever enter this repository.

```
CALLRAIL_API_TOKEN          # CallRail API v3 token (account-level)
CALLRAIL_ACCOUNT_ID         # CallRail account ID
CALLRAIL_COMPANY_IDS        # Comma-separated company IDs to ingest
CALLRAIL_LOOKBACK_DAYS      # Default 7
CALLRAIL_DEFAULT_TIMEZONE   # e.g. America/Los_Angeles
GOOGLE_ADS_CUSTOMER_IDS     # Comma-separated paid-ads customer IDs
GOOGLE_ADS_DEVELOPER_TOKEN  # Google Ads API developer token
GOOGLE_ADS_LOGIN_CUSTOMER_ID
GOOGLE_ADS_REFRESH_TOKEN
```

A YAML stub of the same shape lives at
[`config/callrail.example.yaml`](../../config/callrail.example.yaml).
Copy it to `config/callrail.yaml` (which is already gitignored via
the public mirror's `.gitignore` policy) inside the **private** repo
only.

## Aggregation pipeline (private)

The private builder follows this shape; only the **last** step
touches this public repo.

1. Pull last-7-day calls from `/calls.json` for each company. Use
   `fields=lead_status,first_call,answered,utm_source,utm_medium,utm_campaign,gclid,keyword,company_id`.
2. Join each call to a Google Ads campaign / ad-group via the
   `gclid` (preferred) or `utm_campaign` + `utm_term` (fallback).
3. Aggregate to per-office, per-campaign, and per-ad-group counts.
4. Compute qualified-call CPA only when paid-ads spend is fully
   attributable to the call (require a `gclid` join). Otherwise
   surface `qualified_cpa_usd: 0.0` and the public renderer prints
   `spend not attributable` instead of a misleading number.
5. Build the `callrail_call_quality` block exactly as defined in
   `scripts/validate_public_snapshot.py::REQUIRED_CALLRAIL_TOP_KEYS`.
6. Run the public mirror's
   `scripts/build_snapshot.py` and `scripts/validate_public_snapshot.py`
   before commit.

Any change to the schema must be made in lock-step with the
validator's allowed-key sets in `scripts/validate_public_snapshot.py`.
