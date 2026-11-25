import crypto from "node:crypto";
import { getStore } from "@netlify/blobs";

const ID_RE = /^[0-9a-f]{64}$/;
const MAX_PAYLOAD_LEN = 900_000;

export default async (req) => {
  const store = getStore("sjp-encrypted-vaults");
  const url = new URL(req.url);

  if (req.method === "GET") {
    const id = (url.searchParams.get("id") || "").trim().toLowerCase();
    if (!ID_RE.test(id)) {
      return Response.json({ success: false, error: "invalid_id" }, { status: 400 });
    }
    const entry = await store.get(id);
    if (entry === null) {
      return Response.json({ success: false, error: "not_found" }, { status: 404 });
    }
    return Response.json({ success: true, payload: entry });
  }

  if (req.method === "POST") {
    let body = null;
    try {
      body = await req.json();
    } catch {
      return Response.json({ success: false, error: "invalid_json" }, { status: 400 });
    }

    const id = String(body?.id || "").trim().toLowerCase();
    const payload = body?.payload;
    const writeToken = String(body?.write_token || "").trim();

    if (
      !ID_RE.test(id) ||
      typeof payload !== "string" ||
      payload.length === 0 ||
      payload.length > MAX_PAYLOAD_LEN ||
      writeToken.length < 32
    ) {
      return Response.json({ success: false, error: "invalid_request" }, { status: 400 });
    }

    const tokenHash = crypto.createHash("sha256").update(writeToken).digest("hex");
    const meta = await store.getMetadata(id);
    if (meta !== null && meta.metadata && meta.metadata.token_hash && meta.metadata.token_hash !== tokenHash) {
      return Response.json({ success: false, error: "forbidden" }, { status: 403 });
    }

    await store.set(id, payload, {
      metadata: {
        token_hash: tokenHash,
        updated_at: Date.now(),
      },
    });
    return Response.json({ success: true });
  }

  return Response.json({ success: false, error: "method_not_allowed" }, { status: 405 });
};
