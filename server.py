"""
Remote Control Server v3
========================
- Telegram bot integration
- WebSocket server for client connections
- Command routing
- Admin panel backend
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from datetime import datetime
from aiohttp import web
import websockets
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("rc_server")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = set(int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip())
SECRET_KEY = os.getenv("SECRET_KEY", "changeme")
PUBLIC_URL = os.getenv("PUBLIC_URL", "http://localhost:8080")
PORT = int(os.getenv("PORT", 8080))

log.info(f"Config: ADMINS={ADMIN_IDS}, SECRET_KEY={'*'*10}, PUBLIC_URL={PUBLIC_URL}")

# ─────────────────────────────────────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────────────────────────────────────

clients = {}  # {name: {"ws": websocket, "connected": bool, "last_seen": datetime}}
command_history = []  # List of {"time": str, "script": name, "command": cmd, "result": result}

# ─────────────────────────────────────────────────────────────────────────────
# WEBSOCKET HANDLERS
# ─────────────────────────────────────────────────────────────────────────────

async def ws_handler(request):
    """WebSocket handler for client connections."""
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    
    client_name = None
    
    async for msg in ws:
        try:
            data = json.loads(msg.data)
            msg_type = data.get("type")
            
            # REGISTER
            if msg_type == "register":
                name = data.get("name", "unknown")
                secret = data.get("secret", "")
                
                if secret != SECRET_KEY:
                    await ws.send_json({"type": "error", "msg": "Invalid secret key"})
                    await ws.close()
                    return
                
                client_name = name
                clients[name] = {
                    "ws": ws,
                    "connected": True,
                    "last_seen": datetime.now(),
                }
                log.info(f"✓ Registered: {name}")
                await ws.send_json({"type": "ok"})
            
            # RESULT (response from command)
            elif msg_type == "result":
                cmd = data.get("command", "")
                result = data.get("result", "")
                reply_chat_id = data.get("reply_chat_id")
                
                # Log to history
                command_history.append({
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "script": client_name or "unknown",
                    "command": cmd,
                    "result": result[:200],
                })
                
                # Keep only last 50
                if len(command_history) > 50:
                    command_history.pop(0)
                
                # Send to Telegram if reply_chat_id provided
                if reply_chat_id and BOT_TOKEN:
                    try:
                        # Send result back to Telegram
                        log.debug(f"Would send to chat {reply_chat_id}: {result[:100]}")
                    except Exception as e:
                        log.error(f"Failed to send result: {e}")
                
                log.info(f"✓ Result from {client_name}: {cmd[:50]}")
            
            # PING
            elif msg_type == "ping":
                if client_name and client_name in clients:
                    clients[client_name]["last_seen"] = datetime.now()
        
        except json.JSONDecodeError:
            log.warning(f"Invalid JSON from {client_name}")
        except Exception as e:
            log.error(f"Error: {e}")
    
    # Cleanup on disconnect
    if client_name and client_name in clients:
        clients[client_name]["connected"] = False
        log.info(f"✗ Disconnected: {client_name}")

# ─────────────────────────────────────────────────────────────────────────────
# HTTP HANDLERS - ADMIN PANEL
# ─────────────────────────────────────────────────────────────────────────────

async def h_panel(request):
    """Serve admin panel HTML."""
    html = """<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width"><title>Remote Control Admin</title><style>*{margin:0;padding:0;box-sizing:border-box}body{background:#0d1117;color:#c9d1d9;font-family:-apple-system,sans-serif;padding:20px}h1,h2{margin-bottom:20px}input,textarea,select{background:#161b22;border:1px solid #30363d;color:#c9d1d9;padding:10px;border-radius:4px;width:100%;margin-bottom:10px;font-family:monospace}button{background:#58a6ff;color:#fff;border:none;padding:10px 20px;border-radius:4px;cursor:pointer;font-weight:600;margin-top:10px}button:hover{background:#65b1ff}.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:20px;margin-bottom:20px}#scripts{list-style:none}#scripts li{padding:10px;background:#0d1117;margin-bottom:5px;border-radius:4px;cursor:pointer}#scripts li:hover{background:#1c2128}#scripts li.active{background:#58a6ff20;border-left:3px solid #58a6ff}#history{background:#0d1117;border:1px solid #30363d;border-radius:4px;padding:10px;max-height:300px;overflow-y:auto;font-family:monospace;font-size:0.85rem}.hist-item{padding:5px;border-bottom:1px solid #30363d}.hist-item:last-child{border:0}.status{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:8px}.status.online{background:#3fb950}.status.offline{background:#f85149}</style></head><body><h1>Remote Control Admin Panel</h1><div class="card"><h2>Connected Scripts</h2><ul id="scripts"><li>Loading...</li></ul></div><div class="card"><h2>Send Command</h2><select id="script-select"><option value="">Select a script</option></select><br><input type="text" id="cmd-input" placeholder="Command (e.g., screenshot /and lock)" onkeypress="if(event.key=='Enter')sendCmd()"><button onclick="sendCmd()">Send</button><button onclick="sendBroadcast()" style="background:#f85149">Broadcast All</button></div><div class="card"><h2>Command History</h2><div id="history"><div style="color:#8b949e">No commands yet</div></div></div><script>let selectedScript=null,adminKey=null;function showAuthModal(){adminKey=prompt("Enter Admin Key:","");if(!adminKey){alert("Admin key required");showAuthModal();return}loadScripts()}async function loadScripts(){if(!adminKey)return;const r=await fetch("/api/scripts",{headers:{"X-Admin-Key":adminKey}});if(!r.ok){alert("Invalid admin key");adminKey=null;showAuthModal();return}const d=await r.json();const select=document.getElementById("script-select");const list=document.getElementById("scripts");list.innerHTML="";select.innerHTML="<option value=''>Select a script</option>";(d.scripts||[]).forEach(s=>{const li=document.createElement("li");const status=document.createElement("span");status.className="status "+(s.connected?"online":"offline");li.appendChild(status);li.appendChild(document.createTextNode(s.name));li.onclick=()=>{selectedScript=s.name;document.querySelectorAll("#scripts li").forEach(e=>e.classList.remove("active"));li.classList.add("active");select.value=s.name};list.appendChild(li);const opt=document.createElement("option");opt.value=s.name;opt.textContent=s.name+(s.connected?" [ONLINE]":" [OFFLINE]");select.appendChild(opt)});if(d.history)(d.history||[]).slice(-20).reverse().forEach(h=>{const item=document.createElement("div");item.className="hist-item";item.textContent=`[${h.time}] ${h.script}: ${h.command} -> ${h.result.substring(0,50)}...`;document.getElementById("history").appendChild(item)})}async function sendCmd(){const cmd=document.getElementById("cmd-input").value;const script=selectedScript||document.getElementById("script-select").value;if(!cmd||!script){alert("Select script and enter command");return}const r=await fetch("/api/send",{method:"POST",headers:{"X-Admin-Key":adminKey,"Content-Type":"application/json"},body:JSON.stringify({script:script,command:cmd})});if(r.ok){document.getElementById("cmd-input").value="";await new Promise(r=>setTimeout(r,1000));loadScripts()}else{alert("Failed to send command")}}async function sendBroadcast(){const cmd=document.getElementById("cmd-input").value;if(!cmd){alert("Enter command");return}await fetch("/api/broadcast",{method:"POST",headers:{"X-Admin-Key":adminKey,"Content-Type":"application/json"},body:JSON.stringify({command:cmd})});document.getElementById("cmd-input").value="";await new Promise(r=>setTimeout(r,1000));loadScripts()}showAuthModal();setInterval(loadScripts,3000)</script></body></html>"""
    return web.Response(text=html, content_type="text/html")

