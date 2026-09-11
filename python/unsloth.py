#!/usr/bin/env python3
import asyncio
import datetime
import json
import os
import platform
import re
import secrets
import socket
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Optional, Set
from bs4 import BeautifulSoup

import aiohttp
from aiohttp import web
import discord
from discord.ext import commands
from dotenv import load_dotenv

# --- CONFIGURATION ---
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OWNER_USER_ID = int(os.getenv("OWNER_ID", os.getenv("OWNER_USER_ID", "1468441445525491723")))
WORKSPACE_DIR = os.path.realpath(os.getenv("WORKSPACE_DIR", "./workspace"))
SUDO_PASSWORD = os.getenv("SUDO_PASSWORD", None)

# Unsloth Remote Server Setup
UNSLOTH_API_URL = os.getenv("UNSLOTH_API_URL", "")
UNSLOTH_API_KEY = os.getenv("UNSLOTH_API_KEY", "")
MODEL_NAME = "default"

WHITELIST_FILE = Path("whitelist.json")
MEMORY_FILE = Path("memory.json")
VENV_DIR = os.path.join(WORKSPACE_DIR, ".venv")

os.makedirs(WORKSPACE_DIR, exist_ok=True)

CURRENT_DIR = WORKSPACE_DIR
CHANNEL_HISTORY: Dict[int, list] = {}
LAST_OPENED_URL = "https://open.spotify.com"

# Per-user active sudo and 1-time challenge tokens
sudo_users: Set[int] = set()
pending_sudo_tokens: Dict[int, Dict[str, Any]] = {}

# Global HTTP session for persistent connection keep-alive
HTTP_SESSION: Optional[aiohttp.ClientSession] = None

# --- ZEN BROWSER BRIDGE STATE (PORT 5005) ---
zen_command_queue: asyncio.Queue = asyncio.Queue()
zen_results: Dict[str, asyncio.Future] = {}
zen_latest_url: str = LAST_OPENED_URL
zen_latest_title: str = "Unknown"

if not DISCORD_TOKEN:
    raise ValueError("CRITICAL: DISCORD_TOKEN is not set in your .env file.")


# --- AUTO-PROVISION VIRTUAL ENVIRONMENT ---
def ensure_workspace_venv():
    if not os.path.exists(os.path.join(VENV_DIR, "bin", "python")):
        try:
            subprocess.run([sys.executable, "-m", "venv", VENV_DIR], check=True, capture_output=True)
        except Exception:
            pass


ensure_workspace_venv()


# --- WHITELIST SYSTEM ---
def load_whitelist() -> Set[int]:
    if WHITELIST_FILE.exists():
        try:
            with open(WHITELIST_FILE, "r") as f:
                return set(json.load(f))
        except Exception:
            pass
    return set()


def save_whitelist(wl: Set[int]):
    with open(WHITELIST_FILE, "w") as f:
        json.dump(list(wl), f, indent=2)


whitelist: Set[int] = load_whitelist()


def is_owner(user_id: int) -> bool:
    return OWNER_USER_ID != 0 and user_id == OWNER_USER_ID


# --- WORKSPACE PATH CONFINEMENT ---
def resolve_path(path_str: str, user_id: int) -> str:
    clean = path_str.strip().strip("'\"")
    expanded = os.path.expanduser(clean)

    if os.path.isabs(expanded):
        target = os.path.realpath(expanded)
    else:
        target = os.path.realpath(os.path.join(CURRENT_DIR, expanded))

    if not is_owner(user_id):
        real_ws = os.path.realpath(WORKSPACE_DIR)
        try:
            common = os.path.commonpath([target, real_ws])
        except ValueError:
            raise PermissionError(f"Access Denied: Path '{path_str}' escapes root.")

        if common != real_ws:
            raise PermissionError(f"Access Denied: Path '{path_str}' is outside the workspace.")

    return target


def validate_bash_command(cmd: str, user_id: int, original_prompt: str = ""):
    if is_owner(user_id):
        return

    if ".." in cmd:
        raise PermissionError("Access Denied: Parent directory traversal ('..') is forbidden.")

    restricted_roots = ["/etc", "/home", "/var", "/usr", "/bin", "/sbin", "/root", "/boot", "~"]
    for r in restricted_roots:
        if re.search(rf"(?:^|\s){re.escape(r)}(?:/|\s|$)", cmd):
            raise PermissionError(f"Access Denied: Direct reference to '{r}' outside workspace is forbidden.")

    if re.search(r"\b(pacman\s+-[Syu]+|yay\s+-[Syu]+|paru\s+-[Syu]+)\b", cmd):
        if not any(k in original_prompt.lower() for k in ["install", "pacman", "yay", "update", "upgrade"]):
            raise PermissionError("Access Denied: Rogue package installation blocked. User did not ask to install software.")


# --- CLEAN OUTPUT TEXT ---
def strip_all_emojis_nuclear(text: str) -> str:
    cleaned = re.sub(r"[\U00010000-\U0010ffff\u2600-\u27bf\u2300-\u23ff\u2b50\u3030\ufe0f\u200d\u200e\u200b]", "", text)
    cleaned = re.sub(r"(?:\s|^)[:=]-?[)DdpP3](\s|$)", " ", cleaned)
    cleaned = re.sub(
        r"^(?:Sure,\s*here'?s\s*my\s*final\s*response:?\s*[-—]*\s*)?(?:\[STATUS\]:?\s*)?(?:\[RESPONSE\]:?\s*)?(?:[A-Za-z0-9_]+:\s*)?",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\[STATUS\]:?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\[RESPONSE\]:?\s*", "", cleaned, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", cleaned).strip()


