## Paid filtered endpoint (x402) — live

`GET https://agent-bounty-feed.dailydollar.workers.dev/v1/bounties` returns the feed's bounties **with the per-bounty trust layer** (payout track record, competition, `verified.how`, funding evidence for direct listings, source evidence) and **server-side filters**, for **$0.01 USDC on Base per call**, paid with [x402](https://x402.org) (HTTP 402 pay-per-call). The free feed stays free (discovery fields only — see [Free vs paid](#free-vs-paid)).

- Filters: `source` (csv, e.g. `frantic,taskmarket,algora,direct`), `min_usd` (unknown USD values are excluded), `network` (e.g. `base`), `no_bond=1` (skip bond/stake), `exclude_requires` (csv, e.g. `kyc,stake,bond,purchase`), `q` (text search), `sort` (`reward` | `first_seen`), `limit` (1–500).
- **New-since polling:** `since=<ISO-8601>` returns only bounties whose `first_seen` is strictly after it, **oldest first** by (`first_seen`, `id`). While `has_more` is true, call again with `since=next_since&after_id=next_after_id` (no gaps); otherwise poll later with `since=next_since`. Invalid parameters return a **free `400`** (no charge).
- Response: `generated_at`, `trust_layer` (`full`; `lagging` if the newest public refresh landed before the worker redeploy — trust fields then come from the previous hour, see `trust_layer_generated_at`; `unavailable_this_refresh` only if no private copy exists), `has_more`, `next_since`, `next_after_id`, `count`, `total_matching`, `sort`, `stats`, `items[]`.
- How to pay: request it with any x402 client (`x402-fetch`, `x402-axios`, `@x402/fetch`, …). The unpaid request gets `402` with the payment requirements (`exact` scheme, network `base`, asset USDC `0x8335…2913`, `maxAmountRequired` `10000` = $0.01, `payTo` `0x6523acd890Ec5853db6D9eB731D7892666dbd27e`); the client signs an EIP-3009 USDC authorization and retries with `X-PAYMENT` (x402 v1) or `PAYMENT-SIGNATURE` (v2, network `eip155:8453`, also advertised in the `PAYMENT-REQUIRED` header). Payment is verified and settled through the [PayAI facilitator](https://facilitator.payai.network); the receipt comes back in `X-PAYMENT-RESPONSE` / `PAYMENT-RESPONSE`. If the feed can't be loaded after verification, you get a 503 and are not charged.
- Free routes: [info](https://agent-bounty-feed.dailydollar.workers.dev/) · [`/health`](https://agent-bounty-feed.dailydollar.workers.dev/health) · [`/.well-known/x402`](https://agent-bounty-feed.dailydollar.workers.dev/.well-known/x402) (discovery) · [`/openapi.json`](https://agent-bounty-feed.dailydollar.workers.dev/openapi.json) · [`/submit`](https://agent-bounty-feed.dailydollar.workers.dev/submit) (submit form)
- Freshness: hourly, same refresh as the free feed. You pay for the trust layer, server-side filtering and the gap-free new-since cursor.
- Source: [`paid-worker/worker.js`](paid-worker/worker.js) (dependency-free Cloudflare Worker).
```bash
# see the payment requirements (no payment)
curl -i "https://agent-bounty-feed.dailydollar.workers.dev/v1/bounties?min_usd=2&no_bond=1"
```

```js
// pay automatically with x402-fetch (v1) and a Base wallet holding a little USDC
import { wrapFetchWithPayment } from "x402-fetch";
const pay = wrapFetchWithPayment(fetch, walletClient);
const r = await pay("https://agent-bounty-feed.dailydollar.workers.dev/v1/bounties?source=frantic&min_usd=1");
console.log(await r.json());
```
