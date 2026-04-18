"""
Remote Control Worker Client
─────────────────────────────
Runs on any PC, converts to .exe with PyInstaller.
Connects to your server 24/7 and accepts commands from Telegram / Admin Panel.

Build to .exe:
  pip install pyinstaller
  pyinstaller --onefile --noconsole client.py

Run:
  client.exe --name script1 --server wss://your-server.up.railway.app/ws --secret your_secret
"""

import asyncio, json, os, sys, argparse, logging, subprocess, shutil
import platform, socket, time, base64
from datetime import datetime
from pathlib import Path

import websockets

# ── optional libs (graceful fallback if missing) ──────────────────────────────
try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

try:
    import pyautogui
    pyautogui.FAILSAFE = False
    HAS_GUI = True
except ImportError:
    HAS_GUI = False

try:
    from PIL import ImageGrab
    import io as _io
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    import requests as _requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument("--name",   default=os.environ.get("SCRIPT_NAME", "script1"))
parser.add_argument("--server", default=os.environ.get("SERVER_URL",  "ws://localhost:8080/ws"))
parser.add_argument("--secret", default=os.environ.get("SECRET_KEY",  "changeme"))
args = parser.parse_args()

SCRIPT_NAME     = args.name
SERVER_URL      = args.server
SECRET_KEY      = args.secret
RECONNECT_DELAY = 5

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("client")

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def bytes_to_mb(b): return f"{b/1024/1024:.1f} MB"
def bytes_to_gb(b): return f"{b/1024/1024/1024:.2f} GB"

