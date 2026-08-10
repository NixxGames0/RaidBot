import discord
from discord import app_commands
from discord.ext import commands
import asyncio
from datetime import datetime, timezone, timedelta
import logging

from bot import (
    d1_query,
    GUILD_ID,
    LOG_CHANNEL_ID,
    send_log
)

logger = logging.getLogger(__name__)

# ─── Database setup ─────────────────────────────────────────
async def init_afk_db():
    await d1_query(
        """CREATE TABLE IF NOT EXISTS afk (
            user_id TEXT PRIMARY KEY,
            note TEXT,
            start_time TEXT NOT NULL,
            original_nick TEXT
        )"""
    )


# ─── Helper functions ──────────────────────────────────────
async def get_afk(user_id: int):
    """Return AFK data for a user, or None if not AFK."""
    result = await d1_query(
        "SELECT note, start_time, original_nick FROM afk WHERE user_id = ?",
        [str(user_id)]
    )
    if result["results"]:
        row = result["results"][0]
        return {
            "note": row["note"] or "",
            "start_time": datetime.fromisoformat(row["start_time"]),
            "original_nick": row["original_nick"]
        }
    return None


async def set_afk(user_id: int, note: str, original_nick: str = None):
    """Set a user as AFK."""
    now = datetime.now(timezone.utc).isoformat()
    await d1_query(
        "INSERT OR REPLACE INTO afk (user_id, note, start_time, original_nick) VALUES (?, ?, ?, ?)",
        [str(user_id), note, now, original_nick]
    )


async def remove_afk(user_id: int):
    """Remove a user from AFK and return their data."""
    result = await d1_query(
        "SELECT note, start_time, original_nick FROM afk WHERE user_id = ?",
        [str(user_id)]
    )
    if not result["results"]:
        return None
    row = result["results"][0]
    await d1_query("DELETE FROM afk WHERE user_id = ?", [str(user_id)])
    return {
        "note": row["note"] or "",
        "start_time": datetime.fromisoformat(row["start_time"]),
        "original_nick": row["original_nick"]
    }


def format_duration(seconds: int) -> str:
    """Format seconds into a human-readable duration."""
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    parts = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if not parts:
        return "less than a minute"
    return " ".join(parts)


# ─── Rate limiting for AFK replies ──────────────────────
afk_reply_cooldown = {}


