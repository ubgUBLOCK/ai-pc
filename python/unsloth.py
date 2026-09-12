#!/usr/bin/env python3
import asyncio
import datetime
import json
import logging
import os
import platform
import re
import secrets
import socket
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Optional, Set

import aiohttp
from aiohttp import web
import discord
from discord.ext import commands
from dotenv import load_dotenv

# --- SUPPRESS DISCORD LOGGERS & HIDE SESSION ID ---
logging.getLogger("discord.gateway").setLevel(logging.WARNING)
logging.getLogger("discord.client").setLevel(logging.WARNING)

# --- CONFIGURATION ---
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OWNER_USER_ID = int(os.getenv("OWNER_ID", os.getenv("OWNER_USER_ID", "1468441445525491723")))
WORKSPACE_DIR = os.path.realpath(os.getenv("WORKSPACE_DIR", "./workspace"))
SUDO_PASSWORD = os.getenv("SUDO_PASSWORD", None)
LOG_CHANNEL_ID = 1548117556244521100

UNSLOTH_API_URL = os.getenv("UNSLOTH_API_URL", "")
UNSLOTH_API_KEY = os.getenv("UNSLOTH_API_KEY", "")
MODEL_NAME = "default"

WHITELIST_FILE = Path("whitelist.json")
MEMORY_FILE = Path("memory.json")
VENV_DIR = os.path.join(WORKSPACE_DIR, ".venv")

os.makedirs(WORKSPACE_DIR, exist_ok=True)

CURRENT_DIR = WORKSPACE_DIR
CHANNEL_HISTORY: Dict[int, list] = {}
LAST_OPENED_URL = "https://www.youtube.com"

sudo_users: Set[int] = set()
pending_sudo_tokens: Dict[int, Dict[str, Any]] = {}

HTTP_SESSION: Optional[aiohttp.ClientSession] = None

# Zen Browser Bridge State
zen_command_queue: asyncio.Queue = asyncio.Queue()
zen_results: Dict[str, asyncio.Future] = {}
zen_latest_url: str = LAST_OPENED_URL
zen_latest_title: str = "Unknown"

discord_log_queue: asyncio.Queue = asyncio.Queue()

if not DISCORD_TOKEN:
    raise ValueError("CRITICAL: DISCORD_TOKEN is not set in your .env file.")


# --- DUAL LOGGING SYSTEM (CONSOLE + DISCORD CHANNEL) ---
class Log:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"

    @staticmethod
    def _timestamp() -> str:
        return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]

    @classmethod
    def _dispatch(cls, console_str: str, clean_str: str):
        print(console_str)
        try:
            discord_log_queue.put_nowait(clean_str)
        except Exception:
            pass

    @classmethod
    def info(cls, msg: str):
        t = cls._timestamp()
        cls._dispatch(f"{cls.DIM}[{t}]{cls.RESET} {cls.GREEN}[INFO]{cls.RESET} {msg}", f"[{t}] [INFO] {msg}")

    @classmethod
    def llm_send(cls, turn: int, msg_count: int, target_url: str):
        t = cls._timestamp()
        cls._dispatch(
            f"{cls.DIM}[{t}]{cls.RESET} {cls.CYAN}{cls.BOLD}[LLM REQ >>]{cls.RESET} Turn #{turn} | Dispatching {msg_count} msgs to {target_url}",
            f"[{t}] [LLM REQ >>] Turn #{turn} | {msg_count} msgs sent",
        )

    @classmethod
    def llm_recv(cls, status: int, elapsed_s: float, tokens_preview: str):
        t = cls._timestamp()
        color = cls.GREEN if status == 200 else cls.RED
        cls._dispatch(
            f"{cls.DIM}[{t}]{cls.RESET} {color}{cls.BOLD}[LLM RES <<]{cls.RESET} HTTP {status} ({elapsed_s:.2f}s) | {cls.DIM}{tokens_preview[:90]}...{cls.RESET}",
            f"[{t}] [LLM RES <<] HTTP {status} ({elapsed_s:.2f}s) | {tokens_preview[:80]}",
        )

    @classmethod
    def tool(cls, tool_name: str, payload: str):
        t = cls._timestamp()
        cls._dispatch(
            f"{cls.DIM}[{t}]{cls.RESET} {cls.YELLOW}{cls.BOLD}[TOOL RUN]{cls.RESET} {cls.BOLD}{tool_name}{cls.RESET} -> {payload[:110]}",
            f"[{t}] [TOOL RUN] {tool_name} -> {payload[:90]}",
        )

    @classmethod
    def tool_out(cls, tool_name: str, output: str):
        t = cls._timestamp()
        snippet = output.strip().replace("\n", " ")[:70]
        cls._dispatch(
            f"{cls.DIM}[{t}]{cls.RESET} {cls.MAGENTA}[TOOL DONE]{cls.RESET} {tool_name} returned {len(output)} chars: {cls.DIM}{snippet}...{cls.RESET}",
            f"[{t}] [TOOL DONE] {tool_name} -> {snippet}",
        )

    @classmethod
    def zen(cls, action: str, details: str):
        t = cls._timestamp()
        cls._dispatch(f"{cls.DIM}[{t}]{cls.RESET} {cls.BLUE}[ZEN BRIDGE]{cls.RESET} {action}: {cls.DIM}{details[:100]}{cls.RESET}", f"[{t}] [ZEN BRIDGE] {action}: {details[:80]}")

    @classmethod
    def warn(cls, msg: str):
        t = cls._timestamp()
        cls._dispatch(f"{cls.DIM}[{t}]{cls.RESET} {cls.YELLOW}[WARN]{cls.RESET} {msg}", f"[{t}] [WARN] {msg}")

    @classmethod
    def err(cls, msg: str):
        t = cls._timestamp()
        cls._dispatch(f"{cls.DIM}[{t}]{cls.RESET} {cls.RED}{cls.BOLD}[ERROR]{cls.RESET} {msg}", f"[{t}] [ERROR] {msg}")


