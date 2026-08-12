import discord
from discord import app_commands
from discord.ext import commands, tasks
import asyncio
import json
from datetime import datetime, timezone, timedelta

# Import shared functions from bot.py
from bot import (
    d1_query, 
    GUILD_ID,
    FOUNDER_ROLE_ID,
    HEAD_STAFF_ROLE_ID,
    MOD_ROLE_ID,
    TRIAL_MOD_ROLE_ID,
    HOSTER_ROLE_ID,
    ELITE_HOSTER_ROLE_ID,
    BLACKLIST_ROLE_ID,
    VERIFIED_ROLE_ID,
    LINKED_ROLE_ID,
    LOG_CHANNELS,
    is_verified,
    is_linked,
    is_hoster,
    is_elite_hoster,
    is_staff,
    is_mod_or_higher,
    is_head_staff_or_founder,
    is_founder,
    is_blacklisted,
    get_hoster_bonus
)

# ── XP Level Functions ──────────────────────────────────────────────────────
def xp_needed_for_level(level: int) -> int:
    """XP required to go from `level` → `level + 1`"""
    return 5 * (level ** 2) + 50 * level + 100


def total_xp_for_level(level: int) -> int:
    """Total XP needed to reach this level from 0"""
    return sum(xp_needed_for_level(l) for l in range(level))


def level_from_xp(xp: int) -> int:
    """Calculate level from total XP"""
    level = 0
    while total_xp_for_level(level + 1) <= xp:
        level += 1
    return level


def progress_bar(current: int, needed: int, length: int = 12) -> str:
    """Create a visual progress bar"""
    if needed <= 0:
        return "█" * length
    filled = int((current / needed) * length)
    return "█" * filled + "░" * (length - filled)


# ── Logging Helper ───────────────────────────────────────────────────────────
async def send_log(bot: commands.Bot, embed: discord.Embed):
    """Send a log message to the log channel"""
    channel = bot.get_channel(LOG_CHANNELS.get("moderation", 0))
    if channel:
        try:
            await channel.send(embed=embed)
        except Exception as e:
            print(f"Error sending log: {e}")


# ── Hoster Points Helpers ──────────────────────────────────────────────────
async def add_hoster_points(user_id: int, amount: int) -> dict:
    """
    Add hoster points to a user (balance, total earned, and weekly).
    """
    try:
        now = datetime.now(timezone.utc).isoformat()

        result = await d1_query(
            "SELECT hoster_points, weekly_points, total_points_earned FROM users WHERE discord_id = ?",
            [str(user_id)]
        )

        if not result["results"]:
            return None

        current_lifetime = result["results"][0]["hoster_points"] or 0
        current_weekly = result["results"][0]["weekly_points"] or 0
        current_total_earned = result["results"][0]["total_points_earned"] or 0

        new_lifetime = current_lifetime + amount
        new_weekly = current_weekly + amount
        new_total_earned = current_total_earned + amount

        await d1_query(
            "UPDATE users SET hoster_points = ?, weekly_points = ?, total_points_earned = ?, updated_at = ? WHERE discord_id = ?",
            [new_lifetime, new_weekly, new_total_earned, now, str(user_id)]
        )

        return {
            "old_lifetime": current_lifetime,
            "new_lifetime": new_lifetime,
            "old_weekly": current_weekly,
            "new_weekly": new_weekly,
            "old_total_earned": current_total_earned,
            "new_total_earned": new_total_earned,
            "points_added": amount,
        }
    except Exception as e:
        print(f"Error adding hoster points: {e}")
        return None


