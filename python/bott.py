#!/usr/bin/env python3
import asyncio
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
from typing import Any, Dict, Set

import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv

# --- CONFIGURATION FROM .ENV ---
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OWNER_USER_ID = int(os.getenv("OWNER_ID", os.getenv("OWNER_USER_ID", "1468441445525491723")))
LM_STUDIO_URL = os.getenv("LM_STUDIO_URL", "http://127.0.0.1:1234/v1/chat/completions")
WORKSPACE_DIR = os.path.realpath(os.getenv("WORKSPACE_DIR", "./workspace"))
MODEL_NAME = os.getenv("MODEL_NAME", "local-model")
SUDO_PASSWORD = os.getenv("SUDO_PASSWORD", None)

WHITELIST_FILE = Path("whitelist.json")
MEMORY_FILE = Path("memory.json")
VENV_DIR = os.path.join(WORKSPACE_DIR, ".venv")

os.makedirs(WORKSPACE_DIR, exist_ok=True)

CURRENT_DIR = WORKSPACE_DIR
CHANNEL_HISTORY = {}
LAST_OPENED_URL = "https://www.youtube.com"

# Per-user active sudo and 1-time challenge tokens
sudo_users: Set[int] = set()
# Map: {reply_message_id: {"user_id": int, "token": str}}
pending_sudo_tokens: Dict[int, Dict[str, Any]] = {}

if not DISCORD_TOKEN:
    raise ValueError("CRITICAL: DISCORD_TOKEN is not set in your .env file.")


# --- AUTO-PROVISION VIRTUAL ENVIRONMENT ---
def ensure_workspace_venv():
    if not os.path.exists(os.path.join(VENV_DIR, "bin", "python")):
        try:
            subprocess.run(
                [sys.executable, "-m", "venv", VENV_DIR],
                check=True,
                capture_output=True,
            )
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
    """
    Resolves relative or absolute paths against WORKSPACE_DIR.
    Enforces strict jailing inside WORKSPACE_DIR unless caller is the Owner.
    """
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


def validate_bash_command(cmd: str, user_id: int):
    """Blocks directory escape patterns for non-owner users."""
    if is_owner(user_id):
        return

    if ".." in cmd:
        raise PermissionError("Access Denied: Parent directory traversal ('..') is forbidden.")

    restricted_roots = ["/etc", "/home", "/var", "/usr", "/bin", "/sbin", "/root", "/boot", "~"]
    for r in restricted_roots:
        if re.search(rf"(?:^|\s){re.escape(r)}(?:/|\s|$)", cmd):
            raise PermissionError(f"Access Denied: Direct reference to '{r}' outside workspace is forbidden.")


# --- COMPLETE EMOJI & SMILEY & TAG STRIPPER ---
def strip_all_emojis_nuclear(text: str) -> str:
    cleaned = re.sub(
        r"[\U00010000-\U0010ffff\u2600-\u27bf\u2300-\u23ff\u2b50\u3030\ufe0f\u200d\u200e\u200b]",
        "",
        text,
    )
    cleaned = re.sub(r"(?:\s|^)[:=]-?[)DdpP3](\s|$)", " ", cleaned)
    # Strip LLM chatter prefixes, user tags, and [STATUS] wrappers
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


def add_memory(fact: str) -> str:
    fact = fact.strip()
    if fact and fact not in LONG_TERM_MEMORIES:
        LONG_TERM_MEMORIES.append(fact)
        save_memories()
        return f"Memorized: '{fact}'"
    return "Fact already memorized."


