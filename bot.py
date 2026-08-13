import discord
from discord.ext import commands
import os
import asyncio
import json
import requests
import aiohttp
from dotenv import load_dotenv
from functools import partial

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise ValueError("DISCORD_TOKEN environment variable not set")

# Use hardcoded guild ID from the codebase
GUILD_ID = 1535552806663094332

# ── Channel IDs ──────────────────────────────────────────────────────────────
LOG_CHANNELS: dict[str, int] = {}   # populated by cogs/log_setup.py on_ready
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
TRIAL_HOSTER_ROLE_ID = 1537019401847439410

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
    TRIAL_HOSTER_ROLE_ID,
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
CF_ACCOUNT_ID  = os.getenv("CF_ACCOUNT_ID")
CF_DB_ID       = os.getenv("CF_DB_ID")
CF_API_TOKEN   = os.getenv("CF_API_TOKEN")
ROBLOX_SECRET  = os.getenv("ROBLOX_SECRET", "changeme")

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
    """Send a log message to the general log channel."""
    channel = bot.get_channel(LOG_CHANNELS.get("general", 0))
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

        # ─── Giveaways table ──────────────────────────────────────────────────
        await d1_query(
            """CREATE TABLE IF NOT EXISTS giveaways (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                giveaway_id TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                sponsor TEXT,
                prize TEXT NOT NULL,
                host_id TEXT NOT NULL,
                required_role_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                end_time TEXT NOT NULL,
                ended INTEGER DEFAULT 0,
                winner_ids TEXT,
                channel_id TEXT NOT NULL,
                message_id TEXT,
                bonus_entries TEXT,
                entrants TEXT DEFAULT '[]',
                status TEXT DEFAULT 'active',
                last_reroll TEXT
            )"""
        )

        # ─── PvP tables ───────────────────────────────────────────────────────────
        await d1_query(
            """CREATE TABLE IF NOT EXISTS pvp_matches (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id        TEXT    NOT NULL UNIQUE,
                player1_id      TEXT    NOT NULL,
                player2_id      TEXT    NOT NULL,
                match_type      TEXT    NOT NULL,
                winner_id       TEXT,
                score           TEXT,
                p1_elo_before   INTEGER,
                p2_elo_before   INTEGER,
                p1_elo_after    INTEGER,
                p2_elo_after    INTEGER,
                channel_id      TEXT,
                started_at      TEXT    NOT NULL,
                ended_at        TEXT,
                timeout_flag    INTEGER DEFAULT 0,
                created_at      TEXT    NOT NULL
            )"""
        )
        await d1_query(
            """CREATE TABLE IF NOT EXISTS pvp_reports (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                reporter_id TEXT NOT NULL,
                reported_id TEXT NOT NULL,
                match_id    TEXT,
                reason      TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                reviewed    INTEGER DEFAULT 0,
                action_taken TEXT
            )"""
        )

        await d1_query(
            """CREATE TABLE IF NOT EXISTS pvp_ps_codes (
                code        TEXT PRIMARY KEY,
                match_id    TEXT
            )"""
        )

        # ─── Moderation table ─────────────────────────────────────────────────────
        await d1_query(
            """CREATE TABLE IF NOT EXISTS mod_actions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id    TEXT NOT NULL,
                target_id   TEXT NOT NULL,
                mod_id      TEXT NOT NULL,
                action      TEXT NOT NULL,
                reason      TEXT,
                duration    TEXT,
                expires_at  TEXT,
                created_at  TEXT NOT NULL
            )"""
        )

        # Try adding missing columns (safe to run)
        for col_sql in [
            "ALTER TABLE users ADD COLUMN weekly_points INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN weekly_updated_at TEXT",
            "ALTER TABLE users ADD COLUMN total_points_earned INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN pvp_elo INTEGER DEFAULT 1000",
            "ALTER TABLE users ADD COLUMN pvp_wins INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN pvp_losses INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN pvp_placement_done INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN pvp_placement_left INTEGER DEFAULT 10",
            "ALTER TABLE users ADD COLUMN pvp_rank TEXT DEFAULT 'Unranked'",
            "ALTER TABLE users ADD COLUMN pvp_trust REAL DEFAULT 10.0",
            "ALTER TABLE users ADD COLUMN pvp_deny_count INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN pvp_timeout_until TEXT",
            "ALTER TABLE users ADD COLUMN pvp_banned INTEGER DEFAULT 0",
            "ALTER TABLE pvp_matches ADD COLUMN score TEXT",
            "ALTER TABLE pvp_matches ADD COLUMN timeout_flag INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN roblox_ids TEXT DEFAULT '[]'",
            "ALTER TABLE raids ADD COLUMN host_roblox_ids TEXT DEFAULT '[]'",
        ]:
            try:
                await d1_query(col_sql)
            except Exception:
                pass

        # Indexes — safe to run repeatedly (IF NOT EXISTS)
        for idx_sql in [
            "CREATE INDEX IF NOT EXISTS idx_users_in_raid       ON users(in_raid)",
            "CREATE INDEX IF NOT EXISTS idx_raids_active        ON raids(is_completed)",
            "CREATE INDEX IF NOT EXISTS idx_pvp_matches_p1      ON pvp_matches(player1_id)",
            "CREATE INDEX IF NOT EXISTS idx_pvp_matches_p2      ON pvp_matches(player2_id)",
            "CREATE INDEX IF NOT EXISTS idx_pvp_matches_ended   ON pvp_matches(ended_at)",
            "CREATE INDEX IF NOT EXISTS idx_mod_actions_target  ON mod_actions(target_id)",
            "CREATE INDEX IF NOT EXISTS idx_blacklist_expires   ON blacklist(expires_at)",
        ]:
            try:
                await d1_query(idx_sql)
            except Exception:
                pass

        print("✅ All database tables initialized successfully!")

        # ── Incremental roblox_ids migration ──────────────────────────────────
        # Runs only for users who have names stored but no IDs yet.
        # On subsequent startups this query returns 0 rows → instant no-op.
        await _migrate_roblox_ids()

    except Exception as e:
        print(f"❌ Database initialization error: {e}")
        raise


