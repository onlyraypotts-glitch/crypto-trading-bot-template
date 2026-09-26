# DATA_FEEDS — what you can get free, and what needs money (snapshot 2026-09-26; re-run the probe)

Tested live from the template author's cloud machine on **2026-09-26, 12:33–12:40 PM ET** (16:33–16:40 UTC) with
`python3 $DESK_DIR/tools/probe_feeds.py` (one polite read-only request per endpoint). Raw results, including
each endpoint's last successful time: `$DESK_DIR/data/feed_probe.json`. Re-run the probe any time; it merges results.
Read-only only: no orders, no keys printed. Helius was tested through `$DESK_TOOLS_DIR/crypto_data.py` (key loaded at runtime).

**Shared-IP caveat:** on a shared or cloud IP, DexScreener, GeckoTerminal, CoinGecko (price/global), and Jupiter lite
flip between 200 and 429 within minutes. DexScreener and Jupiter worked at 12:36 ET and were throttled again at 12:40.
Always go through `$DESK_TOOLS_DIR/_http.py` (shared limiter and cache) and reuse cached scans.

Legend: **OK** = worked live today · **FLAKY** = worked today, also got 429 today · **BLOCKED** = geo/firewall block from
the test machine · **PAID** = needs a paid key or plan · **KEY** = free account/key needed (not set up in the test).

## 1. Free metrics available today
| Need | Endpoint (keyless unless noted) | Status | Notes |
|---|---|---|---|
| Token pairs, price, liquidity, volume, % changes, pair age | DexScreener `api.dexscreener.com/tokens/v1/{chain}/{addrs}`, `/latest/dex/search` | FLAKY | Cloudflare 429s; ~300 req/min is documented but the shared IP hits it |
| Paid promotion flag | DexScreener `/token-boosts/latest/v1` | FLAKY | Boosts = paid, a NEGATIVE signal |
| New/trending pools, OHLCV | GeckoTerminal `api.geckoterminal.com/api/v2/networks/{net}/new_pools, trending_pools, pools/{p}/ohlcv` | FLAKY (429 during test) | ~10 calls/min keyless, shared with CoinGecko's per-IP budget |
| Solana price, liquidity, createdAt; trending; quotes/price impact | Jupiter lite `lite-api.jup.ag/price/v3`, `/tokens/v2/toptrending/1h`, `/swap/v1/quote` | FLAKY (OK 12:36) | quotes = executable cost evidence. a free `JUPITER_API_KEY` (portal.jup.ag, stored as an env secret) works on api.jup.ag |
| Solana security (authorities, holders, insiders, LP lockers, creator) | RugCheck `api.rugcheck.xyz/v1/tokens/{mint}/report[/summary]` | OK | |
| Token security Solana + EVM | GoPlus `api.gopluslabs.io/api/v1/solana/token_security`, `/token_security/{chainId}` | OK | code 4029 = rate limited |
| EVM honeypot / taxes | honeypot.is `api.honeypot.is/v2/IsHoneypot` | OK | |
| Solana RPC (holders, txs, balances) | Helius RPC (free key as env secret `HELIUS_API_KEY`, via crypto_data) | OK | Free plan per helius.dev: 1M credits/mo, 10 rps |
| Solana public RPC | `api.mainnet-beta.solana.com` | OK | heavily rate limited; fallback only |
| Base/Ethereum RPC and explorer | `mainnet.base.org`; Blockscout `base.blockscout.com/api` (+ eth.blockscout.com) | OK | keyless Etherscan-compatible API; wallet histories for Smart Money |
| Majors spot prices | Coinbase `api.coinbase.com/v2/prices/{X}-USD/spot`; Kraken `/0/public/Ticker`; Binance.US `/api/v3/klines` | OK | Binance.US klines = free price history for backtests |
| Global mcap, BTC dominance, 24h % | CoinPaprika `api.coinpaprika.com/v1/global`, `/tickers/btc-bitcoin` | OK | used by regime_snapshot.py |
| CoinGecko simple price / global | `api.coingecko.com/api/v3/simple/price`, `/global` | FLAKY (429 in test) | a free **Demo** key (account needed) gives a stable quota |
| CoinGecko trending searches | `/api/v3/search/trending` | OK | attention signal (one platform) |
| Chain TVL | DefiLlama `api.llama.fi/v2/chains` | OK | |
| Stablecoin supply (by asset/chain, 7d change) | DefiLlama `stablecoins.llama.fi/stablecoins`, `/stablecoinchains` | OK | used by regime_snapshot.py |
| DEX volume, fees/revenue | DefiLlama `api.llama.fi/overview/dexs`, `/overview/fees` | OK | |
| Yields, token prices | DefiLlama `yields.llama.fi/pools`, `coins.llama.fi/prices/current/...` | OK | |
| Funding rate + open interest (context only, never a trade) | OKX `/api/v5/public/funding-rate`, `/public/open-interest`; Deribit `get_book_summary_by_currency`; Kraken Futures `/derivatives/api/v3/tickers`; Hyperliquid `POST api.hyperliquid.xyz/info` | OK (Hyperliquid FLAKY) | OKX used by regime_snapshot.py |
| Fear & Greed | alternative.me `api.alternative.me/fng/` | OK | |
| US 10y yield and other FRED series | FRED CSV `fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10` | OK | no key needed for CSV; the FRED JSON API needs a free key |
| Treasury rates | US Treasury FiscalData API | OK | |
| ETF flows (BTC/ETH) | Farside `farside.co.uk/btc/` (HTML table) | OK (scrape) | unofficial HTML page, parse politely (≤ a few times per day); no API |
| News | CoinDesk RSS (plus the other RSS in `$DESK_TOOLS_DIR/crypto_news.py`); GDELT DOC API | OK / FLAKY | GDELT: max 1 request per 5 s |
| Regulators | SEC press RSS, CFTC press RSS | OK | |
| Social (keyless) | Reddit `search.rss`; Telegram `t.me/s/<channel>` previews | OK | JSON Reddit API needs a key; RSS works |
| Event odds (context) | Polymarket gamma `gamma-api.polymarket.com/markets` | OK | read-only market data |
| Dev activity | GitHub REST `api.github.com/repos/...` | FLAKY | 60 req/h unauthenticated per IP; hit the limit during the test (a free token raises it to 5,000/h) |
| Santiment price metrics | `api.santiment.net/graphql` (price_usd) | OK | social metrics need a paid key (see below) |

