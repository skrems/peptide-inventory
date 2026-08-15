# Project State

Last updated: 2026-07-23

## Summary

Peptide Inventory is a standalone admin-only companion app for Peptide Power Assistant. It uses the same SQLite DB file so inventory can be calculated from all users' logged peptide usage.

## Current Decisions

- Local project path: clone this repository wherever you keep local projects.
- ZimaOS port: `8081`
- Access: admin-only
- Shared database path on ZimaOS: `/DATA/AppData/peptide-power-assistant/data/app.db`
- Inventory app image: `ghcr.io/skrems/peptide-inventory:v1.4`

## Shared Tables

Read from Peptide Power:

```text
users
peptides
dose_logs
```

Created by Peptide Inventory:

```text
inventory_lots
inventory_adjustments
```

## Important Behavior

- Login uses existing Peptide Power `users.password_hash` and requires `role = 'admin'`.
- Adding a new peptide while adding inventory writes to the shared `peptides` table.
- Peptide Power dropdowns will then include that peptide.
- WanShun supplier codes are available for inventory entry, for example `SK10` becomes Selank 10 mg per vial.
- Inventory lots can record an optional free-text vendor name, which is shown with the lot details.
- Inventory strengths and audit events support mg or IU, and lot forms include batch and expiry dates.
- Inventory is a physical vial ledger. Dose logs do not automatically deplete inventory.
- Marking a vial as used/reconstituted removes one vial from stock.
- Peptide Power dose logs are used for runway/reorder forecasting after inventory tracking starts for that peptide.
- Dashboard supply-health bars classify peptides as critical, low, healthy, or no-pace based on projected days remaining.
- The first inventory lot or adjustment establishes the starting point. Historical dose logs before that point are ignored, because production stock entry represents current on-hand inventory.
- Inventory is calculated live; no 3am job is needed for MVP.
- The Vial View tab renders one color-coded vial marker for each physical vial currently on hand; used/reconstituted vials are excluded.
- The 1K Foot View renders those markers as one continuous mosaic, changing color between peptides and retaining a compact count legend.

## ZimaOS

The compose file mounts the Peptide Power Assistant data directory:

```yaml
volumes:
  - /DATA/AppData/peptide-power-assistant/data:/data
```

This app should be imported into ZimaOS as a separate app:

```text
Docker image: ghcr.io/skrems/peptide-inventory
Tag: v1.4
Host port: 8081
Container port: 8081
Volume: /DATA/AppData/peptide-power-assistant/data -> /data
```

Follow the shared ZimaOS runbook for release and update flow.