# --- SYSTEM STATS ---
def get_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("1.1.1.1", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


LOCAL_IP = get_local_ip()
HOSTNAME = socket.gethostname()
KERNEL = platform.release()


# --- MEMORY SYSTEM ---
def load_memories() -> list:
    if MEMORY_FILE.exists():
        try:
            with open(MEMORY_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def save_memories():
    with open(MEMORY_FILE, "w") as f:
        json.dump(LONG_TERM_MEMORIES, f, indent=2)


LONG_TERM_MEMORIES = load_memories()


# --- STATIC SYSTEM PROMPT ---
STATIC_SYSTEM_PROMPT = f"""You are the 'estrogen goober', an elite Arch Linux engineering companion and KDE Plasma desktop operator.
Primary Workspace: {WORKSPACE_DIR}
OS: Arch Linux x86_64 | Desktop: KDE Plasma | Hostname: {HOSTNAME} | Kernel: {KERNEL}

WORKSPACE CONFINEMENT:
- Non-owners must keep file writes, reads, checks, and shell commands confined inside {WORKSPACE_DIR}.

AVAILABLE TOOLS:
1. Media Control: [MEDIA_CONTROL: play|pause|play-pause|next|previous|stop]
2. Check Song: [CHECK_SONG]
3. Spotify Search & Play: [SPOTIFY_SEARCH: query]
4. System Volume: [SET_VOLUME: 0-100]
5. Open URL: [BROWSER_OPEN: https://...]
6. Browser DOM Evaluation: [BROWSER_EVAL: javascript_code]
7. Browser DOM Click: [BROWSER_CLICK: css_selector]
8. Inspect Active Page: [INSPECT_PAGE]
9. Desktop Screenshot: [SCREENSHOT]
10. Read/Write Clipboard: [CLIPBOARD_READ] / [CLIPBOARD_WRITE: text]
11. Send Native Desktop Notification: [KDE_NOTIFY: title | message]
12. System Health Doctor: [SYSTEM_DOCTOR]
13. Journal Errors: [JOURNAL_ERRORS]
14. Search AUR: [ARCH_AUR_SEARCH: query]
15. Bash Command: [BASH: command]
16. Web Search: [SEARCH: query]
17. Write File: [WRITE_FILE: relative_filename]\ncontent\n[/WRITE_FILE]
18. Read File: [READ_FILE: relative_filename]
19. Syntax Check: [CHECK_SYNTAX: filename.py]
20. Project Tree: [TREE]
21. Code Grep: [GREP: search_pattern]
22. Send File to Discord: [SEND_FILE: relative_filename]
23. Discord Servers Info: [DISCORD_GUILDS]
24. Discord Members: [DISCORD_MEMBERS]
25. Discord User Info: [DISCORD_USER_INFO: user_id_or_name]
26. Discord Kick: [MOD_KICK: user_id | reason]
27. Discord Ban: [MOD_BAN: user_id | reason]
28. Discord Timeout: [MOD_TIMEOUT: user_id | minutes | reason]
29. Purge Messages: [PURGE_MESSAGES: count]

RULES:
- When asked to inspect, interact with, click, or scrape Spotify/Zen Browser, use [BROWSER_EVAL], [BROWSER_CLICK], or [SPOTIFY_SEARCH].
- KDE Plasma environment: Do not reference Hyprland.
- FAST & CONCISE: Emit tool calls immediately without long rationale.
- ZERO EMOJIS in textual responses.
- TONE: Technically sharp, snarky, witty, direct, elite Arch Linux engineer.
"""


def build_dynamic_context(user_display_name: str, is_user_owner: bool) -> str:
    mem_text = ""
    if LONG_TERM_MEMORIES:
        mem_text = "Memories: " + " | ".join(LONG_TERM_MEMORIES[-8:]) + "\n"
    role = "OWNER (Full Authority)" if is_user_owner else "WHITELISTED GUEST"
    return (
        f"[CURRENT CONTEXT]\n"
        f"User: {user_display_name} ({role})\n"
        f"Active PWD: {CURRENT_DIR}\n"
        f"Active URL: {LAST_OPENED_URL}\n"
        f"Zen Browser Active Title: {zen_latest_title}\n"
        f"{mem_text}"
        f"[END CONTEXT]"
    )


# --- DISCORD CLIENT INITIALIZATION ---
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.check
async def global_whitelist_check(ctx: commands.Context):
    return is_owner(ctx.author.id) or ctx.author.id in whitelist


# --- ZEN BROWSER TAMPERMONKEY BRIDGE (AIOHTTP LOCAL SERVER) ---
async def zen_poll_handler(request: web.Request) -> web.Response:
    try:
        cmd_item = await asyncio.wait_for(zen_command_queue.get(), timeout=1.2)
        return web.json_response(cmd_item)
    except asyncio.TimeoutError:
        return web.json_response({})


async def zen_result_handler(request: web.Request) -> web.Response:
    global zen_latest_url, zen_latest_title
    try:
        data = await request.json()
        zen_latest_url = data.get("url", zen_latest_url)
        res_val = str(data.get("result", ""))
        zen_latest_title = res_val.split(" (http")[0] if " (http" in res_val else res_val[:60]
        # Resolve any waiting command futures
        for cid, fut in list(zen_results.items()):
            if not fut.done():
                fut.set_result(res_val)
        zen_results.clear()
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)


async def start_zen_bridge_server():
    app = web.Application()
    app.router.add_get("/poll", zen_poll_handler)
    app.router.add_post("/result", zen_result_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 5005)
    await site.start()
    print("\033[35m[✓] Zen Browser Tampermonkey Bridge listening on http://127.0.0.1:5005\033[0m")


async def execute_zen_browser_js(js_code: str, timeout: float = 4.0) -> str:
    cid = secrets.token_hex(4)
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    zen_results[cid] = fut
    await zen_command_queue.put({"id": cid, "code": js_code})
    try:
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        zen_results.pop(cid, None)
        return "Browser execution timed out (Is Zen Browser running with the Tampermonkey script active?)"


# --- DESKTOP & MEDIA TOOLS (KDE PLASMA) ---
def control_media(action: str) -> str:
    action = action.strip().lower()
    alias_map = {"unpause": "play", "resume": "play", "toggle": "play-pause", "skip": "next", "prev": "previous"}
    action = alias_map.get(action, action)
    valid_actions = ["play-pause", "play", "pause", "next", "previous", "stop"]
    if action not in valid_actions:
        action = "play-pause"
    try:
        subprocess.run(f"playerctl {action}", shell=True, timeout=2, capture_output=True)
        return f"Media command '{action}' executed."
    except Exception as e:
        return f"Media error: {e}"


def get_current_song() -> str:
    try:
        # Check Spotify directly via D-Bus / KDE first
        q_cmd = "qdbus org.mpris.MediaPlayer2.spotify /org/mpris/MediaPlayer2 org.mpris.MediaPlayer2.Player.Metadata 2>/dev/null"
        q_res = subprocess.run(q_cmd, shell=True, capture_output=True, text=True, timeout=2)
        if q_res.stdout.strip():
            title = re.search(r"xesam:title:\s*(.*)", q_res.stdout)
            artist = re.search(r"xesam:artist:\s*(.*)", q_res.stdout)
            if title:
                t = title.group(1).strip()
                a = artist.group(1).strip() if artist else "Unknown"
                return f"Spotify (Desktop): {t} by {a}"

        cmd = "playerctl metadata --format '{{playerName}}: {{title}} by {{artist}}' 2>/dev/null || playerctl metadata 2>/dev/null"
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=2)
        out = res.stdout.strip()
        return out if out else "No active media detected on the system."
    except Exception as e:
        return f"Playback detection error: {e}"


