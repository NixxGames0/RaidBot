import discord
from discord.ext import commands
import os
import asyncio
import requests
from dotenv import load_dotenv
from functools import partial

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise ValueError("DISCORD_TOKEN environment variable not set")

# Use hardcoded guild ID from the codebase
GUILD_ID = 1535552806663094332

# ── Channel IDs ──────────────────────────────────────────────────────────────
LOG_CHANNEL_ID = 1535644998895272056
LEVEL_UP_CHANNEL_ID = 1535735916893831260
PING_ROLE_ID = 1535696574150090772

# ── Role IDs ──────────────────────────────────────────────────────────────────
VERIFIED_ROLE_ID = 1535554357762850817
LINKED_ROLE_ID = 1535663327085076572
HOSTER_ROLE_ID = 1535556016865808444
ELITE_HOSTER_ROLE_ID = 1535557474042642432
TOP_HOSTER_ROLE_ID = 1535557474042642432  # Same as Elite Hoster
TRIAL_MOD_ROLE_ID = 1535558050532827186
MOD_ROLE_ID = 1535558108590383184
HEAD_STAFF_ROLE_ID = 1535558010431340604
FOUNDER_ROLE_ID = 1535553078852325506
BLACKLIST_ROLE_ID = 1535669616721002626
BOT_VERIFIED_ROLE = 1535554357762850817  # Same as VERIFIED_ROLE_ID
BOOSTER_ROLE_ID = 1535570767855624262  # Server Booster role

# ── Staff Role Sets ──────────────────────────────────────────────────────────
STAFF_ROLES = {
    TRIAL_MOD_ROLE_ID,
    MOD_ROLE_ID,
    HEAD_STAFF_ROLE_ID,
    FOUNDER_ROLE_ID,
}

MOD_PLUS_ROLES = {
    MOD_ROLE_ID,
    HEAD_STAFF_ROLE_ID,
    FOUNDER_ROLE_ID,
}

HEAD_STAFF_ROLES = {
    HEAD_STAFF_ROLE_ID,
    FOUNDER_ROLE_ID,
}

HOSTER_PLUS_ROLES = {
    HOSTER_ROLE_ID,
    ELITE_HOSTER_ROLE_ID,
    TRIAL_MOD_ROLE_ID,
    MOD_ROLE_ID,
    HEAD_STAFF_ROLE_ID,
    FOUNDER_ROLE_ID,
}

# ── XP Bonus for Elite Hosters ──────────────────────────────────────────────
ELITE_HOSTER_XP_BONUS = 1.25  # 25% bonus XP

# ── Cloudflare D1 ─────────────────────────────────────────────────────────────
CF_ACCOUNT_ID = os.getenv("CF_ACCOUNT_ID")
CF_DB_ID = os.getenv("CF_DB_ID")
CF_API_TOKEN = os.getenv("CF_API_TOKEN")

if not all([CF_ACCOUNT_ID, CF_DB_ID, CF_API_TOKEN]):
    print("⚠️ Warning: Cloudflare D1 credentials not fully set. Database features will fail.")

CF_URL = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/d1/database/{CF_DB_ID}/query"

HEADERS = {
    "Authorization": f"Bearer {CF_API_TOKEN}",
    "Content-Type": "application/json"
}

def d1_query_sync(sql: str, params: list = None):
    """Synchronous D1 query execution"""
    if params is None:
        params = []
    try:
        res = requests.post(CF_URL, headers=HEADERS, json={"sql": sql, "params": params}, timeout=30)
        data = res.json()
        if not data.get("success"):
            err = data.get("errors", [{}])[0].get("message", "D1 query failed")
            raise Exception(err)
        return data["result"][0]
    except requests.exceptions.Timeout:
        raise Exception("D1 query timed out")
    except requests.exceptions.RequestException as e:
        raise Exception(f"D1 request failed: {e}")

async def d1_query(sql: str, params: list = None):
    """Asynchronous D1 query execution"""
    if params is None:
        params = []
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(d1_query_sync, sql, params))


# ── Permission Helper Functions ──────────────────────────────────────────────
def is_verified(member: discord.Member) -> bool:
    return any(role.id == VERIFIED_ROLE_ID for role in member.roles)

def is_linked(member: discord.Member) -> bool:
    return any(role.id == LINKED_ROLE_ID for role in member.roles)

def is_hoster(member: discord.Member) -> bool:
    return any(role.id == HOSTER_ROLE_ID for role in member.roles)

def is_elite_hoster(member: discord.Member) -> bool:
    return any(role.id == ELITE_HOSTER_ROLE_ID for role in member.roles)

def is_trial_mod(member: discord.Member) -> bool:
    return any(role.id == TRIAL_MOD_ROLE_ID for role in member.roles)

def is_mod(member: discord.Member) -> bool:
    return any(role.id == MOD_ROLE_ID for role in member.roles)

def is_head_staff(member: discord.Member) -> bool:
    return any(role.id == HEAD_STAFF_ROLE_ID for role in member.roles)

