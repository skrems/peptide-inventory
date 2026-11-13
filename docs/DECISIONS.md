# Architectural Decisions

## Shared SQLite Database

Peptide Inventory reads Peptide Power's users, peptide catalog, and dose logs from the same SQLite file. Inventory-specific tables remain separate. Any schema work must preserve both applications.

## Physical Ledger Versus Dose Forecasting

Dose logs inform runway forecasts but do not deplete stock. Physical stock changes only through lot additions, vial-use/restoration actions, deletions, and explicit adjustments.

## Explicit Measurement Units

Every inventory lot and event carries `mg` or `IU`. A peptide cannot mix units because mixed totals would be misleading.

## Inventory-Only Visual Colors

The vial views use a deterministic high-contrast palette instead of shared `peptides.color` values. This keeps each inventory group distinct without changing colors used by Peptide Power.

## Blend Strength Variants

Physical vial views separate supplier-code and strength variants while dashboard forecasting remains grouped by the shared peptide name. Ingredient contents are supplier-code metadata, and existing lots recover their code from the audit note written when the lot was added. This distinguishes BB10 from BB20 without duplicating shared peptides or dose-log history.

## Versioned ZimaOS Releases

Production uses explicit GHCR `vX.Y` tags on `linux/amd64`. The application version, compose image, Git tag, documentation, and visible version badge move together.

## Pull Confirmation PIN

The shared-device vial-pull flow uses an inventory-specific 4-8 digit PIN while the admin session remains signed in. Store only a salted password hash in `inventory_settings`; enforce it server-side on every vial-use route, including the legacy Manage action. Changing the PIN requires the existing PIN or the signed-in admin's account password for recovery. The PIN is a convenience barrier on the physical device, not a replacement for account authentication.
