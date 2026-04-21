# 🔐 Remote Control — Security & Advanced Features

## Admin Panel Security System

### 1. **Permission-Based Access** 
First time you visit the admin panel:
- Shows a **permission request modal** with required permissions
- User must click **"Allow"** to proceed
- If user clicks **"Deny"** → entire panel is **permanently blocked** (cannot reload, cannot bypass)
- Cannot be dismissed or closed — must accept or deny

**Requested Permissions:**
- Execute remote commands
- View system information  
- Control input devices
- Access file system

---

### 2. **Password Protection**
After granting permissions:
- Password modal appears automatically
- **First time:** You set the password (whatever you type is saved)
- **Subsequent times:** You must enter the saved password
- **Max 5 incorrect attempts** → panel permanently blocks with "Access Denied" message
- Cannot reload page, cannot go back, cannot navigate away — blocked permanently

**Password Features:**
- Uses localStorage to remember your password (first time only)
- Survives page reloads
- Session timeout: 30 minutes of inactivity auto-logs out

---

### 3. **Block Mechanism**
If permission denied or password fails:
- **Blocked Screen appears** with message and icon
- Cannot reload page (beforeunload event prevents it)
- Cannot use back button (pushState prevents it)
- Cannot close and reopen — localStorage tracks denial
- Only way to reset: **Clear localStorage in browser dev tools**

---

## Command Chaining with `/and` — Exit on Error

### Basic Syntax
```
screenshot /and lock
notify Hello /and type_text World
shutdown 60 /and restart 120
```

### How It Works
1. Splits command by ` /and `
2. Executes each command **in sequence** with 200ms delay between them
3. **If any command fails, the chain STOPS immediately** (error exit)
4. Results show which commands succeeded (✓) and which failed (❌)

### Example Execution
```
[1] ✓ screenshot
    → Screenshot ready! 1920x1080

[2] ✓ lock  
    → Screen locked.

[3] ❌ invalid_command
    → Unknown command: 'invalid_command'
    [CHAIN STOPS HERE — no further commands execute]
```

### Why Error Exit?
- Prevents cascading failures
- Stops if PC goes offline mid-chain
- Stops if invalid command is used
- Logical safety: don't execute dependent commands if earlier ones fail

---

## Admin Panel Features

### Dashboard
- Real-time connected scripts count
- Quick command buttons (Status, Screenshot, Lock, etc.)
- System overview
- GitHub-dark theme (professional, not green)

### Command Console
- Type commands with `/and` chaining
- Auto-split on ` /and ` separator
- Shows which commands succeeded/failed
- Command history with timestamps

### Settings
- Session timeout: 30 minutes
- Last login time
- Manual logout button

---

## Usage Examples

### From Telegram (Admin)
```
/send mypc screenshot /and lock /and notify Done

/send brothers_pc notify Hello /and type_text World

/broadcast status /and cpu /and ram
```

### From Admin Panel Web
1. Select script in sidebar
2. Type: `screenshot /and lock`
3. Click Send
4. See results: ✓ screenshot, ✓ lock

### What Happens If A Command Fails
```
User: /send mypc screenshot /and invalid_cmd /and lock

Result:
[1] ✓ screenshot → Success
[2] ❌ invalid_cmd → Unknown command
[3] 🚫 lock → NOT EXECUTED (chain stopped at #2)
```

---

## Security Checklist

✅ **Permission Gate** — must allow access (cannot bypass)
✅ **Password Protection** — must enter correct password
✅ **Blocked State** — permanent block if denied/failed (no reload)
✅ **Session Timeout** — auto-logout after 30 min inactivity
✅ **API Auth** — all API calls require X-Admin-Key header
✅ **Error Exit** — chains stop on first failure (safe execution)
✅ **No Back Button** — history pushState prevents navigation back
✅ **No Unload** — beforeunload prevents reload when blocked

---

## Files Updated

- `admin_panel.html` — Beautiful GitHub-dark panel with permission + password
- `client.py` — Added `handle_chained_command()` with error exit
- `server.py` — Updated to handle `/and` chains safely

---

## Testing the Security

### Test 1: Deny Permissions
1. Visit `/admin`
2. Click "Deny" on permission modal
3. Blocked screen appears
4. Try to reload → nothing happens (blocked)
5. Try to go back → nothing happens (blocked)

### Test 2: Wrong Password
1. Visit `/admin` (first time = set password to "test123")
2. Enter wrong password 5 times
3. Blocked screen appears
4. Try to reload → blocked
5. **To unlock:** Open dev tools → Application → localStorage → delete items → reload

### Test 3: Command Chaining with Error
```
/send mypc notify hi /and invalid_command /and lock

Expected:
[1] ✓ notify hi
[2] ❌ invalid_command
[3] NOT RUN (chain stopped)
```

---

## Default Credentials

**First Visit:**
- Permission: Click "Allow"
- Password: Type anything (that becomes your password)

**Subsequent Visits:**
- Password: Enter what you set on first visit

---

End of documentation
