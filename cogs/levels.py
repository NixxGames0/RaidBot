import discord
from discord import app_commands
from discord.ext import commands
import random
import asyncio
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# Import shared functions from bot.py
from bot import (
    d1_query, 
    GUILD_ID,
    VERIFIED_ROLE_ID,
    LINKED_ROLE_ID,
    LOG_CHANNELS,
    LEVEL_UP_CHANNEL_ID,
    ELITE_HOSTER_ROLE_ID,
    is_verified,
    is_linked,
    is_hoster,
    is_elite_hoster,
    is_staff,
    get_hoster_bonus
)

# ── Role IDs ──────────────────────────────────────────────────────────────────
BOOSTER_ROLE_ID = 1535570767855624262  # Server Booster role

# ── XP Configuration ─────────────────────────────────────────────────────────
XP_MIN = 15
XP_MAX = 25
COOLDOWN_SECONDS = 60
VOICE_XP_INTERVAL = 60  # 1 minute
VOICE_XP_AMOUNT = 5
VOICE_INACTIVITY_TIMEOUT = 300  # 5 minutes
REACTION_XP_AMOUNT = 5
INVITE_XP_REWARD = 250
FIRST_MESSAGE_XP = 100
RAID_PARTICIPATION_XP = 50

# ── Bonus Multipliers (Additive) ────────────────────────────────────────────
LINKED_BONUS_PCT = 10   # 10% bonus for having Linked role
BOOSTER_BONUS_PCT = 25  # 25% bonus for server boosters
ELITE_HOSTER_BONUS_PCT = 25  # 25% bonus for elite hosters

# ── XP Eligibility Requirements ─────────────────────────────────────────────
# Users must have the Verified role to earn XP
# Users must not be bots
# Users must be in a guild (not DMs)


# ── Trackers ──────────────────────────────────────────────────────────────────
message_cooldown = defaultdict(float)
voice_last_activity = defaultdict(float)
voice_last_xp_time = defaultdict(float)
reaction_cooldown = defaultdict(float)
last_message_date = defaultdict(str)  # Track last message date for daily first message


# ── Level Formula Functions ──────────────────────────────────────────────────
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
    channel = bot.get_channel(LOG_CHANNELS.get("general", 0))
    if channel:
        try:
            await channel.send(embed=embed)
        except Exception as e:
            print(f"Error sending log: {e}")


# ── Bonus Calculation (Additive) ──────────────────────────────────────────
def get_xp_multiplier(member: discord.Member) -> tuple:
    """
    Calculate total XP bonus for a member.
    Bonuses ADD together (not multiply):
    - Linked: +10%
    - Booster: +25%
    - Elite Hoster: +25%
    Total possible: 10% + 25% + 25% = 60% bonus
    """
    total_bonus_pct = 0
    bonuses = []
    
    if is_linked(member):
        total_bonus_pct += LINKED_BONUS_PCT
        bonuses.append("Linked (+10%)")
    if any(role.id == BOOSTER_ROLE_ID for role in member.roles):
        total_bonus_pct += BOOSTER_BONUS_PCT
        bonuses.append("Booster (+25%)")
    if is_elite_hoster(member):
        total_bonus_pct += ELITE_HOSTER_BONUS_PCT
        bonuses.append("Elite Hoster (+25%)")
    
    # Convert to multiplier (e.g., 60% bonus = 1.60x)
    multiplier = 1.0 + (total_bonus_pct / 100.0)
    
    return multiplier, bonuses, total_bonus_pct


def get_xp_requirements_text() -> str:
    """Get the XP eligibility requirements text for display"""
    return (
        "**📋 XP Eligibility Requirements:**\n"
        "• ✅ Must be **Verified** (`/verify`)\n"
        "• ✅ Must **not** be a bot\n"
        "• ✅ Must be in a server (not DMs)\n\n"
        "**✨ XP Bonuses (Additive!):**\n"
        "• 🔗 **Linked Account**: +10% bonus on ALL XP\n"
        "• 🎂 **Server Booster**: +25% bonus on ALL XP\n"
        "• 👑 **Elite Hoster**: +25% bonus on ALL XP\n"
        "• **All bonuses add together!** (Up to 60%!)"
    )


