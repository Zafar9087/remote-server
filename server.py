"""
Remote Control Server — RELIABLE CONNECTION
============================================
No stale "connected" flags.
Truth = the WebSocket object itself.
Send flow:
  1. Try to send command
  2. If send succeeds → reply "Sent"
  3. If send fails → wait up to 5s for client to reconnect, retry once
  4. If still fails → reply "Not connected"
"""

import asyncio, json, logging, os
from datetime import datetime
from aiohttp import web
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rc")

BOT_TOKEN  = os.getenv("BOT_TOKEN", "")
ADMIN_IDS  = set(int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip())
SECRET_KEY = os.getenv("SECRET_KEY", "changeme")
PUBLIC_URL = os.getenv("PUBLIC_URL", "http://localhost:8080")
PORT       = int(os.getenv("PORT", 8080))

# clients[name] = WebSocketResponse object (or None if disconnected)
# The WS object itself is the truth — no separate "connected" bool needed
clients: dict[str, web.WebSocketResponse] = {}
last_seen: dict[str, str] = {}
history: list[dict] = []

tg_app = None

def _now():
    return datetime.now().strftime("%H:%M:%S")

def _is_alive(ws: web.WebSocketResponse) -> bool:
    """Check if a WebSocket is actually open and usable."""
    if ws is None:
        return False
    try:
        return not ws.closed
    except Exception:
        return False

async def _try_send(name: str, payload: dict, wait_secs: float = 5.0) -> bool:
    """
    Try to send payload to client.
    If the socket is closed, wait up to wait_secs for a reconnect, then retry.
    Returns True if sent successfully, False otherwise.
    """
    ws = clients.get(name)

    # First attempt — socket looks alive
    if _is_alive(ws):
        try:
            await ws.send_json(payload)
            return True
        except Exception as e:
            log.warning(f"Send to '{name}' failed mid-flight: {e}")
            # Socket died just now — fall through to wait

    # Socket is dead or just died — wait for reconnect
    log.info(f"Waiting {wait_secs}s for '{name}' to reconnect...")
    deadline = asyncio.get_event_loop().time() + wait_secs
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.25)
        ws = clients.get(name)
        if _is_alive(ws):
            try:
                await ws.send_json(payload)
                log.info(f"'{name}' reconnected — command delivered")
                return True
            except Exception as e:
                log.warning(f"Send after reconnect failed: {e}")
                break   # give up

    return False

async def send_telegram(chat_id: int, text: str):
    if tg_app and chat_id:
        try:
            chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
            for chunk in chunks[:3]:
                await tg_app.bot.send_message(chat_id=chat_id, text=chunk)
        except Exception as e:
            log.error(f"Telegram send failed: {e}")

# =============================================================================
#  WEBSOCKET HANDLER
# =============================================================================
async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=25)
    await ws.prepare(request)
    client_name = None

    try:
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                break
            try:
                data = json.loads(msg.data)
                mtype = data.get("type", "")

                # ── REGISTER ──────────────────────────────────────────────────
                if mtype == "register":
                    name   = data.get("name", "").strip()
                    secret = data.get("secret", "")
                    if secret != SECRET_KEY:
                        await ws.send_json({"type": "error", "msg": "Invalid secret key"})
                        await ws.close()
                        return
                    client_name = name
                    clients[name] = ws          # store the live WS object
                    last_seen[name] = _now()
                    log.info(f"+ {name} registered")
                    await ws.send_json({"type": "ok"})

                # ── RESULT ────────────────────────────────────────────────────
                elif mtype == "result":
                    cmd     = data.get("command", "")
                    result  = data.get("result", "")
                    chat_id = data.get("reply_chat_id")
                    history.append({
                        "time":    _now(),
                        "script":  client_name or "?",
                        "command": cmd[:80],
                        "result":  result[:200],
                    })
                    if len(history) > 200:
                        history.pop(0)
                    log.info(f"  result ← {client_name}: {cmd[:50]}")
                    if chat_id:
                        await send_telegram(int(chat_id), f"[{client_name}]\n{result}")

                # ── PING ──────────────────────────────────────────────────────
                elif mtype == "ping":
                    if client_name:
                        last_seen[client_name] = _now()

            except Exception as e:
                log.warning(f"msg parse error: {e}")

    except Exception as e:
        log.warning(f"ws error ({client_name}): {e}")
    finally:
        # Only clear client entry if it's still THIS ws object
        # (a fast reconnect may have already replaced it)
        if client_name and clients.get(client_name) is ws:
            clients[client_name] = None
            log.info(f"- {client_name} disconnected")

    return ws

# =============================================================================
#  TELEGRAM BOT HANDLERS
# =============================================================================
async def tg_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🖥 Remote Control\n\n"
        "/send <script> <cmd> — send command\n"
        "/broadcast <cmd>     — send to all\n"
        "/scripts             — list scripts"
    )