# --- SYSTEM PROMPT ---
def build_system_prompt(user_display_name: str, is_user_owner: bool) -> str:
    memory_section = ""
    if LONG_TERM_MEMORIES:
        memory_section = (
            "LONG-TERM MEMORY:\n"
            + "\n".join(f"- {m}" for m in LONG_TERM_MEMORIES[-15:])
            + "\n\n"
        )

    user_context = (
        f"Active Operator: {user_display_name} (CREATOR / OWNER - FULL ACCESS)"
        if is_user_owner
        else f"Active Operator: {user_display_name} (WHITELISTED GUEST)"
    )

    return (
        "[SYSTEM INSTRUCTIONS]\n"
        "You are the 'estrogen goober', an elite Arch Linux engineering companion and desktop operator.\n"
        f"{user_context}\n\n"
        f"Active Directory: {CURRENT_DIR}\n"
        f"Designated Workspace Root: {WORKSPACE_DIR}\n"
        f"Workspace Virtualenv: Active at {VENV_DIR} (pip & python use this)\n"
        f"Current Webpage: {LAST_OPENED_URL}\n\n"
        "SYSTEM SPECS:\n"
        f"- Local IP: {LOCAL_IP}\n"
        f"- Hostname: {HOSTNAME}\n"
        f"- Kernel: {KERNEL}\n"
        "- OS: Arch Linux x86_64\n\n"
        f"{memory_section}"
        "STRICT WORKSPACE CONFINEMENT:\n"
        "- All file writes, reads, checks, and shell operations MUST be confined to WORKSPACE_DIR.\n"
        "- NEVER attempt to write or inspect files outside of the workspace directory.\n\n"
        "AVAILABLE TOOLS:\n"
        "1. Media Control: [MEDIA_CONTROL: play|pause|play-pause|next|previous]\n"
        "2. Check Music: [CHECK_SONG]\n"
        "3. Open URL: [BROWSER_OPEN: https://...]\n"
        "4. Inspect Active Page: [INSPECT_PAGE]\n"
        "5. Desktop Screenshot: [SCREENSHOT]\n"
        "6. Read/Write Clipboard: [CLIPBOARD_READ] / [CLIPBOARD_WRITE: text]\n"
        "7. Upload File: [SEND_FILE: relative_filename]\n"
        "8. System Health: [SYSTEM_DOCTOR]\n"
        "9. Bash Command: [BASH: command]\n"
        "10. Google Search: [SEARCH: query]\n"
        "11. Write File: [WRITE_FILE: relative_filename]\ncontent\n[/WRITE_FILE]\n"
        "12. Read File: [READ_FILE: relative_filename]\n"
        "13. Check Syntax: [CHECK_SYNTAX: filename.py]\n"
        "14. View Directory Tree: [TREE]\n"
        "15. Search Codebase: [GREP: search_pattern]\n"
        "16. Discord Members / User Info: [DISCORD_MEMBERS] / [DISCORD_USER_INFO: user]\n\n"
        "CRITICAL RULES:\n"
        "- NO FAKE TAGS: Never output [STATUS], [OUTPUT], or [RESPONSE]. Speak directly to the user.\n"
        "- RELEVANT CONFIRMATIONS: When an action finishes (opening browser, changing song, writing a file), give a brief, relevant confirmation of THAT specific action. Never repeat previous turns.\n"
        "- FAST & CONCISE: Emit tool tags immediately without long internal reasoning chains.\n"
        "- NO PYTHON WRAPPERS FOR TERMINAL TASKS: Run standard commands using [BASH: command].\n"
        "- ZERO EMOJIS: Never output emojis or smileys in your responses.\n"
        "- TONE: Technically sharp, witty, direct, and competent.\n"
        "[END SYSTEM INSTRUCTIONS]\n\n"
    )


# --- DISCORD CLIENT ---
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)


# Global Whitelist Check
@bot.check
async def global_whitelist_check(ctx: commands.Context):
    if is_owner(ctx.author.id) or ctx.author.id in whitelist:
        return True
    return False


# --- TOOLS ---
def control_media(action: str) -> str:
    action = action.strip().lower()
    alias_map = {
        "unpause": "play",
        "resume": "play",
        "toggle": "play-pause",
        "skip": "next",
        "prev": "previous",
    }
    action = alias_map.get(action, action)
    valid_actions = ["play-pause", "play", "pause", "next", "previous", "stop"]
    if action not in valid_actions:
        action = "play-pause"
    try:
        subprocess.run(f"playerctl {action}", shell=True, timeout=3, capture_output=True)
        return f"Media command '{action}' executed via playerctl."
    except Exception as e:
        return f"Media control error: {e}"


def get_current_song() -> str:
    try:
        cmd = "playerctl metadata --format '{{title}} by {{artist}}' 2>/dev/null || playerctl metadata 2>/dev/null"
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=2)
        out = res.stdout.strip()
        return out if out else "No active media playing on the system right now."
    except Exception as e:
        return f"Playerctl error: {e}"


