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
    """Check if user has the Verified role"""
    return any(role.id == VERIFIED_ROLE_ID for role in member.roles)


def is_linked(member: discord.Member) -> bool:
    """Check if user has the Linked role"""
    return any(role.id == LINKED_ROLE_ID for role in member.roles)


def is_hoster(member: discord.Member) -> bool:
    """Check if user has the Hoster role"""
    return any(role.id == HOSTER_ROLE_ID for role in member.roles)


def is_elite_hoster(member: discord.Member) -> bool:
    """Check if user has the Elite Hoster role"""
    return any(role.id == ELITE_HOSTER_ROLE_ID for role in member.roles)


def is_trial_mod(member: discord.Member) -> bool:
    """Check if user has the Trial Mod role"""
    return any(role.id == TRIAL_MOD_ROLE_ID for role in member.roles)


def is_mod(member: discord.Member) -> bool:
    """Check if user has the Mod role"""
    return any(role.id == MOD_ROLE_ID for role in member.roles)


def is_head_staff(member: discord.Member) -> bool:
    """Check if user has the Head Staff role"""
    return any(role.id == HEAD_STAFF_ROLE_ID for role in member.roles)


def is_founder(member: discord.Member) -> bool:
    """Check if user has the Founder role"""
    return any(role.id == FOUNDER_ROLE_ID for role in member.roles)


def is_blacklisted(member: discord.Member) -> bool:
    """Check if user has the Blacklisted role"""
    return any(role.id == BLACKLIST_ROLE_ID for role in member.roles)


def is_staff(member: discord.Member) -> bool:
    """Check if user has any staff role (Trial Mod+)"""
    return any(role.id in STAFF_ROLES for role in member.roles)


def is_mod_or_higher(member: discord.Member) -> bool:
    """Check if user is Mod or higher (excludes Trial Mod)"""
    return any(role.id in MOD_PLUS_ROLES for role in member.roles)


def is_head_staff_or_founder(member: discord.Member) -> bool:
    """Check if user is Head Staff or Founder"""
    return any(role.id in HEAD_STAFF_ROLES for role in member.roles)


def is_hoster_or_higher(member: discord.Member) -> bool:
    """Check if user has Hoster role or any staff role"""
    return any(role.id in HOSTER_PLUS_ROLES for role in member.roles)


def get_hoster_bonus(member: discord.Member) -> float:
    """Get the XP bonus multiplier for a member based on their roles"""
    if is_elite_hoster(member):
        return ELITE_HOSTER_XP_BONUS
    return 1.0


# ── Database Initialization ──────────────────────────────────────────────────
async def init_database():
    """Initialize all database tables on startup"""
    try:
        # ── Users table ──────────────────────────────────────────────────────
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
        print("✅ users table created/verified")

        # ── Blacklist table ─────────────────────────────────────────────────
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
        print("✅ blacklist table created/verified")

        # ── PS Codes table ──────────────────────────────────────────────────
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
        print("✅ codes table created/verified")

        # ── Raids table ─────────────────────────────────────────────────────
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
        print("✅ raids table created/verified")

        # ── Bot Meta table ──────────────────────────────────────────────────
        await d1_query(
            """CREATE TABLE IF NOT EXISTS bot_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )"""
        )
        print("✅ bot_meta table created/verified")

        # ── Shop Items table ────────────────────────────────────────────────
        await d1_query(
            """CREATE TABLE IF NOT EXISTS shop_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                cost INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        print("✅ shop_items table created/verified")

        # ── Tickets table ───────────────────────────────────────────────────
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
        print("✅ tickets table created/verified")

        # ── Invite Tracking tables ─────────────────────────────────────────
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
        print("✅ invite_tracking table created/verified")

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
        print("✅ invite_codes table created/verified")

        # ── Try adding missing columns (safe to run) ───────────────────────
        for col_sql in [
            "ALTER TABLE users ADD COLUMN weekly_points INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN weekly_updated_at TEXT",
            "ALTER TABLE users ADD COLUMN total_points_earned INTEGER DEFAULT 0",
        ]:
            try:
                await d1_query(col_sql)
            except Exception:
                pass  # Column already exists

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
# hoster MUST load before raid (raid imports from hoster)
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
]


# ── Bot Class ────────────────────────────────────────────────────────────────
class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.guild_id = GUILD_ID

    async def setup_hook(self):
        """Called when the bot is starting up"""
        print("🔧 Starting setup_hook...")

        # Initialize database tables first
        try:
            await init_database()
        except Exception as e:
            print(f"⚠️ Database initialization failed: {e}")
            # Continue loading - some features may fail but bot can still run

        # Load all cogs
        for cog in COGS:
            try:
                await self.load_extension(cog)
                print(f"✅ {cog} loaded.")
            except Exception as e:
                print(f"❌ Failed to load {cog}: {e}")

        # Sync commands to guild
        guild = discord.Object(id=self.guild_id)
        self.tree.copy_global_to(guild=guild)

        try:
            synced = await self.tree.sync(guild=guild)
            print(f"✅ Synced {len(synced)} commands: {[c.name for c in synced]}")
        except Exception as e:
            print(f"❌ Failed to sync commands: {e}")

        print("✅ Bot setup complete!")

    async def on_ready(self):
        """Called when the bot is fully ready"""
        print(f"✅ Logged in as {self.user} (ID: {self.user.id})")
        print(f"✅ Connected to guild: {self.guild_id}")
        print(f"✅ Bot is ready!")

        # Set bot status
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="Raids | /help"
            )
        )

    async def on_command_error(self, ctx, error):
        """Global error handler for commands"""
        if isinstance(error, commands.CommandNotFound):
            return
        print(f"Command error: {error}")
        if ctx.guild:
            try:
                await ctx.send(f"❌ Error: {error}", ephemeral=True)
            except:
                pass

    async def on_application_command_error(self, interaction, error):
        """Global error handler for slash commands"""
        if isinstance(error, discord.app_commands.CommandNotFound):
            return
        print(f"Application command error: {error}")
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    f"❌ An error occurred: {error}",
                    ephemeral=True
                )
            else:
                await interaction.followup.send(
                    f"❌ An error occurred: {error}",
                    ephemeral=True
                )
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