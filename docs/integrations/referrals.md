# Referrals integration

The **Referrals** tab summarizes referral signals from two sources.
Neither is connected to this public mirror; the tab renders a
compact connection-status card and a 9-office placeholder grid until
the upstream builder populates `referral_insights` in
`data/snapshot.json`.

## Connectors

- **Google Business Profile (GBP)** — per-office profile calls,
  website clicks, direction requests, profile views, profile
  searches, reviews count, and average rating (aggregated only).
- **Open Dental referral query** — aggregated counts of referrals by
  source and office. No connector is currently registered; the
  upstream operator must define a private SQL query that returns
  aggregate counts only.

## Public-mirror contract

The public mirror only ever publishes aggregated metrics and office
labels. The following must never appear in `data/snapshot.json` or
`index.html`:

- GBP account IDs or location resource IDs.
- Reviewer names, review text, photos, or any per-user data.
- Open Dental DSNs, schemas, table names, or query text.
- Patient names, phone numbers, email addresses, dates of birth,
  appointment notes, treatment plans, or any other PHI.
- Raw query results or row-level joins between referrals and
  patients.

See [`config/referrals.example.yaml`](../../config/referrals.example.yaml)
for the private-side config template. Copy that template into the
private operations repo only.

## Snapshot shape

The `referral_insights` object in `data/snapshot.json` carries the
following safe schema:

- `connector_status[]` — `{ integration, status, severity,
  public_exposure, private_config_fields, action }`.
- `summary_cards[]` — `{ label, value, basis, decision }`.
- `top_actions[]` — `{ priority, label, action, owner }`.
- `office_rows[]` — `{ office, profile_calls, website_clicks,
  direction_requests, profile_views, profile_searches, reviews,
  rating, status, action }`. One row per office; nine offices
  total.
- `query_rows[]` — `{ label, value, basis }` for the Open Dental
  referral aggregate counts.
- `freshness_note` — one short line shown at the foot of the tab.

When the connectors are linked and the private referral query is
defined, the upstream builder fills these arrays with sanitized
aggregates. The dashboard renders them as labels, values, status
chips, and action chips with no long prose.