# --- BACKGROUND LOG STREAMER TO DISCORD ---
async def discord_log_dispatcher():
    await bot.wait_until_ready()
    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if not log_channel:
        try:
            log_channel = await bot.fetch_channel(LOG_CHANNEL_ID)
        except Exception as e:
            print(f"\033[31m[!] Could not bind log channel ID {LOG_CHANNEL_ID}: {e}\033[0m")
            return

    buffer = []
    while not bot.is_closed():
        try:
            item = await asyncio.wait_for(discord_log_queue.get(), timeout=1.5)
            buffer.append(item)
            while not discord_log_queue.empty() and len(buffer) < 20:
                buffer.append(discord_log_queue.get_nowait())
        except asyncio.TimeoutError:
            pass

        if buffer:
            chunk = "\n".join(buffer)
            buffer.clear()
            for i in range(0, len(chunk), 1850):
                sub = chunk[i : i + 1850]
                try:
                    await log_channel.send(f"```ini\n{sub}\n```")
                except Exception:
                    pass
        await asyncio.sleep(0.5)


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


def resolve_path(path_str: str, user_id: int) -> str:
    clean = path_str.strip().strip("'\"")
    expanded = os.path.expanduser(clean)
    target = os.path.realpath(expanded) if os.path.isabs(expanded) else os.path.realpath(os.path.join(CURRENT_DIR, expanded))

    if not is_owner(user_id):
        real_ws = os.path.realpath(WORKSPACE_DIR)
        try:
            common = os.path.commonpath([target, real_ws])
        except ValueError:
            raise PermissionError(f"Access Denied: Path '{path_str}' escapes root.")

        if common != real_ws:
            raise PermissionError(f"Access Denied: Path '{path_str}' is outside workspace.")

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
        raise PermissionError("Access Denied: Rogue package installation blocked.")


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


HOSTNAME = socket.gethostname()
KERNEL = platform.release()

