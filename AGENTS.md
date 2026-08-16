# Peptide Inventory Agent Guide

## Start Here

- Read `README.md` and `docs/PROJECT_STATE.md` before making changes.
- Inspect `git status --short --branch` and preserve unrelated work.
- Treat the code and current compose files as authoritative when documentation disagrees.

## Architecture

- This is a dependency-light Python web app in `app/server.py` with static assets in `static/`.
- It is an admin-only companion to Peptide Power Assistant.
- Both apps share the SQLite database mounted from `/DATA/AppData/peptide-power-assistant/data/app.db`.
- Shared tables are `users`, `peptides`, and `dose_logs`; inventory-owned tables are `inventory_lots`, `inventory_adjustments`, and `inventory_events`.
- Supplier-code mappings live in `app/supplier_codes.py`.

## Verification

Run these checks after code changes:

```bash
python3 -m py_compile app/server.py app/supplier_codes.py scripts/smoke_test.py
python3 scripts/smoke_test.py
git diff --check
```

Use a disposable or copied database for local testing. Do not point development servers at the live ZimaOS database.

## Data Safety

- Never copy, replace, or edit the live SQLite database without explicit authorization.
- Before a live schema migration or direct data edit, create a timestamped SQLite `.backup` under `/DATA/AppData/peptide-inventory/backups`.
- Preserve compatibility with Peptide Power's shared tables and historical rows.
- Keep database files, credentials, and generated backups out of Git and container images.

## Releases and ZimaOS

- Production uses explicit GHCR tags; do not deploy `latest`.
- Keep `APP_VERSION`, the image tag in `docker-compose.zima.yml`, README deployment examples, the Git tag, and the visible UI version synchronized.
- Publish `linux/amd64`, verify the GHCR manifest is publicly pullable, then update through `casaos-cli app-management apply`.
- Production port is `8081`; persistent data is outside the container.
- Do not push, tag, or deploy unless the user's request includes publishing or updating production.

## Documentation Upkeep

- Update `docs/PROJECT_STATE.md` after material architecture, schema, release, or deployment changes.
- Record durable architectural choices in `docs/DECISIONS.md`.
- Keep this file concise; operational detail belongs in README or `docs/`.