def check_python_syntax(path_str: str, user_id: int) -> str:
    try:
        full_path = resolve_path(path_str, user_id)
    except PermissionError as pe:
        return str(pe)

    if not os.path.exists(full_path):
        return f"Error: File '{path_str}' does not exist on disk."
    res = subprocess.run(
        [sys.executable, "-m", "py_compile", full_path],
        capture_output=True,
        text=True,
    )
    if res.returncode == 0:
        return f"Syntax OK: '{path_str}' compiled successfully."
    return f"Syntax Error in '{path_str}':\n{res.stderr.strip()}"


def grep_codebase(pattern: str, user_id: int) -> str:
    pattern = pattern.strip().strip("'\"")
    search_dir = CURRENT_DIR
    if not is_owner(user_id):
        search_dir = WORKSPACE_DIR

    cmd = f"grep -rnI --exclude-dir='.venv' --exclude-dir='.git' '{pattern}' '{search_dir}'"
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=5)
    lines = res.stdout.strip().splitlines()
    if not lines:
        return f"No occurrences of '{pattern}' found."
    result = "\n".join(lines[:30])
    if len(lines) > 30:
        result += f"\n... [{len(lines) - 30} more matches omitted]"
    return result


def get_project_tree(user_id: int) -> str:
    target_dir = CURRENT_DIR
    if not is_owner(user_id):
        target_dir = WORKSPACE_DIR

    cmd = (
        f"tree -L 2 -I '.venv|.git|__pycache__' '{target_dir}' 2>/dev/null || "
        f"find '{target_dir}' -maxdepth 2 -not -path '*/.*' | head -n 35"
    )
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=3)
    return res.stdout.strip() if res.stdout.strip() else "(Directory is currently empty)"


async def take_screenshot() -> tuple[str, str or None]:
    path = os.path.join(WORKSPACE_DIR, ".screenshot.png")
    cmd = f"grim '{path}' 2>/dev/null || maim '{path}' 2>/dev/null || scrot '{path}' 2>/dev/null"
    proc = await asyncio.create_subprocess_shell(cmd)
    await proc.communicate()
    if os.path.exists(path) and os.path.getsize(path) > 1000:
        return "Screenshot captured.", path
    return "Screenshot failed. Ensure grim, maim, or scrot is installed.", None


async def read_clipboard() -> str:
    cmd = "wl-paste 2>/dev/null || xclip -selection clipboard -o 2>/dev/null"
    proc = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE)
    stdout, _ = await proc.communicate()
    out = stdout.decode("utf-8", errors="ignore").strip()
    return out if out else "(Clipboard is empty)"


async def write_clipboard(text: str) -> str:
    proc = await asyncio.create_subprocess_shell(
        "wl-copy 2>/dev/null || xclip -selection clipboard 2>/dev/null",
        stdin=asyncio.subprocess.PIPE,
    )
    await proc.communicate(input=text.encode("utf-8"))
    return f"Copied to clipboard: {text[:80]}"


async def run_system_doctor() -> str:
    cmd = (
        "echo '=== FAILED SERVICES ===' && (systemctl --failed --no-legend 2>/dev/null || echo 'None') && "
        "echo '=== ROOT DISK SPACE ===' && df -h / | awk 'NR==2 {print $3 \" used out of \" $2 \" (\" $5 \")\"}' && "
        "echo '=== TOP 4 RAM CONSUMERS ===' && ps aux --sort=-%mem | awk 'NR>1 && NR<=5 {print $11, $4\"%\"}'"
    )
    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, _ = await proc.communicate()
    return stdout.decode("utf-8", errors="ignore").strip()


async def inspect_active_page() -> str:
    global LAST_OPENED_URL
    try:
        cmd = (
            "hyprctl activewindow -j 2>/dev/null | jq -r .title 2>/dev/null || "
            "xdotool getactivewindow getwindowname 2>/dev/null"
        )
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=2)
        win_title = res.stdout.strip()
        if win_title:
            return f"Active Window: '{win_title}' (URL: {LAST_OPENED_URL})"
    except Exception:
        pass

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(LAST_OPENED_URL, headers=headers, timeout=5) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE)
                    title = title_match.group(1).strip() if title_match else "No title found"
                    return f"Webpage ({LAST_OPENED_URL}):\n- Title: {title}"
    except Exception as e:
        return f"Current URL is {LAST_OPENED_URL} (Inspect error: {e})"

    return f"Active URL: {LAST_OPENED_URL}"


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
            return f"Error: File '{full_path}' does not exist on disk."
        if os.path.isdir(full_path):
            return f"Error: '{full_path}' is a directory. Use [TREE]."
        with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        numbered = [f"{i + 1:3d} | {line}" for i, line in enumerate(lines[:120])]
        res = "".join(numbered)
        if len(lines) > 120:
            res += f"\n... [{len(lines) - 120} more lines omitted]"
        return res if res else "(File is empty)"
    except Exception as e:
        return f"Error reading file: {e}"


