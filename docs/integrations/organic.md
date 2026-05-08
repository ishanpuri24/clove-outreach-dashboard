# Organic / SEO integration

The **Organic** tab in the public dashboard summarizes organic search
performance from three connectors. None are connected to this public
mirror; the tab renders compact connection-status cards and
placeholder metric grids until the upstream builder populates
`organic_insights` in `data/snapshot.json`.

## Connectors

- **Google Analytics 4** — sessions, users, conversions by
  source/medium and landing page (aggregated only).
- **Google Search Console** — clicks, impressions, CTR, average
  position by query and page (aggregated only).
- **Ahrefs** — keyword rankings, ranking changes, referring domains,
  backlinks, domain rating (aggregated only).

## Public-mirror contract

The public mirror only ever publishes aggregated metrics. The
following must never appear in `data/snapshot.json` or `index.html`:

- GA4 property IDs, measurement IDs, client IDs, or user IDs.
- Service-account email addresses, private keys, or refresh tokens.
- Verified GSC site URLs or properties when they are private.
- Ahrefs API tokens.
- Per-user paths, search queries containing PII, or any raw event
  rows.

See [`config/organic.example.yaml`](../../config/organic.example.yaml)
for the private-side config template. Copy that template into the
private operations repo only.

## Snapshot shape

The `organic_insights` object in `data/snapshot.json` carries the
following safe schema:

- `connector_status[]` — `{ integration, status, severity,
  public_exposure, private_config_fields, action }`.
- `summary_cards[]` — `{ label, value, basis, decision }`.
- `top_actions[]` — `{ priority, label, action, owner }`.
- `ga_source_medium_rows[]`, `ga_landing_page_rows[]` (aggregates).
- `gsc_query_rows[]`, `gsc_page_rows[]` (aggregates).
- `ahrefs_keyword_rows[]`, `ahrefs_ranking_change_rows[]`,
  `ahrefs_backlinks_summary` (aggregates).
- `seo_opportunity_actions[]` — `{ priority, action, target,
  expected_impact }`.
- `freshness_note` — one short line shown at the foot of the tab.

When the connectors are linked, the upstream builder fills these
arrays with sanitized aggregates; the dashboard renders them as
labels, values, status chips, and action chips with no long prose.
