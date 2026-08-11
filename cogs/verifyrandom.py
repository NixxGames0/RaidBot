# cogs/verifyrandom.py
import discord
from discord.ext import commands
import random
import json
import asyncio
from datetime import datetime, timezone, timedelta

from bot import d1_query, GUILD_ID, LINKED_ROLE_ID
from cogs.verify import send_verification_log, send_log

# ── Role IDs ──────────────────────────────────────────────────────────────
RAID_PINGS_ROLE_ID      = 1535696574150090772
GIVEAWAY_PINGS_ROLE_ID  = 1536324869422063626
UNRANKED_ROLE_ID        = 1536659018549039214

# ── Probabilities ────────────────────────────────────────────────────────
LINK_CHANCE          = 0.60   # 60% chance to be verified
RAID_PINGS_CHANCE    = 0.80   # 80% chance to get Raid Pings role
GIVEAWAY_PINGS_CHANCE= 0.60   # 60% chance to get Giveaway Pings role
DELAY_MIN            = 30     # seconds between verifications
DELAY_MAX            = 120


class VerifyRandom(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.loop.create_task(self._startup_verification())

    async def _startup_verification(self):
        """Run once when the bot is ready – processes members joined <2h ago."""
        await self.bot.wait_until_ready()

        # Ensure we only run once per session
        if hasattr(self, '_started'):
            return
        self._started = True

        guild = self.bot.get_guild(GUILD_ID)
        if not guild:
            print("❌ Guild not found for startup verification.")
            return

        now = datetime.now(timezone.utc)
        two_hours_ago = now - timedelta(hours=2)

        # Find all members who joined less than 2h ago and are NOT linked
        eligible = []
        for member in guild.members:
            if member.joined_at and member.joined_at > two_hours_ago:
                if not any(role.id == LINKED_ROLE_ID for role in member.roles):
                    eligible.append(member)

        if not eligible:
            print("📭 No eligible members found for startup verification.")
            return

        print(f"🔍 Found {len(eligible)} eligible members. Processing with {LINK_CHANCE*100:.0f}% chance each...")

        verified_count = 0
        for member in eligible:
            if random.random() > LINK_CHANCE:
                continue

            success = await self._verify_one(member, guild)
            if success:
                verified_count += 1
                delay = random.randint(DELAY_MIN, DELAY_MAX)
                await asyncio.sleep(delay)

        print(f"✅ Startup verification complete: {verified_count} user(s) verified.")

    async def _verify_one(self, member: discord.Member, guild: discord.Guild) -> bool:
        """
        Perform a single verification.
        Logs are indistinguishable from real user verifications.
        """
        try:
            # Generate a unique Roblox username (fake)
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

            # Create or update user record
            existing = await d1_query(
                "SELECT discord_id, roblox_users FROM users WHERE discord_id = ?",
                [str(member.id)]
            )

            is_new_user = False
            if existing["results"]:
                current = json.loads(existing["results"][0]["roblox_users"] or "[]")
                if roblox_name not in current:
                    current.append(roblox_name)
                    await d1_query(
                        "UPDATE users SET roblox_users = ?, updated_at = ? WHERE discord_id = ?",
                        [json.dumps(current), now_str, str(member.id)]
                    )
            else:
                is_new_user = True
                await d1_query(
                    """INSERT INTO users
                    (discord_id, roblox_users, ps_codes, in_raid, hoster_points, weekly_points,
                     global_waves, raids_completed, risk_value, level, exp, invited_by, created_at, updated_at)
                    VALUES (?, ?, '[]', 0, 0, 0, 0, 0, 0, 1, 0, ?, ?, ?)""",
                    [str(member.id), json.dumps([roblox_name]), None, now_str, now_str]
                )

            # ── Assign roles (always Linked + Unranked) ──────────────────
            linked_role = guild.get_role(LINKED_ROLE_ID)
            if linked_role and linked_role not in member.roles:
                await member.add_roles(linked_role, reason="Verification")

            unranked_role = guild.get_role(UNRANKED_ROLE_ID)
            if unranked_role and unranked_role not in member.roles:
                await member.add_roles(unranked_role, reason="PvP Unranked")

            # Raid Pings – 80% chance
            if random.random() < RAID_PINGS_CHANCE:
                raid_role = guild.get_role(RAID_PINGS_ROLE_ID)
                if raid_role and raid_role not in member.roles:
                    await member.add_roles(raid_role, reason="Raid Pings")

            # Giveaway Pings – 60% chance
            if random.random() < GIVEAWAY_PINGS_CHANCE:
                giveaway_role = guild.get_role(GIVEAWAY_PINGS_ROLE_ID)
                if giveaway_role and giveaway_role not in member.roles:
                    await member.add_roles(giveaway_role, reason="Giveaway Pings")

            # ── Random verification method ──────────────────────────────
            # Choose between "Bio Code" and "Join Game" – both log identically
            method = random.choice(["Bio Code", "Join Game"])
            if method == "Bio Code":
                log_title = "🔗 Account Linked"
                main_title = "🔗 Verification Complete"
                main_desc = f"{member.mention} has verified their Roblox account!"
                method_display = "✅ Ownership Verified"
            else:  # Join Game
                log_title = "🔗 Account Linked (Game Join)"
                main_title = "🔗 Verification Complete (Game Join)"
                main_desc = f"{member.mention} verified via the RHVerif Roblox game!"
                method_display = "🎮 RHVerif Game Join"

            # ── Verification log (staff channel) ──────────────────────────
            log_embed = discord.Embed(
                title=log_title,
                color=discord.Color.blue(),
                timestamp=datetime.now(timezone.utc)
            )
            log_embed.add_field(name="Discord", value=member.mention, inline=True)
            log_embed.add_field(name="Roblox Username", value=f"`{roblox_name}`", inline=True)
            log_embed.add_field(name="Roblox ID", value=f"`{roblox_id}`", inline=True)
            log_embed.add_field(name="Verification Method", value=method_display, inline=True)
            if is_new_user:
                log_embed.add_field(name="New User", value="✅ Yes", inline=True)
            log_embed.set_footer(text=f"User ID: {member.id}")
            await send_verification_log(self.bot, log_embed)

            # ── Main log (public log channel) ─────────────────────────────
            main_embed = discord.Embed(
                title=main_title,
                description=main_desc,
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc)
            )
            main_embed.add_field(name="Roblox", value=f"`{roblox_name}`", inline=True)
            await send_log(self.bot, main_embed)

            return True

        except Exception as e:
            print(f"❌ Error verifying {member.id}: {e}")
            return False


async def setup(bot: commands.Bot):
    await bot.add_cog(VerifyRandom(bot))
    print("✅ VerifyRandom startup cog loaded")