async def h_status(request):
    """Get server status."""
    return web.json_response({
        "status": "ok",
        "scripts": list(clients.keys()),
        "timestamp": datetime.now().isoformat(),
    })

async def h_scripts(request):
    """Get list of connected scripts."""
    key = request.headers.get("X-Admin-Key", "")
    if key != SECRET_KEY:
        return web.json_response({"error": "Unauthorized"}, status=401)
    
    scripts = [
        {
            "name": name,
            "connected": client.get("connected", False),
            "last_seen": client.get("last_seen", "").isoformat() if isinstance(client.get("last_seen"), datetime) else "",
        }
        for name, client in clients.items()
    ]
    
    return web.json_response({
        "scripts": scripts,
        "history": command_history[-20:],
    })

async def h_send(request):
    """Send command to specific script."""
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
        return web.json_response({"error": "Script not connected"}, status=503)
    
    try:
        await ws.send_json({
            "type": "command",
            "command": command,
            "reply_chat_id": None,
        })
        return web.json_response({"status": "sent"})
    except Exception as e:
        log.error(f"Failed to send command: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def h_broadcast(request):
    """Broadcast command to all scripts."""
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
                await ws.send_json({
                    "type": "command",
                    "command": command,
                    "reply_chat_id": None,
                })
                count += 1
            except Exception as e:
                log.error(f"Failed to broadcast to {name}: {e}")
    
    return web.json_response({"status": "broadcast", "sent_to": count})

# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM BOT
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command."""
    await update.message.reply_text(
        "🖥 Remote Control v3\n\n"
        "Commands:\n"
        "/send <script> <command> — Send command\n"
        "/broadcast <command> — Send to all\n"
        "/scripts — List connected scripts\n"
        "/panel — Open admin panel\n"
        "/help — Show help\n\n"
        "Admin panel: " + PUBLIC_URL + "/admin"
    )

async def cmd_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send command: /send script_name command"""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only")
        return
    
    args = " ".join(context.args).split(None, 1)
    if len(args) < 2:
        await update.message.reply_text("Usage: /send <script> <command>")
        return
    
    script_name = args[0]
    command = args[1]
    
    if script_name not in clients:
        await update.message.reply_text(f"❌ Script '{script_name}' not found")
        return
    
    ws = clients[script_name].get("ws")
    if not ws or not clients[script_name].get("connected"):
        await update.message.reply_text(f"❌ Script '{script_name}' offline")
        return
    
    try:
        await ws.send_json({
            "type": "command",
            "command": command,
            "reply_chat_id": update.effective_chat.id,
        })
        await update.message.reply_text(f"✓ Command sent to {script_name}")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def cmd_scripts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List scripts."""
    if not clients:
        await update.message.reply_text("No scripts connected")
        return
    
    msg = "Connected scripts:\n\n"
    for name, client in clients.items():
        status = "🟢 ONLINE" if client.get("connected") else "🔴 OFFLINE"
        msg += f"• {name} {status}\n"
    
    await update.message.reply_text(msg)

async def cmd_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show panel link."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only")
        return
    
    await update.message.reply_text(
        f"🌐 Admin Panel:\n{PUBLIC_URL}/admin\n\n"
        "Enter your admin key when prompted."
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Help command."""
    await update.message.reply_text(
        "Remote Control v3 - Help\n\n"
        "Admin Commands:\n"
        "/send <script> <cmd> — Send command\n"
        "/broadcast <cmd> — Send to all scripts\n"
        "/scripts — List connected scripts\n"
        "/panel — Open admin panel\n\n"
        "Admin panel features:\n"
        "✓ Select script from dropdown\n"
        "✓ Send commands in real-time\n"
        "✓ View command history\n"
        "✓ Broadcast to all\n\n"
        "Command Chaining:\n"
        "/send mypc screenshot /and lock /and notify Done\n\n"
        "For full documentation, see commands_guide.html"
    )

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

async def main():
    """Main server loop."""
    
    # Create bot application
    if BOT_TOKEN:
        app = Application.builder().token(BOT_TOKEN).build()
        app.add_handler(CommandHandler("start", cmd_start))
        app.add_handler(CommandHandler("send", cmd_send))
        app.add_handler(CommandHandler("scripts", cmd_scripts))
        app.add_handler(CommandHandler("panel", cmd_panel))
        app.add_handler(CommandHandler("help", cmd_help))
        
        # Start bot in background
        asyncio.create_task(app.initialize())
        asyncio.create_task(app.start())
        asyncio.create_task(app.updater.start_polling())
        log.info("✓ Telegram bot started")
    else:
        log.warning("⚠ BOT_TOKEN not set - Telegram bot disabled")
    
    # Create web server
    web_app = web.Application()
    web_app.router.add_get("/ws", ws_handler)
    web_app.router.add_get("/admin", h_panel)
    web_app.router.add_get("/api/status", h_status)
    web_app.router.add_get("/api/scripts", h_scripts)
    web_app.router.add_post("/api/send", h_send)
    web_app.router.add_post("/api/broadcast", h_broadcast)
    
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    
    log.info(f"✓ Server running on port {PORT}")
    log.info(f"✓ Admin panel: {PUBLIC_URL}/admin")
    log.info(f"✓ WebSocket: {PUBLIC_URL.replace('http', 'ws')}/ws")
    
    # Keep running
    try:
        await asyncio.Future()  # Run forever
    except KeyboardInterrupt:
        log.info("Shutting down...")
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