# --- REFINED & PROTECTED STATIC SYSTEM PROMPT ---
STATIC_SYSTEM_PROMPT = f"""You are the 'estrogen goober', an elite Arch Linux companion and KDE Plasma operator.
Primary Workspace: {WORKSPACE_DIR}
OS: Arch Linux x86_64 | Desktop: KDE Plasma | Hostname: {HOSTNAME} | Kernel: {KERNEL}

CRITICAL RULES:
- FOR ANY MUSIC/PLAYBACK REQUEST (skip, pause, play, next, previous): ONLY emit [MEDIA_CONTROL: action]. NEVER search web, NEVER change volume, NEVER run bash.
- DO NOT hallucinate multiple tool calls. Emit EXACTLY ONE tool tag if needed, then wait.
- NEVER run package managers (pacman, yay, paru).
- FAST & CONCISE. No emojis.

TOOLS:
1. Media Control: [MEDIA_CONTROL: play|pause|play-pause|next|previous|stop]
2. Check Song: [CHECK_SONG]
3. System Volume: [SET_VOLUME: 0-100]
4. Open URL: [BROWSER_OPEN: https://...]
5. Close Active Tab: [BROWSER_CLOSE_TAB]
6. Inspect Active Page: [INSPECT_PAGE]
7. Desktop Screenshot: [SCREENSHOT]
8. Read/Write Clipboard: [CLIPBOARD_READ] / [CLIPBOARD_WRITE: text]
9. Desktop Notification: [KDE_NOTIFY: title | message]
10. System Doctor: [SYSTEM_DOCTOR]
11. Journal Errors: [JOURNAL_ERRORS]
12. Search AUR: [ARCH_AUR_SEARCH: query]
13. Bash Command: [BASH: command]
14. Web Search: [SEARCH: query]
15. Write File: [WRITE_FILE: relative_filename]\ncontent\n[/WRITE_FILE]
16. Read File: [READ_FILE: relative_filename]
17. Check Syntax: [CHECK_SYNTAX: filename.py]
18. Project Tree: [TREE]
19. Code Grep: [GREP: search_pattern]
20. Send File: [SEND_FILE: relative_filename]
21. Discord Servers: [DISCORD_GUILDS]
22. Discord Members: [DISCORD_MEMBERS]
23. Discord User Info: [DISCORD_USER_INFO: user_id_or_name]
24. Discord Kick: [MOD_KICK: user_id | reason]
25. Discord Ban: [MOD_BAN: user_id | reason]
26. Discord Timeout: [MOD_TIMEOUT: user_id | minutes | reason]
27. Purge Messages: [PURGE_MESSAGES: count]
"""


def build_dynamic_context(user_display_name: str, is_user_owner: bool) -> str:
    role = "OWNER (Full Authority)" if is_user_owner else "WHITELISTED GUEST"
    return (
        f"[CURRENT CONTEXT]\n"
        f"User: {user_display_name} ({role})\n"
        f"Active PWD: {CURRENT_DIR}\n"
        f"Zen Browser Active Title: {zen_latest_title}\n"
        f"[END CONTEXT]"
    )


intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.check
async def global_whitelist_check(ctx: commands.Context):
    return is_owner(ctx.author.id) or ctx.author.id in whitelist


# --- ZEN BROWSER BRIDGE ---
async def zen_poll_handler(request: web.Request) -> web.Response:
    try:
        cmd_item = await asyncio.wait_for(zen_command_queue.get(), timeout=1.2)
        Log.zen("POLL DISPATCH", json.dumps(cmd_item))
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
    Log.info("Zen Browser Bridge listening on http://127.0.0.1:5005")


async def execute_zen_browser_js(js_code: str, timeout: float = 3.0) -> str:
    cid = secrets.token_hex(4)
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    zen_results[cid] = fut
    await zen_command_queue.put({"id": cid, "code": js_code})
    try:
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        zen_results.pop(cid, None)
        return "Browser execution timed out."


# --- SYSTEM TOOLS ---
def control_media(action: str) -> str:
    action = action.strip().lower()
    alias_map = {
        "unpause": "play",
        "resume": "play",
        "toggle": "play-pause",
        "skip": "next",
        "skip song": "next",
        "next song": "next",
        "prev": "previous",
        "previous song": "previous",
    }
    action = alias_map.get(action, action)
    if action not in ["play", "pause", "play-pause", "next", "previous", "stop"]:
        action = "next"
    try:
        subprocess.run(f"playerctl {action}", shell=True, timeout=2, capture_output=True)
        return f"Media command '{action}' executed."
    except Exception as e:
        return f"Media error: {e}"


