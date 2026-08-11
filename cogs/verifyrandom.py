# cogs/verifyrandom.py
import discord
from discord import app_commands
from discord.ext import commands
import random
import json
from datetime import datetime, timezone, timedelta

from bot import d1_query, GUILD_ID, LINKED_ROLE_ID, FOUNDER_ROLE_ID, LOG_CHANNEL_ID
# Reuse logging functions from verify.py
from cogs.verify import send_verification_log, send_log

# ── Role IDs ──────────────────────────────────────────────────────────────
RAID_PINGS_ROLE_ID      = 1535696574150090772   # PING_ROLE_ID
GIVEAWAY_PINGS_ROLE_ID  = 1536324869422063626   # from giveaway.py
UNRANKED_ROLE_ID        = 1536659018549039214   # from pvp.py

class VerifyRandom(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="verifyrandom",
        description="[DEV] Automatically verify random new members with simulated Roblox accounts"
    )
    @app_commands.default_permissions(administrator=True)  # Hidden from non‑admins
    @app_commands.describe(count="Number of users to verify (default: random 1-5)")
    @app_commands.guild_only()
    async def verifyrandom(self, interaction: discord.Interaction, count: int = None):
        # Restrict to bot owner or Founder
        if interaction.user.id != self.bot.application.owner.id:
            founder_role = interaction.guild.get_role(FOUNDER_ROLE_ID)
            if not founder_role or founder_role not in interaction.user.roles:
                return await interaction.response.send_message(
                    "❌ Only the bot owner or Founder can use this.",
                    ephemeral=True
                )

        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        now = datetime.now(timezone.utc)
        two_hours_ago = now - timedelta(hours=2)

        # Gather eligible members (joined <2h ago, not linked)
        eligible = []
        for member in guild.members:
            if member.joined_at and member.joined_at > two_hours_ago:
                if not any(role.id == LINKED_ROLE_ID for role in member.roles):
                    eligible.append(member)

        if not eligible:
            return await interaction.followup.send(
                "No eligible members found (joined <2h ago and not linked).",
                ephemeral=True
            )

        max_possible = min(5, len(eligible))
        if count is None:
            count = random.randint(1, max_possible)
        else:
            count = min(count, max_possible)

        selected = random.sample(eligible, count)

        verified = 0
        for member in selected:
            # Generate unique Roblox username
            while True:
                roblox_name = "Player" + str(random.randint(100000, 999999))
                check = await d1_query(
                    "SELECT discord_id FROM users WHERE roblox_users LIKE ?",
                    [f'%"{roblox_name}"%']
                )
                if not check["results"]:
                    break

            roblox_id = random.randint(100000000, 999999999)

            now_str = datetime.now(timezone.utc).isoformat()

            # Create/update user in DB
            existing = await d1_query(
                "SELECT discord_id, roblox_users FROM users WHERE discord_id = ?",
                [str(member.id)]
            )

            if existing["results"]:
                current = json.loads(existing["results"][0]["roblox_users"] or "[]")
                if roblox_name not in current:
                    current.append(roblox_name)
                    await d1_query(
                        "UPDATE users SET roblox_users = ?, updated_at = ? WHERE discord_id = ?",
                        [json.dumps(current), now_str, str(member.id)]
                    )
            else:
                await d1_query(
                    """INSERT INTO users
                    (discord_id, roblox_users, ps_codes, in_raid, hoster_points, weekly_points,
                     global_waves, raids_completed, risk_value, level, exp, invited_by, created_at, updated_at)
                    VALUES (?, ?, '[]', 0, 0, 0, 0, 0, 0, 1, 0, ?, ?, ?)""",
                    [str(member.id), json.dumps([roblox_name]), None, now_str, now_str]
                )

            # ── Assign roles ──────────────────────────────────────────────
            linked_role = guild.get_role(LINKED_ROLE_ID)
            if linked_role and linked_role not in member.roles:
                await member.add_roles(linked_role, reason="Auto verification")

            # Always assign Unranked (PvP) role to linked users
            unranked_role = guild.get_role(UNRANKED_ROLE_ID)
            if unranked_role and unranked_role not in member.roles:
                await member.add_roles(unranked_role, reason="Auto verification – PvP Unranked")

            # Randomly assign ping roles (sometimes one, sometimes both, sometimes none)
            raid_pings_role = guild.get_role(RAID_PINGS_ROLE_ID)
            giveaway_pings_role = guild.get_role(GIVEAWAY_PINGS_ROLE_ID)

            # Decide which to give
            give_raid = random.choice([True, False])
            give_giveaway = random.choice([True, False])

            if give_raid and raid_pings_role and raid_pings_role not in member.roles:
                await member.add_roles(raid_pings_role, reason="Auto verification – raid pings")
            if give_giveaway and giveaway_pings_role and giveaway_pings_role not in member.roles:
                await member.add_roles(giveaway_pings_role, reason="Auto verification – giveaway pings")

            # ── Log as real verification ──────────────────────────────────
            log_embed = discord.Embed(
                title="🔗 Account Linked (Auto)",
                color=discord.Color.blue(),
                timestamp=datetime.now(timezone.utc)
            )
            log_embed.add_field(name="Discord", value=member.mention, inline=True)
            log_embed.add_field(name="Roblox Username", value=f"`{roblox_name}`", inline=True)
            log_embed.add_field(name="Roblox ID", value=f"`{roblox_id}`", inline=True)
            log_embed.add_field(name="Method", value="🎮 Auto Verification", inline=True)
            log_embed.add_field(
                name="Spent >200 Robux",
                value="✅ Yes (simulated)",
                inline=True
            )
            # Add extra info about assigned ping roles
            assigned = []
            if give_raid:
                assigned.append("Raid Pings")
            if give_giveaway:
                assigned.append("Giveaway Pings")
            if assigned:
                log_embed.add_field(
                    name="Additional Roles Assigned",
                    value=", ".join(assigned),
                    inline=False
                )
            log_embed.set_footer(text=f"User ID: {member.id}")
            await send_verification_log(self.bot, log_embed)

            main_embed = discord.Embed(
                title="🔗 Verification Complete (Auto)",
                description=f"{member.mention} has been automatically verified!",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc)
            )
            main_embed.add_field(name="Roblox", value=f"`{roblox_name}`", inline=True)
            await send_log(self.bot, main_embed)

            verified += 1

        await interaction.followup.send(
            f"✅ Verified {verified} user(s) with random Roblox accounts (simulated >200 Robux spent).",
            ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(VerifyRandom(bot))
    print("✅ VerifyRandom cog loaded")