def search_and_play_spotify(query: str) -> str:
    query = query.strip()
    encoded = urllib.parse.quote(query)
    # 1. Open Spotify Desktop client search URI if available
    res = subprocess.run(f"spotify --uri='spotify:search:{encoded}' 2>/dev/null &", shell=True)
    # 2. Also prepare browser fallback
    return f"Triggered Spotify desktop search for '{query}'. Open URL fallback: https://open.spotify.com/search/{encoded}"


def set_system_volume(level: str) -> str:
    clean = re.sub(r"[^\d]", "", level)
    if not clean:
        return "Error: Provide a volume percentage between 0 and 100."
    vol = max(0, min(100, int(clean)))
    res = subprocess.run(f"pactl set-sink-volume @DEFAULT_SINK@ {vol}% 2>/dev/null || amixer set Master {vol}% 2>/dev/null", shell=True)
    return f"Master audio volume set to {vol}%." if res.returncode == 0 else "Failed to adjust audio sink volume."


def send_kde_notification(title: str, msg: str) -> str:
    t = title.replace("'", "")
    m = msg.replace("'", "")
    cmd = f"kdialog --title '{t}' --passivepopup '{m}' 5 2>/dev/null || notify-send '{t}' '{m}' 2>/dev/null"
    subprocess.run(cmd, shell=True)
    return f"Dispatched KDE notification: [{t}] {m}"


def get_kde_active_window() -> str:
    """KDE Plasma active window detection using kdotool, xdotool, or qdbus KWin."""
    try:
        # Try kdotool (KDE Wayland active window)
        kdo = subprocess.run("kdotool getactivewindow getwindowname 2>/dev/null", shell=True, capture_output=True, text=True, timeout=1)
        if kdo.stdout.strip():
            return kdo.stdout.strip()

        # Try xdotool (X11 / XWayland)
        xdo = subprocess.run("xdotool getactivewindow getwindowname 2>/dev/null", shell=True, capture_output=True, text=True, timeout=1)
        if xdo.stdout.strip():
            return xdo.stdout.strip()

        # Try KWin scripting query via qdbus
        q_cmd = "qdbus org.kde.KWin /KWin org.kde.KWin.activeWindow 2>/dev/null"
        q_res = subprocess.run(q_cmd, shell=True, capture_output=True, text=True, timeout=1)
        if q_res.stdout.strip():
            return f"KWin Window ID: {q_res.stdout.strip()}"
    except Exception:
        pass
    return "Unknown KDE Window"


async def take_screenshot() -> tuple[str, Optional[str]]:
    path = os.path.join(WORKSPACE_DIR, ".screenshot.png")
    # Spectacle is KDE Plasma's native screenshot engine for Wayland and X11
    cmd = (
        f"spectacle -b -n -o '{path}' 2>/dev/null || "
        f"grim '{path}' 2>/dev/null || maim '{path}' 2>/dev/null || scrot '{path}' 2>/dev/null"
    )
    proc = await asyncio.create_subprocess_shell(cmd)
    await proc.communicate()
    if os.path.exists(path) and os.path.getsize(path) > 1000:
        return "Screenshot captured.", path
    return "Screenshot failed (Ensure spectacle or grim is installed).", None


async def read_clipboard() -> str:
    cmd = "wl-paste 2>/dev/null || xclip -selection clipboard -o 2>/dev/null"
    proc = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE)
    stdout, _ = await proc.communicate()
    out = stdout.decode("utf-8", errors="ignore").strip()
    return out if out else "(Clipboard is empty)"


async def write_clipboard(text: str) -> str:
    proc = await asyncio.create_subprocess_shell("wl-copy 2>/dev/null || xclip -selection clipboard 2>/dev/null", stdin=asyncio.subprocess.PIPE)
    await proc.communicate(input=text.encode("utf-8"))
    return f"Copied to desktop clipboard: {text[:80]}"


async def run_system_doctor() -> str:
    cmd = (
        "echo '=== FAILED SYSTEMD SERVICES ===' && (systemctl --failed --no-legend 2>/dev/null || echo 'None') && "
        "echo '=== ROOT DISK USAGE ===' && df -h / | awk 'NR==2 {print $3 \" used out of \" $2 \" (\" $5 \")\"}' && "
        "echo '=== TOP 4 RAM CONSUMERS ===' && ps aux --sort=-%mem | awk 'NR>1 && NR<=5 {print $11, $4\"%\"}'"
    )
    proc = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, _ = await proc.communicate()
    return stdout.decode("utf-8", errors="ignore").strip()


async def get_journal_errors() -> str:
    cmd = "journalctl -p 3 -xb --since '15 min ago' --no-pager 2>/dev/null | tail -n 25"
    proc = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE)
    stdout, _ = await proc.communicate()
    out = stdout.decode("utf-8", errors="ignore").strip()
    return out if out else "No priority 3 systemd errors logged in the last 15 minutes."


async def search_aur(query: str) -> str:
    url = f"https://aur.archlinux.org/rpc/?v=5&type=search&arg={urllib.parse.quote(query)}"
    try:
        async with HTTP_SESSION.get(url, timeout=5) as resp:
            if resp.status == 200:
                data = await resp.json()
                results = data.get("results", [])
                if not results:
                    return f"No AUR packages found matching '{query}'."
                lines = [f"AUR Results for '{query}' (Found {len(results)}):"]
                for p in results[:5]:
                    lines.append(f"- **{p.get('Name')}** ({p.get('Version')}): {p.get('Description', 'No description')}")
                return "\n".join(lines)
    except Exception as e:
        return f"AUR query failed: {e}"
    return "AUR query returned no data."


async def inspect_active_page() -> str:
    global LAST_OPENED_URL
    kde_win = get_kde_active_window()

    # Query the Tampermonkey bridge for live browser title and location
    eval_res = await execute_zen_browser_js("document.title + ' | ' + window.location.href", timeout=1.5)
    if "timed out" not in eval_res:
        return f"KDE Active Window: '{kde_win}'\nZen Browser DOM: {eval_res}"

    try:
        headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}
        async with HTTP_SESSION.get(LAST_OPENED_URL, headers=headers, timeout=4) as resp:
            if resp.status == 200:
                html = await resp.text()
                title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE)
                title = title_match.group(1).strip() if title_match else "No title found"
                return f"KDE Active Window: '{kde_win}'\nWebpage ({LAST_OPENED_URL}):\n- Title: {title}"
    except Exception as e:
        return f"KDE Active Window: '{kde_win}'\nActive URL: {LAST_OPENED_URL} (Inspect error: {e})"

    return f"KDE Active Window: '{kde_win}'\nActive URL: {LAST_OPENED_URL}"


