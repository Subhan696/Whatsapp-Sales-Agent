/**
 * Baileys  <->  FastAPI bridge  (multi-tenant).
 *
 * One Node process manages N independent WhatsApp sessions — one per
 * tenant — using Baileys (a WebSocket-only reimplementation of the WhatsApp
 * Web protocol, no browser/Chromium per session — this is why we moved off
 * whatsapp-web.js: density matters once you're running dozens of tenants).
 *
 * Configure which tenants this bridge instance manages via WA_WEB_TENANT_IDS
 * (comma-separated tenant ids, e.g. "1,2,5"). Defaults to "1" so existing
 * single-tenant setups need no config changes.
 *
 * Auth state is DB-backed (Postgres, table baileys_auth_state) when
 * WA_BRIDGE_DATABASE_URL is set — sessions then survive a redeploy onto a
 * different machine, not just a same-box restart. Falls back to local
 * multi-file storage (./.baileys_auth/<tenantId>/) if that env var is
 * unset, so a bare-bones local dev setup still needs zero DB config.
 *
 * Library-agnostic contract — unchanged from the whatsapp-web.js version,
 * so the Python side needed zero changes for this migration:
 *
 *   Inbound  (bridge -> Python):  POST {PYTHON_INBOUND_URL}
 *     { message_id, from, to, tenant_id, name, type, timestamp, text,
 *       media_base64, mime_type, caption }
 *
 *   Outbound (Python -> bridge):
 *     POST /send        { tenant_id, to, body }                          -> { id }
 *     POST /send-media  { tenant_id, to, link | base64, type, caption }  -> { id }
 *     GET  /health       -> { status, sessions: { [tenantId]: state } }
 *
 * `from` / `to` are plain numbers (no @s.whatsapp.net suffix). The bridge
 * owns every Baileys specific (JID formatting, media download, type
 * mapping) so the Python side stays channel-neutral. Internally the bridge
 * also tracks each contact's exact inbound JID (per-tenant jidCache, see
 * createSession) so replies route correctly for contacts WhatsApp has
 * migrated to @lid identifiers instead of @s.whatsapp.net — Python never
 * needs to know this distinction exists.
 */
require('dotenv').config();

const {
  default: makeWASocket,
  useMultiFileAuthState,
  fetchLatestBaileysVersion,
  downloadMediaMessage,
  DisconnectReason,
  isLidUser,
} = require('@whiskeysockets/baileys');
const { Boom } = require('@hapi/boom');
const qrcodeTerminal = require('qrcode-terminal');
const qrcodePng = require('qrcode');
const express = require('express');
const pino = require('pino');
const { Pool } = require('pg');
const fs = require('fs/promises');
const { usePostgresAuthState, clearAuthState } = require('./pg-auth-state');

const PORT = process.env.BRIDGE_PORT || 3000;
const PYTHON_INBOUND_URL =
  process.env.PYTHON_INBOUND_URL || 'http://localhost:8000/wa-bridge/inbound';
const BRIDGE_TOKEN = process.env.WA_BRIDGE_TOKEN || '';
const TENANT_IDS = (process.env.WA_WEB_TENANT_IDS || '1')
  .split(',')
  .map((s) => s.trim())
  .filter(Boolean);

const baileysLogger = pino({ level: process.env.WA_LOG_LEVEL || 'silent' });

const pgPool = process.env.WA_BRIDGE_DATABASE_URL
  ? new Pool({ connectionString: process.env.WA_BRIDGE_DATABASE_URL })
  : null;
if (pgPool) {
  console.log('[wa-bridge] Using DB-backed auth state (Postgres)');
} else {
  console.log('[wa-bridge] WA_BRIDGE_DATABASE_URL not set — using local file-based auth state');
}

async function clearTenantAuthState(tenantId) {
  if (pgPool) {
    await clearAuthState(pgPool, Number(tenantId));
  } else {
    await fs.rm(`./.baileys_auth/${tenantId}`, { recursive: true, force: true });
  }
}

// Tenants currently being intentionally disconnected via /disconnect. The
// connection.update 'close' handler checks this so an admin-initiated logout
// doesn't get treated like an unexpected drop and auto-reconnect / re-QR.
const disconnecting = new Set();

// Message content types we never forward to the agent.
const SILENT_TYPES = new Set(['reaction', 'protocol', 'unknown']);

// tenantId (string) -> { sock, status }. status: 'qr_pending' | 'ready' | 'disconnected' | 'logged_out'
const sessions = new Map();

function jidToNumber(jid) {
  // "923211017677:42@s.whatsapp.net" -> "923211017677"
  return (jid || '').split('@')[0].split(':')[0];
}