async def _migrate_roblox_ids():
    """Populate roblox_ids for any user whose column is still empty."""
    rows = (await d1_query(
        "SELECT discord_id, roblox_users FROM users"
        " WHERE roblox_users IS NOT NULL AND roblox_users != '[]'"
        " AND (roblox_ids IS NULL OR roblox_ids = '[]')"
    )).get("results", [])

    if not rows:
        return  # nothing to migrate

    print(f"[Migration] Populating roblox_ids for {len(rows)} user(s)...")

    # Collect all unique names across unmigrated users
    user_names: dict[str, list[str]] = {}
    all_names: list[str] = []
    for row in rows:
        names = json.loads(row["roblox_users"] or "[]")
        user_names[row["discord_id"]] = names
        all_names.extend(names)

    unique_names = list({n.lower(): n for n in all_names}.values())

    # Resolve names → IDs via Roblox public API (100 per batch)
    name_to_id: dict[str, tuple[int, str]] = {}
    async with aiohttp.ClientSession() as session:
        for i in range(0, len(unique_names), 100):
            batch = unique_names[i:i + 100]
            try:
                async with session.post(
                    "https://users.roblox.com/v1/usernames/users",
                    json={"usernames": batch, "excludeBannedUsers": False},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for entry in data.get("data", []):
                            key = entry["requestedUsername"].lower()
                            name_to_id[key] = (entry["id"], entry["name"])
                    else:
                        print(f"[Migration] Roblox API returned {resp.status} for batch {i}–{i+len(batch)}")
            except Exception as e:
                print(f"[Migration] Roblox API error for batch {i}: {e}")
            if i + 100 < len(unique_names):
                await asyncio.sleep(1.5)  # avoid rate limiting

    # Update each user
    updated = skipped = 0
    for discord_id, names in user_names.items():
        ids: list[int] = []
        corrected: list[str] = []
        for name in names:
            result = name_to_id.get(name.lower())
            if result:
                roblox_id, current_name = result
                ids.append(roblox_id)
                corrected.append(current_name)
            else:
                corrected.append(name)  # keep old name; account may be deleted/banned

        if not ids:
            skipped += 1
            continue

        await d1_query(
            "UPDATE users SET roblox_ids = ?, roblox_users = ? WHERE discord_id = ?",
            [json.dumps(ids), json.dumps(corrected), discord_id],
        )
        updated += 1

    print(f"[Migration] Done — {updated} updated, {skipped} skipped (no resolvable IDs)")


# ── Bot Intents ──────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.invites = True

# ── Cog Load Order ──────────────────────────────────────────────────────────
COGS = [
    "cogs.log_setup",
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
    "cogs.custom_roles",
    "cogs.giveaway",
    "cogs.afk",
    "cogs.profile",
    "cogs.pvp",
    "cogs.moderation",
    "cogs.trial_hoster",
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
                        from cogs.verify import complete_game_verification
                        await complete_game_verification(self, int(roblox_id), roblox_name)
                        print(f"✅ Game verification processed for {roblox_name}!")
                    except Exception as e:
                        print(f"❌ Error processing Roblox verification: {e}")

                # Fire and forget
                self.loop.create_task(process_verify())

                return web.Response(text="OK")

            except Exception as e:
                print(f"❌ Webhook error: {e}")
                return web.Response(status=500, text="Internal error")

        async def handle_flag_players(request):
            """Receives flagged-player reports from the Lua client checker."""
            try:
                data = await request.json()
            except Exception:
                return web.Response(status=400, text="Invalid JSON")

            if data.get("secret") != ROBLOX_SECRET:
                return web.Response(status=403, text="Forbidden")

            flagged     = data.get("flagged", [])
            blacklisted = data.get("blacklisted", [])
            total       = data.get("total_players", 0)
            raid_active = data.get("raid_active", False)

            if not flagged and not blacklisted:
                return web.Response(text="OK — nothing to flag")

            log_ch = self.get_channel(LOG_CHANNELS.get("general", 0))
            if not log_ch:
                return web.Response(status=500, text="Log channel not found")

            raid_status = "Active raid in progress" if raid_active else "No active raid"

            # ── Blacklisted players embed (highest priority alert) ──
            if blacklisted:
                extra_bl = f" (+{len(blacklisted) - 10} more)" if len(blacklisted) > 10 else ""
                bl_embed = discord.Embed(
                    title="🚫 BLACKLISTED Player Detected",
                    description=(
                        f"**{len(blacklisted)}** blacklisted player(s) joined the server "
                        f"({total} total){extra_bl}.\n*{raid_status}*"
                    ),
                    color=discord.Color.dark_red(),
                    timestamp=discord.utils.utcnow(),
                )
                for p in blacklisted[:10]:
                    expires = p.get("expires_at")
                    expires_text = f"\nExpires: `{expires[:10]}`" if expires else "\nPermanent ban"
                    bl_embed.add_field(
                        name=f"⛔ {p.get('displayName', '?')} (@{p.get('name', '?')})",
                        value=(
                            f"Roblox ID: `{p.get('userId', '?')}`\n"
                            f"Reason: {p.get('reason', 'Unknown')}"
                            f"{expires_text}\n"
                            f"[Profile](https://www.roblox.com/users/{p.get('userId', 0)}/profile)"
                        ),
                        inline=True,
                    )
                bl_embed.set_footer(text="Raid Server Checker — Blacklist Alert")
                await log_ch.send(embed=bl_embed)
                print(f"[FlagPlayers] Blacklisted alert: {len(blacklisted)} player(s)")

            # ── Unregistered players embed ──
            if flagged:
                extra_fl = f" (+{len(flagged) - 25} more)" if len(flagged) > 25 else ""
                fl_embed = discord.Embed(
                    title="🚨 Unregistered Players Detected",
                    description=(
                        f"**{len(flagged)}** player(s) found whose Roblox account is not linked to "
                        f"any Discord account in this bot ({total} total){extra_fl}.\n"
                        f"*{raid_status}*\n\n"
                        f"They may not have verified, or are playing on an unregistered account. "
                        f"Ask them to use **/verify** in Discord."
                    ),
                    color=discord.Color.red(),
                    timestamp=discord.utils.utcnow(),
                )
                for p in flagged[:25]:
                    fl_embed.add_field(
                        name=f"{p.get('displayName', '?')} (@{p.get('name', '?')})",
                        value=(
                            f"Roblox ID: `{p.get('userId', '?')}`\n"
                            f"Reason: {p.get('reason', 'Unknown')}\n"
                            f"[Profile](https://www.roblox.com/users/{p.get('userId', 0)}/profile)"
                        ),
                        inline=True,
                    )
                fl_embed.set_footer(text="Raid Server Checker")
                await log_ch.send(embed=fl_embed)
                print(f"[FlagPlayers] Flagged {len(flagged)} unregistered player(s)")

            return web.Response(text="OK")

        app = web.Application()
        app.router.add_post('/auth/roblox_callback', handle_roblox_callback)
        app.router.add_post('/raid/flag-players', handle_flag_players)

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