def get_discord_members(guild) -> str:
    if not guild:
        return "Error: Cannot fetch members outside of a server."
    members = guild.members[:35]
    lines = [f"Server '{guild.name}' Members ({len(guild.members)} total):"]
    for m in members:
        bot_tag = " [BOT]" if m.bot else ""
        lines.append(f"- {m.name} (Nick: {m.display_name}, ID: {m.id}){bot_tag}")
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
    return (
        f"User: {target.name} (Nick: {target.display_name})\n"
        f"ID: {target.id}\nRoles: {', '.join(roles)}"
    )


async def search_web(query: str) -> str:
    g_url = f"https://www.google.com/search?q={urllib.parse.quote(query)}&hl=en"
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 Chrome/120.0.0.0 Mobile Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(g_url, headers=headers, timeout=7) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    snippets = re.findall(r'<div class="BNeawe[^>]*>(.*?)</div>', html)
                    clean = []
                    for s in snippets:
                        txt = re.sub(r"<[^>]+>", "", s).strip()
                        if len(txt) > 25 and txt not in clean and "Google" not in txt:
                            clean.append(txt)
                    if clean:
                        return "[Google Results]:\n" + "\n".join(f"- {s}" for s in clean[:5])
    except Exception:
        pass
    return "No search results found."


async def execute_bash(cmd: str, user_id: int) -> str:
    global CURRENT_DIR

    try:
        validate_bash_command(cmd, user_id)
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
        stdout, stderr = await asyncio.wait_for(proc.communicate(input=input_data), timeout=45)

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
                        err += "\n[Security]: Escaping workspace directory blocked. Reset to workspace root."
            out = re.sub(r"___PWD___:.*", "", out_raw).strip()
        else:
            out = out_raw.strip()

        if (cmd.strip().startswith("cd ") or cmd.strip() == "cd" or "cd " in cmd) and not out:
            out = f"Directory changed to: {CURRENT_DIR}"

        res = ""
        if out:
            res += out
        if err:
            res += f"\n[STDERR]:\n{err}" if res else err
        return res if res else "(Command completed with no output)"
    except asyncio.TimeoutError:
        return "Command timed out after 45 seconds."
    except Exception as e:
        return f"Execution error: {e}"


# --- LOCAL LM STUDIO LLM CALL ---
async def call_lm_studio(messages: list) -> str:
    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 3500,
        "stream": False,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(LM_STUDIO_URL, json=payload) as resp:
            if resp.status != 200:
                raw_err = await resp.text()
                return f"LM Studio API Error (HTTP {resp.status}): {raw_err}"
            data = await resp.json()
            return data["choices"][0]["message"]["content"]


async def send_split_message(destination, text: str):
    cleaned = strip_all_emojis_nuclear(text)
    if not cleaned:
        return
    for i in range(0, len(cleaned), 1900):
        await destination.send(cleaned[i : i + 1900])