def write_file_tool(path_str: str, content: str, user_id: int) -> str:
    try:
        full_path = resolve_path(path_str, user_id)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        clean = re.sub(r"^```[a-zA-Z0-9_\-\+]*\n", "", content.strip())
        clean = re.sub(r"\n```$", "", clean)
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(clean)
        return f"Successfully wrote {len(clean.splitlines())} lines to `{full_path}`"
    except Exception as e:
        return f"Error writing file: {e}"


def read_file_tool(path_str: str, user_id: int) -> str:
    try:
        full_path = resolve_path(path_str, user_id)
        if not os.path.exists(full_path):
            return f"Error: File '{full_path}' does not exist."
        if os.path.isdir(full_path):
            return f"Error: '{full_path}' is a directory. Use [TREE]."
        with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        numbered = [f"{i + 1:3d} | {line}" for i, line in enumerate(lines[:100])]
        res = "".join(numbered)
        if len(lines) > 100:
            res += f"\n... [{len(lines) - 100} more lines omitted]"
        return res if res else "(File is empty)"
    except Exception as e:
        return f"Error reading file: {e}"


def check_python_syntax(path_str: str, user_id: int) -> str:
    try:
        full_path = resolve_path(path_str, user_id)
    except PermissionError as pe:
        return str(pe)

    if not os.path.exists(full_path):
        return f"Error: File '{path_str}' does not exist on disk."
    res = subprocess.run([sys.executable, "-m", "py_compile", full_path], capture_output=True, text=True)
    return f"Syntax OK: '{path_str}' compiled successfully." if res.returncode == 0 else f"Syntax Error in '{path_str}':\n{res.stderr.strip()}"


def grep_codebase(pattern: str, user_id: int) -> str:
    pattern = pattern.strip().strip("'\"")
    search_dir = CURRENT_DIR if is_owner(user_id) else WORKSPACE_DIR
    cmd = f"grep -rnI --exclude-dir='.venv' --exclude-dir='.git' '{pattern}' '{search_dir}'"
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=4)
    lines = res.stdout.strip().splitlines()
    if not lines:
        return f"No occurrences of '{pattern}' found."
    result = "\n".join(lines[:25])
    if len(lines) > 25:
        result += f"\n... [{len(lines) - 25} more matches omitted]"
    return result


def get_project_tree(user_id: int) -> str:
    target_dir = CURRENT_DIR if is_owner(user_id) else WORKSPACE_DIR
    cmd = f"tree -L 2 -I '.venv|.git|__pycache__' '{target_dir}' 2>/dev/null || find '{target_dir}' -maxdepth 2 -not -path '*/.*' | head -n 30"
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=2)
    return res.stdout.strip() if res.stdout.strip() else "(Directory is empty)"


from bs4 import BeautifulSoup  # run: pip install beautifulsoup4

async def search_web(query: str) -> str:
    url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    try:
        async with HTTP_SESSION.get(url, headers=headers, timeout=6) as resp:
            if resp.status == 200:
                html = await resp.text()
                # Parse with BeautifulSoup or regex
                soup = BeautifulSoup(html, "html.parser")
                results = []
                for r in soup.select(".result__snippet")[:5]:
                    snippet = r.get_text().strip()
                    if snippet and len(snippet) > 20:
                        results.append(f"- {snippet}")
                
                if results:
                    return "[Search Results]:\n" + "\n".join(results)
    except Exception as e:
        return f"Search engine error: {e}"

    # Fallback: ddg instant answers API
    try:
        api_url = f"https://api.duckduckgo.com/?q={urllib.parse.quote(query)}&format=json&no_html=1&skip_disambig=1"
        async with HTTP_SESSION.get(api_url, headers=headers, timeout=4) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                abstract = data.get("AbstractText", "").strip()
                if abstract:
                    return f"[Instant Answer]: {abstract}"
    except Exception:
        pass

    return "No search results found."



# --- DISCORD SERVER INSPECTION & MODERATION TOOLS ---
def list_discord_guilds() -> str:
    guilds = bot.guilds
    if not guilds:
        return "Bot is currently not connected to any servers."
    lines = [f"Connected Servers ({len(guilds)} total):"]
    for g in guilds:
        me = g.me
        perms = me.guild_permissions
        status = "Admin" if perms.administrator else ("Mod" if perms.manage_messages else "Regular")
        lines.append(f"- **{g.name}** (ID: `{g.id}`) | {g.member_count} members | Perm: {status}")
    return "\n".join(lines)


def get_discord_members(guild) -> str:
    if not guild:
        return "Error: Cannot fetch members outside of a server."
    members = guild.members[:30]
    lines = [f"Server '{guild.name}' Members ({len(guild.members)} total):"]
    for m in members:
        bot_tag = " [BOT]" if m.bot else ""
        lines.append(f"- {m.name} (Nick: {m.display_name}, ID: `{m.id}`){bot_tag}")
    return "\n".join(lines)


def get_discord_user_info(guild, query: str) -> str:
    if not guild:
        return "Error: Cannot fetch user info outside of a server."
    query = re.sub(r"[<@!>]", "", query).strip().lower()
    target = None
    for m in guild.members:
        if str(m.id) == query or m.name.lower() == query or m.display_name.lower() == query:
            target = m
            break
    if not target:
        return f"User '{query}' not found."
    roles = [r.name for r in target.roles if r.name != "@everyone"] or ["(No roles)"]
    joined = target.joined_at.strftime("%Y-%m-%d") if target.joined_at else "Unknown"
    created = target.created_at.strftime("%Y-%m-%d")
    return (
        f"User: {target.name} (Nick: {target.display_name})\n"
        f"ID: {target.id}\n"
        f"Account Created: {created} | Joined Server: {joined}\n"
        f"Roles: {', '.join(roles)}"
    )


async def mod_kick_user(guild, author_id: int, user_arg: str, reason: str) -> str:
    if not is_owner(author_id):
        return "Permission Denied: Only the bot owner can execute kicks."
    clean_id = int(re.sub(r"\D", "", user_arg))
    member = guild.get_member(clean_id)
    if not member:
        return f"User {user_arg} is not in this guild."
    try:
        await member.kick(reason=reason)
        return f"Successfully kicked {member.name} ({member.id}). Reason: {reason}"
    except Exception as e:
        return f"Kick failed: {e}"