def run_shell(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout + r.stderr).strip()
        return out[:3000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return f"Timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════
#  1. SCREENSHOT & SCREEN
# ═══════════════════════════════════════════════════════

async def cmd_screenshot(params):
    """Takes a screenshot, uploads to file.io, returns a link. Usage: screenshot"""
    if not HAS_PIL:     return "Pillow not installed. pip install Pillow"
    if not HAS_REQUESTS: return "requests not installed. pip install requests"
    try:
        img = ImageGrab.grab()
        buf = _io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        resp = _requests.post("https://file.io/?expires=1d",
                              files={"file": ("screenshot.png", buf, "image/png")}, timeout=20)
        data = resp.json()
        if data.get("success"):
            return f"Screenshot uploaded!\nLink: {data['link']}\nExpires in 1 day."
        return f"Upload failed: {data}"
    except Exception as e:
        return f"Screenshot error: {e}"


async def cmd_screen_size(params):
    """Returns screen resolution. Usage: screen_size"""
    if HAS_GUI:
        w, h = pyautogui.size()
        return f"Screen size: {w} x {h} px"
    if HAS_PIL:
        img = ImageGrab.grab()
        return f"Screen size: {img.width} x {img.height} px"
    return "pyautogui or Pillow required."


# ═══════════════════════════════════════════════════════
#  2. MOUSE & KEYBOARD
# ═══════════════════════════════════════════════════════

async def cmd_mouse_move(params):
    """Moves mouse to x y. Usage: mouse_move 500 300"""
    if not HAS_GUI: return "pyautogui not installed."
    try:
        x, y = map(int, params.split())
        pyautogui.moveTo(x, y, duration=0.3)
        return f"Mouse moved to ({x}, {y})"
    except:
        return "Usage: mouse_move <x> <y>"


async def cmd_mouse_click(params):
    """Clicks mouse. Usage: mouse_click 500 300 left  |  mouse_click double"""
    if not HAS_GUI: return "pyautogui not installed."
    try:
        parts = params.split()
        button, clicks, x, y = "left", 1, None, None
        for p in parts:
            if p == "right":    button = "right"
            elif p == "double": clicks = 2
            elif p == "left":   button = "left"
            else:
                try:
                    if x is None: x = int(p)
                    else:         y = int(p)
                except: pass
        if x is not None and y is not None:
            pyautogui.click(x, y, clicks=clicks, button=button)
            return f"Clicked ({x},{y}) [{button} x{clicks}]"
        pyautogui.click(clicks=clicks, button=button)
        return f"Clicked current position [{button} x{clicks}]"
    except Exception as e:
        return f"Error: {e}"


async def cmd_mouse_scroll(params):
    """Scrolls mouse. Usage: mouse_scroll 3 (up) or mouse_scroll -3 (down)"""
    if not HAS_GUI: return "pyautogui not installed."
    try:
        amount = int(params.strip() or "3")
        pyautogui.scroll(amount)
        return f"Scrolled {'up' if amount>0 else 'down'} {abs(amount)} clicks"
    except:
        return "Usage: mouse_scroll <number>"


async def cmd_mouse_pos(params):
    """Returns current mouse position. Usage: mouse_pos"""
    if not HAS_GUI: return "pyautogui not installed."
    pos = pyautogui.position()
    return f"Mouse position: x={pos.x}, y={pos.y}"


async def cmd_type_text(params):
    """Types text on the keyboard. Usage: type_text Hello World"""
    if not HAS_GUI: return "pyautogui not installed."
    try:
        await asyncio.sleep(0.5)
        pyautogui.typewrite(params, interval=0.05)
        return f"Typed: {params!r}"
    except Exception as e:
        return f"Error: {e}"


async def cmd_hotkey(params):
    """Presses a key combo. Usage: hotkey ctrl c  |  hotkey alt f4  |  hotkey win d"""
    if not HAS_GUI: return "pyautogui not installed."
    try:
        keys = params.strip().split()
        pyautogui.hotkey(*keys)
        return f"Hotkey: {' + '.join(keys)}"
    except Exception as e:
        return f"Error: {e}"


async def cmd_press_key(params):
    """Presses a single key. Usage: press_key enter  |  press_key escape"""
    if not HAS_GUI: return "pyautogui not installed."
    try:
        pyautogui.press(params.strip())
        return f"Key pressed: {params.strip()}"
    except Exception as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════
#  3. FILE MANAGER
# ═══════════════════════════════════════════════════════

async def cmd_ls(params):
    """Lists files in a directory. Usage: ls C:\\Users\\Name\\Desktop"""
    try:
        path = Path(params.strip() or ".").expanduser()
        if not path.exists(): return f"Path not found: {path}"
        items = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        lines = [f"Directory: {path.resolve()}\n"]
        for item in items[:80]:
            if item.is_dir():
                lines.append(f"  [DIR]  {item.name}")
            else:
                sz = item.stat().st_size
                lines.append(f"  [FILE] {item.name}  ({sz} B)" )
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


async def cmd_read_file(params):
    """Reads a text file. Usage: read_file C:\\path\\file.txt"""
    try:
        path = Path(params.strip()).expanduser()
        if not path.exists(): return f"File not found: {path}"
        if path.stat().st_size > 50_000:
            return f"File too large. Max 50 KB."
        return f"--- {path} ---\n{path.read_text(encoding='utf-8', errors='replace')[:3000]}"
    except Exception as e:
        return f"Error: {e}"


async def cmd_write_file(params):
    """Writes text to a file. Usage: write_file C:\\path\\file.txt | content here"""
    try:
        if "|" not in params: return "Usage: write_file <path> | <content>"
        path_str, content = params.split("|", 1)
        path = Path(path_str.strip()).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content.strip(), encoding="utf-8")
        return f"Written {len(content.strip())} chars to {path}"
    except Exception as e:
        return f"Error: {e}"


async def cmd_delete_file(params):
    """Deletes a file or folder. Usage: delete_file C:\\path\\file.txt"""
    try:
        path = Path(params.strip()).expanduser()
        if not path.exists(): return f"Not found: {path}"
        if path.is_dir(): shutil.rmtree(path)
        else:             path.unlink()
        return f"Deleted: {path}"
    except Exception as e:
        return f"Error: {e}"


async def cmd_copy_file(params):
    """Copies a file. Usage: copy_file C:\\source.txt | C:\\dest.txt"""
    try:
        a, b = params.split("|", 1)
        src, dst = Path(a.strip()).expanduser(), Path(b.strip()).expanduser()
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return f"Copied {src} to {dst}"
    except Exception as e:
        return f"Error: {e}"


async def cmd_move_file(params):
    """Moves a file. Usage: move_file C:\\source.txt | C:\\dest.txt"""
    try:
        a, b = params.split("|", 1)
        src, dst = Path(a.strip()).expanduser(), Path(b.strip()).expanduser()
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return f"Moved {src} to {dst}"
    except Exception as e:
        return f"Error: {e}"


