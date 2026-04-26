"""
Simple Remote Control Server
Simple, stable, just works
"""

import asyncio, json, logging, os
from datetime import datetime
from aiohttp import web
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("rc")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = set(int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip())
SECRET_KEY = os.getenv("SECRET_KEY", "changeme")
PUBLIC_URL = os.getenv("PUBLIC_URL", "http://localhost:8080")
PORT = int(os.getenv("PORT", 8080))

clients = {}
command_history = []

async def ws_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    client_name = None
    
    async for msg in ws:
        try:
            data = json.loads(msg.data)
            
            if data.get("type") == "register":
                name = data.get("name", "unknown")
                secret = data.get("secret", "")
                if secret != SECRET_KEY:
                    await ws.send_json({"type": "error", "msg": "Invalid secret"})
                    await ws.close()
                    return
                client_name = name
                clients[name] = {"ws": ws, "connected": True, "last_seen": datetime.now()}
                log.info(f"✓ {name} registered")
                await ws.send_json({"type": "ok"})
            
            elif data.get("type") == "result":
                cmd = data.get("command", "")
                result = data.get("result", "")
                command_history.append({
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "script": client_name or "unknown",
                    "command": cmd,
                    "result": result[:200],
                })
                if len(command_history) > 50:
                    command_history.pop(0)
                log.info(f"✓ Result from {client_name}")
            
            elif data.get("type") == "ping":
                if client_name and client_name in clients:
                    clients[client_name]["last_seen"] = datetime.now()
        
        except Exception as e:
            log.error(f"Error: {e}")
    
    if client_name and client_name in clients:
        clients[client_name]["connected"] = False
        log.info(f"✗ {client_name} disconnected")

async def h_status(request):
    return web.json_response({"status": "ok", "clients": list(clients.keys())})

async def h_scripts(request):
    key = request.headers.get("X-Admin-Key", "")
    if key != SECRET_KEY:
        return web.json_response({"error": "Unauthorized"}, status=401)
    scripts = [{"name": name, "connected": c.get("connected")} for name, c in clients.items()]
    return web.json_response({"scripts": scripts, "history": command_history[-20:]})

async def h_send(request):
    key = request.headers.get("X-Admin-Key", "")
    if key != SECRET_KEY:
        return web.json_response({"error": "Unauthorized"}, status=401)
    data = await request.json()
    script_name = data.get("script", "")
    command = data.get("command", "")
    if script_name not in clients:
        return web.json_response({"error": "Script not found"}, status=404)
    ws = clients[script_name].get("ws")
    if not ws:
        return web.json_response({"error": "Not connected"}, status=503)
    try:
        await ws.send_json({"type": "command", "command": command, "reply_chat_id": None})
        return web.json_response({"status": "sent"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def h_broadcast(request):
    key = request.headers.get("X-Admin-Key", "")
    if key != SECRET_KEY:
        return web.json_response({"error": "Unauthorized"}, status=401)
    data = await request.json()
    command = data.get("command", "")
    count = 0
    for name, client in clients.items():
        ws = client.get("ws")
        if ws and client.get("connected"):
            try:
                await ws.send_json({"type": "command", "command": command, "reply_chat_id": None})
                count += 1
            except:
                pass
    return web.json_response({"sent": count})

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🖥 Remote Control\n/send <script> <cmd>\n/scripts\n/panel")

async def cmd_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Admin only")
        return
    args = " ".join(context.args).split(None, 1)
    if len(args) < 2:
        await update.message.reply_text("Usage: /send <script> <cmd>")
        return
    script_name, command = args[0], args[1]
    if script_name not in clients or not clients[script_name].get("connected"):
        await update.message.reply_text(f"Script '{script_name}' offline")
        return
    try:
        await clients[script_name]["ws"].send_json({"type": "command", "command": command, "reply_chat_id": update.effective_chat.id})
        await update.message.reply_text("✓ Sent")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def cmd_scripts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not clients:
        await update.message.reply_text("No scripts connected")
        return
    msg = "Scripts:\n"
    for name, c in clients.items():
        msg += f"• {name} {'🟢' if c.get('connected') else '🔴'}\n"
    await update.message.reply_text(msg)

async def cmd_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Admin only")
        return
    await update.message.reply_text(f"Admin Panel:\n{PUBLIC_URL}/admin")

async def main():
    if BOT_TOKEN:
        app = Application.builder().token(BOT_TOKEN).build()
        app.add_handler(CommandHandler("start", cmd_start))
        app.add_handler(CommandHandler("send", cmd_send))
        app.add_handler(CommandHandler("scripts", cmd_scripts))
        app.add_handler(CommandHandler("panel", cmd_panel))
        asyncio.create_task(app.initialize())
        asyncio.create_task(app.start())
        asyncio.create_task(app.updater.start_polling())
        log.info("✓ Bot started")
    
    web_app = web.Application()
    web_app.router.add_get("/ws", ws_handler)
    web_app.router.add_get("/api/status", h_status)
    web_app.router.add_get("/api/scripts", h_scripts)
    web_app.router.add_post("/api/send", h_send)
    web_app.router.add_post("/api/broadcast", h_broadcast)
    
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info(f"✓ Server running on port {PORT}")
    
    try:
        await asyncio.Future()
    except KeyboardInterrupt:
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
