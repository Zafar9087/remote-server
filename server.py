"""
Remote Control Server — финальная версия
HTTP (aiohttp) + WebSocket + Telegram + REST API.
"""

import asyncio
import base64
import io
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

from aiohttp import web
from telegram import (
    ReplyKeyboardMarkup, ReplyKeyboardRemove, Update,
    InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo,
)
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    MessageHandler, CallbackQueryHandler, filters,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rc")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
SECRET_KEY = os.getenv("SECRET_KEY", "")  # НЕ хардкодить! Обязателен через переменную окружения
PUBLIC_URL = os.getenv("PUBLIC_URL", "https://remote-server-mr8v.onrender.com")
PORT = int(os.getenv("PORT", 8080))
UPDATE_VERSION = int(os.getenv("UPDATE_VERSION", "1"))
UPDATE_EXE_URL = os.getenv("UPDATE_EXE_URL", "")


class ClientRegistry:
    """Реестр клиентов. Активность = живой WebSocket."""

    def __init__(self):
        self.clients: dict[str, web.WebSocketResponse | None] = {}
        self.last_seen: dict[str, str] = {}
        self.history: list[dict] = []

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%H:%M:%S")

    @staticmethod
    def is_alive(ws: web.WebSocketResponse | None) -> bool:
        if ws is None:
            return False
        try:
            return not ws.closed
        except Exception:
            return False

    async def try_send(self, name: str, payload: dict, wait_secs: float = 5.0) -> bool:
        ws = self.clients.get(name)
        if self.is_alive(ws):
            try:
                await ws.send_json(payload)
                return True
            except Exception as e:
                log.warning(f"Send to '{name}' failed: {e}")

        deadline = asyncio.get_event_loop().time() + wait_secs
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.25)
            ws = self.clients.get(name)
            if self.is_alive(ws):
                try:
                    await ws.send_json(payload)
                    return True
                except Exception:
                    break
        return False

    def register(self, name: str, ws: web.WebSocketResponse) -> None:
        self.clients[name] = ws
        self.last_seen[name] = self._now()
        active_sessions[name] = {
            "pc_name": name,
            "ws": ws,
            "tg_user_id": active_sessions.get(name, {}).get("tg_user_id"),
            "connected": True,
            "connected_at": datetime.now().isoformat(timespec="seconds"),
            "last_seen": self.last_seen[name],
        }

    def disconnect(self, name: str, ws: web.WebSocketResponse) -> None:
        if self.clients.get(name) is ws:
            self.clients[name] = None
        if active_sessions.get(name, {}).get("ws") is ws:
            active_sessions[name]["ws"] = None
            active_sessions[name]["connected"] = False

    def touch(self, name: str) -> None:
        self.last_seen[name] = self._now()
        if name in active_sessions:
            active_sessions[name]["last_seen"] = self.last_seen[name]

    def add_result(self, script: str, command: str, result: str) -> None:
        short = result[:200] if len(result) < 1000 else "[Screenshot / Binary]"
        self.history.append({
            "time": self._now(),
            "script": script,
            "command": command[:80],
            "result": short,
        })
        if len(self.history) > 200:
            self.history.pop(0)

    def all_names(self) -> set[str]:
        return set(self.clients.keys()) | set(self.last_seen.keys())

    def online_names(self) -> list[str]:
        return [n for n, w in self.clients.items() if self.is_alive(w)]


registry = ClientRegistry()
tg_app: Application | None = None

USER_COMMANDS = {
    "📸 Screenshot":  "screenshot",
    "🔒 Lock screen": "lock",
    "📊 System info": "sysinfo",
    "🔋 Battery":     "battery",
    "🔄 Restart":     "restart",
    "⚡ Power off":   "shutdown",
}