# --- CORE PIPELINE ---
async def process_ai_request(channel, author, prompt: str):
    global LAST_OPENED_URL
    async with channel.typing():
        clean_p = prompt.strip()
        first_token = clean_p.split()[0].lower() if clean_p else ""
        common_cmds = [
            "ls", "dir", "pwd", "cd", "whoami", "uname", "uptime", "tree",
            "lsusb", "lsblk", "hyfetch", "fastfetch", "neofetch", "sensors",
            "free", "df", "ip", "dmesg", "cat", "head", "tail", "clear",
        ]

        if first_token in common_cmds or (
            len(clean_p.split()) <= 2
            and (os.path.exists(f"/usr/bin/{first_token}") or os.path.exists(f"/bin/{first_token}"))
        ):
            target_cmd = "ls -la" if clean_p in ["dir", "ls"] else ("uptime -p" if clean_p == "uptime" else clean_p)
            await channel.send(f"💻 **Running:** `{target_cmd}`")
            out = await execute_bash(target_cmd, author.id)
            await send_split_message(channel, f"```\n{out}\n```")
            return

        if channel.id not in CHANNEL_HISTORY:
            CHANNEL_HISTORY[channel.id] = []

        history = CHANNEL_HISTORY[channel.id]
        sys_prompt = build_system_prompt(author.display_name, is_owner(author.id))

        conversation = list(history)
        conversation.append({"role": "user", "content": prompt})

        messages = [{"role": "system", "content": sys_prompt}]
        for msg in conversation:
            messages.append({"role": msg["role"], "content": msg["content"]})

        recent_actions = []

        for _ in range(12):
            reply = await call_lm_studio(messages)
            reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.DOTALL).strip()

            media_control_match = re.search(r"\[MEDIA_CONTROL:\s*(.*?)\]", reply, re.IGNORECASE)
            check_song_match = re.search(r"\[CHECK_SONG\]", reply, re.IGNORECASE)
            tree_match = re.search(r"\[TREE\]", reply, re.IGNORECASE)
            grep_match = re.search(r"\[GREP:\s*(.*?)\]", reply, re.IGNORECASE)
            syntax_match = re.search(r"\[CHECK_SYNTAX:\s*(.*?)\]", reply, re.IGNORECASE)
            screenshot_match = re.search(r"\[SCREENSHOT\]", reply, re.IGNORECASE)
            clip_read_match = re.search(r"\[CLIPBOARD_READ\]", reply, re.IGNORECASE)
            clip_write_match = re.search(r"\[CLIPBOARD_WRITE:\s*(.*?)\]", reply, re.IGNORECASE)
            send_file_match = re.search(r"\[SEND_FILE:\s*(.*?)\]", reply, re.IGNORECASE)
            doctor_match = re.search(r"\[SYSTEM_DOCTOR\]", reply, re.IGNORECASE)
            inspect_page_match = re.search(r"\[INSPECT_PAGE\]", reply, re.IGNORECASE)
            browser_open_match = re.search(r"\[(?:BROWSER_OPEN|OPEN_LINK|OPEN_URL)[:\]]\s*(.*?)\]", reply, re.IGNORECASE)

            write_match = re.search(r"\[WRITE_FILE[:\]]\s*(.*?)\n(.*?)\[/WRITE_FILE\]", reply, re.DOTALL | re.IGNORECASE)
            if not write_match:
                write_match = re.search(r"\[WRITE_FILE[:\]]\s*([^\n\]]+)\n(.*)", reply, re.DOTALL | re.IGNORECASE)

            read_match = re.search(r"\[READ_FILE:\s*(.*?)\]", reply, re.IGNORECASE)
            search_match = re.search(r"\[SEARCH:\s*(.*?)\]", reply, re.IGNORECASE)
            discord_members_match = re.search(r"\[DISCORD_MEMBERS\]", reply, re.IGNORECASE)
            discord_user_match = re.search(r"\[DISCORD_USER_INFO:\s*(.*?)\]", reply, re.IGNORECASE)

            cmd_to_run = None
            bash_tag = re.search(r"\[BASH:\s*(.*?)\]", reply, re.IGNORECASE) or re.search(
                r"\[BASH\]\s*(.*?)(?:\[/BASH\]|\n|$)", reply, re.IGNORECASE
            )

            if bash_tag:
                cmd_to_run = bash_tag.group(1).strip().rstrip("]")
            else:
                cb = re.search(r"```(?:bash|shell|sh)\s*\n(.*?)\n```", reply, re.DOTALL | re.IGNORECASE)
                if cb:
                    cmd_to_run = cb.group(1).strip()

            current_action = None
            if media_control_match:
                current_action = ("media_ctrl", media_control_match.group(1).strip().rstrip("]"))
            elif check_song_match:
                current_action = ("check_song", "active")
            elif write_match:
                current_action = ("write", write_match.group(1).strip())
            elif tree_match:
                current_action = ("tree", "active")
            elif grep_match:
                current_action = ("grep", grep_match.group(1).strip().rstrip("]"))
            elif syntax_match:
                current_action = ("syntax", syntax_match.group(1).strip().rstrip("]"))
            elif screenshot_match:
                current_action = ("screenshot", "active")
            elif clip_read_match:
                current_action = ("clip_read", "active")
            elif clip_write_match:
                current_action = ("clip_write", clip_write_match.group(1).strip().rstrip("]"))
            elif send_file_match:
                current_action = ("send_file", send_file_match.group(1).strip().rstrip("]"))
            elif doctor_match:
                current_action = ("doctor", "active")
            elif inspect_page_match:
                current_action = ("inspect_page", "active")
            elif browser_open_match and len(browser_open_match.group(1).strip().rstrip("]")) > 3:
                current_action = ("b_open", browser_open_match.group(1).strip().rstrip("]"))
            elif read_match:
                current_action = ("read", read_match.group(1).strip().rstrip("]"))
            elif cmd_to_run:
                current_action = ("bash", cmd_to_run)
            elif search_match:
                current_action = ("search", search_match.group(1).strip().rstrip("]"))
            elif discord_members_match:
                current_action = ("d_members", "all")
            elif discord_user_match:
                current_action = ("d_user", discord_user_match.group(1).strip().rstrip("]"))

            if current_action:
                if recent_actions and current_action == recent_actions[-1]:
                    messages.append({"role": "assistant", "content": reply})
                    messages.append({"role": "user", "content": "[Notice]: You already ran this action. Proceed to the next step."})
                    continue
                if len(recent_actions) >= 3 and current_action == recent_actions[-2]:
                    messages.append({"role": "assistant", "content": reply})
                    messages.append({"role": "user", "content": "[Notice]: Loop detected. Stop repeating commands and answer."})
                    continue
                recent_actions.append(current_action)

            # 1. Media Control
            if media_control_match:
                action = media_control_match.group(1).strip().rstrip("]")
                await channel.send(f"⏯️ **Media Action:** `{action}`")
                out = control_media(action)
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[Media Output]: {out}"})

            # 2. Check Song
            elif check_song_match:
                await channel.send("🎵 **Checking music playback...**")
                out = get_current_song()
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[Media Status]: {out}"})

            # 3. Write File
            elif write_match:
                fname = write_match.group(1).strip().rstrip("]")
                content = write_match.group(2)
                await channel.send(f"📝 **Writing:** `{fname}`")
                out = write_file_tool(fname, content, author.id)
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[File Output]: {out}"})

            # 4. Check Syntax
            elif syntax_match:
                fname = syntax_match.group(1).strip().rstrip("]")
                await channel.send(f"🔎 **Checking Python syntax:** `{fname}`")
                out = check_python_syntax(fname, author.id)
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[Syntax Check]:\n{out}"})

            # 5. Project Tree
            elif tree_match:
                await channel.send("📂 **Inspecting folder tree...**")
                out = get_project_tree(author.id)
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[Project Tree]:\n{out}"})

            # 6. Grep Codebase
            elif grep_match:
                patt = grep_match.group(1).strip().rstrip("]")
                await channel.send(f"🔍 **Searching codebase for:** `{patt}`")
                out = grep_codebase(patt, author.id)
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[Grep Matches]:\n{out}"})

            # 7. Screenshot
            elif screenshot_match:
                await channel.send("📸 **Capturing screenshot...**")
                status_msg, img_path = await take_screenshot()
                if img_path:
                    await channel.send(file=discord.File(img_path))
                    messages.append({"role": "assistant", "content": reply})
                    messages.append({"role": "user", "content": "[Screenshot uploaded to Discord]"})
                else:
                    messages.append({"role": "assistant", "content": reply})
                    messages.append({"role": "user", "content": f"[Screenshot Error]: {status_msg}"})

            # 8. Read Clipboard
            elif clip_read_match:
                await channel.send("📋 **Reading desktop clipboard...**")
                out = await read_clipboard()
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[Clipboard Content]:\n{out}"})

            # 9. Write Clipboard
            elif clip_write_match:
                text_to_copy = clip_write_match.group(1).strip().rstrip("]")
                await channel.send(f"📋 **Copying to clipboard:** `{text_to_copy[:40]}...`")
                out = await write_clipboard(text_to_copy)
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[Clipboard Result]: {out}"})

            # 10. Upload File to Discord
            elif send_file_match:
                f_name = send_file_match.group(1).strip().rstrip("]")
                try:
                    full_path = resolve_path(f_name, author.id)
                    if os.path.exists(full_path) and os.path.isfile(full_path):
                        await channel.send(
                            f"📁 **Attaching file:** `{os.path.basename(full_path)}`",
                            file=discord.File(full_path),
                        )
                        out = f"Attached {os.path.basename(full_path)} to Discord."
                    else:
                        out = f"Error: File '{f_name}' not found."
                except PermissionError as pe:
                    out = str(pe)

                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[File Attachment]: {out}"})

            # 11. System Doctor
            elif doctor_match:
                await channel.send("🩺 **Diagnosing system health...**")
                out = await run_system_doctor()
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[System Health Report]:\n{out}"})

            # 12. Inspect Page
            elif inspect_page_match:
                await channel.send("🔍 **Inspecting webpage...**")
                out = await inspect_active_page()
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[Page Info]:\n{out}"})

            # 13. Open Browser
            elif browser_open_match and len(browser_open_match.group(1).strip().rstrip("]")) > 3:
                url_target = browser_open_match.group(1).strip().rstrip("]")
                if not url_target.startswith(("http://", "https://")):
                    url_target = "https://" + url_target
                LAST_OPENED_URL = url_target
                await channel.send(f"🌐 **Opening in Zen Browser:** `{url_target}`")
                subprocess.Popen(
                    f"zen-browser '{url_target}' >/dev/null 2>&1 || xdg-open '{url_target}' >/dev/null 2>&1",
                    shell=True,
                )
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[Browser Event]: Opened {url_target}."})

            # 14. Read File
            elif read_match:
                fname = read_match.group(1).strip().rstrip("]")
                await channel.send(f"📖 **Reading:** `{fname}`")
                out = read_file_tool(fname, author.id)
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[File Content]:\n{out}"})

            # 15. Bash Command
            elif cmd_to_run:
                if cmd_to_run.strip() in [f"cd {CURRENT_DIR}", "cd ."]:
                    out = f"Notice: Already inside {CURRENT_DIR}."
                else:
                    await channel.send(f"💻 **Running:** `{cmd_to_run}`")
                    out = await execute_bash(cmd_to_run, author.id)

                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[Command Output]:\n{out}"})

            # 16. Web Search
            elif search_match:
                query = search_match.group(1).strip().rstrip("]")
                await channel.send(f"🔍 **Searching Google:** `{query}`")
                out = await search_web(query)
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[Search Results]:\n{out}"})

            # 17. Discord Members
            elif discord_members_match:
                await channel.send("👥 **Fetching server members...**")
                out = get_discord_members(channel.guild)
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[Members]:\n{out}"})

            # 18. Discord User Info
            elif discord_user_match:
                target_user = discord_user_match.group(1).strip().rstrip("]")
                await channel.send(f"🔍 **Looking up user:** `{target_user}`")
                out = get_discord_user_info(channel.guild, target_user)
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[User Info]:\n{out}"})

            else:
                cleaned_reply = strip_all_emojis_nuclear(reply)

                # Loop-breaker: Prevent the model from repeating its exact previous message from turn 1
                if history and len(history) >= 2:
                    last_assistant_msg = history[-1].get("content", "")
                    if cleaned_reply.lower() == strip_all_emojis_nuclear(last_assistant_msg).lower():
                        if browser_open_match:
                            cleaned_reply = f"Opened {url_target} in Zen Browser."
                        elif media_control_match:
                            cleaned_reply = f"Media command '{action}' sent."
                        else:
                            cleaned_reply = "Action complete."

                if cleaned_reply:
                    await send_split_message(channel, cleaned_reply)

                history.append({"role": "user", "content": prompt})
                history.append({"role": "assistant", "content": cleaned_reply or reply})
                CHANNEL_HISTORY[channel.id] = history[-12:]
                return

        cleaned_reply = strip_all_emojis_nuclear(reply)
        if cleaned_reply:
            await send_split_message(channel, cleaned_reply)
        history.append({"role": "user", "content": prompt})
        history.append({"role": "assistant", "content": cleaned_reply or reply})
        CHANNEL_HISTORY[channel.id] = history[-12:]