async def cmd_download_file(params):
    """Uploads a local file to file.io for download. Usage: download_file C:\\path\\file.txt"""
    if not HAS_REQUESTS: return "requests not installed."
    try:
        path = Path(params.strip()).expanduser()
        if not path.exists(): return f"File not found: {path}"
        if path.stat().st_size > 10*1024*1024: return "File too large (max 10 MB)."
        with open(path, "rb") as f:
            resp = _requests.post("https://file.io/?expires=1d",
                                  files={"file": (path.name, f)}, timeout=30)
        data = resp.json()
        if data.get("success"):
            return f"File ready!\nLink: {data['link']}\nExpires in 1 day"
        return f"Upload failed: {data}"
    except Exception as e:
        return f"Error: {e}"


async def cmd_mkdir(params):
    """Creates a directory. Usage: mkdir C:\\path\\new_folder"""
    try:
        path = Path(params.strip()).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return f"Created: {path}"
    except Exception as e:
        return f"Error: {e}"


async def cmd_find_file(params):
    """Searches for files by name pattern. Usage: find_file *.py C:\\Users"""
    try:
        parts = params.strip().split(None, 1)
        pattern  = parts[0]
        base_dir = Path(parts[1] if len(parts) > 1 else ".").expanduser()
        results  = list(base_dir.rglob(pattern))[:30]
        if not results: return f"No files matching '{pattern}' in {base_dir}"
        return "Found:\n" + "\n".join(f"  {r}" for r in results)
    except Exception as e:
        return f"Error: {e}"


async def cmd_pwd(params):
    """Shows current working directory. Usage: pwd"""
    return f"Working directory: {os.getcwd()}"


async def cmd_cd(params):
    """Changes working directory. Usage: cd C:\\Users\\Name"""
    try:
        os.chdir(Path(params.strip()).expanduser())
        return f"Changed to: {os.getcwd()}"
    except Exception as e:
        return f"Error: {e}"


async def cmd_env(params):
    """Lists or gets environment variables. Usage: env  |  env PATH"""
    if params.strip():
        return f"{params.strip()} = {os.environ.get(params.strip(), '(not set)')}"
    return "Environment:\n" + "\n".join(f"{k}={v}" for k,v in sorted(os.environ.items()))[:3000]


# ═══════════════════════════════════════════════════════
#  4. REMOTE TERMINAL
# ═══════════════════════════════════════════════════════

async def cmd_shell(params):
    """Runs a shell command. Usage: shell dir  |  shell ipconfig"""
    if not params.strip(): return "Usage: shell <command>"
    return f"$ {params}\n{'-'*30}\n{run_shell(params)}"


async def cmd_powershell(params):
    """Runs a PowerShell command (Windows). Usage: powershell Get-Process"""
    return f"PS> {params}\n{'-'*30}\n{run_shell(f'powershell -Command \"{params}\"')}"


# ═══════════════════════════════════════════════════════
#  5. SYSTEM MONITOR
# ═══════════════════════════════════════════════════════

async def cmd_status(params):
    """Full system status. Usage: status"""
    lines = [
        f"[{SCRIPT_NAME}] System Status",
        f"Time:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"OS:     {platform.system()} {platform.release()} ({platform.machine()})",
        f"Host:   {socket.gethostname()}",
        f"Python: {sys.version.split()[0]}",
    ]
    if HAS_PSUTIL:
        cpu  = psutil.cpu_percent(interval=1)
        mem  = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        uptime = datetime.now() - datetime.fromtimestamp(psutil.boot_time())
        lines += [
            f"CPU:    {cpu}% ({psutil.cpu_count()} cores)",
            f"RAM:    {bytes_to_mb(mem.used)} / {bytes_to_mb(mem.total)} ({mem.percent}%)",
            f"Disk:   {bytes_to_gb(disk.used)} / {bytes_to_gb(disk.total)} ({disk.percent}%)",
            f"Uptime: {str(uptime).split('.')[0]}",
        ]
    try:
        lines.append(f"IP:     {socket.gethostbyname(socket.gethostname())}")
    except: pass
    return "\n".join(lines)


async def cmd_cpu(params):
    """Shows CPU usage. Usage: cpu"""
    if not HAS_PSUTIL: return "psutil not installed."
    total = psutil.cpu_percent(interval=1)
    cores = psutil.cpu_percent(interval=0.5, percpu=True)
    lines = [f"CPU: {total}% total | {psutil.cpu_count()} cores"]
    for i, c in enumerate(cores):
        bar = "█" * int(c/10) + "░" * (10-int(c/10))
        lines.append(f"  Core {i}: {bar} {c}%")
    return "\n".join(lines)