def user_inline_keyboard(pc_name: str) -> InlineKeyboardMarkup:
    """Inline кнопки для пользователя — красиво и удобно."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📸 Screenshot",  callback_data=f"cmd:{pc_name}:screenshot"),
            InlineKeyboardButton("🔒 Lock",        callback_data=f"cmd:{pc_name}:lock"),
        ],
        [
            InlineKeyboardButton("📊 Sysinfo",     callback_data=f"cmd:{pc_name}:sysinfo"),
            InlineKeyboardButton("🔋 Battery",     callback_data=f"cmd:{pc_name}:battery"),
        ],
        [
            InlineKeyboardButton("🔄 Restart",     callback_data=f"cmd:{pc_name}:restart"),
            InlineKeyboardButton("⚡ Power off",   callback_data=f"cmd:{pc_name}:shutdown"),
        ],
        [
            InlineKeyboardButton("🖥 Status",      callback_data=f"cmd:{pc_name}:status"),
            InlineKeyboardButton("📋 Processes",   callback_data=f"cmd:{pc_name}:processes"),
        ],
    ])

def admin_pc_keyboard(pc_name: str) -> InlineKeyboardMarkup:
    """Inline кнопки для быстрых действий админа по конкретному ПК."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📸 Screenshot", callback_data=f"cmd:{pc_name}:screenshot"),
            InlineKeyboardButton("📊 Status",     callback_data=f"cmd:{pc_name}:status"),
        ],
        [
            InlineKeyboardButton("🔒 Lock",       callback_data=f"cmd:{pc_name}:lock"),
            InlineKeyboardButton("🔄 Restart",    callback_data=f"cmd:{pc_name}:restart"),
        ],
        [
            InlineKeyboardButton("🔌 Plugins",    callback_data=f"cmd:{pc_name}:plugins_list"),
            InlineKeyboardButton("⚡ Shutdown",   callback_data=f"cmd:{pc_name}:shutdown"),
        ],
    ])

# pc_name -> session metadata. The WebSocket itself is still owned by ClientRegistry.
active_sessions: dict[str, dict] = {}

# tg_user_id -> pc_name
user_bindings: dict[int, str] = {}


def do_bind(user_id: int, pc_name: str) -> tuple[bool, str]:
    """Admin-forced bind: link a Telegram user to a computer (add/replace)."""
    if pc_name not in registry.all_names():
        return False, f"Computer '{pc_name}' not found."

    prev_pc = user_bindings.get(user_id)
    if prev_pc and prev_pc != pc_name and active_sessions.get(prev_pc, {}).get("tg_user_id") == user_id:
        active_sessions[prev_pc]["tg_user_id"] = None

    user_bindings[user_id] = pc_name
    active_sessions.setdefault(pc_name, {
        "pc_name": pc_name,
        "ws": registry.clients.get(pc_name),
        "connected": registry.is_alive(registry.clients.get(pc_name)),
        "last_seen": registry.last_seen.get(pc_name, "never"),
    })
    active_sessions[pc_name]["tg_user_id"] = user_id
    return True, f"Bound user {user_id} to {pc_name}."


def do_unbind(target: str) -> tuple[bool, str]:
    """Admin-forced unbind. `target` can be a Telegram user id or a computer name."""
    target = target.strip()
    if target.isdigit():
        uid = int(target)
        pc = user_bindings.pop(uid, None)
        if pc and active_sessions.get(pc, {}).get("tg_user_id") == uid:
            active_sessions[pc]["tg_user_id"] = None
        if pc:
            return True, f"Unbound user {uid} from {pc}."
        return False, f"User {uid} was not bound to anything."
    else:
        pc = target
        uid = active_sessions.get(pc, {}).get("tg_user_id")
        if uid:
            user_bindings.pop(uid, None)
            active_sessions[pc]["tg_user_id"] = None
            return True, f"Unbound {pc} from user {uid}."
        return False, f"{pc} had no user bound."


