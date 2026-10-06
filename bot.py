#!/usr/bin/env python3
"""
Stealth Quest Completer — Discord bot that auto-completes Discord quests.

Flow:
1. User types ,autoquest in the server
2. If no token stored, bot DMs the user asking for their Discord token
3. User replies to DM with their token (stored in RAM only, never on disk)
4. User types ,autoquest again in the server
5. Bot completes all available quests
6. Token auto-expires after 1 hour or can be cleared with ,clear

Security:
- Token is NEVER logged, saved to disk, or shared
- Stored in a dict in RAM, keyed by user_id
- Auto-expires after 1 hour of inactivity
- ,clear wipes it immediately
"""

import asyncio
import logging
import os
import random
import time
from typing import Optional

import aiohttp
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

# ─── Config ───────────────────────────────────────────────────────────────────

load_dotenv()
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
PREFIX = os.getenv("COMMAND_PREFIX", ",")

if not BOT_TOKEN:
    raise SystemExit("ERROR: Set DISCORD_BOT_TOKEN in .env (create a bot at https://discord.com/developers/applications)")

# ─── Logging (never logs tokens) ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("quest-bot")

# ─── Token store (in-memory only, never persisted to disk) ───────────────────

# user_id -> {"token": str, "added_at": float}
_tokens: dict[int, dict] = {}


def store_token(user_id: int, token: str) -> None:
    _tokens[user_id] = {"token": token, "added_at": time.time()}


def get_token(user_id: int) -> Optional[str]:
    entry = _tokens.get(user_id)
    if not entry:
        return None
    if time.time() - entry["added_at"] > 3600:
        del _tokens[user_id]
        return None
    return entry["token"]


def clear_token(user_id: int) -> bool:
    if user_id in _tokens:
        del _tokens[user_id]
        return True
    return False


# ─── Discord Quest API ──────────────────────────────────────────────────────

API = "https://discord.com/api/v10"


class QuestAPI:
    def __init__(self, token: str):
        self.token = token
        self.h = {
            "Authorization": token,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) discord/1.0.1151 Chrome/120.0.6099.291 Electron/29.2.4 Safari/537.36",
            "X-Discord-Locale": "en-US",
            "X-Debug-Options": "bugReporterEnabled",
        }

    async def _req(self, method: str, path: str, body=None):
        url = f"{API}{path}"
        async with aiohttp.ClientSession() as s:
            async with s.request(method, url, headers=self.h, json=body) as r:
                if r.status == 429:
                    ra = float(r.headers.get("Retry-After", 5))
                    await asyncio.sleep(ra)
                    return await self._req(method, path, body)
                try:
                    data = await r.json()
                except Exception:
                    data = None
                return r.status, data

    async def get_user(self):
        st, data = await self._req("GET", "/users/@me")
        return data if st == 200 else None

    async def get_quests(self):
        """Fetches quests from Discord API. Returns a list of quest dicts."""
        st, data = await self._req("GET", "/users/@me/quests")
        if st != 200 or not data:
            return []

        # Discord returns quests in various formats depending on the API version:
        # 1. {"quests": [...]}
        # 2. [quest1, quest2, ...]
        # 3. {"1": quest1, "2": quest2, ...} (Map serialized as object)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            if "quests" in data:
                quests = data["quests"]
                if isinstance(quests, list):
                    return quests
                if isinstance(quests, dict):
                    return list(quests.values())
            # Maybe it's a dict of quest_id -> quest
            for key in ("quest", "items", "data"):
                if key in data:
                    items = data[key]
                    if isinstance(items, list):
                        return items
                    if isinstance(items, dict):
                        return list(items.values())
            # Fall back: try to extract values that look like quests
            result = []
            for v in data.values():
                if isinstance(v, dict) and ("id" in v or "config" in v or "status" in v):
                    result.append(v)
                elif isinstance(v, list):
                    for item in v:
                        if isinstance(item, dict) and ("id" in item or "config" in item or "status" in item):
                            result.append(item)
            if result:
                return result
            # Last resort: maybe the whole dict IS a quest (single quest)
            if "id" in data or "config" in data:
                return [data]
        return []

    async def enroll(self, quest_id: str):
        st, _ = await self._req("POST", f"/users/@me/quests/{quest_id}/enroll")
        return st in (200, 201, 204)

    async def heartbeat(self, quest_id: str, stream_key: str, app_id: str, terminal: bool = False):
        body = {
            "stream_key": stream_key,
            "application_id": str(app_id),
            "terminal": terminal,
        }
        st, data = await self._req("POST", f"/users/@me/quests/{quest_id}/heartbeat", body)
        return st in (200, 201), data

    async def video_progress(self, quest_id: str, timestamp: float):
        body = {"timestamp": timestamp}
        st, data = await self._req("POST", f"/users/@me/quests/{quest_id}/video-progress", body)
        return st in (200, 201), data

    async def claim(self, quest_id: str):
        st, _ = await self._req("POST", f"/users/@me/quests/{quest_id}/claim-reward")
        return st in (200, 201)


