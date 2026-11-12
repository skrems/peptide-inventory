# Project State

Last updated: 2026-11-12

## Summary

Peptide Inventory is an admin-only Python/SQLite companion app for Peptide Power Assistant. It tracks physical vial lots, vendors, strengths, units, batch and expiry dates, vial-use events, adjustments, and inventory runway. Dose logs are forecasting inputs only and do not automatically consume physical stock.

## Repository and Production

- Local path: `/Users/skrems/Projects/peptide-inventory`
- GitHub: `git@github.com:skrems/peptide-inventory.git`
- Branch: `main`
- Current release: `v1.9`
- Image: `ghcr.io/skrems/peptide-inventory:v1.9`
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
- BB10 and BB20 remain one shared forecasting peptide but are separated in physical-stock displays by supplier code and component strength: 5 mg + 5 mg versus 10 mg + 10 mg per vial.
- KLOW80 is stored in the shared peptide catalog under its concise code name so Peptide Power dropdowns show `KLOW80`; its 80 mg component formula remains available from supplier-code metadata.
- Physical inventory decreases only when a vial is marked used/reconstituted or through an explicit adjustment.
- Forecasting ignores dose logs that predate the first inventory baseline for a peptide.
- Vial View and 1K Foot View use an inventory-only 32-color high-contrast palette without altering shared peptide colors.
- Stock Totals view combines current on-hand lots by product name and per-vial strength, showing vial count and total contents per product-strength group.
- `NJ1000` creates the distinct product `NAD1000`; lower-strength NAD products remain `NAD`.
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
- `v1.8`: show blend component strengths and separate BB10/BB20 physical vial groups without splitting their shared dose-log forecasting history.
- `v1.9`: add product-strength Stock Totals view and identify 1,000 mg NAD as `NAD1000`.

## Next Candidates

- Reorder thresholds and vendor lead-time projections.
- Optional inventory snapshots and trend history.
- Additional supplier-code catalogs with provenance and validation.