async def cmd_ram(params):
    """Shows RAM usage. Usage: ram"""
    if not HAS_PSUTIL: return "psutil not installed."
    m = psutil.virtual_memory()
    s = psutil.swap_memory()
    bar = "█" * int(m.percent/10) + "░" * (10-int(m.percent/10))
    return (f"RAM: {bar} {m.percent}%\n"
            f"  Used: {bytes_to_mb(m.used)}  Free: {bytes_to_mb(m.available)}  Total: {bytes_to_mb(m.total)}\n"
            f"  Swap: {bytes_to_mb(s.used)} / {bytes_to_mb(s.total)}")


async def cmd_disk(params):
    """Shows disk usage. Usage: disk"""
    if not HAS_PSUTIL: return "psutil not installed."
    lines = ["Disk usage:"]
    for p in psutil.disk_partitions():
        try:
            u = psutil.disk_usage(p.mountpoint)
            bar = "█"*int(u.percent/10) + "░"*(10-int(u.percent/10))
            lines.append(f"  {p.device}: {bar} {u.percent}%  {bytes_to_gb(u.used)}/{bytes_to_gb(u.total)}")
        except: pass
    return "\n".join(lines)


async def cmd_processes(params):
    """Lists top processes by CPU. Usage: processes"""
    if not HAS_PSUTIL: return "psutil not installed."
    procs = []
    for p in psutil.process_iter(["pid","name","cpu_percent","memory_percent"]):
        try: procs.append(p.info)
        except: pass
    procs.sort(key=lambda x: x.get("cpu_percent",0), reverse=True)
    lines = ["Top processes:\n  PID      CPU%     MEM%     Name", "  " + "-"*45]
    for p in procs[:15]:
        lines.append(f"  {p['pid']:<8} {p['cpu_percent']:<8.1f} {p['memory_percent']:<8.1f} {p['name']}")
    return "\n".join(lines)


async def cmd_kill_process(params):
    """Kills a process by PID or name. Usage: kill_process 1234  |  kill_process notepad.exe"""
    if not HAS_PSUTIL: return "psutil not installed."
    try:
        p = psutil.Process(int(params.strip()))
        name = p.name(); p.terminate()
        return f"Killed: {name} (PID {int(params.strip())})"
    except ValueError:
        killed = []
        for p in psutil.process_iter(["pid","name"]):
            if params.strip().lower() in p.info["name"].lower():
                p.terminate(); killed.append(f"{p.info['name']} ({p.info['pid']})")
        return ("Killed: " + ", ".join(killed)) if killed else f"No process: {params}"
    except Exception as e:
        return f"Error: {e}"


async def cmd_network(params):
    """Shows network stats and interfaces. Usage: network"""
    if not HAS_PSUTIL: return "psutil not installed."
    io = psutil.net_io_counters()
    lines = [f"Network stats:",
             f"  Sent:     {bytes_to_mb(io.bytes_sent)}",
             f"  Received: {bytes_to_mb(io.bytes_recv)}",
             "", "  Interfaces:"]
    for name, addrs in psutil.net_if_addrs().items():
        for addr in addrs:
            if addr.family == socket.AF_INET:
                lines.append(f"  {name}: {addr.address}")
    return "\n".join(lines)


async def cmd_temp(params):
    """Shows hardware temperatures. Usage: temp"""
    if not HAS_PSUTIL: return "psutil not installed."
    try:
        temps = psutil.sensors_temperatures()
        if not temps: return "Temperature sensors not available."
        lines = ["Temperatures:"]
        for name, entries in temps.items():
            for e in entries:
                lines.append(f"  {name}/{e.label or 'core'}: {e.current}°C")
        return "\n".join(lines)
    except AttributeError:
        return "Temperature sensors not supported on this OS."


# ═══════════════════════════════════════════════════════
#  6. POWER & APP CONTROL
# ═══════════════════════════════════════════════════════

async def cmd_shutdown(params):
    """Shuts down the PC. Usage: shutdown [seconds]"""
    d = int(params.strip() or "10")
    run_shell(f"shutdown /s /t {d}" if platform.system()=="Windows" else f"shutdown -h +{max(1,d//60)}")
    return f"Shutdown in {d} seconds."

