"""
Central Server — connects Telegram bot ↔ worker clients
Deploy on Railway / Render / any cloud host
"""

import asyncio
import json
import os
import logging
from datetime import datetime
from typing import Optional

import aiohttp
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────
# CONFIG  (set as environment variables on your host)
# ─────────────────────────────────────────────────────────
BOT_TOKEN    = os.environ["BOT_TOKEN"]          # Telegram bot token
ADMIN_IDS    = set(map(int, os.environ.get("ADMIN_IDS", "").split(",")))  # comma-separated Telegram user IDs
SECRET_KEY   = os.environ.get("SECRET_KEY", "changeme")  # shared secret between server ↔ clients
PANEL_PORT   = int(os.environ.get("PORT", 8080))

# ─────────────────────────────────────────────────────────
# IN-MEMORY STATE
# ─────────────────────────────────────────────────────────
# clients: { script_name -> { ws, last_seen, pending_commands:[] } }
clients: dict[str, dict] = {}

# command history: list of { time, from (telegram|admin), script, command, result }
history: list[dict] = []

# pending intercepts set by admin: { script_name -> intercept_command }
intercepts: dict[str, str] = {}


def now_str():
    return datetime.utcnow().strftime("%H:%M:%S UTC")


def add_history(from_: str, script: str, command: str, result: str = "pending"):
    history.append({"time": now_str(), "from": from_, "script": script,
                    "command": command, "result": result})
    if len(history) > 200:
        history.pop(0)


# ─────────────────────────────────────────────────────────
# WEBSOCKET ENDPOINT  (clients connect here)
# ─────────────────────────────────────────────────────────
async def ws_handler(request: web.Request):
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    script_name: Optional[str] = None

    async for msg in ws:
        if msg.type == aiohttp.WSMsgType.TEXT:
            try:
                data = json.loads(msg.data)
            except Exception:
                continue

            kind = data.get("type")

            # ── 1. registration ────────────────────────────────────
            if kind == "register":
                if data.get("secret") != SECRET_KEY:
                    await ws.send_json({"type": "error", "msg": "bad secret"})
                    await ws.close()
                    return ws

                script_name = data["name"]
                clients[script_name] = {"ws": ws, "last_seen": now_str(), "pending": []}
                log.info(f"[+] {script_name} connected")
                await ws.send_json({"type": "ok", "msg": f"Registered as {script_name}"})

            # ── 2. result from a command ───────────────────────────
            elif kind == "result":
                cmd     = data.get("command", "")
                result  = data.get("result", "")
                log.info(f"[result] {script_name}: {cmd!r} → {result!r}")
                add_history("client", script_name, cmd, result)

                # Forward result to Telegram if there's a chat_id stored
                if "reply_chat_id" in data:
                    await send_telegram_message(
                        data["reply_chat_id"],
                        f"✅ *{script_name}* finished `{cmd}`\n```\n{result}\n```"
                    )

            # ── 3. heartbeat ───────────────────────────────────────
            elif kind == "ping":
                if script_name and script_name in clients:
                    clients[script_name]["last_seen"] = now_str()
                await ws.send_json({"type": "pong"})

        elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
            break

    if script_name and script_name in clients:
        del clients[script_name]
        log.info(f"[-] {script_name} disconnected")

    return ws


# ─────────────────────────────────────────────────────────
# HELPER: send a command to a specific client
# ─────────────────────────────────────────────────────────
async def send_command(script_name: str, command: str, reply_chat_id: int | None = None):
    if script_name not in clients:
        return False, "script not connected"
    ws = clients[script_name]["ws"]
    payload: dict = {"type": "command", "command": command}
    if reply_chat_id:
        payload["reply_chat_id"] = reply_chat_id
    await ws.send_json(payload)
    add_history("server", script_name, command)
    return True, "sent"


# ─────────────────────────────────────────────────────────
# TELEGRAM BOT
# ─────────────────────────────────────────────────────────
_bot_app: Application | None = None


async def send_telegram_message(chat_id: int, text: str):
    if _bot_app:
        await _bot_app.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")


def is_admin(update: Update) -> bool:
    return update.effective_user.id in ADMIN_IDS


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Not authorised.")
        return
    await update.message.reply_text(
        "🖥 *Command Centre*\n\n"
        "Commands:\n"
        "/scripts — list connected scripts\n"
        "/send — send a command to a script\n"
        "/intercept — queue a command for a specific script\n"
        "/history — last 10 commands\n"
        "/panel — web admin panel link",
        parse_mode="Markdown"
    )


