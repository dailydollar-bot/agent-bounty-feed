#!/usr/bin/env python3
"""Agent Bounty Feed collector.

Collects currently open, claimable, *paid* bounties from public agent bounty
boards (no auth, read-only GETs) and writes:
  docs/feed.json  (JSON, schema in README)
  docs/feed.xml   (RSS 2.0)
  docs/llms.txt   (markdown for LLMs/humans)
  docs/index.html (simple HTML table)
Stdlib only. Never reads or writes credentials.
"""
import json, re, sys, os, html, datetime, urllib.request, urllib.error
from email.utils import format_datetime

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")
SITE = os.environ.get("FEED_SITE", "https://dailydollar-bot.github.io/agent-bounty-feed")
UA = "agent-bounty-feed/1.0 (+https://github.com/dailydollar-bot/agent-bounty-feed)"
NOW = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
NOW_S = NOW.isoformat().replace("+00:00", "Z")
TIP = "0x6523acd890Ec5853db6D9eB731D7892666dbd27e"
CONTACT = "dailydollar@mail.grokbot.com"
# Paid x402 endpoint: only advertised once paid-worker/LIVE_URL exists (written after a live 402 check).
_live = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paid-worker", "LIVE_URL")
PAID = open(_live).read().strip() if os.path.exists(_live) else ""


def get(url, timeout=25, tries=3):
    import time
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    for n in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError):
            if n == tries - 1:
                raise
            time.sleep(3 * (n + 1))


def clean(s, n=200):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


USD_LIKE = ("USD", "USDC", "USDT", "USDG", "PYUSD")


def item(source, id_, title, reward, currency, url, deadline=None, eligibility="", network=None, extra=None,
         requires=None, slots_left=None, competition=None, track=None, verified=""):
    """requires: list from REQUIRES_VOCAB. slots_left: int or None (unlimited / not exposed).
    competition: dict (submissions/attempts so far). track: payout_track_record dict or None."""
    est = round(float(reward), 2) if reward is not None and (currency or "").upper() in USD_LIKE else None
    return {
        "source": source,
        "kind": "bounty",
        "id": f"{source}:{id_}",
        "title": clean(title, 180),
        "reward": round(float(reward), 6) if reward is not None else None,
        "currency": currency,
        "network": network,
        "deadline": deadline,
        "url": url,
        "eligibility": clean(eligibility, 400),
        "est_usd": est,
        "requires": sorted(set(requires or [])),
        "slots_left": slots_left,
        "competition": competition or None,
        "payout_track_record": track,
        "verified": {"checked_at": NOW_S, "how": verified} if verified else None,
        "fetched_at": NOW_S,
        **({"extra": extra} if extra else {}),
    }


# Shared reward pools (e.g. "$1 USDC split across 2 answers"): keep the native pool for discovery, but only a
# source-stated per-worker amount counts as per_worker_usd (Codex rule, issue #1, 28 Oct). No assumed equal splits.
POOL_RE = re.compile(r"\b(split (?:across|between|among)|shared (?:across|between|among)|divided (?:across|between|among)|"
                     r"(?:prize|reward) pool|pool(?:ed)? (?:of|reward)|first \d+ (?:answers|agents|submissions|winners)|"
                     r"up to \d+ (?:winners|answers|agents))\b", re.I)
PER_WORKER_RE = re.compile(r"\$\s?(\d+(?:\.\d+)?)\s*(?:USDC|USD)?\s*(?:each|per (?:answer|winner|submission|agent|worker))\b", re.I)


def pool_basis(it, text):
    """Mark shared-pool rewards on an item in place. per_worker_usd stays None unless the source states it."""
    t = text or ""
    if not (POOL_RE.search(t) or PER_WORKER_RE.search(t)):
        return it
    it["reward_basis"] = "shared_pool"
    it["pool_amount"] = it.get("reward")
    m = PER_WORKER_RE.search(t)
    # Keep raw source precision for eligibility ($0.999 must stay < 1); round for display only (Codex fixture, issue #1).
    it["per_worker_usd"] = float(m.group(1)) if (m and it.get("est_usd") is not None) else None
    return it


REQUIRES_VOCAB = {
    "agent_account": "Register/enlist an agent account on the source board.",
    "wallet_evm": "An EVM wallet (Base or the listed chain) to receive USDC.",
    "wallet_solana": "A Solana wallet to receive the reward.",
    "bond": "A refundable claim bond / deposit is locked when claiming.",
    "stake": "A stake is required and can be slashed.",
    "kyc": "Payout needs identity verification (e.g. Stripe Connect / KYC).",
    "github": "A GitHub account (work is delivered as a pull request).",
    "x_account": "An X/Twitter account (the task asks for a post or reply).",
    "public_post": "Publishing a public post/article (Medium, dev.to, Bluesky, X...).",
    "human_only": "The board marks this as human-only; agents should not apply.",
    "purchase": "The task asks you to spend or buy something first.",
}
DROPS = {}


def drop(reason):
    DROPS[reason] = DROPS.get(reason, 0) + 1


def text_requires(t):
    t = (t or "").lower()
    r = []
    if re.search(r"\b(x\.com|twitter|tweet|repost|quote[- ]post)\b", t):
        r.append("x_account")
    if re.search(r"\b(medium|dev\.to|bluesky|publish (?:your|an|a)|blog post|article)\b", t):
        r.append("public_post")
    if re.search(r"\b(buy|purchase|hold) (?:\$?\d|some|it|the token|tokens?)\b", t) or funds_other_work(t):
        r.append("purchase")
    return r


# Rebates/"on the house" offers that pay you back only after you fund someone else's work are an outlay, not free money
# (e.g. Frantic #97: fund a >= $10 bounty, an independent worker must be paid from your funds, then claim $10 back).
FUND_OTHER_RE = re.compile(
    r"on the house|\brebate|\bfund (?:it|a|an|another|your|the) (?:\$?\d+ )?(?:bounty|posting|task|job)|\bfund it\b|"
    r"paid from your funds|from your funds|your funding receipt|funding receipt'?s payer|"
    r"\bpost (?:a|an|another|your own) (?:funded )?(?:bounty|task|job)\b.*\b(?:fund|pay)|"
    r"\bpay (?:a|an|another) (?:independent |different |real )?worker\b")


def funds_other_work(t):
    return bool(FUND_OTHER_RE.search((t or "").lower()))


def past(deadline):
    if not deadline:
        return False
    try:
        d = datetime.datetime.fromisoformat(str(deadline).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=datetime.timezone.utc)
        return d <= NOW
    except ValueError:
        return False


# ---------------------------------------------------------------- sources
def frantic():
    out = []
    feed = get("https://gofrantic.com/feed.json")
    for it in feed.get("items", []):
        f = it.get("_frantic", {})
        slots = f.get("claim_slots", {})
        if slots.get("available", 1) <= 0:
            drop("frantic: no claim slots left")
            continue
        extra, elig = {"claim_slots": slots}, "Enlist a Frantic agent (gofrantic.com). Paid in USDC after auto + sponsor review."
        req_extra = ["purchase"] if funds_other_work(it.get("title", "") + " " + (it.get("content_text") or "")) else []
        slots_left, track, comp, how = slots.get("available"), None, None, "feed.json claim_slots"
        try:
            b = get(f["api_url"])["bounty"]
            if b.get("work_status") != "open" or not b.get("funded", True) or b.get("cancellation_status"):
                drop("frantic: not open/funded on detail API")
                continue
            cp = b.get("claim_progress") or {}
            slots_left = cp.get("available", slots_left)
            if slots_left is not None and slots_left <= 0:
                drop("frantic: no claim slots left")
                continue
            track = {k: cp.get(k, 0) for k in ("paid", "accepted", "rejected", "expired")}
            track["basis"] = "this bounty's claims so far (Frantic API)"
            q = b.get("quality") or {}
            if q.get("count"):
                track["review_avg_score"] = q.get("average_score")
            comp = {"active_claims": cp.get("active"), "delivered_awaiting_review": cp.get("delivered")}
            extra["claim_window_minutes"] = b.get("claim_window_minutes")
            extra["required_artifacts"] = b.get("required_artifacts")
            if b.get("claim_window_minutes"):
                elig += f" Claim window {b.get('claim_window_minutes')} min."
            if b.get("required_artifacts"):
                elig += f" Artifacts: {', '.join(b['required_artifacts'])}."
            elig += f" So far {cp.get('paid',0)} paid / {cp.get('rejected',0)} rejected."
            if funds_other_work(" ".join(str(b.get(k) or "") for k in ("title", "description", "criteria"))):
                req_extra.append("purchase")
                elig = ("OUTLAY: you must first fund a separate bounty from your own money and an independent worker must be paid from it "
                        "before any rebate; not free money. ") + elig
            how = "Frantic detail API: work_status=open, funded, claim slots available"
            degraded = False
        except Exception:
            # Detail check failed: funded/open status NOT re-confirmed this refresh. Keep listed (feed.json says open)
            # but mark verification degraded so consumers (and /earn-now) don't treat it as freshly verified.
            degraded = True
            how = "DEGRADED: feed.json claim_slots only; Frantic detail API check failed this refresh (funded/open not re-confirmed)"
        it_ = item("frantic", f.get("number"), re.sub(r"^#\d+\s*|\s*·\s*\$[\d.]+$", "", it.get("title", "")),
                   f.get("price_usd"), "USD", it.get("url"), None, elig, "base (USDC)", extra,
                   ["agent_account", "wallet_evm"] + req_extra, slots_left, comp, track, how)
        if degraded and it_.get("verified"):
            it_["verified"]["degraded"] = True
        out.append(it_)
    return out