async def cmd_restart(params):
    """Restarts the PC. Usage: restart [seconds]"""
    d = int(params.strip() or "10")
    run_shell(f"shutdown /r /t {d}" if platform.system()=="Windows" else f"shutdown -r +{max(1,d//60)}")
    return f"Restart in {d} seconds."

async def cmd_sleep(params):
    """Puts the PC to sleep. Usage: sleep"""
    if platform.system()=="Windows": run_shell("rundll32.exe powrprof.dll,SetSuspendState 0,1,0")
    elif platform.system()=="Darwin": run_shell("pmset sleepnow")
    else: run_shell("systemctl suspend")
    return "Going to sleep."

async def cmd_lock(params):
    """Locks the screen. Usage: lock"""
    if platform.system()=="Windows": run_shell("rundll32.exe user32.dll,LockWorkStation")
    elif platform.system()=="Darwin": run_shell("pmset displaysleepnow")
    else: run_shell("loginctl lock-session")
    return "Screen locked."

async def cmd_cancel_shutdown(params):
    """Cancels a pending shutdown. Usage: cancel_shutdown"""
    run_shell("shutdown /a" if platform.system()=="Windows" else "shutdown -c")
    return "Shutdown cancelled."

async def cmd_open_app(params):
    """Opens an application or URL. Usage: open_app notepad.exe  |  open_app https://google.com"""
    try:
        if platform.system()=="Windows": os.startfile(params.strip())
        elif platform.system()=="Darwin": subprocess.Popen(["open", params.strip()])
        else: subprocess.Popen(["xdg-open", params.strip()])
        return f"Opened: {params.strip()}"
    except Exception as e:
        return f"Error: {e}"

async def cmd_close_app(params):
    """Closes an app by name. Usage: close_app notepad"""
    result = run_shell(f"taskkill /IM {params.strip()} /F" if platform.system()=="Windows" else f"pkill {params.strip()}")
    return result


# ═══════════════════════════════════════════════════════
#  7. CLIPBOARD
# ═══════════════════════════════════════════════════════

async def cmd_clipboard_get(params):
    """Gets clipboard text. Usage: clipboard_get"""
    try:
        import pyperclip
        return f"Clipboard:\n{pyperclip.paste()[:2000]}"
    except ImportError:
        return "pip install pyperclip"
    except Exception as e:
        return f"Error: {e}"

async def cmd_clipboard_set(params):
    """Sets clipboard text. Usage: clipboard_set Hello World"""
    try:
        import pyperclip; pyperclip.copy(params.strip())
        return f"Clipboard set to: {params.strip()!r}"
    except ImportError:
        return "pip install pyperclip"
    except Exception as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════
#  8. NOTIFICATIONS
# ═══════════════════════════════════════════════════════

async def cmd_notify(params):
    """Shows a notification on the PC screen. Usage: notify Hello from Telegram!"""
    msg = params.strip() or "Hello from remote!"
    if platform.system()=="Windows":
        run_shell(f'powershell -Command "New-BurntToastNotification -Text \'Remote\', \'{msg}\'"')
    elif platform.system()=="Darwin":
        run_shell(f'osascript -e \'display notification "{msg}" with title "Remote"\'')
    else:
        run_shell(f'notify-send "Remote" "{msg}"')
    return f"Notification sent: {msg!r}"

async def cmd_msgbox(params):
    """Shows a blocking message box on the PC. Usage: msgbox Are you there?"""
    msg = params.strip() or "Message from remote."
    if platform.system()=="Windows":
        run_shell(f'mshta vbscript:Execute("MsgBox ""{msg}"":close")')
    elif platform.system()=="Darwin":
        run_shell(f'osascript -e \'tell app "System Events" to display dialog "{msg}"\'')
    else:
        run_shell(f'zenity --info --text="{msg}"')
    return f"Message box shown: {msg!r}"


# ═══════════════════════════════════════════════════════
#  9. AUDIO
# ═══════════════════════════════════════════════════════