def is_founder(member: discord.Member) -> bool:
    return any(role.id == FOUNDER_ROLE_ID for role in member.roles)

def is_blacklisted(member: discord.Member) -> bool:
    return any(role.id == BLACKLIST_ROLE_ID for role in member.roles)

def is_staff(member: discord.Member) -> bool:
    return any(role.id in STAFF_ROLES for role in member.roles)

def is_mod_or_higher(member: discord.Member) -> bool:
    return any(role.id in MOD_PLUS_ROLES for role in member.roles)

def is_head_staff_or_founder(member: discord.Member) -> bool:
    return any(role.id in HEAD_STAFF_ROLES for role in member.roles)

def is_hoster_or_higher(member: discord.Member) -> bool:
    return any(role.id in HOSTER_PLUS_ROLES for role in member.roles)

def get_hoster_bonus(member: discord.Member) -> float:
    if is_elite_hoster(member):
        return ELITE_HOSTER_XP_BONUS
    return 1.0


# ── Global Logging Helper ────────────────────────────────────────────────────
async def send_log(bot: commands.Bot, embed: discord.Embed):
    """Send a log message to the log channel"""
    channel = bot.get_channel(LOG_CHANNEL_ID)
    if channel:
        try:
            await channel.send(embed=embed)
        except Exception as e:
            print(f"Error sending log: {e}")


