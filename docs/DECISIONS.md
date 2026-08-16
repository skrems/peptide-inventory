# Architectural Decisions

## Shared SQLite Database

Peptide Inventory reads Peptide Power's users, peptide catalog, and dose logs from the same SQLite file. Inventory-specific tables remain separate. Any schema work must preserve both applications.

## Physical Ledger Versus Dose Forecasting

Dose logs inform runway forecasts but do not deplete stock. Physical stock changes only through lot additions, vial-use/restoration actions, deletions, and explicit adjustments.

## Explicit Measurement Units

Every inventory lot and event carries `mg` or `IU`. A peptide cannot mix units because mixed totals would be misleading.

## Inventory-Only Visual Colors

The vial views use a deterministic high-contrast palette instead of shared `peptides.color` values. This keeps each inventory group distinct without changing colors used by Peptide Power.

## Versioned ZimaOS Releases

Production uses explicit GHCR `vX.Y` tags on `linux/amd64`. The application version, compose image, Git tag, documentation, and visible version badge move together.
