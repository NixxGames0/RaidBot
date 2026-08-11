import discord
from discord import app_commands
from discord.ext import commands, tasks
import asyncio
from datetime import datetime, timezone, timedelta

# Try to import zoneinfo
try:
    from zoneinfo import ZoneInfo
    HAS_ZONEINFO = True
except ImportError:
    ZoneInfo = None
    HAS_ZONEINFO = False
    print("⚠️ zoneinfo not available, using UTC with manual offset")

# Import shared functions from bot.py
from bot import (
    d1_query, 
    GUILD_ID,
    HOSTER_ROLE_ID,
    ELITE_HOSTER_ROLE_ID,
    TOP_HOSTER_ROLE_ID,
    LOG_CHANNEL_ID,
    STAFF_ROLES,
    is_hoster,
    is_elite_hoster,
    is_staff,
    is_mod_or_higher,
    is_head_staff_or_founder,
    get_hoster_bonus
)

# ── Timezone Setup ──────────────────────────────────────────────────────────
if HAS_ZONEINFO and ZoneInfo:
    try:
        EASTERN = ZoneInfo("America/New_York")
        HAS_EASTERN = True
    except Exception:
        HAS_EASTERN = False
        EASTERN = None
        print("⚠️ America/New_York timezone not found, using UTC with manual offset")
else:
    HAS_EASTERN = False
    EASTERN = None
    print("⚠️ America/New_York timezone not available, using UTC with manual offset")

# ── XP Level Functions ──────────────────────────────────────────────────────
def progress_bar(current: int, needed: int, length: int = 12) -> str:
    """Create a visual progress bar"""
    if needed <= 0:
        return "█" * length
    filled = int((current / needed) * length)
    return "█" * filled + "░" * (length - filled)


# ── Logging Helper ───────────────────────────────────────────────────────────
async def send_log(bot: commands.Bot, embed: discord.Embed):
    """Send a log message to the log channel"""
    channel = bot.get_channel(LOG_CHANNEL_ID)
    if channel:
        try:
            await channel.send(embed=embed)
        except Exception as e:
            print(f"Error sending log: {e}")


# ── Timezone Helpers ──────────────────────────────────────────────────────────
def get_eastern_now() -> datetime:
    """Get current time in Eastern timezone (EDT/EST)"""
    if HAS_EASTERN and EASTERN:
        return datetime.now(EASTERN)
    
    # Fallback: manual offset
    now_utc = datetime.now(timezone.utc)
    month = now_utc.month
    if 3 <= month <= 11:
        offset = -4  # EDT
    else:
        offset = -5  # EST
    return now_utc + timedelta(hours=offset)


def get_week_start() -> datetime:
    """Sunday at midnight Eastern, returned as UTC-aware datetime."""
    now_eastern = get_eastern_now()
    days_since_sunday = (now_eastern.weekday() + 1) % 7
    sunday = now_eastern - timedelta(days=days_since_sunday)
    sunday_midnight = sunday.replace(hour=0, minute=0, second=0, microsecond=0)
    return sunday_midnight.astimezone(timezone.utc)


def get_week_end() -> datetime:
    """Saturday at 23:59:59 Eastern, returned as UTC-aware datetime."""
    return get_week_start() + timedelta(days=7) - timedelta(seconds=1)


