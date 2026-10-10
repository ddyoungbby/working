# AI Tech Tilt — signal daemon console

A monitoring + signal-generation dashboard for a rules-based AI/tech equity
strategy. It ranks a fixed universe of AI/tech tickers, surfaces live-ticking
quotes, runs a deterministic risk gate, and (optionally) classifies recent news
catalysts with Claude.

**Monitoring only — it never places orders.**

## Run it

```bash
npm install
npm run dev
```

Open http://localhost:5173/. It starts in **demo mode** with deterministic,
seeded data — no API keys required.

## Data sources

- **Demo (default):** deterministic generated bars and quotes. Fully offline.
- **Alpaca live:** open the **Settings** tab, enter an Alpaca API key ID +
  secret, and switch to "Alpaca live". Uses the free IEX feed (no SIP
  subscription needed); paper-account keys work since this only reads market
  data. Keys live in session memory only — nothing is stored.

## News / AI catalyst classification

The **News / AI** tab classifies recent catalysts for the top-ranked tickers
using Claude with live web search. The call runs **server-side** (via a Vite
dev-middleware at `/api/classify`) so the Anthropic API key never reaches the
browser.

To enable it, set `ANTHROPIC_API_KEY` before starting the dev server:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
npm run dev
```

Without the key, the rest of the app works normally; the News / AI tab returns a
clear "key not set" message instead of classifying.

- Model: `claude-opus-5-5`
- Server handler: `server/classify.mjs`

## Project layout

- `src/App.jsx` — the dashboard (single React component)
- `server/classify.mjs` — server-side Anthropic catalyst classifier
- `vite.config.js` — Vite config + `/api/classify` dev middleware