async def cmd_volume(params):
    """Sets volume (Windows). Usage: volume 50  |  volume mute"""
    p = params.strip().lower()
    if platform.system()!="Windows": return "Volume control: Windows only."
    if p=="mute":
        run_shell('powershell -Command "$obj=New-Object -ComObject WScript.Shell;$obj.SendKeys([char]173)"')
        return "Muted."
    try:
        vol = int(p)
        script = f"$o=New-Object -ComObject WScript.Shell;for($i=0;$i -lt 50;$i++){{$o.SendKeys([char]174)}};for($i=0;$i -lt {vol//2};$i++){{$o.SendKeys([char]175)}}"
        run_shell(f'powershell -Command "{script}"')
        return f"Volume set to ~{vol}%"
    except:
        return "Usage: volume 0-100  |  volume mute"

async def cmd_play_sound(params):
    """Plays a beep. Usage: play_sound"""
    if platform.system()=="Windows":
        run_shell('powershell -Command "[System.Media.SystemSounds]::Beep.Play()"')
    else:
        run_shell("paplay /usr/share/sounds/freedesktop/stereo/bell.oga 2>/dev/null || beep")
    return "Beep!"


# ═══════════════════════════════════════════════════════
#  10. NETWORK UTILITIES
# ═══════════════════════════════════════════════════════

async def cmd_ping_host(params):
    """Pings a host. Usage: ping_host google.com"""
    host = params.strip() or "8.8.8.8"
    flag = "-n 4" if platform.system()=="Windows" else "-c 4"
    return run_shell(f"ping {flag} {host}")

async def cmd_public_ip(params):
    """Gets the public IP of this PC. Usage: public_ip"""
    if not HAS_REQUESTS: return "requests not installed."
    try:
        return f"Public IP: {_requests.get('https://api.ipify.org', timeout=5).text}"
    except Exception as e:
        return f"Error: {e}"

async def cmd_wifi_list(params):
    """Lists available WiFi networks. Usage: wifi_list"""
    if platform.system()=="Windows": return run_shell("netsh wlan show networks")
    elif platform.system()=="Darwin": return run_shell("/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport -s")
    else: return run_shell("nmcli dev wifi list")


# ═══════════════════════════════════════════════════════
#  11. MISC UTILITIES
# ═══════════════════════════════════════════════════════

async def cmd_whoami(params):
    """Returns current user. Usage: whoami"""
    return run_shell("whoami")

async def cmd_installed_apps(params):
    """Lists installed applications. Usage: installed_apps"""
    if platform.system()=="Windows":
        return run_shell('powershell -Command "Get-ItemProperty HKLM:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\* | Select DisplayName | Where {$_.DisplayName} | Sort DisplayName | Format-Table -Hide"')
    return run_shell("dpkg --list 2>/dev/null | head -40 || brew list 2>/dev/null | head -40")

async def cmd_hello(params):
    """Says hello. Usage: hello"""
    return f"Hello from {SCRIPT_NAME}! I am online and ready. Type 'help' for all commands."