TM_CHAINS = {"8453": "base", "84532": "base-sepolia", "4217": "tempo"}
# Settlement token per chain (taskmarket.dev/reference/network.md): Base=USDC, Tempo=OUSD (no USD parity assumed).
TM_TOKEN = {"base": "USDC", "tempo": "OUSD"}
_tm_track = None
_TM_PAID = [0, 0.0]


def taskmarket_requesters():
    """Count tasks each requester has completed with an award (last ~300 completed tasks)."""
    global _tm_track
    if _tm_track is not None:
        return _tm_track
    _tm_track, cursor = {}, None
    for _ in range(3):
        try:
            d = get("https://api.taskmarket.dev/api/tasks?status=completed&limit=100" + (f"&cursor={cursor}" if cursor else ""))
        except Exception:
            break
        for t in d.get("tasks", []):
            r = (t.get("requester") or "").lower()
            if t.get("awardCount"):
                _tm_track[r] = _tm_track.get(r, 0) + 1
                if TM_CHAINS.get(str(t.get("chainId"))) == "base":  # USDC only; Tempo pays OUSD
                    _TM_PAID[0] += 1
                    _TM_PAID[1] += int(t.get("reward") or 0) / 1e6
        cursor = d.get("nextCursor")
        if not d.get("hasMore") or not cursor:
            break
    return _tm_track


def taskmarket():
    out, cursor, pages = [], None, 0
    track = taskmarket_requesters()
    while pages < 5:
        u = "https://api.taskmarket.dev/api/tasks?status=open&limit=100" + (f"&cursor={cursor}" if cursor else "")
        d = get(u)
        for t in d.get("tasks", []):
            if t.get("status") != "open" or not t.get("submissionWindowOpen", True) or t.get("phase") not in (None, "active"):
                drop("taskmarket: not open / submission window closed")
                continue
            if t.get("taskVisibility", "public") != "public" or t.get("hasAccessPassword"):
                drop("taskmarket: private or password-protected")
                continue
            if past(t.get("expiryTime")):
                drop("expired (deadline passed)")
                continue
            if t.get("claimedBy") and t.get("mode") != "bounty":
                drop("taskmarket: already claimed")
                continue
            desc = t.get("description") or ""
            m = re.match(r"\s*Title:\s*(.+)", desc)
            title = m.group(1) if m else desc.strip().split("\n")[0]
            reward = int(t.get("reward") or 0) / 1e6
            if reward <= 0 or not t.get("escrowTxHash"):
                drop("unfunded / no escrow")
                continue
            chain = TM_CHAINS.get(str(t.get("chainId")), f"eip155:{t.get('chainId')}")
            if chain == "base-sepolia":
                drop("testnet")
                continue
            elig = f"mode={t.get('mode')} (requester picks winner(s)); {t.get('submissionCount',0)} submissions so far; platform fee {int(t.get('platformFeeBps') or 0)/100:g}%; reward escrowed on-chain"
            req = ["agent_account", "wallet_evm"] + text_requires(desc)
            if t.get("stakeRequired"):
                elig += f"; STAKE REQUIRED ({t.get('stakeBps')} bps)"
                req.append("stake")
            elig += ". Needs a Taskmarket wallet (CLI: taskmarket.dev/skill.md)."
            r = (t.get("requester") or "").lower()
            tr = {"requester_awarded_tasks": track.get(r, 0), "basis": "requester's completed+awarded tasks among the last ~300 completed on Taskmarket"} if track else None
            if "purchase" in req:
                drop("asks the solver to buy/hold a token")
                continue
            out.append(pool_basis(item("taskmarket", t.get("referenceCode") or t["id"], title, reward, TM_TOKEN.get(chain, "UNKNOWN"),
                            f"https://taskmarket.dev/tasks/{t.get('referenceCode') or t['id']}",
                            t.get("expiryTime"), elig, chain,
                            {"mode": t.get("mode"), "chain_id": t.get("chainId")},
                            req, None, {"submissions": t.get("submissionCount")}, tr,
                            "Taskmarket API: status=open, submissionWindowOpen, escrowTxHash set, not expired"), desc))
        cursor = d.get("nextCursor")
        pages += 1
        if not d.get("hasMore") or not cursor:
            break
    return out


def basedagents():
    out = []
    d = get("https://api.basedagents.ai/v1/tasks?status=open&limit=100")
    for t in d.get("tasks", []):
        b = t.get("bounty") or {}
        amt = float(b.get("amount_display") or 0)
        if t.get("status") != "open" or not t.get("claimable") or t.get("claimed_by_agent_id"):
            drop("basedagents: not claimable")
            continue
        if amt <= 0:
            drop("no cash reward")
            continue
        if past(t.get("expires_at")):
            drop("expired (deadline passed)")
            continue
        # Only list tasks whose bounty payment is actually verified/escrowed; 'pending' = promise only.
        if not (t.get("payment_verified") or t.get("escrow")):
            drop("unfunded / no escrow")
            continue
        net = {"eip155:8453": "base"}.get(b.get("network"), b.get("network"))
        elig = f"category={t.get('category')}; needs capabilities {', '.join(t.get('required_capabilities') or []) or 'none listed'}. Register a BasedAgents agent to claim."
        out.append(item("basedagents", t["task_id"], t.get("title"), amt, b.get("token") or "USDC",
                        f"https://basedagents.ai/tasks/{t['task_id']}", t.get("expires_at"), elig, net, None,
                        ["agent_account", "wallet_evm"] + text_requires(t.get("description")),
                        1, None, None,
                        "BasedAgents API: open, claimable, unclaimed, payment verified/escrowed"))
    return out


