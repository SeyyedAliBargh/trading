# Wallex API — Reference

> **Sources:** This document is derived **exclusively** from the project files `wallex_swagger.json` and `spot_swagger.json`
> (both Swagger 2.0, in the project root), the real-response captures in `docs/samples/` (`markets_raw.json` — a large real
> response, read only via a summary script; `markets_sample.json`; `market_symbols.txt`), and facts stated by the project
> owner in the task prompts. Everything the files do not contain is listed in the [Unknown](#6-unknown) section.
> Disagreements between sources are labeled **CONFLICT**, with the source named for each version.
>
> Swagger metadata — `wallex_swagger.json`: title **Spot**, version **1.0**, description *"This is the API server for basic-services"*, contact: empty.
> Swagger metadata — `spot_swagger.json`: title **Spot**, version **1.0**, description *"This is the API server for Spot"*, contact: empty.

---

## 1. Base URL and Authentication

**Base URL to use: `https://api.wallex.ir/`** (per the project owner's verified fact; see CONFLICT below).

| Item | `wallex_swagger.json` | `spot_swagger.json` | Project owner (verified fact) |
|---|---|---|---|
| Scheme | `https` (declared) | not declared (no `schemes` field in the file) | — |
| Host | `api.wallex.com` | `api.wallex.com` | `api.wallex.com` does not resolve from the owner's network (DNS error `getaddrinfo failed`); `api.wallex.ir` works |
| Base path | `/` | `/` | — |

**CONFLICT — base URL / host.** Both versions are kept, with sources named:
- `wallex_swagger.json` declares the base URL **`https://api.wallex.com/`** (schemes `https`, host `api.wallex.com`, basePath `/`).
- `spot_swagger.json` declares host `api.wallex.com` and basePath `/`, but declares no scheme.
- Project owner (verified fact given in the task, not from the swagger files): `https://api.wallex.com` does not resolve from the owner's network (DNS error `getaddrinfo failed`), while `https://api.wallex.ir` works.
- Resolution adopted in this document: **treat `https://api.wallex.ir` as the working base URL**; both swagger files still declare `api.wallex.com`.

**Authentication method:** API key in a request header. Both files define the security scheme and their declarations are **identical — no CONFLICT on the header name**:

```json
"ApiKeyAuth": {
  "type": "apiKey",
  "in": "header",
  "name": "x-api-key",
  "description": "Use your API key to authenticate requests."
}
```

- The API-key header is named **`x-api-key`** (source: `securityDefinitions.ApiKeyAuth` in both `wallex_swagger.json` and `spot_swagger.json`).
- Per-operation requirements differ between the two files:
  - `wallex_swagger.json`: **every** operation (including market-data endpoints such as markets, depth, history, and trades) declares `ApiKeyAuth`.
  - `spot_swagger.json`: the order endpoints (`GET /v1/account/openOrders`, `GET`/`POST`/`DELETE /v1/account/orders`, `GET`/`DELETE /v1/account/orders/{client_id}`) declare `ApiKeyAuth`; `GET /hector/web/v1/markets` and `GET /depth/all` declare **no** security requirement (see the markets CONFLICT in section 2.1).
- No other authentication mechanism (signatures, HMAC, OAuth, etc.) is defined in either file.
- All documented operations produce `application/json`.

---

## 2. Endpoints

`wallex_swagger.json` documents 10 paths / 11 operations; `spot_swagger.json` documents 5 paths / 8 operations. The eight requested topics are covered below, with the source file named for every endpoint. New endpoints from `spot_swagger.json` that do not fit any existing subsection are in the [Spot endpoints](#spot-endpoints) section.

### 2.1 Markets

| Item | Value |
|---|---|
| Path | `/hector/web/v1/markets` |
| Method | `GET` |
| Operation ID | `getMarkets` |
| Summary | "Get all markets" / "Retrieve all markets" |
| Parameters | **None** |
| Security | `ApiKeyAuth` (per `wallex_swagger.json`) — **CONFLICT**: `spot_swagger.json` declares no security for this operation (see note below) |
| Response `200` | Schema `resources.allMarkets` |

**Sample response** (`200`) — real structure from `docs/samples/markets_raw.json` (a real response of this endpoint captured by the project owner; summarized in `docs/samples/markets_sample.json`). Top-level keys: **`result`, `message`, `success`** (in the capture: `message` = `"Success"`, `success` = `true`); the markets are an array under `result.markets` — **469 entries** in the capture. One real entry (PEPETMN):

```json
{
  "result": {
    "markets": [
      {
        "symbol": "PEPETMN",
        "base_asset": "PEPE",
        "quote_asset": "TMN",
        "fa_base_asset": "پپه",
        "fa_quote_asset": "تومان",
        "en_base_asset": "Pepe",
        "en_quote_asset": "Toman",
        "categories": [4],
        "price": "1.2481",
        "is_new": false,
        "is_spot": true,
        "is_otc": true,
        "is_margin": true,
        "is_tmn_based": true,
        "is_usdt_based": false,
        "is_zero_fee": false,
        "leverage_step": 0.5,
        "max_leverage": 10,
        "created_at": "2023-05-24T00:00:00Z",
        "amount_precision": 0,
        "price_precision": 4,
        "flags": [],
        "is_market_type_enable": false,
        "otc_min_notional": 90000,
        "otc_max_notional": 200000000,
        "otc_buy_enabled": true,
        "otc_sell_enabled": true,
        "fair_price": {
          "ask": "1.249150063410755",
          "bid": "1.249150063410755",
          "spot": { "threshold": "4.00", "max_acceptable_buy": "8.00", "max_acceptable_sell": "8.00" },
          "margin_market": { "threshold": null, "max_acceptable_buy": null, "max_acceptable_sell": null },
          "margin_limit": { "threshold": "5.00", "max_acceptable_buy": null, "max_acceptable_sell": null },
          "sakoo": { "threshold": "4.00", "max_acceptable_buy": "8.00", "max_acceptable_sell": "8.00" },
          "market_maker": { "threshold": "5.00", "max_acceptable_buy": "10.00", "max_acceptable_sell": "10.00" }
        },
        "change_24h": 5.33,
        "volume_24h": 77538806596,
        "change_7D": 15.69,
        "quote_volume_24h": 94815225457.8454,
        "spot_is_new": false,
        "otc_is_new": false,
        "highest_24h_price": "1.2556",
        "lowest_24h_price": "1.16"
      }
    ]
  },
  "message": "Success",
  "success": true
}
```

**Fields seen in the real response** (types as seen in `docs/samples/markets_sample.json`):

| Field | Type (as seen) | Real example |
|---|---|---|
| `symbol` | string | `"PEPETMN"` |
| `base_asset` | string | `"PEPE"` |
| `quote_asset` | string | `"TMN"` |
| `is_spot` | boolean | `true` |
| `is_otc` | boolean | `true` |
| `is_margin` | boolean | `true` |
| `is_tmn_based` | boolean | `true` |
| `is_usdt_based` | boolean | `false` |
| `is_zero_fee` | boolean | `false` — meaning **Unknown**, see section 4 |
| `amount_precision` | integer | `0` (PEPETMN); `8` (BTCTMN) |
| `price_precision` | integer | `4` (PEPETMN); `0` (BTCTMN) |
| `otc_min_notional` | integer | `90000` — **OTC markets only; NOT the spot minimum order size** (see section 4) |
| `otc_max_notional` | integer | `200000000` (PEPETMN); `400000000` (BTCTMN) — OTC only |
| `volume_24h` | number | `77538806596` (PEPETMN); `4.08264356` (BTCTMN) |
| `quote_volume_24h` | number | `94815225457.8454` |
| `fair_price` | object | `ask`/`bid` strings plus objects `spot`, `margin_market`, `margin_limit`, `sakoo`, `market_maker`, each with `threshold`, `max_acceptable_buy`, `max_acceptable_sell` (string or null) |
| `price` | string | `"1.2481"` |
| `flags` | array of strings | `[]` (PEPETMN); `["isRisky"]` (PUMPTMN) |
| `is_market_type_enable` | boolean | `false` (PEPETMN); `true` (BTCTMN) — meaning not documented anywhere |

Notes:
- The real entry is a **superset** of both swagger schemas: all 28 schema fields are present, plus 8 fields the schemas never mention (`is_market_type_enable`, `otc_min_notional`, `otc_max_notional`, `otc_buy_enabled`, `otc_sell_enabled`, `fair_price`, `highest_24h_price`, `lowest_24h_price`). The schemas are incomplete, not contradictory.
- Schema-vs-reality discrepancy: both schemas type `leverage_step`/`max_leverage` as plain numbers, but the real capture shows them as `null` for PUMPTMN (a market with `is_margin: false`) — the real response is more loosely typed than the schemas claim.

**Market population** (counts from a summary script run over `docs/samples/markets_raw.json`; the script prints counts only — the large file was never dumped):

| Count | Value |
|---|---|
| Total markets in the capture | 469 |
| `is_spot = true` | **373** |
| — of those, `is_tmn_based = true` | **187** |
| — of those, `is_usdt_based = true` | **186** (187 + 186 = 373: every spot market is quoted in exactly one of TMN or USDT) |
| `is_otc = true` (all markets, not only spot) | **442** |

**Symbol naming** (from `docs/samples/market_symbols.txt` — 469 symbols, one per market):
- A symbol is the **concatenation of `base_asset` + `quote_asset`** with no separator: `PEPE`+`TMN` → `PEPETMN`, `BTC`+`USDT` → `BTCUSDT`, `1BBABYDOGE`+`TMN` → `1BBABYDOGETMN`.
- **TMN-based pairs** end with the suffix `TMN` and are quoted in toman (`is_tmn_based = true`), e.g. `BTCTMN`, `USDTTMN` (base `USDT`, quote `TMN`). 235 of the 469 symbols end with `TMN`.
- **USDT-based pairs** end with the suffix `USDT` and are quoted in USDT (`is_usdt_based = true`), e.g. `BTCUSDT`, `USDCUSDT`. 234 symbols end with `USDT`; none of the 469 ends with anything else.
- The same base asset usually exists on both quotes (`PEPETMN` and `PEPEUSDT`, `AAPLXTMN` and `AAPLXUSDT`), so a base asset alone does not identify a market.
- The authoritative split of a symbol is the `base_asset`/`quote_asset` fields of the markets response — suffix-matching the string alone can mislead: `USDTTMN` is base `USDT` + quote `TMN` (a TMN-based pair), not `USDTT`+`MN`.

**Duplicate declaration / CONFLICT — authentication requirement.** `spot_swagger.json` also documents `GET /hector/web/v1/markets` (same path, same operationId `getMarkets`, same `resources.allMarkets` response schema; tag `spot` instead of `basic-services`, which is not a functional difference):
- `wallex_swagger.json`: declares `security: ApiKeyAuth` on this operation → **API key required**.
- `spot_swagger.json`: declares **no** security requirement on this operation.

Both versions are kept. The response schema is identical in both files (no conflict there); only the authentication requirement conflicts.

### 2.2 Order Book (Market Depth)

| Item | Value |
|---|---|
| Path | `/v1/depth` |
| Method | `GET` |
| Operation ID | `getMarketDepth` |
| Summary | "دریافت عمق بازار" — *Get market depth* (translated from Persian) |
| Description | Returns the list of buy (bid) and sell (ask) orders for a specific symbol |
| Security | `ApiKeyAuth` |

**Parameters:**

| Name | In | Required | Type | Example | Description |
|---|---|---|---|---|---|
| `symbol` | query | yes | string | `USDCUSDT` | The trading symbol of interest |

**Sample response** (`200`, verbatim from `wallex_swagger.json`):

```json
{
  "result": {
    "ask": [
      { "price": 1, "quantity": 24.79, "sum": "24.79" },
      { "price": 1.0045, "quantity": 314.37, "sum": "315.784665" }
    ],
    "bid": [
      { "price": 0.9944, "quantity": 317.57, "sum": "315.791608" },
      { "price": 0.9869, "quantity": 479.97, "sum": "473.682393" }
    ]
  },
  "message": "عملیات موفقیت‌آمیز بود.",
  "success": true
}
```

Each entry (`OrderBookEntry`, from `wallex_swagger.json`) has `price` (number), `quantity` (number), `sum` (string).

#### All-markets depth — `GET /depth/all` (from `spot_swagger.json`)

| Item | Value |
|---|---|
| Path | `/depth/all` |
| Method | `GET` |
| Operation ID | `getDepth` |
| Summary | "Get all markets depth" / "Retrieve market depth for all markets" |
| Parameters | **None** |
| Security | **No security requirement declared** in `spot_swagger.json` (unlike `/v1/depth` in `wallex_swagger.json`, which requires `ApiKeyAuth`) |
| Response `200` | Schema `resources.AllMarketDepthResponse` |
| Response `500` | "Internal server error" — schema `resources.ErrorResponse` |

**Sample response** — `spot_swagger.json` provides no example payload; skeleton derived from the schema (`result` is a map keyed by market, each value an `ask`/`bid` list of `DepthTupleResource` entries):

```json
{
  "message": "<string>",
  "result": {
    "<market_key>": {
      "ask": [ { "price": "<number>", "quantity": "<number>", "sum": "<number>" } ],
      "bid": [ { "price": "<number>", "quantity": "<number>", "sum": "<number>" } ]
    }
  },
  "success": "<boolean>"
}
```

**CONFLICT — `sum` type in depth entries.** Both versions are kept, with sources named:
- `wallex_swagger.json` — `OrderBookEntry.sum` is a **string** (example `"315.784665"`), returned by `GET /v1/depth`.
- `spot_swagger.json` — `DepthTupleResource.sum` is a **number**, returned by `GET /depth/all`.
- Both files agree that `price` and `quantity` are numbers. Nothing is assumed about which typing applies at runtime.
- Note: `spot_swagger.json` also defines `resources.SingleMarketDepthResponse` (the single-market envelope: `message`/`result`/`success` with `result` = `MarketDepthResource`), but **no path in that file uses it**; the single-market depth endpoint itself (`GET /v1/depth`) exists only in `wallex_swagger.json`.

### 2.3 Candles / History

| Item | Value |
|---|---|
| Path | `/v1/udf/history` |
| Method | `GET` |
| Operation ID | `candleHistory` |
| Summary | "Get historical market data (candles)" |
| Description | Retrieve historical OHLCV data for a symbol. Useful for charting and analysis. |
| Security | `ApiKeyAuth` |

**Parameters (all required):**

| Name | In | Type | Example | Description |
|---|---|---|---|---|
| `symbol` | query | string | `BTCTMN` | The symbol to retrieve historical data for |
| `resolution` | query | string | `60` | Candle resolution (e.g. 1, 5, 15, 60, D) — `wallex_swagger.json` gives only these examples |
| `from` | query | integer (int64) | `1733425200` | Start of time range (Unix timestamp) |
| `to` | query | integer (int64) | `1733428800` | End of time range (Unix timestamp) |

**Sample response** (`200`, verbatim from `wallex_swagger.json` — arrays of open `o`, high `h`, low `l`, close `c`, volume `v`, times `t`):

```json
{
  "s": "ok",
  "t": [1733425200, 1733428800],
  "c": ["7209722048.0000000000000000", "7235725819.0000000000000000"],
  "o": ["7286137604.0000000000000000", "7200151050.0000000000000000"],
  "h": ["7286137604.0000000000000000", "7235725819.0000000000000000"],
  "l": ["7209722048.0000000000000000", "7150000001.0000000000000000"],
  "v": ["0.2544820000000000", "1.0201680000000000"]
}
```

Note: this endpoint uses the UDF response shape (`s`/`t`/`o`/`h`/`l`/`c`/`v`), not the `{result, message, success}` envelope used elsewhere.

### 2.4 Balances

| Item | Value |
|---|---|
| Path | `/v1/account/balances` |
| Method | `GET` |
| Operation ID | `getAccountBalances` |
| Summary | "Get account balances" |
| Description | Retrieve the list of all account balances. Requires API key. |
| Parameters | **None** |
| Security | `ApiKeyAuth` |
| Response `200` | Schema `AccountBalancesResponse` |

**Sample response** — `wallex_swagger.json` provides no operation-level example. The skeleton below is assembled from the schema definitions; values shown are the schema's declared property examples, `<…>` marks fields with no example in the file. `balances` is a map: each key identifies an asset, each value is an `AccountBalanceItem`:

```json
{
  "result": {
    "balances": {
      "<asset_key>": {
        "asset": "1BBABYDOGE",
        "asset_png_icon": "<uri>",
        "asset_svg_icon": "<uri>",
        "faName": "۱میلیارد بیبی دوج",
        "fiat": "<boolean>",
        "value": "13.97200000",
        "locked": "0.00000000",
        "is_dust": "<boolean>",
        "is_digital_gold": "<boolean>"
      }
    }
  },
  "message": "عملیات با موفقیت انجام شد",
  "success": true
}
```

**Note:** `spot_swagger.json` does **not** document balances; this endpoint remains documented only by `wallex_swagger.json` and is unchanged by this update.

### 2.5 Create Order

`wallex_swagger.json` contains nothing on this (no path, method, parameters, request body, or response). **Documented by `spot_swagger.json` and clarified by the official Wallex docs:**

| Item | Value |
|---|---|
| Path | `/v1/account/orders` |
| Method | `POST` |
| Operation ID | `postAccountOrders` |
| Summary | "Submit a new trading order" |
| Security | `ApiKeyAuth` (`x-api-key` header) |
| Request body | Required; fields `side`, `symbol`, `type` always required. `price` and `quantity` are **strings** formatted to the market’s `price_precision` / `amount_precision`. `stop_Price` (exact casing) only for `STOP_LIMIT` / `STOP_MARKET`. `client_id` may contain only `A-Z a-z 0-9 _` and must be unique. |
| Success response | **HTTP 201** — envelope `{"result": <order>, "success": true, "message": "..."}`. |
| Error responses | `400`, `401`, `422` — schema `resources.ErrorResponse`. |
| Rate limit | **20 requests per 10 seconds** (order placement). No automatic throttling in the client; caller must respect. |

**Request body fields** (derived from `spot_swagger.json` + official docs):

| Field | Type | Required | Notes |
|---|---|---|---|
| `side` | string | yes | `BUY` or `SELL` (uppercase) |
| `symbol` | string | yes | Market symbol, e.g. `BTCTMN` |
| `type` | string | yes | `LIMIT`, `MARKET`, `STOP_LIMIT`, `STOP_MARKET` |
| `price` | string | **yes for LIMIT/STOP_LIMIT**, omitted for `MARKET` (see open question) |
| `quantity` | string | yes | Formatted to `amount_precision` |
| `stop_Price` | string | only for `STOP_LIMIT` / `STOP_MARKET` | Exact casing `stop_Price` |
| `client_id` | string | no (generated if omitted) | Regex `^[A-Za-z0-9_]+$` |

**Sample response** (HTTP 201) — envelope with parsed order (see order object shape below).

**Open questions**

* The official docs table marks `price` and `quantity` as required for **every** type, but the Swagger schema only requires them for non‑market types. The client currently omits `price` for `MARKET`; if the API returns `400`, the field can be added with a small comment in the code.

* Full list of `status` strings is not documented; the probe will discover them.

* Meaning of `stopPriceCondition` is unknown.

* Minimum order size / notional is not published; the probe will surface the error text.

---

### 2.6 Cancel Order

`wallex_swagger.json` contains nothing. **Official docs expose only the path‑parameter variant:**

| Item | Value |
|---|---|
| Path | `/v1/account/orders/{client_id}` |
| Method | `DELETE` (no request body) |
| Security | `ApiKeyAuth` |
| Success | HTTP 200 — envelope with cancelled order object |
| Errors | `400`, `401`, `422` |

The body‑based variant (`DELETE /v1/account/orders` with JSON) is **not** in the official docs and is omitted.

---

### 2.7 Order Status

| Item | Value |
|---|---|
| Path | `/v1/account/orders/{client_id}` |
| Method | `GET` |
| Security | `ApiKeyAuth` |
| Success | HTTP 200 — envelope with order object |
| Errors | `400`, `401`, `404` |

The order object returned by **all** order endpoints (`create`, `cancel`, `status`, `openOrders`, `orders`) shares a common shape (see below). The parser tolerates missing optional fields (`fee`, `executedPercent` as string or number, etc.).

---

### Order object shape (common to all order endpoints)

| Field | Type | Notes |
|---|---|---|
| `active` | boolean | |
| `clientOrderId` | string | |
| `created_at` | string (date‑time) | |
| `executedPercent` | number (string or numeric) | |
| `executedPrice` | string | |
| `executedQty` | string | |
| `executedSum` | string | |
| `fee` | string (may be missing) | |
| `fills` | array of fill objects | each fill has `fee`, `feeAsset`, `feeCoefficient`, `isBuyer`, `makerFeeCoefficient`, `price`, `quantity`, `sum`, `symbol`, `takerFeeCoefficient`, `timestamp` |
| `origQty` | string | |
| `price` | string | |
| `side` | string | `BUY` / `SELL` |
| `status` | string | values not fully documented (see Unknown) |
| `stopPrice` | string | |
| `stopPriceCondition` | string | meaning unknown |
| `sum` | string | |
| `symbol` | string | |
| `transactTime` | integer | |
| `type` | string | |
| `updated_at` | string | |

The parser used by the client (`_parse_order`) applies `float()` to numeric strings, uses `.get()` for optional fields, and never raises on missing keys.

---

### 2.8 Open Orders List

| Item | Value |
|---|---|
| Path | `/v1/account/openOrders` |
| Method | `GET` |
| Security | `ApiKeyAuth` |
| Query params | `symbol` (optional), `page` (default 1), `per_page` (default 25) |
| Response | `result` is a **dictionary whose values are arrays of order objects** (keys like `additionalProp1`…). The client flattens all list values. A plain list or empty dict is also handled. |

---

### 2.9 Orders List (with filters)

| Item | Value |
|---|---|
| Path | `/v1/account/orders` |
| Method | `GET` |
| Security | `ApiKeyAuth` |
| Query params | `market`, `page`, `per_page`, `from` (`YYYY-MM-DD`), `to` (`YYYY-MM-DD`), `type` (`STOP_LIMIT`/`STOP_MARKET`/`LIMIT`/`MARKET`), `side` (`BUY`/`SELL`) |
| Response | Same dict‑of‑lists envelope as `openOrders`. |

**Parameters** (all optional):

| Name | In | Type | Description |
|---|---|---|---|
| `symbol` | query | string | Market symbol |
| `page` | query | integer | Page number (default: 1) |
| `per_page` | query | integer | Items per page (no default or maximum stated) |

**Sample response** — no example in `spot_swagger.json`; skeleton from `resources.OrderListResponseV1`. Note that `result` is defined as a **map whose values are arrays** of order objects (as in section 2.5); the file does not specify what the map keys are, and `result_info` is declared with an empty schema (shape unspecified):

```json
{
  "message": "<string>",
  "result": {
    "<key>": [ { "…": "order object — see section 2.5" } ]
  },
  "result_info": "<shape not specified in spot_swagger.json>",
  "success": "<boolean>"
}
```

A second, broader list endpoint (`GET /v1/account/orders`, all orders with filters) is documented in the [Spot endpoints](#spot-endpoints) section.

### Other endpoints present in wallex_swagger.json (not requested)

For completeness, `wallex_swagger.json` also documents these non-trading operations (`spot_swagger.json` adds no other account endpoints beyond the order endpoints above):

- `GET /v1/account/wallets/{symbol}` — deposit wallet address per coin/network (`symbol` path param, optional `network` query param)
- `POST /v1/account/ibans` — add an IBAN; `GET /v1/account/ibans` — list IBANs (with `page`/`per_page` query params)
- `POST /v1/account/money-withdrawal` — request an IRT bank withdrawal
- `GET /v1/account/card-numbers` — list bank cards
- `GET /v1/account/fee` — per-market maker/taker fee rates (see section 4)
- `GET /v1/trades` — latest executed market trades by `symbol`

---

## Spot endpoints

New material from `spot_swagger.json` that has no matching subsection above.

### Orders list (all orders, with filters) — `GET /v1/account/orders`

| Item | Value |
|---|---|
| Path | `/v1/account/orders` |
| Method | `GET` |
| Operation ID | `getOrders` |
| Summary | "List orders" |
| Description | Get a paginated list of orders with optional filters — all orders, not only active ones (the active-only endpoint is `GET /v1/account/openOrders`, section 2.8) |
| Security | `ApiKeyAuth` |
| Response `200` | Schema `resources.OrderPaginateResponseV1` — same envelope shape as `OrderListResponseV1` in section 2.8 (`result` = map of arrays of order objects, `result_info` = unspecified shape) |
| Error responses | `400`, `401`, `422` — schema `resources.ErrorResponse` |

**Parameters** (all optional):

| Name | In | Type | Allowed values | Description |
|---|---|---|---|---|
| `market` | query | string | — | Market symbol (note: named `market` here, but `symbol` on the open-orders endpoint) |
| `type` | query | string | `STOP_LIMIT`, `STOP_MARKET`, `LIMIT`, `MARKET` | Order type filter |
| `side` | query | string | `BUY`, `SELL` | Order side filter |
| `page` | query | integer | — | Page number (default: 1) |
| `per_page` | query | integer | — | Items per page (default: 25, max: 100) |
| `from` | query | string | — | Start date (format: `YYYY-MM-DD`) |
| `to` | query | string | — | End date (format: `YYYY-MM-DD`) |

**Sample response:** no example in `spot_swagger.json`; same envelope skeleton as section 2.8, with order objects as in section 2.5.

### Error responses (all `spot_swagger.json` order endpoints)

Non-success responses on the spot endpoints use HTTP `400` (invalid request parameters), `401` (unauthorized), `404` (order not found — only on `GET /v1/account/orders/{client_id}`), `422` (validation error), and `500` (internal server error — only on `GET /depth/all`), all with schema `resources.ErrorResponse`:

| Field | Type |
|---|---|
| `code` | integer |
| `message` | string |
| `result` | object — map of string keys to arrays (item type unspecified) |
| `success` | boolean |

(`wallex_swagger.json` documents no error responses at all — only `200`/`201` — so the error behavior of its endpoints remains unknown; see section 6.)

### Defined in `spot_swagger.json` but attached to no path

These request/response schemas exist in the file but **no documented endpoint uses them** — hints of API surface that is not documented:

- `requests.CreateSubAccountOrderRequest` — required: `side`, `symbol`, `type`; optional: `client_id`, `price`, `quantity`, `stop_Price`, `subAccount_client_id` (all string)
- `requests.CancelSubAccountOrderRequest` — required: `client_order_id`, `subAccount_client_id` (both string)
- `resources.SingleMarketDepthResponse` — single-market depth envelope (see note in section 2.2)
- `resources.OrderListResponse` and `resources.OrderPaginateResponse` — non-V1 envelope variants whose `result` is a plain **array** of order objects (plus unspecified `result_info`); the documented endpoints use the V1 variants instead

---

## 3. Supported Order Types

`wallex_swagger.json` contains **no** order-type information (that statement remains true for that file). **`spot_swagger.json` documents the following:**

The order types the API recognizes are enumerated by the `type` filter parameter of `GET /v1/account/orders` (`spot_swagger.json`): **`LIMIT`**, **`MARKET`**, **`STOP_LIMIT`**, **`STOP_MARKET`**.

| Order type | Status in the files | Fields documented in `spot_swagger.json` |
|---|---|---|
| Limit (`LIMIT`) | Documented (as a type value) | No per-type field list. All types share the single create schema `requests.CreateOrderRequest`: required `side`, `symbol`, `type`; optional `price`, `quantity`, `stop_Price`, `client_id` (see section 2.5) |
| Market (`MARKET`) | Documented (as a type value) | Same shared schema — the file does not state whether `price` is used or forbidden for market orders |
| Stop-limit (`STOP_LIMIT`) | Documented (as a type value) | Same shared schema; `stop_Price` is the only stop-related field in the create request — the file does not explicitly bind it to the stop types |
| Stop-market (`STOP_MARKET`) | Documented (as a type value; not among the four types originally asked about) | Same shared schema |
| OCO | **Not documented in either file** | — |

- Which fields are required or used **per order type** (e.g., whether a `MARKET` order omits `price`) is not specified anywhere in `spot_swagger.json`.
- The `type` and `side` fields of the create request are plain strings — allowed values are not enumerated on the create endpoint; the enums (`STOP_LIMIT`/`STOP_MARKET`/`LIMIT`/`MARKET` and `BUY`/`SELL`) appear only on the list filter parameters of `GET /v1/account/orders`.

**Server-side stop-loss:** `spot_swagger.json` shows that **stop orders can be registered on the server**:
- `POST /v1/account/orders` accepts a `stop_Price` field (section 2.5);
- the order types include `STOP_LIMIT` and `STOP_MARKET`;
- the order resource returned by the API includes `stopPrice` and `stopPriceCondition` fields (section 2.5), so stop orders are stored and tracked server-side.

Caveats (kept as unknowns in section 6): the files never use the term "stop-loss"; no OCO/bracket mechanism (a stop-loss + take-profit pair) is documented; allowed `stopPriceCondition` values are not enumerated; and no trigger-direction or time-in-force semantics are described.

- (From `wallex_swagger.json`, unchanged): the only "order"-related definition there is `OrderBookEntry` (`price`, `quantity`, `sum`), which is an order-book price level — not an order type.

---

## 4. Markets Response: Minimum Order Size, Precision, and Fees

Sources: the swagger schemas (`resources.allMarkets`, identical in `wallex_swagger.json` and `spot_swagger.json`) and the **real captured response** in `docs/samples/` — see the real entry, field types and counts in section 2.1. The real response is a superset of the schemas.

| Requested item | Field in the markets response | Notes |
|---|---|---|
| Price precision | **`price_precision`** (integer) | Number of decimal places for price, per market (real: `4` for PEPETMN, `0` for BTCTMN) |
| Quantity precision | **`amount_precision`** (integer) | Number of decimal places for amount/quantity, per market (real: `0` for PEPETMN, `8` for BTCTMN) |
| Minimum order size (spot) | **Unknown** | The real markets response contains **no** spot minimum-quantity or minimum-notional field. `otc_min_notional` / `otc_max_notional` (integers, e.g. `90000` / `200000000`) apply to **OTC markets only** and must **NOT** be treated as the spot minimum order size. The only other min-values in the sources are `min_withdrawal_value` / `min_deposit_value` on the crypto-wallet endpoint, which concern deposits/withdrawals, **not** orders |
| Fees | **`is_zero_fee`** (boolean) — meaning **Unknown** | The field name suggests a zero-fee market, but neither swagger file nor the samples documents what it actually zeroes (maker? taker? which market types?); its semantics are unknown. Actual fee **rates are not included** in the markets response |

**Where fee rates actually come from (per `wallex_swagger.json` + real probe; `spot_swagger.json` does not describe `GET /v1/account/fee`):** the API-key-protected endpoint `GET /v1/account/fee` returns a **dict keyed by market symbol** (e.g. `BTCTMN`), each value containing:

| Field | Type | Example |
|---|---|---|
| `makerFeeRate` | string | `"0.00200000"` |
| `takerFeeRate` | string | `"0.00200000"` |
| `recent_days_sum` | number (double) | `250000000.0` |

Real fields of a markets entry (superset of the swagger schemas — see the type table in section 2.1): the 19 fields listed there, plus `fa_base_asset`, `fa_quote_asset`, `en_base_asset`, `en_quote_asset`, `categories`, `is_new`, `leverage_step`, `max_leverage`, `created_at`, `change_24h`, `change_7D`, `spot_is_new`, `otc_is_new`, `otc_buy_enabled`, `otc_sell_enabled`, `highest_24h_price`, `lowest_24h_price`.

---

## 5. Rate Limits

**Not mentioned in either file.** There are no rate-limit quotas, time windows, throttling headers, `429` responses, or any other rate-limiting information anywhere in `wallex_swagger.json` or `spot_swagger.json` (both searched in full). The only numeric request caps in `spot_swagger.json` are pagination caps — `per_page` "default: 25, max: 100" on `GET /v1/account/orders` and `page` "default: 1" — which limit page size, not request rate. Rate limits remain unknown.

---

## 6. Unknown

Everything below is absent from **both** `wallex_swagger.json` and `spot_swagger.json`, and therefore still unknown:

**Trading / order management**
- **OCO order type** — not mentioned in either file (limit, market, stop-limit and stop-market are now documented; see section 3).
- **Per-type order fields** — which fields are required or used for each order type (e.g., whether `MARKET` orders omit `price`) is not fully specified; the official docs table marks `price` and `quantity` required for every type, but the Swagger schema only requires them for non‑market types. The client currently omits `price` for `MARKET`; this is noted as an open question.
- **Allowed values on the create request** — `side` and `type` are plain strings on `POST /v1/account/orders`; their enums appear only on the list filter.
- **Order status values** — `status` is a string in the order resource; its allowed values are not enumerated. Same for `stopPriceCondition`.
- **Time-in-force, trigger direction, order lifecycle transitions** — not documented.
- **Spot minimum order size** — still unknown. The real markets response (`docs/samples/`) has no spot minimum-quantity or minimum-notional field; its `otc_min_notional` / `otc_max_notional` apply to **OTC markets only** and must **NOT** be treated as the spot minimum. The only other min-values in the sources are `min_withdrawal_value` / `min_deposit_value` on the crypto-wallet endpoint (`wallex_swagger.json`), which concern deposits/withdrawals, **not** orders. A probe or an answer from the owner is required.
- **Meaning of `is_zero_fee`** — the flag exists in the real data (`false`/`true`), but nothing documents what it actually zeroes (maker/taker? which market types?); unknown.
- **Fee rates inside the markets response** — only the `is_zero_fee` flag exists there; rates live solely in `GET /v1/account/fee` (`wallex_swagger.json`).
- **Semantics of several real markets fields** — `is_market_type_enable`, the `fair_price` sub-objects (`spot`, `margin_market`, `margin_limit`, `sakoo`, `market_maker`), the possible `flags` values (e.g. `"isRisky"`), and `otc_buy_enabled` / `otc_sell_enabled` are seen in the real data but documented nowhere; only their types are known.
- **Sub-account order endpoints** — `spot_swagger.json` defines `CreateSubAccountOrderRequest` / `CancelSubAccountOrderRequest` schemas but documents no paths that use them.
- **Shape of `result_info` and the `result` map keys** in the order-list responses — declared empty / unspecified in `spot_swagger.json`.
- **Account's own standalone trade history** — still no dedicated endpoint; per-order `fills` (`TradeResource`) are now documented via `spot_swagger.json`, and `GET /v1/trades` (`wallex_swagger.json`) is market-wide only.
- **Order placement rate limit** — 20 requests per 10 seconds (documented in official docs); not in swagger.
- **Full list of `status` strings** — not enumerated; probe will discover.
- **`stopPriceCondition` meaning** — unknown.
- **MARKET order price requirement** — docs say required, Swagger says optional; client currently omits for MARKET (open question).

**General API behavior**
- **Rate limits** — only order placement limit (20/10s) documented; other endpoints unmentioned.
- **Error responses of the `wallex_swagger.json` endpoints** — that file documents only `200`/`201`; error schemas and codes are declared solely on `spot_swagger.json`'s paths.
- **WebSocket / streaming APIs** — not mentioned in either file.
- **Order book depth control** — neither `GET /v1/depth` (`wallex_swagger.json`) nor `GET /depth/all` (`spot_swagger.json`) has a `limit`/depth parameter.
- **Full list of candle resolutions** — still only the example text "e.g. 1, 5, 15, 60, D" (`wallex_swagger.json`).
- **Pagination** for markets, balances, depth, and history — not documented (the order lists and IBANs do have `page`/`per_page`).
- **API-key scopes/permissions** and key management — not documented in either file; the files only say to send the key in the `x-api-key` header.
- **Sandbox / test environment** — not mentioned.

*Removed from this list in this update (now answered by `spot_swagger.json` and official docs):* create order, cancel order, order status, open orders list, order types limit/market/stop-limit (+ stop-market), server-side stop-order registration, error responses for the spot endpoints, pagination for the order lists, order object shape, dict‑of‑lists result envelope, client_id validation rule, order placement rate limit.

---

## Real read-only probe findings (STEP 4b — to be confirmed where noted)

Facts recorded from the real probe run against `https://api.wallex.ir`:

- **Symbol format** — the plain concatenated form (`BTCTMN`) works for both `/v1/depth` and `/v1/udf/history`.
- **Precisions** — `price_precision = 0` for all four configured symbols; `amount_precision`: BTC 8, ETH 5, SOL 5, XRP 1.
- **Market flags** — `is_market_type_enable`: BTC True, ETH True, SOL False, XRP False (meaning undocumented; possibly “MARKET orders not accepted”). `is_otc = true` on all four (not meaningful for spot).
- **Fees** — `GET /v1/account/fee` returns a dict keyed by market symbol; first market seen: maker 0.0025, taker 0.003. The client now parses ALL markets (`get_fee`) and exposes `get_fee_for(symbol)`.
- **Candles** — resolution `"5"` → `"Invalid resolution!"`; `"1"` and `"15"` returned IDENTICAL data (~360 one-minute candles); `"60"` and `"240"` returned IDENTICAL data (6 hourly candles); `"D"` returned 0 candles. Cause unknown (server aliasing vs client bug) — **to be confirmed by the updated probe**.
- **Rate-limit headers** — `x-ratelimit-limit = 600`, `x-ratelimit-remaining = 599` (window unknown).
- **Latency** — min ≈380 ms / avg ≈700 ms / max ≈2.8 s.
- **Top of book** — thin: best-ask notional ≈0.2M TMN (SOL), ≈0.3–7M (XRP), ≈3.8M (ETH). Planned order size ≈1M TMN.
- **Balances shape** — `{"TMN": {"free": float, "locked": float}, "BTC": {...}, ...}`; floats; key = asset code without quote suffix.

---

## Where each file applies

| Source | What it contributes to this document |
|---|---|
| `wallex_swagger.json` | Base URL declaration `https://api.wallex.com/` (CONFLICT with the verified fact); `x-api-key` security scheme; `GET /hector/web/v1/markets` (2.1 — its auth requirement conflicts with `spot_swagger.json`); `GET /v1/depth` (2.2); `GET /v1/udf/history` (2.3); `GET /v1/account/balances` (2.4); wallets, IBANs, money-withdrawal, card-numbers (other endpoints); `GET /v1/account/fee` (section 4 fee rates); `GET /v1/trades` |
| `spot_swagger.json` | Host/base-path declaration `api.wallex.com`, no scheme (CONFLICT with the verified fact); identical `x-api-key` security scheme; duplicate markets declaration (2.1, no security — CONFLICT); `GET /depth/all` (2.2, with the `sum`-type CONFLICT); `POST /v1/account/orders` (2.5); `DELETE /v1/account/orders` and `DELETE /v1/account/orders/{client_id}` (2.6); `GET /v1/account/orders/{client_id}` (2.7); `GET /v1/account/openOrders` (2.8); `GET /v1/account/orders` orders list (Spot endpoints); order types `LIMIT`/`MARKET`/`STOP_LIMIT`/`STOP_MARKET` (section 3); `OrderResource`/`TradeResource`/`ErrorResponse` schemas; unused sub-account and non-V1 envelope schemas (Spot endpoints) |
| `docs/samples/markets_raw.json` | Real response of `GET /hector/web/v1/markets`: top-level structure (`result`/`message`/`success`, markets under `result.markets`), 469 markets, spot/TMN/USDT/OTC counts (section 2.1). Large file — read only via a summary script, never dumped in full |
| `docs/samples/markets_sample.json` | Curated summary of the raw capture: real market-entry structure and field types; the real PEPETMN entry shown in section 2.1 |
| `docs/samples/market_symbols.txt` | All 469 market symbols — basis of the symbol-naming rules in section 2.1 |
| Project owner (verified fact given in the task, not from the swagger files) | `api.wallex.com` does not resolve from the owner's network (DNS error `getaddrinfo failed`); `api.wallex.ir` works → adopted as the base URL to use |

---

## Summary (Sections 3, 4 and 6) — 10 lines

1. `spot_swagger.json` documents four order types — `LIMIT`, `MARKET`, `STOP_LIMIT`, `STOP_MARKET` (enum on the orders-list `type` filter); **OCO remains undocumented** in both files.
2. All types share one create schema (`POST /v1/account/orders`): required `side`, `symbol`, `type`; optional `price`, `quantity`, `stop_Price`, `client_id` — per-type field requirements are not documented.
3. Stop orders can be registered server-side: the create request takes `stop_Price`, and the returned order resource includes `stopPrice` and `stopPriceCondition`, so a stop order (e.g., a sell stop) can live on the server.
4. The term "stop-loss" itself never appears in either file, and no OCO/bracket (stop-loss + take-profit) mechanism, trigger direction, or time-in-force semantics are documented.
5. In the markets response, price precision is `price_precision` (integer) and quantity precision is `amount_precision` (integer); both files define this schema identically.
6. The markets response still has no spot minimum-order-size field, and fees appear there only as the `is_zero_fee` boolean — whose meaning is unknown; `otc_min_notional` is OTC-only and must not be used as the spot minimum.
7. Actual fee rates remain available only via `wallex_swagger.json`'s `GET /v1/account/fee` (`makerFeeRate`, `takerFeeRate`, `recent_days_sum`).
8. Remaining unknowns: OCO, minimum order size, rate limits (absent from both files), order-status value enums, per-type field requirements, `stopPriceCondition` values, and the sub-account order paths.
9. Also still unknown: WebSocket streams, order-book depth limits, the full candle-resolution list, pagination for markets/balances/depth/history, error responses of the `wallex_swagger.json` endpoints, and API-key scopes.
10. `spot_swagger.json` resolved the biggest former unknowns — all order endpoints (create, cancel, status, open/all lists), stop-order support, and error responses (`400`/`401`/`404`/`422`/`500` with an `ErrorResponse` schema) — and `docs/samples/` now grounds the markets docs in reality (real structure, one real entry, symbol naming, market counts), while the spot minimum order size and the meaning of `is_zero_fee` remain unknown.

---

## Changes made in this update (step 2)

**New endpoints added (all from `spot_swagger.json`):**
- `POST /v1/account/orders` — create order (section 2.5)
- `DELETE /v1/account/orders` — cancel order via body `clientOrderId` (section 2.6)
- `DELETE /v1/account/orders/{client_id}` — cancel order via path parameter (section 2.6)
- `GET /v1/account/orders/{client_id}` — order status/details (section 2.7)
- `GET /v1/account/openOrders` — active/open orders list (section 2.8)
- `GET /v1/account/orders` — paginated orders list with filters (Spot endpoints)
- `GET /depth/all` — depth for all markets (section 2.2)

**Sections updated:**
- Header — sources now include `spot_swagger.json` and the verified base-URL fact; `markets_raw.json` noted as missing.
- 1 — base URL changed to `https://api.wallex.ir` with the CONFLICT recorded; `spot_swagger.json`'s host/scheme/auth recorded; auth header confirmed identical in both files (no conflict).
- 2 (intro), 2.1 (duplicate markets declaration + auth CONFLICT), 2.2 (`/depth/all` added; `sum`-type CONFLICT), 2.4 (note: balances absent from `spot_swagger.json`), 2.5–2.8 (filled in from `spot_swagger.json`), Other endpoints (source clarified).
- New "Spot endpoints" section — orders list with filters, error responses, defined-but-pathless schemas.
- 3 — order types `LIMIT`/`MARKET`/`STOP_LIMIT`/`STOP_MARKET` with the shared create fields; server-side stop orders documented; OCO still absent.
- 4 — sources noted; no new fields (identical `allMarkets` schema in both files); fee endpoint kept, attributed to `wallex_swagger.json`.
- 5 — no rate limits in `spot_swagger.json` either (only pagination caps).
- 6 — removed items now answered (all order endpoints, order types, stop orders, spot error responses, order-list pagination); kept/added remaining unknowns.
- New "Where each file applies" section; summary rewritten.

**CONFLICTs found (both versions kept, sources named):**
1. **Base URL / host** — `wallex_swagger.json` declares `https://api.wallex.com/` (scheme `https`, host `api.wallex.com`, basePath `/`); `spot_swagger.json` declares host `api.wallex.com`, basePath `/`, and no scheme; the project owner's verified fact says `api.wallex.com` does not resolve (DNS `getaddrinfo failed`) and `api.wallex.ir` works. The document adopts `https://api.wallex.ir` as the base URL to use.
2. **Markets endpoint authentication** — `wallex_swagger.json` requires `ApiKeyAuth` on `GET /hector/web/v1/markets`; `spot_swagger.json` declares no security for the same path/operation.
3. **Depth entry `sum` type** — string in `wallex_swagger.json` (`OrderBookEntry`, `GET /v1/depth`) vs number in `spot_swagger.json` (`DepthTupleResource`, `GET /depth/all`).
4. **Authentication header — no conflict:** both files declare the same `x-api-key` header (recorded explicitly).

---

## Changes made in this update (step 3)

Sources added: `docs/samples/markets_sample.json`, `docs/samples/market_symbols.txt`, `docs/samples/markets_raw.json` (counts only, via a summary script).

- **2.1** — the schema-derived markets skeleton was **replaced** with the real response structure (top-level keys `result`, `message`, `success`; markets array under `result.markets`; capture values `message: "Success"`, `success: true`, 469 markets) and one real entry (PEPETMN). Added: a field-type table from the real data (the 19 requested fields), market-population counts (373 spot; of those 187 TMN-based and 186 USDT-based; 442 OTC across all markets), and the symbol-naming rules from `market_symbols.txt` (base+quote concatenation; `TMN` suffix = toman-quoted, `USDT` suffix = USDT-quoted; 235 TMN vs 234 USDT symbols).
- **4** — `otc_min_notional` / `otc_max_notional` documented (integer) with an explicit warning: they apply to OTC markets only and must NOT be treated as the spot minimum order size. The **spot minimum order size is marked Unknown**. The meaning of **`is_zero_fee` is marked Unknown**. Real precision examples added (`price_precision`, `amount_precision`).
- **6 (Unknown)** — spot minimum order size kept (now with the OTC warning), `is_zero_fee` meaning added, and a new item lists real fields whose semantics are undocumented (`is_market_type_enable`, `fair_price` sub-objects, `flags` values such as `"isRisky"`, `otc_buy_enabled`/`otc_sell_enabled`).
- **Where each file applies** — rows added for the three `docs/samples/` files; the header sources updated.
- **No new CONFLICTs.** One schema-vs-reality discrepancy recorded in 2.1: the real response is a superset of both schemas (8 extra fields), and `leverage_step`/`max_leverage` can be `null` in reality where the schemas type them as plain numbers.