function extractContent(message) {
  if (!message) return { type: 'unknown', text: '', mediaMessage: null };
  if (message.conversation) return { type: 'text', text: message.conversation, mediaMessage: null };
  if (message.extendedTextMessage) {
    return { type: 'text', text: message.extendedTextMessage.text || '', mediaMessage: null };
  }
  if (message.imageMessage) {
    return { type: 'image', text: message.imageMessage.caption || '', mediaMessage: message.imageMessage };
  }
  if (message.videoMessage) {
    return { type: 'video', text: message.videoMessage.caption || '', mediaMessage: message.videoMessage };
  }
  if (message.audioMessage) {
    return { type: 'audio', text: '', mediaMessage: message.audioMessage };
  }
  if (message.documentMessage) {
    return {
      type: 'document',
      text: message.documentMessage.caption || '',
      mediaMessage: message.documentMessage,
    };
  }
  if (message.locationMessage) return { type: 'location', text: '', mediaMessage: null };
  if (message.reactionMessage) return { type: 'reaction', text: '', mediaMessage: null };
  return { type: 'unknown', text: '', mediaMessage: null };
}

async function postInbound(body) {
  const res = await fetch(PYTHON_INBOUND_URL, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(BRIDGE_TOKEN ? { 'X-Bridge-Token': BRIDGE_TOKEN } : {}),
    },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`inbound POST ${res.status}`);
}

let cachedVersion = null;

async function createSession(tenantId) {
  const { state, saveCreds } = pgPool
    ? await usePostgresAuthState(pgPool, Number(tenantId))
    : await useMultiFileAuthState(`./.baileys_auth/${tenantId}`);
  if (!cachedVersion) {
    cachedVersion = (await fetchLatestBaileysVersion()).version;
  }

  const sock = makeWASocket({
    version: cachedVersion,
    auth: state,
    logger: baileysLogger,
  });

  // number -> full inbound JID, e.g. "923211017677" -> "923211017677@s.whatsapp.net"
  // or, for contacts WhatsApp has migrated to its privacy-preserving identifier,
  // "184320..." -> "184320...@lid". Replies must go to the exact JID a contact
  // messaged from — reconstructing "<number>@s.whatsapp.net" blindly is wrong
  // for @lid contacts and silently fails to deliver even though sendMessage()
  // reports success (it just means the syntactically-valid JID was accepted,
  // not that it resolves to the right account).
  sessions.set(tenantId, { sock, status: 'qr_pending', jidCache: new Map() });

  sock.ev.on('creds.update', saveCreds);

  sock.ev.on('connection.update', (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      console.log(`\n[wa-bridge] [tenant ${tenantId}] Scan this QR with that tenant's WhatsApp number:\n`);
      qrcodeTerminal.generate(qr, { small: true });
      sessions.get(tenantId).status = 'qr_pending';
      sessions.get(tenantId).qr = qr;
    }

    if (connection === 'open') {
      console.log(`[wa-bridge] [tenant ${tenantId}] WhatsApp session ready.`);
      sessions.get(tenantId).status = 'ready';
      sessions.get(tenantId).qr = null;
    }

    if (connection === 'close') {
      // Admin-initiated disconnect: the /disconnect handler owns all cleanup
      // (logout, session delete, auth wipe). Do nothing here — otherwise we'd
      // immediately spin up a fresh QR session and undo the disconnect.
      if (disconnecting.has(tenantId)) return;

      const statusCode =
        lastDisconnect && lastDisconnect.error instanceof Boom
          ? lastDisconnect.error.output?.statusCode
          : undefined;
      const loggedOut = statusCode === DisconnectReason.loggedOut;
      console.error(
        `[wa-bridge] [tenant ${tenantId}] disconnected (loggedOut=${loggedOut}):`,
        lastDisconnect?.error?.message || 'unknown reason'
      );
      const entry = sessions.get(tenantId);
      if (entry) entry.status = loggedOut ? 'logged_out' : 'disconnected';
      if (!loggedOut) {
        // Network blip / restart — reconnect using the same saved creds.
        createSession(tenantId).catch((e) =>
          console.error(`[wa-bridge] [tenant ${tenantId}] reconnect failed:`, e.message)
        );
      } else {
        // Saved creds are no longer valid — wipe them and start a fresh
        // session immediately so a new QR is ready to scan, rather than
        // leaving the tenant stuck until someone restarts the process.
        clearTenantAuthState(tenantId)
          .catch((e) => console.error(`[wa-bridge] [tenant ${tenantId}] clear auth state failed:`, e.message))
          .finally(() => {
            createSession(tenantId).catch((e) =>
              console.error(`[wa-bridge] [tenant ${tenantId}] fresh session start failed:`, e.message)
            );
          });
      }
    }
  });

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    if (type !== 'notify') return;
    for (const msg of messages) {
      try {
        if (!msg.message) continue;
        if (msg.key.fromMe) continue;
        const remoteJid = msg.key.remoteJid || '';
        if (remoteJid.endsWith('@g.us')) continue; // skip groups for the trial
        if (remoteJid === 'status@broadcast') continue;

        const { type: msgType, text, mediaMessage } = extractContent(msg.message);
        if (SILENT_TYPES.has(msgType)) continue;

        // jidToNumber(remoteJid) is only a real phone number for non-LID
        // contacts. For @lid contacts it's WhatsApp's opaque privacy
        // identifier — useless as a customer-facing "number" in the CRM.
        // Resolve the real phone number when possible; the LID itself
        // still gets cached below for routing replies (delivery needs the
        // exact JID a contact messaged from, not their phone number).
        let from = jidToNumber(remoteJid);
        if (isLidUser(remoteJid)) {
          try {
            const pn = await sock.signalRepository.lidMapping.getPNForLID(remoteJid);
            if (pn) from = jidToNumber(pn);
          } catch (e) {
            console.error(`[wa-bridge] [tenant ${tenantId}] LID->PN resolution failed for ${remoteJid}:`, e.message);
          }
        }
        const to = jidToNumber(sock.user?.id || '');
        sessions.get(tenantId).jidCache.set(from, remoteJid);

        const payload = {
          message_id: msg.key.id,
          from,
          to,
          tenant_id: tenantId,
          name: msg.pushName || '',
          type: msgType,
          timestamp: Number(msg.messageTimestamp) || Math.floor(Date.now() / 1000),
          text: msgType === 'text' ? text : null,
          media_base64: null,
          mime_type: null,
          caption: text || null,
        };

        if (mediaMessage) {
          const buffer = await downloadMediaMessage(msg, 'buffer', {});
          payload.media_base64 = buffer.toString('base64');
          payload.mime_type = mediaMessage.mimetype || 'application/octet-stream';
        }

        await postInbound(payload);
        console.log(`[wa-bridge] [tenant ${tenantId}] -> agent  ${msgType} from ${from}`);
      } catch (e) {
        console.error(`[wa-bridge] [tenant ${tenantId}] inbound error:`, e.message);
      }
    }
  });

  return sock;
}

