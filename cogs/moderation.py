import re
import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone, timedelta

from bot import (
    d1_query,
    GUILD_ID,
    LOG_CHANNEL_ID,
    MOD_ROLE_ID,
    HEAD_STAFF_ROLE_ID,
    FOUNDER_ROLE_ID,
    TRIAL_MOD_ROLE_ID,
    is_staff,
)

# ── Config ─────────────────────────────────────────────────────────────────────
MAX_TIMEOUT_DAYS = 28   # Discord hard limit for timeouts

ACTION_COLORS = {
    "warn":    0xF59E0B,
    "mute":    0x6366F1,
    "unmute":  0x10B981,
    "kick":    0xEF4444,
    "ban":     0xDC2626,
    "unban":   0x22C55E,
    "note":    0x94A3B8,
}

ACTION_EMOJIS = {
    "warn":   "⚠️",
    "mute":   "🔇",
    "unmute": "🔊",
    "kick":   "👢",
    "ban":    "🔨",
    "unban":  "✅",
    "note":   "📝",
}

_DURATION_RE = re.compile(r"^(\d+)\s*([smhdw])$", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text: str) -> timedelta | None:
    m = _DURATION_RE.match(text.strip())
    if not m:
        return None
    return timedelta(seconds=int(m.group(1)) * _UNIT_SECONDS[m.group(2).lower()])


def fmt_duration(td: timedelta) -> str:
    total = int(td.total_seconds())
    if total >= 86400:
        d, rem = divmod(total, 86400)
        return f"{d}d {rem//3600}h" if rem >= 3600 else f"{d}d"
    if total >= 3600:
        h, rem = divmod(total, 3600)
        return f"{h}h {rem//60}m" if rem >= 60 else f"{h}h"
    if total >= 60:
        m, s = divmod(total, 60)
        return f"{m}m {s}s" if s else f"{m}m"
    return f"{total}s"


def is_mod(user: discord.Member) -> bool:
    return any(r.id in {MOD_ROLE_ID, HEAD_STAFF_ROLE_ID, FOUNDER_ROLE_ID} for r in user.roles)


def is_head_staff(user: discord.Member) -> bool:
    return any(r.id in {HEAD_STAFF_ROLE_ID, FOUNDER_ROLE_ID} for r in user.roles)


def _can_moderate(actor: discord.Member, target: discord.Member) -> str | None:
    """Return an error string if actor cannot moderate target, else None."""
    if target.bot:
        return "❌ You can't moderate a bot."
    if target.id == actor.id:
        return "❌ You can't moderate yourself."
    if target.guild_permissions.administrator and not is_head_staff(actor):
        return "❌ You cannot moderate an administrator."
    if actor.top_role <= target.top_role:
        return "❌ You cannot moderate someone with an equal or higher role."
    return None


async def _log_action(
    guild: discord.Guild,
    action: str,
    target: discord.Member | discord.User,
    mod: discord.Member,
    reason: str,
    case_id: int,
    duration: str | None = None,
    expires_at: str | None = None,
):
    channel = guild.get_channel(LOG_CHANNEL_ID)
    if not channel:
        return

    color = ACTION_COLORS.get(action, 0x6B7280)
    emoji = ACTION_EMOJIS.get(action, "🛡️")

    embed = discord.Embed(
        title=f"{emoji} {action.capitalize()} — Case #{case_id}",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Target",
                    value=f"{target.mention} (`{target.id}`)", inline=True)
    embed.add_field(name="Moderator",
                    value=f"{mod.mention}", inline=True)
    if duration:
        embed.add_field(name="Duration", value=duration, inline=True)
    if expires_at:
        ts = int(datetime.fromisoformat(expires_at).timestamp())
        embed.add_field(name="Expires", value=f"<t:{ts}:R>", inline=True)
    embed.add_field(name="Reason",
                    value=reason or "No reason provided", inline=False)
    embed.set_thumbnail(url=target.display_avatar.url)

    try:
        await channel.send(embed=embed)
    except Exception:
        pass


