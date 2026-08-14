import discord
from discord import app_commands
from discord.ext import commands, tasks
import time
from datetime import datetime, timezone

from bot import (
    d1_query,
    GUILD_ID,
    FOUNDER_ROLE_ID,
    HEAD_STAFF_ROLE_ID,
    LOG_CHANNELS,
)

TRIAL_HOSTER_ROLE_ID = 1537778520217096230
HOSTER_ROLE_ID       = 1537778520217096231

TRIAL_DURATION = 7 * 24 * 3600
POINTS_REQUIRED = 100


async def _ensure_tables():
    await d1_query(
        "CREATE TABLE IF NOT EXISTS trial_hosters "
        "(user_id TEXT PRIMARY KEY, trial_start INTEGER NOT NULL, "
        "points_baseline INTEGER NOT NULL DEFAULT 0, milestone_notified INTEGER NOT NULL DEFAULT 0)"
    )
    await d1_query(
        "CREATE TABLE IF NOT EXISTS trial_history "
        "(user_id TEXT PRIMARY KEY, failed_count INTEGER NOT NULL DEFAULT 0, last_failed INTEGER)"
    )


class TrialHoster(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.loop.create_task(self._startup())

    async def _startup(self):
        await self.bot.wait_until_ready()
        await _ensure_tables()
        self._check_loop.start()
        print("✅ Trial Hoster cog ready")

    def cog_unload(self):
        self._check_loop.cancel()

    # ── Background task ───────────────────────────────────────────────────────

    @tasks.loop(minutes=2)
    async def _check_loop(self):
        try:
            rows = await d1_query(
                "SELECT user_id, trial_start, points_baseline, milestone_notified FROM trial_hosters"
            )
            if not rows["results"]:
                return

            guild = self.bot.get_guild(GUILD_ID)
            if not guild:
                return

            now = int(time.time())
            for row in rows["results"]:
                uid = int(row["user_id"])
                trial_start = row["trial_start"]
                baseline = row["points_baseline"]
                milestone_notified = row["milestone_notified"]

                member = guild.get_member(uid)
                if not member:
                    await d1_query("DELETE FROM trial_hosters WHERE user_id = ?", [str(uid)])
                    continue

                user_row = await d1_query(
                    "SELECT total_points_earned FROM users WHERE discord_id = ?", [str(uid)]
                )
                total = user_row["results"][0]["total_points_earned"] if user_row["results"] else 0
                trial_pts = max(0, total - baseline)
                elapsed = now - trial_start

                if trial_pts >= POINTS_REQUIRED:
                    await self._promote(guild, member, trial_pts)
                elif elapsed >= TRIAL_DURATION:
                    await self._expire(guild, member, trial_pts)
                elif trial_pts >= 50 and not milestone_notified:
                    await d1_query(
                        "UPDATE trial_hosters SET milestone_notified = 1 WHERE user_id = ?",
                        [str(uid)]
                    )
                    days_left = max(0, (TRIAL_DURATION - elapsed) // 86400)
                    try:
                        await member.send(
                            f"🔥 Halfway there! You've earned **50/{POINTS_REQUIRED} trial points** "
                            f"in **{guild.name}**!\n"
                            f"You have **{days_left} days** left. Keep hosting to become **Hoster**!"
                        )
                    except discord.HTTPException:
                        pass
        except Exception as e:
            print(f"[TrialHoster] check_loop error: {e}")

    # ── Called from raid.py after each point award ────────────────────────────

    async def check_user_promotion(self, guild: discord.Guild, user_id: int):
        try:
            row = await d1_query(
                "SELECT trial_start, points_baseline, milestone_notified FROM trial_hosters WHERE user_id = ?",
                [str(user_id)]
            )
            if not row["results"]:
                return

            baseline = row["results"][0]["points_baseline"]
            trial_start = row["results"][0]["trial_start"]
            milestone_notified = row["results"][0]["milestone_notified"]

            user_row = await d1_query(
                "SELECT total_points_earned FROM users WHERE discord_id = ?", [str(user_id)]
            )
            total = user_row["results"][0]["total_points_earned"] if user_row["results"] else 0
            trial_pts = max(0, total - baseline)

            member = guild.get_member(user_id)
            if not member:
                return

            if trial_pts >= POINTS_REQUIRED:
                await self._promote(guild, member, trial_pts)
            elif trial_pts >= 50 and not milestone_notified:
                await d1_query(
                    "UPDATE trial_hosters SET milestone_notified = 1 WHERE user_id = ?",
                    [str(user_id)]
                )
                now = int(time.time())
                days_left = max(0, (TRIAL_DURATION - (now - trial_start)) // 86400)
                try:
                    await member.send(
                        f"🔥 Halfway there! You've earned **50/{POINTS_REQUIRED} trial points** "
                        f"in **{guild.name}**!\n"
                        f"You have **{days_left} days** left. Keep hosting to become **Hoster**!"
                    )
                except discord.HTTPException:
                    pass
        except Exception as e:
            print(f"[TrialHoster] check_user_promotion error: {e}")

    # ── Public: called by ticket accept ──────────────────────────────────────

    async def setup_trial(self, guild: discord.Guild, member: discord.Member, given_by: discord.Member):
        trial_role = guild.get_role(TRIAL_HOSTER_ROLE_ID)

        user_row = await d1_query(
            "SELECT total_points_earned FROM users WHERE discord_id = ?", [str(member.id)]
        )
        baseline = user_row["results"][0]["total_points_earned"] if user_row["results"] else 0

        now_ts = int(time.time())
        await d1_query(
            "INSERT INTO trial_hosters (user_id, trial_start, points_baseline, milestone_notified) "
            "VALUES (?, ?, ?, 0) ON CONFLICT(user_id) DO UPDATE SET "
            "trial_start = excluded.trial_start, points_baseline = excluded.points_baseline, milestone_notified = 0",
            [str(member.id), now_ts, baseline]
        )

        if trial_role:
            try:
                await member.add_roles(trial_role, reason=f"Trial Hoster given by {given_by}")
            except discord.HTTPException:
                pass

        expire_ts = now_ts + TRIAL_DURATION
        try:
            await member.send(
                f"🎯 You've been given **Trial Hoster** in **{guild.name}**!\n\n"
                f"You have **7 days** (expires <t:{expire_ts}:F>) to earn **{POINTS_REQUIRED} points** "
                f"by hosting raids.\nReach {POINTS_REQUIRED} points and you'll automatically become **Hoster**!"
            )
        except discord.HTTPException:
            pass

        log_ch = guild.get_channel(LOG_CHANNELS.get("trial_hoster", 0))
        if log_ch:
            embed = discord.Embed(
                title="🎯 Trial Hoster Given",
                color=discord.Color.blue(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="User", value=f"{member.mention} ({member})", inline=True)
            embed.add_field(name="Given By", value=given_by.mention, inline=True)
            embed.add_field(name="Expires", value=f"<t:{expire_ts}:R>", inline=True)
            try:
                await log_ch.send(embed=embed)
            except discord.HTTPException:
                pass

    # ── Internal promote / expire ─────────────────────────────────────────────

    async def _promote(self, guild: discord.Guild, member: discord.Member, trial_pts: int):
        trial_role = guild.get_role(TRIAL_HOSTER_ROLE_ID)
        hoster_role = guild.get_role(HOSTER_ROLE_ID)
        log_ch = guild.get_channel(LOG_CHANNELS.get("trial_hoster", 0))

        try:
            if trial_role and trial_role in member.roles:
                await member.remove_roles(trial_role, reason="Trial complete — promoted to Hoster")
            if hoster_role and hoster_role not in member.roles:
                await member.add_roles(hoster_role, reason="Trial complete — 100 points reached")
        except discord.HTTPException:
            pass

        await d1_query("DELETE FROM trial_hosters WHERE user_id = ?", [str(member.id)])
        await d1_query(
            "INSERT INTO trial_history (user_id, failed_count, last_failed) VALUES (?, 0, NULL) "
            "ON CONFLICT(user_id) DO UPDATE SET failed_count = 0, last_failed = NULL",
            [str(member.id)]
        )

        try:
            await member.send(
                f"🎉 **Congratulations!** You've been promoted to **Hoster** in **{guild.name}**!\n"
                f"You earned **{trial_pts}/{POINTS_REQUIRED} points** during your trial. Well done!"
            )
        except discord.HTTPException:
            pass

        if log_ch:
            embed = discord.Embed(
                title="✅ Trial Hoster Promoted",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="User", value=f"{member.mention} ({member})", inline=True)
            embed.add_field(name="Points Earned", value=f"{trial_pts}/{POINTS_REQUIRED}", inline=True)
            try:
                await log_ch.send(embed=embed)
            except discord.HTTPException:
                pass

    async def _expire(self, guild: discord.Guild, member: discord.Member, trial_pts: int):
        trial_role = guild.get_role(TRIAL_HOSTER_ROLE_ID)
        log_ch = guild.get_channel(LOG_CHANNELS.get("trial_hoster", 0))

        try:
            if trial_role and trial_role in member.roles:
                await member.remove_roles(trial_role, reason="Trial expired — 100 points not reached in 7 days")
        except discord.HTTPException:
            pass

        await d1_query("DELETE FROM trial_hosters WHERE user_id = ?", [str(member.id)])

        now = int(time.time())
        await d1_query(
            "INSERT INTO trial_history (user_id, failed_count, last_failed) VALUES (?, 1, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET failed_count = failed_count + 1, last_failed = ?",
            [str(member.id), now, now]
        )

        try:
            await member.send(
                f"⏰ Your **Trial Hoster** period in **{guild.name}** has ended.\n"
                f"You reached **{trial_pts}/{POINTS_REQUIRED} points** and did not meet the requirement.\n"
                f"Your Trial Hoster role has been removed. Contact staff if you have questions."
            )
        except discord.HTTPException:
            pass

        if log_ch:
            embed = discord.Embed(
                title="⏰ Trial Hoster Expired",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="User", value=f"{member.mention} ({member})", inline=True)
            embed.add_field(name="Points Reached", value=f"{trial_pts}/{POINTS_REQUIRED}", inline=True)
            try:
                await log_ch.send(embed=embed)
            except discord.HTTPException:
                pass

    # ── Commands ─────────────────────────────────────────────────────────────

    trial_group = app_commands.Group(
        name="trialhoster",
        description="Trial Hoster management"
    )

    def _is_auth(self, member: discord.Member) -> bool:
        return any(r.id in (HEAD_STAFF_ROLE_ID, FOUNDER_ROLE_ID) for r in member.roles)

    @trial_group.command(name="give", description="Give a user the Trial Hoster role")
    @app_commands.describe(user="User to give Trial Hoster to")
    async def give(self, interaction: discord.Interaction, user: discord.Member):
        if not self._is_auth(interaction.user):
            return await interaction.response.send_message(
                "❌ Only Head Staff and Founders can use this.", ephemeral=True
            )

        try:
            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException:
            return

        trial_role = interaction.guild.get_role(TRIAL_HOSTER_ROLE_ID)
        hoster_role = interaction.guild.get_role(HOSTER_ROLE_ID)

        if trial_role and trial_role in user.roles:
            return await interaction.followup.send(
                f"❌ {user.mention} already has Trial Hoster.", ephemeral=True
            )
        if hoster_role and hoster_role in user.roles:
            return await interaction.followup.send(
                f"❌ {user.mention} is already a Hoster.", ephemeral=True
            )

        history = await d1_query(
            "SELECT failed_count FROM trial_history WHERE user_id = ?", [str(user.id)]
        )
        warning = ""
        if history["results"]:
            failed = history["results"][0]["failed_count"]
            if failed > 0:
                warning = f"\n\n⚠️ **Warning:** This user has failed **{failed}** previous trial attempt(s)."

        await self.setup_trial(interaction.guild, user, given_by=interaction.user)

        expire_ts = int(time.time()) + TRIAL_DURATION
        await interaction.followup.send(
            f"✅ {user.mention} has been given **Trial Hoster**. "
            f"They have 7 days (until <t:{expire_ts}:F>) to earn {POINTS_REQUIRED} points.{warning}",
            ephemeral=True
        )

    @trial_group.command(name="remove", description="Remove Trial Hoster from a user")
    @app_commands.describe(user="User to remove Trial Hoster from")
    async def remove(self, interaction: discord.Interaction, user: discord.Member):
        if not self._is_auth(interaction.user):
            return await interaction.response.send_message(
                "❌ Only Head Staff and Founders can use this.", ephemeral=True
            )

        try:
            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException:
            return

        trial_role = interaction.guild.get_role(TRIAL_HOSTER_ROLE_ID)
        row = await d1_query(
            "SELECT user_id FROM trial_hosters WHERE user_id = ?", [str(user.id)]
        )

        if not row["results"] and (not trial_role or trial_role not in user.roles):
            return await interaction.followup.send(
                f"❌ {user.mention} is not a Trial Hoster.", ephemeral=True
            )

        if trial_role and trial_role in user.roles:
            try:
                await user.remove_roles(trial_role, reason=f"Trial Hoster removed by {interaction.user}")
            except discord.HTTPException:
                pass

        await d1_query("DELETE FROM trial_hosters WHERE user_id = ?", [str(user.id)])

        try:
            await user.send(
                f"Your **Trial Hoster** role in **{interaction.guild.name}** has been removed by staff."
            )
        except discord.HTTPException:
            pass

        log_ch = interaction.guild.get_channel(LOG_CHANNELS.get("trial_hoster", 0))
        if log_ch:
            embed = discord.Embed(
                title="🗑️ Trial Hoster Removed",
                color=discord.Color.orange(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="User", value=f"{user.mention} ({user})", inline=True)
            embed.add_field(name="Removed By", value=interaction.user.mention, inline=True)
            try:
                await log_ch.send(embed=embed)
            except discord.HTTPException:
                pass

        await interaction.followup.send(
            f"✅ Removed Trial Hoster from {user.mention}.", ephemeral=True
        )

    @trial_group.command(name="list", description="View all active Trial Hosters and their progress")
    async def list_trials(self, interaction: discord.Interaction):
        if not self._is_auth(interaction.user):
            return await interaction.response.send_message(
                "❌ Only Head Staff and Founders can use this.", ephemeral=True
            )

        try:
            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException:
            return

        rows = await d1_query(
            "SELECT user_id, trial_start, points_baseline FROM trial_hosters"
        )
        if not rows["results"]:
            return await interaction.followup.send("No active Trial Hosters.", ephemeral=True)

        now = int(time.time())
        embed = discord.Embed(
            title="📋 Active Trial Hosters",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc)
        )

        lines = []
        for row in rows["results"]:
            uid = int(row["user_id"])
            trial_start = row["trial_start"]
            baseline = row["points_baseline"]
            elapsed = now - trial_start
            time_left = max(0, TRIAL_DURATION - elapsed)
            days_left = time_left // 86400
            hours_left = (time_left % 86400) // 3600

            user_row = await d1_query(
                "SELECT total_points_earned FROM users WHERE discord_id = ?", [str(uid)]
            )
            total = user_row["results"][0]["total_points_earned"] if user_row["results"] else 0
            trial_pts = max(0, total - baseline)

            member = interaction.guild.get_member(uid)
            name = member.mention if member else f"<@{uid}>"
            filled = min(10, int(trial_pts / POINTS_REQUIRED * 10))
            bar = "█" * filled + "░" * (10 - filled)
            lines.append(
                f"{name}\n`{bar}` **{trial_pts}/{POINTS_REQUIRED} pts** — ⏰ {days_left}d {hours_left}h left"
            )

        embed.description = "\n\n".join(lines)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @trial_group.command(name="status", description="Check your Trial Hoster progress")
    async def status(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException:
            return

        row = await d1_query(
            "SELECT trial_start, points_baseline FROM trial_hosters WHERE user_id = ?",
            [str(interaction.user.id)]
        )
        if not row["results"]:
            return await interaction.followup.send(
                "❌ You are not currently a Trial Hoster.", ephemeral=True
            )

        trial_start = row["results"][0]["trial_start"]
        baseline = row["results"][0]["points_baseline"]

        now = int(time.time())
        elapsed = now - trial_start
        time_left = max(0, TRIAL_DURATION - elapsed)
        days_left = time_left // 86400
        hours_left = (time_left % 86400) // 3600

        user_row = await d1_query(
            "SELECT total_points_earned FROM users WHERE discord_id = ?",
            [str(interaction.user.id)]
        )
        total = user_row["results"][0]["total_points_earned"] if user_row["results"] else 0
        trial_pts = max(0, total - baseline)

        trial_dt = datetime.fromtimestamp(trial_start, timezone.utc).isoformat()
        raid_row = await d1_query(
            "SELECT COUNT(*) as cnt FROM raids "
            "WHERE host_discord_ids LIKE ? AND time_started >= ? AND is_completed = 1",
            [f"%{interaction.user.id}%", trial_dt]
        )
        raid_count = raid_row["results"][0]["cnt"] if raid_row["results"] else 0

        filled = min(10, int(trial_pts / POINTS_REQUIRED * 10))
        bar = "█" * filled + "░" * (10 - filled)
        pct = min(100, int(trial_pts / POINTS_REQUIRED * 100))
        expire_ts = trial_start + TRIAL_DURATION
        pts_needed = max(0, POINTS_REQUIRED - trial_pts)

        embed = discord.Embed(
            title="🎯 Trial Hoster Status",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(
            name="Progress",
            value=f"`{bar}` **{trial_pts}/{POINTS_REQUIRED}** pts ({pct}%)",
            inline=False
        )
        embed.add_field(name="Time Left", value=f"{days_left}d {hours_left}h", inline=True)
        embed.add_field(name="Raids Hosted", value=str(raid_count), inline=True)
        embed.add_field(name="Expires", value=f"<t:{expire_ts}:R>", inline=True)
        embed.set_footer(text=f"Earn {pts_needed} more point(s) to be promoted to Hoster!")

        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(TrialHoster(bot))
    print("✅ Trial Hoster cog loaded")
