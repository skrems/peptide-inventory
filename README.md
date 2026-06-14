# Peptide Inventory

Admin-only inventory companion app for Peptide Power Assistant.

This app uses the same SQLite database as Peptide Power Assistant. It reads shared users, peptides, and dose logs, then adds inventory-specific tables for vial stock and manual adjustments.

## MVP Features

- Admin login using Peptide Power Assistant credentials.
- Admin-only access.
- Add inventory lots by peptide, vial count, and mg per vial.
- Add inventory by supplier code, such as `SK10` for Selank 10 mg.
- Add a new peptide while adding inventory.
- New peptides are inserted into the shared `peptides` table so they appear in Peptide Power Assistant dropdowns.
- Mark individual vials as used/reconstituted when they leave physical stock.
- Peptide Power dose logs are used for runway/reorder forecasting only; they do not automatically deplete inventory.
- Dashboard supply-health bars show critical, low, healthy, or no-pace status at a glance.
- Existing historical dose logs are ignored when you first enter current stock, so the lot count you enter becomes the starting inventory.
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

## Supplier Codes

The Add Inventory form includes a checked-in WanShun supplier code lookup generated from the supplier PDF kept outside this repository.

Examples:

```text
SK10 -> Selank, 10 mg per vial
RT10 -> Retatrutide, 10 mg per vial
2S10 -> SS-31, 10 mg per vial
CU50 -> GHK-Cu, 50 mg per vial
```

Entering a code auto-fills peptide name and mg per vial in the browser. The server also validates the code, so direct form submissions work too.

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

The smoke test creates a throwaway SQLite database with Peptide Power-compatible `users`, `peptides`, and `dose_logs`, then verifies baseline inventory calculations, vial-use depletion, forecasting-only dose logs, and admin-only access.

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
ghcr.io/skrems/peptide-inventory:v1.1
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
git tag <version>
git push origin <version>
```

4. Wait for GitHub Actions to publish GHCR.
5. Install/update in ZimaOS with:

```text
Docker image: ghcr.io/skrems/peptide-inventory
Tag: v1.1
Port: 8081 -> 8081
Volume: /DATA/AppData/peptide-power-assistant/data -> /data
```

## Notes

- Inventory is shared household/main inventory, not per-user inventory.
- Dose logs are forecasting inputs only; inventory is depleted by marking physical vials as used.
- A future version can add reorder thresholds, lead time projections, and optional daily snapshots.
