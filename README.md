# PawVida Southern Automation — Deployment Starter v1

This is the first deployable backend for PawVida. It is deliberately **read-only / dry-run** until the Southern CSV column mapping and Shopify inventory location are verified.

## What v1 does
- Exchanges Shopify Client ID + Client Secret for a short-lived Admin API access token.
- Tests access to the PawVida Shopify store.
- Discovers and previews Southern's 5-minute stock CSV.
- Discovers and previews Southern's GTIN/barcode/weight CSV.
- Keeps all secrets in host environment variables.
- Blocks live Shopify inventory writes while `DRY_RUN=true`.

## Environment variables
Set these in Render/Railway secret settings, not in source code:
- `SHOPIFY_SHOP=pawvida-6`
- `SHOPIFY_CLIENT_ID=<your client id>`
- `SHOPIFY_CLIENT_SECRET=<your client secret>`
- `SHOPIFY_API_VERSION=2026-07`
- `SOUTHERN_STOCK_URL=https://www.agline.com/stock-level-csv/`
- `SOUTHERN_GTIN_URL=https://www.southernpetsupplies.com.au/resources/product-barcode-gtin-list/`
- `DRY_RUN=true`

## First tests after deployment
Open:
- `/health`
- `/shopify/test`
- `/southern/stock-preview`
- `/southern/gtin-preview`

Do not change `DRY_RUN` to false yet.

## Next development stage
After the four tests pass, v2 will add:
1. exact Southern SKU/stock column mapping;
2. exact GTIN/weight mapping;
3. Shopify location selection;
4. inventory write with absolute source-of-truth quantities;
5. margin-hold metafields;
6. Southern logged-in wholesale price capture;
7. controlled 50–100 product import;
8. paid-order webhook and supervised Southern order bot.