# ── Hoster Points Functions ─────────────────────────────────────────────────
async def add_hoster_points(user_id: int, amount: int) -> dict:
    """
    Add hoster points to a user (both lifetime and weekly).
    Also tracks total points earned (never decreases on spend).
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
    """Remove hoster points from a user (only from balance, not total earned)"""
    try:
        now = datetime.now(timezone.utc).isoformat()

        result = await d1_query(
            "SELECT hoster_points, weekly_points FROM users WHERE discord_id = ?",
            [str(user_id)]
        )

        if not result["results"]:
            return None

        current_lifetime = result["results"][0]["hoster_points"] or 0
        current_weekly = result["results"][0]["weekly_points"] or 0

        new_lifetime = max(0, current_lifetime - amount)
        new_weekly = max(0, current_weekly - amount)

        await d1_query(
            "UPDATE users SET hoster_points = ?, weekly_points = ?, updated_at = ? WHERE discord_id = ?",
            [new_lifetime, new_weekly, now, str(user_id)]
        )

        return {
            "old_lifetime": current_lifetime,
            "new_lifetime": new_lifetime,
            "old_weekly": current_weekly,
            "new_weekly": new_weekly,
            "points_removed": amount,
        }
    except Exception as e:
        print(f"Error removing hoster points: {e}")
        return None


async def get_user_total_earned(user_id: int) -> int:
    """Get a user's total points earned (lifetime, never decreases)"""
    result = await d1_query(
        "SELECT total_points_earned FROM users WHERE discord_id = ?",
        [str(user_id)]
    )
    if result["results"]:
        return result["results"][0]["total_points_earned"] or 0
    return 0


# ── Weekly Reset Functions ───────────────────────────────────────────────────
async def reset_weekly_points(bot: commands.Bot) -> bool:
    """Reset weekly points for all users."""
    try:
        now = datetime.now(timezone.utc).isoformat()
        await d1_query("UPDATE users SET weekly_points = 0, weekly_updated_at = ?", [now])
        print("✅ Weekly points reset completed")

        embed = discord.Embed(
            title="🔄 Weekly Points Reset",
            description="All weekly hoster points have been reset to 0.",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc)
        )
        await send_log(bot, embed)

        return True
    except Exception as e:
        print(f"Error resetting weekly points: {e}")
        return False


async def update_top_hoster_roles(guild: discord.Guild, bot: commands.Bot = None):
    """
    Assign TOP_HOSTER_ROLE_ID to the top 3 total points earned holders.
    ONLY users with total_points_earned > 0 are eligible.
    """
    try:
        # Only get users with points > 0
        result = await d1_query(
            "SELECT discord_id FROM users WHERE total_points_earned > 0 ORDER BY total_points_earned DESC LIMIT 3"
        )
        top_hoster_ids = [int(row["discord_id"]) for row in result["results"]]

        top_role = guild.get_role(TOP_HOSTER_ROLE_ID)
        if not top_role:
            print(f"Top hoster role {TOP_HOSTER_ROLE_ID} not found")
            return

        current_top = [m.id for m in guild.members if top_role in m.roles]

        removed_count = 0
        added_count = 0

        # Remove role from users no longer in top 3 (or who have 0 points)
        for member_id in current_top:
            # Check if user still has points > 0 and is in top 3
            if member_id not in top_hoster_ids:
                member = guild.get_member(member_id)
                if member:
                    await member.remove_roles(top_role, reason="No longer in top 3 hosters or has 0 points")
                    removed_count += 1

        # Add role to new top 3
        medals = ["🥇", "🥈", "🥉"]
        for i, member_id in enumerate(top_hoster_ids):
            member = guild.get_member(member_id)
            if member and top_role not in member.roles:
                await member.add_roles(top_role, reason="Top 3 hoster!")
                added_count += 1

                # DM the user
                try:
                    pts_result = await d1_query(
                        "SELECT total_points_earned FROM users WHERE discord_id = ?",
                        [str(member_id)]
                    )
                    points = pts_result["results"][0]["total_points_earned"] if pts_result["results"] else 0

                    dm_embed = discord.Embed(
                        title="👑 Congratulations!",
                        description=f"{medals[i]} You've made it into the **Top {i + 1} Hosters**!",
                        color=discord.Color.gold(),
                        timestamp=datetime.now(timezone.utc)
                    )
                    dm_embed.add_field(
                        name="Reward",
                        value=f"You've been given the <@&{TOP_HOSTER_ROLE_ID}> role!",
                        inline=False
                    )
                    dm_embed.add_field(
                        name="Your Total Points Earned",
                        value=f"**{points}** lifetime points",
                        inline=True
                    )
                    dm_embed.add_field(
                        name="Keep it up!",
                        value="Maintain your position to keep the role!",
                        inline=False
                    )
                    await member.send(embed=dm_embed)
                except Exception as e:
                    print(f"Could not DM {member.display_name}: {e}")

        # Log the changes
        if removed_count > 0 or added_count > 0:
            embed = discord.Embed(
                title="👑 Top Hosters Updated",
                color=discord.Color.gold(),
                timestamp=datetime.now(timezone.utc)
            )

            top_lines = []
            for i, member_id in enumerate(top_hoster_ids):
                member = guild.get_member(member_id)
                if member:
                    pts_result = await d1_query(
                        "SELECT total_points_earned FROM users WHERE discord_id = ?",
                        [str(member_id)]
                    )
                    points = pts_result["results"][0]["total_points_earned"] if pts_result["results"] else 0
                    top_lines.append(f"{medals[i]} {member.mention} — **{points}** pts")

            if top_lines:
                embed.add_field(name="Current Top 3 Hosters", value="\n".join(top_lines), inline=False)
            if removed_count > 0:
                embed.add_field(name="Roles Removed", value=f"**{removed_count}** users", inline=True)
            if added_count > 0:
                embed.add_field(name="Roles Added", value=f"**{added_count}** users", inline=True)

            await send_log(bot, embed)

    except Exception as e:
        print(f"Error updating top hoster roles: {e}")