## 2. Not available free from the test machine
| Need | Why | Status |
|---|---|---|
| Binance futures funding/OI | HTTP 451 "restricted location" | BLOCKED (use OKX/Deribit/Kraken Futures instead) |
| Bybit tickers | CloudFront 403 | BLOCKED |
| mempool.space (BTC fees) | TLS connection reset from the test machine (curl too) | BLOCKED/UNKNOWN |
| DefiLlama bridges, token unlocks (emissions) | HTTP 402 "Upgrade to the paid API plan" | PAID (unlocks: read official project pages by hand) |
| Etherscan V2 | "Missing/Invalid API Key" | KEY (free key covers Ethereum; Base needs a paid plan per earlier notes; Blockscout covers Base keyless) |
| X/Twitter search, mentions, follower data | 401 | PAID (see below) |
| Labeled wallet PnL (Nansen/Arkham/Birdeye/GMGN) | 401/400 without keys | PAID/KEY |
| Coinglass (aggregated liquidations/OI/funding) | "API key missing" | PAID |
| LunarCrush, CryptoPanic, Santiment social | 401 / key-only endpoints | PAID |
| Bubblemaps clusters, Solscan Pro | paid API (per earlier research, unverified today) | PAID |
Until one of these is bought, the matching metric is **UNKNOWN**. Bots must write null and never estimate it.

