#!/usr/bin/env python3
"""
Stealth Quest Completer — Discord bot that auto-completes Discord quests (Slash Commands version).
"""

import asyncio
import logging
import os
import random
import time
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")

if not BOT_TOKEN:
    raise SystemExit("ERROR: Set DISCORD_BOT_TOKEN in .env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("quest-bot")

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

API = "https://discord.com/api/v10"

class QuestAPI:
    def __init__(self, token: str):
        self.token = token
        self.h = {
            "Authorization": token,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) discord/1.0.9255 Chrome/120.0.6099.291 Electron/29.2.4 Safari/537.36",
            "X-Discord-Locale": "en-US",
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
        st, data = await self._req("GET", "/quests/@me")
        if st != 200 or not data:
            return []
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            if "quests" in data:
                quests = data["quests"]
                if isinstance(quests, list):
                    return quests
                if isinstance(quests, dict):
                    return list(quests.values())
            for key in ("quest", "items", "data"):
                if key in data:
                    items = data[key]
                    if isinstance(items, list):
                        return items
                    if isinstance(items, dict):
                        return list(items.values())
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
            if "id" in data or "config" in data:
                return [data]
        return []

    async def enroll(self, quest_id: str, traffic_sealed=None):
        body = {
            "location": 11,
            "is_targeted": False,
            "metadata_sealed": None,
            "traffic_metadata_sealed": traffic_sealed,
        }
        st, _ = await self._req("POST", f"/quests/{quest_id}/enroll", body)
        return st in (200, 201, 204)

    async def heartbeat(self, quest_id: str, stream_key: str, app_id: str, terminal: bool = False):
        body = {
            "stream_key": stream_key,
            "application_id": str(app_id),
            "terminal": terminal,
        }
        st, data = await self._req("POST", f"/quests/{quest_id}/heartbeat", body)
        return st in (200, 201), data

    async def video_progress(self, quest_id: str, timestamp: float):
        body = {"timestamp": timestamp}
        st, data = await self._req("POST", f"/quests/{quest_id}/video-progress", body)
        return st in (200, 201), data

    async def claim(self, quest_id: str, traffic_sealed=None):
        body = {
            "platform": 0,
            "location": 11,
            "is_targeted": False,
            "metadata_sealed": None,
            "traffic_metadata_sealed": traffic_sealed,
        }
        st, _ = await self._req("POST", f"/quests/{quest_id}/claim-reward", body)
        return st in (200, 201)

def parse_quest(q: dict) -> dict:
    qid = q.get("id", "")
    cfg = q.get("config", {})
    messages = cfg.get("messages", {})
    name = messages.get("quest_name") or messages.get("game_title") or cfg.get("application", {}).get("name", "Unknown quest")

    user_status = q.get("userStatus", {})
    completed_at = user_status.get("completedAt")
    enrolled_at = user_status.get("enrolledAt")
    top_status = q.get("status", "")
    is_completed = bool(completed_at) or top_status in ("COMPLETED", "CLAIMED")
    traffic_sealed = q.get("trafficMetadataSealed")

    task_cfg = cfg.get("taskConfigV2", {}).get("tasks", {})
    if not task_cfg:
        task_cfg = cfg.get("tasks", {})

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
        "status": top_status,
        "task_type": task_type,
        "target": target,
        "app_id": app_id,
        "enrolled": bool(enrolled_at),
        "completed": is_completed,
        "traffic_sealed": traffic_sealed,
    }

def build_stream_key(user_id: str, channel_id: str = "0") -> str:
    return f"call:{channel_id}:{user_id}"

async def complete_quests(token: str, callback):
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
    total_quests = len(quests)
    pending = [q for q in quests if not q["completed"]]
    already_done = total_quests - len(pending)

    if not pending:
        await callback(f"All {already_done} quests are already completed. Nothing to do!")
        return True

    await callback(f"Found **{len(pending)}** pending quests ({already_done} already done). Completing...")

    completed = 0
    failed = 0
    skipped = 0

    for i, q in enumerate(pending, 1):
        if q["completed"]:
            await callback(f"[{i}/{len(pending)}] **{q['name']}** — already completed")
            skipped += 1
            continue

        if not q["enrolled"]:
            await callback(f"[{i}/{len(pending)}] **{q['name']}** — enrolling...")
            if not await api.enroll(q["id"], q["traffic_sealed"]):
                await callback(f"[{i}/{len(pending)}] Failed to enroll in **{q['name']}**")
                failed += 1
                continue
            await asyncio.sleep(random.uniform(0.8, 1.5))
        else:
            await callback(f"[{i}/{len(pending)}] **{q['name']}** — already enrolled, completing...")

        tt = q["task_type"]
        target = q["target"] if q["target"] > 0 else 60
        app_id = q["app_id"] or "0"
        stream_key = build_stream_key(user_id)

        await callback(f"[{i}/{len(pending)}] **{q['name']}** ({tt}) — sending progress...")

        try:
            if "VIDEO" in tt:
                await _complete_video(api, q, target)
            else:
                await _complete_heartbeat(api, q, stream_key, app_id, target)
        except Exception as e:
            await callback(f"[{i}/{len(pending)}] Error on **{q['name']}**: {e}")
            failed += 1
            continue

        await callback(f"[{i}/{len(pending)}] **{q['name']}** — claiming reward...")
        claimed = await api.claim(q["id"], q["traffic_sealed"])

        if claimed:
            completed += 1
            await callback(f"[{i}/{len(pending)}] **{q['name']}** — done!")
        else:
            completed += 1
            await callback(f"[{i}/{len(pending)}] **{q['name']}** — completed (claim pending)")

        if i < len(pending):
            await asyncio.sleep(random.uniform(5, 12))

    await callback(f"**Summary:** {completed} completed | {already_done} already done | {failed} failed")
    return True

