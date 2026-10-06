#!/usr/bin/env python3
"""
Stealth Quest Completer — Bot de Discord que completa quests automáticamente.

Flujo:
1. Usuario escribe ,autoquest en el servidor
2. Si no tiene token guardado → el bot le envía un DM pidiendo su token
3. El usuario envía su token por DM → se guarda en memoria (nunca en disco)
4. Usuario escribe ,autoquest de nuevo en el servidor → se autocompletan las quests
5. El token se borra de memoria tras 1 hora o con ,clear

Seguridad:
- El token NUNCA se loggea, ni se guarda en disco, ni se comparte
- Se guarda en un dict en RAM indexado por user_id
- Se borra automáticamente tras 1 hora de inactividad
- ,clear lo borra inmediatamente
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
    raise SystemExit("ERROR: Pon DISCORD_BOT_TOKEN en .env (crea un bot en https://discord.com/developers/applications)")

# ─── Logging (sin tokens) ────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("quest-bot")

# ─── Token store (en memoria, nunca a disco) ────────────────────────────────

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
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
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
        st, data = await self._req("GET", "/users/@me/quests")
        return data if st == 200 else []

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
    """Extrae la info relevante de una quest del JSON de Discord."""
    qid = q.get("id", "")
    cfg = q.get("config", {})
    name = cfg.get("name", "Quest desconocida")
    status = q.get("status", "UNKNOWN")

    # Buscar el task type y target
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

    # Fallback: old config path
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
    """Construye un stream_key válido para heartbeats."""
    return f"call:{channel_id}:{user_id}"


# ─── Quest completion ───────────────────────────────────────────────────────

async def complete_quests(token: str, callback):
    """Completa todas las quests disponibles. callback(msg) para reportar progreso."""
    api = QuestAPI(token)

    await callback("🔍 Validando token...")
    user = await api.get_user()
    if not user:
        await callback("❌ Token inválido o expirado.")
        return False

    username = user.get("username", "unknown")
    user_id = user.get("id", "0")
    await callback(f"✅ Conectado como **{username}** (`{user_id}`)")

    await callback("📋 Buscando quests disponibles...")
    quests_raw = await api.get_quests()
    if not quests_raw:
        await callback("ℹ️ No tienes quests disponibles ahora mismo.")
        return True

    # quests_raw puede ser una lista o un dict con "quests"
    if isinstance(quests_raw, dict):
        quests_list = quests_raw.get("quests", [])
    else:
        quests_list = quests_raw

    if not quests_list:
        await callback("ℹ️ No tienes quests disponibles ahora mismo.")
        return True

    quests = [parse_quest(q) for q in quests_list]
    await callback(f"🎯 Encontradas **{len(quests)}** quests. Completando...")

    completed = 0
    failed = 0
    skipped = 0

    for i, q in enumerate(quests, 1):
        if q["status"] == "COMPLETED":
            await callback(f"⏭️ [{i}/{len(quests)}] **{q['name']}** — ya completada")
            skipped += 1
            continue

        await callback(f"▶️ [{i}/{len(quests)}] **{q['name']}** — enrolando...")

        # Enroll
        if not await api.enroll(q["id"]):
            await callback(f"⚠️ [{i}/{len(quests)}] No se pudo enrolar en **{q['name']}**")
            failed += 1
            continue

        tt = q["task_type"]
        target = q["target"] if q["target"] > 0 else 60
        app_id = q["app_id"] or "0"
        stream_key = build_stream_key(user_id)

        await callback(f"⏳ [{i}/{len(quests)}] **{q['name']}** ({tt}) — enviando progreso...")

        # Enviar heartbeats/video-progress según el tipo
        try:
            if "VIDEO" in tt:
                await _complete_video(api, q, target, callback, i, len(quests))
            elif "PLAY" in tt or "ACTIVITY" in tt or "ACHIEVEMENT" in tt:
                await _complete_heartbeat(api, q, stream_key, app_id, target, callback, i, len(quests))
            elif "STREAM" in tt:
                await _complete_heartbeat(api, q, stream_key, app_id, target, callback, i, len(quests))
            else:
                # Tipo desconocido — intentar heartbeat genérico
                await _complete_heartbeat(api, q, stream_key, app_id, target, callback, i, len(quests))
        except Exception as e:
            await callback(f"❌ [{i}/{len(quests)}] Error en **{q['name']}**: {e}")
            failed += 1
            continue

        # Claim reward
        await callback(f"🎁 [{i}/{len(quests)}] **{q['name']}** — reclamando recompensa...")
        claimed = await api.claim(q["id"])

        if claimed:
            completed += 1
            await callback(f"✅ [{i}/{len(quests)}] **{q['name']}** — completada!")
        else:
            # A veces el claim falla pero la quest ya está hecha
            completed += 1
            await callback(f"⚠️ [{i}/{len(quests)}] **{q['name']}** — completada (claim pendiente)")

        # Delay entre quests
        if i < len(quests):
            delay = random.uniform(5, 12)
            await asyncio.sleep(delay)

    await callback(
        f"📊 **Resumen:** ✅ {completed} completadas • ⏭️ {skipped} ya hechas • ❌ {failed} fallidas"
    )
    return True


async def _complete_heartbeat(api, q, stream_key, app_id, target, callback, idx, total):
    """Completa una quest enviando heartbeats periódicos."""
    progress = 0
    max_beats = 30  # safety limit
    beats = 0

    while progress < target and beats < max_beats:
        beats += 1
        ok, data = await api.heartbeat(q["id"], stream_key, app_id)

        if ok and data:
            # Leer progreso del servidor
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
            # Intentar de todas formas
            progress += max(5, target // 10)

        # Delay entre heartbeats (3-8 seg para parecer natural)
        delay = random.uniform(3, 8)
        await asyncio.sleep(delay)

    # Heartbeat terminal
    await api.heartbeat(q["id"], stream_key, app_id, terminal=True)


async def _complete_video(api, q, target, callback, idx, total):
    """Completa una quest de video enviando timestamps de progreso."""
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

# Track users que están esperando token por DM
# user_id -> True
_pending_dm: set[int] = set()


@bot.event
async def on_ready():
    log.info(f"Bot listo: {bot.user} (ID: {bot.user.id})")
    log.info(f"Prefijo: {PREFIX}")
    log.info(f"Tokens en memoria: {len(_tokens)}")
    try:
        synced = await bot.tree.sync()
        log.info(f"Slash commands: {len(synced)}")
    except Exception as e:
        log.error(f"Error sync: {e}")


@bot.event
async def on_message(message: discord.Message):
    """Escucha DMs para capturar tokens."""
    # Ignorar mensajes del propio bot
    if message.author == bot.user:
        return

    # Si es un DM y el usuario está esperando a introducir su token
    if isinstance(message.channel, discord.DMChannel) and message.author.id in _pending_dm:
        token = message.content.strip()

        # Validación básica: los tokens de Discord suelen tener 50+ chars
        if len(token) < 50:
            await message.channel.send(
                "❌ Eso no parece un token válido. Los tokens de Discord tienen al menos 50 caracteres.\n"
                "Inténtalo de nuevo pegando tu token completo."
            )
            return

        # Guardar en memoria
        store_token(message.author.id, token)
        _pending_dm.discard(message.author.id)

        await message.channel.send(
            "✅ **Token guardado en memoria.**\n\n"
            "Ahora ve al servidor y escribe `,,autoquest` para completar tus quests.\n\n"
            "🔒 Tu token no se ha guardado en ningún archivo ni log. "
            "Se borrará automáticamente en 1 hora o puedes borrarlo con `,,clear`."
        )

        # Avisar al usuario que ya puede usar ,autoquest en el servidor
        try:
            # Buscar un canal común del servidor donde el bot y el usuario estén
            for guild in bot.guilds:
                if message.author in guild.members:
                    # Intentar enviar al primer canal de texto donde el bot pueda hablar
                    for ch in guild.text_channels:
                        if ch.permissions_for(guild.me).send_messages:
                            await ch.send(
                                f"🔔 {message.author.mention} Tu token está listo. "
                                f"Escribe `{PREFIX}autoquest` para completar tus quests!",
                                delete_after=60,
                            )
                            break
                    break
        except Exception:
            pass

        return

    # Procesar comandos normalmente
    await bot.process_commands(message)


@bot.command(name="autoquest")
async def cmd_autoquest(ctx: commands.Context):
    """Completa tus quests de Discord automáticamente."""

    # Solo funciona en servidores, no en DMs
    if isinstance(ctx.channel, discord.DMChannel):
        await ctx.send("❌ Este comando solo funciona en servidores. Ve al servidor del bot y usa `,autoquest` ahí.")
        return

    user_id = ctx.author.id
    token = get_token(user_id)

    if not token:
        # No hay token → enviar DM pidiéndolo
        _pending_dm.add(user_id)

        try:
            dm = await ctx.author.send(
                "🔒 **Introduce tu token de Discord**\n\n"
                "Para completar tus quests necesito tu token de Discord.\n\n"
                "**¿Cómo obtener tu token?**\n"
                "```\n"
                "1. Abre Discord en el navegador (no la app)\n"
                "2. Presiona Ctrl+Shift+I (DevTools)\n"
                "3. Ve a la pestaña Network\n"
                "4. Filtra por 'api'\n"
                "5. Click en cualquier request\n"
                "6. En Headers, busca 'Authorization'\n"
                "7. Copia el valor\n"
                "```\n\n"
                "**Responde a este DM pegando tu token.**\n\n"
                "🔒 Tu token se guarda SOLO en memoria. No se loggea, "
                "no se guarda en disco, no se comparte. Se borra en 1 hora "
                f"o puedes borrarlo con `{PREFIX}clear`."
            )
            await ctx.send(f"📩 Te he enviado un DM para introducir tu token. Revisa tus mensajes privados.", delete_after=30)
        except discord.Forbidden:
            await ctx.send(
                f"❌ No puedo enviarte un DM. Activa 'Permitir mensajes directos de miembros del servidor' "
                f"en tu configuración de Discord, o envíame tu token directamente por DM.",
                delete_after=30,
            )
            _pending_dm.discard(user_id)
        return

    # Hay token → completar quests
    await ctx.send(f"🚀 Iniciando completion de quests para {ctx.author.mention}...")

    async def callback(msg: str):
        await ctx.send(msg)

    try:
        success = await complete_quests(token, callback)
        if not success:
            # Token inválido — limpiar
            clear_token(user_id)
            await ctx.send(
                "❌ Tu token es inválido o ha expirado. Usa `,autoquest` de nuevo para introducir uno nuevo."
            )
    except Exception as e:
        await ctx.send(f"❌ Error: {e}")


@bot.command(name="quests")
async def cmd_quests(ctx: commands.Context):
    """Lista tus quests pendientes."""
    if isinstance(ctx.channel, discord.DMChannel):
        await ctx.send("❌ Este comando solo funciona en servidores.")
        return

    user_id = ctx.author.id
    token = get_token(user_id)

    if not token:
        await ctx.send(f"❌ No tienes token guardado. Usa `{PREFIX}autoquest` para introducirlo.", delete_after=20)
        return

    await ctx.typing()
    api = QuestAPI(token)
    user = await api.get_user()
    if not user:
        await ctx.send("❌ Token inválido.")
        clear_token(user_id)
        return

    quests_raw = await api.get_quests()
    if isinstance(quests_raw, dict):
        quests_list = quests_raw.get("quests", [])
    else:
        quests_list = quests_raw

    if not quests_list:
        await ctx.send("ℹ️ No tienes quests disponibles.")
        return

    embed = discord.Embed(
        title=f"📋 Quests de {user.get('username', 'unknown')}",
        color=0x5865F2,
    )
    for q in quests_list[:10]:
        pq = parse_quest(q)
        emoji = "✅" if pq["status"] == "COMPLETED" else "⏳"
        embed.add_field(
            name=f"{emoji} {pq['name']}",
            value=f"Tipo: `{pq['task_type']}`\nEstado: `{pq['status']}`",
            inline=False,
        )

    if len(quests_list) > 10:
        embed.set_footer(text=f"+{len(quests_list) - 10} más...")

    await ctx.send(embed=embed)


@bot.command(name="status")
async def cmd_status(ctx: commands.Context):
    """Muestra si tienes token guardado."""
    token = get_token(ctx.author.id)
    if token:
        await ctx.send(
            "✅ Tienes un token guardado en memoria.\n"
            f"Usa `{PREFIX}autoquest` para completar quests o `{PREFIX}clear` para borrarlo.",
            delete_after=20,
        )
    else:
        await ctx.send(f"❌ No tienes token. Usa `{PREFIX}autoquest` para introducirlo.", delete_after=20)


@bot.command(name="clear")
async def cmd_clear(ctx: commands.Context):
    """Borra tu token de la memoria."""
    if clear_token(ctx.author.id):
        await ctx.send("✅ Token borrado de la memoria.", delete_after=15)
    else:
        await ctx.send("ℹ️ No tenías token guardado.", delete_after=15)


@bot.command(name="help")
async def cmd_help(ctx: commands.Context):
    """Lista de comandos."""
    embed = discord.Embed(
        title="📖 Comandos — Stealth Quest Completer",
        color=0x5865F2,
        description="Bot que completa tus quests de Discord automáticamente.",
    )
    embed.add_field(name=f"`{PREFIX}autoquest`", value="Completa todas tus quests (te pide token por DM si no lo tienes)", inline=False)
    embed.add_field(name=f"`{PREFIX}quests`", value="Lista tus quests pendientes", inline=False)
    embed.add_field(name=f"`{PREFIX}status`", value="Muestra si tienes token guardado", inline=False)
    embed.add_field(name=f"`{PREFIX}clear`", value="Borra tu token de la memoria", inline=False)
    embed.add_field(name=f"`{PREFIX}help`", value="Muestra esta ayuda", inline=False)
    embed.set_footer(text="discord.gg/hqE5drDHF7 | Token nunca se guarda en disco")
    await ctx.send(embed=embed)


# ─── Limpieza periódica de tokens expirados ─────────────────────────────────

@tasks.loop(minutes=5)
async def cleanup_tokens():
    """Borra tokens que llevan más de 1 hora en memoria."""
    now = time.time()
    expired = [uid for uid, data in _tokens.items() if now - data["added_at"] > 3600]
    for uid in expired:
        del _tokens[uid]
    if expired:
        log.info(f"Tokens expirados eliminados: {len(expired)}")


@cleanup_tokens.before_loop
async def before_cleanup():
    await bot.wait_until_ready()


# ─── Main ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cleanup_tokens.start()
    bot.run(BOT_TOKEN)