async def mod_ban_user(guild, author_id: int, user_arg: str, reason: str) -> str:
    if not is_owner(author_id):
        return "Permission Denied: Only the bot owner can execute bans."
    clean_id = int(re.sub(r"\D", "", user_arg))
    try:
        await guild.ban(discord.Object(id=clean_id), reason=reason)
        return f"Successfully banned user ID {clean_id}. Reason: {reason}"
    except Exception as e:
        return f"Ban failed: {e}"


async def mod_timeout_user(guild, author_id: int, user_arg: str, minutes: int, reason: str) -> str:
    if not is_owner(author_id):
        return "Permission Denied: Only the bot owner can timeout members."
    clean_id = int(re.sub(r"\D", "", user_arg))
    member = guild.get_member(clean_id)
    if not member:
        return f"User {user_arg} not found in this guild."
    try:
        duration = datetime.timedelta(minutes=minutes)
        await member.timeout(duration, reason=reason)
        return f"Timed out {member.name} for {minutes} minutes. Reason: {reason}"
    except Exception as e:
        return f"Timeout failed: {e}"


async def purge_channel_messages(channel, author_id: int, limit: int) -> str:
    if not is_owner(author_id):
        return "Permission Denied: Only the bot owner can purge messages."
    count = max(1, min(100, limit))
    try:
        deleted = await channel.purge(limit=count + 1)
        return f"Purged {len(deleted) - 1} messages."
    except Exception as e:
        return f"Purge failed: {e}"


# --- BASH ENGINE ---
async def execute_bash(cmd: str, user_id: int, original_prompt: str = "") -> str:
    global CURRENT_DIR

    try:
        validate_bash_command(cmd, user_id, original_prompt)
    except PermissionError as pe:
        return f"⛔ {pe}"

    needs_sudo = bool(re.search(r"\bsudo\b", cmd))
    if needs_sudo:
        if not is_owner(user_id) and user_id not in sudo_users:
            return "⛔ Sudo is locked. Run `!allowsudo` to request authorization."
        if SUDO_PASSWORD:
            cmd = re.sub(r"\bsudo\b", "sudo -S", cmd)

    env = os.environ.copy()
    venv_bin = os.path.join(VENV_DIR, "bin")
    if os.path.exists(venv_bin):
        env["PATH"] = f"{venv_bin}:{env.get('PATH', '')}"
        env["VIRTUAL_ENV"] = VENV_DIR

    wrapped_cmd = f"{cmd}\n__EXIT_CODE__=$?\necho '___PWD___:'$(pwd)\nexit $__EXIT_CODE__"

    try:
        proc = await asyncio.create_subprocess_shell(
            wrapped_cmd,
            stdin=asyncio.subprocess.PIPE if (needs_sudo and SUDO_PASSWORD) else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=CURRENT_DIR,
            env=env,
            executable="/bin/bash",
        )
        input_data = f"{SUDO_PASSWORD}\n".encode() if (needs_sudo and SUDO_PASSWORD) else None
        stdout, stderr = await asyncio.wait_for(proc.communicate(input=input_data), timeout=30)

        out_raw = stdout.decode("utf-8", errors="ignore")
        err = stderr.decode("utf-8", errors="ignore").strip()

        pwd_match = re.search(r"___PWD___:(.*)", out_raw)
        if pwd_match:
            new_dir = pwd_match.group(1).strip()
            if os.path.isdir(new_dir):
                if is_owner(user_id):
                    CURRENT_DIR = new_dir
                else:
                    real_ws = os.path.realpath(WORKSPACE_DIR)
                    if os.path.commonpath([os.path.realpath(new_dir), real_ws]) == real_ws:
                        CURRENT_DIR = new_dir
                    else:
                        CURRENT_DIR = WORKSPACE_DIR
                        err += "\n[Security]: Reset to workspace root."
            out = re.sub(r"___PWD___:.*", "", out_raw).strip()
        else:
            out = out_raw.strip()

        if (cmd.strip().startswith("cd ") or cmd.strip() == "cd" or "cd " in cmd) and not out:
            out = f"Directory changed to: {CURRENT_DIR}"

        res = f"{out}\n{err}".strip() if err else out
        return res if res else "(Command completed with no output)"
    except asyncio.TimeoutError:
        return "Command timed out after 30 seconds."
    except Exception as e:
        return f"Execution error: {e}"


# --- UNSLOTH LLM INFERENCE ---
async def call_unsloth_api(messages: list) -> str:
    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "temperature": 0.1,
        "top_p": 0.9,
        "max_tokens": 1024,
        "frequency_penalty": 0.2,
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {UNSLOTH_API_KEY}",
        "Content-Type": "application/json",
        "Connection": "keep-alive",
    }
    async with HTTP_SESSION.post(UNSLOTH_API_URL, json=payload, headers=headers) as resp:
        if resp.status != 200:
            raw_err = await resp.text()
            return f"Unsloth API Error (HTTP {resp.status}): {raw_err}"
        data = await resp.json()
        return data["choices"][0]["message"]["content"]


async def send_split_message(destination, text: str):
    cleaned = strip_all_emojis_nuclear(text)
    if not cleaned:
        return
    for i in range(0, len(cleaned), 1900):
        await destination.send(cleaned[i : i + 1900])


