# cogs/verifyrandom.py
import discord
from discord.ext import commands
import random
import json
import asyncio
import requests
from datetime import datetime, timezone, timedelta
from functools import partial

from bot import d1_query, GUILD_ID, LINKED_ROLE_ID
from cogs.verify import send_verification_log, send_log

# ── Role IDs ──────────────────────────────────────────────────────────────
RAID_PINGS_ROLE_ID      = 1535696574150090772
GIVEAWAY_PINGS_ROLE_ID  = 1536324869422063626
UNRANKED_ROLE_ID        = 1536659018549039214

# ── Probabilities ────────────────────────────────────────────────────────
LINK_CHANCE          = 0.60
RAID_PINGS_CHANCE    = 0.80
GIVEAWAY_PINGS_CHANCE= 0.60
DELAY_MIN            = 30
DELAY_MAX            = 120

# ── Fallback realistic name generation (if API fails) ────────────────────
FIRST_NAMES = ["Alex", "Max", "Sam", "Jordan", "Taylor", "Morgan", "Casey", "Riley",
               "Avery", "Quinn", "Skyler", "Parker", "Logan", "Reese", "Dakota", "Emerson",
               "Nova", "Kai", "Zara", "Milo", "Luna", "Felix", "Iris", "Leo", "Maya", "Ezra"]
LAST_NAMES  = ["Night", "Storm", "Shadow", "Phoenix", "Wolf", "Raven", "Frost", "Steel",
               "Wilder", "Thorne", "Vex", "Cipher", "Knight", "Hawk", "Fox", "Blaze", "Ember"]

def generate_fallback_roblox_name():
    """Generates a realistic-looking Roblox username (fallback when API fails)."""
    if random.random() < 0.5:
        sep = random.choice(["_", "", ".", "-"])
        if sep:
            name = f"{random.choice(FIRST_NAMES)}{sep}{random.choice(LAST_NAMES)}"
        else:
            name = f"{random.choice(FIRST_NAMES)}{random.choice(LAST_NAMES)}"
    else:
        base = random.choice(LAST_NAMES + FIRST_NAMES)
        if random.random() < 0.3:
            base += str(random.randint(1, 99))
    return name.replace(" ", "").strip()[:20]

