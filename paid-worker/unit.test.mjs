// Unit tests for feed integrity (no network). Run: node paid-worker/unit.test.mjs
import assert from "node:assert/strict";
import w, { _test as T } from "./worker.js";
import { mcp } from "./mcp.js";
let n = 0; const ok = (name, fn) => { fn(); n++; console.log("ok -", name); };
const q = (s) => { const r = T.parseQuery(new URLSearchParams(s)); assert.deepEqual(r.errs, []); return r.opts; };
const GEN = "2026-10-28T00:00:00Z";

// ---- Bug 1: unknown USD stays unknown
const usdFeed = { generated_at: GEN, items: [
  { id: "taskmarket:TSK-Y7YSTT0V", reward: 100, currency: "OUSD", est_usd: null, first_seen: "2026-10-27T01:00:00Z" },
  { id: "taskmarket:TSK-FXES37VN", reward: 50, currency: "OUSD", est_usd: null, first_seen: "2026-10-27T02:00:00Z" },
  { id: "x:usdc50", reward: 50, currency: "USDC", est_usd: 50, first_seen: "2026-10-27T03:00:00Z" },
  { id: "x:usdc50-noest", reward: 50, currency: "USDC", first_seen: "2026-10-27T03:30:00Z" },
  { id: "x:zero", reward: 0, currency: "USD", est_usd: 0, first_seen: "2026-10-27T04:00:00Z" },
  { id: "x:bad", reward: "lots", currency: "USD", est_usd: "50", first_seen: "2026-10-27T05:00:00Z" },
  { id: "x:nan", reward: NaN, currency: "USDC", first_seen: "2026-10-27T06:00:00Z" },
  { id: "x:none", first_seen: "2026-10-27T07:00:00Z" },
] };
ok("knownUsd: OUSD/null -> null; USDC explicit -> 50; USD-like reward fallback; malformed -> null", () => {
  const by = Object.fromEntries(usdFeed.items.map(i => [i.id, T.knownUsd(i)]));
  assert.equal(by["taskmarket:TSK-Y7YSTT0V"], null); assert.equal(by["taskmarket:TSK-FXES37VN"], null);
  assert.equal(by["x:usdc50"], 50); assert.equal(by["x:usdc50-noest"], 50); assert.equal(by["x:zero"], 0);
  assert.equal(by["x:bad"], null); assert.equal(by["x:nan"], null); assert.equal(by["x:none"], null);
});
ok("min_usd=50 excludes 100 OUSD/null and 50 OUSD/null, keeps explicit 50 USDC", () => {
  const ids = T.applyFilter(usdFeed, q("min_usd=50")).items.map(i => i.id).sort();
  assert.deepEqual(ids, ["x:usdc50", "x:usdc50-noest"]);
});
ok("min_usd=0.01 excludes zero/malformed/unknown", () => {
  const ids = T.applyFilter(usdFeed, q("min_usd=0.01")).items.map(i => i.id);
  for (const bad of ["x:zero", "x:bad", "x:nan", "x:none", "taskmarket:TSK-Y7YSTT0V"]) assert.ok(!ids.includes(bad), bad);
});
ok("sort=reward puts unknown USD after known values", () => {
  const ids = T.applyFilter(usdFeed, q("sort=reward")).items.map(i => i.id);
  const firstNull = ids.findIndex(id => T.knownUsd(usdFeed.items.find(i => i.id === id)) === null);
  assert.ok(ids.slice(firstNull).every(id => T.knownUsd(usdFeed.items.find(i => i.id === id)) === null));
  assert.deepEqual(ids.slice(0, 2).sort(), ["x:usdc50", "x:usdc50-noest"]);
});