def _extract_image_bytes(text: str) -> bytes | None:
    """Извлечь PNG/JPEG из ответа клиента (включая [SCREENSHOT_B64])."""
    raw = text.strip()

    # Формат клиента: [SCREENSHOT_B64]1920x1080\n<base64>
    m = re.match(r"^\[SCREENSHOT_B64\][^\n]*\n(.+)$", raw, re.DOTALL)
    if m:
        raw = m.group(1).strip()

    if raw.startswith("data:image"):
        raw = raw.split(",", 1)[-1].strip()

    # OK 1920x1080\n<link> — не картинка
    if raw.startswith("OK ") and "\nhttp" in raw:
        return None

    # Чистый base64 или с префиксом PNG/JPEG
    b64 = raw
    if not re.match(r"^[A-Za-z0-9+/=]+$", b64[:80].replace("\n", "")):
        # Попробуем найти base64 после первой строки
        lines = raw.split("\n")
        for line in reversed(lines):
            line = line.strip()
            if len(line) > 200 and re.match(r"^[A-Za-z0-9+/=]+$", line[:80]):
                b64 = line
                break
        else:
            return None

    try:
        data = base64.b64decode(b64, validate=False)
        if len(data) > 100 and data[:4] in (b"\x89PNG", b"\xff\xd8\xff", b"GIF8"):
            return data
    except Exception:
        pass
    return None


async def send_telegram(chat_id: int, text: str, client_name: str = "Script") -> None:
    if not tg_app or not chat_id:
        return
    try:
        img_bytes = _extract_image_bytes(text)
        if img_bytes:
            image_file = io.BytesIO(img_bytes)
            image_file.name = f"screenshot_{client_name}.png"
            await tg_app.bot.send_photo(
                chat_id=chat_id,
                photo=image_file,
                caption=f"Screenshot from {client_name}",
            )
            return

        chunks = [text[i : i + 4000] for i in range(0, len(text), 4000)]
        for chunk in chunks[:3]:
            await tg_app.bot.send_message(chat_id=chat_id, text=f"[{client_name}]\n{chunk}")
    except Exception as e:
        log.error(f"Telegram send failed: {e}")


async def notify_admins(text: str) -> None:
    if not tg_app:
        return
    for admin_id in ADMIN_IDS:
        try:
            await tg_app.bot.send_message(chat_id=admin_id, text=text)
        except Exception as e:
            log.warning(f"Admin notify failed for {admin_id}: {e}")


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
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

                if mtype == "register":
                    name = (data.get("name") or data.get("pc_name") or data.get("script_name") or "").strip()
                    if data.get("secret", "") != SECRET_KEY:
                        await ws.send_json({"type": "error", "msg": "Invalid secret key"})
                        await ws.close()
                        return ws
                    if not name:
                        await ws.send_json({"type": "error", "msg": "Empty client name"})
                        await ws.close()
                        return ws
                    client_name = name
                    registry.register(name, ws)
                    # Сохранить плагины клиента
                    plugins = data.get("plugins", [])
                    if name in active_sessions:
                        active_sessions[name]["plugins"] = plugins
                    log.info(f"+ {name} registered ({len(plugins)} plugins)")
                    await ws.send_json({"type": "ok"})

                elif mtype == "result":
                    cmd = data.get("command", "")
                    result = data.get("result", "")
                    chat_id = data.get("reply_chat_id")
                    registry.add_result(client_name or "?", cmd, result)
                    if chat_id:
                        await send_telegram(int(chat_id), result, client_name or "Script")

                elif mtype == "ping" and client_name:
                    registry.touch(client_name)
            except Exception as e:
                log.warning(f"msg parse error: {e}")
    except Exception as e:
        log.warning(f"ws error ({client_name}): {e}")
    finally:
        if client_name:
            registry.disconnect(client_name, ws)
    return ws


async def tg_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает нажатия inline кнопок (cmd:<pc>:<command>)."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    data = query.data or ""

    if not data.startswith("cmd:"):
        return

    _, pc_name, cmd = data.split(":", 2)

    # Проверка прав — только привязанный юзер или админ
    if user_id not in ADMIN_IDS:
        bound = user_bindings.get(user_id)
        if bound != pc_name:
            await query.answer("You don't have access to this computer.", show_alert=True)
            return

    ok = await registry.try_send(pc_name, {
        "type": "command",
        "command": cmd,
        "reply_chat_id": query.message.chat_id,
    })

    status = "✅ Sent" if ok else "❌ Offline"
    try:
        await query.edit_message_reply_markup(
            reply_markup=user_inline_keyboard(pc_name)
            if user_id not in ADMIN_IDS else admin_pc_keyboard(pc_name)
        )
    except Exception:
        pass

    if not ok:
        await ctx.bot.send_message(
            query.message.chat_id,
            f"❌ {pc_name} is offline right now."
        )


