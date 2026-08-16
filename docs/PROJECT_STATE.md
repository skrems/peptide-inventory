# Project State

Last updated: 2026-08-15

## Summary

Peptide Inventory is an admin-only Python/SQLite companion app for Peptide Power Assistant. It tracks physical vial lots, vendors, strengths, units, batch and expiry dates, vial-use events, adjustments, and inventory runway. Dose logs are forecasting inputs only and do not automatically consume physical stock.

## Repository and Production

- Local path: `/Users/skrems/Projects/peptide-inventory`
- GitHub: `git@github.com:skrems/peptide-inventory.git`
- Branch: `main`
- Current release: `v1.7`
- Image: `ghcr.io/skrems/peptide-inventory:v1.7`
- ZimaOS app name: `peptide-inventory`
- Browser URL: `http://<zimaboard-ip>:8081/`
- Health check: `http://127.0.0.1:8081/healthz` from the ZimaBoard

## Shared Database

Production mounts:

```text
/DATA/AppData/peptide-power-assistant/data/app.db -> /data/app.db
```

Read from Peptide Power:

```text
users
peptides
dose_logs
```

Owned by Peptide Inventory:

```text
inventory_lots
inventory_adjustments
inventory_events
```

Inventory lots support `mg` and `IU`, a free-text vendor, batch date, expiry date, quantity, strength, used-vial count, notes, and audit history. A peptide cannot mix measurement units.

## Current Behavior

- Login uses Peptide Power users and requires the admin role.
- New peptides added from inventory are inserted into the shared peptide catalog.
- Supplier codes can fill peptide, strength, unit, and pack quantity; `H36` maps to HGH 36 IU.
- Physical inventory decreases only when a vial is marked used/reconstituted or through an explicit adjustment.
- Forecasting ignores dose logs that predate the first inventory baseline for a peptide.
- Vial View and 1K Foot View use an inventory-only 32-color high-contrast palette without altering shared peptide colors.
- The running release is visible beside the signed-in user.

## Local Development

```bash
python3 -m app.server
python3 scripts/smoke_test.py
```

Default local port is `8081`. Use a throwaway database for tests.

## ZimaOS Paths

```text
/DATA/AppData/peptide-inventory/source
/DATA/AppData/peptide-inventory/backups
/DATA/AppData/peptide-inventory/docker-config
/DATA/AppData/peptide-power-assistant/data/app.db
```

The compose source is `/DATA/AppData/peptide-inventory/source/docker-compose.zima.yml`. Releases are published through GitHub Actions and applied with an explicit version tag.

## Recent Releases

- `v1.5`: vendor, batch/expiry, mg/IU-aware lots and HGH/H36 support.
- `v1.6`: visible running-version badge.
- `v1.7`: distinct high-contrast vial colors for the visual inventory views.

## Next Candidates

- Reorder thresholds and vendor lead-time projections.
- Optional inventory snapshots and trend history.
- Additional supplier-code catalogs with provenance and validation.
