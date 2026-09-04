# API reference

Base URL in development: `http://localhost:4000`

Three ways to authenticate:

| Caller | Header | Used by |
|---|---|---|
| A person | `Authorization: Bearer <token>` | The web app, after login |
| A shop's till | `x-api-key: mela_sk_...` | Billing counters, POS integrations |
| Nobody | *(none)* | The public endpoints |

All bodies are JSON. All money is **paise** (integers). All token amounts are **wei**
(strings, because they exceed JavaScript's safe integer range).

Errors come back as `{"error": "a message you can show the user"}` with a sensible
status: `400` a rule was broken, `401` not signed in, `403` wrong role, `404` missing,
`409` duplicate, `429` too many requests, `500` our bug.

---

## Public

| Method | Path | Returns |
|---|---|---|
| `GET` | `/api/health` | Liveness and the current chain mode |
| `GET` | `/api/public/vendors` | Every active shop and its rates |
| `GET` | `/api/public/token` | MELA name, symbol, decimals, price, addresses |

## Accounts

| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/auth/register` | `role: "customer"` or `"vendor"`. A vendor also needs `shop_name`, and gets `api_key` back **once**. |
| `POST` | `/api/auth/login` | Returns `token` |
| `POST` | `/api/auth/logout` | Invalidates the token immediately |
| `GET` | `/api/auth/me` | The signed-in user and their shop, if any |

## Customer *(customer token)*

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/customer/summary` | Points per shop, MELA balance, totals. One call for the home screen. |
| `GET` | `/api/customer/points/:vendorId` | The individual lots, in the order they will be spent |
| `GET` | `/api/customer/history` | Purchases, point events, conversions, token ledger |
| `PATCH` | `/api/customer/profile` | Set `wallet_address` or `phone` |

## Wallet *(customer token)*

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/wallet` | Balance, ledger, withdrawals, chain mode |
| `POST` | `/api/wallet/convert/quote` | `{vendor_id, points}` → a full breakdown. Changes nothing. |
| `POST` | `/api/wallet/convert` | Same, plus `expect_mela_wei` from the quote. Refuses if the price moved. |
| `POST` | `/api/wallet/withdraw` | `{to_address, mela_wei}` → a signed voucher (or a record, in mock mode) |

## Vendor *(vendor token)*

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/vendor/me` | Shop settings, sales stats, outstanding liability |
| `PATCH` | `/api/vendor/settings` | Any of the rate fields below |
| `POST` | `/api/vendor/api-key` | New till key; the old one dies instantly |
| `GET` | `/api/vendor/purchases` | Recent sales |
| `GET` | `/api/vendor/customers` | Who holds this shop's points |
| `GET` | `/api/vendor/settlement` | The running account with the platform |

Editable settings: `name`, `category`, `city`, `earn_milli_points_per_rupee`,
`redeem_milli_paise_per_point`, `min_redeem_points`, `max_redeem_bps`,
`points_expiry_days`, `mela_conversion_fee_bps`, `earn_on_net`,
`allow_mela_conversion`, `accepts_mela`, `active`.

## POS *(x-api-key)*

This is what a shop's billing system integrates with.

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/pos/customer?customer=<email or phone>` | Look up a customer's standing before billing |
| `POST` | `/api/pos/quote` | Price a bill. Safe to call on every keystroke. |
| `POST` | `/api/pos/purchase` | Commit it. **Requires `idempotency_key`.** |

## Admin *(admin token)*

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/admin/stats` | Volume, revenue, MELA outstanding, backing ratio |
| `POST` | `/api/admin/mela-price` | `{mela_price_paise}` |
| `GET` | `/api/admin/settlements` | Every shop with a non-zero balance |
| `POST` | `/api/admin/settlements/payment` | Record money actually moving |
| `POST` | `/api/admin/expire-points` | Run the expiry sweep by hand |
| `GET` | `/api/admin/audit` | Who changed what, when |

---

## A complete till integration, in four calls

```bash
BASE=http://localhost:4000
KEY=mela_sk_your_till_key

# 1. Who is this customer, and what do they have with us?
curl -s "$BASE/api/pos/customer?customer=anita@example.test" -H "x-api-key: $KEY"

# 2. Price a Rs 500 bill where they want to use 300 points.
curl -s -X POST "$BASE/api/pos/quote" -H "x-api-key: $KEY" \
  -H 'content-type: application/json' \
  -d '{"customer":"anita@example.test","gross_paise":50000,"points_to_redeem":300}'
```

```jsonc
// The reply tells the cashier everything, including the limits:
{
  "gross_paise": 50000,
  "points_available": 2740,
  "max_redeemable_points": 5000,   // what the shop's cap allows on this bill
  "points_discount_paise": 1500,   // 300 points x 5 paise = Rs 15
  "net_paise": 48500,              // what to actually charge
  "points_earned": 970,            // what they'll get for paying Rs 485
  "display": { "net": "₹485.00" }  // pre-formatted, ready to print
}
```

```bash
# 3. Commit it. The idempotency key is YOUR bill number.
curl -s -X POST "$BASE/api/pos/purchase" -H "x-api-key: $KEY" \
  -H 'content-type: application/json' \
  -d '{"customer":"anita@example.test","gross_paise":50000,
       "points_to_redeem":300,"idempotency_key":"INV-2024-0912","bill_ref":"INV-2024-0912"}'

# 4. Network dropped? Send the exact same request again.
#    You get the original receipt with "replayed": true. Nothing is charged twice.
```

That is the entire integration. A shop's developer should be done in an afternoon.