def agentbounties():
    out = []
    d = get("https://api.agentbounties.app/v1/base/autonomous-bounties/feed?network=base-mainnet&claimable_only=true")
    for b in d if isinstance(d, list) else d.get("items", []):
        if b.get("status") != "claimable":
            drop("agentbounties: not claimable")
            continue
        doc = (b.get("terms") or {}).get("document") or {}
        ct = doc.get("contract_terms") or {}
        reward = int(b.get("solver_reward") or 0) / 1e6
        if reward <= 0 or int(b.get("funded_amount") or 0) < int(b.get("target_amount") or 0):
            drop("unfunded / no escrow")
            continue
        bond = int(b.get("claim_bond") or 0) / 1e6
        elig = f"On-chain bounty (Base), fully funded in contract. Claim bond {bond:g} USDC; claim window {int(ct.get('claim_window_seconds') or 0)//3600} h; verification {b.get('verification_mode')}."
        if b.get("verification_ready") is False:
            elig += " Verification not ready yet."
        dl = None
        if ct.get("funding_deadline"):
            dl = datetime.datetime.fromtimestamp(int(ct["funding_deadline"]), datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        req = ["wallet_evm"] + (["bond"] if bond else [])
        if int(b.get("required_external_spend") or 0):
            req.append("purchase")
        out.append(item("agentbounties", b["bounty_id"][:18], doc.get("title") or b["bounty_id"], reward, "USDC",
                        f"https://agentbounties.app/bounties/{b['bounty_id']}", dl, elig, "base",
                        {"claim_bond_usdc": bond}, req, 1, None, None,
                        "Agent Bounties API: status=claimable, funded_amount >= target_amount"))
    return out


MONEY = re.compile(r"\$\s?(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s?(USDC|USDT|USD)\b", re.I)


def colony():
    out = []
    d = get("https://thecolony.ai/api/v1/posts?post_type=paid_task&limit=50")
    for p in d.get("items", []):
        t = p.get("title") or ""
        if t.lower().startswith("for hire") or "for-hire" in (p.get("tags") or []):
            continue  # service offers, not bounties
        if p.get("status") != "open" or p.get("accepting_submissions") is False or p.get("closed_at"):
            drop("colony: closed / not accepting submissions")
            continue
        m = MONEY.search(t)
        if not m:
            continue
        amt = float(m.group(1) or m.group(2))
        if amt <= 0:
            continue
        req = ["agent_account"] + text_requires(t + " " + (p.get("body") or "")[:3000])
        if "purchase" in req:
            drop("asks the solver to buy/hold a token")
            continue
        elig = f"Colony paid_task post by @{(p.get('author') or {}).get('username')}; reward parsed from the title (not escrowed), read the post for exact terms; {p.get('comment_count',0)} replies so far."
        out.append(item("colony", p["id"][:8], t, amt, "USD", f"https://thecolony.ai/post/{p['id']}", None, elig, None,
                        {"colony": p.get("colony_name"), "escrowed": False}, req, None,
                        {"replies": p.get("comment_count", 0)}, None, "Colony API: status=open, accepting_submissions"))
    return out


def superteam():
    out = []
    d = get("https://superteam.fun/api/listings?take=100")
    for x in d if isinstance(d, list) else d.get("listings", []):
        if x.get("status") != "OPEN" or x.get("isWinnersAnnounced") or past(x.get("deadline")):
            drop("superteam: closed / winners announced / past deadline")
            continue
        if x.get("agentAccess") == "HUMAN_ONLY":
            drop("human-only listing")
            continue
        amt = x.get("rewardAmount") or 0
        if not amt:
            continue
        elig = f"Superteam Earn {x.get('type')}; agentAccess={x.get('agentAccess')}; sponsor {(x.get('sponsor') or {}).get('name')}. Usually contest-style (winners picked by sponsor)."
        out.append(item("superteam", x.get("slug"), x.get("title"), amt, x.get("token") or "USDC",
                        f"https://superteam.fun/earn/listing/{x.get('slug')}", x.get("deadline"), elig, "solana", None,
                        ["agent_account", "wallet_solana"], None, None, None, "Superteam API: OPEN, agent access, before deadline"))
        ranked_basis(out[-1], x)
    return out


def ranked_basis(it, x):
    """Superteam contest bounties: rewardAmount is the TOTAL prize pool split across ranked winners, not one payout.
    Keep the native pool for discovery; per_worker_usd stays None (no assumed tier/equal split; Codex, issue #1, 29 Oct)."""
    if (x.get("type") or "").lower() != "bounty":
        return it
    it["reward_basis"] = "ranked_prizes"
    it["pool_amount"] = it.get("reward")
    it["per_worker_usd"] = None
    return it


def licium():
    out = []
    d = get("https://www.licium.ai/v1/bounties?status=open&limit=200")
    for b in d.get("data", []):
        amt = float(b.get("solver_payout_usd") or b.get("amount_usd") or 0)
        if amt <= 0 or not (b.get("funding") or {}).get("funded"):
            drop("unfunded / no escrow")
            continue
        if past(b.get("closes_at")):
            drop("expired (deadline passed)")
            continue
        out.append(item("licium", b["id"][:8], b.get("title"), amt, "USD", f"https://www.licium.ai/bounties/{b['id']}",
                        b.get("closes_at"), f"Licium {b.get('selection_mode')}; category {b.get('category')}.", None, None,
                        ["agent_account"], None, None, None, "Licium API: open and funded"))
    return out


# ---- Algora bounties on GitHub (via the bot's authenticated gh CLI; read-only search + comments)
ALGORA_BOT = "algora-pbc[bot]"
ALGORA_AMT = re.compile(r"##\s*💎\s*\$([\d,]+(?:\.\d+)?)\s*bounty(?:\s*\[•\s*([^\]]+)\]\((https://algora\.io/[^)]+)\))?")
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")


def gh(*args):
    import subprocess
    env = dict(os.environ)
    env.setdefault("GH_CONFIG_DIR", "/home/box/daily-dollar/accounts/gh-bot")
    r = subprocess.run(["gh", "api", *args], capture_output=True, text=True, timeout=60, env=env)
    if r.returncode != 0:
        raise RuntimeError(clean(r.stderr, 160))
    return json.loads(r.stdout)


def algora():
    import time
    os.makedirs(CACHE, exist_ok=True)
    cpath = os.path.join(CACHE, "algora-repo-track.json")
    try:
        rtrack = json.load(open(cpath))
    except Exception:
        rtrack = {}
    since = (NOW - datetime.timedelta(days=365)).date().isoformat()
    q = f'label:"💎 Bounty" state:open is:issue commenter:app/algora-pbc -label:"💰 Rewarded" updated:>{since}'
    issues, page = [], 1
    while page <= 3:
        d = gh("-X", "GET", "search/issues", "-f", f"q={q}", "-f", "per_page=100", "-f", f"page={page}", "-f", "sort=updated")
        issues += d.get("items", [])
        if len(d.get("items", [])) < 100:
            break
        page += 1
    out = []
    for iss in issues:
        labels = [l["name"] for l in iss.get("labels", [])]
        low = " ".join(labels).lower()
        if iss.get("state") != "open" or "rewarded" in low or "on-hold" in low or "on hold" in low or iss.get("locked"):
            drop("algora: rewarded / on hold / locked")
            continue
        repo = iss["repository_url"].split("/repos/")[1]
        try:
            cs = gh(f"repos/{repo}/issues/{iss['number']}/comments?per_page=100")
        except Exception:
            drop("algora: comments unreadable")
            continue
        bot = [c for c in cs if c.get("user", {}).get("login") == ALGORA_BOT]
        amt, sponsor, sponsor_url = None, None, None
        posted = None
        for c in bot:
            m = ALGORA_AMT.search(c.get("body") or "")
            if m:
                amt = float(m.group(1).replace(",", "")); sponsor = (m.group(2) or "").strip() or None; sponsor_url = m.group(3)
                posted = c.get("created_at")
        if not amt:
            drop("algora: no bounty amount from Algora bot")
            continue
        joined = " ".join(c.get("body") or "" for c in bot).lower()
        if "has been rewarded" in joined or "bounty was cancelled" in joined or "bounty has been cancelled" in joined:
            drop("algora: rewarded / cancelled per Algora bot")
            continue
        attempts = sum(1 for c in cs if re.search(r"(?m)^\s*/attempt\b", c.get("body") or ""))
        claims = sum(1 for c in bot if "claims the bounty" in (c.get("body") or ""))
        if repo not in rtrack or rtrack[repo].get("at", "") < (NOW - datetime.timedelta(days=1)).isoformat():
            try:
                time.sleep(2.2)  # search API: 30 req/min
                n = gh("-X", "GET", "search/issues", "-f", f'q=repo:{repo} label:"💰 Rewarded"', "-f", "per_page=1")["total_count"]
                ri = gh(f"repos/{repo}")
                rtrack[repo] = {"rewarded": n, "archived": bool(ri.get("archived")), "pushed_at": ri.get("pushed_at"), "at": NOW.isoformat()}
            except Exception:
                pass
        tr = {"repo_rewarded_bounties": rtrack[repo]["rewarded"], "basis": "issues in this repo labelled 💰 Rewarded by Algora"} if repo in rtrack else None
        if rtrack.get(repo, {}).get("archived"):
            drop("algora: repository archived")
            continue
        pushed = rtrack.get(repo, {}).get("pushed_at") or ""
        if pushed and pushed < (NOW - datetime.timedelta(days=180)).isoformat():
            drop("algora: repo inactive >180 days (bounty likely abandoned)")
            continue
        if not tr or not tr["repo_rewarded_bounties"]:
            # Quality gate: repos that have never paid an Algora bounty (some post hundreds of unpaid "bounties") are not listed.
            drop("algora: repo has never paid an Algora bounty")
            continue
        req = ["github", "kyc"] + (["human_only"] if "humans only" in low or "human only" in low else [])
        elig = (f"Algora bounty on GitHub ({repo}); sponsor {sponsor or 'see issue'}. Comment /attempt #{iss['number']}, then open a PR with /claim #{iss['number']}. "
                f"Paid in USD via Algora (Stripe payout, needs KYC). Bounty posted {(posted or '?')[:10]}; {attempts} /attempt comments, {claims} claiming PRs so far.")
        if "ai only" in low or "ai agent friendly" in low:
            elig += " Labelled AI-agent friendly."
        out.append(item("algora", f"{repo}#{iss['number']}", iss.get("title"), amt, "USD", iss["html_url"], None, elig, None,
                        {"repo": repo, "labels": labels, "sponsor_url": sponsor_url, "bounty_posted_at": posted, "issue_updated_at": iss.get("updated_at"), "repo_pushed_at": rtrack.get(repo, {}).get("pushed_at")},
                        req, None, {"attempts": attempts, "claiming_prs": claims, "comments": iss.get("comments")}, tr,
                        "GitHub: issue open, 💎 Bounty label, amount from the algora-pbc[bot] comment, not rewarded/on-hold"))
    json.dump(rtrack, open(cpath, "w"))
    return out


# ---------------------------------------------------------------- board-level payout evidence (filled by collectors)
PAYOUT = {}   # source -> {"paid_count": int|None, "paid_usd": float|None, "basis": str, "url": str}


def hondo():
    """LN Church Hondō agent task board: small USDC (Base) tasks with public per-task paid counters."""
    out = []
    base = "https://kari.mayim-mayim.com"
    d = get(base + "/api/agent/tasks?limit=50")
    paid_n, paid_usd = 0, 0.0
    for c in d.get("tasks", []):
        tt = c.get("task_type") or ""
        if "paid_service_trial" in tt or "trial" in tt:
            drop("hondo: paid-service trial (solver spends money)")
            continue
        try:
            t = get(c["detail_url"])
            t = t.get("task", t)
        except Exception:
            drop("hondo: task detail unreadable")
            continue
        if t.get("status") != "OPEN":
            drop("hondo: not open")
            continue
        rw = t.get("reward") or {}
        atomic = rw.get("amount_atomic") or rw.get("normal_amount_atomic") or rw.get("correct_amount_atomic")
        try:
            amt = int(atomic) / 1e6
        except (TypeError, ValueError):
            drop("no cash reward")
            continue
        cap = t.get("capacity_remaining", t.get("capacity_available"))
        if cap is not None and int(cap) <= 0:
            drop("hondo: no capacity left")
            continue
        paid = t.get("rewarded_execution_count", t.get("rewards_paid_confirmed"))
        paid_minor = t.get("reward_paid_total_minor")
        if paid:
            paid_n += int(paid)
            paid_usd += (int(paid_minor) / 1e6) if paid_minor else int(paid) * amt
        if not paid:
            drop("never paid (no confirmed payouts on this task type yet)")
            continue
        title = t.get("title") or c.get("title") or tt
        if title and c.get("title") and title != c.get("title"):
            title = f"{c.get('title')} ({title})"
        caveat = ("Caveat: respondent-profile task; the guide may limit who may answer (group/membership), not verified by this feed. ") if re.search(r"respondent", f"{title} {c.get('title') or ''}", re.I) else ""
        elig = caveat + (f"Hondō task type {tt}; claim via API (no key), lease-based, one open claim per reward address. Reward {amt:g} USDC on Base"
                f"{' (up to; reduced for low-quality answers)' if 'normal_amount_atomic' in rw or 'correct_amount_atomic' in rw else ''}. Read the immutable task guide before claiming: {'see extra.definition_url' if caveat else c.get('definition_url')}")
        out.append(item("hondo", c["task_id"][-12:], title, amt, "USDC", c["detail_url"], t.get("expires_at") or c.get("claim_before"), elig, "base",
                        {"task_type": tt, "definition_url": c.get("definition_url"), "board": base + "/agent-taskboard.html"},
                        ["wallet_evm"], int(cap) if cap is not None else None,
                        {"claims_total": t.get("claim_count_total", t.get("successful_claims_lifetime"))},
                        {"paid": int(paid), "basis": "confirmed payouts on this task (Hondō API)"},
                        "Hondō API: status=OPEN, capacity remaining, confirmed payouts > 0"))
    PAYOUT["hondo"] = {"paid_count": paid_n, "paid_usd": round(paid_usd, 2), "basis": "confirmed payouts on currently listed Hondō tasks", "url": base + "/agent-taskboard.html"}
    return out


AUDIT_ELIG = ("Competitive security audit contest: find and report vulnerabilities; the prize pool is split among valid findings, so most entrants earn little or nothing. "
              "Check the platform's rules on AI-assisted findings (low-quality or AI-spam reports are penalised) and its KYC terms before entering.")


def cantina():
    """Cantina public audit competitions (fixed prize pools). Open-ended bug bounties are excluded (max payouts, submission fees)."""
    out = []
    hdr = get("https://cantina.xyz/api/v0/opportunities")
    PAYOUT["cantina"] = {"paid_count": None, "paid_usd": float(hdr.get("totalPaidOut") or 0) or None,
                         "basis": "platform-wide total paid out to researchers (Cantina API)", "url": "https://cantina.xyz/opportunities"}
    d = get("https://cantina.xyz/api/v0/opportunities?kind=public_contest")
    for c in d.get("items", []):
        if c.get("kind") != "public_contest" or c.get("status") != "live":
            drop("cantina: not a live public contest")
            continue
        tf = c.get("timeframe") or {}
        if past(tf.get("end")):
            drop("expired (deadline passed)")
            continue
        try:
            pot = float(c.get("totalRewardPot") or 0)
        except ValueError:
            pot = 0
        if pot <= 0:
            drop("no cash reward")
            continue
        fee = c.get("submissionFee")
        req = ["agent_account", "github"] + (["kyc"] if c.get("kycRequired") else []) + (["purchase"] if fee and float(fee) > 0 else [])
        if "purchase" in req:
            drop("cantina: submission fee required")
            continue
        out.append(item("cantina", c["id"][:8], f"Audit contest: {c.get('name')}", pot, c.get("currencyCode") or "USDC", c.get("url"), tf.get("end"),
                        AUDIT_ELIG + f" Prize pool {pot:g} {c.get('currencyCode')}; {c.get('totalFindings') or 0} findings submitted so far.", None,
                        {"company": (c.get("company") or {}).get("name"), "starts": tf.get("start")}, req, None,
                        {"findings": c.get("totalFindings")}, None, "Cantina API: kind=public_contest, status=live, end in future"))
        out[-1]["kind"] = "audit_contest"
    return out


def sherlock():
    """Sherlock public audit contests that are running or announced."""
    out = []
    import time as _t
    os.makedirs(CACHE, exist_ok=True)
    cpath = os.path.join(CACHE, "sherlock-paid.json")
    try:
        paid = json.load(open(cpath))
    except Exception:
        paid = {}
    if paid.get("at", "") < (NOW - datetime.timedelta(days=1)).isoformat():
        d = get("https://audits.sherlock.xyz/api/contests?status=FINISHED&page=1")
        tot, n = 0.0, 0
        for c in d.get("items", []):
            if c.get("token") in USD_LIKE + ("DAI",):
                tot += float(c.get("rewards") or 0); n += 1
        paid = {"at": NOW.isoformat(), "usd": tot, "n": n}
        json.dump(paid, open(cpath, "w"))
    PAYOUT["sherlock"] = {"paid_count": paid.get("n"), "paid_usd": round(paid.get("usd", 0), 2) or None,
                          "basis": "total rewards of the 10 largest finished public contests (Sherlock API; lower bound)", "url": "https://audits.sherlock.xyz/contests"}
    for st in ("RUNNING", "CREATED"):
        d = get(f"https://audits.sherlock.xyz/api/contests?status={st}")
        for c in d.get("items", []):
            if c.get("private"):
                drop("sherlock: private contest")
                continue
            end = datetime.datetime.fromtimestamp(int(c["ends_at"]), datetime.timezone.utc).isoformat().replace("+00:00", "Z") if c.get("ends_at") else None
            if past(end):
                drop("expired (deadline passed)")
                continue
            pot = float(c.get("prize_pool") or c.get("rewards") or 0)
            if pot <= 0:
                drop("no cash reward")
                continue
            out.append(item("sherlock", c["id"], f"Audit contest: {c.get('title')}", pot, c.get("token") or "USDC", f"https://audits.sherlock.xyz/contests/{c['id']}", end,
                            AUDIT_ELIG + f" Status {st.lower()}; type {c.get('type_label')}.", None, {"status": st}, ["agent_account", "github", "kyc"], None, None, None,
                            f"Sherlock API: status={st}, public, end in future"))
            out[-1]["kind"] = "audit_contest"
    return out


def code4rena():
    """Code4rena audits that are upcoming or active (public code)."""
    out = []
    d = get("https://code4rena.com/api/v1/audits?page=1")
    audits = (d.get("data") or {}).get("audits", [])
    done = [a for a in audits if a.get("status") == "Completed"]
    PAYOUT["code4rena"] = {"paid_count": len(done), "paid_usd": None,
                           "basis": "completed audits on the latest API page (awards published per audit)", "url": "https://code4rena.com/audits"}
    for a in audits:
        if a.get("status") not in ("Active", "Upcoming", "Created", "Live"):
            continue
        if a.get("codeAccess") not in (None, "public"):
            drop("code4rena: private code")
            continue
        if past(a.get("endTime")):
            drop("expired (deadline passed)")
            continue
        m = re.search(r"\$([\d,]+)", a.get("formattedAmount") or "")
        if not m:
            drop("no cash reward")
            continue
        pot = float(m.group(1).replace(",", ""))
        out.append(item("code4rena", a.get("slug"), f"Audit contest: {a.get('title')}", pot, "USDC", f"https://code4rena.com/audits/{a.get('slug')}", a.get("endTime"),
                        AUDIT_ELIG + f" {a.get('formattedAmount')}; starts {(a.get('startTime') or '')[:10]}.", None, {"status": a.get("status")},
                        ["agent_account", "github", "kyc"], None, None, None, f"Code4rena API: status={a.get('status')}, public code, end in future"))
        out[-1]["kind"] = "audit_contest"
    return out


DIRECT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "direct-listings.json")


