# wa-bridge

Phase 0 bridge between **whatsapp-web.js** and the FastAPI/LangGraph agent. It runs
a single WhatsApp Web session (your own number for the trial), forwards inbound
messages to the Python app, and sends the agent's replies back.

The bridge speaks a **library-agnostic contract** (see the header in `index.js`), so
it can be re-implemented on **Baileys** at Phase 2 without touching any Python.

## Prerequisites

- Node.js **18+** (uses the global `fetch`)
- The Python app running with `CHANNEL_PROVIDER=wa_web`

## Setup

```bash
cd wa-bridge
npm install
cp .env.example .env   # adjust if your FastAPI app isn't on localhost:8000
```

> If `npm install whatsapp-web.js` resolves an old version, pin the latest:
> `npm install whatsapp-web.js@latest`. It pulls a headless Chromium via Puppeteer.

## Run (two terminals)

**Terminal 1 — the Python app** (with these in your Python `.env`):

```
CHANNEL_PROVIDER=wa_web
WA_BRIDGE_URL=http://localhost:3000
# WA_BRIDGE_TOKEN=some-shared-secret   # optional, must match wa-bridge/.env
```

```bash
uvicorn app.main:app --reload --port 8000
```

**Terminal 2 — the bridge:**

```bash
cd wa-bridge
npm start
```

A QR code prints in the terminal. Scan it with the WhatsApp app on the number you
want the agent to run on (Settings → Linked Devices → Link a device). Once it says
`WhatsApp session ready.`, message that number from another phone and the agent
replies.

## Notes

- Session auth persists in `wa-bridge/.wwebjs_auth/` — keep it to avoid re-scanning;
  delete it to link a different number.
- Single number only in Phase 0. Multi-session (one per tenant) + Baileys comes at
  Phase 2.
- Inbound media (receipts) is downloaded by the bridge and handed to the agent's
  existing OCR flow; no Meta media IDs involved.