async def cmd_scripts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    if not clients:
        await update.message.reply_text("No scripts connected.")
        return
    lines = ["📡 *Connected scripts:*"]
    for name, info in clients.items():
        inter = f"  ⚡ intercept: `{intercepts[name]}`" if name in intercepts else ""
        lines.append(f"• `{name}` — last seen {info['last_seen']}{inter}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_send(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    # Usage: /send script1 run_task
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text("Usage: `/send <script_name> <command>`", parse_mode="Markdown")
        return
    script_name, command = args[0], " ".join(args[1:])

    # Check for admin intercept
    if script_name in intercepts:
        command = intercepts.pop(script_name)
        await update.message.reply_text(
            f"ℹ️ Admin intercept active — sending `{command}` to `{script_name}` instead.",
            parse_mode="Markdown"
        )

    ok, msg = await send_command(script_name, command, update.effective_chat.id)
    if ok:
        await update.message.reply_text(f"📤 Sent `{command}` → `{script_name}`", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ {msg}")


async def cmd_intercept(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text("Usage: `/intercept <script_name> <command>`", parse_mode="Markdown")
        return
    script_name, command = args[0], " ".join(args[1:])
    intercepts[script_name] = command
    await update.message.reply_text(
        f"⚡ Next command for `{script_name}` will be replaced with:\n`{command}`",
        parse_mode="Markdown"
    )


async def cmd_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    if not history:
        await update.message.reply_text("No history yet.")
        return
    lines = ["📜 *Last commands:*"]
    for h in history[-10:][::-1]:
        lines.append(f"`{h['time']}` [{h['from']}→{h['script']}] `{h['command']}` → {h['result']}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    host = os.environ.get("PUBLIC_URL", f"http://localhost:{PANEL_PORT}")
    await update.message.reply_text(f"🌐 Admin panel: {host}/admin")


# ─────────────────────────────────────────────────────────
# WEB ADMIN PANEL  (HTML)
# ─────────────────────────────────────────────────────────
PANEL_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Admin Panel</title>
<style>
  :root{--bg:#0f1117;--card:#1a1d27;--border:#2a2d3e;--text:#e2e8f0;--muted:#64748b;--accent:#6366f1;--green:#22c55e;--red:#ef4444;--amber:#f59e0b}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:system-ui,sans-serif;padding:24px;min-height:100vh}
  h1{font-size:1.4rem;margin-bottom:20px;color:#fff}
  h2{font-size:1rem;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-bottom:10px}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px}
  .card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px}
  .dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--green);margin-right:6px}
  .dot.off{background:var(--red)}
  .badge{font-size:.7rem;padding:2px 8px;border-radius:20px;background:#6366f120;color:var(--accent);border:1px solid var(--accent)30}
  input,select{width:100%;padding:8px 12px;border-radius:6px;border:1px solid var(--border);background:#111;color:var(--text);font-size:.9rem;margin-bottom:8px}
  button{padding:8px 16px;border-radius:6px;border:none;background:var(--accent);color:#fff;cursor:pointer;font-size:.9rem;font-weight:600;width:100%}
  button:hover{opacity:.85}
  button.danger{background:var(--red)}
  .log{font-size:.78rem;font-family:monospace;color:var(--muted);max-height:220px;overflow-y:auto;display:flex;flex-direction:column-reverse}
  .log div{padding:3px 0;border-bottom:1px solid var(--border)10}
  .tag{color:var(--accent)}
  .tag.res{color:var(--green)}
  #toast{position:fixed;top:20px;right:20px;background:var(--green);color:#fff;padding:10px 20px;border-radius:8px;display:none;font-weight:600}
  @media(max-width:640px){.grid{grid-template-columns:1fr}}
</style>
</head>
<body>
<h1>🖥 Script Admin Panel</h1>
<div class="grid">
  <div class="card">
    <h2>Connected Scripts</h2>
    <div id="scripts-list"><span style="color:var(--muted)">Loading…</span></div>
  </div>
  <div class="card">
    <h2>Send Command</h2>
    <select id="target-script"><option value="">— select script —</option></select>
    <input id="cmd-input" placeholder="e.g. run_task" />
    <button onclick="sendCmd()">Send Command</button>
    <div style="margin-top:10px">
      <h2 style="margin-bottom:6px">Set Intercept</h2>
      <input id="intercept-cmd" placeholder="intercept command" />
      <button class="danger" onclick="setIntercept()">Set Intercept for Script</button>
    </div>
  </div>
</div>
<div class="card">
  <h2>Command History</h2>
  <div class="log" id="log"></div>
</div>
<div id="toast">✓ Done</div>
<script>
const KEY = localStorage.getItem('admin_key') || prompt('Admin secret key:');
localStorage.setItem('admin_key', KEY);

async function api(path, body=null){
  const r = await fetch(path, body ? {method:'POST',headers:{'Content-Type':'application/json','X-Admin-Key':KEY},body:JSON.stringify(body)} : {headers:{'X-Admin-Key':KEY}});
  return r.json();
}

function toast(){ const t=document.getElementById('toast'); t.style.display='block'; setTimeout(()=>t.style.display='none',2000); }

async function refresh(){
  const d = await api('/api/status');
  const sl = document.getElementById('scripts-list');
  const sel = document.getElementById('target-script');
  sel.innerHTML = '<option value="">— select script —</option>';
  if(!d.scripts || d.scripts.length===0){ sl.innerHTML='<span style="color:var(--muted)">None connected</span>'; return; }
  sl.innerHTML = d.scripts.map(s=>`<div style="padding:6px 0;border-bottom:1px solid var(--border)20">
    <span class="dot"></span><b>${s.name}</b> <span class="badge">last ${s.last_seen}</span>
    ${s.intercept?`<span style="color:var(--amber);font-size:.8rem"> ⚡ intercept: ${s.intercept}</span>`:''}
  </div>`).join('');
  d.scripts.forEach(s=>{ const o=document.createElement('option'); o.value=s.name; o.textContent=s.name; sel.appendChild(o); });
  const log = document.getElementById('log');
  log.innerHTML = (d.history||[]).slice(-40).reverse().map(h=>
    `<div>[<span class="tag">${h.time}</span>] <span class="tag">${h.from}→${h.script}</span> <b>${h.command}</b> <span class="tag res">${h.result}</span></div>`
  ).join('');
}

async function sendCmd(){
  const script = document.getElementById('target-script').value;
  const cmd = document.getElementById('cmd-input').value.trim();
  if(!script||!cmd) return alert('Select a script and enter a command.');
  await api('/api/send', {script, command: cmd});
  document.getElementById('cmd-input').value='';
  toast(); refresh();
}

async function setIntercept(){
  const script = document.getElementById('target-script').value;
  const cmd = document.getElementById('intercept-cmd').value.trim();
  if(!script||!cmd) return alert('Select a script and enter an intercept command.');
  await api('/api/intercept', {script, command: cmd});
  document.getElementById('intercept-cmd').value='';
  toast(); refresh();
}

refresh(); setInterval(refresh, 3000);
</script>
</body>
</html>
"""


def check_admin_key(request: web.Request) -> bool:
    return request.headers.get("X-Admin-Key") == SECRET_KEY


async def handle_admin_panel(request: web.Request):
    return web.Response(text=PANEL_HTML, content_type="text/html")


async def handle_api_status(request: web.Request):
    if not check_admin_key(request):
        return web.Response(status=403, text="Forbidden")
    scripts_out = []
    for name, info in clients.items():
        scripts_out.append({
            "name": name,
            "last_seen": info["last_seen"],
            "intercept": intercepts.get(name),
        })
    return web.json_response({"scripts": scripts_out, "history": history})


async def handle_api_send(request: web.Request):
    if not check_admin_key(request):
        return web.Response(status=403, text="Forbidden")
    data = await request.json()
    script, command = data.get("script"), data.get("command")
    # Check intercept
    if script in intercepts:
        command = intercepts.pop(script)
    ok, msg = await send_command(script, command)
    return web.json_response({"ok": ok, "msg": msg})


async def handle_api_intercept(request: web.Request):
    if not check_admin_key(request):
        return web.Response(status=403, text="Forbidden")
    data = await request.json()
    script, command = data["script"], data["command"]
    intercepts[script] = command
    return web.json_response({"ok": True})


# ─────────────────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────────────────
async def main():
    global _bot_app

    # Build Telegram bot
    _bot_app = Application.builder().token(BOT_TOKEN).build()
    _bot_app.add_handler(CommandHandler("start",     cmd_start))
    _bot_app.add_handler(CommandHandler("scripts",   cmd_scripts))
    _bot_app.add_handler(CommandHandler("send",      cmd_send))
    _bot_app.add_handler(CommandHandler("intercept", cmd_intercept))
    _bot_app.add_handler(CommandHandler("history",   cmd_history))
    _bot_app.add_handler(CommandHandler("panel",     cmd_panel))

    await _bot_app.initialize()
    await _bot_app.start()

    # Poll for Telegram updates in background
    async def poll():
        offset = None
        while True:
            try:
                updates = await _bot_app.bot.get_updates(offset=offset, timeout=10, allowed_updates=["message"])
                for u in updates:
                    offset = u.update_id + 1
                    await _bot_app.process_update(Update.de_json(u.to_dict(), _bot_app.bot))
            except Exception as e:
                log.warning(f"Telegram poll error: {e}")
            await asyncio.sleep(1)

    asyncio.create_task(poll())

    # Web server
    app_web = web.Application()
    app_web.router.add_get("/ws",            ws_handler)
    app_web.router.add_get("/admin",         handle_admin_panel)
    app_web.router.add_get("/api/status",    handle_api_status)
    app_web.router.add_post("/api/send",     handle_api_send)
    app_web.router.add_post("/api/intercept",handle_api_intercept)

    runner = web.AppRunner(app_web)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PANEL_PORT)
    await site.start()

    log.info(f"Server running on port {PANEL_PORT}")
    await asyncio.Event().wait()   # run forever


if __name__ == "__main__":
    asyncio.run(main())
