# Peptide Inventory

Admin-only inventory companion app for Peptide Power Assistant.

This app uses the same SQLite database as Peptide Power Assistant. It reads shared users, peptides, and dose logs, then adds inventory-specific tables for vial stock and manual adjustments.

## MVP Features

- Admin login using Peptide Power Assistant credentials.
- Admin-only access.
- Add inventory lots by peptide, vial count, and mg per vial.
- Add a new peptide while adding inventory.
- New peptides are inserted into the shared `peptides` table so they appear in Peptide Power Assistant dropdowns.
- Usage is calculated from all users' completed `dose_logs` in mg.
- Manual inventory adjustments for waste, corrections, or found stock.
- ZimaOS deployment on port `8081`.

## Shared Database

The app reads existing Peptide Power tables:

```text
users
peptides
dose_logs
```

The app creates its own tables:

```text
inventory_lots
inventory_adjustments
```

## Run Locally

Use a test database or point to a copy of the Peptide Power database:

```bash
INVENTORY_DB=/path/to/app.db python3 -m app.server
```

Open:

```text
http://127.0.0.1:8081
```

## Test

```bash
make smoke
```

The smoke test creates a throwaway SQLite database with Peptide Power-compatible `users`, `peptides`, and `dose_logs`, then verifies inventory calculations and admin-only access.

## ZimaOS Deployment

This app should run beside Peptide Power Assistant:

```text
Peptide Power Assistant: http://<zimaboard-ip>:8080
Peptide Inventory:       http://<zimaboard-ip>:8081
```

The ZimaOS compose file mounts the existing Peptide Power database folder:

```yaml
volumes:
  - /DATA/AppData/peptide-power-assistant/data:/data
```

That means both apps use:

```text
/DATA/AppData/peptide-power-assistant/data/app.db
```

Current image tag:

```text
ghcr.io/skrems/peptide-inventory:v0.1
```

Use explicit version tags on ZimaOS custom apps. Do not rely on `latest`.

## Release Flow

1. Test locally:

```bash
python3 -m py_compile app/server.py scripts/smoke_test.py
python3 scripts/smoke_test.py
git diff --check
```

2. Commit and push.
3. Create and push a matching tag:

```bash
git tag v0.1
git push origin v0.1
```

4. Wait for GitHub Actions to publish GHCR.
5. Install/update in ZimaOS with:

```text
Docker image: ghcr.io/skrems/peptide-inventory
Tag: v0.1
Port: 8081 -> 8081
Volume: /DATA/AppData/peptide-power-assistant/data -> /data
```

## Notes

- Inventory is shared household/main inventory, not per-user inventory.
- Dose usage only counts completed logs with `dose_unit = 'mg'`.
- A future version can add reorder thresholds, lead time projections, and optional daily snapshots.