async def tg_start_v2(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id in ADMIN_IDS:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "🖥 Open Admin Panel",
                web_app=WebAppInfo(url=f"{PUBLIC_URL}/admin?key={SECRET_KEY}")
            )
        ]])
        await update.message.reply_text(
            "🔧 *Remote Control Admin*\n\n"
            "Commands:\n"
            "/scripts — computers online/offline\n"
            "/online · /offline — filter by status\n"
            "/users — bound Telegram users\n"
            "/bind <user\\_id> <pc> — link user to PC\n"
            "/unbind <user\\_id|pc> — remove link\n"
            "/plugins — installed plugins\n"
            "/send <pc> <cmd> — run command\n"
            "/broadcast <cmd> — send to all online\n"
            "/panel — admin panel link",
            parse_mode="Markdown",
            reply_markup=kb,
        )
        return

    await update.message.reply_text(
        "👋 Welcome to *Remote Control*\n\n"
        "Send your computer name to connect.",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )


async def tg_sessions(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Admin only")
        return

    if not active_sessions:
        await update.message.reply_text("No active sessions.")
        return

    lines = ["Active sessions", ""]
    for name in sorted(active_sessions):
        session = active_sessions[name]
        connected = "Online" if session.get("connected") else "Offline"
        user_id = session.get("tg_user_id") or "not bound"
        lines.append(
            f"{name}\n"
            f"Status: {connected}\n"
            f"User: {user_id}\n"
            f"Last seen: {session.get('last_seen', 'never')}\n"
        )
    await update.message.reply_text("\n".join(lines))


async def tg_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Admin only")
        return
    if not user_bindings:
        await update.message.reply_text("No users bound yet. Use /bind <user_id> <computer>.")
        return
    lines = ["Bound users", ""]
    for uid, pc in sorted(user_bindings.items()):
        status = "Online" if registry.is_alive(registry.clients.get(pc)) else "Offline"
        lines.append(f"{uid} → {pc} ({status})")
    await update.message.reply_text("\n".join(lines))


async def tg_bind(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Admin only")
        return
    if len(ctx.args) < 2:
        await update.message.reply_text("Usage: /bind <telegram_user_id> <computer>")
        return
    try:
        uid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("telegram_user_id must be a number.")
        return
    ok, msg = do_bind(uid, ctx.args[1])
    await update.message.reply_text(msg)


async def tg_unbind(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Admin only")
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /unbind <telegram_user_id | computer>")
        return
    ok, msg = do_unbind(ctx.args[0])
    await update.message.reply_text(msg)


async def tg_offline(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Admin only")
        return
    online = set(registry.online_names())
    offline = sorted(n for n in registry.all_names() if n not in online)
    if not offline:
        await update.message.reply_text("All computers are online.")
        return
    lines = [f"Offline ({len(offline)})", ""]
    for n in offline:
        lines.append(f"{n} — last seen {registry.last_seen.get(n, 'never')}")
    await update.message.reply_text("\n".join(lines))


async def tg_online(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Admin only")
        return
    online = sorted(registry.online_names())
    if not online:
        await update.message.reply_text("No computers online right now.")
        return
    lines = [f"Online ({len(online)})", ""]
    for n in online:
        uid = active_sessions.get(n, {}).get("tg_user_id")
        lines.append(f"{n}" + (f" (user {uid})" if uid else ""))
    await update.message.reply_text("\n".join(lines))


async def tg_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()

    if user_id in ADMIN_IDS:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "🖥 Open Admin Panel",
                web_app=WebAppInfo(url=f"{PUBLIC_URL}/admin?key={SECRET_KEY}")
            )
        ]])
        await update.message.reply_text(
            "Use /start to see all commands.",
            reply_markup=kb,
        )
        return

    bound_pc = user_bindings.get(user_id)
    if bound_pc:
        # Юзер уже привязан — показываем inline кнопки ещё раз
        is_online = registry.is_alive(registry.clients.get(bound_pc))
        status = "🟢 Online" if is_online else "🔴 Offline"
        await update.message.reply_text(
            f"🖥 *{bound_pc}* — {status}\nChoose an action:",
            parse_mode="Markdown",
            reply_markup=user_inline_keyboard(bound_pc),
        )
        return

    # Юзер не привязан — пробуем привязать по имени ПК
    pc_name = text
    if pc_name not in registry.all_names():
        await update.message.reply_text(
            "❌ Computer not found.\nCheck the name and try again."
        )
        return

    existing_user = active_sessions.get(pc_name, {}).get("tg_user_id")
    if existing_user and existing_user != user_id:
        await update.message.reply_text(
            "⚠️ This computer is already linked to another user."
        )
        return

    user_bindings[user_id] = pc_name
    active_sessions.setdefault(pc_name, {
        "pc_name": pc_name,
        "ws": registry.clients.get(pc_name),
        "connected": registry.is_alive(registry.clients.get(pc_name)),
        "last_seen": registry.last_seen.get(pc_name, "never"),
    })
    active_sessions[pc_name]["tg_user_id"] = user_id

    is_online = registry.is_alive(registry.clients.get(pc_name))
    status = "🟢 Online" if is_online else "🔴 Offline"
    await update.message.reply_text(
        f"✅ Connected to *{pc_name}*\n{status}",
        parse_mode="Markdown",
        reply_markup=user_inline_keyboard(pc_name),
    )
    await notify_admins(f"👤 User {user_id} connected to {pc_name}.")


async def tg_send(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Admin only")
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
    if name not in registry.clients and name not in registry.last_seen:
        await update.message.reply_text(f"Computer '{name}' was not found.")
        return
    ok = await registry.try_send(name, {
        "type": "command",
        "command": cmd,
        "reply_chat_id": update.effective_chat.id,
    })
    if ok:
        await update.message.reply_text(f"Sent to {name}: {cmd}")
    else:
        await update.message.reply_text(
            f"{name} is offline. Last seen: {registry.last_seen.get(name, 'never')}"
        )


async def tg_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Admin only")
        return
    cmd = " ".join(ctx.args)
    if not cmd:
        await update.message.reply_text("Usage: /broadcast <command>")
        return
    sent = []
    for name, ws in registry.clients.items():
        if registry.is_alive(ws):
            try:
                await ws.send_json({
                    "type": "command",
                    "command": cmd,
                    "reply_chat_id": update.effective_chat.id,
                })
                sent.append(name)
            except Exception:
                pass
    await update.message.reply_text(f"Broadcast sent to: {', '.join(sent) or 'none'}")


async def tg_scripts(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Admin only")
        return
    names = registry.all_names()
    if not names:
        await update.message.reply_text("No computers have connected yet.")
        return

    online  = sorted(registry.online_names())
    offline = sorted(n for n in names if n not in online)

    header = f"🖥 *Computers* — {len(online)} online / {len(offline)} offline"
    await update.message.reply_text(header, parse_mode="Markdown")

    # Для каждого онлайн ПК — отдельное сообщение с inline кнопками
    for n in online:
        uid = active_sessions.get(n, {}).get("tg_user_id")
        user_line = f"👤 User {uid}" if uid else "👤 No user"
        await update.message.reply_text(
            f"🟢 *{n}*\n{user_line}",
            parse_mode="Markdown",
            reply_markup=admin_pc_keyboard(n),
        )

    # Офлайн — просто списком
    if offline:
        lines = ["🔴 *Offline:*"] + [f"  • {n} — last seen {registry.last_seen.get(n,'never')}" for n in offline]
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def tg_plugins(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Admin only")
        return
    lines = ["Plugins by computer", ""]
    found_any = False
    for name in sorted(active_sessions):
        plugins = active_sessions[name].get("plugins", [])
        if not plugins:
            continue
        found_any = True
        status = "Online" if registry.is_alive(registry.clients.get(name)) else "Offline"
        lines.append(f"{name} ({status}):")
        for pl in plugins:
            lines.append(f"  • {pl['name']} v{pl.get('version','?')} — {pl.get('description','')}")
            cmds = pl.get("commands", [])
            if cmds:
                lines.append(f"    Commands: {', '.join(cmds)}")
        lines.append("")
    if not found_any:
        await update.message.reply_text("No clients have plugins installed.")
        return
    await update.message.reply_text("\n".join(lines))


async def tg_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Admin only")
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "🖥 Open Admin Panel",
            web_app=WebAppInfo(url=f"{PUBLIC_URL}/admin?key={SECRET_KEY}")
        )
    ]])
    await update.message.reply_text(
        "🔧 *Admin Panel*\nOpen the full web panel:",
        parse_mode="Markdown",
        reply_markup=kb,
    )


