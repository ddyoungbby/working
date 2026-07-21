#!/usr/bin/env node
/**
 * Unusual Whales screener dashboard — local server.
 *
 * Zero-dependency Node (>=18) server that:
 *   1. Serves the static dashboard from ./public
 *   2. Proxies a whitelisted set of Unusual Whales API endpoints so the
 *      API key never reaches the browser
 *   3. Caches responses in memory (TTL per route) to stay well inside
 *      UW rate limits while the dashboard auto-refreshes
 *
 * Config: UW_API_KEY (required) and PORT (default 3000), via env or ./.env
 */

const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');

// ---------------------------------------------------------------------------
// .env loader (no dotenv dependency)
// ---------------------------------------------------------------------------
function loadDotEnv(file) {
  try {
    const text = fs.readFileSync(file, 'utf8');
    for (const line of text.split('\n')) {
      const m = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$/);
      if (!m) continue;
      const [, key, raw] = m;
      if (process.env[key] !== undefined) continue; // real env wins
      process.env[key] = raw.replace(/^["']|["']$/g, '');
    }
  } catch {
    /* no .env file — fine, env vars may be set directly */
  }
}
loadDotEnv(path.join(__dirname, '.env'));

const API_KEY = process.env.UW_API_KEY;
const PORT = Number(process.env.PORT || 3000);
const UW_BASE = 'https://api.unusualwhales.com';

if (!API_KEY) {
  console.error(
    'Missing UW_API_KEY. Copy .env.example to .env and add your Unusual Whales API key.'
  );
  process.exit(1);
}

// ---------------------------------------------------------------------------
// In-memory TTL cache
// ---------------------------------------------------------------------------
const cache = new Map(); // url -> { expires, body, status }

function cacheGet(key) {
  const hit = cache.get(key);
  if (hit && hit.expires > Date.now()) return hit;
  cache.delete(key);
  return null;
}

function cacheSet(key, status, body, ttlMs) {
  cache.set(key, { expires: Date.now() + ttlMs, status, body });
  // Opportunistic sweep so the map doesn't grow unbounded
  if (cache.size > 500) {
    const now = Date.now();
    for (const [k, v] of cache) if (v.expires <= now) cache.delete(k);
  }
}

// ---------------------------------------------------------------------------
// Proxy route table — local path -> UW path + allowed query params + cache TTL
// ---------------------------------------------------------------------------
const ROUTES = {
  '/api/market-tide': {
    uw: '/api/market/market-tide',
    params: ['date', 'interval_5m', 'otm_only'],
    ttl: 60_000,
  },
  '/api/top-net-impact': {
    uw: '/api/market/top-net-impact',
    params: ['limit', 'date'],
    ttl: 60_000,
  },
  '/api/flow-alerts': {
    uw: '/api/option-trades/flow-alerts',
    params: [
      'limit',
      'ticker_symbol',
      'min_premium',
      'max_premium',
      'min_size',
      'min_volume_oi_ratio',
      'is_call',
      'is_put',
      'is_sweep',
      'is_floor',
      'is_ask_side',
      'is_bid_side',
      'all_opening',
      'issue_types[]',
      'min_dte',
      'max_dte',
    ],
    ttl: 30_000,
  },
  '/api/darkpool': {
    uw: '/api/darkpool/recent',
    params: ['limit', 'min_premium', 'min_size', 'date'],
    ttl: 60_000,
  },
  '/api/screener': {
    uw: '/api/screener/stocks',
    params: [
      'limit',
      'order',
      'order_direction',
      'ticker',
      'min_marketcap',
      'max_marketcap',
      'min_volume',
      'min_change',
      'max_change',
      'min_iv_rank',
      'max_iv_rank',
      'min_put_call_ratio',
      'max_put_call_ratio',
      'sectors[]',
      'issue_types[]',
      'has_dividends',
      'is_s_p_500',
    ],
    ttl: 60_000,
  },
  '/api/congress': {
    uw: '/api/congress/recent-trades',
    params: ['limit', 'ticker'],
    ttl: 300_000,
  },
};

async function handleProxy(route, reqUrl, res) {
  const upstream = new URL(route.uw, UW_BASE);
  for (const key of route.params) {
    const values = reqUrl.searchParams.getAll(key);
    for (const v of values) upstream.searchParams.append(key, v);
  }

  const cacheKey = upstream.toString();
  const hit = cacheGet(cacheKey);
  if (hit) {
    res.writeHead(hit.status, {
      'Content-Type': 'application/json',
      'X-Cache': 'HIT',
    });
    res.end(hit.body);
    return;
  }

  try {
    const uwRes = await fetch(cacheKey, {
      headers: {
        Authorization: `Bearer ${API_KEY}`,
        Accept: 'application/json',
      },
    });
    const body = await uwRes.text();
    // Cache successes for the route TTL; brief cache on errors so a burst of
    // dashboard refreshes can't hammer a failing endpoint.
    cacheSet(cacheKey, uwRes.status, body, uwRes.ok ? route.ttl : 10_000);
    res.writeHead(uwRes.status, {
      'Content-Type': 'application/json',
      'X-Cache': 'MISS',
    });
    res.end(body);
  } catch (err) {
    res.writeHead(502, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: `Upstream request failed: ${err.message}` }));
  }
}

// ---------------------------------------------------------------------------
// Static file serving
// ---------------------------------------------------------------------------
const PUBLIC_DIR = path.join(__dirname, 'public');
const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
};

function serveStatic(pathname, res) {
  const rel = pathname === '/' ? 'index.html' : pathname.slice(1);
  const file = path.normalize(path.join(PUBLIC_DIR, rel));
  if (!file.startsWith(PUBLIC_DIR)) {
    res.writeHead(403);
    res.end('Forbidden');
    return;
  }
  fs.readFile(file, (err, data) => {
    if (err) {
      res.writeHead(404, { 'Content-Type': 'text/plain' });
      res.end('Not found');
      return;
    }
    res.writeHead(200, {
      'Content-Type': MIME[path.extname(file)] || 'application/octet-stream',
    });
    res.end(data);
  });
}

// ---------------------------------------------------------------------------
// Server
// ---------------------------------------------------------------------------
const server = http.createServer((req, res) => {
  const reqUrl = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
  const route = ROUTES[reqUrl.pathname];
  if (route) {
    handleProxy(route, reqUrl, res);
  } else if (reqUrl.pathname.startsWith('/api/')) {
    res.writeHead(404, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'Unknown API route' }));
  } else {
    serveStatic(reqUrl.pathname, res);
  }
});

server.listen(PORT, () => {
  console.log(`Unusual Whales dashboard running at http://localhost:${PORT}`);
});