// Guards concurrent /connect calls for the same tenant from racing each
// other into two parallel Baileys sockets — createSession does real async
// work (reading auth state) before `sessions.set(...)` runs, so two
// near-simultaneous callers could both see "no session yet" otherwise.
const sessionCreationInFlight = new Set();

async function ensureSession(tenantId) {
  if (sessions.has(tenantId) || sessionCreationInFlight.has(tenantId)) return;
  // Clear any lingering intentional-disconnect flag — an explicit connect
  // overrides a prior disconnect for this tenant.
  disconnecting.delete(tenantId);
  sessionCreationInFlight.add(tenantId);
  try {
    await createSession(tenantId);
  } finally {
    sessionCreationInFlight.delete(tenantId);
  }
}

async function getBootTenantIds() {
  if (pgPool) {
    const res = await pgPool.query("SELECT id FROM tenants WHERE status = 'active' ORDER BY id");
    return res.rows.map((r) => String(r.id));
  }
  return TENANT_IDS;
}

getBootTenantIds()
  .then((bootTenantIds) => {
    for (const tenantId of bootTenantIds) {
      createSession(tenantId).catch((e) =>
        console.error(`[wa-bridge] [tenant ${tenantId}] failed to start:`, e.message)
      );
    }
  })
  .catch((e) => console.error('[wa-bridge] failed to determine boot tenant list:', e.message));

// --------------------------------------------------------------------------
// Outbound HTTP surface (called by WaWebClient on the Python side)
// --------------------------------------------------------------------------
const app = express();
app.use(express.json({ limit: '25mb' }));

app.use((req, res, next) => {
  if (BRIDGE_TOKEN && req.headers['x-bridge-token'] !== BRIDGE_TOKEN) {
    return res.status(403).json({ error: 'invalid token' });
  }
  next();
});

function sessionFor(req, res) {
  const tenantId = String(req.body.tenant_id ?? TENANT_IDS[0]);
  const session = sessions.get(tenantId);
  if (!session) {
    res.status(404).json({ error: `no session configured for tenant_id=${tenantId}` });
    return null;
  }
  if (session.status !== 'ready') {
    res.status(503).json({ error: `tenant_id=${tenantId} session not ready (status=${session.status})` });
    return null;
  }
  return session;
}

