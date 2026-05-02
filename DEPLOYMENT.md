# Deployment and Import Guide

This guide makes the public Clove Dental outreach dashboard
self-deployable. Anyone with read access to this repository can
publish it without coordinating with the original author or the
private operations repository.

The dashboard is a single static `index.html` plus a sanitized
`data/snapshot.json`. There is **no build step, no package manager,
no server, no environment variables, and no secrets** required to
host it.

## Prerequisites

- Git, plus either `gh` (GitHub CLI) or browser access to GitHub.
- Python 3.9+ to run the validator and the optional re-inject
  script. No virtualenv or `pip install` is required; both scripts
  use only the Python standard library.
- A modern browser to spot-check the rendered page.

## Quick local preview

The dashboard renders fine when opened directly from disk because
the snapshot is also embedded inline in `index.html`. From the repo
root:

```sh
# Option A: open the file directly. The inline snapshot is used.
xdg-open index.html        # Linux
open index.html            # macOS
start index.html           # Windows

# Option B: serve the directory over HTTP so the runtime fetch of
# data/snapshot.json also exercises (recommended for QA).
python3 -m http.server 8000
# then visit http://localhost:8000/
```

Use Option B before publishing a new snapshot. It confirms both the
inline copy and the `fetch()` path render identically.

## Deploying to GitHub Pages

This is the recommended canonical deployment because it produces a
stable public URL tied to the repository itself.

1. Push this repository to GitHub (`main` branch).
2. In the repository settings, go to **Settings -> Pages**.
3. Under **Build and deployment**, set:
   - Source: **Deploy from a branch**
   - Branch: **main**
   - Folder: **/ (root)**
4. Click **Save**. GitHub builds and serves the site within a
   minute or two.
5. The public URL will be
   `https://<owner>.github.io/<repo>/`. For this repo the
   canonical URL is
   `https://ishanpuri24.github.io/clove-outreach-dashboard/`.
6. Every subsequent push to `main` redeploys automatically. There
   is no GitHub Actions workflow to maintain.

If you fork the repo, your fork will publish to
`https://<your-account>.github.io/clove-outreach-dashboard/` once
you enable Pages in your fork's settings.

## Importing into Vercel

Vercel recognizes this repo as a static site without any
configuration.

1. Sign in to https://vercel.com and click **Add New -> Project**.
2. Choose **Import Git Repository** and select this repository (or
   your fork). Vercel will request read access to the repo.
3. On the configuration screen:
   - **Framework Preset**: **Other** (the project is plain static).
   - **Build Command**: leave **empty** (do not run any command).
   - **Output Directory**: set to `.` (the repository root).
   - **Install Command**: leave **empty**.
   - **Environment Variables**: none required.
4. Click **Deploy**. Vercel publishes the static site directly.
5. Subsequent commits to `main` trigger an automatic redeploy.
   Pull-request branches get preview deployments.

If Vercel ever prompts for a framework, choose **Other / Static**.
Do not enable any serverless function runtime: this site does not
need one.

## Deploying to other static hosts

The same pattern applies to any static origin. Upload the
repository contents (or just `index.html` and `data/snapshot.json`)
to the host and serve them with default static-file MIME types.
Tested-friendly hosts:

- **Netlify**: drag-and-drop the repo folder onto the Netlify dashboard,
  or connect the Git repo with build command empty and publish
  directory `.`.
- **Cloudflare Pages**: connect the repo, build command empty,
  output directory `/`.
- **AWS S3 + CloudFront**: `aws s3 sync . s3://<bucket>` excluding
  `.git`, `scripts`, and `*.md`. Set `index.html` as the index
  document.
- **Internal nginx / Apache**: copy `index.html` and `data/` into
  the document root.

There are no rewrites, redirects, or headers required. The dashboard
sets its own `Cache-Control: no-store` on the `fetch` of
`snapshot.json`, so the host's default caching is fine.

## Updating the snapshot safely

This is the most sensitive operation in the public mirror. Follow
the steps in order. Do not skip the validator.

1. **Generate a fresh sanitized snapshot in the private operations
   repository.** The private builder applies
   `sanitize_for_public()` and emits a JSON document that matches
   the schema of `data/snapshot.json`. Never hand-edit a snapshot
   that was produced from raw run state.
2. **Copy the sanitized JSON into this repo** at
   `data/snapshot.json`. Overwrite the existing file.
3. **Re-inject the JSON into `index.html`** so the inline copy used
   for `file://` rendering stays in sync:

   ```sh
   python3 scripts/build_snapshot.py
   ```

   This rewrites the block between `/* SNAPSHOT_START */` and
   `/* SNAPSHOT_END */` in `index.html`. It also re-applies
   `sanitize_for_public()` defensively, so even an over-broad copy
   is reduced before it ships.
4. **Run the validator.** This is mandatory.

   ```sh
   python3 scripts/validate_public_snapshot.py
   ```

   The validator parses `data/snapshot.json`, checks that the inline
   embedded snapshot in `index.html` matches it, verifies the
   required operator sections and KPI fields, and scans both files
   for forbidden sensitive patterns (real Google Sheet URLs or IDs,
   prospect or reply-sender email addresses, GitHub or generic API
   tokens, JWTs, AWS access keys, and `mailto:` links). A non-zero
   exit code means **do not commit**. Resolve every finding and
   re-run.
5. **Spot-check the rendered page** locally
   (`python3 -m http.server 8000` and load
   `http://localhost:8000/`). Confirm the KPI cards, daily trend,
   and reply mix reflect the new run.
6. **Commit and push.** Use a short imperative commit message that
   names the snapshot date. Example:

   ```sh
   git add data/snapshot.json index.html
   git commit -m "refresh public snapshot for YYYY-MM-DD outreach run"
   git push origin main
   ```

7. **Verify the live deployment** picked up the change. GitHub
   Pages and Vercel both redeploy on push automatically; the
   updated `generated_at` timestamp will appear in the page footer.

If a snapshot update needs to be rolled back, revert the commit and
push. The deployment target will redeploy the previous version.

## What the validator catches

The validator (`scripts/validate_public_snapshot.py`) is the
single source of truth for the public-mirror contract. It will
fail the build if any of the following is true:

- `data/snapshot.json` is missing, malformed, or missing required
  top-level sections.
- KPI fields are missing or non-numeric.
- `sources.sheet_url` or `sources.sheet_id` look like real values.
- `replies[]` contains forbidden free-text fields, or
  `Organization` is not redacted.
- A recipient-level `latest_batch` array is present.
- The `github` section exposes commit hashes or repo identifiers.
- The inline embedded snapshot in `index.html` does not match
  `data/snapshot.json`.
- Either file contains forbidden patterns: Google Sheet URLs/IDs,
  GitHub PATs (`ghp_...`, `github_pat_...`), generic secret keys
  (`sk-...`), JWTs, AWS access keys (`AKIA...`), `mailto:` links,
  any non-operator email address, or any unexpected
  `clovedds.com` address other than `ip@clovedds.com` and
  `aryaan@clovedds.com`.

If the validator passes, the snapshot is safe to publish. If it
fails, fix the upstream sanitization before pushing - do not edit
the validator to silence findings.

## Source-of-truth reminder

The Google Sheet inside the private operations environment is the
**private source of truth** for the campaign. It contains prospect
identities, reply bodies, and operator follow-up notes that must
never appear in this public mirror. This repository only ever
stores the sanitized aggregate snapshot. If you find yourself
about to commit raw recipient data, stop and re-run the
sanitization pipeline in the private repo.