# ── XP Award Function ──────────────────────────────────────────────────────
async def award_xp(user_id: int, amount: int, member: discord.Member = None) -> dict:
    """
    Award XP to a user with bonuses applied (additive).
    Returns dict with level_up info or None if failed.
    """
    try:
        bonus_text = ""
        multiplier, bonus_pct = 1.0, 0
        if member:
            multiplier, _, bonus_pct = get_xp_multiplier(member)
            if multiplier > 1.0:
                amount = int(amount * multiplier)
                bonus_text = f" (+{bonus_pct}%)"
        
        now = datetime.now(timezone.utc).isoformat()

        result = await d1_query(
            "SELECT level, exp FROM users WHERE discord_id = ?",
            [str(user_id)]
        )

        if not result["results"]:
            return None

        current_level = result["results"][0]["level"] or 1
        current_exp = result["results"][0]["exp"] or 0

        new_exp = current_exp + amount
        new_level = level_from_xp(new_exp)

        await d1_query(
            "UPDATE users SET level = ?, exp = ?, updated_at = ? WHERE discord_id = ?",
            [new_level, new_exp, now, str(user_id)]
        )

        level_up = new_level > current_level

        return {
            "old_level": current_level,
            "new_level": new_level,
            "old_exp": current_exp,
            "new_exp": new_exp,
            "xp_gained": amount,
            "leveled_up": level_up,
            "bonus_applied": multiplier > 1.0,
            "bonus_text": bonus_text,
            "bonus_pct": bonus_pct,
        }

    except Exception as e:
        print(f"Error awarding XP to {user_id}: {e}")
        return None


async def send_level_up_message(bot: commands.Bot, member: discord.Member, old_level: int, new_level: int, new_exp: int):
    """Send a level-up message to the designated channel"""
    try:
        channel = bot.get_channel(LEVEL_UP_CHANNEL_ID)
        if not channel:
            print(f"Level-up channel {LEVEL_UP_CHANNEL_ID} not found")
            return

        # Calculate progress to next level
        xp_for_current = total_xp_for_level(new_level)
        xp_for_next = total_xp_for_level(new_level + 1)
        needed = xp_for_next - xp_for_current
        progress = new_exp - xp_for_current
        bar = progress_bar(progress, needed)
        percent = int((progress / needed) * 100) if needed > 0 else 0
        percent = max(0, min(100, percent))

        embed = discord.Embed(
            title="🎉 Level Up!",
            description=f"{member.mention} has reached **Level {new_level}**!",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc)
        )

        if member.display_avatar:
            embed.set_thumbnail(url=member.display_avatar.url)

        embed.add_field(
            name="Level",
            value=f"{old_level} → **{new_level}**",
            inline=True
        )
        embed.add_field(
            name="Total XP",
            value=f"**{new_exp:,}**",
            inline=True
        )
        embed.add_field(
            name="Progress to Next Level",
            value=f"`{bar}` {percent}%",
            inline=False
        )
        embed.add_field(
            name=f"XP to Level {new_level + 1}",
            value=f"**{max(0, needed - progress):,}** XP remaining",
            inline=True
        )

        embed.set_footer(text=f"ID: {member.id}")
        await channel.send(embed=embed)

        # Log the level up
        log_embed = discord.Embed(
            title="🎉 Level Up",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc)
        )
        log_embed.add_field(name="User", value=member.mention, inline=True)
        log_embed.add_field(name="New Level", value=f"**{new_level}**", inline=True)
        log_embed.add_field(name="New XP", value=f"**{new_exp:,}**", inline=True)
        await send_log(bot, log_embed)

    except Exception as e:
        print(f"Error sending level-up message: {e}")