async def remove_hoster_points(user_id: int, amount: int) -> dict:
    """
    Remove hoster points from a user (balance and weekly only).
    Total earned NEVER decreases.
    """
    try:
        now = datetime.now(timezone.utc).isoformat()

        result = await d1_query(
            "SELECT hoster_points, weekly_points, total_points_earned FROM users WHERE discord_id = ?",
            [str(user_id)]
        )

        if not result["results"]:
            return None

        current_lifetime = result["results"][0]["hoster_points"] or 0
        current_weekly = result["results"][0]["weekly_points"] or 0
        current_total_earned = result["results"][0]["total_points_earned"] or 0

        new_lifetime = max(0, current_lifetime - amount)
        new_weekly = max(0, current_weekly - amount)
        # Total earned stays the same - NEVER decreases!

        await d1_query(
            "UPDATE users SET hoster_points = ?, weekly_points = ?, updated_at = ? WHERE discord_id = ?",
            [new_lifetime, new_weekly, now, str(user_id)]
        )

        return {
            "old_lifetime": current_lifetime,
            "new_lifetime": new_lifetime,
            "old_weekly": current_weekly,
            "new_weekly": new_weekly,
            "old_total_earned": current_total_earned,
            "new_total_earned": current_total_earned,  # Unchanged
            "points_removed": amount,
        }
    except Exception as e:
        print(f"Error removing hoster points: {e}")
        return None


