export default async (req) => {
  const healthUrl =
    (typeof Netlify !== "undefined" && Netlify.env?.get("RENDER_HEALTH_URL")) ||
    "https://superset-job-pilot.onrender.com/api/health";

  try {
    const res = await fetch(healthUrl, {
      method: "GET",
      headers: { "User-Agent": "Netlify-Keep-Warm/1.0" },
      signal: AbortSignal.timeout(15000),
    });
    const text = await res.text();
    console.log(`[keep-render-warm] Pinged ${healthUrl} -> HTTP ${res.status}: ${text.slice(0, 120)}`);
    return Response.json({ ok: res.ok, status: res.status });
  } catch (err) {
    console.error(`[keep-render-warm] Ping failed for ${healthUrl}:`, err?.message || err);
    return Response.json({ ok: false, error: String(err?.message || err) }, { status: 502 });
  }
};

export const config = {
  schedule: "*/8 * * * *",
};