async def check_and_warn_hosters(bot: commands.Bot, guild: discord.Guild):
    """DM hosters below 100 weekly points when under 24 hours remain."""
    try:
        hoster_role = guild.get_role(HOSTER_ROLE_ID)
        if not hoster_role:
            return

        hosters = [m for m in guild.members if hoster_role in m.roles]
        below_100 = []

        for member in hosters:
            result = await d1_query(
                "SELECT weekly_points FROM users WHERE discord_id = ?",
                [str(member.id)]
            )
            if not result["results"]:
                continue

            weekly = result["results"][0]["weekly_points"] or 0
            if weekly >= 100:
                continue

            below_100.append((member, weekly))

            try:
                week_end = get_week_end()
                hours_left = int((week_end - datetime.now(timezone.utc)).total_seconds() / 3600)

                dm_embed = discord.Embed(
                    title="⚠️ Weekly Points Warning",
                    description=f"You currently have **{weekly}/100** weekly hoster points.",
                    color=discord.Color.orange(),
                    timestamp=datetime.now(timezone.utc)
                )
                dm_embed.add_field(
                    name="Time Remaining",
                    value=f"About **{hours_left}** hours left in the week",
                    inline=True
                )
                dm_embed.add_field(
                    name="Points Needed",
                    value=f"**{100 - weekly}** more points needed",
                    inline=True
                )
                dm_embed.add_field(
                    name="What happens if I don't reach 100?",
                    value=f"You will lose your <@&{HOSTER_ROLE_ID}> role!",
                    inline=False
                )
                dm_embed.add_field(
                    name="How to earn points",
                    value="Host raids to earn points! Each raid gives points based on waves completed.",
                    inline=False
                )
                dm_embed.set_footer(
                    text=f"Deadline: {week_end.strftime('%b %d, %Y at %I:%M %p UTC')}"
                )
                await member.send(embed=dm_embed)
            except Exception as e:
                print(f"Could not DM {member.display_name}: {e}")

        if below_100:
            log_embed = discord.Embed(
                title="📋 Weekly Points Warnings Sent",
                description=f"**{len(below_100)}** hosters were warned about their weekly points.",
                color=discord.Color.orange(),
                timestamp=datetime.now(timezone.utc)
            )
            lines = [f"{m.mention} — **{pts}/100**" for m, pts in below_100[:10]]
            log_embed.add_field(name="Hosters Below 100", value="\n".join(lines), inline=False)
            await send_log(bot, log_embed)

    except Exception as e:
        print(f"Error checking hoster warnings: {e}")


