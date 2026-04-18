# Setup & Deployment Guide

## How it all fits together

```
[Telegram Bot]  ←→  [Server on Railway]  ←→  [client.exe on PC 1]
                                         ←→  [client.exe on PC 2]
                                         ←→  [client.exe on PC 3]
```

You type a command in Telegram → server receives it → sends it to the right script.exe → result comes back to Telegram.

---

## Step 1 — Create a Telegram Bot

1. Open Telegram, message **@BotFather**
2. `/newbot` → follow prompts → copy the **token**
3. Get your Telegram user ID: message **@userinfobot**

---

## Step 2 — Deploy the Server (Railway)

1. Push `server.py` and `requirements.txt` to a GitHub repo
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Set these **environment variables** in Railway:

```
BOT_TOKEN    = your_telegram_bot_token
ADMIN_IDS    = your_telegram_user_id
SECRET_KEY   = pick_any_long_random_string
PUBLIC_URL   = https://your-app.up.railway.app
```

4. Railway will give you a URL like `https://your-app.up.railway.app`
5. Your admin panel will be at: `https://your-app.up.railway.app/admin`

---

## Step 3 — Build the .exe (Windows)

```bash
pip install pyinstaller websockets
pyinstaller --onefile client.py
```

The .exe will be in the `dist/` folder.

**To run as script1:**
```
client.exe --name script1 --server wss://your-app.up.railway.app/ws --secret your_secret_key
```

Or set environment variables instead of flags:
```
set SCRIPT_NAME=script1
set SERVER_URL=wss://your-app.up.railway.app/ws
set SECRET_KEY=your_secret_key
client.exe
```

**For multiple PCs:**  
Same .exe, different `--name` argument on each PC.  
script1.exe, script2.exe… are just the same file with different names or different launch args.

---

## Step 4 — Add Your Own Commands

Open `client.py` and find this section:

```python
async def cmd_my_task(params: str) -> str:
    """← PUT YOUR ACTUAL TASK HERE."""
    ...
```

Add your logic there. Then add it to the router:

```python
COMMANDS = {
    "hello":     cmd_hello,
    "my_task":   cmd_my_task,
    "your_cmd":  cmd_your_function,   # ← add here
}
```

Then rebuild the .exe.

---

## Using from Telegram

Once a script is connected, use these bot commands:

| Command | What it does |
|---|---|
| `/scripts` | Show all connected scripts |
| `/send script1 my_task hello` | Send `my_task hello` to script1 |
| `/send script2 status` | Ask script2 for its status |
| `/intercept script1 shell dir` | Next command to script1 will be replaced with `shell dir` |
| `/history` | Show last 10 commands |
| `/panel` | Get the admin panel link |

---

## Admin Panel Features

Visit `/admin` on your server URL:

- **See all connected scripts** in real time (refreshes every 3s)
- **Send a command** to any specific script
- **Set an intercept** — override the next command sent to a script
- **Command history** — see all commands and their results

The panel password = your `SECRET_KEY` environment variable.

---

## Running as a Windows Service (optional)

To make the .exe auto-start and keep running:

```bash
pip install pywin32
# Then use Windows Task Scheduler or NSSM (Non-Sucking Service Manager)
# to run client.exe at startup
```