def get_current_song() -> str:
    try:
        cmd = "playerctl metadata --format '{{title}} by {{artist}}' 2>/dev/null || playerctl metadata 2>/dev/null"
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=2)
        out = res.stdout.strip()
        return out if out else "No active media detected on the system."
    except Exception as e:
        return f"Playback detection error: {e}"


def set_system_volume(level: str) -> str:
    clean = re.sub(r"[^\d]", "", level)
    if not clean:
        return "Error: Volume must be between 0 and 100."
    vol = max(0, min(100, int(clean)))
    subprocess.run(f"pactl set-sink-volume @DEFAULT_SINK@ {vol}% 2>/dev/null || amixer set Master {vol}% 2>/dev/null", shell=True)
    return f"Master volume set to {vol}%."


def send_kde_notification(title: str, msg: str) -> str:
    t = title.replace("'", "")
    m = msg.replace("'", "")
    subprocess.run(f"kdialog --title '{t}' --passivepopup '{m}' 5 2>/dev/null || notify-send '{t}' '{m}' 2>/dev/null", shell=True)
    return f"Dispatched KDE notification: [{t}] {m}"


async def close_browser_tab() -> str:
    y_cmd = "ydotool key 29:1 17:1 17:0 29:0 2>/dev/null"
    x_cmd = "xdotool search --onlyvisible --class 'zen' windowactivate --sync key ctrl+w 2>/dev/null"
    proc = await asyncio.create_subprocess_shell(f"{y_cmd} || {x_cmd}")
    await proc.communicate()
    return "Closed active browser tab."


async def take_screenshot() -> tuple[str, Optional[str]]:
    path = os.path.join(WORKSPACE_DIR, ".screenshot.png")
    cmd = f"spectacle -b -n -o '{path}' 2>/dev/null || grim '{path}' 2>/dev/null"
    proc = await asyncio.create_subprocess_shell(cmd)
    await proc.communicate()
    if os.path.exists(path) and os.path.getsize(path) > 1000:
        return "Screenshot captured.", path
    return "Screenshot failed.", None


async def read_clipboard() -> str:
    cmd = "wl-paste 2>/dev/null || xclip -selection clipboard -o 2>/dev/null"
    proc = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE)
    stdout, _ = await proc.communicate()
    out = stdout.decode("utf-8", errors="ignore").strip()
    return out if out else "(Clipboard is empty)"


async def write_clipboard(text: str) -> str:
    proc = await asyncio.create_subprocess_shell("wl-copy 2>/dev/null || xclip -selection clipboard 2>/dev/null", stdin=asyncio.subprocess.PIPE)
    await proc.communicate(input=text.encode("utf-8"))
    return f"Copied to clipboard: {text[:80]}"


