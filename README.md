# Whale Screener

A stock screener dashboard powered by the [Unusual Whales API](https://unusualwhales.com/api).
Zero dependencies — plain Node.js server plus a static frontend.

## What it shows

| Panel | UW endpoint | Signal |
|---|---|---|
| Sentiment tiles | `/api/market/market-tide` | Market-wide net call/put premium, net options volume, bullish/bearish read |
| Market tide chart | `/api/market/market-tide` | Intraday net call vs put premium (5-minute ticks) with crosshair tooltip |
| Top net premium impact | `/api/market/top-net-impact` | Tickers moving today's options tape, diverging bars |
| Stock screener | `/api/screener/stocks` | Filterable table ranked by net premium, put/call ratio, IV rank, volume, market cap |
| Options flow alerts | `/api/option-trades/flow-alerts` | Unusual trades flagged by UW alert rules ($50K+ premium) |
| Dark pool prints | `/api/darkpool/recent` | Large off-exchange block trades ($1M+ notional) |
| Congress trades | `/api/congress/recent-trades` | Recent congressional trading disclosures |

## Setup

Requires Node.js 18+.

```bash
cp .env.example .env      # then paste your UW API key into .env
node server.js            # or: npm start
```

Open http://localhost:3000.

## How it works

- `server.js` proxies a **whitelisted** set of Unusual Whales endpoints so the
  API key stays server-side and never reaches the browser.
- Responses are cached in memory (30–300s per route) so the 60-second
  auto-refresh stays well inside UW rate limits.
- `public/` is the dashboard: vanilla JS, hand-rolled SVG chart, light/dark
  theme via `prefers-color-scheme`.

## Security notes

- The API key lives only in `.env`, which is gitignored. Never commit it.
- The proxy only forwards whitelisted routes and query params — the browser
  can't use your key to call arbitrary UW endpoints.

## Unusual Whales MCP server (for Claude Code)

The repo ships a project-scoped `.mcp.json` that connects Claude Code to the
official Unusual Whales MCP server (115 tools: options flow, screeners, dark
pool, short data, insiders, institutions, congress, crypto whales, custom
alerts). It reads the key from the `UW_API_KEY` environment variable, so no
secret lives in the repo:

```bash
export UW_API_KEY=your-key-here   # must be in the shell env; .env is not read by Claude Code
claude                            # approve the project MCP server when prompted
```

Alternatively, register it user-wide (works from any directory):

```bash
# HTTP transport (recommended — no local process)
claude mcp add --scope user --transport http unusual-whales \
  https://api.unusualwhales.com/api/mcp \
  --header "Authorization: Bearer your-key-here"

# or the npx stdio package
claude mcp add --scope user unusual-whales -e UW_API_KEY=your-key-here -- npx -y @unusualwhales/mcp
```

The MCP server is for interactive research in Claude Code; the dashboard uses
the REST proxy in `server.js`. Both use the same key.

## Extending

Add a new panel in three steps:

1. Register the UW endpoint in `ROUTES` in `server.js` (path, allowed params, cache TTL).
2. Add a card to `public/index.html`.
3. Add a `load*()` function in `public/app.js` and include it in `refreshAll()`.

Useful next endpoints: `/api/stock/{ticker}/greek-exposure` (per-ticker GEX),
`/api/stock/{ticker}/max-pain`, `/api/insider/transactions`, `/api/etfs/{ticker}/tide`.

*Data shown is informational only — not investment advice.*