def direct():
    """Bounties submitted to us via the 'Submit a bounty' issue form, listed only after manual funding verification."""
    out = []
    try:
        rows = json.load(open(DIRECT)).get("listings", [])
    except FileNotFoundError:
        return out
    for r in rows:
        if not r.get("verified") or not r.get("verification_evidence"):
            drop("direct: not yet verified (funding unproven)")
            continue
        if r.get("status", "open") != "open":
            drop("direct: closed by maintainer")
            continue
        if past(r.get("deadline")):
            drop("expired (deadline passed)")
            continue
        it = item("direct", r["id"], r["title"], r["reward"], r.get("currency", "USDC"), r["url"], r.get("deadline"),
                  r.get("eligibility", "") + f" Directly submitted by the bounty owner; funding verified by the maintainer on {r.get('verified_at','?')[:10]} (evidence in the paid trust layer).",
                  r.get("network"), {"submission_issue": r.get("submission_issue"), "verified_at": r.get("verified_at")},
                  r.get("requires", []), r.get("slots_left"), None,
                  ({"paid": r["poster_paid_count"], "basis": r.get("poster_paid_basis", "poster's prior paid bounties, checked by maintainer")} if r.get("poster_paid_count") else None),
                  "Direct submission: funding verified by maintainer (escrow tx or payout record)")
        it["verified_direct"] = True
        it["verification_evidence"] = r["verification_evidence"]
        out.append(it)
    return out


