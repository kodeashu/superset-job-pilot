/**
 * Local Loopback-Only WhatsApp Linked-Device Bridge for Superset Job Pilot.
 *
 * Binds strictly to 127.0.0.1 (default port 8091).
 * Persists WhatsApp multi-device session in ../data/wa_auth (gitignored).
 * Pair once by scanning the QR code in WhatsApp -> Settings -> Linked Devices.
 */
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import makeWASocket, {
  Browsers,
  DisconnectReason,
  fetchLatestBaileysVersion,
  jidNormalizedUser,
  useMultiFileAuthState,
} from '@whiskeysockets/baileys';
import pino from 'pino';
import QRCode from 'qrcode';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PORT = Number(process.env.WA_BRIDGE_PORT || 8091);
const AUTH_DIR = process.env.WA_AUTH_DIR || path.resolve(__dirname, '..', 'data', 'wa_auth');

fs.mkdirSync(AUTH_DIR, { recursive: true });

let sock = null;
let state = 'idle'; // idle | connecting | qr_ready | connected | logged_out | error
let qrDataUrl = '';
let qrText = '';
let qrUpdatedAt = 0;
let mePhone = '';
let meName = '';
let lastError = '';
let reconnectTimer = null;
let starting = false;

function hasSavedCreds() {
  try {
    return fs.existsSync(path.join(AUTH_DIR, 'creds.json'));
  } catch {
    return false;
  }
}

function normalizePhoneDigits(phone) {
  const digits = String(phone || '').replace(/\D+/g, '');
  if (digits.length === 10) return '91' + digits;
  return digits;
}

function getStatusPayload() {
  return {
    running: true,
    connected: state === 'connected',
    state,
    me_phone: mePhone,
    me_name: meName,
    qr_data_url: state === 'qr_ready' ? qrDataUrl : '',
    qr_updated_at: qrUpdatedAt,
    has_saved_session: hasSavedCreds(),
    last_error: lastError,
  };
}

function clearReconnectTimer() {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
}

async function teardownSocket() {
  clearReconnectTimer();
  if (sock) {
    try {
      sock.ev.removeAllListeners();
      sock.ws?.close();
    } catch {
      // ignore close errors
    }
    sock = null;
  }
}

async function startSocket({ fresh = false } = {}) {
  if (starting && !fresh) return;
  starting = true;
  lastError = '';

  try {
    if (fresh) {
      await teardownSocket();
      fs.rmSync(AUTH_DIR, { recursive: true, force: true });
      fs.mkdirSync(AUTH_DIR, { recursive: true });
      mePhone = '';
      meName = '';
      qrDataUrl = '';
      qrText = '';
    } else if (sock && (state === 'connected' || state === 'qr_ready')) {
      starting = false;
      return;
    } else {
      await teardownSocket();
    }

    state = 'connecting';
    const { state: authState, saveCreds } = await useMultiFileAuthState(AUTH_DIR);

    let version;
    try {
      const latest = await fetchLatestBaileysVersion();
      version = latest.version;
    } catch {
      version = undefined;
    }

    const socket = makeWASocket({
      ...(version ? { version } : {}),
      auth: authState,
      browser: Browsers.macOS('Superset Job Pilot'),
      logger: pino({ level: 'silent' }),
      printQRInTerminal: false,
      markOnlineOnConnect: false,
      syncFullHistory: false,
      connectTimeoutMs: 25000,
    });
    sock = socket;

    socket.ev.on('creds.update', saveCreds);

    socket.ev.on('connection.update', async (update) => {
      if (sock !== socket) return;
      const { connection, lastDisconnect, qr } = update;

      if (qr) {
        qrText = qr;
        qrUpdatedAt = Date.now();
        try {
          qrDataUrl = await QRCode.toDataURL(qr, {
            width: 280,
            margin: 2,
            color: { dark: '#09090b', light: '#ffffff' },
          });
          state = 'qr_ready';
          lastError = '';
          console.log('[WA-BRIDGE] QR code ready for WhatsApp Linked Devices scan.');
        } catch (err) {
          lastError = `QR render error: ${err.message}`;
        }
      }

      if (connection === 'open') {
        state = 'connected';
        qrDataUrl = '';
        qrText = '';
        lastError = '';
        const user = socket.user || {};
        const normJid = user.id ? jidNormalizedUser(user.id) : '';
        mePhone = normJid ? normJid.split('@')[0] : '';
        meName = user.name || '';
        console.log(`[WA-BRIDGE] Connected to WhatsApp (${mePhone || 'linked'}).`);
      }

      if (connection === 'close') {
        const statusCode =
          lastDisconnect?.error?.output?.statusCode ??
          lastDisconnect?.error?.statusCode ??
          0;
        const reasonMsg = lastDisconnect?.error?.message || `Closed (${statusCode})`;
        const isLoggedOut = statusCode === DisconnectReason.loggedOut || statusCode === 401;

        if (isLoggedOut) {
          state = 'logged_out';
          qrDataUrl = '';
          mePhone = '';
          meName = '';
          lastError = 'WhatsApp session unlinked. Click Generate QR Code to pair again.';
          try {
            fs.rmSync(AUTH_DIR, { recursive: true, force: true });
            fs.mkdirSync(AUTH_DIR, { recursive: true });
          } catch {
            // ignore
          }
          sock = null;
          console.log('[WA-BRIDGE] Session logged out — cleared local auth.');
        } else {
          // 515 restartRequired (normal right after QR scan!) or transient network drop -> reconnect
          const delayMs = statusCode === DisconnectReason.restartRequired || statusCode === 515 ? 800 : 3500;
          state = 'connecting';
          lastError = statusCode === 515 ? '' : reasonMsg;
          console.log(`[WA-BRIDGE] Reconnecting in ${delayMs}ms (code=${statusCode})...`);
          clearReconnectTimer();
          reconnectTimer = setTimeout(() => {
            startSocket({ fresh: false }).catch((e) => {
              lastError = e.message;
              state = 'error';
            });
          }, delayMs);
        }
      }
    });
  } catch (err) {
    state = 'error';
    lastError = err.message || String(err);
    console.error('[WA-BRIDGE] Start error:', err);
  } finally {
    starting = false;
  }
}