async def cmd_help(params):
    """Lists all available commands. Usage: help"""
    lines = [f"Commands on {SCRIPT_NAME} ({len(COMMANDS)} total):\n"]
    for name, fn in sorted(COMMANDS.items()):
        doc = (fn.__doc__ or "").strip().split("\n")[0]
        lines.append(f"  {name} — {doc}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════
#  YOUR CUSTOM TASK — put your actual logic here
# ══════════════════════════════════════════════════

async def cmd_my_task(params):
    """Your custom task. Replace with your actual logic. Usage: my_task [params]"""
    await asyncio.sleep(0.5)
    return f"[{SCRIPT_NAME}] my_task ran! Input: {params!r}"


# ─────────────────────────────────────────────────────────────────────────────
# COMMAND ROUTER
# ─────────────────────────────────────────────────────────────────────────────
COMMANDS = {
    "help":            cmd_help,
    "hello":           cmd_hello,
    # Status & monitor
    "status":          cmd_status,
    "cpu":             cmd_cpu,
    "ram":             cmd_ram,
    "disk":            cmd_disk,
    "processes":       cmd_processes,
    "network":         cmd_network,
    "temp":            cmd_temp,
    # Screenshot
    "screenshot":      cmd_screenshot,
    "screen_size":     cmd_screen_size,
    # Mouse & keyboard
    "mouse_move":      cmd_mouse_move,
    "mouse_click":     cmd_mouse_click,
    "mouse_scroll":    cmd_mouse_scroll,
    "mouse_pos":       cmd_mouse_pos,
    "type_text":       cmd_type_text,
    "hotkey":          cmd_hotkey,
    "press_key":       cmd_press_key,
    # File manager
    "ls":              cmd_ls,
    "read_file":       cmd_read_file,
    "write_file":      cmd_write_file,
    "delete_file":     cmd_delete_file,
    "copy_file":       cmd_copy_file,
    "move_file":       cmd_move_file,
    "download_file":   cmd_download_file,
    "mkdir":           cmd_mkdir,
    "find_file":       cmd_find_file,
    "pwd":             cmd_pwd,
    "cd":              cmd_cd,
    "env":             cmd_env,
    # Shell
    "shell":           cmd_shell,
    "powershell":      cmd_powershell,
    # Power & apps
    "shutdown":        cmd_shutdown,
    "restart":         cmd_restart,
    "sleep":           cmd_sleep,
    "lock":            cmd_lock,
    "cancel_shutdown": cmd_cancel_shutdown,
    "open_app":        cmd_open_app,
    "close_app":       cmd_close_app,
    "kill_process":    cmd_kill_process,
    # Clipboard
    "clipboard_get":   cmd_clipboard_get,
    "clipboard_set":   cmd_clipboard_set,
    # Notifications
    "notify":          cmd_notify,
    "msgbox":          cmd_msgbox,
    # Audio
    "volume":          cmd_volume,
    "play_sound":      cmd_play_sound,
    # Network
    "ping_host":       cmd_ping_host,
    "public_ip":       cmd_public_ip,
    "wifi_list":       cmd_wifi_list,
    # Misc
    "whoami":          cmd_whoami,
    "installed_apps":  cmd_installed_apps,
    # Custom
    "my_task":         cmd_my_task,
}


async def dispatch(command):
    parts  = command.strip().split(None, 1)
    name   = parts[0].lower()
    params = parts[1] if len(parts) > 1 else ""
    if name in COMMANDS:
        try:    return str(await COMMANDS[name](params))
        except Exception as e:
            log.exception(f"Error in {name}")
            return f"Exception in {name}: {e}"
    return f"Unknown command: {name!r}  Type 'help' to see all commands."


# ─────────────────────────────────────────────────────────────────────────────
# WEBSOCKET LOOP
# ─────────────────────────────────────────────────────────────────────────────
async def run():
    while True:
        try:
            log.info(f"Connecting to {SERVER_URL} as '{SCRIPT_NAME}' ...")
            async with websockets.connect(SERVER_URL, ping_interval=20) as ws:
                await ws.send(json.dumps({"type":"register","name":SCRIPT_NAME,"secret":SECRET_KEY}))
                resp = json.loads(await ws.recv())
                if resp.get("type") == "error":
                    log.error(f"Registration failed: {resp.get('msg')}"); return
                log.info(f"Registered as '{SCRIPT_NAME}'. {len(COMMANDS)} commands ready.")

                async def heartbeat():
                    while True:
                        await asyncio.sleep(15)
                        try: await ws.send(json.dumps({"type":"ping"}))
                        except: break

                asyncio.create_task(heartbeat())

                async for raw in ws:
                    try: msg = json.loads(raw)
                    except: continue
                    if msg.get("type") == "command":
                        command = msg.get("command","")
                        log.info(f"IN: {command!r}")
                        result = await dispatch(command)
                        log.info(f"OUT: {result[:80]!r}")
                        reply = {"type":"result","command":command,"result":result}
                        if "reply_chat_id" in msg: reply["reply_chat_id"] = msg["reply_chat_id"]
                        await ws.send(json.dumps(reply))

        except (websockets.exceptions.ConnectionClosed, OSError, ConnectionRefusedError) as e:
            log.warning(f"Disconnected ({e}). Retrying in {RECONNECT_DELAY}s...")
            await asyncio.sleep(RECONNECT_DELAY)
        except KeyboardInterrupt:
            log.info("Stopped."); break
        except Exception as e:
            log.exception(f"Unexpected: {e}")
            await asyncio.sleep(RECONNECT_DELAY)


if __name__ == "__main__":
    print(f"""
+==============================================+
|  Remote Control Client                       |
|  Name   : {SCRIPT_NAME:<34}|
|  Server : {SERVER_URL:<34}|
|  Cmds   : {len(COMMANDS):<34}|
+==============================================+
Type 'help' in Telegram to list all commands.
""")
    asyncio.run(run())