import Anthropic from "@anthropic-ai/sdk";

/* ============================================================
   /api/classify — server-side catalyst classifier
   Keeps the Anthropic API key server-side (never shipped to the
   browser) and routes the call through the official SDK. Mounted
   as Vite dev-server middleware (see vite.config.js).
   ============================================================ */

const MODEL = "claude-opus-5-5";

function send(res, status, payload) {
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json");
  res.end(JSON.stringify(payload));
}

async function readJson(req) {
  let raw = "";
  for await (const chunk of req) raw += chunk;
  if (!raw) return {};
  return JSON.parse(raw);
}

export async function classifyHandler(req, res) {
  if (req.method !== "POST") return send(res, 405, { error: "Method not allowed — use POST." });

  if (!process.env.ANTHROPIC_API_KEY) {
    return send(res, 503, {
      error:
        "ANTHROPIC_API_KEY is not set on the server. Export it and restart `npm run dev` to enable catalyst classification.",
    });
  }

  let tickers;
  try {
    ({ tickers } = await readJson(req));
  } catch {
    return send(res, 400, { error: "Request body must be valid JSON." });
  }
  if (!Array.isArray(tickers) || tickers.length === 0) {
    return send(res, 400, { error: "Body must include a non-empty `tickers` array." });
  }

  const prompt = `You are the news-catalyst classifier for a rules-based AI/tech equity strategy. For each ticker in ${JSON.stringify(
    tickers,
  )}, search the web for the most significant catalyst from the last 5 trading days (earnings, guidance, analyst action, product/AI news, regulatory).
Respond with ONLY a JSON array, no markdown fences, no prose. Each element:
{"ticker":"NVDA","headline":"<=90 chars","classification":"bullish|bearish|neutral","confidence":0-100,"rationale":"<=140 chars"}
If nothing material found for a ticker, use classification "neutral", confidence 20, headline "No material catalyst found".`;

  try {
    const client = new Anthropic(); // resolves ANTHROPIC_API_KEY from env
    const message = await client.messages.create({
      model: MODEL,
      max_tokens: 4000,
      messages: [{ role: "user", content: prompt }],
      tools: [{ type: "web_search_20260209", name: "web_search", max_uses: 5 }],
    });

    if (message.stop_reason === "refusal") {
      return send(res, 502, { error: "The model declined to classify these tickers." });
    }

    const text = message.content
      .filter((b) => b.type === "text")
      .map((b) => b.text)
      .join("\n");
    const start = text.indexOf("[");
    const end = text.lastIndexOf("]");
    if (start === -1 || end === -1 || end < start) {
      throw new Error("Model response did not contain a JSON array.");
    }
    const rows = JSON.parse(text.slice(start, end + 1));
    return send(res, 200, { rows });
  } catch (e) {
    if (e instanceof Anthropic.AuthenticationError) {
      return send(res, 401, { error: "Anthropic rejected the API key (authentication failed)." });
    }
    if (e instanceof Anthropic.RateLimitError) {
      return send(res, 429, { error: "Rate limited by the Anthropic API — try again shortly." });
    }
    return send(res, 502, { error: "Classifier failed: " + String(e?.message || e) });
  }
}