// ---- Bug 2: gap-free delta pages
const mk = (k, ts) => ({ id: `t:${String(k).padStart(3, "0")}`, reward: k, currency: "USD", est_usd: k, first_seen: ts });
const pad = (m) => `2026-10-27T10:${String(m).padStart(2, "0")}:00Z`;
function drain(feed, since, limit, extra = "") {
  const got = []; let cur = { since, after: null }, pages = 0;
  for (;;) {
    const s = `since=${cur.since}&limit=${limit}${cur.after ? "&after_id=" + encodeURIComponent(cur.after) : ""}${extra}`;
    const r = T.filter(feed, new URLSearchParams(s), q(s)); pages++;
    got.push(...r.items.map(i => i.id));
    if (!r.has_more) { assert.equal(r.next_since, feed.generated_at); assert.equal(r.next_after_id, null); return { got, pages }; }
    assert.ok(r.next_since && r.next_after_id); cur = { since: r.next_since, after: r.next_after_id };
    assert.ok(pages < 50, "runaway paging");
  }
}
ok("25 new unique items, limit 20 -> 2 pages, all 25 delivered once", () => {
  const feed = { generated_at: GEN, items: Array.from({ length: 25 }, (_, k) => mk(k, pad(k + 1))) };
  const first = T.applyFilter(feed, q("since=2026-10-27T00:00:00Z&limit=20"));
  assert.equal(first.items.length, 20); assert.equal(first.has_more, true); assert.equal(first.total_matching, 25);
  const { got, pages } = drain(feed, "2026-10-27T00:00:00Z", 20);
  assert.equal(pages, 2); assert.equal(got.length, 25); assert.equal(new Set(got).size, 25);
});
ok("tied timestamps across page boundary are not lost or duplicated", () => {
  const feed = { generated_at: GEN, items: Array.from({ length: 25 }, (_, k) => mk(k, k < 3 ? pad(1) : pad(2))) }; // 3 at :01, 22 at :02
  for (const lim of [1, 2, 3, 5, 20, 22, 24]) {
    const { got } = drain(feed, "2026-10-27T00:00:00Z", lim);
    assert.equal(got.length, 25, "limit " + lim); assert.equal(new Set(got).size, 25, "limit " + lim);
  }
});
ok("sort=reward with since still pages gap-free", () => {
  const feed = { generated_at: GEN, items: Array.from({ length: 25 }, (_, k) => mk(k, pad(k % 4))) };
  const { got } = drain(feed, "2026-10-27T00:00:00Z", 7, "&sort=reward");
  assert.equal(new Set(got).size, 25);
});
ok("empty drain: has_more=false, next_since=generated_at, count 0", () => {
  const feed = { generated_at: GEN, items: [mk(1, pad(1))] };
  const r = T.filter(feed, new URLSearchParams(), q("since=2026-10-27T12:00:00Z&limit=20"));
  assert.equal(r.count, 0); assert.equal(r.has_more, false); assert.equal(r.next_since, GEN);
});
ok("no since + truncated: next_since null (not a safe baseline)", () => {
  const feed = { generated_at: GEN, items: Array.from({ length: 5 }, (_, k) => mk(k, pad(k))) };
  const r = T.applyFilter(feed, q("limit=2")); assert.equal(r.has_more, true); assert.equal(r.next_since, null);
  assert.equal(T.applyFilter(feed, q("limit=5")).next_since, GEN);
});
ok("after_id without since is a free 400-style error", () => {
  assert.ok(T.parseQuery(new URLSearchParams("after_id=t:001")).errs.length === 1);
});
// MCP path uses the same cursor
const feed25 = { generated_at: GEN, items: Array.from({ length: 25 }, (_, k) => mk(k, pad(k + 1))) };
const call = async (name, args) => { const r = await mcp(new Request("https://x/mcp", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/call", params: { name, arguments: args } }) }), new URL("https://x/mcp"), { getFeed: async () => feed25, parseQuery: T.parseQuery, applyFilter: T.applyFilter }); return (await r.json()).result.structuredContent; };
{
  const a = await call("get_new_bounties", { since: "2026-10-27T00:00:00Z", limit: 20 });
  assert.equal(a.count, 20); assert.equal(a.has_more, true);
  const b = await call("get_new_bounties", { since: a.next_since, after_id: a.next_after_id, limit: 20 });
  assert.equal(b.count, 5); assert.equal(b.has_more, false); assert.equal(b.next_since, GEN);
  assert.equal(new Set([...a.items, ...b.items].map(i => i.id)).size, 25); n++; console.log("ok - MCP get_new_bounties 20+5 pages");
  const c = await call("list_bounties", { min_usd: 50 });
  assert.equal(c.count, 0); n++; console.log("ok - MCP list_bounties min_usd filter");
}
{
  const r = await mcp(new Request("https://x/mcp", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/call", params: { name: "list_bounties", arguments: { min_usd: 50 } } }) }), new URL("https://x/mcp"), { getFeed: async () => usdFeed, parseQuery: T.parseQuery, applyFilter: T.applyFilter });
  const ids = (await r.json()).result.structuredContent.items.map(i => i.id);
  assert.ok(!ids.some(id => id.startsWith("taskmarket:"))); n++; console.log("ok - MCP min_usd=50 excludes OUSD-null rows");
}

// ---- Bug 3: earn-now conservative eligibility + freshness
const NOW = Date.parse("2026-10-28T00:00:00Z");
const v = (h) => ({ checked_at: new Date(NOW - h * 3600e3).toISOString(), how: "t" });
const earnFeed = { generated_at: GEN, items: [
  { id: "frantic:97", kind: "bounty", reward: 10, currency: "USD", est_usd: 10, requires: ["agent_account", "purchase", "wallet_evm"], verified: v(0.5) },
  { id: "ok:fresh", kind: "bounty", reward: 5, currency: "USD", est_usd: 5, requires: ["agent_account"], verified: v(1) },
  { id: "old:verify", kind: "bounty", reward: 6, currency: "USD", est_usd: 6, requires: [], verified: v(7) },
  { id: "stale:flag", kind: "bounty", reward: 7, currency: "USD", est_usd: 7, requires: [], verified: v(1), stale: true },
  { id: "past:deadline", kind: "bounty", reward: 8, currency: "USD", est_usd: 8, requires: [], verified: v(1), deadline: "2026-10-27T23:00:00Z" },
  { id: "ousd:null", kind: "bounty", reward: 100, currency: "OUSD", est_usd: null, requires: [], verified: v(1) },
  { id: "no:verify", kind: "bounty", reward: 9, currency: "USD", est_usd: 9, requires: [] },
  { id: "fetched:recent", kind: "bounty", reward: 2, currency: "USDC", requires: [], fetched_at: new Date(NOW - 3600e3).toISOString() },
] };
ok("earn-now: excludes purchase (frantic:97), stale, >6h unverified, past deadline, OUSD-null", () => {
  const e = T.earnNow(earnFeed, NOW);
  assert.deepEqual(e.items.map(i => i.id), ["ok:fresh", "fetched:recent"]);
  assert.equal(e.excluded_not_fresh, 4);
});
ok("earn-now: excludes items whose source detail check failed (verified.degraded)", () => {
  const f = { generated_at: new Date(NOW).toISOString(), items: [
    { id: "frantic:deg", kind: "bounty", reward: 20, currency: "USD", est_usd: 20, requires: ["agent_account"], verified: { ...v(0.5), degraded: true } },
    { id: "frantic:ok", kind: "bounty", reward: 3, currency: "USD", est_usd: 3, requires: ["agent_account"], verified: v(0.5) },
  ] };
  const e = T.earnNow(f, NOW);
  assert.deepEqual(e.items.map(i => i.id), ["frantic:ok"]);
  assert.equal(e.excluded_not_fresh, 1);
});
ok("earn-now: shared pools list only a source-stated per-worker amount (TSK-9Y6447C6 drops out)", () => {
  const f = { generated_at: new Date(NOW).toISOString(), items: [
    { id: "taskmarket:TSK-9Y6447C6", kind: "bounty", reward: 1, currency: "USDC", est_usd: 1, reward_basis: "shared_pool", pool_amount: 1, per_worker_usd: null, requires: ["agent_account"], verified: v(0.5) },
    { id: "pool:stated-low", kind: "bounty", reward: 5, currency: "USDC", est_usd: 5, reward_basis: "shared_pool", pool_amount: 5, per_worker_usd: 0.5, requires: [], verified: v(0.5) },
    { id: "pool:stated-ok", kind: "bounty", reward: 10, currency: "USDC", est_usd: 10, reward_basis: "shared_pool", pool_amount: 10, per_worker_usd: 2, requires: [], verified: v(0.5) },
    { id: "single:2", kind: "bounty", reward: 2, currency: "USDC", est_usd: 2, requires: [], verified: v(0.5) },
  ] };
  const e = T.earnNow(f, NOW);
  assert.deepEqual(e.items.map(i => [i.id, i.est_usd]), [["pool:stated-ok", 2], ["single:2", 2]]);
  assert.equal(T.earnUsd(f.items[0]), null);
});
ok("earn-now: Superteam ranked prize totals are not one payout; raw 0.999 stays below the $1 bar", () => {
  const f = { generated_at: new Date(NOW).toISOString(), items: [
    { id: "superteam:raptorx", kind: "bounty", reward: 125, currency: "USDC", est_usd: 125, reward_basis: "ranked_prizes", pool_amount: 125, per_worker_usd: null, requires: ["agent_account"], verified: v(0.5) },
    { id: "pool:0999", kind: "bounty", reward: 5, currency: "USDC", est_usd: 5, reward_basis: "shared_pool", pool_amount: 5, per_worker_usd: 0.999, requires: [], verified: v(0.5) },
    { id: "pool:1000", kind: "bounty", reward: 5, currency: "USDC", est_usd: 5, reward_basis: "shared_pool", pool_amount: 5, per_worker_usd: 1.0, requires: [], verified: v(0.5) },
  ] };
  const e = T.earnNow(f, NOW);
  assert.deepEqual(e.items.map(i => i.id), ["pool:1000"]);
  assert.equal(T.earnUsd(f.items[0]), null);
});

// ---- Free vs paid: per-item trust layer is paid-only
const RICH = { id: "direct:demo", source: "direct", kind: "bounty", title: "t", reward: 5, currency: "USDC", est_usd: 5, url: "https://example.com/b",
  requires: ["purchase"], deadline: null, slots_left: 1, eligibility: "e", first_seen: "2026-10-27T01:00:00Z", fetched_at: "2026-10-27T01:00:00Z",
  competition: { attempts: 3 }, payout_track_record: { paid: 7, basis: "x" }, verification_evidence: "escrow tx 0xabc", extra: { sponsor_url: "s" },
  verified: { checked_at: "2026-10-27T01:00:00Z", how: "escrow checked", degraded: true }, verified_direct: true };
ok("redactItem drops trust layer, keeps discovery + freshness flags + purchase tag", () => {
  const r = T.redactItem(RICH);
  for (const k of T.TRUST_FIELDS) assert.equal(k in r, false, k);
  assert.deepEqual(r.verified, { checked_at: "2026-10-27T01:00:00Z", degraded: true });
  assert.equal(r.trust_layer, "paid");
  for (const k of ["id", "title", "reward", "url", "requires", "deadline", "slots_left", "est_usd", "first_seen", "verified_direct"]) assert.deepEqual(r[k], RICH[k], k);
  assert.equal(RICH.payout_track_record.paid, 7); // input not mutated
});
ok("paid filter() keeps the full trust layer and labels it", () => {
  const out = T.filter({ generated_at: GEN, items: [RICH] }, new URLSearchParams("source=direct"), q("source=direct"));
  assert.equal(out.trust_layer, "full"); assert.equal(out.items[0].payout_track_record.paid, 7);
  assert.equal(out.items[0].verified.how, "escrow checked"); assert.equal(out.items[0].verification_evidence, "escrow tx 0xabc");
});
{
  const feed = { generated_at: GEN, stats: {}, items: [RICH, { ...RICH, id: "frantic:1", source: "frantic", requires: [] }] };
  const deps = { getFeed: async () => T.redactFeed(feed), parseQuery: T.parseQuery, applyFilter: T.applyFilter, redactItem: T.redactItem };
  const call = async (name, args) => { const r = await mcp(new Request("https://x/mcp", { method: "POST", body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/call", params: { name, arguments: args } }) }), new URL("https://x/mcp"), deps); return (await r.json()).result.structuredContent; };
  const lb = await call("list_bounties", { source: "direct" });
  ok("MCP list_bounties source=direct works and omits trust layer", () => {
    assert.deepEqual(lb.items.map(i => i.id), ["direct:demo"]);
    const txt = JSON.stringify(lb.items);
    for (const k of ["payout_track_record", "competition", "verification_evidence", "escrow checked", "sponsor_url"]) assert.equal(txt.includes(k), false, k);
    assert.match(lb.note, /submit-bounty\.yml/);
  });
  const gb = await call("get_bounty", { id: "direct:demo" });
  ok("MCP get_bounty is redacted even if the upstream item were rich", () => {
    assert.equal("payout_track_record" in gb.item, false); assert.equal(gb.item.verified.how, undefined);
  });
  const tl = await (await mcp(new Request("https://x/mcp", { method: "POST", body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/list" }) }), new URL("https://x/mcp"), deps)).json();
  ok("MCP tool descriptions advertise direct submissions", () => { assert.match(JSON.stringify(tl), /submit-bounty\.yml/); assert.match(JSON.stringify(tl), /direct/); });
}
ok("earn-now output carries no trust layer and links the submit form", () => {
  const now = Date.parse("2026-10-27T02:00:00Z");
  const e = T.earnNow({ generated_at: GEN, items: [{ ...RICH, requires: [], verified: { checked_at: "2026-10-27T01:00:00Z", how: "h" } }] }, now);
  assert.equal(e.count, 1); assert.equal(JSON.stringify(e.items).includes("payout_track_record"), false);
  assert.match(e.submit_a_bounty, /submit-bounty\.yml/);
});
{
  const realFetch = globalThis.fetch;
  const pub = { generated_at: "2026-10-28T01:00:00Z", items: [T.redactItem(RICH), T.redactItem({ ...RICH, id: "new:1" })] };
  globalThis.fetch = async () => new Response(JSON.stringify(pub));
  const a = await T.getPaidFeed(null, { generated_at: "2026-10-28T01:00:00Z", items: [RICH] });
  const b = await T.getPaidFeed(null, null);
  const c = await T.getPaidFeed(null, { generated_at: "2026-10-28T00:00:00Z", items: [RICH] });
  globalThis.fetch = realFetch;
  ok("getPaidFeed: bundled full feed when current; enriched 'lagging' when older; marked fallback when missing", () => {
    assert.equal(a.trust_layer, "full"); assert.equal(a.feed.items[0].payout_track_record.paid, 7);
    assert.equal(b.trust_layer, "unavailable_this_refresh");
    assert.equal(c.trust_layer, "lagging"); assert.equal(c.feed.items.length, 2);
    assert.equal(c.feed.items[0].payout_track_record.paid, 7); assert.equal(c.feed.items[0].verified.how, "escrow checked");
    assert.equal(c.feed.items[0].verified.checked_at, "2026-10-27T01:00:00Z"); assert.equal(c.feed.items[1].trust_layer_missing, true);
  });
}
// ---- No-charge-without-trust guard + lagging honesty (Codex strategy, shared#1)
{
  const OLD = "2026-10-28T00:00:00Z", NEW = "2026-10-28T01:00:00Z";
  const changed = [
    { ...T.redactItem(RICH), id: "direct:demo" },
    { ...T.redactItem({ ...RICH, id: "r:1" }), reward: 9 },
    { ...T.redactItem({ ...RICH, id: "s:1", status: "open" }), status: "closed" },
    { ...T.redactItem({ ...RICH, id: "d:1" }), deadline: "2026-12-01T00:00:00Z" },
    { ...T.redactItem({ ...RICH, id: "u:1" }), url: "https://example.com/other" },
  ];
  const full = { generated_at: OLD, items: [RICH, { ...RICH, id: "r:1" }, { ...RICH, id: "s:1", status: "open" }, { ...RICH, id: "d:1" }, { ...RICH, id: "u:1" }] };
  const e = T.enrich({ generated_at: NEW, items: changed }, full);
  const by = Object.fromEntries(e.items.map(i => [i.id, i]));
  ok("lagging enrich: unchanged item gets trust + trust_checked_at from the trust source", () => {
    assert.equal(by["direct:demo"].payout_track_record.paid, 7); assert.equal(by["direct:demo"].trust_checked_at, "2026-10-27T01:00:00Z");
    assert.equal(by["direct:demo"].trust_layer_missing, undefined);
  });
  ok("lagging enrich: reward/status/deadline/url changed vs older full copy -> no stale trust, marked missing with reason", () => {
    for (const [id, k] of [["r:1", "reward"], ["s:1", "status"], ["d:1", "deadline"], ["u:1", "url"]]) {
      const i = by[id];
      for (const f of T.TRUST_FIELDS) assert.equal(f in i, false, id + " " + f);
      assert.equal(i.verified.how, undefined, id); assert.equal(i.trust_layer_missing, true, id);
      assert.match(i.trust_missing_reason, new RegExp(k), id); assert.equal(i.trust_checked_at, undefined, id);
    }
    assert.deepEqual(T.diverged({ reward: 5, url: "a" }, { reward: 5, url: "a" }), []);
  });
  const realFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({ generated_at: NEW, items: changed.slice(1) }));
  const allChanged = await T.getPaidFeed(null, full);
  globalThis.fetch = realFetch;
  ok("getPaidFeed: lagging with zero usable enrichment is treated as unavailable_this_refresh", () => {
    assert.equal(allChanged.trust_layer, "unavailable_this_refresh");
  });
  ok("trustGuard: blocks unavailable/unknown, passes full/lagging", () => {
    assert.match(T.trustGuard({ trust_layer: "unavailable_this_refresh" }).error, /not charged/);
    assert.ok(T.trustGuard(null)); assert.ok(T.trustGuard({ trust_layer: "weird" }));
    assert.equal(T.trustGuard({ trust_layer: "full" }), null); assert.equal(T.trustGuard({ trust_layer: "lagging" }), null);
  });
  // End-to-end through handleBounties with a stubbed facilitator: settle must never be called without trust.
  const run = async (got) => {
    const calls = [];
    const deps = { facilitator: async (path) => { calls.push(path); return path === "/verify" ? { isValid: true } : { success: true, transaction: "0xt" }; },
      getPaidFeed: async () => { if (got instanceof Error) throw got; return got; } };
    const url = new URL("https://x/v1/bounties");
    const r = await T.handleBounties(new Request(url, { headers: { "X-PAYMENT": btoa(JSON.stringify({ x402Version: 1 })) } }), url, null, deps);
    return { r, calls, body: await r.json() };
  };
  const un = await run({ feed: { generated_at: NEW, items: [T.redactItem(RICH)] }, trust_layer: "unavailable_this_refresh", trust_layer_generated_at: null });
  ok("handleBounties: unavailable_this_refresh -> free 503, /verify only, /settle NOT called", () => {
    assert.equal(un.r.status, 503); assert.deepEqual(un.calls, ["/verify"]);
    assert.match(un.body.error, /not charged/); assert.equal(un.r.headers.get("X-PAYMENT-RESPONSE"), null);
    assert.equal(JSON.stringify(un.body).includes("direct:demo"), false);
  });
  const down = await run(new Error("x"));
  ok("handleBounties: feed down -> free 503, /settle NOT called", () => { assert.equal(down.r.status, 503); assert.deepEqual(down.calls, ["/verify"]); });
  const good = await run({ feed: { generated_at: OLD, items: [RICH] }, trust_layer: "full", trust_layer_generated_at: OLD });
  ok("handleBounties: full trust -> settles once and returns trust layer", () => {
    assert.equal(good.r.status, 200); assert.deepEqual(good.calls, ["/verify", "/settle"]);
    assert.equal(good.body.trust_layer, "full"); assert.equal(good.body.items[0].payout_track_record.paid, 7);
    assert.ok(good.r.headers.get("X-PAYMENT-RESPONSE"));
  });
}

// ---- Page-level no-charge guard (shared#1 6539669141 / public #5): filtered page with only trust-missing items
{
  const OLD = "2026-10-28T00:00:00Z";
  const items = [ { ...RICH, id: "direct:demo", source: "direct" }, { ...T.redactItem({ ...RICH, id: "new:1", source: "new" }), trust_layer: "paid", trust_layer_missing: true, trust_missing_reason: "new since trust snapshot" } ];
  const got = { feed: { generated_at: OLD, items }, trust_layer: "lagging", trust_layer_generated_at: OLD };
  const run = async (qs, hdr) => {
    const calls = [];
    const deps = { facilitator: async (path) => { calls.push(path); return path === "/verify" ? { isValid: true } : { success: true, transaction: "0xt" }; }, getPaidFeed: async () => got };
    const url = new URL("https://x/v1/bounties" + qs);
    const r = await T.handleBounties(new Request(url, { headers: { [hdr]: btoa(JSON.stringify({ x402Version: hdr === "X-PAYMENT" ? 1 : 2 })) } }), url, null, deps);
    return { r, calls, body: await r.json() };
  };
  for (const hdr of ["X-PAYMENT", "PAYMENT-SIGNATURE"]) {
    for (const qs of ["?source=new", "?id=new:1"]) {
      const b = await run(qs, hdr);
      ok(`page guard ${hdr} ${qs}: all-missing page -> 503, /verify only, no receipt`, () => {
        assert.equal(b.r.status, 503); assert.deepEqual(b.calls, ["/verify"]); assert.match(b.body.error, /not charged/);
        assert.equal(b.r.headers.get("X-PAYMENT-RESPONSE"), null); assert.equal(b.r.headers.get("PAYMENT-RESPONSE"), null);
        assert.equal(b.r._stage, "pay_blocked_unpaid");
      });
    }
    const mixed = await run("", hdr);
    ok(`page guard ${hdr}: mixed page still settles`, () => { assert.equal(mixed.r.status, 200); assert.deepEqual(mixed.calls, ["/verify", "/settle"]); });
    const trusted = await run("?source=direct", hdr);
    ok(`page guard ${hdr}: trusted page settles`, () => { assert.equal(trusted.r.status, 200); assert.deepEqual(trusted.calls, ["/verify", "/settle"]); });
    ok(`stage tags ${hdr}: blocked -> pay_blocked_unpaid, settled -> pay_settle_ok`, () => { assert.equal(trusted.r._stage, "pay_settle_ok"); });
    const empty = await run("?id=nope:0", hdr);
    ok(`page guard ${hdr}: empty page keeps existing policy (settles, 0 items)`, () => { assert.equal(empty.r.status, 200); assert.equal(empty.body.items.length, 0); });
  }
}

// ---- paid_evidence coverage hint + id filter (31 Oct, Codex "state coverage before purchase")
{
  const FULLX = { generated_at: "2026-10-31T00:00:00Z", items: [
    { id: "frantic:1", reward: 5, currency: "USD", url: "u1", payout_track_record: { paid: 3 }, competition: { active_claims: 1 }, verified: { checked_at: "2026-10-31T00:00:00Z", how: "api" } },
    { id: "frantic:2", reward: 5, currency: "USD", url: "u2", payout_track_record: { paid: 1 } },
    { id: "x:bare", reward: 1, currency: "USD", url: "u3", extra: {} },
  ] };
  ok("paidEvidence: lists field NAMES only, url with id, never values", () => {
    const e = T.paidEvidence({ id: "frantic:1", reward: 5, currency: "USD", url: "u1" }, FULLX);
    assert.deepEqual(e.available, ["payout_track_record", "competition", "verified.how"]);
    assert.ok(e.url.endsWith("/v1/bounties?id=frantic%3A1")); assert.equal(e.trust_checked_at, "2026-10-31T00:00:00Z");
    assert.ok(!JSON.stringify(e).includes("active_claims"));
  });
  ok("paidEvidence: changed / new / empty items get no buy url", () => {
    assert.equal(T.paidEvidence({ id: "frantic:2", reward: 9, currency: "USD", url: "u2" }, FULLX).url, undefined);
    assert.equal(T.paidEvidence({ id: "new:1", reward: 9 }, FULLX).url, undefined);
    assert.deepEqual(T.paidEvidence({ id: "x:bare", reward: 1, currency: "USD", url: "u3" }, FULLX).available, []);
  });
  ok("id filter: exact ids only, >50 ids rejected", () => {
    const ids = T.applyFilter(FULLX, q("id=frantic:2,x:bare")).items.map(i => i.id).sort();
    assert.deepEqual(ids, ["frantic:2", "x:bare"]);
    const many = Array.from({ length: 51 }, (_, i) => "a:" + i).join(",");
    assert.ok(T.parseQuery(new URLSearchParams("id=" + many)).errs.length === 1);
  });
  ok("enrich (paid lagging path) strips the free paid_evidence hint", () => {
    const pub = { generated_at: "2026-10-31T01:00:00Z", items: [{ id: "frantic:1", reward: 5, currency: "USD", url: "u1", paid_evidence: { available: ["x"] } }] };
    assert.equal(T.enrich(pub, FULLX).items[0].paid_evidence, undefined);
  });
}
console.log(`\n${n} unit tests passed`);