SOURCES = {
    "frantic": ("https://gofrantic.com", frantic),
    "taskmarket": ("https://taskmarket.dev", taskmarket),
    "basedagents": ("https://basedagents.ai", basedagents),
    "agentbounties": ("https://agentbounties.app", agentbounties),
    "superteam": ("https://superteam.fun/earn", superteam),
    "licium": ("https://www.licium.ai", licium),
    "algora": ("https://algora.io", algora),
    "hondo": ("https://kari.mayim-mayim.com/agent-taskboard.html", hondo),
    "cantina": ("https://cantina.xyz/competitions", cantina),
    "sherlock": ("https://audits.sherlock.xyz/contests", sherlock),
    "code4rena": ("https://code4rena.com/audits", code4rena),
    "direct": ("https://github.com/dailydollar-bot/agent-bounty-feed/issues/new?template=submit-bounty.yml", direct),
}
NOT_INCLUDED = {
    "colony": "The Colony paid_task posts are not escrowed and the board has no public payout record; removed 2026-10-24 (reputation over count).",
    "bountybook": "Jobs are not escrowed on-chain (contract_job_id 0) and the treasury has shown failed payouts; parked until payouts are healthy.",
    "deskcrew": "Bounty board currently empty/disabled; no public payout record yet.",
    "opire": "Only 3 open rewards in the public API (one on a throwaway test repo) and no public payout history; re-check monthly.",
    "issuehunt": "Effectively dormant: ~3 open funded issues on GitHub, the newest years old.",
    "open-ended-bug-bounties": "Immunefi / Cantina / HackerOne-style bug bounties show maximum payouts for critical bugs, not claimable rewards, and often need KYC or submission fees; only fixed-pool audit contests are listed.",
    "gitcoin-replit-bountysource-polar": "Discontinued bounty products (Gitcoin Bounties 2023, Replit Bounties moved to Contra, Bountysource shut down unpaid, Polar dropped issue funding).",
    "near-agent-market": "market.near.ai job board needs an API token to read; not included in the free feed.",
    "hashn": "hashn store is prose, not structured paid bounties; not included.",
    "github-other-bounty-labels": "Generic 'bounty' / '$' labels on GitHub are mostly unfunded or abandoned; only Algora-backed bounties (amount confirmed by the algora-pbc bot) are listed.",
}


def board_evidence(items):
    """Board-level payout evidence for sources whose collectors don't fill PAYOUT themselves. Best effort; never fatal."""
    fr = [i for i in items if i["source"] == "frantic" and i.get("payout_track_record")]
    if fr:
        PAYOUT["frantic"] = {"paid_count": sum(int(i["payout_track_record"].get("paid") or 0) for i in fr), "paid_usd": round(sum(int(i["payout_track_record"].get("paid") or 0) * (i.get("est_usd") or 0) for i in fr), 2),
                             "basis": "paid claims on currently listed Frantic bounties (Frantic API)", "url": "https://gofrantic.com"}
    if _TM_PAID[0]:
        PAYOUT["taskmarket"] = {"paid_count": _TM_PAID[0], "paid_usd": round(_TM_PAID[1], 2), "basis": "awarded tasks among the last ~300 completed (Taskmarket API)", "url": "https://taskmarket.dev/tasks"}
    try:
        d = get("https://api.basedagents.ai/v1/tasks?status=verified&limit=100")
        st = [t for t in (d.get("tasks", []) if isinstance(d, dict) else d) if t.get("payment_status") == "settled" and t.get("payment_tx_hash")]
        PAYOUT["basedagents"] = {"paid_count": len(st), "paid_usd": round(sum(int(t.get("bounty_amount") or 0) for t in st) / 1e6, 2),
                                 "basis": "verified tasks with a settled payment tx (last 100, BasedAgents API)", "url": "https://basedagents.ai"}
    except Exception:
        pass
    try:
        d = get("https://api.agentbounties.app/v1/base/autonomous-bounties/feed?network=base-mainnet")
        pd = [b for b in (d if isinstance(d, list) else d.get("items", [])) if b.get("status") == "paid"]
        PAYOUT["agentbounties"] = {"paid_count": len(pd), "paid_usd": round(sum(int(b.get("solver_reward") or 0) for b in pd) / 1e6, 2),
                                   "basis": "bounties with status=paid (on-chain settlement, Agent Bounties API)", "url": "https://agentbounties.app/metrics.html"}
    except Exception:
        pass
    try:
        d = get("https://www.licium.ai/v1/bounties?status=accepted&limit=200")
        rows = d.get("data", [])
        PAYOUT["licium"] = {"paid_count": sum(1 for b in rows if (b.get("paid_out_usd") or 0) > 0), "paid_usd": round(sum(float(b.get("paid_out_usd") or 0) for b in rows), 2),
                            "basis": "accepted bounties with paid_out_usd (Licium API)", "url": "https://www.licium.ai"}
    except Exception:
        pass
    try:
        d = get("https://superteam.fun/api/homepage/stats")
        PAYOUT["superteam"] = {"paid_count": d.get("count"), "paid_usd": d.get("totalInUSD"), "basis": "platform-wide earnings paid to contributors (Superteam stats API)", "url": "https://superteam.fun/earn"}
    except Exception:
        pass
    try:
        rt = json.load(open(os.path.join(CACHE, "algora-repo-track.json")))
        PAYOUT["algora"] = {"paid_count": sum(int(v.get("rewarded") or 0) for v in rt.values()), "paid_usd": None,
                            "basis": f"issues labelled 💰 Rewarded across {len(rt)} tracked repos (GitHub)", "url": "https://algora.io"}
    except Exception:
        pass
    dl = [i for i in items if i["source"] == "direct"]
    PAYOUT.setdefault("direct", {"paid_count": None, "paid_usd": None, "basis": "each listing's funding is verified by hand (escrow tx or payout record)", "url": "https://github.com/dailydollar-bot/agent-bounty-feed/blob/main/SUBMIT.md"})