function readJsonBody(req) {
  return new Promise((resolve, reject) => {
    let raw = '';
    req.on('data', (chunk) => {
      raw += chunk;
      if (raw.length > 256_000) {
        reject(new Error('Body too large'));
      }
    });
    req.on('end', () => {
      if (!raw.trim()) return resolve({});
      try {
        resolve(JSON.parse(raw));
      } catch (e) {
        reject(new Error('Invalid JSON'));
      }
    });
    req.on('error', reject);
  });
}

function sendJson(res, status, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(body),
  });
  res.end(body);
}

async function waitForQrOrConnect(timeoutMs = 7000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (state === 'connected' || (state === 'qr_ready' && qrDataUrl) || state === 'error') {
      break;
    }
    await new Promise((r) => setTimeout(r, 250));
  }
}

const server = http.createServer(async (req, res) => {
  const urlPath = (req.url || '/').split('?')[0];

  try {
    if (req.method === 'GET' && urlPath === '/status') {
      return sendJson(res, 200, getStatusPayload());
    }

    if (req.method === 'POST' && urlPath === '/start') {
      const body = await readJsonBody(req);
      const fresh = Boolean(body.fresh);
      await startSocket({ fresh });
      await waitForQrOrConnect(7500);
      return sendJson(res, 200, { success: true, ...getStatusPayload() });
    }

    if (req.method === 'POST' && urlPath === '/logout') {
      if (sock) {
        try {
          await sock.logout();
        } catch {
          // ignore
        }
      }
      await teardownSocket();
      fs.rmSync(AUTH_DIR, { recursive: true, force: true });
      fs.mkdirSync(AUTH_DIR, { recursive: true });
      state = 'idle';
      qrDataUrl = '';
      qrText = '';
      mePhone = '';
      meName = '';
      lastError = '';
      return sendJson(res, 200, { success: true, ...getStatusPayload() });
    }

    if (req.method === 'POST' && urlPath === '/send') {
      const body = await readJsonBody(req);
      const text = String(body.text || '').trim();
      if (!text) {
        return sendJson(res, 400, { success: false, message: 'Message text is required.' });
      }
      if (state !== 'connected' || !sock) {
        return sendJson(res, 503, {
          success: false,
          message: 'WhatsApp is not linked yet. Open WhatsApp & Alerts and scan the QR code first.',
          state,
        });
      }

      const targetDigits = normalizePhoneDigits(body.phone || '');
      let jid = '';
      if (targetDigits) {
        jid = `${targetDigits}@s.whatsapp.net`;
      } else if (sock.user?.id) {
        jid = jidNormalizedUser(sock.user.id);
      } else {
        return sendJson(res, 400, { success: false, message: 'Could not determine destination WhatsApp JID.' });
      }

      await sock.sendMessage(jid, { text });
      console.log(`[WA-BRIDGE] Delivered WhatsApp message (${body.event_type || 'alert'}) -> ${jid}`);
      return sendJson(res, 200, {
        success: true,
        jid,
        message: `Delivered directly on WhatsApp (${jid.split('@')[0]})`,
      });
    }

    return sendJson(res, 404, { success: false, message: 'Not found' });
  } catch (err) {
    console.error('[WA-BRIDGE] HTTP error:', err);
    return sendJson(res, 500, { success: false, message: err.message || 'Internal bridge error' });
  }
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(`[WA-BRIDGE] Loopback WhatsApp bridge listening on http://127.0.0.1:${PORT}`);
  // Auto-resume existing Linked Device session on startup
  if (hasSavedCreds()) {
    console.log('[WA-BRIDGE] Saved WhatsApp session found — reconnecting automatically...');
    startSocket({ fresh: false }).catch((e) => console.error('[WA-BRIDGE] Auto-resume error:', e));
  }
});