async def run_system_doctor() -> str:
    cmd = (
        "echo '=== FAILED SERVICES ===' && (systemctl --failed --no-legend 2>/dev/null || echo 'None') && "
        "echo '=== DISK SPACE ===' && df -h / | awk 'NR==2 {print $3 \" used out of \" $2 \" (\" $5 \")\"}' && "
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
    return out if out else "No systemd errors in the last 15 minutes."


async def search_aur(query: str) -> str:
    url = f"https://aur.archlinux.org/rpc/?v=5&type=search&arg={urllib.parse.quote(query)}"
    try:
        async with HTTP_SESSION.get(url, timeout=5) as resp:
            if resp.status == 200:
                data = await resp.json()
                results = data.get("results", [])
                if not results:
                    return f"No AUR packages found for '{query}'."
                lines = [f"AUR Results for '{query}':"]
                for p in results[:4]:
                    lines.append(f"- **{p.get('Name')}** ({p.get('Version')}): {p.get('Description', 'No description')}")
                return "\n".join(lines)
    except Exception as e:
        return f"AUR error: {e}"
    return "No AUR results found."


async def search_web(query: str) -> str:
    url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"}
    try:
        async with HTTP_SESSION.get(url, headers=headers, timeout=6) as resp:
            if resp.status == 200:
                html = await resp.text()
                snippets = re.findall(r'<a class="result__snippet[^>]*>(.*?)</a>', html, re.DOTALL)
                clean = [re.sub(r"<[^>]+>", "", s).strip() for s in snippets if len(s) > 20]
                if clean:
                    return "[Search Results]:\n" + "\n".join(f"- {s}" for s in clean[:4])
    except Exception as e:
        Log.err(f"Search error: {e}")
    return "No search results found."


def write_file_tool(path_str: str, content: str, user_id: int) -> str:
    try:
        full_path = resolve_path(path_str, user_id)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        clean = re.sub(r"^```[a-zA-Z0-9_\-\+]*\n", "", content.strip())
        clean = re.sub(r"\n```$", "", clean)
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(clean)
        return f"Wrote {len(clean.splitlines())} lines to `{full_path}`"
    except Exception as e:
        return f"Error writing file: {e}"


def read_file_tool(path_str: str, user_id: int) -> str:
    try:
        full_path = resolve_path(path_str, user_id)
        if not os.path.exists(full_path):
            return f"Error: File '{full_path}' does not exist."
        with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        return "".join([f"{i + 1:3d} | {line}" for i, line in enumerate(lines[:100])])
    except Exception as e:
        return f"Error reading file: {e}"


def check_python_syntax(path_str: str, user_id: int) -> str:
    try:
        full_path = resolve_path(path_str, user_id)
    except PermissionError as pe:
        return str(pe)
    if not os.path.exists(full_path):
        return f"File '{path_str}' does not exist."
    res = subprocess.run([sys.executable, "-m", "py_compile", full_path], capture_output=True, text=True)
    return "Syntax OK." if res.returncode == 0 else f"Syntax Error:\n{res.stderr.strip()}"


def grep_codebase(pattern: str, user_id: int) -> str:
    search_dir = CURRENT_DIR if is_owner(user_id) else WORKSPACE_DIR
    cmd = f"grep -rnI --exclude-dir='.venv' --exclude-dir='.git' '{pattern}' '{search_dir}'"
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=4)
    lines = res.stdout.strip().splitlines()
    return "\n".join(lines[:25]) if lines else f"No occurrences of '{pattern}' found."


def get_project_tree(user_id: int) -> str:
    target_dir = CURRENT_DIR if is_owner(user_id) else WORKSPACE_DIR
    cmd = f"tree -L 2 -I '.venv|.git|__pycache__' '{target_dir}' 2>/dev/null || find '{target_dir}' -maxdepth 2 | head -n 30"
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=2)
    return res.stdout.strip() if res.stdout.strip() else "(Directory is empty)"


async def inspect_active_page() -> str:
    eval_res = await execute_zen_browser_js("document.title + ' | ' + window.location.href", timeout=1.5)
    return f"Active Browser Page: {eval_res}"


# --- DISCORD MANAGEMENT TOOLS ---
def list_discord_guilds() -> str:
    return "\n".join(f"- **{g.name}** (ID: `{g.id}`) | {g.member_count} members" for g in bot.guilds)


def get_discord_members(guild) -> str:
    if not guild:
        return "Error: Cannot fetch members outside of a server."
    return "\n".join(f"- {m.name} (Nick: {m.display_name}, ID: `{m.id}`)" for m in guild.members[:30])


def get_discord_user_info(guild, query: str) -> str:
    if not guild:
        return "Cannot fetch user info outside a server."
    clean = re.sub(r"[<@!>]", "", query).strip().lower()
    target = next((m for m in guild.members if str(m.id) == clean or m.name.lower() == clean), None)
    if not target:
        return f"User '{query}' not found."
    roles = [r.name for r in target.roles if r.name != "@everyone"] or ["None"]
    return f"User: {target.name} | ID: {target.id} | Roles: {', '.join(roles)}"


async def mod_kick_user(guild, author_id: int, user_arg: str, reason: str) -> str:
    if not is_owner(author_id):
        return "Permission Denied."
    member = guild.get_member(int(re.sub(r"\D", "", user_arg)))
    if not member:
        return f"User {user_arg} not found."
    await member.kick(reason=reason)
    return f"Kicked {member.name} ({member.id})."


async def mod_ban_user(guild, author_id: int, user_arg: str, reason: str) -> str:
    if not is_owner(author_id):
        return "Permission Denied."
    clean_id = int(re.sub(r"\D", "", user_arg))
    await guild.ban(discord.Object(id=clean_id), reason=reason)
    return f"Banned user ID {clean_id}."