# --- EVENT LISTENER FOR 1-TIME TOKEN REPLY ---
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Check if this message is a direct reply to a pending sudo authorization message
    if message.reference and message.reference.message_id in pending_sudo_tokens:
        ref_id = message.reference.message_id
        challenge = pending_sudo_tokens[ref_id]

        if message.author.id == challenge["user_id"]:
            input_token = message.content.strip().upper()
            expected_token = challenge["token"]

            # Token is strictly 1-time: immediately invalidate it
            del pending_sudo_tokens[ref_id]

            if input_token == expected_token:
                sudo_users.add(message.author.id)
                await message.reply("🛡️ Sudo privileges enabled for your user session.")
            else:
                await message.reply("❌ Invalid token. Token burned. Run `!allowsudo` to request a new one.")
            return

    await bot.process_commands(message)


# --- COMMANDS ---
@bot.command(name="whitelist")
async def cmd_whitelist(ctx: commands.Context, action: str = None, user_arg: str = None):
    """Owner-only command to manage user access."""
    if not is_owner(ctx.author.id):
        await ctx.reply("⛔ Only the bot owner can manage the whitelist.")
        return

    # If user ran: !whitelist <userid> directly
    if action and action.isdigit():
        user_arg = action
        action = "add"

    if not action or action.lower() == "list":
        if not whitelist:
            await ctx.reply("📋 Whitelist is currently empty.")
        else:
            entries = "\n".join(f"- <@{uid}> (`{uid}`)" for uid in whitelist)
            await ctx.reply(f"📋 **Whitelisted Users:**\n{entries}")
        return

    if not user_arg:
        await ctx.reply("Usage: `!whitelist add <user_id>` or `!whitelist remove <user_id>`")
        return

    clean_id = int(re.sub(r"\D", "", user_arg))
    act = action.lower()

    if act == "add":
        whitelist.add(clean_id)
        save_whitelist(whitelist)
        await ctx.reply(f"✅ Added <@{clean_id}> (`{clean_id}`) to the whitelist.")
    elif act in ["remove", "rm", "del"]:
        whitelist.discard(clean_id)
        sudo_users.discard(clean_id)
        save_whitelist(whitelist)
        await ctx.reply(f"🗑️ Removed <@{clean_id}> (`{clean_id}`) from the whitelist.")
    else:
        await ctx.reply("Unknown action. Use `add`, `remove`, or `list`.")