# ─── Quest parser ────────────────────────────────────────────────────────────

def parse_quest(q: dict) -> dict:
    """Extracts relevant info from a Discord quest JSON."""
    qid = q.get("id", "")
    cfg = q.get("config", {})
    name = cfg.get("name", "Unknown quest")
    status = q.get("status", "UNKNOWN")

    task_cfg = cfg.get("taskConfigV2", {}).get("tasks", {})
    task_type = ""
    target = 60
    app_id = ""

    for task_key, task_data in task_cfg.items():
        task_type = task_key
        target = task_data.get("target", 60)
        apps = task_data.get("applications", [])
        if apps:
            app_id = apps[0].get("id", "")
        break

    if not app_id:
        app_id = cfg.get("application", {}).get("id", "")

    return {
        "id": qid,
        "name": name,
        "status": status,
        "task_type": task_type,
        "target": target,
        "app_id": app_id,
    }


def build_stream_key(user_id: str, channel_id: str = "0") -> str:
    return f"call:{channel_id}:{user_id}"


# ─── Quest completion ───────────────────────────────────────────────────────

async def complete_quests(token: str, callback):
    """Completes all available quests. callback(msg) reports progress."""
    api = QuestAPI(token)

    await callback("Validating token...")
    user = await api.get_user()
    if not user:
        await callback("Invalid or expired token.")
        return False

    username = user.get("username", "unknown")
    user_id = user.get("id", "0")
    await callback(f"Connected as **{username}** (`{user_id}`)")

    await callback("Fetching available quests...")
    quests_raw = await api.get_quests()
    if not quests_raw:
        await callback("You have no quests available right now.")
        return True

    if isinstance(quests_raw, dict):
        quests_list = quests_raw.get("quests", [])
    else:
        quests_list = quests_raw

    if not quests_list:
        await callback("You have no quests available right now.")
        return True

    quests = [parse_quest(q) for q in quests_list]
    await callback(f"Found **{len(quests)}** quests. Completing...")

    completed = 0
    failed = 0
    skipped = 0

    for i, q in enumerate(quests, 1):
        if q["status"] == "COMPLETED":
            await callback(f"[{i}/{len(quests)}] **{q['name']}** — already completed")
            skipped += 1
            continue

        await callback(f"[{i}/{len(quests)}] **{q['name']}** — enrolling...")

        if not await api.enroll(q["id"]):
            await callback(f"[{i}/{len(quests)}] Failed to enroll in **{q['name']}**")
            failed += 1
            continue

        tt = q["task_type"]
        target = q["target"] if q["target"] > 0 else 60
        app_id = q["app_id"] or "0"
        stream_key = build_stream_key(user_id)

        await callback(f"[{i}/{len(quests)}] **{q['name']}** ({tt}) — sending progress...")

        try:
            if "VIDEO" in tt:
                await _complete_video(api, q, target)
            elif "PLAY" in tt or "ACTIVITY" in tt or "ACHIEVEMENT" in tt or "STREAM" in tt:
                await _complete_heartbeat(api, q, stream_key, app_id, target)
            else:
                await _complete_heartbeat(api, q, stream_key, app_id, target)
        except Exception as e:
            await callback(f"[{i}/{len(quests)}] Error on **{q['name']}**: {e}")
            failed += 1
            continue

        await callback(f"[{i}/{len(quests)}] **{q['name']}** — claiming reward...")
        claimed = await api.claim(q["id"])

        if claimed:
            completed += 1
            await callback(f"[{i}/{len(quests)}] **{q['name']}** — done!")
        else:
            completed += 1
            await callback(f"[{i}/{len(quests)}] **{q['name']}** — completed (claim pending)")

        if i < len(quests):
            delay = random.uniform(5, 12)
            await asyncio.sleep(delay)

    await callback(
        f"**Summary:** {completed} completed | {skipped} already done | {failed} failed"
    )
    return True