## 3. Paid-feed shortlist (prices checked on 2026-09-26; nothing bought)
| Feed | What it adds for the desk | Price | Verification |
|---|---|---|---|
| **CoinGecko API** | stable quota for prices, global, categories, onchain DEX data (removes the 429 problem) | Demo $0 (account + key); Basic $35/mo ($29/mo yearly); Analyst $129/mo ($103/mo yearly) | **verified**: coingecko.com/en/api/pricing |
| **Helius** (already on the free plan) | more credits for wallet scoring, webhooks | Free $0 (1M credits); Developer $49/mo (10M credits, 50 rps); Business $499/mo | **verified**: helius.dev/pricing |
| **Birdeye** | Solana/EVM token and wallet data, trending, OHLCV | Standard free; Lite $39/mo (2.5M CUs); Starter $99/mo; Premium $199/mo; Business $499/mo | **verified**: bds.birdeye.so/pricing |
| **Coinglass** | funding, OI, liquidations across exchanges (context for regime) | Hobbyist $29/mo; Startup $79/mo | **verified**: coinglass.com/pricing (higher tiers not captured) |
| **Nansen** | labeled smart-money wallets, realized PnL leaderboards | Pro $69/mo monthly or $49/mo yearly; API credits $10 per 10,000 (Pro includes 2,000/mo) | official academy/docs pages via search (direct fetch blocked by Cloudflare): **verified from official text, not fetched directly** |
| **Santiment** | social volume/dominance, dev activity, on-chain metrics | Sanbase Pro $49/mo; Sanbase Max $249/mo (full API) | **verified**: app.santiment.net/pricing |
| **X API** | real mention velocity, unique authors | pay-per-use: $0.005 per post read, $0.010 per user read | **verified**: docs.x.com/x-api/getting-started/pricing |
| **LunarCrush** | social metrics aggregated across platforms | Individual $90/mo; Builder $300/mo; Scale $900/mo | **verified** (pricing page title): lunarcrush.com/pricing |
| **CryptoPanic** | aggregated news with sentiment and PanicScore | Growth $199/mo ($179/mo yearly) or $50/week; Enterprise from $899/mo. Free developer API removed 2026-04-01 | official plans page via search; direct fetch returned only a JS shell: **verified from official text, not fetched directly** |
| **DefiLlama API** | unlocks, bridges/inflows, pro endpoints | API $300/mo ($3,000/yr); Pro (no API) $49/mo | docs.llama.fi/pro-api via search (subscription page behind Cloudflare): **verified from official text, not fetched directly** |
| **Arkham** | entity labels, counterparties, flows | usage-based plans "start at $100" (official getting-access guide); free trial on request; x402 pay-per-request available | official arkm.com guide via search: **not fetched directly**. A third-party listing says $900/mo, so treat the entry price as **unverified** |
| **GMGN** | wallet PnL/win rate, dev history, bundler/insider tags | no published API price found; API key requires an account plus a generated signing key pair | **UNVERIFIED** (also a custodial trading platform, so use for data only) |
| Bubblemaps, Solscan Pro | holder clusters, explorer API | ~$170/mo and $49+/mo (from 2026-09-25 research) | **UNVERIFIED** today |

**Suggested order if the owner wants to spend (smallest first):** (1) free CoinGecko Demo key + free GitHub token (both $0,
remove throttling); (2) Coinglass Hobbyist $29/mo *or* Birdeye Lite $39/mo; (3) Nansen Pro $49–69/mo for labeled
realized wallet PnL (Smart Money); (4) X API pay-per-use with a hard monthly budget for Radar. Any sign-up or payment is the
owner's decision. No bot may buy, sign up, or enter card details.

## 4. Tools (template repo `research/` folder) that wrap these feeds
`$DESK_TOOLS_DIR/crypto_data.py` (DexScreener, GeckoTerminal, RugCheck, GoPlus, Jupiter, Helius, CoinGecko, CoinPaprika,
DefiLlama, F&G) · `evm_data.py` (DexScreener, GeckoTerminal, GoPlus, honeypot.is, public RPC) · `evm_smart_money.py`
(Blockscout) · `smart_money.py` (Helius) · `social_data.py` (Reddit RSS, Telegram, news, trending, paid flags) ·
`crypto_news.py` (RSS, GDELT, status pages) · `telegram_calls.py` + `shadow_tracker.py` (call channels graded against real
prices) · `$DESK_DIR/tools/regime_snapshot.py` (regime) · `$DESK_DIR/tools/probe_feeds.py` (this inventory).
