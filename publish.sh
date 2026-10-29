#!/usr/bin/env bash
# Refresh the public feed and push it (run hourly from the box rescan).
set -euo pipefail
cd "$(dirname "$0")"
export GH_CONFIG_DIR=/home/box/daily-dollar/accounts/gh-bot
git pull -q --rebase origin main || true
python3 collect.py docs
# Paid-only trust layer: bundle the full feed (never committed; .cache/ and *.gen.js are gitignored) into the paid
# worker and redeploy, so only GET /v1/bounties serves it. On failure the worker keeps its previous bundled copy.
if [ -s .cache/feed-full.json ]; then
  python3 -c "import json;d=json.load(open('.cache/feed-full.json'));open('paid-worker/full-feed.gen.js','w').write('export default '+json.dumps(d,ensure_ascii=False)+';\n')"
  ( cd paid-worker && PATH=/home/box/node-v22.11.0-linux-x64/bin:$PATH npx -y wrangler@4.40.0 deploy </dev/null >/tmp/abf-deploy.log 2>&1 ) \
    || echo "WARN paid worker redeploy failed (see /tmp/abf-deploy.log); paid trust layer may lag"
fi
# Weekly 'State of Agent Bounties' report: regenerate on Sundays (UTC) or if missing; cheap, so also fine to run by hand.
if [ "$(date -u +%u)" = 7 ] || [ ! -f docs/report/index.html ]; then python3 report.py || echo "WARN report failed"; fi
# Cheap liveness check of the paid x402 endpoint (unpaid GET -> expect 402; never pays).
if [ -s paid-worker/LIVE_URL ]; then
  PURL=$(cat paid-worker/LIVE_URL)
  pc=$(curl -s -m 15 -o /dev/null -w '%{http_code}' "$PURL/v1/bounties" || echo 000)
  hc=$(curl -s -m 15 -o /dev/null -w '%{http_code}' "$PURL/health" || echo 000)
  echo "$(date '+%F %T %Z') paid=$pc health=$hc" >> /home/box/daily-dollar/ledger/paid-endpoint-checks.log
  [ "$pc" = 402 ] || echo "WARN paid endpoint returned $pc (expected 402)"
fi
git add docs data
if git diff --cached --quiet; then echo "no change"; exit 0; fi
git -c user.name=dailydollar-bot -c user.email=dailydollar@mail.grokbot.com commit -qm "feed: refresh $(date -u +%Y-%m-%dT%H:%MZ)"
git push -q origin main
echo "pushed"