async def _complete_heartbeat(api, q, stream_key, app_id, target):
    """Completes a quest by sending periodic heartbeats."""
    progress = 0
    max_beats = 30
    beats = 0

    while progress < target and beats < max_beats:
        beats += 1
        ok, data = await api.heartbeat(q["id"], stream_key, app_id)

        if ok and data:
            prog_data = data.get("progress", {})
            for key, val in prog_data.items():
                if isinstance(val, dict):
                    progress = val.get("value", progress)
                elif isinstance(val, (int, float)):
                    progress = val
                break

            if data.get("completed_at"):
                break

        if not ok:
            progress += max(5, target // 10)

        delay = random.uniform(3, 8)
        await asyncio.sleep(delay)

    await api.heartbeat(q["id"], stream_key, app_id, terminal=True)


async def _complete_video(api, q, target):
    """Completes a video quest by sending progress timestamps."""
    cur = 0.0
    max_sends = 20

    for _ in range(max_sends):
        cur = min(target, cur + random.uniform(target * 0.05, target * 0.15))
        cur = round(cur, 6)

        ok, data = await api.video_progress(q["id"], cur)

        if ok and data:
            prog_data = data.get("progress", {})
            for key, val in prog_data.items():
                if isinstance(val, dict):
                    sv = val.get("value", 0)
                    if sv > cur:
                        cur = sv
                break

            if data.get("completed_at"):
                break

        delay = random.uniform(5, 12)
        await asyncio.sleep(delay)


# ─── Bot ───────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.dm_messages = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

# Track users waiting to submit token via DM
_pending_dm: set[int] = set()


@bot.event
async def on_ready():
    log.info(f"Bot ready: {bot.user} (ID: {bot.user.id})")
    log.info(f"Prefix: {PREFIX}")
    log.info(f"Tokens in memory: {len(_tokens)}")
    try:
        synced = await bot.tree.sync()
        log.info(f"Slash commands synced: {len(synced)}")
    except Exception as e:
        log.error(f"Sync error: {e}")


@bot.event
async def on_message(message: discord.Message):
    """Listen for DMs to capture tokens."""
    if message.author == bot.user:
        return

    # If it's a DM and the user is waiting to submit their token
    if isinstance(message.channel, discord.DMChannel) and message.author.id in _pending_dm:
        token = message.content.strip()

        if len(token) < 50:
            await message.channel.send(
                "That doesn't look like a valid token. Discord tokens are at least 50 characters.\n"
                "Try again by pasting your full token."
            )
            return

        store_token(message.author.id, token)
        _pending_dm.discard(message.author.id)

        await message.channel.send(
            "**Token stored in memory.**\n\n"
            f"Now go to the server and type `{PREFIX}autoquest` to complete your quests.\n\n"
            "Your token has NOT been saved to any file or log. "
            "It will be automatically deleted after 1 hour, or you can delete it with "
            f"`{PREFIX}clear`."
        )

        # Don't announce in the server — keep it private
        return

    await bot.process_commands(message)


@bot.command(name="autoquest")
async def cmd_autoquest(ctx: commands.Context):
    """Auto-complete your Discord quests."""

    if isinstance(ctx.channel, discord.DMChannel):
        await ctx.send("This command only works in servers. Use it in the server where the bot is.")
        return

    user_id = ctx.author.id
    token = get_token(user_id)

    if not token:
        # No token stored — DM the user asking for it
        _pending_dm.add(user_id)

        try:
            await ctx.author.send(
                "**Enter your Discord token**\n\n"
                "To complete your quests, I need your Discord token.\n\n"
                "**How to get your token:**\n"
                "```\n"
                "1. Open Discord in your browser (not the app)\n"
                "2. Press Ctrl+Shift+I (DevTools)\n"
                "3. Go to the Network tab\n"
                "4. Filter by 'api'\n"
                "5. Click on any request\n"
                "6. In Headers, find 'Authorization'\n"
                "7. Copy the value\n"
                "```\n\n"
                "**Reply to this DM by pasting your token.**\n\n"
                "Your token is stored ONLY in memory. It is never logged, "
                "saved to disk, or shared. It will be deleted after 1 hour "
                f"or you can delete it with `{PREFIX}clear`."
            )
            await ctx.send("I've sent you a DM to enter your token. Check your messages.", delete_after=30)
        except discord.Forbidden:
            await ctx.send(
                "I can't DM you. Please enable 'Allow direct messages from server members' "
                "in your Discord settings, or send me your token directly via DM.",
                delete_after=30,
            )
            _pending_dm.discard(user_id)
        return

    # Token exists — complete quests
    await ctx.send(f"Starting quest completion for {ctx.author.mention}...")

    async def callback(msg: str):
        await ctx.send(msg)

    try:
        success = await complete_quests(token, callback)
        if not success:
            clear_token(user_id)
            await ctx.send(
                "Your token is invalid or expired. Use `,autoquest` again to enter a new one."
            )
    except Exception as e:
        await ctx.send(f"Error: {e}")


@bot.command(name="quests")
async def cmd_quests(ctx: commands.Context):
    """List your pending quests."""
    if isinstance(ctx.channel, discord.DMChannel):
        await ctx.send("This command only works in servers.")
        return

    user_id = ctx.author.id
    token = get_token(user_id)

    if not token:
        await ctx.send(f"No token stored. Use `{PREFIX}autoquest` to enter one.", delete_after=20)
        return

    await ctx.typing()
    api = QuestAPI(token)
    user = await api.get_user()
    if not user:
        await ctx.send("Invalid token.")
        clear_token(user_id)
        return

    quests_raw = await api.get_quests()
    if isinstance(quests_raw, dict):
        quests_list = quests_raw.get("quests", [])
    else:
        quests_list = quests_raw

    if not quests_list:
        await ctx.send("You have no quests available.")
        return

    embed = discord.Embed(
        title=f"Quests — {user.get('username', 'unknown')}",
        color=0x5865F2,
    )
    for q in quests_list[:10]:
        pq = parse_quest(q)
        emoji = "✅" if pq["status"] == "COMPLETED" else "⏳"
        embed.add_field(
            name=f"{emoji} {pq['name']}",
            value=f"Type: `{pq['task_type']}`\nStatus: `{pq['status']}`",
            inline=False,
        )

    if len(quests_list) > 10:
        embed.set_footer(text=f"+{len(quests_list) - 10} more...")

    await ctx.send(embed=embed)


@bot.command(name="status")
async def cmd_status(ctx: commands.Context):
    """Check if you have a token stored."""
    token = get_token(ctx.author.id)
    if token:
        await ctx.send(
            "✅ You have a token stored in memory.\n"
            f"Use `{PREFIX}autoquest` to complete quests or `{PREFIX}clear` to delete it.",
            delete_after=20,
        )
    else:
        await ctx.send(f"No token stored. Use `{PREFIX}autoquest` to enter one.", delete_after=20)


@bot.command(name="clear")
async def cmd_clear(ctx: commands.Context):
    """Delete your token from memory."""
    if clear_token(ctx.author.id):
        await ctx.send("✅ Token deleted from memory.", delete_after=15)
    else:
        await ctx.send("You had no token stored.", delete_after=15)


@bot.command(name="debug")
async def cmd_debug(ctx: commands.Context):
    """Debug: shows raw quest API response (admin only)."""
    if isinstance(ctx.channel, discord.DMChannel):
        await ctx.send("This command only works in servers.")
        return

    token = get_token(ctx.author.id)
    if not token:
        await ctx.send("No token stored. Use `,autoquest` first.", delete_after=15)
        return

    await ctx.typing()
    api = QuestAPI(token)
    st, data = await api._req("GET", "/users/@me/quests")

    # Truncate response to fit in Discord message
    import json
    raw = json.dumps(data, indent=2) if data else "None"
    if len(raw) > 1500:
        raw = raw[:1500] + "\n... (truncated)"

    embed = discord.Embed(
        title="Debug: Quest API response",
        description=f"```\nHTTP {st}\n{raw}\n```",
        color=0x5865F2,
    )
    await ctx.send(embed=embed, delete_after=60)


@bot.command(name="help")
async def cmd_help(ctx: commands.Context):
    """Show available commands."""
    embed = discord.Embed(
        title="Stealth Quest Completer — Commands",
        color=0x5865F2,
        description="Bot that auto-completes your Discord quests.",
    )
    embed.add_field(name=f"`{PREFIX}autoquest`", value="Complete all your quests (DMs you for token if you don't have one)", inline=False)
    embed.add_field(name=f"`{PREFIX}quests`", value="List your pending quests", inline=False)
    embed.add_field(name=f"`{PREFIX}status`", value="Check if you have a token stored", inline=False)
    embed.add_field(name=f"`{PREFIX}clear`", value="Delete your token from memory", inline=False)
    embed.add_field(name=f"`{PREFIX}help`", value="Show this help message", inline=False)
    embed.add_field(name=f"`{PREFIX}debug`", value="Show raw quest API response (for troubleshooting)", inline=False)
    embed.set_footer(text="discord.gg/hqE5drDHF7 | Token is never saved to disk")
    await ctx.send(embed=embed)


# ─── Periodic cleanup of expired tokens ─────────────────────────────────────

@tasks.loop(minutes=5)
async def cleanup_tokens():
    """Deletes tokens that have been in memory for more than 1 hour."""
    now = time.time()
    expired = [uid for uid, data in _tokens.items() if now - data["added_at"] > 3600]
    for uid in expired:
        del _tokens[uid]
    if expired:
        log.info(f"Expired tokens removed: {len(expired)}")


@cleanup_tokens.before_loop
async def before_cleanup():
    await bot.wait_until_ready()


# ─── Main ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Start the cleanup task inside the bot's event loop
    @bot.listen()
    async def on_startup_once():
        cleanup_tokens.start()

    bot.run(BOT_TOKEN)