async def mod_timeout_user(guild, author_id: int, user_arg: str, minutes: int, reason: str) -> str:
    if not is_owner(author_id):
        return "Permission Denied."
    member = guild.get_member(int(re.sub(r"\D", "", user_arg)))
    if not member:
        return f"User {user_arg} not found."
    await member.timeout(datetime.timedelta(minutes=minutes), reason=reason)
    return f"Timed out {member.name} for {minutes}m."


async def purge_channel_messages(channel, author_id: int, limit: int) -> str:
    if not is_owner(author_id):
        return "Permission Denied."
    deleted = await channel.purge(limit=max(1, min(100, limit)) + 1)
    return f"Purged {len(deleted) - 1} messages."


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
            return "⛔ Sudo is locked."
        if SUDO_PASSWORD:
            cmd = re.sub(r"\bsudo\b", "sudo -S", cmd)

    env = os.environ.copy()
    venv_bin = os.path.join(VENV_DIR, "bin")
    if os.path.exists(venv_bin):
        env["PATH"] = f"{venv_bin}:{env.get('PATH', '')}"

    wrapped = f"{cmd}\n__EXIT_CODE__=$?\necho '___PWD___:'$(pwd)\nexit $__EXIT_CODE__"
    try:
        proc = await asyncio.create_subprocess_shell(
            wrapped,
            stdin=asyncio.subprocess.PIPE if (needs_sudo and SUDO_PASSWORD) else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=CURRENT_DIR,
            env=env,
            executable="/bin/bash",
        )
        inp = f"{SUDO_PASSWORD}\n".encode() if (needs_sudo and SUDO_PASSWORD) else None
        stdout, stderr = await asyncio.wait_for(proc.communicate(input=inp), timeout=30)
        out_raw = stdout.decode("utf-8", errors="ignore")
        err = stderr.decode("utf-8", errors="ignore").strip()

        pwd_m = re.search(r"___PWD___:(.*)", out_raw)
        if pwd_m:
            new_dir = pwd_m.group(1).strip()
            if os.path.isdir(new_dir) and (is_owner(user_id) or os.path.commonpath([os.path.realpath(new_dir), os.path.realpath(WORKSPACE_DIR)]) == os.path.realpath(WORKSPACE_DIR)):
                CURRENT_DIR = new_dir
            out = re.sub(r"___PWD___:.*", "", out_raw).strip()
        else:
            out = out_raw.strip()

        return f"{out}\n{err}".strip() if err else (out if out else "(Completed)")
    except Exception as e:
        return f"Execution error: {e}"


# --- LLM API CALL ---
async def call_unsloth_api(messages: list, turn: int = 1) -> str:
    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "temperature": 0.1,
        "top_p": 0.9,
        "max_tokens": 512,
        "frequency_penalty": 0.2,
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {UNSLOTH_API_KEY}",
        "Content-Type": "application/json",
        "Connection": "keep-alive",
    }

    Log.llm_send(turn, len(messages), UNSLOTH_API_URL)
    t_start = time.time()
    try:
        async with HTTP_SESSION.post(UNSLOTH_API_URL, json=payload, headers=headers, timeout=45) as resp:
            elapsed = time.time() - t_start
            if resp.status != 200:
                raw_err = await resp.text()
                Log.llm_recv(resp.status, elapsed, f"FAIL: {raw_err}")
                return f"Unsloth Error: {raw_err}"
            data = await resp.json()
            content = data["choices"][0]["message"]["content"]
            Log.llm_recv(resp.status, elapsed, content.replace("\n", " ").strip())
            return content
    except Exception as e:
        elapsed = time.time() - t_start
        Log.err(f"LLM Network Error ({elapsed:.2f}s): {e}")
        return f"LLM Error: {e}"


async def send_split_message(destination, text: str):
    cleaned = strip_all_emojis_nuclear(text) or "Done."
    for i in range(0, len(cleaned), 1900):
        await destination.send(cleaned[i : i + 1900])