@bot.command(name="allowsudo")
async def allowsudo_cmd(ctx: commands.Context):
    """Generates a 1-time token to console and waits for a direct reply."""
    if is_owner(ctx.author.id):
        await ctx.reply("You are the owner and already have permanent sudo privileges.")
        return

    token = secrets.token_hex(4).upper()

    print("\n" + "=" * 60)
    print(f"[SECURITY CONSOLE] 1-Time Sudo Token for {ctx.author.name} (ID: {ctx.author.id}):")
    print(f" >>> {token} <<<")
    print("=" * 60 + "\n")

    prompt_msg = await ctx.reply("check console for your 1 time token.")
    pending_sudo_tokens[prompt_msg.id] = {
        "user_id": ctx.author.id,
        "token": token
    }


@bot.command(name="disallowsudo")
async def disallowsudo_cmd(ctx: commands.Context):
    """Revokes active sudo session."""
    if ctx.author.id in sudo_users:
        sudo_users.discard(ctx.author.id)
        await ctx.reply("🔒 Sudo privileges disabled for your session.")
    else:
        await ctx.reply("ℹ️ You do not currently have sudo enabled.")


@bot.command(name="ask")
async def ask_cmd(ctx: commands.Context, *, prompt: str):
    await process_ai_request(ctx.channel, ctx.author, prompt)


@bot.command(name="clear")
async def clear_history_cmd(ctx: commands.Context):
    CHANNEL_HISTORY[ctx.channel.id] = []
    await ctx.send("🧹 **Conversation history cleared.**")


@bot.command(name="setstatus")
async def set_status_cmd(ctx: commands.Context, *, text: str):
    if not is_owner(ctx.author.id):
        return
    try:
        await bot.change_presence(
            status=discord.Status.dnd,
            activity=discord.CustomActivity(name=text),
        )
        await ctx.send(f"✅ Set custom status to: `{text}` (DND)")
    except Exception:
        pass


@bot.event
async def on_ready():
    print(f"\033[32m[✓] Bot online: {bot.user} (Owner ID: {OWNER_USER_ID})\033[0m")
    try:
        await bot.change_presence(
            status=discord.Status.dnd,
            activity=discord.CustomActivity(name="being an estrogen goober (!ask)"),
        )
    except Exception:
        pass


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