# ─── Cog ────────────────────────────────────────────────────
class AFK(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.loop.create_task(self.init_db())

    async def init_db(self):
        await self.bot.wait_until_ready()
        await init_afk_db()
        print("✅ AFK database ready")

    # ─── Commands ──────────────────────────────────────────
    @app_commands.command(name="afk", description="Set yourself as AFK (optionally with a note)")
    @app_commands.describe(note="Optional note to show when someone pings you")
    @app_commands.checks.cooldown(1, 5)
    async def afk(self, interaction: discord.Interaction, note: str = None):
        """Set AFK status."""
        user = interaction.user
        guild = interaction.guild

        # Get current AFK status
        afk_data = await get_afk(user.id)
        if afk_data:
            # Already AFK – update note and reset timer
            await set_afk(user.id, note or "", afk_data["original_nick"])
            # Re-apply nickname (in case it was changed manually)
            await self._set_afk_nickname(user, afk_data["original_nick"])
            await interaction.response.send_message(
                f"✅ Updated your AFK status. You are still marked as AFK.",
                ephemeral=True
            )
            return

        # Store original nickname (or None)
        original_nick = user.nick

        # Set AFK in DB
        await set_afk(user.id, note or "", original_nick)

        # Change nickname
        await self._set_afk_nickname(user, original_nick)

        embed = discord.Embed(
            title="✅ You are now AFK",
            description=f"You are now marked as AFK.{' Your note: **' + note + '**' if note else ''}",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_footer(text="Type a message to return.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

        # Log
        log_embed = discord.Embed(
            title="🔇 AFK Set",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc)
        )
        log_embed.add_field(name="User", value=user.mention, inline=True)
        log_embed.add_field(name="Note", value=note or "None", inline=True)
        await send_log(self.bot, log_embed)

    async def _set_afk_nickname(self, member: discord.Member, original_nick: str = None):
        """Set the member's nickname to [AFK] + original name, respecting Discord's 32‑char limit."""
        if not member.guild.me.guild_permissions.manage_nicknames:
            return

        display_name = original_nick or member.display_name
        prefix = "[AFK] "
        max_len = 32
        # Truncate the display name to fit within 32 chars
        available = max_len - len(prefix)
        if len(display_name) > available:
            display_name = display_name[:available]

        new_nick = prefix + display_name
        try:
            await member.edit(nick=new_nick, reason="AFK status")
        except Exception as e:
            logger.warning(f"Failed to set AFK nickname for {member}: {e}")

    async def _restore_nickname(self, member: discord.Member, original_nick: str = None):
        """Restore the member's original nickname."""
        if not member.guild.me.guild_permissions.manage_nicknames:
            return
        try:
            await member.edit(nick=original_nick, reason="AFK removed")
        except Exception as e:
            logger.warning(f"Failed to restore nickname for {member}: {e}")

    @app_commands.command(name="afklist", description="List all users currently AFK")
    @app_commands.checks.cooldown(1, 10)
    async def afklist(self, interaction: discord.Interaction):
        """Show all AFK users."""
        result = await d1_query(
            "SELECT user_id, note, start_time FROM afk ORDER BY start_time"
        )
        if not result["results"]:
            return await interaction.response.send_message("📭 No one is currently AFK.", ephemeral=True)

        embed = discord.Embed(
            title="😴 Currently AFK",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc)
        )
        for row in result["results"]:
            user_id = int(row["user_id"])
            member = interaction.guild.get_member(user_id)
            name = member.mention if member else f"<@{user_id}>"
            note = row["note"] or "No note"
            start = datetime.fromisoformat(row["start_time"])
            duration = format_duration(int((datetime.now(timezone.utc) - start).total_seconds()))
            embed.add_field(
                name=name,
                value=f"📝 {note}\n⏰ {duration}",
                inline=False
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ─── Event listeners ──────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if not message.guild or message.guild.id != GUILD_ID:
            return

        # ── Remove AFK if the author sends a message ──
        afk_data = await remove_afk(message.author.id)
        if afk_data:
            # Restore nickname
            await self._restore_nickname(message.author, afk_data["original_nick"])
            # Calculate duration
            duration_seconds = int((datetime.now(timezone.utc) - afk_data["start_time"]).total_seconds())
            duration_str = format_duration(duration_seconds)
            # Send welcome back message
            embed = discord.Embed(
                title="👋 Welcome Back!",
                description=f"{message.author.mention} was AFK for **{duration_str}**.",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc)
            )
            if afk_data["note"]:
                embed.add_field(name="Note", value=afk_data["note"], inline=False)
            await message.channel.send(embed=embed)

            # Log
            log_embed = discord.Embed(
                title="🔊 AFK Removed",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc)
            )
            log_embed.add_field(name="User", value=message.author.mention, inline=True)
            log_embed.add_field(name="Duration", value=duration_str, inline=True)
            await send_log(self.bot, log_embed)

        # ── Reply to pings of AFK users ──
        if message.mentions:
            # Avoid replying to our own messages, and avoid replying to the same user too often
            now = datetime.now(timezone.utc).timestamp()
            for mentioned in message.mentions:
                if mentioned.id == message.author.id or mentioned.bot:
                    continue
                # Rate limit per mentioned user (once per 30 seconds)
                key = (mentioned.id, message.channel.id)  # per user per channel
                last = afk_reply_cooldown.get(key, 0)
                if now - last < 30:
                    continue
                afk_reply_cooldown[key] = now

                afk_data = await get_afk(mentioned.id)
                if afk_data:
                    duration_seconds = int((datetime.now(timezone.utc) - afk_data["start_time"]).total_seconds())
                    duration_str = format_duration(duration_seconds)
                    embed = discord.Embed(
                        title="😴 AFK",
                        description=f"{mentioned.display_name} is currently AFK.",
                        color=discord.Color.orange(),
                        timestamp=datetime.now(timezone.utc)
                    )
                    embed.add_field(name="Since", value=f"{duration_str} ago", inline=True)
                    if afk_data["note"]:
                        embed.add_field(name="Note", value=afk_data["note"], inline=True)
                    await message.reply(embed=embed, mention_author=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(AFK(bot))
    print("✅ AFK cog loaded")