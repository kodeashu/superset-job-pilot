import crypto from "node:crypto";
import { getStore } from "@netlify/blobs";

const ID_RE = /^[0-9a-f]{64}$/;
const MAX_PAYLOAD_LEN = 900_000;
const RATE_LIMIT = 30;
const RATE_WINDOW_MS = 60_000;

const ipBuckets = new Map();

function getClientIp(req) {
  const xff = (req.headers.get("x-forwarded-for") || "").trim();
  if (xff) {
    const first = xff.split(",")[0].trim();
    if (first) return first.slice(0, 64);
  }
  return (req.headers.get("x-nf-client-connection-ip") || "unknown").slice(0, 64);
}

function checkRateLimit(ip) {
  const now = Date.now();
  const cutoff = now - RATE_WINDOW_MS;
  let arr = ipBuckets.get(ip);
  if (!arr) {
    arr = [];
    ipBuckets.set(ip, arr);
  }
  while (arr.length > 0 && arr[0] <= cutoff) {
    arr.shift();
  }
  if (arr.length >= RATE_LIMIT) {
    const retryAfter = Math.max(1, Math.ceil((arr[0] + RATE_WINDOW_MS - now) / 1000));
    return { allowed: false, retryAfter };
  }
  arr.push(now);
  return { allowed: true, retryAfter: 0 };
}

function safeHexEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string") return false;
  if (a.length !== 64 || b.length !== 64) return false;
  if (!ID_RE.test(a) || !ID_RE.test(b)) return false;
  return crypto.timingSafeEqual(Buffer.from(a, "hex"), Buffer.from(b, "hex"));
}

function sha256Hex(raw) {
  return crypto.createHash("sha256").update(String(raw || "")).digest("hex");
}

function jsonNoStore(data, status = 200, extraHeaders = {}) {
  return Response.json(data, {
    status,
    headers: {
      "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff",
      ...extraHeaders,
    },
  });
}

export default async (req) => {
  const clientIp = getClientIp(req);
  const rl = checkRateLimit(clientIp);
  if (!rl.allowed) {
    return jsonNoStore(
      { success: false, error: "rate_limited", retry_after: rl.retryAfter },
      429,
      { "Retry-After": String(rl.retryAfter) }
    );
  }

  const store = getStore("sjp-encrypted-vaults");
  const url = new URL(req.url);

  if (req.method === "GET") {
    const id = (url.searchParams.get("id") || "").trim().toLowerCase();
    if (!ID_RE.test(id)) {
      return jsonNoStore({ success: false, error: "invalid_id" }, 400);
    }

    const vToken = (req.headers.get("x-vault-token") || "").trim();
    const vLegacyToken = (req.headers.get("x-vault-token-legacy") || "").trim();
    if (vToken.length < 32 && vLegacyToken.length < 32) {
      return jsonNoStore({ success: false, error: "unauthorized" }, 401);
    }

    const meta = await store.getMetadata(id);
    if (meta === null) {
      return jsonNoStore({ success: false, error: "not_found" }, 404);
    }

    const storedHash = String(meta?.metadata?.token_hash || "").toLowerCase();
    if (storedHash) {
      const hCurrent = vToken.length >= 32 ? sha256Hex(vToken) : "";
      const hLegacy = vLegacyToken.length >= 32 ? sha256Hex(vLegacyToken) : "";
      if (!safeHexEqual(hCurrent, storedHash) && !safeHexEqual(hLegacy, storedHash)) {
        return jsonNoStore({ success: false, error: "forbidden" }, 403);
      }
    }

    const entry = await store.get(id);
    if (entry === null) {
      return jsonNoStore({ success: false, error: "not_found" }, 404);
    }
    return jsonNoStore({ success: true, payload: entry }, 200);
  }

  if (req.method === "POST") {
    let body = null;
    try {
      body = await req.json();
    } catch {
      return jsonNoStore({ success: false, error: "invalid_json" }, 400);
    }

    const id = String(body?.id || "").trim().toLowerCase();
    const payload = body?.payload;
    const writeToken = String(body?.write_token || "").trim();
    const legacyWriteToken = String(body?.legacy_write_token || "").trim();

    if (
      !ID_RE.test(id) ||
      typeof payload !== "string" ||
      payload.length === 0 ||
      payload.length > MAX_PAYLOAD_LEN ||
      writeToken.length < 32
    ) {
      return jsonNoStore({ success: false, error: "invalid_request" }, 400);
    }

    const tokenHash = sha256Hex(writeToken);
    const legacyTokenHash = legacyWriteToken.length >= 32 ? sha256Hex(legacyWriteToken) : "";
    const meta = await store.getMetadata(id);
    const storedHash = String(meta?.metadata?.token_hash || "").toLowerCase();
    if (storedHash) {
      if (!safeHexEqual(tokenHash, storedHash) && !safeHexEqual(legacyTokenHash, storedHash)) {
        return jsonNoStore({ success: false, error: "forbidden" }, 403);
      }
    }

    await store.set(id, payload, {
      metadata: {
        token_hash: tokenHash,
        updated_at: Date.now(),
      },
    });
    return jsonNoStore({ success: true }, 200);
  }

  return jsonNoStore({ success: false, error: "method_not_allowed" }, 405);
};
