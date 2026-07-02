"""
Remote Control Server — финальная версия
=========================================
HTTP (aiohttp) + WebSocket для стабильной связи с клиентами.
Telegram-бот и REST API для удалённого управления.
"""

import asyncio
import base64
import io
import json
import logging
import os
from datetime import datetime
from pathlib import Path

from aiohttp import web
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rc")

# ── Конфигурация из переменных окружения ──
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
SECRET_KEY = os.getenv("SECRET_KEY", "changeme")
PUBLIC_URL = os.getenv("PUBLIC_URL", "http://localhost:8080")
PORT = int(os.getenv("PORT", 8080))
UPDATE_VERSION = int(os.getenv("UPDATE_VERSION", "1"))
UPDATE_EXE_URL = os.getenv("UPDATE_EXE_URL", "")


class ClientRegistry:
    """Реестр подключённых клиентов. Истина — живой WebSocket-объект."""

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
        """Отправка команды с ожиданием переподключения клиента."""
        ws = self.clients.get(name)

        if self.is_alive(ws):
            try:
                await ws.send_json(payload)
                return True
            except Exception as e:
                log.warning(f"Send to '{name}' failed mid-flight: {e}")

        log.info(f"Waiting {wait_secs}s for '{name}' to reconnect...")
        deadline = asyncio.get_event_loop().time() + wait_secs
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.25)
            ws = self.clients.get(name)
            if self.is_alive(ws):
                try:
                    await ws.send_json(payload)
                    log.info(f"'{name}' reconnected — command delivered")
                    return True
                except Exception as e:
                    log.warning(f"Send after reconnect failed: {e}")
                    break
        return False

    def register(self, name: str, ws: web.WebSocketResponse) -> None:
        self.clients[name] = ws
        self.last_seen[name] = self._now()
        log.info(f"+ {name} registered")

    def disconnect(self, name: str, ws: web.WebSocketResponse) -> None:
        if self.clients.get(name) is ws:
            self.clients[name] = None
            log.info(f"- {name} disconnected")

    def touch(self, name: str) -> None:
        self.last_seen[name] = self._now()

    def add_result(self, script: str, command: str, result: str) -> None:
        history_res = result[:200] if len(result) < 1000 else "[Binary Data / Screenshot]"
        self.history.append({
            "time": self._now(),
            "script": script,
            "command": command[:80],
            "result": history_res,
        })
        if len(self.history) > 200:
            self.history.pop(0)

    def all_names(self) -> set[str]:
        return set(self.clients.keys()) | set(self.last_seen.keys())

    def online_names(self) -> list[str]:
        return [n for n, w in self.clients.items() if self.is_alive(w)]


registry = ClientRegistry()
tg_app: Application | None = None


async def send_telegram(chat_id: int, text: str, client_name: str = "Script") -> None:
    """Отправка текста или скриншота (base64) в Telegram."""
    if not tg_app or not chat_id:
        return
    try:
        clean_text = text.strip()
        if clean_text.startswith("data:image"):
            clean_text = clean_text.split(",", 1)[-1]

        if len(clean_text) > 1000 and (
            clean_text.startswith("/9j/")
            or clean_text.startswith("iVBORw")
            or clean_text.startswith("/tGcD")
        ):
            try:
                image_bytes = base64.b64decode(clean_text)
                image_file = io.BytesIO(image_bytes)
                image_file.name = f"screenshot_{client_name}.png"
                await tg_app.bot.send_photo(
                    chat_id=chat_id,
                    photo=image_file,
                    caption=f"📸 Скриншот от [{client_name}]",
                )
                return
            except Exception as e:
                log.warning(f"Failed to decode base64 as image, falling back to text: {e}")

        chunks = [text[i : i + 4000] for i in range(0, len(text), 4000)]
        for chunk in chunks[:3]:
            await tg_app.bot.send_message(chat_id=chat_id, text=f"[{client_name}]\n{chunk}")
    except Exception as e:
        log.error(f"Telegram send failed: {e}")


# =============================================================================
#  WebSocket
# =============================================================================
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
                    name = data.get("name", "").strip()
                    secret = data.get("secret", "")
                    if secret != SECRET_KEY:
                        await ws.send_json({"type": "error", "msg": "Invalid secret key"})
                        await ws.close()
                        return ws
                    client_name = name
                    registry.register(name, ws)
                    await ws.send_json({"type": "ok"})

                elif mtype == "result":
                    cmd = data.get("command", "")
                    result = data.get("result", "")
                    chat_id = data.get("reply_chat_id")
                    registry.add_result(client_name or "?", cmd, result)
                    log.info(f"  result ← {client_name}: {cmd[:50]}")
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


# =============================================================================
#  Telegram-бот
# =============================================================================
async def tg_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id in ADMIN_IDS:
        await update.message.reply_text(
            "🖥 Remote Control — Admin\n\n"
            "/send <script> <cmd> — send command\n"
            "/broadcast <cmd>     — send to all\n"
            "/scripts             — list scripts"
        )
    else:
        await update.message.reply_text("🖥 Remote Control")