def to_usd(it):
    """USD used for totals/sorting: audit-contest prize pools are shared by all entrants, so they are counted separately."""
    return 0 if it.get("kind") == "audit_contest" else (it.get("est_usd") or 0)


def make_stats(items, status):
    by = {}
    for i in items:
        s = by.setdefault(i["source"], {"items": 0, "usd": 0.0})
        s["items"] += 1
        s["usd"] = round(s["usd"] + (i.get("est_usd") or 0), 2)
    for name, st in status.items():
        by.setdefault(name, {"items": 0, "usd": 0.0})["ok"] = st["ok"]
    day = (NOW - datetime.timedelta(days=1)).isoformat().replace("+00:00", "Z")
    req = {}
    for i in items:
        for r in i["requires"]:
            req[r] = req.get(r, 0) + 1
    return {
        "items": len(items),
        "total_usd": round(sum(to_usd(i) for i in items), 2),
        "by_source": by,
        "new_last_24h": sum(1 for i in items if (i.get("first_seen") or "") >= day),
        "no_bond_no_stake_no_kyc": sum(1 for i in items if not set(i["requires"]) & {"bond", "stake", "kyc", "purchase", "human_only"}),
        "requires_counts": dict(sorted(req.items())),
        "dropped_this_refresh": dict(sorted(DROPS.items(), key=lambda kv: -kv[1])),
        "degraded_verification": sum(1 for i in items if (i.get("verified") or {}).get("degraded")),
        "audit_contests": sum(1 for i in items if i.get("kind") == "audit_contest"),
        "audit_contest_prize_pools_usd": round(sum(i.get("est_usd") or 0 for i in items if i.get("kind") == "audit_contest"), 2),
        "direct_verified": sum(1 for i in items if i["source"] == "direct"),
        "payout_evidence": PAYOUT,
        "last_refresh": NOW_S,
        "refresh_interval": "hourly",
    }


# ---------------------------------------------------------------- free vs paid split
# The per-item trust layer is paid-only (served by the x402 worker from KV, never published to the public site).
TRUST_FIELDS = ("payout_track_record", "competition", "verification_evidence", "extra")
FULL_FEED = os.path.join(CACHE, "feed-full.json")  # .cache/ is gitignored; publish.sh uploads it to the worker's KV
SUBMIT_URL = "https://github.com/dailydollar-bot/agent-bounty-feed/issues/new?template=submit-bounty.yml"
TRUST_DOC = {
    "free_fields": ["id", "source", "kind", "title", "reward", "currency", "est_usd", "network", "deadline", "url", "eligibility", "requires",
                    "slots_left", "reward_basis", "pool_amount", "per_worker_usd", "verified.checked_at", "verified.degraded", "stale", "verified_direct", "first_seen", "fetched_at"],
    "paid_only_fields": ["payout_track_record", "competition", "verified.how", "verification_evidence", "extra"],
    "paid_endpoint": None,
    "note": "Free outputs (feed.json, feed.xml, llms.txt, MCP, /earn-now) carry discovery fields. The per-bounty trust layer is served only by the paid x402 endpoint. Board-level payout evidence (stats.payout_evidence, /sources/) stays free.",
}


def redact(i):
    o = {k: v for k, v in i.items() if k not in TRUST_FIELDS and k != "verified"}
    v = i.get("verified")
    o["verified"] = ({"checked_at": v.get("checked_at"), **({"degraded": True} if v.get("degraded") else {})} if isinstance(v, dict) else v)
    o["trust_layer"] = "paid"
    return o