async def h_keepalive_ping(request: web.Request) -> web.Response:
    return web.Response(text="OK")


async def h_status(request: web.Request) -> web.Response:
    online = registry.online_names()
    return web.json_response({
        "status": "ok",
        "online": online,
        "offline": [n for n in registry.all_names() if n not in online],
    })


def _check_admin(request: web.Request) -> bool:
    return request.headers.get("X-Admin-Key", "") == SECRET_KEY


async def h_scripts(request: web.Request) -> web.Response:
    if not _check_admin(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    scripts = sorted([
        {
            "name": n,
            "connected": registry.is_alive(registry.clients.get(n)),
            "last_seen": registry.last_seen.get(n, "never"),
        }
        for n in registry.all_names()
    ], key=lambda x: (not x["connected"], x["name"]))
    return web.json_response({"scripts": scripts, "history": registry.history[-30:]})


async def h_users(request: web.Request) -> web.Response:
    if not _check_admin(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    users = [
        {
            "user_id": uid,
            "pc_name": pc,
            "connected": registry.is_alive(registry.clients.get(pc)),
        }
        for uid, pc in sorted(user_bindings.items())
    ]
    return web.json_response({"users": users})


async def h_bind(request: web.Request) -> web.Response:
    if not _check_admin(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    try:
        data = await request.json()
        uid = int(data.get("user_id"))
        pc_name = str(data.get("pc_name", "")).strip()
    except Exception:
        return web.json_response({"error": "Bad request — need user_id (int) and pc_name"}, status=400)
    ok, msg = do_bind(uid, pc_name)
    return web.json_response({"ok": ok, "message": msg}, status=200 if ok else 400)


async def h_plugins(request: web.Request) -> web.Response:
    if not _check_admin(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    result = []
    for name, session in active_sessions.items():
        plugins = session.get("plugins", [])
        if plugins:
            result.append({
                "pc_name": name,
                "connected": registry.is_alive(registry.clients.get(name)),
                "plugins": plugins,
            })
    return web.json_response({"clients_with_plugins": result})


async def h_unbind(request: web.Request) -> web.Response:
    if not _check_admin(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    try:
        data = await request.json()
        target = str(data.get("target", "")).strip()
    except Exception:
        return web.json_response({"error": "Bad request"}, status=400)
    if not target:
        return web.json_response({"error": "target required"}, status=400)
    ok, msg = do_unbind(target)
    return web.json_response({"ok": ok, "message": msg}, status=200 if ok else 400)


async def h_send(request: web.Request) -> web.Response:
    if not _check_admin(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Bad JSON"}, status=400)
    name = data.get("script", "").strip()
    cmd = data.get("command", "").strip()
    if not name or not cmd:
        return web.json_response({"error": "script and command required"}, status=400)
    ok = await registry.try_send(name, {
        "type": "command",
        "command": cmd,
        "reply_chat_id": data.get("reply_chat_id"),
    })
    return web.json_response({"status": "sent"} if ok else {"error": "not connected"}, status=200 if ok else 503)


async def h_broadcast(request: web.Request) -> web.Response:
    if not _check_admin(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    try:
        data = await request.json()
        cmd = data.get("command", "").strip()
    except Exception:
        return web.json_response({"error": "Bad JSON"}, status=400)
    if not cmd:
        return web.json_response({"error": "command required"}, status=400)
    sent = []
    for name, ws in registry.clients.items():
        if registry.is_alive(ws):
            try:
                await ws.send_json({"type": "command", "command": cmd})
                sent.append(name)
            except Exception:
                pass
    return web.json_response({"sent_to": sent, "count": len(sent)})


async def h_version(request: web.Request) -> web.Response:
    return web.json_response({"version": UPDATE_VERSION, "url": UPDATE_EXE_URL})


async def h_update(request: web.Request) -> web.Response:
    local_exe = Path(__file__).parent / "RemoteControl.exe"
    if local_exe.exists():
        return web.FileResponse(local_exe, headers={
            "Content-Disposition": "attachment; filename=RemoteControl.exe"
        })
    if UPDATE_EXE_URL:
        raise web.HTTPFound(UPDATE_EXE_URL)
    return web.json_response({"error": "No update file"}, status=404)


ADMIN_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Remote Control Admin</title>
<style>
 body{background:#0d1117;color:#e6edf3;font-family:"Segoe UI",sans-serif;padding:24px;max-width:900px;margin:auto}
 h1{font-size:20px} h2{font-size:14px;color:#8b949e;margin-top:26px;text-transform:uppercase;letter-spacing:.04em}
 #summary{color:#8b949e;margin-bottom:4px}
 .card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px 14px;margin-bottom:6px;font-size:14px}
 .online{border-left:3px solid #3fb950} .offline{border-left:3px solid #f85149}
 input{background:#161b22;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:7px 10px;margin-right:6px}
 button{background:#161b22;color:#58a6ff;border:1px solid #30363d;border-radius:6px;padding:7px 14px;cursor:pointer}
 button:hover{border-color:#58a6ff}
 .row{display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap}
 small{color:#8b949e}
 pre{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px;font-size:12px;white-space:pre-wrap}
</style></head>
<body>
<h1>Remote Control — Admin</h1>
<div id="summary"></div>

<h2>Online</h2><div id="online"></div>
<h2>Offline</h2><div id="offline"></div>

<h2>Users</h2><div id="users"></div>
<div class="row">
  <input id="bind-uid" placeholder="Telegram user id">
  <input id="bind-pc" placeholder="Computer name">
  <button onclick="doBind()">Bind</button>
  <button onclick="doUnbind()">Unbind</button>
</div>

<h2>Send command</h2>
<div class="row">
  <input id="cmd-pc" placeholder="Computer name">
  <input id="cmd-text" placeholder="command" style="width:260px">
  <button onclick="doSend()">Send</button>
</div>
<pre id="log">ready</pre>

<script>
const KEY = new URLSearchParams(location.search).get("key") || "";
const H = {"X-Admin-Key": KEY, "Content-Type": "application/json"};

async function refresh() {
  const r = await fetch("/api/scripts", {headers: H});
  const d = await r.json();
  if (d.error) { log(d); return; }
  const online = d.scripts.filter(s => s.connected);
  const offline = d.scripts.filter(s => !s.connected);
  document.getElementById("summary").innerText = online.length + " online / " + offline.length + " offline";
  document.getElementById("online").innerHTML = online.map(s =>
    '<div class="card online">' + s.name + '</div>').join("") || "<small>none</small>";
  document.getElementById("offline").innerHTML = offline.map(s =>
    '<div class="card offline">' + s.name + ' — last seen ' + s.last_seen + '</div>').join("") || "<small>none</small>";

  const ur = await fetch("/api/users", {headers: H});
  const ud = await ur.json();
  document.getElementById("users").innerHTML = (ud.users || []).map(u =>
    '<div class="card">user ' + u.user_id + ' &rarr; ' + u.pc_name + ' (' + (u.connected ? "online" : "offline") + ')</div>'
  ).join("") || "<small>none</small>";
}

async function doBind() {
  const uid = document.getElementById("bind-uid").value.trim();
  const pc = document.getElementById("bind-pc").value.trim();
  const r = await fetch("/api/bind", {method:"POST", headers:H, body: JSON.stringify({user_id: uid, pc_name: pc})});
  log(await r.json()); refresh();
}
async function doUnbind() {
  const pc = document.getElementById("bind-pc").value.trim();
  const uid = document.getElementById("bind-uid").value.trim();
  const r = await fetch("/api/unbind", {method:"POST", headers:H, body: JSON.stringify({target: pc || uid})});
  log(await r.json()); refresh();
}
async function doSend() {
  const pc = document.getElementById("cmd-pc").value.trim();
  const cmd = document.getElementById("cmd-text").value.trim();
  const r = await fetch("/api/send", {method:"POST", headers:H, body: JSON.stringify({script: pc, command: cmd})});
  log(await r.json());
}
function log(x){ document.getElementById("log").innerText = JSON.stringify(x, null, 2); }

refresh();
setInterval(refresh, 4000);
</script>
</body></html>"""


async def h_admin(request: web.Request) -> web.Response:
    # Сначала ищем admin_panel.html рядом с server_final.py (для Render — залейте туда)
    html_file = Path(__file__).parent / "admin_panel.html"
    if html_file.exists():
        return web.Response(text=html_file.read_text(encoding="utf-8"), content_type="text/html")
    # Запасной вариант — встроенный HTML
    return web.Response(text=ADMIN_HTML, content_type="text/html")


async def main() -> None:
    if not SECRET_KEY:
        log.error("FATAL: SECRET_KEY env var is empty — refusing to start (would accept any client).")
        raise SystemExit(1)

    global tg_app
    if BOT_TOKEN:
        tg_app = Application.builder().token(BOT_TOKEN).build()
        tg_app.add_handler(CommandHandler("start", tg_start_v2))
        tg_app.add_handler(CommandHandler("send", tg_send))
        tg_app.add_handler(CommandHandler("broadcast", tg_broadcast))
        tg_app.add_handler(CommandHandler("scripts", tg_scripts))
        tg_app.add_handler(CommandHandler("online", tg_online))
        tg_app.add_handler(CommandHandler("offline", tg_offline))
        tg_app.add_handler(CommandHandler("sessions", tg_sessions))
        tg_app.add_handler(CommandHandler("users", tg_users))
        tg_app.add_handler(CommandHandler("bind", tg_bind))
        tg_app.add_handler(CommandHandler("unbind", tg_unbind))
        tg_app.add_handler(CommandHandler("panel", tg_panel))
        tg_app.add_handler(CommandHandler("plugins", tg_plugins))
        tg_app.add_handler(CallbackQueryHandler(tg_callback))
        tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, tg_text))
        await tg_app.initialize()
        await tg_app.start()
        asyncio.create_task(tg_app.updater.start_polling())
        log.info("Telegram bot started")
    else:
        log.warning("BOT_TOKEN not set")

    app = web.Application()
    app.router.add_get("/", h_keepalive_ping)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/admin", h_admin)
    app.router.add_get("/api/status", h_status)
    app.router.add_get("/api/scripts", h_scripts)
    app.router.add_get("/api/users", h_users)
    app.router.add_post("/api/bind", h_bind)
    app.router.add_post("/api/unbind", h_unbind)
    app.router.add_post("/api/send", h_send)
    app.router.add_post("/api/broadcast", h_broadcast)
    app.router.add_get("/version", h_version)
    app.router.add_get("/update", h_update)
    app.router.add_get("/api/plugins", h_plugins)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info(f"Server on :{PORT}  WS: {PUBLIC_URL.replace('http', 'ws')}/ws")
    try:
        await asyncio.Future()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