async def _complete_heartbeat(api, q, stream_key, app_id, target):
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

        await asyncio.sleep(random.uniform(3, 8))

    await api.heartbeat(q["id"], stream_key, app_id, terminal=True)

async def _complete_video(api, q, target):
    cur = 0.0
    for _ in range(20):
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

        await asyncio.sleep(random.uniform(5, 12))

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.dm_messages = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
_pending_dm: set[int] = set()

@bot.event
async def on_ready():
    log.info(f"Bot ready: {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        log.info(f"Slash commands synced: {len(synced)}")
    except Exception as e:
        log.error(f"Sync error: {e}")

@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return
    if isinstance(message.channel, discord.DMChannel) and message.author.id in _pending_dm:
        token = message.content.strip()
        if len(token) < 50:
            await message.channel.send("That doesn't look like a valid token. Try again.")
            return
        store_token(message.author.id, token)
        _pending_dm.discard(message.author.id)
        await message.channel.send("**Token stored in memory.** Now use `/autoquest` in the server.")
        return

@bot.tree.command(name="autoquest", description="Auto-complete your Discord quests")
async def slash_autoquest(interaction: discord.Interaction):
    if isinstance(interaction.channel, discord.DMChannel):
        await interaction.response.send_message("This command only works in servers.", ephemeral=True)
        return

    user_id = interaction.user.id
    token = get_token(user_id)

    if not token:
        _pending_dm.add(user_id)
        try:
            await interaction.user.send(
                "**Enter your Discord token**\n\n"
                "Reply to this DM by pasting your token.\n"
                "Stored in memory only, auto-expires in 1 hour or use `/clear`."
            )
            await interaction.response.send_message("I've sent you a DM to enter your token.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("I can't DM you. Please enable DMs from server members.", ephemeral=True)
            _pending_dm.discard(user_id)
        return

    await interaction.response.defer(thinking=True)
    author = interaction.user

    async def callback(msg: str):
        try:
            await interaction.followup.send(msg)
        except Exception:
            pass
        if any(w in msg.lower() for w in ["done!", "completed", "summary"]):
            try:
                await author.send(f"🔔 **Quest update:** {msg}")
            except discord.Forbidden:
                pass

    try:
        success = await complete_quests(token, callback)
        if not success:
            clear_token(user_id)
            await interaction.followup.send("Your token is invalid or expired. Use `/autoquest` again.")
        else:
            try:
                await author.send("✅ **Quest completion finished!**")
            except discord.Forbidden:
                pass
    except Exception as e:
        await interaction.followup.send(f"Error: {e}")

@bot.tree.command(name="quests", description="List your pending quests")
async def slash_quests(interaction: discord.Interaction):
    if isinstance(interaction.channel, discord.DMChannel):
        await interaction.response.send_message("This command only works in servers.", ephemeral=True)
        return

    token = get_token(interaction.user.id)
    if not token:
        await interaction.response.send_message("No token stored. Use `/autoquest` first.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    api = QuestAPI(token)
    user = await api.get_user()
    if not user:
        await interaction.followup.send("Invalid token.")
        clear_token(interaction.user.id)
        return

    quests_raw = await api.get_quests()
    if isinstance(quests_raw, dict):
        quests_list = quests_raw.get("quests", [])
    else:
        quests_list = quests_raw

    if not quests_list:
        await interaction.followup.send("You have no quests available.")
        return

    embed = discord.Embed(title=f"Quests — {user.get('username', 'unknown')}", color=0x5865F2)
    for q in quests_list[:10]:
        pq = parse_quest(q)
        emoji = "✅" if pq["status"] == "COMPLETED" else "⏳"
        embed.add_field(name=f"{emoji} {pq['name']}", value=f"Type: `{pq['task_type']}`\nStatus: `{pq['status']}`", inline=False)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="status", description="Check if you have a token stored")
async def slash_status(interaction: discord.Interaction):
    if get_token(interaction.user.id):
        await interaction.response.send_message("✅ Token stored in memory.", ephemeral=True)
    else:
        await interaction.response.send_message("No token stored.", ephemeral=True)

@bot.tree.command(name="clear", description="Delete your token from memory")
async def slash_clear(interaction: discord.Interaction):
    if clear_token(interaction.user.id):
        await interaction.response.send_message("✅ Token deleted from memory.", ephemeral=True)
    else:
        await interaction.response.send_message("You had no token stored.", ephemeral=True)

@bot.tree.command(name="help", description="Show available slash commands")
async def slash_help(interaction: discord.Interaction):
    embed = discord.Embed(title="Commands", color=0x5865F2)
    embed.add_field(name="`/autoquest`", value="Complete all your quests", inline=False)
    embed.add_field(name="`/quests`", value="List pending quests", inline=False)
    embed.add_field(name="`/status`", value="Check token storage status", inline=False)
    embed.add_field(name="`/clear`", value="Clear token from memory", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tasks.loop(minutes=5)
async def cleanup_tokens():
    now = time.time()
    expired = [uid for uid, data in _tokens.items() if now - data["added_at"] > 3600]
    for uid in expired:
        del _tokens[uid]

@cleanup_tokens.before_loop
async def before_cleanup():
    await bot.wait_until_ready()

if __name__ == "__main__":
    @bot.listen()
    async def on_startup_once():
        cleanup_tokens.start()
    bot.run(BOT_TOKEN)
