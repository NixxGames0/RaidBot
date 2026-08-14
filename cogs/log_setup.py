"""Auto-creates all bot log channels on startup and persists their IDs in D1."""
import discord
from discord import app_commands
from discord.ext import commands

import bot as bot_module
from bot import d1_query, GUILD_ID, HEAD_STAFF_ROLE_ID, FOUNDER_ROLE_ID

LOG_CATEGORY_NAME = "◆ ＬＯＧＳ ◆"

# (key, channel-name, topic)
_CHANNELS = [
    ("raids",         "raid-logs",         "Raid events, hoster points, and flagged raids"),
    ("trial_hoster",  "trial-hoster-logs", "Trial hoster progress, promotions, and expirations"),
    ("tickets",       "ticket-logs",       "Support ticket activity and staff applications"),
    ("moderation",    "mod-logs",          "Moderation actions: warns, mutes, bans"),
    ("verification",  "verify-logs",       "Roblox verification events"),
    ("manual_verify", "manual-verify",     "Manual verification requests for staff review"),
    ("members",       "member-logs",       "Member joins and leaves"),
    ("messages",      "message-logs",      "Edited and deleted messages"),
    ("giveaways",     "giveaway-logs",     "Giveaway creation and winners"),
    ("shop",          "shop-logs",         "Shop purchases and transactions"),
    ("pvp",           "pvp-logs",          "PvP match results"),
    ("general",       "bot-logs",          "General bot activity"),
]


async def get_or_create_category(guild: discord.Guild, name: str) -> discord.CategoryChannel:
    """Find a Discord category by name, or create it if missing."""
    cat = discord.utils.get(guild.categories, name=name)
    if cat is None:
        cat = await guild.create_category(name)
        print(f"[LogSetup] Created category '{name}' ({cat.id})")
    return cat


async def ensure_log_channels(bot: commands.Bot) -> None:
    """Verify all log channels exist; create missing ones; update LOG_CHANNELS dict."""
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        print("[LogSetup] Guild not found.")
        return

    try:
        category = await get_or_create_category(guild, LOG_CATEGORY_NAME)
    except Exception as e:
        print(f"[LogSetup] Failed to get/create log category: {e}")
        return

    head_staff = guild.get_role(HEAD_STAFF_ROLE_ID)
    founder    = guild.get_role(FOUNDER_ROLE_ID)

    created = 0
    ok = 0
    failed = 0

    for key, name, topic in _CHANNELS:
        db_key = f"log_channel_{key}"

        try:
            # Load saved ID from D1
            rows = await d1_query("SELECT value FROM bot_meta WHERE key = ?", [db_key])
            results = rows.get("results", []) if rows else []
            saved_id = int(results[0]["value"]) if results else None

            # Verify the saved channel still exists in this guild
            channel = guild.get_channel(saved_id) if saved_id else None

            if channel is None:
                overwrites: dict = {
                    guild.default_role: discord.PermissionOverwrite(view_channel=False),
                }
                for role in (head_staff, founder):
                    if role:
                        overwrites[role] = discord.PermissionOverwrite(
                            view_channel=True, send_messages=False
                        )

                channel = await guild.create_text_channel(
                    name=name,
                    category=category,
                    topic=topic,
                    overwrites=overwrites,
                )
                await d1_query(
                    "INSERT OR REPLACE INTO bot_meta (key, value) VALUES (?, ?)",
                    [db_key, str(channel.id)],
                )
                print(f"[LogSetup] Created #{name} ({channel.id})")
                created += 1
            else:
                print(f"[LogSetup] #{name} OK ({channel.id})")
                ok += 1

            bot_module.LOG_CHANNELS[key] = channel.id

        except Exception as e:
            print(f"[LogSetup] Failed to create #{name}: {e}")
            failed += 1

    print(f"[LogSetup] Done — {created} created, {ok} existing, {failed} failed.")


class LogSetup(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        await ensure_log_channels(self.bot)

    @app_commands.command(name="setuplogs", description="Recreate any missing log channels.")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def setuplogs(self, interaction: discord.Interaction):
        if not any(r.id in (HEAD_STAFF_ROLE_ID, FOUNDER_ROLE_ID) for r in interaction.user.roles):
            await interaction.response.send_message("No permission.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await ensure_log_channels(self.bot)
        await interaction.followup.send("✅ Log channels verified/created.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(LogSetup(bot))