async def tg_send(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
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
    if name not in registry.clients and name not in registry.last_seen:
        await update.message.reply_text(f"❌ Script '{name}' not found.")
        return

    payload = {
        "type": "command",
        "command": cmd,
        "reply_chat_id": update.effective_chat.id,
    }
    ok = await registry.try_send(name, payload)
    if ok:
        await update.message.reply_text(f"✅ Sent to {name}")
    else:
        await update.message.reply_text(
            f"❌ '{name}' is not connected.\n"
            f"Last seen: {registry.last_seen.get(name, 'never')}"
        )


async def tg_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only")
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
            except Exception as e:
                log.warning(f"broadcast to {name}: {e}")

    await update.message.reply_text(
        f"📡 Broadcast to {len(sent)}: {', '.join(sent) or 'none'}"
    )


async def tg_scripts(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only")
        return
    all_names = registry.all_names()
    if not all_names:
        await update.message.reply_text("No scripts have ever connected.")
        return
    msg = "📋 Scripts:\n\n"
    for name in sorted(all_names):
        ws = registry.clients.get(name)
        icon = "🟢" if registry.is_alive(ws) else "🔴"
        seen = registry.last_seen.get(name, "never")
        msg += f"{icon} {name}  (last seen: {seen})\n"
    await update.message.reply_text(msg)


async def tg_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only")
        return
    await update.message.reply_text(f"🌐 Admin Panel:\n{PUBLIC_URL}/admin\nKey: {SECRET_KEY}")


# =============================================================================
#  HTTP API
# =============================================================================
async def h_keepalive_ping(request: web.Request) -> web.Response:
    """Эндпоинт для UptimeRobot — сервер не засыпает."""
    return web.Response(text="Я живой!")


async def h_status(request: web.Request) -> web.Response:
    online = registry.online_names()
    offline = [n for n in registry.all_names() if n not in online]
    return web.json_response({"status": "ok", "online": online, "offline": offline})


async def h_scripts(request: web.Request) -> web.Response:
    if request.headers.get("X-Admin-Key", "") != SECRET_KEY:
        return web.json_response({"error": "Unauthorized"}, status=401)
    scripts = [
        {
            "name": n,
            "connected": registry.is_alive(registry.clients.get(n)),
            "last_seen": registry.last_seen.get(n, "never"),
        }
        for n in sorted(registry.all_names())
    ]
    scripts.sort(key=lambda x: (not x["connected"], x["name"]))
    return web.json_response({"scripts": scripts, "history": registry.history[-30:]})


async def h_send(request: web.Request) -> web.Response:
    if request.headers.get("X-Admin-Key", "") != SECRET_KEY:
        return web.json_response({"error": "Unauthorized"}, status=401)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Bad JSON"}, status=400)

    name = data.get("script", "").strip()
    cmd = data.get("command", "").strip()
    if not name or not cmd:
        return web.json_response({"error": "script and command required"}, status=400)

    payload = {"type": "command", "command": cmd, "reply_chat_id": data.get("reply_chat_id")}
    ok = await registry.try_send(name, payload)
    if ok:
        return web.json_response({"status": "sent"})
    return web.json_response({"error": f"'{name}' not connected"}, status=503)


async def h_broadcast(request: web.Request) -> web.Response:
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
    for name, ws in registry.clients.items():
        if registry.is_alive(ws):
            try:
                await ws.send_json({"type": "command", "command": cmd})
                sent.append(name)
            except Exception:
                pass
    return web.json_response({"sent_to": sent, "count": len(sent)})


async def h_version(request: web.Request) -> web.Response:
    """GET /version — клиенты проверяют наличие обновлений."""
    return web.json_response({"version": UPDATE_VERSION, "url": UPDATE_EXE_URL})


async def h_update(request: web.Request) -> web.Response:
    """GET /update — отдача актуального exe."""
    local_exe = Path(__file__).parent / "RemoteControl.exe"
    if local_exe.exists():
        return web.FileResponse(
            local_exe,
            headers={"Content-Disposition": "attachment; filename=RemoteControl.exe"},
        )
    if UPDATE_EXE_URL:
        raise web.HTTPFound(UPDATE_EXE_URL)
    return web.json_response({"error": "No update file available"}, status=404)


# =============================================================================
#  Запуск
# =============================================================================
async def main() -> None:
    global tg_app

    if BOT_TOKEN:
        tg_app = Application.builder().token(BOT_TOKEN).build()
        tg_app.add_handler(CommandHandler("start", tg_start))
        tg_app.add_handler(CommandHandler("send", tg_send))
        tg_app.add_handler(CommandHandler("broadcast", tg_broadcast))
        tg_app.add_handler(CommandHandler("scripts", tg_scripts))
        tg_app.add_handler(CommandHandler("panel", tg_panel))
        await tg_app.initialize()
        await tg_app.start()
        asyncio.create_task(tg_app.updater.start_polling())
        log.info("✓ Telegram bot started")
    else:
        log.warning("BOT_TOKEN not set — bot disabled")

    web_app = web.Application()
    web_app.router.add_get("/", h_keepalive_ping)
    web_app.router.add_get("/ws", ws_handler)
    web_app.router.add_get("/api/status", h_status)
    web_app.router.add_get("/api/scripts", h_scripts)
    web_app.router.add_post("/api/send", h_send)
    web_app.router.add_post("/api/broadcast", h_broadcast)
    web_app.router.add_get("/version", h_version)
    web_app.router.add_get("/update", h_update)

    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

    log.info(f"✓ Server on port {PORT}")
    log.info(f"✓ WS: {PUBLIC_URL.replace('http', 'ws')}/ws")

    try:
        await asyncio.Future()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