async def tg_send(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only")
        return

    args = " ".join(ctx.args)
    if not args:
        await update.message.reply_text("Usage: /send <script> <command>")
        return
    parts = args.split(None, 1)
    if len(parts) < 2:
        await update.message.reply_text("Usage: /send <script> <command>")
        return

    name, cmd = parts[0], parts[1]

    # Script never registered at all
    if name not in clients and name not in last_seen:
        online = [n for n, w in clients.items() if _is_alive(w)]
        await update.message.reply_text(
            f"❌ Script '{name}' not found.\n"
            f"Online: {', '.join(online) or 'none'}"
        )
        return

    # Build payload
    payload = {
        "type":          "command",
        "command":       cmd,
        "reply_chat_id": update.effective_chat.id,
    }

    # Send with wait-and-retry
    ok = await _try_send(name, payload, wait_secs=5.0)

    if ok:
        await update.message.reply_text(f"✅ Sent to {name}")
    else:
        await update.message.reply_text(
            f"❌ '{name}' is not connected.\n"
            f"Last seen: {last_seen.get(name, 'never')}"
        )

async def tg_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only")
        return
    cmd = " ".join(ctx.args)
    if not cmd:
        await update.message.reply_text("Usage: /broadcast <command>")
        return

    sent = []
    for name, ws in clients.items():
        if _is_alive(ws):
            try:
                await ws.send_json({
                    "type":          "command",
                    "command":       cmd,
                    "reply_chat_id": update.effective_chat.id,
                })
                sent.append(name)
            except Exception as e:
                log.warning(f"broadcast to {name}: {e}")

    await update.message.reply_text(
        f"📡 Broadcast to {len(sent)}: {', '.join(sent) or 'none'}"
    )

async def tg_scripts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    all_names = set(clients.keys()) | set(last_seen.keys())
    if not all_names:
        await update.message.reply_text("No scripts have ever connected.")
        return
    msg = "📋 Scripts:\n\n"
    for name in sorted(all_names):
        ws = clients.get(name)
        icon = "🟢" if _is_alive(ws) else "🔴"
        seen = last_seen.get(name, "never")
        msg += f"{icon} {name}  (last seen: {seen})\n"
    await update.message.reply_text(msg)

async def tg_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only")
        return
    await update.message.reply_text(f"🌐 Admin Panel:\n{PUBLIC_URL}/admin\nKey: {SECRET_KEY}")

# =============================================================================
#  HTTP API
# =============================================================================
async def h_status(request):
    online  = [n for n, w in clients.items() if _is_alive(w)]
    offline = [n for n in (set(clients) | set(last_seen)) if n not in online]
    return web.json_response({"status": "ok", "online": online, "offline": offline})

async def h_scripts(request):
    if request.headers.get("X-Admin-Key", "") != SECRET_KEY:
        return web.json_response({"error": "Unauthorized"}, status=401)
    all_names = set(clients.keys()) | set(last_seen.keys())
    scripts = [
        {
            "name":      n,
            "connected": _is_alive(clients.get(n)),
            "last_seen": last_seen.get(n, "never"),
        }
        for n in sorted(all_names)
    ]
    scripts.sort(key=lambda x: (not x["connected"], x["name"]))
    return web.json_response({"scripts": scripts, "history": history[-30:]})

async def h_send(request):
    if request.headers.get("X-Admin-Key", "") != SECRET_KEY:
        return web.json_response({"error": "Unauthorized"}, status=401)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Bad JSON"}, status=400)

    name = data.get("script", "").strip()
    cmd  = data.get("command", "").strip()
    if not name or not cmd:
        return web.json_response({"error": "script and command required"}, status=400)

    payload = {"type": "command", "command": cmd, "reply_chat_id": data.get("reply_chat_id")}
    ok = await _try_send(name, payload, wait_secs=5.0)
    if ok:
        return web.json_response({"status": "sent"})
    return web.json_response({"error": f"'{name}' not connected"}, status=503)

async def h_broadcast(request):
    if request.headers.get("X-Admin-Key", "") != SECRET_KEY:
        return web.json_response({"error": "Unauthorized"}, status=401)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Bad JSON"}, status=400)
    cmd = data.get("command", "").strip()
    if not cmd:
        return web.json_response({"error": "command required"}, status=400)
    sent = []
    for name, ws in clients.items():
        if _is_alive(ws):
            try:
                await ws.send_json({"type": "command", "command": cmd})
                sent.append(name)
            except Exception:
                pass
    return web.json_response({"sent_to": sent, "count": len(sent)})

# =============================================================================
#  MAIN
# =============================================================================
async def main():
    global tg_app

    if BOT_TOKEN:
        tg_app = Application.builder().token(BOT_TOKEN).build()
        tg_app.add_handler(CommandHandler("start",     tg_start))
        tg_app.add_handler(CommandHandler("send",      tg_send))
        tg_app.add_handler(CommandHandler("broadcast", tg_broadcast))
        tg_app.add_handler(CommandHandler("scripts",   tg_scripts))
        tg_app.add_handler(CommandHandler("panel",     tg_panel))
        await tg_app.initialize()
        await tg_app.start()
        asyncio.create_task(tg_app.updater.start_polling())
        log.info("✓ Telegram bot started")
    else:
        log.warning("BOT_TOKEN not set — bot disabled")

    web_app = web.Application()
    web_app.router.add_get( "/ws",            ws_handler)
    web_app.router.add_get( "/api/status",    h_status)
    web_app.router.add_get( "/api/scripts",   h_scripts)
    web_app.router.add_post("/api/send",      h_send)
    web_app.router.add_post("/api/broadcast", h_broadcast)

    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

    log.info(f"✓ Server on port {PORT}")
    log.info(f"✓ WS: {PUBLIC_URL.replace('http','ws')}/ws")

    try:
        await asyncio.Future()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