async def _dm_user(
    user: discord.User | discord.Member,
    action: str,
    guild: discord.Guild,
    reason: str,
    duration: str | None = None,
):
    emoji = ACTION_EMOJIS.get(action, "🛡️")
    lines = [
        f"{emoji} **You have been {action}ed** in **{guild.name}**.",
        f"**Reason:** {reason or 'No reason provided'}",
    ]
    if duration:
        lines.append(f"**Duration:** {duration}")
    try:
        await user.send("\n".join(lines))
    except Exception:
        pass


async def _record(
    guild_id: int,
    target_id: int,
    mod_id: int,
    action: str,
    reason: str,
    duration: str | None = None,
    expires_at: str | None = None,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    row = await d1_query(
        """INSERT INTO mod_actions
           (guild_id, target_id, mod_id, action, reason, duration, expires_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [str(guild_id), str(target_id), str(mod_id),
         action, reason, duration, expires_at, now]
    )
    id_row = await d1_query("SELECT last_insert_rowid() AS id")
    return id_row["results"][0]["id"] if id_row["results"] else 0


# ── Moderation Cog ─────────────────────────────────────────────────────────────

class ModerationCog(commands.Cog):

    # ── /warn ──────────────────────────────────────────────────────────────

    @app_commands.command(name="warn", description="Issue a warning to a member")
    @app_commands.describe(
        member="Member to warn",
        reason="Reason for the warning",
    )
    async def warn(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str = "No reason provided",
    ):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        err = _can_moderate(interaction.user, member)
        if err:
            return await interaction.response.send_message(err, ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        case_id = await _record(
            interaction.guild_id, member.id, interaction.user.id,
            "warn", reason
        )
        await _dm_user(member, "warn", interaction.guild, reason)
        await _log_action(interaction.guild, "warn", member, interaction.user, reason, case_id)

        # Warn count for this member
        count_row = await d1_query(
            "SELECT COUNT(*) AS n FROM mod_actions WHERE target_id = ? AND guild_id = ? AND action = 'warn'",
            [str(member.id), str(interaction.guild_id)]
        )
        count = count_row["results"][0]["n"] if count_row["results"] else 1

        embed = discord.Embed(
            title="⚠️ Warning Issued",
            description=f"{member.mention} has been warned. They now have **{count}** warning(s).",
            color=ACTION_COLORS["warn"],
        )
        embed.add_field(name="Reason", value=reason)
        embed.add_field(name="Case", value=f"#{case_id}", inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /mute ──────────────────────────────────────────────────────────────

    @app_commands.command(name="mute", description="Timeout (mute) a member for a duration")
    @app_commands.describe(
        member="Member to mute",
        duration="Duration: 30m, 2h, 1d, 1w (max 28d)",
        reason="Reason for the mute",
    )
    async def mute(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        duration: str,
        reason: str = "No reason provided",
    ):
        if not is_mod(interaction.user):
            return await interaction.response.send_message("❌ Moderator+ only.", ephemeral=True)
        err = _can_moderate(interaction.user, member)
        if err:
            return await interaction.response.send_message(err, ephemeral=True)

        td = parse_duration(duration)
        if not td:
            return await interaction.response.send_message(
                "❌ Invalid duration. Examples: `30m`, `2h`, `1d`, `1w`", ephemeral=True
            )
        if td.total_seconds() > MAX_TIMEOUT_DAYS * 86400:
            return await interaction.response.send_message(
                f"❌ Maximum timeout duration is {MAX_TIMEOUT_DAYS} days.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        expires = datetime.now(timezone.utc) + td
        dur_str = fmt_duration(td)

        try:
            await member.timeout(expires, reason=f"[{interaction.user}] {reason}")
        except discord.Forbidden:
            return await interaction.followup.send(
                "❌ I don't have permission to timeout this member.", ephemeral=True
            )

        case_id = await _record(
            interaction.guild_id, member.id, interaction.user.id,
            "mute", reason, dur_str, expires.isoformat()
        )
        await _dm_user(member, "mute", interaction.guild, reason, dur_str)
        await _log_action(
            interaction.guild, "mute", member, interaction.user,
            reason, case_id, dur_str, expires.isoformat()
        )

        embed = discord.Embed(
            title="🔇 Member Muted",
            description=f"{member.mention} has been muted for **{dur_str}**.",
            color=ACTION_COLORS["mute"],
        )
        embed.add_field(name="Reason", value=reason)
        embed.add_field(name="Expires", value=f"<t:{int(expires.timestamp())}:R>", inline=True)
        embed.add_field(name="Case", value=f"#{case_id}", inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /unmute ────────────────────────────────────────────────────────────

    @app_commands.command(name="unmute", description="Remove a timeout from a member")
    @app_commands.describe(
        member="Member to unmute",
        reason="Reason for removing the mute",
    )
    async def unmute(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str = "No reason provided",
    ):
        if not is_mod(interaction.user):
            return await interaction.response.send_message("❌ Moderator+ only.", ephemeral=True)

        if not member.is_timed_out():
            return await interaction.response.send_message(
                f"❌ {member.mention} is not currently muted.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        try:
            await member.timeout(None, reason=f"[{interaction.user}] {reason}")
        except discord.Forbidden:
            return await interaction.followup.send(
                "❌ I don't have permission to remove this timeout.", ephemeral=True
            )

        case_id = await _record(
            interaction.guild_id, member.id, interaction.user.id, "unmute", reason
        )
        await _log_action(interaction.guild, "unmute", member, interaction.user, reason, case_id)

        embed = discord.Embed(
            title="🔊 Member Unmuted",
            description=f"{member.mention}'s timeout has been removed.",
            color=ACTION_COLORS["unmute"],
        )
        embed.add_field(name="Reason", value=reason)
        embed.add_field(name="Case", value=f"#{case_id}", inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /kick ──────────────────────────────────────────────────────────────

    @app_commands.command(name="kick", description="Kick a member from the server")
    @app_commands.describe(
        member="Member to kick",
        reason="Reason for the kick",
    )
    async def kick(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str = "No reason provided",
    ):
        if not is_mod(interaction.user):
            return await interaction.response.send_message("❌ Moderator+ only.", ephemeral=True)
        err = _can_moderate(interaction.user, member)
        if err:
            return await interaction.response.send_message(err, ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        await _dm_user(member, "kick", interaction.guild, reason)

        try:
            await member.kick(reason=f"[{interaction.user}] {reason}")
        except discord.Forbidden:
            return await interaction.followup.send(
                "❌ I don't have permission to kick this member.", ephemeral=True
            )

        case_id = await _record(
            interaction.guild_id, member.id, interaction.user.id, "kick", reason
        )
        await _log_action(interaction.guild, "kick", member, interaction.user, reason, case_id)

        embed = discord.Embed(
            title="👢 Member Kicked",
            description=f"**{member}** has been kicked.",
            color=ACTION_COLORS["kick"],
        )
        embed.add_field(name="Reason", value=reason)
        embed.add_field(name="Case", value=f"#{case_id}", inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /ban ───────────────────────────────────────────────────────────────

    @app_commands.command(name="ban", description="Ban a member from the server")
    @app_commands.describe(
        member="Member to ban",
        reason="Reason for the ban",
        delete_days="Days of messages to delete (0–7)",
    )
    async def ban(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str = "No reason provided",
        delete_days: app_commands.Range[int, 0, 7] = 0,
    ):
        if not is_mod(interaction.user):
            return await interaction.response.send_message("❌ Moderator+ only.", ephemeral=True)
        err = _can_moderate(interaction.user, member)
        if err:
            return await interaction.response.send_message(err, ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        await _dm_user(member, "ban", interaction.guild, reason)

        try:
            await member.ban(
                reason=f"[{interaction.user}] {reason}",
                delete_message_days=delete_days,
            )
        except discord.Forbidden:
            return await interaction.followup.send(
                "❌ I don't have permission to ban this member.", ephemeral=True
            )

        case_id = await _record(
            interaction.guild_id, member.id, interaction.user.id, "ban", reason
        )
        await _log_action(interaction.guild, "ban", member, interaction.user, reason, case_id)

        embed = discord.Embed(
            title="🔨 Member Banned",
            description=f"**{member}** (`{member.id}`) has been banned.",
            color=ACTION_COLORS["ban"],
        )
        embed.add_field(name="Reason", value=reason)
        if delete_days:
            embed.add_field(name="Messages Deleted", value=f"{delete_days}d", inline=True)
        embed.add_field(name="Case", value=f"#{case_id}", inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /unban ─────────────────────────────────────────────────────────────

    @app_commands.command(name="unban", description="Unban a user by their ID")
    @app_commands.describe(
        user_id="The user's Discord ID",
        reason="Reason for the unban",
    )
    async def unban(
        self,
        interaction: discord.Interaction,
        user_id: str,
        reason: str = "No reason provided",
    ):
        if not is_mod(interaction.user):
            return await interaction.response.send_message("❌ Moderator+ only.", ephemeral=True)

        try:
            uid = int(user_id.strip())
        except ValueError:
            return await interaction.response.send_message("❌ Invalid user ID.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        try:
            ban_entry = await interaction.guild.fetch_ban(discord.Object(id=uid))
        except discord.NotFound:
            return await interaction.followup.send(
                f"❌ No ban found for user ID `{uid}`.", ephemeral=True
            )

        await interaction.guild.unban(
            ban_entry.user, reason=f"[{interaction.user}] {reason}"
        )

        case_id = await _record(
            interaction.guild_id, uid, interaction.user.id, "unban", reason
        )
        await _log_action(
            interaction.guild, "unban", ban_entry.user, interaction.user, reason, case_id
        )

        embed = discord.Embed(
            title="✅ User Unbanned",
            description=f"**{ban_entry.user}** (`{uid}`) has been unbanned.",
            color=ACTION_COLORS["unban"],
        )
        embed.add_field(name="Reason", value=reason)
        embed.add_field(name="Case", value=f"#{case_id}", inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /note ──────────────────────────────────────────────────────────────

    @app_commands.command(name="note", description="Add an internal staff note to a member (not DM'd)")
    @app_commands.describe(
        member="Member to attach the note to",
        text="The note content",
    )
    async def note(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        text: str,
    ):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("❌ Staff only.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        case_id = await _record(
            interaction.guild_id, member.id, interaction.user.id, "note", text
        )
        await _log_action(interaction.guild, "note", member, interaction.user, text, case_id)

        await interaction.followup.send(
            f"📝 Note #{case_id} added for {member.mention}.", ephemeral=True
        )

    # ── /warnings ──────────────────────────────────────────────────────────

    @app_commands.command(name="warnings", description="View all warnings for a member")
    @app_commands.describe(member="Member to check")
    async def warnings(self, interaction: discord.Interaction, member: discord.Member):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("❌ Staff only.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        row = await d1_query(
            """SELECT id, reason, mod_id, created_at FROM mod_actions
               WHERE target_id = ? AND guild_id = ? AND action = 'warn'
               ORDER BY created_at DESC LIMIT 20""",
            [str(member.id), str(interaction.guild_id)]
        )
        results = row.get("results", [])

        embed = discord.Embed(
            title=f"⚠️ Warnings — {member.display_name}",
            color=ACTION_COLORS["warn"],
        )
        embed.set_thumbnail(url=member.display_avatar.url)

        if not results:
            embed.description = "✅ No warnings on record."
        else:
            embed.description = f"**{len(results)}** warning(s) on record."
            for r in results:
                ts = r.get("created_at", "")[:10]
                embed.add_field(
                    name=f"Case #{r['id']} — {ts}",
                    value=f"**Mod:** <@{r['mod_id']}>\n**Reason:** {r['reason'] or 'None'}",
                    inline=False,
                )

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /delwarn ───────────────────────────────────────────────────────────

    @app_commands.command(name="delwarn", description="Delete a specific warning by case ID")
    @app_commands.describe(case_id="The case # of the warning to delete")
    async def delwarn(self, interaction: discord.Interaction, case_id: int):
        if not is_mod(interaction.user):
            return await interaction.response.send_message("❌ Moderator+ only.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        row = await d1_query(
            "SELECT * FROM mod_actions WHERE id = ? AND guild_id = ? AND action = 'warn'",
            [case_id, str(interaction.guild_id)]
        )
        if not row.get("results"):
            return await interaction.followup.send(
                f"❌ Warning case #{case_id} not found.", ephemeral=True
            )

        await d1_query("DELETE FROM mod_actions WHERE id = ?", [case_id])
        await interaction.followup.send(
            f"✅ Warning case #{case_id} deleted.", ephemeral=True
        )

    # ── /clearwarnings ─────────────────────────────────────────────────────

    @app_commands.command(name="clearwarnings", description="Clear all warnings for a member (Head Staff only)")
    @app_commands.describe(member="Member to clear warnings for")
    async def clearwarnings(self, interaction: discord.Interaction, member: discord.Member):
        if not is_head_staff(interaction.user):
            return await interaction.response.send_message("❌ Head Staff only.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        row = await d1_query(
            "SELECT COUNT(*) AS n FROM mod_actions WHERE target_id = ? AND guild_id = ? AND action = 'warn'",
            [str(member.id), str(interaction.guild_id)]
        )
        count = row["results"][0]["n"] if row.get("results") else 0

        await d1_query(
            "DELETE FROM mod_actions WHERE target_id = ? AND guild_id = ? AND action = 'warn'",
            [str(member.id), str(interaction.guild_id)]
        )

        embed = discord.Embed(
            title="🗑️ Warnings Cleared",
            description=f"Cleared **{count}** warning(s) from {member.mention}.",
            color=0x94A3B8,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /modhistory ────────────────────────────────────────────────────────

    @app_commands.command(name="modhistory", description="View full mod action history for a member")
    @app_commands.describe(member="Member to look up")
    async def modhistory(self, interaction: discord.Interaction, member: discord.Member):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("❌ Staff only.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        row = await d1_query(
            """SELECT id, action, reason, mod_id, duration, created_at FROM mod_actions
               WHERE target_id = ? AND guild_id = ?
               ORDER BY created_at DESC LIMIT 25""",
            [str(member.id), str(interaction.guild_id)]
        )
        results = row.get("results", [])

        embed = discord.Embed(
            title=f"📋 Mod History — {member.display_name}",
            color=0x6B7280,
        )
        embed.set_thumbnail(url=member.display_avatar.url)

        if not results:
            embed.description = "✅ No mod actions on record."
        else:
            embed.description = f"**{len(results)}** action(s) on record (newest first)."
            for r in results:
                emoji  = ACTION_EMOJIS.get(r["action"], "•")
                ts     = r.get("created_at", "")[:10]
                dur    = f" · {r['duration']}" if r.get("duration") else ""
                reason = (r.get("reason") or "None")[:80]
                embed.add_field(
                    name=f"{emoji} {r['action'].capitalize()} — Case #{r['id']} ({ts}){dur}",
                    value=f"**Mod:** <@{r['mod_id']}>\n**Reason:** {reason}",
                    inline=False,
                )

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /modlogs ───────────────────────────────────────────────────────────

    @app_commands.command(name="modlogs", description="View recent mod actions in the server")
    @app_commands.describe(
        limit="Number of entries to show (max 20, default 10)",
        action="Filter by action type",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="All",    value="all"),
        app_commands.Choice(name="Warn",   value="warn"),
        app_commands.Choice(name="Mute",   value="mute"),
        app_commands.Choice(name="Kick",   value="kick"),
        app_commands.Choice(name="Ban",    value="ban"),
        app_commands.Choice(name="Unban",  value="unban"),
        app_commands.Choice(name="Note",   value="note"),
    ])
    async def modlogs(
        self,
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 1, 20] = 10,
        action: str = "all",
    ):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("❌ Staff only.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        if action == "all":
            row = await d1_query(
                """SELECT id, action, target_id, mod_id, reason, duration, created_at
                   FROM mod_actions WHERE guild_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                [str(interaction.guild_id), limit]
            )
        else:
            row = await d1_query(
                """SELECT id, action, target_id, mod_id, reason, duration, created_at
                   FROM mod_actions WHERE guild_id = ? AND action = ?
                   ORDER BY created_at DESC LIMIT ?""",
                [str(interaction.guild_id), action, limit]
            )

        results = row.get("results", [])

        embed = discord.Embed(
            title="📋 Recent Mod Logs",
            color=0x6B7280,
            timestamp=datetime.now(timezone.utc),
        )

        if not results:
            embed.description = "No mod actions found."
        else:
            lines = []
            for r in results:
                emoji  = ACTION_EMOJIS.get(r["action"], "•")
                ts     = r.get("created_at", "")[:10]
                dur    = f" ({r['duration']})" if r.get("duration") else ""
                reason = (r.get("reason") or "None")[:50]
                lines.append(
                    f"`#{r['id']}` {emoji} **{r['action'].capitalize()}**{dur} "
                    f"<@{r['target_id']}> by <@{r['mod_id']}> — {ts}\n"
                    f"↳ {reason}"
                )
            embed.description = "\n\n".join(lines)

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /slowmode ──────────────────────────────────────────────────────────

    @app_commands.command(name="slowmode", description="Set slowmode in the current channel")
    @app_commands.describe(seconds="Slowmode in seconds (0 to disable, max 21600)")
    async def slowmode(
        self,
        interaction: discord.Interaction,
        seconds: app_commands.Range[int, 0, 21600] = 0,
    ):
        if not is_mod(interaction.user):
            return await interaction.response.send_message("❌ Moderator+ only.", ephemeral=True)

        await interaction.channel.edit(slowmode_delay=seconds)
        if seconds == 0:
            msg = f"✅ Slowmode disabled in {interaction.channel.mention}."
        else:
            msg = f"✅ Slowmode set to **{fmt_duration(timedelta(seconds=seconds))}** in {interaction.channel.mention}."
        await interaction.response.send_message(msg, ephemeral=True)

    # ── /purge ─────────────────────────────────────────────────────────────

    @app_commands.command(name="purge", description="Bulk-delete messages in the current channel")
    @app_commands.describe(
        amount="Number of messages to delete (1–100)",
        member="Only delete messages from this member (optional)",
    )
    async def purge(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, 100],
        member: discord.Member | None = None,
    ):
        if not is_mod(interaction.user):
            return await interaction.response.send_message("❌ Moderator+ only.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        check = (lambda m: m.author == member) if member else None

        try:
            deleted = await interaction.channel.purge(limit=amount, check=check)
        except discord.Forbidden:
            return await interaction.followup.send(
                "❌ I don't have permission to delete messages here.", ephemeral=True
            )

        target_note = f" from {member.mention}" if member else ""
        await interaction.followup.send(
            f"🗑️ Deleted **{len(deleted)}** message(s){target_note}.", ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(ModerationCog(bot))
    print("✅ Moderation cog loaded")