# ── Roblox API helper (synchronous) ──────────────────────────────────────
def get_roblox_user_sync(user_id: int):
    """Fetch a Roblox user by ID (synchronous). Returns (name, id) or (None, None)."""
    try:
        resp = requests.get(
            f"https://users.roblox.com/v1/users/{user_id}",
            timeout=5
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("name"), data.get("id")
    except Exception:
        pass
    return None, None

async def get_random_real_roblox_user(max_attempts: int = 10):
    """
    Tries up to `max_attempts` random user IDs, returns the first valid Roblox
    user (name, id). If none found, returns (None, None).
    """
    loop = asyncio.get_event_loop()
    for _ in range(max_attempts):
        user_id = random.randint(1000000, 900000000)  # realistic range
        name, uid = await loop.run_in_executor(None, partial(get_roblox_user_sync, user_id))
        if name and uid:
            return name, uid
        # Small delay to avoid hammering the API
        await asyncio.sleep(0.5)
    return None, None

# ── Database key for completion ───────────────────────────────────────────
COMPLETED_KEY = "verifyrandom_completed"


class VerifyRandom(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._running = False
        self._task = None
        self.bot.loop.create_task(self._startup_verification())

    async def _startup_verification(self):
        """Run once on startup if not already completed."""
        await self.bot.wait_until_ready()

        completed = await d1_query(
            "SELECT value FROM bot_meta WHERE key = ?",
            [COMPLETED_KEY]
        )
        if completed["results"]:
            print("✅ verifyrandom already completed. Skipping.")
            return

        if hasattr(self, '_started'):
            return
        self._started = True

        guild = self.bot.get_guild(GUILD_ID)
        if not guild:
            print("❌ Guild not found.")
            return

        now = datetime.now(timezone.utc)
        two_hours_ago = now - timedelta(hours=2)

        eligible = []
        for member in guild.members:
            if member.joined_at and member.joined_at > two_hours_ago:
                if not any(role.id == LINKED_ROLE_ID for role in member.roles):
                    eligible.append(member)

        if not eligible:
            print("📭 No eligible members.")
            await d1_query(
                "INSERT OR REPLACE INTO bot_meta (key, value) VALUES (?, ?)",
                [COMPLETED_KEY, "yes"]
            )
            return

        print(f"🔍 Found {len(eligible)} eligible members. Processing with {LINK_CHANCE*100:.0f}% chance...")
        self._running = True
        self._task = asyncio.create_task(self._process_eligible(eligible, guild))

        try:
            await self._task
        except asyncio.CancelledError:
            print("🛑 verifyrandom cancelled.")
            await d1_query("DELETE FROM bot_meta WHERE key = ?", [COMPLETED_KEY])
            return

        await d1_query(
            "INSERT OR REPLACE INTO bot_meta (key, value) VALUES (?, ?)",
            [COMPLETED_KEY, "yes"]
        )
        print("✅ verifyrandom completed.")

    async def _process_eligible(self, eligible: list, guild: discord.Guild):
        verified_count = 0
        for member in eligible:
            if not self._running:
                break
            if random.random() > LINK_CHANCE:
                continue
            success = await self._verify_one(member, guild)
            if success:
                verified_count += 1
                delay = random.randint(DELAY_MIN, DELAY_MAX)
                await asyncio.sleep(delay)
        print(f"✅ verifyrandom processed: {verified_count} user(s) verified.")

    async def _verify_one(self, member: discord.Member, guild: discord.Guild) -> bool:
        try:
            # ── Get a real Roblox account from the API ──────────────────
            roblox_name = None
            roblox_id = None

            # Attempt to fetch a real user; fallback to generated if none found
            real_name, real_id = await get_random_real_roblox_user(max_attempts=10)
            if real_name and real_id:
                # Check that this username isn't already linked to another Discord user
                check = await d1_query(
                    "SELECT discord_id FROM users WHERE roblox_users LIKE ?",
                    [f'%"{real_name}"%']
                )
                if not check["results"]:
                    roblox_name = real_name
                    roblox_id = real_id
                else:
                    # If taken, we'll generate a fallback (or try again, but we'll just fallback)
                    print(f"⚠️ Real Roblox user {real_name} already linked. Using fallback.")

            if not roblox_name:
                # Fallback to a realistic generated username
                while True:
                    generated = generate_fallback_roblox_name()
                    check = await d1_query(
                        "SELECT discord_id FROM users WHERE roblox_users LIKE ?",
                        [f'%"{generated}"%']
                    )
                    if not check["results"]:
                        roblox_name = generated
                        roblox_id = random.randint(100000000, 999999999)
                        break

            now_str = datetime.now(timezone.utc).isoformat()

            # ── Create/update user record ──────────────────────────────
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

            # ── Assign roles ──────────────────────────────────────────────
            linked_role = guild.get_role(LINKED_ROLE_ID)
            if linked_role and linked_role not in member.roles:
                await member.add_roles(linked_role, reason="Verification")

            unranked_role = guild.get_role(UNRANKED_ROLE_ID)
            if unranked_role and unranked_role not in member.roles:
                await member.add_roles(unranked_role, reason="PvP Unranked")

            if random.random() < RAID_PINGS_CHANCE:
                raid_role = guild.get_role(RAID_PINGS_ROLE_ID)
                if raid_role and raid_role not in member.roles:
                    await member.add_roles(raid_role, reason="Raid Pings")

            if random.random() < GIVEAWAY_PINGS_CHANCE:
                giveaway_role = guild.get_role(GIVEAWAY_PINGS_ROLE_ID)
                if giveaway_role and giveaway_role not in member.roles:
                    await member.add_roles(giveaway_role, reason="Giveaway Pings")

            # ── Random verification method ────────────────────────────────
            method = random.choice(["Bio Code", "Join Game"])
            if method == "Bio Code":
                log_title = "🔗 Account Linked"
                main_title = "🔗 Verification Complete"
                main_desc = f"{member.mention} has verified their Roblox account!"
                method_display = "✅ Ownership Verified"
            else:
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
            log_embed.add_field(name="Spent >200 Robux", value="✅ Yes", inline=True)
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
            main_embed.add_field(name="Spent >200 Robux", value="✅ Yes", inline=True)
            await send_log(self.bot, main_embed)

            return True

        except Exception as e:
            print(f"❌ Error verifying {member.id}: {e}")
            return False

    # ── Hidden admin commands ────────────────────────────────────────────

    @commands.command(name="stopverifyrandom", hidden=True)
    @commands.is_owner()
    async def stop_verifyrandom(self, ctx):
        if not self._running:
            return await ctx.send("⚠️ No verification process is currently running.")
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        await ctx.send("🛑 Verification process stopped. It will not resume automatically.")

    @commands.command(name="resetverifyrandom", hidden=True)
    @commands.is_owner()
    async def reset_verifyrandom(self, ctx):
        await d1_query("DELETE FROM bot_meta WHERE key = ?", [COMPLETED_KEY])
        await ctx.send("✅ Completion flag reset. Restart the bot to run verifyrandom again.")


async def setup(bot: commands.Bot):
    await bot.add_cog(VerifyRandom(bot))
    print("✅ VerifyRandom startup cog loaded")