# ── Public XP Award Functions (for other cogs) ─────────────────────────────
async def award_raid_participation_xp(user_id: int, member: discord.Member) -> dict:
    """Award XP for participating in a raid"""
    return await award_xp(user_id, RAID_PARTICIPATION_XP, member)


async def award_invite_xp(user_id: int, member: discord.Member) -> dict:
    """Award XP for inviting a user"""
    return await award_xp(user_id, INVITE_XP_REWARD, member)


async def award_first_message_xp(user_id: int, member: discord.Member) -> dict:
    """Award XP for first message of the day"""
    return await award_xp(user_id, FIRST_MESSAGE_XP, member)


# ── Levels Cog ──────────────────────────────────────────────────────────────
class Levels(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.voice_xp_task = None
        self.reaction_xp_task = None

    async def cog_load(self):
        """Start background tasks when cog loads"""
        self.voice_xp_task = self.bot.loop.create_task(self.voice_xp_loop())
        self.reaction_xp_task = self.bot.loop.create_task(self.reaction_xp_loop())
        print("✅ Levels background tasks started")

    async def cog_unload(self):
        """Cancel background tasks when cog unloads"""
        if self.voice_xp_task:
            self.voice_xp_task.cancel()
        if self.reaction_xp_task:
            self.reaction_xp_task.cancel()
        print("✅ Levels background tasks stopped")

    # ── Voice Activity Detection ────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        """Track when a user joins/moves in voice channels"""
        if member.bot:
            return

        if after.channel is not None:
            voice_last_activity[member.id] = datetime.now(timezone.utc).timestamp()
            voice_last_xp_time[member.id] = datetime.now(timezone.utc).timestamp()
        else:
            if member.id in voice_last_activity:
                del voice_last_activity[member.id]
            if member.id in voice_last_xp_time:
                del voice_last_xp_time[member.id]

    @commands.Cog.listener()
    async def on_socket_response(self, data):
        """Listen for voice speaking events to detect activity"""
        try:
            if data.get('t') == 'VOICE_SPEAKING' and data.get('d', {}).get('speaking'):
                user_id = int(data['d']['user_id'])
                voice_last_activity[user_id] = datetime.now(timezone.utc).timestamp()
        except (KeyError, ValueError, TypeError):
            pass

    # ── Voice XP Loop ────────────────────────────────────────────────────────
    async def voice_xp_loop(self):
        """Award XP to users in voice channels every minute if active"""
        await self.bot.wait_until_ready()

        while not self.bot.is_closed():
            try:
                await asyncio.sleep(VOICE_XP_INTERVAL)

                current_time = datetime.now(timezone.utc).timestamp()

                for guild in self.bot.guilds:
                    if guild.id != GUILD_ID:
                        continue

                    for channel in guild.voice_channels:
                        if not channel.members:
                            continue

                        for member in channel.members:
                            if member.bot:
                                continue

                            if not is_verified(member):
                                continue

                            last_active = voice_last_activity.get(member.id, 0)
                            time_since_activity = current_time - last_active

                            if time_since_activity > VOICE_INACTIVITY_TIMEOUT:
                                continue

                            last_xp_time = voice_last_xp_time.get(member.id, 0)
                            if current_time - last_xp_time < VOICE_XP_INTERVAL:
                                continue

                            voice_last_xp_time[member.id] = current_time

                            result = await award_xp(member.id, VOICE_XP_AMOUNT, member)

                            if result and result["leveled_up"]:
                                await send_level_up_message(
                                    self.bot,
                                    member,
                                    result["old_level"],
                                    result["new_level"],
                                    result["new_exp"]
                                )

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Error in voice XP loop: {e}")
                await asyncio.sleep(60)

    # ── Reaction XP Loop ─────────────────────────────────────────────────────
    async def reaction_xp_loop(self):
        """Process reaction XP periodically"""
        await self.bot.wait_until_ready()

        while not self.bot.is_closed():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Error in reaction XP loop: {e}")
                await asyncio.sleep(60)

    # ── Message XP Listener ──────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Award XP for messages"""
        if message.author.bot or not message.guild:
            return

        if not isinstance(message.channel, discord.TextChannel):
            return

        if not is_verified(message.author):
            return

        now = datetime.now(timezone.utc)

        # ── First Message of the Day ────────────────────────────────────────
        today = now.strftime("%Y-%m-%d")
        user_id = str(message.author.id)

        if last_message_date.get(user_id) != today:
            # First message of the day - award bonus XP
            last_message_date[user_id] = today
            result = await award_first_message_xp(message.author.id, message.author)
            if result:
                print(f"[XP] {message.author.display_name} got {FIRST_MESSAGE_XP} XP for first message of the day!")
                if result["leveled_up"]:
                    await send_level_up_message(
                        self.bot,
                        message.author,
                        result["old_level"],
                        result["new_level"],
                        result["new_exp"]
                    )

        # ── Regular Message XP ──────────────────────────────────────────────
        # Check cooldown
        if now.timestamp() - message_cooldown[message.author.id] < COOLDOWN_SECONDS:
            return

        message_cooldown[message.author.id] = now.timestamp()

        # Calculate XP gain (random between min and max)
        xp_gain = random.randint(XP_MIN, XP_MAX)

        # Bonus XP for longer messages
        if len(message.content) > 100:
            xp_gain += random.randint(5, 15)

        # Bonus XP for helpful responses (mentions)
        if message.mentions:
            xp_gain += 5

        result = await award_xp(message.author.id, xp_gain, message.author)

        if result and result["leveled_up"]:
            await send_level_up_message(
                self.bot,
                message.author,
                result["old_level"],
                result["new_level"],
                result["new_exp"]
            )

    # ── Reaction XP Listener ─────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_reaction_add(self, reaction: discord.Reaction, user: discord.Member):
        """Award XP for receiving reactions"""
        if user.bot or not reaction.message.guild:
            return

        if reaction.message.author.bot:
            return

        if not is_verified(user):
            return

        now = datetime.now(timezone.utc).timestamp()
        key = f"{reaction.message.author.id}_{reaction.message.id}"
        if now - reaction_cooldown[key] < 300:
            return

        reaction_cooldown[key] = now

        result = await award_xp(reaction.message.author.id, REACTION_XP_AMOUNT, reaction.message.author)

        if result and result["leveled_up"]:
            await send_level_up_message(
                self.bot,
                reaction.message.author,
                result["old_level"],
                result["new_level"],
                result["new_exp"]
            )

    # ── /rank ──────────────────────────────────────────────────────────────────
    @app_commands.command(
        name="rank",
        description="View your or another user's level, XP, and stats"
    )
    @app_commands.describe(user="User to check (leave empty for yourself)")
    @app_commands.checks.cooldown(1, 5)
    async def rank(self, interaction: discord.Interaction, user: discord.Member = None):
        target = user or interaction.user
        await interaction.response.defer(ephemeral=False)

        try:
            row = await d1_query(
                "SELECT level, exp, hoster_points, raids_completed FROM users WHERE discord_id = ?",
                [str(target.id)]
            )
            if not row["results"]:
                return await interaction.followup.send(
                    f"❌ {target.mention} hasn't verified yet — they need `/verify` first."
                )

            data = row["results"][0]
            level = data["level"] or 1
            exp = data["exp"] or 0
            hoster_pts = data["hoster_points"] or 0
            raids_done = data["raids_completed"] or 0

            xp_for_current = total_xp_for_level(level)
            xp_for_next = total_xp_for_level(level + 1)
            needed = xp_for_next - xp_for_current
            progress = max(0, exp - xp_for_current)
            percent = min(100, int((progress / needed) * 100)) if needed > 0 else 100
            bar = progress_bar(progress, needed, 16)

            rank_result = await d1_query(
                "SELECT COUNT(*) + 1 as rank FROM users WHERE exp > ?", [exp]
            )
            global_rank = rank_result["results"][0]["rank"] if rank_result["results"] else "N/A"
            rank_str = f"#{global_rank}" if str(global_rank) != "N/A" else "N/A"

            top10_badge = " 🏆" if str(global_rank) != "N/A" and int(global_rank) <= 10 else ""

            embed = discord.Embed(
                color=discord.Color.blurple(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.set_author(
                name=f"{target.display_name}{top10_badge}",
                icon_url=target.display_avatar.url if target.display_avatar else None
            )

            embed.add_field(
                name="Level",
                value=f"**{level}**",
                inline=True
            )
            embed.add_field(
                name="Total XP",
                value=f"**{exp:,}**",
                inline=True
            )
            embed.add_field(
                name="Rank",
                value=f"**{rank_str}**",
                inline=True
            )

            embed.add_field(
                name=f"Progress to Level {level + 1}",
                value=f"`{bar}` **{percent}%**\n{progress:,} / {needed:,} XP  •  **{needed - progress:,}** to go",
                inline=False
            )

            embed.add_field(
                name="Hoster Points",
                value=f"**{hoster_pts:,}**",
                inline=True
            )
            embed.add_field(
                name="Raids Completed",
                value=f"**{raids_done:,}**",
                inline=True
            )

            # XP bonus — only show for own card
            if target.id == interaction.user.id:
                _, bonuses, bonus_pct = get_xp_multiplier(target)
                if bonus_pct > 0:
                    embed.add_field(
                        name="✨ XP Bonus",
                        value=f"+**{bonus_pct}%** · {', '.join(bonuses)}",
                        inline=True
                    )

            embed.set_footer(text=f"ID: {target.id} · Use /xprewards to see how XP is earned")
            await interaction.followup.send(embed=embed)

        except Exception as e:
            print(f"Error in rank command: {e}")
            await interaction.followup.send(f"❌ Error: {str(e)[:100]}")

    # ── /leaderboard ──────────────────────────────────────────────────────────
    @app_commands.command(
        name="leaderboard",
        description="Top 10 users by level and XP"
    )
    @app_commands.checks.cooldown(1, 10)
    async def leaderboard(self, interaction: discord.Interaction):
        """Display the top 10 users by level and XP"""
        await interaction.response.defer(ephemeral=False)

        try:
            result = await d1_query(
                "SELECT discord_id, level, exp FROM users ORDER BY exp DESC LIMIT 10"
            )
            rows = result["results"]

            if not rows:
                return await interaction.followup.send("📭 No users found.")

            lines = []
            medals = ["🥇", "🥈", "🥉"]

            for i, row in enumerate(rows):
                prefix = medals[i] if i < 3 else f"`#{i+1}`"
                member = interaction.guild.get_member(int(row['discord_id']))
                name = member.display_name if member else f"<@{row['discord_id']}>"

                level = row['level'] or 1
                exp = row['exp'] or 0

                if exp > 0:
                    xp_for_current = total_xp_for_level(level)
                    xp_for_next = total_xp_for_level(level + 1)
                    progress = exp - xp_for_current
                    needed = xp_for_next - xp_for_current
                    progress = max(0, progress)
                    percent = int((progress / needed) * 100) if needed > 0 else 0
                    percent = max(0, min(100, percent))
                    bar = progress_bar(progress, needed, 8)
                else:
                    percent = 0
                    bar = "░░░░░░░░"

                # Show if user has bonuses
                bonus_tag = ""
                if member:
                    _, _, bonus_pct = get_xp_multiplier(member)
                    if bonus_pct > 0:
                        bonus_tag = " ✨"

                lines.append(
                    f"{prefix} **{name}**{bonus_tag} — Lvl **{level}** ({exp:,} XP) `{bar}` {percent}%"
                )

            embed = discord.Embed(
                title="🏆 Level Leaderboard",
                description="\n".join(lines),
                color=discord.Color.gold(),
                timestamp=datetime.now(timezone.utc)
            )

            embed.set_footer(text="✨ = Has XP bonus (Linked/Booster/Elite Hoster) • XP from messages, voice, reactions, and raids")

            await interaction.followup.send(embed=embed)

        except Exception as e:
            print(f"Error in leaderboard command: {e}")
            await interaction.followup.send(
                f"❌ Error: {str(e)[:100]}"
            )

    # ── /xprewards ────────────────────────────────────────────────────────────
    @app_commands.command(
        name="xprewards",
        description="View all the ways to earn XP and raid wave rewards"
    )
    @app_commands.checks.cooldown(1, 30)
    async def xprewards(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="📊 How to Earn XP",
            description="Every verified member earns XP. Level up to unlock roles and rewards!",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc)
        )

        # ── XP Methods ──────────────────────────────────────────────────────
        embed.add_field(
            name="💬 Messages",
            value=f"• {XP_MIN}-{XP_MAX} XP per message\n"
                  f"• +5-15 XP for long messages (>100 chars)\n"
                  f"• +5 XP for replying to others\n"
                  f"• Cooldown: {COOLDOWN_SECONDS}s between messages",
            inline=False
        )

        embed.add_field(
            name="👋 First Message of the Day",
            value=f"• {FIRST_MESSAGE_XP} XP for your first message each day\n"
                  f"• Resets at midnight UTC",
            inline=False
        )

        embed.add_field(
            name="🎙️ Voice Chat",
            value=f"• {VOICE_XP_AMOUNT} XP every {VOICE_XP_INTERVAL} seconds\n"
                  f"• Must be **actively speaking** in a voice channel\n"
                  f"• Stops after {VOICE_INACTIVITY_TIMEOUT//60} minutes of silence",
            inline=False
        )

        embed.add_field(
            name="👍 Reactions Received",
            value=f"• {REACTION_XP_AMOUNT} XP per reaction received\n"
                  f"• 5 minute cooldown per message",
            inline=False
        )

        embed.add_field(
            name="🎯 Raid Participation",
            value=f"• {RAID_PARTICIPATION_XP} XP for joining a raid\n"
                  f"• **Stacks** with raid wave XP!",
            inline=False
        )

        embed.add_field(
            name="👥 Invites",
            value=f"• {INVITE_XP_REWARD} XP per verified invite\n"
                  f"• Your invitee must verify their Roblox account",
            inline=False
        )

        # ── Bonus Info ──────────────────────────────────────────────────────
        embed.add_field(
            name="✨ XP Bonuses (Additive!)",
            value=(
                "• 🔗 **Linked Account**: +10% bonus on ALL XP\n"
                "• 🎂 **Server Booster**: +25% bonus on ALL XP\n"
                "• 👑 **Elite Hoster**: +25% bonus on ALL XP\n"
                "• **All bonuses add together!** (Up to 60%!)"
            ),
            inline=False
        )

        # ── Requirements ─────────────────────────────────────────────────────
        embed.add_field(
            name="📋 Requirements",
            value=(
                "• ✅ Must be **Verified** (`/verify`)\n"
                "• ✅ Must **not** be a bot\n"
                "• ✅ Must be in a server (not DMs)"
            ),
            inline=False
        )

        # Wave XP milestones (formula: 25 × 1.12^(wave-1))
        milestones = [(w, int(25 * (1.12 ** (w - 1)))) for w in [1, 5, 10, 15, 20]]
        wave_lines = "  ".join(f"W{w}: **{xp}**" for w, xp in milestones)
        total_xp_full = sum(int(25 * (1.12 ** (w - 1))) for w in range(1, 21))
        embed.add_field(
            name="⚔️ Raid Hosting (Hoster+ only)",
            value=(
                f"• XP scales per wave completed — higher waves = much more XP\n"
                f"{wave_lines}\n"
                f"• Full clear (wave 20): **{total_xp_full:,}** XP + **20** hoster points\n"
                f"• Elite Hosters get **+25%** on all raid XP"
            ),
            inline=False
        )

        embed.set_footer(text="Use /rank to see your current level and progress")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    """Setup function for the cog"""
    await bot.add_cog(Levels(bot))
    print("✅ Levels cog setup complete")