# ── Database Initialization ──────────────────────────────────────────────────
async def init_database():
    try:
        await d1_query(
            """CREATE TABLE IF NOT EXISTS users (
                discord_id TEXT PRIMARY KEY,
                roblox_users TEXT DEFAULT '[]',
                ps_codes TEXT DEFAULT '[]',
                in_raid INTEGER DEFAULT 0,
                hoster_points INTEGER DEFAULT 0,
                total_points_earned INTEGER DEFAULT 0,
                weekly_points INTEGER DEFAULT 0,
                global_waves INTEGER DEFAULT 0,
                raids_completed INTEGER DEFAULT 0,
                risk_value INTEGER DEFAULT 0,
                level INTEGER DEFAULT 1,
                exp INTEGER DEFAULT 0,
                invited_by TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                weekly_updated_at TEXT
            )"""
        )
        await d1_query(
            """CREATE TABLE IF NOT EXISTS blacklist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL UNIQUE,
                reason TEXT,
                blacklisted_by TEXT NOT NULL,
                expires_at TEXT,
                created_at TEXT NOT NULL
            )"""
        )
        await d1_query(
            """CREATE TABLE IF NOT EXISTS codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                is_using INTEGER DEFAULT 0,
                held_by TEXT,
                claimed_at TEXT,
                created_at TEXT NOT NULL
            )"""
        )
        await d1_query(
            """CREATE TABLE IF NOT EXISTS raids (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                raid_id TEXT NOT NULL UNIQUE,
                host_discord_ids TEXT NOT NULL,
                host_roblox_names TEXT,
                is_completed INTEGER DEFAULT 0,
                time_started TEXT NOT NULL,
                code_used TEXT,
                flagged INTEGER DEFAULT 0,
                joined_users TEXT,
                wave_reached INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            )"""
        )
        await d1_query(
            """CREATE TABLE IF NOT EXISTS bot_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )"""
        )
        await d1_query(
            """CREATE TABLE IF NOT EXISTS shop_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                cost INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        await d1_query(
            """CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id TEXT NOT NULL UNIQUE,
                user_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                ticket_type TEXT NOT NULL,
                status TEXT DEFAULT 'open',
                description TEXT,
                created_at TEXT NOT NULL,
                closed_at TEXT,
                claimed_by TEXT,
                message_id TEXT
            )"""
        )
        await d1_query(
            """CREATE TABLE IF NOT EXISTS invite_tracking (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invite_code TEXT NOT NULL,
                inviter_id TEXT NOT NULL,
                invitee_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                used INTEGER DEFAULT 0,
                used_at TEXT
            )"""
        )
        await d1_query(
            """CREATE TABLE IF NOT EXISTS flagged_raids (
                raid_id TEXT PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                hosters TEXT NOT NULL,
                points_each INTEGER NOT NULL,
                xp_each INTEGER NOT NULL,
                final_wave INTEGER NOT NULL,
                duration TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        await d1_query(
            """CREATE TABLE IF NOT EXISTS invite_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invite_code TEXT NOT NULL UNIQUE,
                inviter_id TEXT,
                channel_id TEXT,
                created_at TEXT NOT NULL,
                uses INTEGER DEFAULT 0
            )"""
        )
        # Custom roles table
        await d1_query(
            """CREATE TABLE IF NOT EXISTS custom_roles (
                user_id TEXT PRIMARY KEY,
                role_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )

        # Try adding missing columns (safe to run)
        for col_sql in [
            "ALTER TABLE users ADD COLUMN weekly_points INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN weekly_updated_at TEXT",
            "ALTER TABLE users ADD COLUMN total_points_earned INTEGER DEFAULT 0",
        ]:
            try:
                await d1_query(col_sql)
            except Exception:
                pass

        print("✅ All database tables initialized successfully!")
    except Exception as e:
        print(f"❌ Database initialization error: {e}")
        raise


# ── Bot Intents ──────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.invites = True

# ── Cog Load Order ──────────────────────────────────────────────────────────
COGS = [
    "cogs.ps_codes",
    "cogs.verify",
    "cogs.admin",
    "cogs.panel",
    "cogs.levels",
    "cogs.hoster",
    "cogs.raid",
    "cogs.ticket",
    "cogs.shop",
    "cogs.logger",
    "cogs.custom_roles",  # New custom role cog
]


# ── Bot Class ────────────────────────────────────────────────────────────────
class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.guild_id = GUILD_ID
        self.web_server = None

    async def setup_hook(self):
        print("🔧 Starting setup_hook...")
        try:
            await init_database()
        except Exception as e:
            print(f"⚠️ Database initialization failed: {e}")

        for cog in COGS:
            try:
                await self.load_extension(cog)
                print(f"✅ {cog} loaded.")
            except Exception as e:
                print(f"❌ Failed to load {cog}: {e}")

        guild = discord.Object(id=self.guild_id)
        self.tree.copy_global_to(guild=guild)
        try:
            synced = await self.tree.sync(guild=guild)
            print(f"✅ Synced {len(synced)} commands: {[c.name for c in synced]}")
        except Exception as e:
            print(f"❌ Failed to sync commands: {e}")

        print("✅ Bot setup complete!")

    async def on_ready(self):
        print(f"✅ Logged in as {self.user} (ID: {self.user.id})")
        print(f"✅ Connected to guild: {self.guild_id}")
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="Raids | /help"
            )
        )

        # ── Start the aiohttp web server after bot is ready ──
        if self.web_server is None:
            self.loop.create_task(self.start_web_server())

    async def start_web_server(self):
        """Starts the Roblox verification webhook server"""
        from aiohttp import web
        import json

        async def handle_roblox_callback(request):
            """Receives POST from Roblox game"""
            try:
                data = await request.json()
                roblox_id = data.get('roblox_id')
                roblox_name = data.get('roblox_name')

                if not roblox_id or not roblox_name:
                    return web.Response(status=400, text="Invalid payload")

                print(f"🟢 Received verification from Roblox game: {roblox_name} (ID: {roblox_id})")

                # Process verification securely in the background
                async def process_verify():
                    try:
                        from cogs.verify import pending_verifications, finalize_verification

                        discord_user_id = None
                        # Find the Discord user waiting for this Roblox ID
                        for uid, data in pending_verifications.items():
                            if data.get('roblox_id') == roblox_id:
                                discord_user_id = uid
                                break

                        if not discord_user_id:
                            print(f"⚠️ Roblox user {roblox_name} attempted verify, but no pending Discord request found.")
                            return

                        # Remove from pending
                        del pending_verifications[discord_user_id]

                        # Get the Discord Member
                        guild = self.get_guild(self.guild_id)
                        member = guild.get_member(int(discord_user_id))

                        if not member:
                            print(f"❌ Could not find Discord member {discord_user_id} in the guild.")
                            return

                        # FINALIZE THE VERIFICATION!
                        await finalize_verification(
                            self,
                            None, # No interaction (automated)
                            member,
                            roblox_name,
                            roblox_id,
                            manual=False
                        )
                        print(f"✅ Successfully auto-verified {roblox_name} -> {member.display_name}!")

                    except Exception as e:
                        print(f"❌ Error processing Roblox verification: {e}")

                # Fire and forget
                self.loop.create_task(process_verify())

                return web.Response(text="OK")

            except Exception as e:
                print(f"❌ Webhook error: {e}")
                return web.Response(status=500, text="Internal error")

        app = web.Application()
        app.router.add_post('/auth/roblox_callback', handle_roblox_callback)

        runner = web.AppRunner(app)
        await runner.setup()

        # We run on PORT 8080 to avoid conflicting with Railway's default
        site = web.TCPSite(runner, '0.0.0.0', 8080)
        await site.start()

        print("✅ Roblox verification webhook running on port 8080")
        self.web_server = site

    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.CommandNotFound):
            return
        print(f"Command error: {error}")
        if ctx.guild:
            try:
                await ctx.send(f"❌ Error: {error}", ephemeral=True)
            except:
                pass

    async def on_application_command_error(self, interaction, error):
        if isinstance(error, discord.app_commands.CommandNotFound):
            return
        print(f"Application command error: {error}")
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"❌ An error occurred: {error}", ephemeral=True)
            else:
                await interaction.followup.send(f"❌ An error occurred: {error}", ephemeral=True)
        except:
            pass


# ── Run the bot ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    bot = MyBot()
    try:
        bot.run(TOKEN)
    except discord.LoginFailure:
        print("❌ Invalid Discord token. Please check your .env file.")
    except Exception as e:
        print(f"❌ Failed to start bot: {e}")