# Valio Aimo mock API notes

Source: resource portal `https://junction26.valioaimo.fi/` (event password), saved under `docs/portal/`.
Probed live on 2026-11-13.

## Auth
- Portal pages and `/files/...`: session cookie from `POST /login` (form field `password`).
- Everything under `/api`: header `X-API-Key: <key>`.
- Rate limits: "undisclosed". The spec explicitly says **do not poll price/availability for all products**;
  narrow down from static data first. We query at most a few dozen candidates per request.

## Endpoints
| Method | Path | Purpose |
|---|---|---|
| GET | `/api/ping` | `{"status":"ok"}` |
| POST | `/api/price` | Price per sales unit for 1-1000 product codes on a delivery date |
| POST | `/api/availability` | Deliverability, quantity and order cut-off for 1-1000 product codes |
| GET | `/api/products/download` | Product data JSON (redirect) |
| GET | `/api/delivery-reliability/download` | CSV `productId,deliveryReliability` (0-1) |
| GET | `/api/carbon-footprint/download` | CSV `productId,AVG kg CO2-eq/kg` (product-group average) |

Request body for price and availability:
`{"customer": "123", "requestedDate": "2026-11-16", "products": ["463038", "464179"]}`.
`customer` is ignored in the mock. More than 1000 products gives HTTP 422.

### Price response (per product)
- `catalogPrice.value` = EUR per **sales unit**, before tax; `valueWithTax` includes VAT (13.5 % seen on milk).
- `comparisonPrice.value` = EUR per `comparisonUnit` (`L` or `KG`). This is the only reliable way to know
  how many litres/kg one sales unit holds: `catalogPrice / comparisonPrice` (463038: 31.31 / 1.57 = 20 L).
- `listPrice` = price before discount; `validFrom`/`validTo`; `netPriceFound`; `error: true` = no price.

### Availability response (per product)
- `logisticModel`: `Stock` (real quantity known) or `Terminal` (supplier-delivered, quantity always `999999`).
- `closingTime`: order cut-off for the returned `availableDate`. **Can already be in the past.**
  Seen on Fri 13 Nov 22:26 for delivery Mon 16 Nov: Terminal products closed Thu 12 Nov 15:00,
  Stock products close Sun 15 Nov 17:00.
- `scheduleLines`: 0 or 1 entry. Empty = cannot be delivered on any date. `availableDate` may be later than requested.
- `availableAmount` is in `salesUnit`. `0` = none. Stock products can show a real figure (469363: 384)
  or an "unlimited" sentinel (`9999999999`).
- `salesBlock: true` = cannot be ordered.

## Static data
- Product JSON: 17 546 products, 648 vendors. Key fields: `id`, `salesUnit`, `baseUnit`, `units[]`
  (`sizeInBaseUnits`, GTIN per unit), `category`, `vendorName`, `substitutions[]` (only 196 products),
  `synkkaData` (names fi/en/sv, brand, ingredients, marketing text, `classifications`).
- `classifications` enums: `allergen` (`CONTAINS` / `MAY_CONTAIN`, GS1 allergen codes such as `AM` milk,
  `AW` gluten cereals, `AE` egg, `AN` nuts), `nonAllergen` (`FREE_FROM`), `nutritionalClaim`
  (`FREE_FROM_LACTOSE`, `LOW_LACTOSE`, `FREE_FROM_GLUTEN`, `VEGAN`, ...), `packagingMarkedLabel`
  (`EU_ORGANIC_FARMING`, `FAIR_TRADE_MARK`, `MARINE_STEWARDSHIP_COUNCIL_LABEL`, ...).
- 3 079 products have no GTIN-backed allergen data (typically fruit and vegetables).
- Delivery reliability covers ~4 400 products, carbon footprint ~11 000.
- Sales unit codes: `KI` case, `ST` each, `KG`, `PAK`, `BAG`, `RAS`, `PRK`, `BOT`, ...

## What is missing (we mock these locally, see ASSUMPTIONS.md)
- **No order endpoint.** No order status, no order confirmation.
- **No offer/quote object.** Price and availability are two separate stateless lookups.
- **No per-supplier interface.** Suppliers exist only as `vendorName` on products.
- No customer accounts, mandates, approvals or audit log.
- No product search endpoint: search runs on the static file.
- Static package volume is patchy (`unitConversions` often lacks volume for case units).