// Reply to the exact JID a contact last messaged from (preserves @lid vs
// @s.whatsapp.net — see the jidCache comment in createSession). Falls back
// to the standard phone-number JID for outbound-initiated conversations
// we've never received an inbound message from yet.
function resolveOutboundJid(session, to) {
  return session.jidCache.get(to) || `${to}@s.whatsapp.net`;
}

app.get('/health', (_req, res) => {
  const out = {};
  for (const [tenantId, s] of sessions) out[tenantId] = s.status;
  res.json({ status: 'ok', sessions: out });
});

// Starts a session on demand for a tenant that doesn't have one running yet
// (e.g. just created through the CRM, never seen by this bridge process
// before). Safe to call repeatedly — a no-op once a session exists,
// regardless of its status. This is what makes "connect via dashboard"
// possible without editing WA_WEB_TENANT_IDS or restarting the process.
app.post('/connect/:tenantId', async (req, res) => {
  try {
    await ensureSession(req.params.tenantId);
    const session = sessions.get(req.params.tenantId);
    res.json({ status: session ? session.status : 'starting' });
  } catch (e) {
    console.error(`[wa-bridge] [tenant ${req.params.tenantId}] connect failed:`, e.message);
    res.status(500).json({ error: e.message });
  }
});

// Disconnect a tenant's WhatsApp: log the device out (so it disappears from
// the phone's Linked Devices list), tear down the socket, and wipe the saved
// auth state so it does NOT auto-reconnect. Reconnecting afterward requires a
// fresh QR scan. This is how the CRM "Disconnect / Stop agent" button works —
// a disconnected tenant receives no messages, so the agent is effectively off.
app.post('/disconnect/:tenantId', async (req, res) => {
  const tenantId = req.params.tenantId;
  const session = sessions.get(tenantId);
  disconnecting.add(tenantId);
  try {
    if (session && session.sock) {
      try {
        await session.sock.logout();
      } catch (e) {
        // logout can throw if already disconnected — proceed to clean up anyway.
        console.warn(`[wa-bridge] [tenant ${tenantId}] logout warning:`, e.message);
      }
      try {
        session.sock.end();
      } catch { /* already closed */ }
    }
    sessions.delete(tenantId);
    await clearTenantAuthState(tenantId);
    // NOTE: tenantId intentionally stays in `disconnecting` so any late
    // 'close' event from the socket teardown is ignored rather than
    // triggering an auto-reconnect. The flag is cleared when the admin
    // explicitly reconnects (ensureSession) — keeping the tenant off until then.
    console.log(`[wa-bridge] [tenant ${tenantId}] disconnected and auth state cleared.`);
    res.json({ status: 'disconnected' });
  } catch (e) {
    console.error(`[wa-bridge] [tenant ${tenantId}] disconnect failed:`, e.message);
    res.status(500).json({ error: e.message });
  }
});

// Renders the current pairing QR as a PNG, so it can be viewed/scanned
// without needing terminal access to read the ASCII version off the log.
app.get('/qr/:tenantId', async (req, res) => {
  const session = sessions.get(req.params.tenantId);
  if (!session) return res.status(404).json({ error: `no session configured for tenant_id=${req.params.tenantId}` });
  if (!session.qr) return res.status(404).json({ error: `no pending QR for tenant_id=${req.params.tenantId} (status=${session.status})` });
  const png = await qrcodePng.toBuffer(session.qr, { width: 400, margin: 2 });
  res.set('Content-Type', 'image/png');
  res.send(png);
});

app.post('/send', async (req, res) => {
  try {
    const session = sessionFor(req, res);
    if (!session) return;
    const { to, body } = req.body;
    const sent = await session.sock.sendMessage(resolveOutboundJid(session, to), { text: body });
    res.json({ id: sent.key.id });
  } catch (e) {
    console.error('[wa-bridge] /send error:', e.message);
    res.status(500).json({ error: e.message });
  }
});

app.post('/send-media', async (req, res) => {
  try {
    const session = sessionFor(req, res);
    if (!session) return;
    const { to, link, base64, mime_type, caption, type } = req.body;
    const mediaField = type === 'video' ? 'video' : 'image';
    const content = link
      ? { [mediaField]: { url: link }, caption: caption || '' }
      : { [mediaField]: Buffer.from(base64, 'base64'), mimetype: mime_type, caption: caption || '' };
    const sent = await session.sock.sendMessage(resolveOutboundJid(session, to), content);
    res.json({ id: sent.key.id });
  } catch (e) {
    console.error('[wa-bridge] /send-media error:', e.message);
    res.status(500).json({ error: e.message });
  }
});

app.listen(PORT, () => console.log(`[wa-bridge] HTTP listening on :${PORT} — tenants: ${TENANT_IDS.join(', ')}`));