# ---------------------------------------------------------------- writers
def write_all(items, status):
    os.makedirs(OUT, exist_ok=True)
    total = round(sum(to_usd(i) for i in items), 2)
    stats = make_stats(items, status)
    feed = {
        "name": "Agent Bounty Feed",
        "description": "Open, claimable, paid bounties for AI agents, collected hourly from public agent bounty boards.",
        "home_page_url": SITE + "/",
        "feed_url": SITE + "/feed.json",
        "schema_version": "1.2",
        "generated_at": NOW_S,
        "count": len(items),
        "total_open_usd_equiv": total,
        "stats": stats,
        "requires_vocabulary": REQUIRES_VOCAB,
        "sources": status,
        "not_included": NOT_INCLUDED,
        "sponsored": [],
        "tip_address": {"network": "base", "token": "USDC", "address": TIP},
        "contact": CONTACT,
        "paid_endpoint": ({"url": PAID + "/v1/bounties", "protocol": "x402", "price": "0.01", "token": "USDC", "network": "base",
                           "pay_to": TIP, "facilitator": "https://facilitator.payai.network",
                           "filters": ["source", "min_usd", "network", "no_bond", "exclude_requires", "since", "after_id", "sort", "limit"], "info": PAID + "/"} if PAID else None),
        "mcp_endpoint": (PAID + "/mcp" if PAID else None),
        "submit_bounty": SUBMIT_URL,
        "trust_layer": dict(TRUST_DOC, paid_endpoint=(PAID + "/v1/bounties" if PAID else None)),
        "items": items,
    }
    # Full feed (with trust layer) -> private cache for the paid worker; public feed gets redacted items.
    os.makedirs(CACHE, exist_ok=True)
    with open(FULL_FEED, "w") as f:
        json.dump(dict(feed, trust_layer=dict(feed["trust_layer"], this_copy="full")), f, ensure_ascii=False)
    full_items = items
    items = [redact(i) for i in full_items]
    feed["items"] = items
    with open(os.path.join(OUT, "feed.json"), "w") as f:
        json.dump(feed, f, indent=1, ensure_ascii=False)

    # RSS
    def fs(i):
        try:
            return format_datetime(datetime.datetime.fromisoformat(i["first_seen"].replace("Z", "+00:00")))
        except Exception:
            return format_datetime(NOW)
    rss = ['<?xml version="1.0" encoding="UTF-8"?>', '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"><channel>',
           "<title>Agent Bounty Feed</title>", f"<link>{SITE}/</link>",
           f'<atom:link href="{SITE}/feed.xml" rel="self" type="application/rss+xml"/>',
           "<description>Open, claimable, paid bounties for AI agents (hourly).</description>",
           f"<lastBuildDate>{format_datetime(NOW)}</lastBuildDate>"]
    for i in items:
        r = f"{i['reward']:g} {i['currency']}" if i["reward"] is not None else "?"
        desc = f"[{i['source']}] {r}. Deadline: {i['deadline'] or 'none listed'}. Requires: {', '.join(i['requires']) or 'nothing special'}. {i['eligibility']}"
        rss.append("<item>" + f"<title>{html.escape(f'{r} · {i['title']}')}</title><link>{html.escape(i['url'])}</link>"
                   + f'<guid isPermaLink="false">{html.escape(i["id"])}</guid><category>{i["source"]}</category><pubDate>{fs(i)}</pubDate>'
                   + f"<description>{html.escape(desc)}</description></item>")
    rss.append("</channel></rss>")
    with open(os.path.join(OUT, "feed.xml"), "w") as f:
        f.write("\n".join(rss))

    # llms.txt
    def short(i):
        bits = [f"requires: {', '.join(i['requires']) or 'nothing special'}"]
        if i.get("slots_left") is not None:
            bits.append(f"slots left {i['slots_left']}")
        bits.append(f"first seen {i.get('first_seen')}")
        return "; ".join(bits)

    L = ["# Agent Bounty Feed", "",
         "> Open, claimable, paid bounties for AI agents, collected hourly from public agent bounty boards and Algora (GitHub). "
         "Every item is re-checked each refresh (open, funded/escrowed where the board exposes it, slots left, not expired). "
         "Free, read-only, no auth. Machine-readable: " + SITE + "/feed.json (JSON) and " + SITE + "/feed.xml (RSS)."
         + (f" MCP server (Streamable HTTP, free): {PAID}/mcp" if PAID else ""), "",
         f"Updated: {NOW_S} · {len(items)} open bounties · ~${total:g} total (USD-equivalent) · {stats['new_last_24h']} new in the last 24 h · "
         f"{stats['no_bond_no_stake_no_kyc']} need no bond, stake or KYC.", "",
         "Each item: source, id, title, reward, currency, est_usd, network, deadline, url, eligibility, requires[], slots_left, "
         "verified (checked_at, degraded), first_seen, fetched_at. The per-bounty trust layer (payout_track_record, competition, verified.how, verification_evidence, extra source evidence) is paid-only via the x402 endpoint below; board-level payout evidence stays free at " + SITE + "/sources/ . requires[] values: "
         + "; ".join(f"{k} = {v}" for k, v in REQUIRES_VOCAB.items()) + ". "
         "Always confirm terms on the source board before working; this feed only links out.", "",
         "Audit contests (kind=audit_contest: Cantina, Sherlock, Code4rena) are listed separately from the total: their prize pools are shared by all entrants. "
         "Only boards whose listings are funded and that show public payout evidence are included; per-source evidence: " + SITE + "/sources/ . "
         "Weekly report: " + SITE + "/report/ . "
         "Maintained by an autonomous agent (dailydollar-bot).", "",
         "## Submit a bounty (free, unique supply)", "",
         f"Bounty owners and agent platforms can list a funded bounty here for free: {SUBMIT_URL} (rules: https://github.com/dailydollar-bot/agent-bounty-feed/blob/main/SUBMIT.md). "
         "It is published as source=direct only after we verify the funding by hand (escrow transaction or a prior payout record). Filter direct listings with source=direct (MCP list_bounties or /v1/bounties)."
         + (f" Short link: {PAID}/submit" if PAID else ""), "",
         "## Sources", ""]
    for s, st in status.items():
        L.append(f"- {s}: {st['count']} items, ${stats['by_source'].get(s, {}).get('usd', 0):g} ({'ok' if st['ok'] else 'unreachable: ' + st.get('error','')}) — {st['home']}")
    for s, why in NOT_INCLUDED.items():
        L.append(f"- {s}: not included — {why}")
    if DROPS:
        L += ["", "Dropped this refresh (not listed): " + "; ".join(f"{k}: {v}" for k, v in stats["dropped_this_refresh"].items()) + "."]
    L += ["", "## Open bounties (largest reward first)", ""]
    for i in items:
        r = f"{i['reward']:g} {i['currency']}"
        L.append(f"- [{r}] [{i['title']}]({i['url']}) — {i['source']}; deadline {i['deadline'] or 'none listed'}; {short(i)}. {i['eligibility']}")
    L += ["", "## Sponsored", "", f"(empty) Clearly-labelled sponsored slot for agent bounty boards and agent tooling. Contact {CONTACT}.", "",
          "## Support", "", f"Tips (USDC on Base): {TIP}", ""]
    if PAID:
        L += ["## MCP server (free)", "",
              f"Remote MCP endpoint (Streamable HTTP, no auth): {PAID}/mcp — tools: list_bounties, get_new_bounties, get_bounty, get_feed_stats. "
              "Serves this same hourly feed. Claude Code: `claude mcp add --transport http agent-bounty-feed " + PAID + "/mcp`.", "",
              "## Paid filtered endpoint (x402)", "",
              f"GET {PAID}/v1/bounties — the same bounties plus the paid-only per-bounty trust layer (payout_track_record, competition, verified.how, verification_evidence, extra), filtered server-side: source (csv), min_usd, network, no_bond=1, exclude_requires (csv, e.g. kyc,stake,bond), "
              "since (ISO-8601; returns only items first seen after it, oldest first by first_seen+id), after_id (page cursor), sort (first_seen|reward), limit. Responses include has_more, next_since and next_after_id: while has_more, page with since=next_since&after_id=next_after_id (no gaps). min_usd excludes items with unknown USD value. "
              f"$0.01 USDC on Base per call via x402 (HTTP 402; v1 X-PAYMENT or v2 PAYMENT-SIGNATURE; facilitator PayAI). "
              f"Unpaid requests get a 402 with payment requirements; x402 clients pay and retry automatically. Bad parameters get a free 400. Free info: {PAID}/ · health: {PAID}/health. "
              "The free feed above stays free (discovery fields only).", ""]
    with open(os.path.join(OUT, "llms.txt"), "w") as f:
        f.write("\n".join(L))

    # index.html
    def cell_req(i):
        return " ".join(f'<span class="tag{" warn" if r in ("bond","stake","kyc","purchase","human_only") else ""}" title="{html.escape(REQUIRES_VOCAB.get(r, r))}">{r}</span>' for r in i["requires"]) or "—"

    def cell_track(i):
        sl = "" if i.get("slots_left") is None else f"<b>{i['slots_left']}</b> slots left<br>"
        return sl + '<small>payout record &amp; competition: <a href="#paid">paid API</a></small>'

    rows = "\n".join(
        f"<tr><td>{html.escape(f'{i['reward']:g} {i['currency']}')}</td><td><a href=\"{html.escape(i['url'])}\">{html.escape(i['title'])}</a>"
        f"<br><small>{html.escape(i['eligibility'])}</small></td>"
        f"<td>{i['source']}</td><td>{cell_req(i)}</td><td>{cell_track(i)}</td><td>{html.escape(str(i['deadline'] or '—'))[:10]}</td><td>{html.escape((i.get('first_seen') or '')[:10])}</td></tr>" for i in items)
    srcs = " · ".join(f"{s}: {st['count']} (${stats['by_source'].get(s, {}).get('usd', 0):g}){'' if st['ok'] else ' (down)'}" for s, st in status.items())
    drops = "; ".join(f"{k}: {v}" for k, v in stats["dropped_this_refresh"].items())
    ld = json.dumps({
        "@context": "https://schema.org", "@type": "Dataset", "name": "Agent Bounty Feed",
        "description": "Open, claimable, paid bounties for AI agents, verified and collected hourly from public agent bounty boards and Algora.",
        "url": SITE + "/", "isAccessibleForFree": True, "dateModified": NOW_S,
        "creator": {"@type": "Organization", "name": "dailydollar-bot", "url": "https://github.com/dailydollar-bot"},
        "distribution": [
            {"@type": "DataDownload", "encodingFormat": "application/json", "contentUrl": SITE + "/feed.json"},
            {"@type": "DataDownload", "encodingFormat": "application/rss+xml", "contentUrl": SITE + "/feed.xml"},
            {"@type": "DataDownload", "encodingFormat": "text/plain", "contentUrl": SITE + "/llms.txt"}],
    }).replace("</", "<\\/")
    paid_html = (f'''<h2>Use it from an agent</h2>
<p><b>MCP server (free):</b> <code>{html.escape(PAID)}/mcp</code> (Streamable HTTP, no auth). Tools: <code>list_bounties</code>, <code>get_new_bounties</code>, <code>get_bounty</code>, <code>get_feed_stats</code>.<br>
Claude Code: <code>claude mcp add --transport http agent-bounty-feed {html.escape(PAID)}/mcp</code><br>
Cursor / other clients: <code>{{"mcpServers":{{"agent-bounty-feed":{{"url":"{html.escape(PAID)}/mcp"}}}}}}</code></p>
<p id="paid"><b>Paid, filtered endpoint with the trust layer (x402):</b> items include each bounty's <code>payout_track_record</code>, <code>competition</code> (attempts/submissions so far), <code>verified.how</code>, <code>verification_evidence</code> and source evidence links — the free feed, MCP and /earn-now carry discovery fields only.<br> <code>GET {html.escape(PAID)}/v1/bounties?since=2026-10-24T00:00:00Z&amp;exclude_requires=kyc,stake,bond</code> — $0.01 USDC on Base per call, paid with <a href="https://x402.org">x402</a>. Filters: <code>source</code>, <code>min_usd</code>, <code>network</code>, <code>no_bond=1</code>, <code>exclude_requires</code>, <code>since</code> (new items only, oldest first; page gap-free with <code>has_more</code> / <code>next_since</code> / <code>after_id</code>), <code>sort</code>, <code>limit</code>. <a href="{html.escape(PAID)}/">Info</a> · <a href="{html.escape(PAID)}/health">health</a>. The free feed stays free.</p>
''' if PAID else "")
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent Bounty Feed — open paid bounties for AI agents ({len(items)} open, ${total:g})</title>
<meta name="description" content="Hourly, verified feed of {len(items)} open, claimable, paid bounties for AI agents (${total:g} total) from Frantic, Taskmarket, Algora, Agent Bounties, BasedAgents, Superteam Earn, Licium, Hondō and verified direct submissions, plus live security audit contests (Cantina, Sherlock, Code4rena). Only funded boards with public payout evidence. JSON, RSS, llms.txt and a free MCP server.">
<link rel="alternate" type="application/rss+xml" title="Agent Bounty Feed" href="feed.xml">
<link rel="alternate" type="application/json" title="Agent Bounty Feed (JSON)" href="feed.json">
<link rel="alternate" type="text/plain" title="Agent Bounty Feed (llms.txt)" href="llms.txt">
<link rel="canonical" href="{SITE}/">
<script type="application/ld+json">{ld}</script>
<style>body{{font-family:system-ui,sans-serif;max-width:1200px;margin:2em auto;padding:0 1em;color:#222}}table{{border-collapse:collapse;width:100%;font-size:14px}}td,th{{border-bottom:1px solid #ddd;padding:6px;vertical-align:top;text-align:left}}th{{background:#f4f4f4}}.sp{{border:2px dashed #bbb;padding:1em;margin:1em 0;color:#555}}code{{background:#f4f4f4;padding:1px 4px}}small{{color:#666}}.tag{{display:inline-block;background:#eef;border-radius:3px;padding:0 4px;margin:1px;font-size:12px}}.warn{{background:#fde8d8}}.stats{{display:flex;gap:1.5em;flex-wrap:wrap;background:#f7f7f7;padding:.8em 1em;border-radius:6px}}.stats div b{{display:block;font-size:22px}}</style></head><body>
<h1>Agent Bounty Feed</h1>
<p>Open, claimable, <b>paid</b> bounties for AI agents, refreshed and re-verified hourly from public boards. Free and read-only: <a href="feed.json"><code>feed.json</code></a> · <a href="feed.xml"><code>feed.xml</code></a> (RSS) · <a href="llms.txt"><code>llms.txt</code></a>{f' · <a href="{html.escape(PAID)}/mcp"><code>MCP</code></a>' if PAID else ''} · <a href="https://github.com/dailydollar-bot/agent-bounty-feed">source &amp; schema</a></p>
<p><b><a href="{SUBMIT_URL}">Submit a funded bounty (free)</a></b> — listed as <code>source=direct</code> after we verify funding (<a href="https://github.com/dailydollar-bot/agent-bounty-feed/blob/main/SUBMIT.md">rules</a>) · <a href="sources/">Sources &amp; payout evidence</a> · <a href="report/">State of Agent Bounties (weekly)</a> · <a href="sources/#badge">Verified badge</a> · <small>Maintained by an autonomous agent (dailydollar-bot); only funded boards with public payout evidence are listed.</small></p>
<div class="stats"><div><b>{len(items)}</b>open bounties</div><div><b>${total:g}</b>total (USD eq.)</div><div><b>{stats['new_last_24h']}</b>new in 24 h</div><div><b>{stats['no_bond_no_stake_no_kyc']}</b>no bond / stake / KYC</div><div><b>{len([s for s in status.values() if s['ok']])}/{len(status)}</b>sources up</div><div><b>{stats.get('audit_contests', 0)}</b>audit contests (${stats.get('audit_contest_prize_pools_usd', 0):g} pools, not in total)</div><div><b>{NOW_S[11:16]} UTC</b>{NOW_S[:10]} refresh</div></div>
<p><small>By source — {srcs}.{(' Dropped this refresh (not listed): ' + html.escape(drops) + '.') if drops else ''}</small></p>
<div class="sp"><b>Sponsored</b> — this slot is empty. Agent boards and agent tooling can sponsor a clearly-labelled listing here: {CONTACT}</div>
<table><tr><th>Reward</th><th>Bounty</th><th>Board</th><th>Requires</th><th>Slots · trust layer</th><th>Deadline</th><th>First seen</th></tr>
{rows}
</table>
{paid_html}<p>Always confirm terms on the source board; this page only links out. Per-bounty payout track records (paid API) are what each board exposes (e.g. Frantic per-bounty paid/rejected counts, Taskmarket requester award history, Algora repo rewards), not a guarantee. Tips (USDC on Base): <code>{TIP}</code></p>
</body></html>"""
    with open(os.path.join(OUT, "index.html"), "w") as f:
        f.write(page)

    # per-source pages, badges, history
    from extras import write_extras
    extra_urls = write_extras(OUT, SITE, NOW_S, items, status, stats, NOT_INCLUDED)
    # discovery: sitemap.xml + robots.txt (allow all)
    urls = ["/", "/llms.txt", "/feed.json", "/feed.xml", "/report/"] + extra_urls
    sm = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for u in urls:
        sm.append(f"<url><loc>{SITE}{u}</loc><lastmod>{NOW_S}</lastmod><changefreq>hourly</changefreq></url>")
    sm.append("</urlset>")
    with open(os.path.join(OUT, "sitemap.xml"), "w") as f:
        f.write("\n".join(sm) + "\n")
    with open(os.path.join(OUT, "robots.txt"), "w") as f:
        f.write(f"User-agent: *\nAllow: /\n\nSitemap: {SITE}/sitemap.xml\n")


def main():
    items, status = [], {}
    for name, (home, fn) in SOURCES.items():
        try:
            got = fn()
            status[name] = {"ok": True, "count": len(got), "home": home}
            items += got
        except Exception as e:
            status[name] = {"ok": False, "count": 0, "home": home, "error": clean(f"{type(e).__name__}: {e}", 120)}
            print(f"[warn] {name}: {e}", file=sys.stderr)
    # A board that errors this hour keeps its last verified items for up to 3 h (marked stale) instead of vanishing.
    prev_items = []
    for pth in (FULL_FEED, os.path.join(OUT, "feed.json")):
        try:
            prev_items = json.load(open(pth)).get("items", [])
            break
        except Exception:
            pass
    lim = (NOW - datetime.timedelta(hours=3)).isoformat().replace("+00:00", "Z")
    for name, st in status.items():
        if st["ok"]:
            continue
        carried = [dict({k: v for k, v in i.items() if k != "trust_layer"}, stale=True) for i in prev_items if i.get("source") == name and (i.get("fetched_at") or "") >= lim]
        if carried:
            st["carried_over_from_last_refresh"] = len(carried)
            items += carried
    kept = []
    for i in items:
        if past(i.get("deadline")):
            drop("expired (deadline passed)")
        else:
            kept.append(i)
    items = kept
    # first_seen survives a source being briefly down: persisted in .cache/first_seen.json (30-day memory).
    os.makedirs(CACHE, exist_ok=True)
    fpath = os.path.join(CACHE, "first_seen.json")
    seen = {}
    try:
        seen = json.load(open(fpath))
    except Exception:
        try:
            seen = {i["id"]: i.get("first_seen") for i in json.load(open(os.path.join(OUT, "feed.json"))).get("items", []) if i.get("first_seen")}
        except Exception:
            pass
    for i in items:
        i["first_seen"] = seen.get(i["id"]) or NOW_S
        seen[i["id"]] = i["first_seen"]
    cutoff = (NOW - datetime.timedelta(days=30)).isoformat().replace("+00:00", "Z")
    json.dump({k: v for k, v in seen.items() if v >= cutoff or any(i["id"] == k for i in items)}, open(fpath, "w"))
    board_evidence(items)
    items.sort(key=lambda i: (i.get("kind") == "audit_contest", -to_usd(i), -(i.get("est_usd") or 0), i["source"]))
    if not any(s["ok"] for s in status.values()):
        print("all sources failed; not overwriting feed", file=sys.stderr)
        sys.exit(1)
    write_all(items, status)
    print(json.dumps({k: v["count"] if v["ok"] else "ERR" for k, v in status.items()}), len(items))


if __name__ == "__main__":
    main()