# --- CORE AGENT PIPELINE ---
async def process_ai_request(channel, author, prompt: str):
    global LAST_OPENED_URL
    async with channel.typing():
        clean_p = prompt.strip().lower()

        # Fast Song Shortcut
        if any(t in clean_p for t in ["what song", "what's playing", "whats playing", "current song", "now playing", "check song"]):
            await channel.send("Checking music playback...")
            out = get_current_song()
            await send_split_message(channel, out)
            return

        # Fast Terminal Shortcuts
        first_token = clean_p.split()[0].lower() if clean_p else ""
        common_cmds = ["ls", "dir", "pwd", "cd", "whoami", "uname", "uptime", "tree", "free", "df", "fastfetch", "neofetch", "sensors"]
        if first_token in common_cmds:
            target_cmd = "ls -la" if clean_p in ["dir", "ls"] else ("uptime -p" if clean_p == "uptime" else clean_p)
            await channel.send(f"Running: `{target_cmd}`")
            out = await execute_bash(target_cmd, author.id, prompt)
            await send_split_message(channel, f"```\n{out}\n```")
            return

        if channel.id not in CHANNEL_HISTORY:
            CHANNEL_HISTORY[channel.id] = []

        history = CHANNEL_HISTORY[channel.id]
        dynamic_ctx = build_dynamic_context(author.display_name, is_owner(author.id))

        messages = [
            {"role": "system", "content": STATIC_SYSTEM_PROMPT},
            {"role": "system", "content": dynamic_ctx},
        ]
        for msg in history[-6:]:
            messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({"role": "user", "content": prompt})

        recent_actions = []

        for _ in range(7):
            reply = await call_unsloth_api(messages)
            reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.DOTALL).strip()

            # Regex Matchers for All Available Tools
            media_m = re.search(r"\[MEDIA_CONTROL:\s*(.*?)\]", reply, re.IGNORECASE)
            song_m = re.search(r"\[CHECK_SONG\]", reply, re.IGNORECASE)
            spot_m = re.search(r"\[SPOTIFY_SEARCH:\s*(.*?)\]", reply, re.IGNORECASE)
            vol_m = re.search(r"\[SET_VOLUME:\s*(.*?)\]", reply, re.IGNORECASE)
            kdenotif_m = re.search(r"\[KDE_NOTIFY:\s*(.*?)\|(.*?)\]", reply, re.IGNORECASE)
            aur_m = re.search(r"\[ARCH_AUR_SEARCH:\s*(.*?)\]", reply, re.IGNORECASE)
            journal_m = re.search(r"\[JOURNAL_ERRORS\]", reply, re.IGNORECASE)
            doctor_m = re.search(r"\[SYSTEM_DOCTOR\]", reply, re.IGNORECASE)

            browser_eval_m = re.search(r"\[BROWSER_EVAL:\s*(.*?)\]", reply, re.IGNORECASE | re.DOTALL)
            browser_click_m = re.search(r"\[BROWSER_CLICK:\s*(.*?)\]", reply, re.IGNORECASE)
            browser_open_m = re.search(r"\[(?:BROWSER_OPEN|OPEN_LINK|OPEN_URL)[:\]]\s*(.*?)\]", reply, re.IGNORECASE)
            inspect_page_m = re.search(r"\[INSPECT_PAGE\]", reply, re.IGNORECASE)

            tree_m = re.search(r"\[TREE\]", reply, re.IGNORECASE)
            grep_m = re.search(r"\[GREP:\s*(.*?)\]", reply, re.IGNORECASE)
            syntax_m = re.search(r"\[CHECK_SYNTAX:\s*(.*?)\]", reply, re.IGNORECASE)
            screen_m = re.search(r"\[SCREENSHOT\]", reply, re.IGNORECASE)
            clip_r_m = re.search(r"\[CLIPBOARD_READ\]", reply, re.IGNORECASE)
            clip_w_m = re.search(r"\[CLIPBOARD_WRITE:\s*(.*?)\]", reply, re.IGNORECASE)
            send_file_m = re.search(r"\[SEND_FILE:\s*(.*?)\]", reply, re.IGNORECASE)
            read_m = re.search(r"\[READ_FILE:\s*(.*?)\]", reply, re.IGNORECASE)
            search_m = re.search(r"\[SEARCH:\s*(.*?)\]", reply, re.IGNORECASE)

            write_m = re.search(r"\[WRITE_FILE[:\]]\s*(.*?)\n(.*?)\[/WRITE_FILE\]", reply, re.DOTALL | re.IGNORECASE)
            if not write_m:
                write_m = re.search(r"\[WRITE_FILE[:\]]\s*([^\n\]]+)\n(.*)", reply, re.DOTALL | re.IGNORECASE)

            # Moderation Matchers
            guilds_m = re.search(r"\[DISCORD_GUILDS\]", reply, re.IGNORECASE)
            members_m = re.search(r"\[DISCORD_MEMBERS\]", reply, re.IGNORECASE)
            user_info_m = re.search(r"\[DISCORD_USER_INFO:\s*(.*?)\]", reply, re.IGNORECASE)
            kick_m = re.search(r"\[MOD_KICK:\s*(.*?)\|(.*?)\]", reply, re.IGNORECASE)
            ban_m = re.search(r"\[MOD_BAN:\s*(.*?)\|(.*?)\]", reply, re.IGNORECASE)
            timeout_m = re.search(r"\[MOD_TIMEOUT:\s*(.*?)\|(.*?)\|(.*?)\]", reply, re.IGNORECASE)
            purge_m = re.search(r"\[PURGE_MESSAGES:\s*(\d+)\]", reply, re.IGNORECASE)

            # Bash Tool Matcher
            cmd_to_run = None
            bash_tag = re.search(r"\[BASH:\s*(.*?)\]", reply, re.IGNORECASE) or re.search(r"\[BASH\]\s*(.*?)(?:\[/BASH\]|\n|$)", reply, re.IGNORECASE)
            if bash_tag:
                cmd_to_run = bash_tag.group(1).strip().rstrip("]")
            else:
                cb = re.search(r"```(?:bash|shell|sh)\s*\n(.*?)\n```", reply, re.DOTALL | re.IGNORECASE)
                if cb:
                    cmd_to_run = cb.group(1).strip()

            if cmd_to_run and any(cmd_to_run.strip().startswith(x) for x in ["mpc", "playerctl metadata"]):
                song_m = True
                cmd_to_run = None

            # Track loops
            current_action = None
            if spot_m:
                current_action = ("spot", spot_m.group(1).strip())
            elif browser_eval_m:
                current_action = ("b_eval", browser_eval_m.group(1).strip())
            elif browser_click_m:
                current_action = ("b_click", browser_click_m.group(1).strip())
            elif media_m:
                current_action = ("media", media_m.group(1).strip())
            elif song_m:
                current_action = ("song", "active")
            elif vol_m:
                current_action = ("vol", vol_m.group(1).strip())
            elif kdenotif_m:
                current_action = ("kdenotif", kdenotif_m.group(1).strip())
            elif aur_m:
                current_action = ("aur", aur_m.group(1).strip())
            elif journal_m:
                current_action = ("journal", "active")
            elif doctor_m:
                current_action = ("doctor", "active")
            elif screen_m:
                current_action = ("screen", "active")
            elif guilds_m:
                current_action = ("guilds", "active")
            elif purge_m:
                current_action = ("purge", purge_m.group(1).strip())
            elif kick_m:
                current_action = ("kick", kick_m.group(1).strip())
            elif ban_m:
                current_action = ("ban", ban_m.group(1).strip())
            elif timeout_m:
                current_action = ("timeout", timeout_m.group(1).strip())
            elif browser_open_m:
                current_action = ("b_open", browser_open_m.group(1).strip())
            elif write_m:
                current_action = ("write", write_m.group(1).strip())
            elif read_m:
                current_action = ("read", read_m.group(1).strip())
            elif cmd_to_run:
                current_action = ("bash", cmd_to_run)
            elif search_m:
                current_action = ("search", search_m.group(1).strip())

            if current_action:
                if recent_actions and current_action == recent_actions[-1]:
                    messages.append({"role": "assistant", "content": reply})
                    messages.append({"role": "user", "content": "[Notice]: Action already completed. Provide your final response."})
                    continue
                recent_actions.append(current_action)

            # Tool 1: Spotify Search & Play
            if spot_m:
                q = spot_m.group(1).strip().rstrip("]")
                await channel.send(f"Opening Spotify search for `{q}`...")
                out = search_and_play_spotify(q)
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[Spotify Output]: {out}"})

            # Tool 2: Browser DOM JS Evaluation
            elif browser_eval_m:
                code = browser_eval_m.group(1).strip().rstrip("]")
                await channel.send(f"Executing JavaScript in Zen Browser: `{code[:80]}...`")
                out = await execute_zen_browser_js(code)
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[Browser JS Result]: {out}"})

            # Tool 3: Browser DOM Click
            elif browser_click_m:
                sel = browser_click_m.group(1).strip().rstrip("]")
                click_js = f"document.querySelector('{sel}').click(); 'Clicked: {sel}'"
                await channel.send(f"Clicking selector in Zen Browser: `{sel}`")
                out = await execute_zen_browser_js(click_js)
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[Browser Click Result]: {out}"})

            # Tool 4: Audio Volume Control
            elif vol_m:
                val = vol_m.group(1).strip().rstrip("]")
                out = set_system_volume(val)
                await channel.send(f"Volume: {out}")
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[Volume]: {out}"})

            # Tool 5: KDE Plasma Native Notification
            elif kdenotif_m:
                ntitle = kdenotif_m.group(1).strip()
                nmsg = kdenotif_m.group(2).strip().rstrip("]")
                out = send_kde_notification(ntitle, nmsg)
                await channel.send(f"Pushed KDE desktop notification.")
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[Notification Result]: {out}"})

            # Tool 6: Search Arch AUR
            elif aur_m:
                pkg = aur_m.group(1).strip().rstrip("]")
                await channel.send(f"Searching Arch User Repository for: `{pkg}`...")
                out = await search_aur(pkg)
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[AUR Search Results]:\n{out}"})

            # Tool 7: Journal Errors
            elif journal_m:
                await channel.send("Checking recent systemd journal errors...")
                out = await get_journal_errors()
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[Journalctl Logs]:\n{out}"})

            # Tool 8: Discord Server Guilds List
            elif guilds_m:
                out = list_discord_guilds()
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[Discord Servers]:\n{out}"})

            # Tool 9: Purge Messages
            elif purge_m:
                cnt = int(purge_m.group(1).strip())
                out = await purge_channel_messages(channel, author.id, cnt)
                await channel.send(out)
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[Purge Status]: {out}"})

            # Tool 10: Kick Member
            elif kick_m:
                target_u = kick_m.group(1).strip()
                reason_u = kick_m.group(2).strip().rstrip("]")
                out = await mod_kick_user(channel.guild, author.id, target_u, reason_u)
                await channel.send(out)
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[Kick Action]: {out}"})

            # Tool 11: Ban Member
            elif ban_m:
                target_u = ban_m.group(1).strip()
                reason_u = ban_m.group(2).strip().rstrip("]")
                out = await mod_ban_user(channel.guild, author.id, target_u, reason_u)
                await channel.send(out)
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[Ban Action]: {out}"})

            # Tool 12: Timeout Member
            elif timeout_m:
                target_u = timeout_m.group(1).strip()
                minutes_u = int(timeout_m.group(2).strip())
                reason_u = timeout_m.group(3).strip().rstrip("]")
                out = await mod_timeout_user(channel.guild, author.id, target_u, minutes_u, reason_u)
                await channel.send(out)
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[Timeout Action]: {out}"})

            # Media Control
            elif media_m:
                act = media_m.group(1).strip().rstrip("]")
                await channel.send(f"Media Action: `{act}`")
                out = control_media(act)
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[Media Output]: {out}"})

            # Check Song
            elif song_m:
                await channel.send("Checking media playback...")
                out = get_current_song()
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[Media Status]: {out}"})

            # Screenshot (KDE Spectacle Native)
            elif screen_m:
                await channel.send("Capturing KDE desktop screenshot...")
                status_msg, img_path = await take_screenshot()
                if img_path:
                    await channel.send(file=discord.File(img_path))
                    messages.append({"role": "assistant", "content": reply})
                    messages.append({"role": "user", "content": "[Screenshot uploaded to Discord]"})
                else:
                    messages.append({"role": "assistant", "content": reply})
                    messages.append({"role": "user", "content": f"[Screenshot Error]: {status_msg}"})

            # Clipboard Tools
            elif clip_r_m:
                await channel.send("Reading desktop clipboard...")
                out = await read_clipboard()
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[Clipboard Content]:\n{out}"})

            elif clip_w_m:
                text_to_copy = clip_w_m.group(1).strip().rstrip("]")
                out = await write_clipboard(text_to_copy)
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[Clipboard Result]: {out}"})

            # Write File
            elif write_m:
                fname = write_m.group(1).strip().rstrip("]")
                fcontent = write_m.group(2)
                await channel.send(f"Writing file: `{fname}`")
                out = write_file_tool(fname, fcontent, author.id)
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[File Output]: {out}"})

            # Read File
            elif read_m:
                fname = read_m.group(1).strip().rstrip("]")
                await channel.send(f"Reading file: `{fname}`")
                out = read_file_tool(fname, author.id)
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[File Content]:\n{out}"})

            # Check Python Syntax
            elif syntax_m:
                fname = syntax_m.group(1).strip().rstrip("]")
                out = check_python_syntax(fname, author.id)
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[Syntax Check]:\n{out}"})

            # Directory Tree
            elif tree_m:
                out = get_project_tree(author.id)
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[Project Tree]:\n{out}"})

            # Grep
            elif grep_m:
                patt = grep_m.group(1).strip().rstrip("]")
                out = grep_codebase(patt, author.id)
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[Grep Matches]:\n{out}"})

            # System Doctor
            elif doctor_m:
                await channel.send("Running system diagnostic...")
                out = await run_system_doctor()
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[System Health Report]:\n{out}"})

            # Inspect Page
            elif inspect_page_m:
                await channel.send("Inspecting active window and webpage...")
                out = await inspect_active_page()
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[Page Info]:\n{out}"})

            # Open Browser
            elif browser_open_m and len(browser_open_m.group(1).strip().rstrip("]")) > 3:
                url_target = browser_open_m.group(1).strip().rstrip("]")
                if not url_target.startswith(("http://", "https://")):
                    url_target = "https://" + url_target
                LAST_OPENED_URL = url_target
                await channel.send(f"Opening in Zen Browser: `{url_target}`")
                subprocess.Popen(f"zen-browser '{url_target}' >/dev/null 2>&1 || xdg-open '{url_target}' >/dev/null 2>&1", shell=True)
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[Browser Event]: Opened {url_target}."})

            # Upload File
            elif send_file_m:
                f_name = send_file_m.group(1).strip().rstrip("]")
                try:
                    full_path = resolve_path(f_name, author.id)
                    if os.path.exists(full_path) and os.path.isfile(full_path):
                        await channel.send(f"Attaching `{os.path.basename(full_path)}`", file=discord.File(full_path))
                        out = f"Attached {os.path.basename(full_path)}."
                    else:
                        out = f"Error: File '{f_name}' not found."
                except PermissionError as pe:
                    out = str(pe)
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[File Attachment]: {out}"})

            # Bash Command
            elif cmd_to_run:
                if cmd_to_run.strip() in [f"cd {CURRENT_DIR}", "cd ."]:
                    out = f"Notice: Already inside {CURRENT_DIR}."
                else:
                    await channel.send(f"Running: `{cmd_to_run}`")
                    out = await execute_bash(cmd_to_run, author.id, prompt)
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[Command Output]:\n{out}"})

            # Web Search
            elif search_m:
                q = search_m.group(1).strip().rstrip("]")
                await channel.send(f"Searching web: `{q}`")
                out = await search_web(q)
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[Search Results]:\n{out}"})

            # Discord Members
            elif members_m:
                out = get_discord_members(channel.guild)
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[Members]:\n{out}"})

            # Discord User Info
            elif user_info_m:
                target_user = user_info_m.group(1).strip().rstrip("]")
                out = get_discord_user_info(channel.guild, target_user)
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[User Info]:\n{out}"})

            else:
                # Final response reached
                cleaned_reply = strip_all_emojis_nuclear(reply)
                if cleaned_reply:
                    await send_split_message(channel, cleaned_reply)

                history.append({"role": "user", "content": prompt})
                history.append({"role": "assistant", "content": cleaned_reply or reply})
                CHANNEL_HISTORY[channel.id] = history[-8:]
                return

        cleaned_reply = strip_all_emojis_nuclear(reply)
        if cleaned_reply:
            await send_split_message(channel, cleaned_reply)
        history.append({"role": "user", "content": prompt})
        history.append({"role": "assistant", "content": cleaned_reply or reply})
        CHANNEL_HISTORY[channel.id] = history[-8:]