# ── Hoster Cog ───────────────────────────────────────────────────────────────
class Hoster(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.check_weekly_reset.start()
        self.check_hoster_warnings.start()

    def cog_unload(self):
        self.check_weekly_reset.cancel()
        self.check_hoster_warnings.cancel()

    # ── Weekly Reset Task ─────────────────────────────────────────────────────
    @tasks.loop(minutes=5)
    async def check_weekly_reset(self):
        """
        Fire the weekly reset on Sunday at midnight Eastern.
        Uses bot_meta table to track when the last reset occurred.
        """
        try:
            now_eastern = get_eastern_now()

            # Only run on Sunday between midnight and 00:05
            if not (now_eastern.weekday() == 6 and now_eastern.hour == 0 and now_eastern.minute < 5):
                return

            today_str = now_eastern.strftime("%Y-%m-%d")

            # Check when we last reset
            result = await d1_query(
                "SELECT value FROM bot_meta WHERE key = 'last_weekly_reset'"
            )

            if result["results"]:
                last_reset = result["results"][0]["value"]
                if last_reset == today_str:
                    return  # Already reset today

            # Do the reset
            await reset_weekly_points(self.bot)

            # Record that we did it
            await d1_query(
                """INSERT INTO bot_meta (key, value) VALUES ('last_weekly_reset', ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                [today_str]
            )

            # Update top hoster roles after reset
            guild = self.bot.get_guild(GUILD_ID)
            if guild:
                await update_top_hoster_roles(guild, self.bot)

        except Exception as e:
            print(f"Error in weekly reset check: {e}")

    @check_weekly_reset.before_loop
    async def before_weekly_reset(self):
        await self.bot.wait_until_ready()

    # ── Hoster Warning Task ───────────────────────────────────────────────────
    @tasks.loop(hours=1)
    async def check_hoster_warnings(self):
        """Send DM warnings to hosters below 100 points when <24 hours remain."""
        try:
            now = datetime.now(timezone.utc)
            week_end = get_week_end()
            hours_left = (week_end - now).total_seconds() / 3600

            if 0 < hours_left <= 25:
                guild = self.bot.get_guild(GUILD_ID)
                if guild:
                    await check_and_warn_hosters(self.bot, guild)
        except Exception as e:
            print(f"Error in hoster warnings check: {e}")

    @check_hoster_warnings.before_loop
    async def before_hoster_warnings(self):
        await self.bot.wait_until_ready()

    # ── Public helper (called by raid.py) ─────────────────────────────────────
    async def update_top_hoster(self, guild: discord.Guild):
        await update_top_hoster_roles(guild, self.bot)

    # ── /hleaderboard ─────────────────────────────────────────────────────────
    @app_commands.command(
        name="hosterboard",
        description="Hoster points leaderboard — lifetime and weekly rankings"
    )
    @app_commands.checks.cooldown(1, 10)
    async def hleaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)

        try:
            hoster_role = interaction.guild.get_role(HOSTER_ROLE_ID)
            if not hoster_role:
                return await interaction.followup.send("❌ Hoster role not found.")

            hoster_members = [m for m in interaction.guild.members if hoster_role in m.roles]
            hoster_ids = [str(m.id) for m in hoster_members]

            if not hoster_ids:
                return await interaction.followup.send("No hosters found.")

            placeholders = ','.join(['?'] * len(hoster_ids))

            # Only get users with points > 0 for the leaderboard
            lifetime_result = await d1_query(
                f"SELECT discord_id, total_points_earned FROM users WHERE discord_id IN ({placeholders}) AND total_points_earned > 0 ORDER BY total_points_earned DESC",
                hoster_ids
            )
            weekly_result = await d1_query(
                f"SELECT discord_id, weekly_points FROM users WHERE discord_id IN ({placeholders}) AND weekly_points > 0 ORDER BY weekly_points DESC",
                hoster_ids
            )

            embed = discord.Embed(
                title="🏆 Hoster Points Leaderboard",
                color=discord.Color.gold(),
                timestamp=datetime.now(timezone.utc)
            )

            medals = ["🥇", "🥈", "🥉"]

            # Lifetime points (total earned) - only show users with points > 0
            lifetime_rows = lifetime_result["results"]
            if lifetime_rows:
                lines = []
                for i, row in enumerate(lifetime_rows):
                    prefix = medals[i] if i < 3 else f"`#{i+1}`"
                    member = interaction.guild.get_member(int(row["discord_id"]))
                    name = member.display_name if member else f"<@{row['discord_id']}>"
                    is_elite = is_elite_hoster(member) if member else False
                    elite_tag = " 👑" if is_elite else ""
                    lines.append(f"{prefix} **{name}**{elite_tag} — **{row['total_points_earned'] or 0}** pts")
                embed.add_field(name="🏅 Lifetime Points Earned", value="\n".join(lines), inline=False)
            else:
                embed.add_field(name="🏅 Lifetime Points Earned", value="*No hosters have earned points yet*", inline=False)

            # Weekly points - only show users with points > 0
            weekly_rows = weekly_result["results"]
            week_start = get_week_start()
            week_end = get_week_end()

            if weekly_rows:
                lines = []
                for i, row in enumerate(weekly_rows):
                    prefix = medals[i] if i < 3 else f"`#{i+1}`"
                    member = interaction.guild.get_member(int(row["discord_id"]))
                    name = member.display_name if member else f"<@{row['discord_id']}>"
                    is_elite = is_elite_hoster(member) if member else False
                    elite_tag = " 👑" if is_elite else ""
                    lines.append(f"{prefix} **{name}**{elite_tag} — **{row['weekly_points'] or 0}** pts")
                embed.add_field(
                    name=f"📅 This Week ({week_start.strftime('%b %d')} - {week_end.strftime('%b %d')})",
                    value="\n".join(lines),
                    inline=False
                )
            else:
                embed.add_field(name="📅 This Week", value="*No hosters have earned weekly points yet*", inline=False)

            # Requirements
            embed.add_field(
                name="📋 Weekly Requirement",
                value="Hosters need **100 points per week** to maintain their role!\nPoints reset every **Sunday at midnight ET**.",
                inline=False
            )

            top_role = interaction.guild.get_role(TOP_HOSTER_ROLE_ID)
            if top_role:
                embed.add_field(
                    name="👑 Top Hoster / Elite Hoster Role",
                    value=f"Top 3 lifetime hosters get the <@&{TOP_HOSTER_ROLE_ID}> role!\n"
                          f"**Elite Hosters get 25% bonus XP from hosting raids!**\n\n"
                          f"⚠️ **You must have points to appear on the leaderboard!**",
                    inline=False
                )

            embed.add_field(
                name="💡 Points Info",
                value="**Total Earned** = points you've earned (never decreases)\n"
                      "**Weekly Points** = points earned this week (resets Sunday)\n"
                      "Spending points only affects your balance, not your total earned!\n\n"
                      "📌 **Only users with points appear on the leaderboard**",
                inline=False
            )

            embed.set_footer(text="Points are earned by hosting raids • 👑 = Elite Hoster")
            await interaction.followup.send(embed=embed)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}")

    # ── /points ──────────────────────────────────────────────────────────────
    @app_commands.command(
        name="points",
        description="Check your weekly hoster points (Hoster+ only)"
    )
    @app_commands.checks.cooldown(1, 5)
    async def points(self, interaction: discord.Interaction):
        """Check weekly hoster points - shows balance and total earned"""
        # Hoster+ only
        if not is_hoster(interaction.user) and not is_staff(interaction.user):
            return await interaction.response.send_message(
                "❌ You need the Hoster role to use this command.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        try:
            result = await d1_query(
                "SELECT hoster_points, weekly_points, total_points_earned FROM users WHERE discord_id = ?",
                [str(interaction.user.id)]
            )

            if not result["results"]:
                return await interaction.followup.send(
                    "❌ You don't have a record. Please `/verify` first.",
                    ephemeral=True
                )

            balance = result["results"][0]["hoster_points"] or 0
            weekly = result["results"][0]["weekly_points"] or 0
            total_earned = result["results"][0]["total_points_earned"] or 0
            is_hoster_now = is_hoster(interaction.user)
            is_elite = is_elite_hoster(interaction.user)
            has_top_role = any(role.id == TOP_HOSTER_ROLE_ID for role in interaction.user.roles)

            embed = discord.Embed(
                title=f"📊 {interaction.user.display_name}'s Hoster Points",
                color=discord.Color.blurple(),
                timestamp=datetime.now(timezone.utc)
            )
            if interaction.user.display_avatar:
                embed.set_thumbnail(url=interaction.user.display_avatar.url)

            embed.add_field(name="💰 Current Balance", value=f"**{balance}** pts", inline=True)
            embed.add_field(name="🏅 Total Earned", value=f"**{total_earned}** pts", inline=True)
            embed.add_field(name="📅 Weekly Points", value=f"**{weekly}** pts", inline=True)
            embed.add_field(name="Hoster Role", value="✅ Yes" if is_hoster_now else "❌ No", inline=True)

            if is_elite:
                embed.add_field(name="👑 Elite Hoster", value="✅ **25% XP Bonus!**", inline=True)
            elif has_top_role:
                embed.add_field(name="👑 Top Hoster", value="✅ You're in the top 3!", inline=True)

            # Show XP bonus info for elite hosters
            if is_elite:
                embed.add_field(
                    name="✨ XP Bonus",
                    value="You earn **25% bonus XP** from hosting raids!",
                    inline=False
                )

            embed.add_field(
                name="💡 Points Info",
                value="**Balance** = points you can spend in the shop\n"
                      "**Total Earned** = all points you've ever earned (never decreases)\n"
                      "Spending points only affects your balance!\n"
                      f"📌 **You have {total_earned} total points** - you need points to appear on the leaderboard!",
                inline=False
            )

            week_start = get_week_start()
            week_end = get_week_end()

            if weekly < 100:
                bar = progress_bar(weekly, 100)
                needed = 100 - weekly
                embed.add_field(
                    name="Weekly Progress (Need 100)",
                    value=f"`{bar}` {weekly}/100 ({int(weekly / 100 * 100)}%)",
                    inline=False
                )
                embed.add_field(
                    name="Points Needed",
                    value=f"**{needed}** more points to maintain role",
                    inline=True
                )

                hours_left = int((week_end - datetime.now(timezone.utc)).total_seconds() / 3600)
                if hours_left > 0:
                    embed.add_field(
                        name="⏰ Time Remaining",
                        value=f"About **{hours_left}** hours left in the week",
                        inline=True
                    )
            else:
                embed.add_field(
                    name="✅ Weekly Goal Met!",
                    value=f"You've earned **{weekly}** points this week!\nKeep going to stay on top!",
                    inline=False
                )

            embed.set_footer(
                text=f"Week: {week_start.strftime('%b %d')} - {week_end.strftime('%b %d')} | Resets Sunday at midnight ET"
            )

            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    # ── on_member_update ──────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """Log when a user loses their hoster role."""
        hoster_role = before.guild.get_role(HOSTER_ROLE_ID)
        if not hoster_role:
            return

        # Check if hoster role was removed
        if hoster_role not in before.roles or hoster_role in after.roles:
            return

        embed = discord.Embed(
            title="⚠️ Hoster Role Removed",
            description=f"{after.mention} has lost their hoster role.",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc)
        )

        result = await d1_query(
            "SELECT weekly_points FROM users WHERE discord_id = ?",
            [str(after.id)]
        )

        if result["results"]:
            weekly = result["results"][0]["weekly_points"] or 0
            embed.add_field(name="Weekly Points", value=f"{weekly}/100", inline=True)
            embed.add_field(
                name="Reason",
                value="Did not meet weekly requirement (need 100 points)" if weekly < 100 else "Role removed by staff",
                inline=True
            )

        channel = self.bot.get_channel(LOG_CHANNEL_ID)
        if channel:
            await channel.send(embed=embed)

    # ── /checkhoster ──────────────────────────────────────────────────────────
    @app_commands.command(
        name="checkhoster",
        description="Check all hosters' weekly points (Staff only)"
    )
    @app_commands.checks.cooldown(1, 10)
    async def checkhoster(self, interaction: discord.Interaction):
        if not is_staff(interaction.user):
            return await interaction.response.send_message(
                "❌ You do not have permission to use this command.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        try:
            hoster_role = interaction.guild.get_role(HOSTER_ROLE_ID)
            if not hoster_role:
                return await interaction.followup.send("❌ Hoster role not found.", ephemeral=True)

            hoster_members = [m for m in interaction.guild.members if hoster_role in m.roles]
            hoster_ids = [str(m.id) for m in hoster_members]

            if not hoster_ids:
                return await interaction.followup.send("No hosters found.", ephemeral=True)

            placeholders = ','.join(['?'] * len(hoster_ids))
            result = await d1_query(
                f"SELECT discord_id, hoster_points, total_points_earned, weekly_points FROM users WHERE discord_id IN ({placeholders}) ORDER BY weekly_points DESC",
                hoster_ids
            )

            rows = result["results"]
            if not rows:
                return await interaction.followup.send("No hoster data found.", ephemeral=True)

            week_start = get_week_start()
            week_end = get_week_end()

            below_100 = []
            above_100 = []

            for row in rows:
                member = interaction.guild.get_member(int(row["discord_id"]))
                if not member:
                    continue
                weekly = row["weekly_points"] or 0
                balance = row["hoster_points"] or 0
                total_earned = row["total_points_earned"] or 0
                is_elite = is_elite_hoster(member)
                elite_tag = " 👑" if is_elite else ""
                # Show warning if total_earned == 0
                warning_tag = " ⚠️" if total_earned == 0 else ""
                line = f"{member.mention}{elite_tag}{warning_tag} — Weekly: **{weekly}** | Balance: **{balance}** | Total Earned: **{total_earned}**"
                (below_100 if weekly < 100 else above_100).append(line)

            embed = discord.Embed(
                title="📊 All Hoster Points",
                color=discord.Color.blurple(),
                timestamp=datetime.now(timezone.utc)
            )

            if below_100:
                embed.add_field(
                    name=f"⚠️ Below 100 ({len(below_100)})",
                    value="\n".join(below_100[:20]),
                    inline=False
                )
            else:
                embed.add_field(
                    name="✅ Below 100",
                    value="All hosters are above 100 points!",
                    inline=False
                )

            if above_100:
                embed.add_field(
                    name=f"✅ Above 100 ({len(above_100)})",
                    value="\n".join(above_100[:20]),
                    inline=False
                )

            embed.add_field(
                name="📌 Note",
                value="⚠️ = User has 0 total earned points (won't appear on leaderboard)\n"
                      "👑 = Elite Hoster (top 3)",
                inline=False
            )

            embed.set_footer(
                text=f"Week: {week_start.strftime('%b %d')} - {week_end.strftime('%b %d')} | Resets Sunday at midnight ET"
            )

            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    # ── /forcetophoster ──────────────────────────────────────────────────────
    @app_commands.command(
        name="forcetophoster",
        description="Force update top hoster roles (Staff only)"
    )
    @app_commands.checks.cooldown(1, 30)
    async def forcetophoster(self, interaction: discord.Interaction):
        if not is_mod_or_higher(interaction.user):
            return await interaction.response.send_message(
                "❌ You do not have permission to use this command.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        try:
            await update_top_hoster_roles(interaction.guild, self.bot)
            await interaction.followup.send("✅ Top hoster roles updated successfully!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


async def setup(bot: commands.Bot):
    """Setup function for the cog"""
    await bot.add_cog(Hoster(bot))
    print("✅ Hoster cog setup complete")