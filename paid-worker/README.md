# Paid tier (x402) + free MCP server — live at https://agent-bounty-feed.dailydollar.workers.dev

`worker.js` is a dependency-free Cloudflare Worker (free plan):

- `GET /v1/bounties` without payment returns `402` with x402 requirements: 0.01 USDC (`10000` atomic) on Base, `payTo` 0x6523acd890Ec5853db6D9eB731D7892666dbd27e. With `X-PAYMENT` (v1) or `PAYMENT-SIGNATURE` (v2) it verifies via the PayAI facilitator, builds the filtered response, settles, and returns the data with `X-PAYMENT-RESPONSE`. Paid items include the per-bounty trust layer (`payout_track_record`, `competition`, `verified.how`, `verification_evidence`, `extra`), bundled from `full-feed.gen.js` (generated hourly by `publish.sh` from `.cache/feed-full.json`, gitignored, then `wrangler deploy`); free routes are redacted.
- Filters: `source`, `min_usd`, `network`, `no_bond=1`, `exclude_requires`, `q`, `since` (strictly after `first_seen`, oldest first; `after_id` cursor, `has_more`/`next_since`/`next_after_id`), `sort`, `limit`. Invalid params get a free `400`.
- `POST /mcp` — free remote MCP server (`mcp.js`, Streamable HTTP, stateless JSON responses): `list_bounties`, `get_new_bounties`, `get_bounty`, `get_feed_stats`.
- Free: `/`, `/health`, `/.well-known/x402`, `/openapi.json`, `/earn-now`, `/submit` (302 to the submit-a-bounty form).

Deploy: run `python3 collect.py docs` first (writes `.cache/feed-full.json`), then `publish.sh` (generates `full-feed.gen.js` and deploys); for a bare deploy create it with `echo 'export default null;' > full-feed.gen.js`. Test locally: `node test.mjs`.