# --- COMMANDS & EVENTS ---
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Sudo Challenge Reply Verification
    if message.reference and message.reference.message_id in pending_sudo_tokens:
        ref_id = message.reference.message_id
        challenge = pending_sudo_tokens[ref_id]

        if message.author.id == challenge["user_id"]:
            input_token = message.content.strip().upper()
            expected_token = challenge["token"]
            del pending_sudo_tokens[ref_id]

            if input_token == expected_token:
                sudo_users.add(message.author.id)
                await message.reply("Sudo privileges granted for this session.")
            else:
                await message.reply("Invalid token. Challenge burned.")
            return

    await bot.process_commands(message)


@bot.command(name="whitelist")
async def cmd_whitelist(ctx: commands.Context, action: str = None, user_arg: str = None):
    if not is_owner(ctx.author.id):
        await ctx.reply("Only the bot owner can manage the whitelist.")
        return

    if action and action.isdigit():
        user_arg = action
        action = "add"

    if not action or action.lower() == "list":
        if not whitelist:
            await ctx.reply("Whitelist is currently empty.")
        else:
            entries = "\n".join(f"- <@{uid}> (`{uid}`)" for uid in whitelist)
            await ctx.reply(f"**Whitelisted Users:**\n{entries}")
        return

    if not user_arg:
        await ctx.reply("Usage: `!whitelist add <user_id>` or `!whitelist remove <user_id>`")
        return

    clean_id = int(re.sub(r"\D", "", user_arg))
    act = action.lower()

    if act == "add":
        whitelist.add(clean_id)
        save_whitelist(whitelist)
        await ctx.reply(f"Added <@{clean_id}> to whitelist.")
    elif act in ["remove", "rm", "del"]:
        whitelist.discard(clean_id)
        sudo_users.discard(clean_id)
        save_whitelist(whitelist)
        await ctx.reply(f"Removed <@{clean_id}> from whitelist.")