# --- CORE AGENT PIPELINE ---
async def process_ai_request(channel, author, prompt: str):
    global LAST_OPENED_URL
    Log.info(f"Prompt from {author.name}: '{prompt}'")

    async with channel.typing():
        clean_p = prompt.strip().lower()

        # FAST PATH 1: Instant Media Actions (Bypasses LLM completely)
        media_triggers = {
            "skip": "next",
            "skip song": "next",
            "next": "next",
            "next song": "next",
            "skip the music": "next",
            "pause": "pause",
            "pause music": "pause",
            "pause song": "pause",
            "stop": "stop",
            "play": "play",
            "resume": "play",
            "unpause": "play",
            "previous": "previous",
            "prev": "previous",
            "prev song": "previous",
        }
        if clean_p in media_triggers or any(clean_p.startswith(k) for k in ["skip song", "skip the song", "next song"]):
            action = media_triggers.get(clean_p, "next")
            Log.tool("FAST_PATH_MEDIA", action)
            out = control_media(action)
            await channel.send(f"Media Action: `{action}`")
            return

        # FAST PATH 2: Instant Song Checking
        if any(t in clean_p for t in ["what song", "what's playing", "whats playing", "current song", "now playing", "check song"]):
            Log.tool("FAST_PATH_CHECK_SONG", prompt)
            out = get_current_song()
            await channel.send(f"Now playing: {out}")
            return

        # FAST PATH 3: Common Terminal Commands
        first_token = clean_p.split()[0].lower() if clean_p else ""
        if first_token in ["ls", "dir", "pwd", "cd", "whoami", "uname", "uptime", "tree", "free", "df", "fastfetch"]:
            target_cmd = "ls -la" if clean_p in ["dir", "ls"] else ("uptime -p" if clean_p == "uptime" else clean_p)
            Log.tool("FAST_PATH_BASH", target_cmd)
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

        # Process Turns (Capped to 3 turns to stop hallucination spams)
        for turn_idx in range(1, 4):
            reply = await call_unsloth_api(messages, turn=turn_idx)
            reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.DOTALL).strip()

            # Tool Matchers
            media_m = re.search(r"\[MEDIA_CONTROL:\s*(.*?)\]", reply, re.IGNORECASE)
            song_m = re.search(r"\[CHECK_SONG\]", reply, re.IGNORECASE)
            vol_m = re.search(r"\[SET_VOLUME:\s*(.*?)\]", reply, re.IGNORECASE)
            kdenotif_m = re.search(r"\[KDE_NOTIFY:\s*(.*?)\|(.*?)\]", reply, re.IGNORECASE)
            aur_m = re.search(r"\[ARCH_AUR_SEARCH:\s*(.*?)\]", reply, re.IGNORECASE)
            journal_m = re.search(r"\[JOURNAL_ERRORS\]", reply, re.IGNORECASE)
            doctor_m = re.search(r"\[SYSTEM_DOCTOR\]", reply, re.IGNORECASE)
            browser_close_m = re.search(r"\[BROWSER_CLOSE_TAB\]", reply, re.IGNORECASE)
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

            guilds_m = re.search(r"\[DISCORD_GUILDS\]", reply, re.IGNORECASE)
            members_m = re.search(r"\[DISCORD_MEMBERS\]", reply, re.IGNORECASE)
            user_info_m = re.search(r"\[DISCORD_USER_INFO:\s*(.*?)\]", reply, re.IGNORECASE)
            kick_m = re.search(r"\[MOD_KICK:\s*(.*?)\|(.*?)\]", reply, re.IGNORECASE)
            ban_m = re.search(r"\[MOD_BAN:\s*(.*?)\|(.*?)\]", reply, re.IGNORECASE)
            timeout_m = re.search(r"\[MOD_TIMEOUT:\s*(.*?)\|(.*?)\|(.*?)\]", reply, re.IGNORECASE)
            purge_m = re.search(r"\[PURGE_MESSAGES:\s*(\d+)\]", reply, re.IGNORECASE)

            cmd_to_run = None
            bash_tag = re.search(r"\[BASH:\s*(.*?)\]", reply, re.IGNORECASE)
            if bash_tag:
                cmd_to_run = bash_tag.group(1).strip().rstrip("]")

            # Media Priority
            if media_m:
                act = media_m.group(1).strip().rstrip("]")
                Log.tool("MEDIA_CONTROL", act)
                out = control_media(act)
                await channel.send(f"Media Action: `{act}`")
                history.append({"role": "user", "content": prompt})
                history.append({"role": "assistant", "content": f"Executed media command '{act}'."})
                CHANNEL_HISTORY[channel.id] = history[-8:]
                return

            elif song_m:
                Log.tool("CHECK_SONG", "Invoked")
                out = get_current_song()
                await channel.send(f"Now playing: {out}")
                history.append({"role": "user", "content": prompt})
                history.append({"role": "assistant", "content": out})
                CHANNEL_HISTORY[channel.id] = history[-8:]
                return

            elif browser_close_m:
                Log.tool("BROWSER_CLOSE_TAB", "Invoked")
                out = await close_browser_tab()
                await channel.send(out)
                return

            elif vol_m:
                val = vol_m.group(1).strip().rstrip("]")
                out = set_system_volume(val)
                await channel.send(out)
                return

            elif screen_m:
                _, img_path = await take_screenshot()
                if img_path:
                    await channel.send(file=discord.File(img_path))
                return

            elif search_m:
                q = search_m.group(1).strip().rstrip("]")
                Log.tool("SEARCH", q)
                await channel.send(f"Searching web: `{q}`")
                out = await search_web(q)
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[Search Results]:\n{out}"})
                continue

            elif doctor_m:
                out = await run_system_doctor()
                await channel.send(f"```\n{out}\n```")
                return

            elif guilds_m:
                out = list_discord_guilds()
                await channel.send(out)
                return

            elif purge_m:
                cnt = int(purge_m.group(1).strip())
                out = await purge_channel_messages(channel, author.id, cnt)
                await channel.send(out)
                return

            elif cmd_to_run:
                Log.tool("BASH", cmd_to_run)
                out = await execute_bash(cmd_to_run, author.id, prompt)
                await channel.send(f"```\n{out}\n```")
                return

            else:
                cleaned_reply = strip_all_emojis_nuclear(reply)
                if not cleaned_reply:
                    cleaned_reply = "Hey. System's ready. What are we doing?"
                await send_split_message(channel, cleaned_reply)
                history.append({"role": "user", "content": prompt})
                history.append({"role": "assistant", "content": cleaned_reply})
                CHANNEL_HISTORY[channel.id] = history[-8:]
                return

        cleaned_reply = strip_all_emojis_nuclear(reply) or "Done."
        await send_split_message(channel, cleaned_reply)
        history.append({"role": "user", "content": prompt})
        history.append({"role": "assistant", "content": cleaned_reply})
        CHANNEL_HISTORY[channel.id] = history[-8:]