# ── Admin Cog ────────────────────────────────────────────────────────────────
class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.check_blacklist_expiry.start()

    def cog_unload(self):
        self.check_blacklist_expiry.cancel()

    # ── Blacklist Expiry Task ──────────────────────────────────────────────
    @tasks.loop(minutes=30)
    async def check_blacklist_expiry(self):
        """Auto-remove expired blacklists every 30 minutes"""
        try:
            now = datetime.now(timezone.utc).isoformat()

            result = await d1_query(
                "SELECT user_id FROM blacklist WHERE expires_at IS NOT NULL AND expires_at < ?",
                [now]
            )

            for row in result["results"]:
                user_id = int(row["user_id"])

                guild = self.bot.get_guild(GUILD_ID)
                if guild:
                    member = guild.get_member(user_id)
                    if member:
                        bl_role = guild.get_role(BLACKLIST_ROLE_ID)
                        if bl_role and bl_role in member.roles:
                            await member.remove_roles(bl_role, reason="Blacklist expired")

                await d1_query(
                    "DELETE FROM blacklist WHERE user_id = ?",
                    [str(user_id)]
                )

                print(f"✅ Removed expired blacklist for user {user_id}")

                embed = discord.Embed(
                    title="⏰ Blacklist Expired",
                    description=f"<@{user_id}>'s blacklist has expired and been removed automatically.",
                    color=discord.Color.green(),
                    timestamp=datetime.now(timezone.utc)
                )
                await send_log(self.bot, embed)

        except Exception as e:
            print(f"Error checking blacklist expiry: {e}")

    @check_blacklist_expiry.before_loop
    async def before_blacklist_check(self):
        await self.bot.wait_until_ready()

    # ── /whois ──────────────────────────────────────────────────────────────
    @app_commands.command(
        name="whois",
        description="Look up a user by Discord ping or Roblox username (Staff only)"
    )
    @app_commands.describe(query="@Discord user or Roblox username")
    @app_commands.checks.cooldown(1, 5)
    async def whois(self, interaction: discord.Interaction, query: str):
        # Staff and Hosters
        if not is_staff(interaction.user) and not is_hoster(interaction.user):
            return await interaction.response.send_message(
                "❌ You do not have permission to use this command.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        try:
            row = None
            discord_id = None

            # Check if query is a Discord mention or ID
            if query.startswith("<@") and query.endswith(">"):
                # Extract ID from mention
                discord_id = query.strip("<@!>")
                result = await d1_query(
                    "SELECT * FROM users WHERE discord_id = ?",
                    [discord_id]
                )
                if result["results"]:
                    row = result["results"][0]
                else:
                    return await interaction.followup.send(
                        f"❌ No record found for {query}. They haven't verified yet.",
                        ephemeral=True
                    )
            else:
                # Search by Roblox username
                result = await d1_query(
                    "SELECT * FROM users WHERE roblox_users LIKE ?",
                    [f'%"{query}"%']
                )
                if result["results"]:
                    row = result["results"][0]
                    discord_id = row["discord_id"]
                else:
                    return await interaction.followup.send(
                        f"❌ No user found with Roblox username `{query}`.",
                        ephemeral=True
                    )

            # Get member
            member = interaction.guild.get_member(int(discord_id))

            # Parse data
            roblox_users = json.loads(row["roblox_users"] or "[]")
            ps_codes = json.loads(row["ps_codes"] or "[]")

            # Role checks
            role_ids = {r.id for r in member.roles} if member else set()
            bot_verified = VERIFIED_ROLE_ID in role_ids
            account_linked = LINKED_ROLE_ID in role_ids
            blacklisted = BLACKLIST_ROLE_ID in role_ids
            is_hoster_role = HOSTER_ROLE_ID in role_ids
            is_elite = ELITE_HOSTER_ROLE_ID in role_ids

            # Blacklist details
            blacklist_info = None
            if blacklisted:
                bl_result = await d1_query(
                    "SELECT reason, expires_at, blacklisted_by FROM blacklist WHERE user_id = ?",
                    [str(discord_id)]
                )
                if bl_result["results"]:
                    blacklist_info = bl_result["results"][0]

            # Top role
            top_role = None
            if member:
                roles = [r for r in member.roles if r.name != "@everyone"]
                top_role = max(roles, key=lambda r: r.position) if roles else None

            created_at = discord.utils.snowflake_time(int(discord_id))
            joined_at = member.joined_at if member else None

            def time_ago(dt):
                if not dt:
                    return "N/A"
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                delta = datetime.now(timezone.utc) - dt
                days = delta.days
                if days < 1:
                    return "Today"
                if days < 30:
                    return f"{days} days ago"
                if days < 365:
                    months = days // 30
                    return f"{months} month{'s' if months > 1 else ''} ago"
                years = days // 365
                return f"{years} year{'s' if years > 1 else ''} ago"

            # Build embed
            embed = discord.Embed(
                title="👤 User Info",
                color=top_role.color if top_role and top_role.color.value else discord.Color.blurple(),
                timestamp=datetime.now(timezone.utc)
            )

            if member and member.display_avatar:
                embed.set_thumbnail(url=member.display_avatar.url)

            embed.add_field(name="User", value=f"<@{discord_id}> (`{discord_id}`)", inline=False)
            embed.add_field(name="Joined Server", value=time_ago(joined_at), inline=True)
            embed.add_field(name="Account Created", value=time_ago(created_at), inline=True)
            embed.add_field(name="\u200b", value="\u200b", inline=True)

            # Verification status
            verif_lines = [
                "✅ Bot Verified" if bot_verified else "❌ Bot Verified",
                "✅ Account Linked" if account_linked else "❌ Account Linked",
            ]
            embed.add_field(name="Verification", value="\n".join(verif_lines), inline=False)

            # Linked Roblox accounts
            embed.add_field(
                name="Linked Roblox Accounts",
                value="\n".join([f"`{r}`" for r in roblox_users]) if roblox_users else "None",
                inline=False
            )

            embed.add_field(name="Top Role", value=top_role.mention if top_role else "N/A", inline=True)
            embed.add_field(name="Roles", value=str(len(role_ids) - 1) if member else "N/A", inline=True)
            embed.add_field(name="\u200b", value="\u200b", inline=True)

            # Stats
            raids_done = row["raids_completed"] or 0
            global_waves = row["global_waves"] or 0
            avg_wave = round(global_waves / raids_done, 1) if raids_done > 0 else "N/A"

            total_earned = row.get("total_points_earned", 0) or 0

            embed.add_field(name="Current Balance", value=str(row["hoster_points"] or 0), inline=True)
            embed.add_field(name="Total Points Earned", value=str(total_earned), inline=True)
            embed.add_field(name="Weekly Points", value=str(row.get("weekly_points", 0)), inline=True)
            embed.add_field(name="Global Waves", value=str(global_waves), inline=True)
            embed.add_field(name="Raids Done", value=str(raids_done), inline=True)
            embed.add_field(name="Average Wave", value=str(avg_wave), inline=True)
            embed.add_field(name="Risk Value", value=str(row["risk_value"] or 0), inline=True)
            embed.add_field(name="In Raid", value="Yes" if row["in_raid"] else "No", inline=True)

            # Hoster roles
            if is_hoster_role:
                embed.add_field(name="Hoster Status", value="✅ Hoster" + (" 👑 Elite" if is_elite else ""), inline=True)

            # Level & XP
            level = row.get("level", 1) or 1
            exp = row.get("exp", 0) or 0
            xp_for_current = total_xp_for_level(level)
            xp_for_next = total_xp_for_level(level + 1)
            needed = xp_for_next - xp_for_current
            progress = exp - xp_for_current
            percent = int((progress / needed) * 100) if needed > 0 else 100
            bar = progress_bar(progress, needed)

            embed.add_field(
                name="Level & XP",
                value=(
                    f"**Level:** {level}\n"
                    f"**XP:** {exp:,}\n"
                    f"**Progress:** `{bar}` {percent}%\n"
                    f"**To Next Level:** {max(0, needed - progress):,} XP"
                ),
                inline=True
            )

            # Blacklist info
            if blacklisted and blacklist_info:
                bl_reason = blacklist_info.get("reason") or "No reason provided"
                bl_expires = blacklist_info.get("expires_at")
                bl_by = blacklist_info.get("blacklisted_by")

                if bl_expires:
                    expiry_date = datetime.fromisoformat(bl_expires)
                    bl_status = (
                        f"⚠️ Expires: <t:{int(expiry_date.timestamp())}:R>"
                        if expiry_date > datetime.now(timezone.utc)
                        else "⚠️ EXPIRED (should be removed)"
                    )
                else:
                    bl_status = "🚫 PERMANENT"

                embed.add_field(
                    name="Blacklist Status",
                    value=f"{bl_status}\n**Reason:** {bl_reason}\n**By:** <@{bl_by}>",
                    inline=True
                )
            else:
                embed.add_field(name="Blacklisted", value="✅ No" if not blacklisted else "🚫 Yes", inline=True)

            embed.add_field(name="\u200b", value="\u200b", inline=True)

            # PS Codes
            embed.add_field(
                name="PS Codes",
                value="\n".join([f"`{c}`" for c in ps_codes]) if ps_codes else "None",
                inline=False
            )

            embed.set_footer(
                text=f"ID: {discord_id} • Verified since {row['created_at'][:10] if row['created_at'] else 'N/A'}"
            )

            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    # ── /gpoints ─────────────────────────────────────────────────────────────
    @app_commands.command(
        name="gpoints",
        description="Give hoster points to a user (Head Staff+)"
    )
    @app_commands.describe(
        user="The user to give points to",
        points="Amount of points to give"
    )
    @app_commands.checks.cooldown(1, 5)
    async def gpoints(self, interaction: discord.Interaction, user: discord.Member, points: int):
        # Head Staff+ only
        if not is_head_staff_or_founder(interaction.user):
            return await interaction.response.send_message(
                "❌ You do not have permission to use this command.",
                ephemeral=True
            )

        if points <= 0:
            return await interaction.response.send_message(
                "❌ Points amount must be greater than 0.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        try:
            existing = await d1_query(
                "SELECT discord_id FROM users WHERE discord_id = ?",
                [str(user.id)]
            )
            if not existing["results"]:
                return await interaction.followup.send(
                    f"❌ {user.mention} has no record in the database. Have them verify first.",
                    ephemeral=True
                )

            result = await add_hoster_points(user.id, points)

            if not result:
                return await interaction.followup.send(
                    f"❌ Failed to add points to {user.mention}.",
                    ephemeral=True
                )

            await interaction.followup.send(
                f"✅ Gave **{points}** hoster points to {user.mention}.\n"
                f"💰 Balance: **{result['new_lifetime']}** pts\n"
                f"🏅 Total Earned: **{result['new_total_earned']}** pts\n"
                f"📅 Weekly: **{result['new_weekly']}** pts",
                ephemeral=True
            )

            # Log the action
            embed = discord.Embed(
                title="⭐ Hoster Points Given",
                color=discord.Color.gold(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="User", value=user.mention, inline=True)
            embed.add_field(name="Points Given", value=str(points), inline=True)
            embed.add_field(name="New Balance", value=str(result["new_lifetime"]), inline=True)
            embed.add_field(name="New Total Earned", value=str(result["new_total_earned"]), inline=True)
            embed.add_field(name="New Weekly", value=str(result["new_weekly"]), inline=True)
            embed.add_field(name="Given By", value=interaction.user.mention, inline=True)
            embed.set_footer(text=f"User ID: {user.id}")
            await send_log(self.bot, embed)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    # ── /rpoints ─────────────────────────────────────────────────────────────
    @app_commands.command(
        name="rpoints",
        description="Remove hoster points from a user (Head Staff+)"
    )
    @app_commands.describe(
        user="The user to remove points from",
        points="Amount of points to remove"
    )
    @app_commands.checks.cooldown(1, 5)
    async def rpoints(self, interaction: discord.Interaction, user: discord.Member, points: int):
        # Head Staff+ only
        if not is_head_staff_or_founder(interaction.user):
            return await interaction.response.send_message(
                "❌ You do not have permission to use this command.",
                ephemeral=True
            )

        if points <= 0:
            return await interaction.response.send_message(
                "❌ Points amount must be greater than 0.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        try:
            existing = await d1_query(
                "SELECT hoster_points, weekly_points, total_points_earned FROM users WHERE discord_id = ?",
                [str(user.id)]
            )
            if not existing["results"]:
                return await interaction.followup.send(
                    f"❌ {user.mention} has no record in the database.",
                    ephemeral=True
                )

            result = await remove_hoster_points(user.id, points)

            if not result:
                return await interaction.followup.send(
                    f"❌ Failed to remove points from {user.mention}.",
                    ephemeral=True
                )

            await interaction.followup.send(
                f"✅ Removed **{points}** hoster points from {user.mention}.\n"
                f"💰 Balance: **{result['new_lifetime']}** pts\n"
                f"🏅 Total Earned: **{result['new_total_earned']}** pts (unchanged)\n"
                f"📅 Weekly: **{result['new_weekly']}** pts",
                ephemeral=True
            )

            # Log the action
            embed = discord.Embed(
                title="⭐ Hoster Points Removed",
                color=discord.Color.orange(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="User", value=user.mention, inline=True)
            embed.add_field(name="Points Removed", value=str(points), inline=True)
            embed.add_field(name="New Balance", value=str(result["new_lifetime"]), inline=True)
            embed.add_field(name="Total Earned", value=str(result["new_total_earned"]), inline=True)
            embed.add_field(name="New Weekly", value=str(result["new_weekly"]), inline=True)
            embed.add_field(name="Removed By", value=interaction.user.mention, inline=True)
            embed.set_footer(text=f"User ID: {user.id}")
            await send_log(self.bot, embed)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    # ── /addbl ──────────────────────────────────────────────────────────────
    @app_commands.command(
        name="addbl",
        description="Blacklist a user with reason and optional duration (Mod+)"
    )
    @app_commands.describe(
        user="The user to blacklist",
        reason="Reason for blacklisting",
        duration="Duration in days (leave empty for permanent)"
    )
    @app_commands.checks.cooldown(1, 10)
    async def addbl(self, interaction: discord.Interaction, user: discord.Member,
                    reason: str, duration: int = None):
        # Mod+ only (excludes Trial Mod)
        if not is_mod_or_higher(interaction.user):
            return await interaction.response.send_message(
                "❌ You do not have permission to use this command.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        try:
            bl_role = interaction.guild.get_role(BLACKLIST_ROLE_ID)
            if not bl_role:
                return await interaction.followup.send(
                    "❌ Blacklist role not found. Please contact an admin.",
                    ephemeral=True
                )

            if bl_role in user.roles:
                return await interaction.followup.send(
                    f"⚠️ {user.mention} is already blacklisted.",
                    ephemeral=True
                )

            expires_at = None
            duration_text = "Permanent"
            if duration:
                if duration <= 0:
                    return await interaction.followup.send(
                        "❌ Duration must be greater than 0 days.",
                        ephemeral=True
                    )
                expires_at = (datetime.now(timezone.utc) + timedelta(days=duration)).isoformat()
                duration_text = f"{duration} day{'s' if duration != 1 else ''}"

            # Add to blacklist
            await d1_query(
                """INSERT INTO blacklist
                (user_id, reason, blacklisted_by, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?)""",
                [
                    str(user.id),
                    reason,
                    str(interaction.user.id),
                    expires_at,
                    datetime.now(timezone.utc).isoformat()
                ]
            )

            # Add role
            await user.add_roles(bl_role, reason=f"Blacklisted by {interaction.user}: {reason}")

            await interaction.followup.send(
                f"🚫 {user.mention} has been blacklisted.\n"
                f"**Reason:** {reason}\n"
                f"**Duration:** {duration_text}",
                ephemeral=True
            )

            # Log the action
            embed = discord.Embed(
                title="🚫 User Blacklisted",
                color=discord.Color.dark_red(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="User", value=user.mention, inline=True)
            embed.add_field(name="Reason", value=reason, inline=True)
            embed.add_field(name="Duration", value=duration_text, inline=True)
            embed.add_field(name="By", value=interaction.user.mention, inline=True)
            embed.set_footer(text=f"User ID: {user.id}")
            await send_log(self.bot, embed)

            # DM the user
            try:
                dm_embed = discord.Embed(
                    title="🚫 You Have Been Blacklisted",
                    color=discord.Color.dark_red(),
                    timestamp=datetime.now(timezone.utc)
                )
                dm_embed.add_field(name="Reason", value=reason, inline=False)
                dm_embed.add_field(name="Duration", value=duration_text, inline=True)
                if expires_at:
                    expiry_date = datetime.fromisoformat(expires_at)
                    dm_embed.add_field(
                        name="Expires",
                        value=f"<t:{int(expiry_date.timestamp())}:F>",
                        inline=True
                    )
                dm_embed.add_field(
                    name="What to do?",
                    value="If you believe this was a mistake, please contact a staff member to appeal.",
                    inline=False
                )
                await user.send(embed=dm_embed)
            except Exception:
                pass

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    # ── /delbl ──────────────────────────────────────────────────────────────
    @app_commands.command(
        name="delbl",
        description="Remove a user from the blacklist (Mod+)"
    )
    @app_commands.describe(user="The user to unblacklist")
    @app_commands.checks.cooldown(1, 5)
    async def delbl(self, interaction: discord.Interaction, user: discord.Member):
        # Mod+ only (excludes Trial Mod)
        if not is_mod_or_higher(interaction.user):
            return await interaction.response.send_message(
                "❌ You do not have permission to use this command.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        try:
            bl_role = interaction.guild.get_role(BLACKLIST_ROLE_ID)
            if not bl_role:
                return await interaction.followup.send(
                    "❌ Blacklist role not found.",
                    ephemeral=True
                )

            if bl_role not in user.roles:
                return await interaction.followup.send(
                    f"⚠️ {user.mention} is not blacklisted.",
                    ephemeral=True
                )

            # Get the reason before removing
            bl_record = await d1_query(
                "SELECT reason FROM blacklist WHERE user_id = ?",
                [str(user.id)]
            )
            reason = bl_record["results"][0]["reason"] if bl_record["results"] else "Unknown"

            # Remove role and blacklist entry
            await user.remove_roles(bl_role, reason=f"Unblacklisted by {interaction.user}")
            await d1_query("DELETE FROM blacklist WHERE user_id = ?", [str(user.id)])

            await interaction.followup.send(
                f"✅ {user.mention} has been removed from the blacklist.",
                ephemeral=True
            )

            # Log the action
            embed = discord.Embed(
                title="✅ User Unblacklisted",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="User", value=user.mention, inline=True)
            embed.add_field(name="Original Reason", value=reason, inline=True)
            embed.add_field(name="By", value=interaction.user.mention, inline=True)
            embed.set_footer(text=f"User ID: {user.id}")
            await send_log(self.bot, embed)

            # DM the user
            try:
                dm_embed = discord.Embed(
                    title="✅ You Have Been Unblacklisted",
                    color=discord.Color.green(),
                    timestamp=datetime.now(timezone.utc)
                )
                dm_embed.add_field(
                    name="Good News!",
                    value="You have been removed from the blacklist and can now access the server again.",
                    inline=False
                )
                await user.send(embed=dm_embed)
            except Exception:
                pass

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    # ── /blacklistinfo ─────────────────────────────────────────────────────
    @app_commands.command(
        name="blacklistinfo",
        description="View blacklist information for a user (Staff only)"
    )
    @app_commands.describe(user="The user to check")
    @app_commands.checks.cooldown(1, 5)
    async def blacklistinfo(self, interaction: discord.Interaction, user: discord.Member):
        # Staff and Hosters
        if not is_staff(interaction.user) and not is_hoster(interaction.user):
            return await interaction.response.send_message(
                "❌ You do not have permission to use this command.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        try:
            result = await d1_query(
                "SELECT * FROM blacklist WHERE user_id = ?",
                [str(user.id)]
            )

            if not result["results"]:
                return await interaction.followup.send(
                    f"✅ {user.mention} is not blacklisted.",
                    ephemeral=True
                )

            row = result["results"][0]

            embed = discord.Embed(
                title="📋 Blacklist Information",
                color=discord.Color.orange(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="User", value=user.mention, inline=True)
            embed.add_field(name="Reason", value=row["reason"] or "No reason provided", inline=False)
            embed.add_field(name="Blacklisted By", value=f"<@{row['blacklisted_by']}>", inline=True)

            if row["expires_at"]:
                expiry_date = datetime.fromisoformat(row["expires_at"])
                if expiry_date > datetime.now(timezone.utc):
                    embed.add_field(
                        name="Expires",
                        value=f"<t:{int(expiry_date.timestamp())}:R>",
                        inline=True
                    )
                else:
                    embed.add_field(name="Expires", value="⚠️ EXPIRED", inline=True)
            else:
                embed.add_field(name="Expires", value="🚫 Permanent", inline=True)

            embed.add_field(
                name="Created At",
                value=f"<t:{int(datetime.fromisoformat(row['created_at']).timestamp())}:F>",
                inline=True
            )

            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    # ── /addxp ──────────────────────────────────────────────────────────────
    @app_commands.command(
        name="addxp",
        description="Add XP to a user (Head Staff+)"
    )
    @app_commands.describe(
        user="The user to give XP to",
        amount="Amount of XP to give"
    )
    @app_commands.checks.cooldown(1, 5)
    async def addxp(self, interaction: discord.Interaction, user: discord.Member, amount: int):
        # Head Staff+ only
        if not is_head_staff_or_founder(interaction.user):
            return await interaction.response.send_message(
                "❌ You do not have permission to use this command.",
                ephemeral=True
            )

        if amount <= 0:
            return await interaction.response.send_message(
                "❌ Amount must be greater than 0. Use `/removexp` to subtract XP.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        try:
            from cogs.levels import award_xp

            existing = await d1_query(
                "SELECT discord_id FROM users WHERE discord_id = ?",
                [str(user.id)]
            )
            if not existing["results"]:
                return await interaction.followup.send(
                    f"❌ {user.mention} has no record in the database. Have them verify first.",
                    ephemeral=True
                )

            result = await award_xp(user.id, amount)

            if not result:
                return await interaction.followup.send(
                    f"❌ Failed to add XP to {user.mention}.",
                    ephemeral=True
                )

            await interaction.followup.send(
                f"✅ Added **{amount:,}** XP to {user.mention}. "
                f"New total: **{result['new_exp']:,}** XP (Level {result['new_level']})",
                ephemeral=True
            )

            # Log the action
            embed = discord.Embed(
                title="⭐ XP Added",
                color=discord.Color.gold(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="User", value=user.mention, inline=True)
            embed.add_field(name="XP Added", value=f"+**{amount}**", inline=True)
            embed.add_field(name="New Total XP", value=f"**{result['new_exp']:,}**", inline=True)
            embed.add_field(name="New Level", value=f"**{result['new_level']}**", inline=True)
            embed.add_field(name="Added By", value=interaction.user.mention, inline=True)
            embed.set_footer(text=f"User ID: {user.id}")
            await send_log(self.bot, embed)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    # ── /removexp ───────────────────────────────────────────────────────────
    @app_commands.command(
        name="removexp",
        description="Remove XP from a user (Head Staff+)"
    )
    @app_commands.describe(
        user="The user to remove XP from",
        amount="Amount of XP to remove"
    )
    @app_commands.checks.cooldown(1, 5)
    async def removexp(self, interaction: discord.Interaction, user: discord.Member, amount: int):
        # Head Staff+ only
        if not is_head_staff_or_founder(interaction.user):
            return await interaction.response.send_message(
                "❌ You do not have permission to use this command.",
                ephemeral=True
            )

        if amount <= 0:
            return await interaction.response.send_message(
                "❌ Amount must be greater than 0.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        try:
            existing = await d1_query(
                "SELECT level, exp FROM users WHERE discord_id = ?",
                [str(user.id)]
            )
            if not existing["results"]:
                return await interaction.followup.send(
                    f"❌ {user.mention} has no record in the database.",
                    ephemeral=True
                )

            current_exp = existing["results"][0]["exp"] or 0
            new_exp = max(0, current_exp - amount)
            new_level = level_from_xp(new_exp)

            now = datetime.now(timezone.utc).isoformat()
            await d1_query(
                "UPDATE users SET level = ?, exp = ?, updated_at = ? WHERE discord_id = ?",
                [new_level, new_exp, now, str(user.id)]
            )

            await interaction.followup.send(
                f"✅ Removed **{amount:,}** XP from {user.mention}. "
                f"New total: **{new_exp:,}** XP (Level {new_level})",
                ephemeral=True
            )

            # Log the action
            embed = discord.Embed(
                title="⭐ XP Removed",
                color=discord.Color.orange(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="User", value=user.mention, inline=True)
            embed.add_field(name="XP Removed", value=f"-**{amount}**", inline=True)
            embed.add_field(name="New Total XP", value=f"**{new_exp:,}**", inline=True)
            embed.add_field(name="New Level", value=f"**{new_level}**", inline=True)
            embed.add_field(name="Removed By", value=interaction.user.mention, inline=True)
            embed.set_footer(text=f"User ID: {user.id}")
            await send_log(self.bot, embed)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


async def setup(bot: commands.Bot):
    """Setup function for the cog"""
    await bot.add_cog(Admin(bot))
    print("✅ Admin cog setup complete")