@bot.command(name="allowsudo")
async def allowsudo_cmd(ctx: commands.Context):
    if is_owner(ctx.author.id):
        await ctx.reply("You are the owner and already hold permanent sudo privileges.")
        return

    token = secrets.token_hex(4).upper()
    print(f"\n[SECURITY] Sudo Token for {ctx.author.name}: >>> {token} <<<\n")
    prompt_msg = await ctx.reply("Check console for your 1-time token.")
    pending_sudo_tokens[prompt_msg.id] = {"user_id": ctx.author.id, "token": token}


@bot.command(name="servers")
async def servers_cmd(ctx: commands.Context):
    await ctx.reply(list_discord_guilds())


@bot.command(name="ask")
async def ask_cmd(ctx: commands.Context, *, prompt: str):
    await process_ai_request(ctx.channel, ctx.author, prompt)


@bot.command(name="clear")
async def clear_history_cmd(ctx: commands.Context):
    CHANNEL_HISTORY[ctx.channel.id] = []
    await ctx.send("Conversation history cleared.")


@bot.event
async def on_ready():
    global HTTP_SESSION
    if HTTP_SESSION is None or HTTP_SESSION.closed:
        connector = aiohttp.TCPConnector(limit=25, keepalive_timeout=60)
        HTTP_SESSION = aiohttp.ClientSession(connector=connector)

    # Start Zen Tampermonkey bridge server on port 5005
    asyncio.create_task(start_zen_bridge_server())

    print(f"\033[32m[✓] Bot online: {bot.user} (Owner ID: {OWNER_USER_ID})\033[0m")
    print(f"\033[36m[✓] KDE Plasma Desktop environment integration enabled\033[0m")
    try:
        await bot.change_presence(
            status=discord.Status.dnd,
            activity=discord.CustomActivity(name="estrogen is yummy mmmm~"),
        )
    except Exception:
        pass


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