# --- COMMANDS ---
@bot.command(name="whitelist")
async def cmd_whitelist(ctx: commands.Context, action: str = None, user_arg: str = None):
    if not is_owner(ctx.author.id):
        await ctx.reply("Only the bot owner can manage the whitelist.")
        return
    if action and action.isdigit():
        user_arg = action
        action = "add"
    if not action or action.lower() == "list":
        entries = "\n".join(f"- <@{uid}> (`{uid}`)" for uid in whitelist) if whitelist else "Whitelist empty."
        await ctx.reply(entries)
        return
    clean_id = int(re.sub(r"\D", "", user_arg))
    if action.lower() == "add":
        whitelist.add(clean_id)
        save_whitelist(whitelist)
        await ctx.reply(f"Added <@{clean_id}>.")
    else:
        whitelist.discard(clean_id)
        save_whitelist(whitelist)
        await ctx.reply(f"Removed <@{clean_id}>.")


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
        HTTP_SESSION = aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=25, keepalive_timeout=60))

    asyncio.create_task(start_zen_bridge_server())
    asyncio.create_task(discord_log_dispatcher())

    Log.info(f"Bot online: {bot.user} (Owner ID: {OWNER_USER_ID})")
    try:
        await bot.change_presence(status=discord.Status.dnd, activity=discord.CustomActivity(name="meow | discord.gg/WEHmFYbbh | Made by lea  "))
    except Exception:
        pass


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
