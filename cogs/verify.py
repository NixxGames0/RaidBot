import discord
from discord import app_commands
from discord.ext import commands, tasks
import asyncio
import json
import secrets
import requests
from datetime import datetime, timezone, timedelta
from functools import partial

# Import shared functions from bot.py
from bot import (
    d1_query, 
    GUILD_ID,
    VERIFIED_ROLE_ID,
    LINKED_ROLE_ID,
    FOUNDER_ROLE_ID,
    HEAD_STAFF_ROLE_ID,
    MOD_ROLE_ID,
    TRIAL_MOD_ROLE_ID,
    LOG_CHANNEL_ID,
    is_verified,
    is_linked,
    is_staff,
    is_mod_or_higher,
    is_head_staff_or_founder,
    is_founder,
    is_hoster,
    is_elite_hoster
)

# ── Import XP function from levels.py ──────────────────────────────────────
try:
    from cogs.levels import award_invite_xp
except ImportError:
    # Fallback if levels.py isn't loaded yet
    async def award_invite_xp(user_id, member):
        return None

# ── Channel IDs ──────────────────────────────────────────────────────────────
VERIFICATION_CHANNEL_ID = 1535735916893831260
MANUAL_VERIFY_CHANNEL_ID = 1535893394533126154

# ── Roles to ping for manual verification ──────────────────────────────────
PING_ROLES = [
    HEAD_STAFF_ROLE_ID,
    MOD_ROLE_ID,
    TRIAL_MOD_ROLE_ID,
]

# ── Staff roles that can approve manual verification ──────────────────────
STAFF_ROLES = {HEAD_STAFF_ROLE_ID, MOD_ROLE_ID, TRIAL_MOD_ROLE_ID, FOUNDER_ROLE_ID}
HEAD_STAFF_ROLES = {HEAD_STAFF_ROLE_ID, FOUNDER_ROLE_ID}

# ── Invite XP Reward ────────────────────────────────────────────────────────
INVITE_XP_REWARD = 250  # Updated from 150

# ── Timeout for verification (1 hour) ──────────────────────────────────────
VERIFICATION_TIMEOUT = 3600  # seconds

# ── Pending verifications in memory ────────────────────────────────────────
pending_verifications = {}

# ── Pending game-join verifications ─────────────────────────────────────────
# Keyed by discord_user_id (str), stores roblox_name, roblox_id, timestamp,
# and a reference to the followup message so we can edit it on success.
pending_game_verifications = {}

# ── RHVerif Game Info ────────────────────────────────────────────────────────
RHVERIF_GAME_URL = "https://www.roblox.com/games/start?placeId=77181424740709"
RHVERIF_GAME_NAME = "RHVerif"


# ── Logging Helper ───────────────────────────────────────────────────────────
async def send_log(bot: commands.Bot, embed: discord.Embed):
    """Send a log message to the main log channel"""
    channel = bot.get_channel(LOG_CHANNEL_ID)
    if channel:
        try:
            await channel.send(embed=embed)
        except Exception as e:
            print(f"Error sending log: {e}")


async def send_verification_log(bot: commands.Bot, embed: discord.Embed):
    """Send a verification log to the manual verification channel"""
    channel = bot.get_channel(MANUAL_VERIFY_CHANNEL_ID)
    if channel:
        try:
            await channel.send(embed=embed)
        except Exception as e:
            print(f"Error sending verification log: {e}")


# ── XP Functions ─────────────────────────────────────────────────────────────
def xp_needed_for_level(level: int) -> int:
    return 5 * (level ** 2) + 50 * level + 100


def total_xp_for_level(level: int) -> int:
    return sum(xp_needed_for_level(l) for l in range(level))


def level_from_xp(xp: int) -> int:
    level = 0
    while total_xp_for_level(level + 1) <= xp:
        level += 1
    return level


async def award_xp_to_user(user_id: int, amount: int, member=None):
    """Award XP to a user and handle level ups"""
    try:
        from cogs.levels import award_xp
        return await award_xp(user_id, amount, member)
    except ImportError:
        # Fallback if levels.py isn't loaded
        try:
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

            return {
                "old_level": current_level,
                "new_level": new_level,
                "old_exp": current_exp,
                "new_exp": new_exp,
                "xp_gained": amount,
                "leveled_up": new_level > current_level
            }
        except Exception as e:
            print(f"Error awarding XP: {e}")
            return None


# ── Roblox API Functions ────────────────────────────────────────────────────
def get_roblox_user_id_sync(username: str):
    """Get Roblox user ID from username (synchronous)"""
    try:
        res = requests.post(
            "https://users.roblox.com/v1/usernames/users",
            json={"usernames": [username], "excludeBannedUsers": False},
            timeout=10
        )
        if res.status_code == 200:
            data = res.json()
            if data.get("data"):
                return data["data"][0]
        return None
    except Exception as e:
        print(f"Error getting Roblox user ID: {e}")
        return None


async def get_roblox_user_id(username: str):
    """Get Roblox user ID from username (asynchronous)"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(get_roblox_user_id_sync, username))


async def get_roblox_profile(user_id: int):
    """Get Roblox profile data"""
    try:
        res = requests.get(
            f"https://users.roblox.com/v1/users/{user_id}",
            timeout=10
        )
        if res.status_code == 200:
            return res.json()
        return None
    except Exception as e:
        print(f"Error getting Roblox profile: {e}")
        return None


async def get_roblox_friend_count(user_id: int) -> int:
    """Get the number of friends a Roblox user has"""
    try:
        res = requests.get(
            f"https://friends.roblox.com/v1/users/{user_id}/friends/count",
            timeout=10
        )
        if res.status_code == 200:
            data = res.json()
            return data.get("count", 0)
        return 0
    except Exception:
        return 0


async def get_roblox_creation_date(user_id: int) -> datetime:
    """Get the creation date of a Roblox account"""
    try:
        profile = await get_roblox_profile(user_id)
        if profile and "created" in profile:
            return datetime.fromisoformat(profile["created"].replace("Z", "+00:00"))
        return None
    except Exception:
        return None


async def get_roblox_badges(user_id: int) -> list:
    """Get badges from a Roblox account"""
    try:
        badges = []
        cursor = None
        while True:
            url = f"https://badges.roblox.com/v1/users/{user_id}/badges?limit=100"
            if cursor:
                url += f"&cursor={cursor}"
            res = requests.get(url, timeout=10)
            if res.status_code == 200:
                data = res.json()
                badges.extend(data.get("data", []))
                cursor = data.get("nextPageCursor")
                if not cursor:
                    break
            else:
                break
        return badges
    except Exception as e:
        print(f"Error getting Roblox badges: {e}")
        return []


async def check_account_flagged(user_id: int) -> tuple:
    """
    Check if a Roblox account is flagged as a potential alt.
    Returns (is_flagged, flags_list)
    """
    flags = []

    # Check friend count
    friend_count = await get_roblox_friend_count(user_id)
    if friend_count < 5:
        flags.append(f"Very low friend count: {friend_count} friends")

    # Check account age
    creation_date = await get_roblox_creation_date(user_id)
    if creation_date:
        age_days = (datetime.now(timezone.utc) - creation_date).days
        if age_days < 14:
            flags.append(f"Account is very new: {age_days} days old")
        elif age_days < 30:
            flags.append(f"Account is new: {age_days} days old")

    # Check badge count
    badges = await get_roblox_badges(user_id)
    badge_count = len(badges)
    if badge_count < 50:
        flags.append(f"Very low badge count: {badge_count} badges")
    elif badge_count < 100:
        flags.append(f"Low badge count: {badge_count} badges")

    # Flag if 2 or more conditions are met
    is_flagged = len(flags) >= 2

    return is_flagged, flags


async def check_roblox_ownership(user_id: int, verification_code: str) -> bool:
    """Check if a Roblox user has the verification code in their profile description."""
    try:
        profile = await get_roblox_profile(user_id)
        if not profile:
            return False

        description = profile.get("description", "")
        code_no_spaces = verification_code.replace(" ", "")
        desc_no_spaces = description.replace(" ", "")
        return code_no_spaces in desc_no_spaces
    except Exception as e:
        print(f"Error checking Roblox ownership: {e}")
        return False


# ── Verification Functions ──────────────────────────────────────────────────
def generate_verification_code(length: int = 8) -> str:
    """Generate a random verification code using easy-to-read characters"""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def format_code_with_spaces(code: str) -> str:
    """Format code with spaces every 4 characters for readability"""
    return ' '.join(code[i:i+4] for i in range(0, len(code), 4))


async def finalize_verification(bot: commands.Bot, interaction: discord.Interaction,
                                target: discord.Member, roblox: str, roblox_id: int,
                                manual: bool = False):
    """Finalize the verification after ownership is confirmed"""
    roblox = roblox.strip()
    now = datetime.now(timezone.utc).isoformat()

    linked_role = interaction.guild.get_role(LINKED_ROLE_ID)
    if not linked_role:
        return await interaction.followup.send(
            "❌ Linked role not found in the server. Please contact an admin.",
            ephemeral=True
        )

    # Check if user already exists
    existing = await d1_query(
        "SELECT discord_id, roblox_users, invited_by FROM users WHERE discord_id = ?",
        [str(target.id)]
    )

    is_new_user = False
    inviter_id = None

    if existing["results"]:
        current = json.loads(existing["results"][0]["roblox_users"] or "[]")
        if roblox in current:
            return await interaction.followup.send(
                f"⚠️ `{roblox}` is already linked to this account.",
                ephemeral=True
            )
        current.append(roblox)
        await d1_query(
            "UPDATE users SET roblox_users = ?, updated_at = ? WHERE discord_id = ?",
            [json.dumps(current), now, str(target.id)]
        )

        # Add linked role if not present
        if linked_role not in target.roles:
            await target.add_roles(linked_role, reason="Verified via verification system")

        await interaction.followup.send(
            f"✅ Added `{roblox}` to {target.mention}'s linked accounts.",
            ephemeral=True
        )
    else:
        is_new_user = True

        # Check for invite tracking
        invite_tracking = await d1_query(
            "SELECT inviter_id FROM invite_tracking WHERE invitee_id = ? AND used = 0",
            [str(target.id)]
        )

        if invite_tracking["results"]:
            inviter_id = int(invite_tracking["results"][0]["inviter_id"])
            await d1_query(
                "UPDATE invite_tracking SET used = 1, used_at = ? WHERE invitee_id = ?",
                [now, str(target.id)]
            )

        # Create new user record
        await d1_query(
            """INSERT INTO users 
            (discord_id, roblox_users, ps_codes, in_raid, hoster_points, weekly_points,
             global_waves, raids_completed, risk_value, level, exp, invited_by, created_at, updated_at)
            VALUES (?, ?, '[]', 0, 0, 0, 0, 0, 0, 1, 0, ?, ?, ?)""",
            [str(target.id), json.dumps([roblox]), str(inviter_id) if inviter_id else None, now, now]
        )

        # Add linked role
        if linked_role:
            await target.add_roles(linked_role, reason="Verified via verification system")
            await interaction.followup.send(
                f"✅ {target.mention} has been linked as `{roblox}` and given the Linked role.",
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"✅ {target.mention} has been linked as `{roblox}`.",
                ephemeral=True
            )

        # Process invite reward with new XP system
        if inviter_id:
            await process_invite_reward(bot, target, inviter_id)

    # Send verification log
    log_embed = discord.Embed(
        title="🔗 Account Linked",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc)
    )
    log_embed.add_field(name="Discord", value=target.mention, inline=True)
    log_embed.add_field(name="Roblox Username", value=f"`{roblox}`", inline=True)
    log_embed.add_field(name="Roblox ID", value=f"`{roblox_id}`", inline=True)
    log_embed.add_field(
        name="Verification Method",
        value="✅ Ownership Verified" if not manual else "🔧 Manual Verification",
        inline=True
    )
    if inviter_id:
        log_embed.add_field(name="Invited By", value=f"<@{inviter_id}>", inline=True)

    if is_new_user:
        log_embed.add_field(name="New User", value="✅ Yes", inline=True)

    log_embed.set_footer(text=f"User ID: {target.id}")
    await send_verification_log(bot, log_embed)

    # Also send to main log
    main_embed = discord.Embed(
        title="🔗 Verification Complete",
        description=f"{target.mention} has verified their Roblox account!",
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc)
    )
    main_embed.add_field(name="Roblox", value=f"`{roblox}`", inline=True)
    await send_log(bot, main_embed)


async def process_invite_reward(bot: commands.Bot, new_user: discord.Member, inviter_id: int):
    """Process invite reward for the inviter using the new XP system"""
    try:
        # Check if inviter exists in database
        inviter_check = await d1_query(
            "SELECT discord_id FROM users WHERE discord_id = ?",
            [str(inviter_id)]
        )

        if not inviter_check["results"]:
            return

        # Get inviter member object
        inviter = new_user.guild.get_member(inviter_id)
        if not inviter:
            # Try to fetch the user
            inviter = await bot.fetch_user(inviter_id)
            if not inviter:
                return

        # Award XP using the new system (with bonuses)
        xp_result = await award_invite_xp(inviter_id, inviter)

        # Try to DM the inviter
        try:
            dm_embed = discord.Embed(
                title="🎉 Invite Reward!",
                description=f"{new_user.mention} just verified their Roblox account!",
                color=discord.Color.gold(),
                timestamp=datetime.now(timezone.utc)
            )
            
            if xp_result:
                bonus_text = xp_result.get("bonus_text", "")
                dm_embed.add_field(
                    name="Reward",
                    value=f"+**{INVITE_XP_REWARD}** XP{bonus_text}",
                    inline=True
                )
                if xp_result.get("leveled_up", False):
                    dm_embed.add_field(
                        name="Level Up!",
                        value=f"🎉 You reached Level **{xp_result['new_level']}**!",
                        inline=True
                    )
            else:
                dm_embed.add_field(
                    name="Reward",
                    value=f"+**{INVITE_XP_REWARD}** XP",
                    inline=True
                )
            
            dm_embed.set_footer(text="Keep inviting friends to earn more XP!")
            await inviter.send(embed=dm_embed)
        except Exception as e:
            print(f"Could not DM inviter {inviter_id}: {e}")

        # Log the invite reward
        log_embed = discord.Embed(
            title="🎉 Invite Reward",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc)
        )
        log_embed.add_field(name="Inviter", value=f"<@{inviter_id}>", inline=True)
        log_embed.add_field(name="Invitee", value=new_user.mention, inline=True)
        log_embed.add_field(name="XP Reward", value=f"+**{INVITE_XP_REWARD}** XP", inline=True)
        
        if xp_result and xp_result.get("bonus_text"):
            log_embed.add_field(name="Bonuses Applied", value=xp_result.get("bonus_text", ""), inline=True)
        
        await send_log(bot, log_embed)

    except Exception as e:
        print(f"Error processing invite reward: {e}")


async def send_manual_verification(bot: commands.Bot, interaction: discord.Interaction,
                                   user: discord.Member, roblox_name: str,
                                   roblox_id: int, flags: list):
    """Send a manual verification request to the manual verify channel"""
    channel = bot.get_channel(MANUAL_VERIFY_CHANNEL_ID)
    if not channel:
        return await interaction.followup.send(
            "❌ Manual verification channel not found. Please contact staff.",
            ephemeral=True
        )

    # Create the manual verification embed
    embed = discord.Embed(
        title="⚠️ Manual Verification Required",
        description=f"{user.mention} is trying to verify but their account has been flagged.",
        color=discord.Color.orange(),
        timestamp=datetime.now(timezone.utc)
    )

    embed.add_field(name="User", value=f"{user.mention} (`{user.id}`)", inline=False)
    embed.add_field(name="Roblox Username", value=f"`{roblox_name}`", inline=True)
    embed.add_field(name="Roblox ID", value=f"`{roblox_id}`", inline=True)

    if flags:
        flag_text = "\n".join([f"• {flag}" for flag in flags])
        embed.add_field(name="🚩 Flags", value=flag_text, inline=False)
    else:
        embed.add_field(name="🚩 Flags", value="No specific flags detected", inline=False)

    embed.add_field(
        name="📋 Instructions",
        value=(
            "Please review this user's Roblox account and decide if they should be verified.\n"
            "Click **Approve** to verify them, or **Deny** to reject."
        ),
        inline=False
    )

    embed.set_footer(text="Manual Verification Required")

    # Create view with approve/deny buttons
    view = ManualVerifyView(
        user_id=user.id,
        roblox_name=roblox_name,
        roblox_id=roblox_id,
        original_interaction=interaction,
        flags=flags
    )

    # Ping the staff roles
    role_mentions = " ".join([f"<@&{role_id}>" for role_id in PING_ROLES if interaction.guild.get_role(role_id)])
    await channel.send(content=f"{role_mentions} - Manual verification required!", embed=embed, view=view)

    await interaction.followup.send(
        "⚠️ Your account has been flagged for manual review. Staff have been notified and will review your verification request.\n"
        "Please wait for a staff member to approve your account.",
        ephemeral=True
    )

    # Log the manual verification request
    log_embed = discord.Embed(
        title="⚠️ Manual Verification Request",
        color=discord.Color.orange(),
        timestamp=datetime.now(timezone.utc)
    )
    log_embed.add_field(name="User", value=user.mention, inline=True)
    log_embed.add_field(name="Roblox Username", value=f"`{roblox_name}`", inline=True)
    log_embed.add_field(name="Roblox ID", value=f"`{roblox_id}`", inline=True)
    if flags:
        log_embed.add_field(name="Flags", value="\n".join(flags), inline=False)
    await send_log(bot, log_embed)


# ── Views ────────────────────────────────────────────────────────────────────
class ManualVerifyView(discord.ui.View):
    def __init__(self, user_id: int, roblox_name: str, roblox_id: int,
                 original_interaction: discord.Interaction, flags: list):
        super().__init__(timeout=86400)  # 24 hours
        self.user_id = user_id
        self.roblox_name = roblox_name
        self.roblox_id = roblox_id
        self.original_interaction = original_interaction
        self.flags = flags

    @discord.ui.button(label="✅ Approve", style=discord.ButtonStyle.success, emoji="✅")
    async def approve_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message(
                "❌ You do not have permission to approve this.",
                ephemeral=True
            )

        await interaction.response.defer()

        # Get the user
        user = interaction.guild.get_member(self.user_id)
        if not user:
            return await interaction.followup.send("❌ User not found in the server.", ephemeral=True)

        # Finalize verification
        await finalize_verification(
            interaction.client,
            self.original_interaction,
            user,
            self.roblox_name,
            self.roblox_id,
            manual=True
        )

        # DM the user
        try:
            dm_embed = discord.Embed(
                title="✅ Verification Approved!",
                description=f"Your Roblox account **{self.roblox_name}** has been approved by staff.",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc)
            )
            dm_embed.add_field(
                name="Welcome!",
                value="You now have access to all channels. Enjoy!",
                inline=False
            )
            await user.send(embed=dm_embed)
        except Exception:
            pass

        # Update the original message
        embed = interaction.message.embeds[0]
        embed.color = discord.Color.green()
        embed.add_field(
            name="✅ Approved",
            value=f"Approved by {interaction.user.mention}",
            inline=False
        )

        self.approve_button.disabled = True
        self.deny_button.disabled = True

        await interaction.message.edit(embed=embed, view=self)

        # Log the approval
        log_embed = discord.Embed(
            title="✅ Manual Verification Approved",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc)
        )
        log_embed.add_field(name="User", value=f"<@{self.user_id}>", inline=True)
        log_embed.add_field(name="Roblox", value=f"`{self.roblox_name}`", inline=True)
        log_embed.add_field(name="Approved By", value=interaction.user.mention, inline=True)
        await send_log(interaction.client, log_embed)

        await interaction.followup.send("✅ User has been verified.", ephemeral=True)

    @discord.ui.button(label="❌ Deny", style=discord.ButtonStyle.danger, emoji="❌")
    async def deny_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message(
                "❌ You do not have permission to deny this.",
                ephemeral=True
            )

        # Show modal for denial reason
        await interaction.response.send_modal(DenyReasonModal(
            user_id=self.user_id,
            roblox_name=self.roblox_name,
            roblox_id=self.roblox_id,
            original_interaction=self.original_interaction
        ))


class DenyReasonModal(discord.ui.Modal, title="Deny Verification"):
    reason = discord.ui.TextInput(
        label="Reason for Denial",
        placeholder="Please provide a reason why this verification was denied...",
        required=True,
        max_length=500,
        style=discord.TextStyle.paragraph
    )

    def __init__(self, user_id: int, roblox_name: str, roblox_id: int,
                 original_interaction: discord.Interaction):
        super().__init__()
        self.user_id = user_id
        self.roblox_name = roblox_name
        self.roblox_id = roblox_id
        self.original_interaction = original_interaction

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()

        # Get the user
        user = interaction.guild.get_member(self.user_id)

        # Update the original message
        channel = interaction.client.get_channel(MANUAL_VERIFY_CHANNEL_ID)
        if channel:
            async for msg in channel.history(limit=50):
                if msg.embeds and msg.embeds[0].title == "⚠️ Manual Verification Required":
                    # Check if it has our user ID
                    for field in msg.embeds[0].fields:
                        if field.name == "User" and f"<@{self.user_id}>" in field.value:
                            embed = msg.embeds[0]
                            embed.color = discord.Color.red()
                            embed.add_field(
                                name="❌ Denied",
                                value=f"Denied by {interaction.user.mention}\n**Reason:** {self.reason.value}",
                                inline=False
                            )

                            # Disable buttons
                            for child in msg.components:
                                if hasattr(child, "children"):
                                    for button in child.children:
                                        button.disabled = True

                            await msg.edit(embed=embed, view=msg.components[0])
                            break

        # DM the user
        if user:
            try:
                dm_embed = discord.Embed(
                    title="❌ Verification Denied",
                    description=f"Your Roblox account **{self.roblox_name}** verification was denied.",
                    color=discord.Color.red(),
                    timestamp=datetime.now(timezone.utc)
                )
                dm_embed.add_field(
                    name="Reason",
                    value=self.reason.value,
                    inline=False
                )
                dm_embed.add_field(
                    name="What to do?",
                    value="Please fix the issues mentioned above and try again, or contact staff for more information.",
                    inline=False
                )
                await user.send(embed=dm_embed)
            except Exception:
                pass

        # Log the denial
        log_embed = discord.Embed(
            title="❌ Manual Verification Denied",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc)
        )
        log_embed.add_field(name="User", value=f"<@{self.user_id}>", inline=True)
        log_embed.add_field(name="Roblox", value=f"`{self.roblox_name}`", inline=True)
        log_embed.add_field(name="Denied By", value=interaction.user.mention, inline=True)
        log_embed.add_field(name="Reason", value=self.reason.value, inline=False)
        await send_log(interaction.client, log_embed)

        await interaction.followup.send("✅ Verification denied and user has been notified.", ephemeral=True)


class VerifyConfirmView(discord.ui.View):
    def __init__(self, user_id: str):
        super().__init__(timeout=VERIFICATION_TIMEOUT)
        self.user_id = user_id

    @discord.ui.button(label="✅ I've Added It", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.user_id:
            return await interaction.response.send_message(
                "❌ This verification is not for you.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        if self.user_id not in pending_verifications:
            return await interaction.followup.send(
                "❌ No pending verification found. Please start with `/verify <roblox_username>` first.",
                ephemeral=True
            )

        pending = pending_verifications[self.user_id]
        roblox_name = pending["roblox_name"]
        roblox_id = pending["roblox_id"]
        code = pending["code"]

        # Check if the code is in the Roblox profile
        ownership_verified = await check_roblox_ownership(roblox_id, code)

        if not ownership_verified:
            formatted_code = pending.get("formatted_code", code)
            return await interaction.followup.send(
                f"❌ Verification code not found in your Roblox profile.\n"
                f"Make sure you added `{formatted_code}` to your profile's **About** section.\n"
                f"Then click the button again.\n\n"
                f"💡 **Tip:** If Roblox censored the code, click **🔄 Regenerate Code** for a new one.",
                ephemeral=True
            )

        # Remove pending verification
        del pending_verifications[self.user_id]

        # Finalize verification
        await finalize_verification(
            interaction.client,
            interaction,
            interaction.user,
            roblox_name,
            roblox_id
        )

        # Disable all buttons
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)

    @discord.ui.button(label="🔄 Regenerate Code", style=discord.ButtonStyle.secondary, emoji="🔄")
    async def regenerate_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.user_id:
            return await interaction.response.send_message(
                "❌ This verification is not for you.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        if self.user_id not in pending_verifications:
            return await interaction.followup.send(
                "❌ No pending verification found. Please start with `/verify <roblox_username>` first.",
                ephemeral=True
            )

        pending = pending_verifications[self.user_id]
        roblox_name = pending["roblox_name"]
        roblox_id = pending["roblox_id"]

        # Generate new code
        new_code = generate_verification_code()
        new_formatted_code = format_code_with_spaces(new_code)

        # Update pending verification
        pending_verifications[self.user_id]["code"] = new_code
        pending_verifications[self.user_id]["formatted_code"] = new_formatted_code
        pending_verifications[self.user_id]["timestamp"] = datetime.now(timezone.utc).timestamp()

        # Update the embed
        embed = interaction.message.embeds[0]
        for i, field in enumerate(embed.fields):
            if field.name == "📝 Step 2":
                embed.set_field_at(
                    i,
                    name="📝 Step 2",
                    value=f"Add this verification code to your **About** section (with or without spaces):\n```\n{new_formatted_code}\n```\n**Copy the code above** and paste it into your profile.",
                    inline=False
                )
                break

        # Re-enable confirm button
        for item in self.children:
            if hasattr(item, "custom_id") and "confirm" in str(item.custom_id):
                item.disabled = False

        await interaction.followup.send(
            f"✅ New verification code generated: `{new_formatted_code}`\n"
            f"Replace the old code in your Roblox profile with this new one.",
            ephemeral=True
        )

        await interaction.message.edit(embed=embed, view=self)

        # Log regeneration
        log_embed = discord.Embed(
            title="🔄 Verification Code Regenerated",
            color=discord.Color.yellow(),
            timestamp=datetime.now(timezone.utc)
        )
        log_embed.add_field(name="User", value=interaction.user.mention, inline=True)
        log_embed.add_field(name="Roblox Username", value=f"`{roblox_name}`", inline=True)
        await send_verification_log(interaction.client, log_embed)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger, emoji="❌")
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.user_id:
            return await interaction.response.send_message(
                "❌ This verification is not for you.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        if self.user_id not in pending_verifications:
            return await interaction.followup.send(
                "❌ No pending verification found to cancel.",
                ephemeral=True
            )

        pending = pending_verifications[self.user_id]
        roblox_name = pending["roblox_name"]

        # Remove pending verification
        del pending_verifications[self.user_id]

        # Disable all buttons
        for item in self.children:
            item.disabled = True

        # Update the embed
        embed = interaction.message.embeds[0]
        embed.color = discord.Color.red()
        embed.title = "❌ Verification Cancelled"
        embed.description = "You have cancelled the verification process."

        await interaction.message.edit(embed=embed, view=self)

        await interaction.followup.send(
            "✅ Verification has been cancelled.",
            ephemeral=True
        )

        # Log cancellation
        log_embed = discord.Embed(
            title="❌ Verification Cancelled",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc)
        )
        log_embed.add_field(name="User", value=interaction.user.mention, inline=True)
        log_embed.add_field(name="Roblox Username", value=f"`{roblox_name}`", inline=True)
        await send_verification_log(interaction.client, log_embed)


class VerifyModal(discord.ui.Modal, title="Verify Your Roblox Account"):
    roblox_username = discord.ui.TextInput(
        label="Roblox Username",
        placeholder="Enter your Roblox username...",
        required=True,
        max_length=50
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        # Check if user is Double Counter verified
        if not is_verified(interaction.user):
            return await interaction.followup.send(
                "❌ You must be verified with Double Counter first before you can link your Roblox account.",
                ephemeral=True
            )

        roblox_user = self.roblox_username.value.strip()

        # Get Roblox user info
        roblox_data = await get_roblox_user_id(roblox_user)
        if not roblox_data:
            return await interaction.followup.send(
                f"❌ Roblox user `{roblox_user}` does not exist.",
                ephemeral=True
            )

        roblox_name = roblox_data["name"]
        roblox_id = roblox_data["id"]

        # Check if Roblox account is already linked to another Discord user
        claimed = await d1_query(
            "SELECT discord_id FROM users WHERE roblox_users LIKE ?",
            [f'%"{roblox_name}"%']
        )
        if claimed["results"]:
            owner_id = claimed["results"][0]["discord_id"]
            if str(owner_id) != str(interaction.user.id):
                return await interaction.followup.send(
                    f"❌ `{roblox_name}` is already linked to another account.",
                    ephemeral=True
                )

        # Check if account is flagged
        is_flagged, flags = await check_account_flagged(roblox_id)

        if is_flagged:
            await send_manual_verification(
                interaction.client,
                interaction,
                interaction.user,
                roblox_name,
                roblox_id,
                flags
            )
            return

        # Normal verification flow
        code = generate_verification_code()
        formatted_code = format_code_with_spaces(code)

        # Store pending verification
        pending_verifications[str(interaction.user.id)] = {
            "roblox_name": roblox_name,
            "roblox_id": roblox_id,
            "code": code,
            "formatted_code": formatted_code,
            "timestamp": datetime.now(timezone.utc).timestamp()
        }

        embed = discord.Embed(
            title="🔐 Roblox Ownership Verification",
            description=f"To verify that you own the Roblox account **{roblox_name}**, please follow these steps:",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc)
        )

        embed.add_field(
            name="📝 Step 1",
            value="Go to your Roblox profile settings and edit your **About** section.",
            inline=False
        )

        embed.add_field(
            name="📝 Step 2",
            value=f"Add this verification code to your **About** section (with or without spaces):\n```\n{formatted_code}\n```\n**Copy the code above** and paste it into your profile.",
            inline=False
        )

        embed.add_field(
            name="📝 Step 3",
            value="Click the **✅ I've Added It** button below when you're done.",
            inline=False
        )

        embed.add_field(
            name="⏰ Time Limit",
            value="You have **1 hour** to complete this verification.",
            inline=True
        )

        embed.add_field(
            name="🔄 Code Not Working?",
            value="If the code is being censored by Roblox, click **🔄 Regenerate Code** for a new one.",
            inline=True
        )

        embed.add_field(
            name="🔒 Security",
            value="This code is unique and generated for this verification attempt only.",
            inline=True
        )

        embed.set_footer(text=f"Code will expire in 1 hour | Roblox ID: {roblox_id}")

        view = VerifyConfirmView(user_id=str(interaction.user.id))
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

        # Log verification started
        log_embed = discord.Embed(
            title="🔐 Verification Started",
            color=discord.Color.yellow(),
            timestamp=datetime.now(timezone.utc)
        )
        log_embed.add_field(name="User", value=interaction.user.mention, inline=True)
        log_embed.add_field(name="Roblox Username", value=f"`{roblox_name}`", inline=True)
        log_embed.add_field(name="Roblox ID", value=f"`{roblox_id}`", inline=True)
        await send_verification_log(interaction.client, log_embed)


# ── Verification Method Select ───────────────────────────────────────────────
class VerifyMethodSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(
                label="Bio Code",
                value="bio_code",
                description="Add a code to your Roblox profile's About section.",
                emoji="📝"
            ),
            discord.SelectOption(
                label="Join Game",
                value="join_game",
                description="Join the RHVerif Roblox game to verify instantly.",
                emoji="🎮"
            ),
        ]
        super().__init__(
            placeholder="Choose a verification method...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="verify_method_select"
        )

    async def callback(self, interaction: discord.Interaction):
        method = self.values[0]

        if method == "bio_code":
            await interaction.response.send_modal(VerifyModal())

        elif method == "join_game":
            await interaction.response.send_modal(GameVerifyUsernameModal())


class VerifyMethodView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(VerifyMethodSelect())


# ── Game Verify: Username Input Modal ───────────────────────────────────────
class GameVerifyUsernameModal(discord.ui.Modal, title="Verify via RHVerif Game"):
    roblox_username = discord.ui.TextInput(
        label="Roblox Username",
        placeholder="Enter your exact Roblox username...",
        required=True,
        max_length=50
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if not is_verified(interaction.user):
            return await interaction.followup.send(
                "❌ You must be verified with Double Counter first before linking your Roblox account.",
                ephemeral=True
            )

        roblox_user = self.roblox_username.value.strip()

        # Validate Roblox user exists
        roblox_data = await get_roblox_user_id(roblox_user)
        if not roblox_data:
            return await interaction.followup.send(
                f"❌ Roblox user `{roblox_user}` does not exist. Double-check your spelling!",
                ephemeral=True
            )

        roblox_name = roblox_data["name"]
        roblox_id = roblox_data["id"]

        # Check if already claimed by someone else
        claimed = await d1_query(
            "SELECT discord_id FROM users WHERE roblox_users LIKE ?",
            [f'%"{roblox_name}"%']
        )
        if claimed["results"]:
            owner_id = claimed["results"][0]["discord_id"]
            if str(owner_id) != str(interaction.user.id):
                return await interaction.followup.send(
                    f"❌ `{roblox_name}` is already linked to another account.",
                    ephemeral=True
                )

        # Check if flagged (same anti-alt logic)
        is_flagged, flags = await check_account_flagged(roblox_id)
        if is_flagged:
            await send_manual_verification(
                interaction.client,
                interaction,
                interaction.user,
                roblox_name,
                roblox_id,
                flags
            )
            return

        # Store as a pending game verification
        pending_game_verifications[str(interaction.user.id)] = {
            "roblox_name": roblox_name,
            "roblox_id": roblox_id,
            "timestamp": datetime.now(timezone.utc).timestamp(),
            "message": None,  # filled in below after we send the embed
        }

        embed = discord.Embed(
            title="🎮 Verify via RHVerif Game",
            description=(
                f"To verify ownership of **{roblox_name}**, join the RHVerif Roblox game "
                f"and the bot will automatically detect you!"
            ),
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc)
        )

        embed.add_field(
            name="📋 Instructions",
            value=(
                f"1. Make sure you're logged in as **{roblox_name}** on Roblox.\n"
                f"2. Click the link below to join the game.\n"
                f"3. Wait a few seconds — verification is automatic once you join!\n"
            ),
            inline=False
        )

        embed.add_field(
            name="🔗 Game Link",
            value=f"[Click here to join **{RHVERIF_GAME_NAME}**]({RHVERIF_GAME_URL})",
            inline=False
        )

        embed.add_field(
            name="⏰ Time Limit",
            value="This verification slot expires in **1 hour**.",
            inline=True
        )

        embed.add_field(
            name="⏳ Status",
            value="Waiting for you to join the game...",
            inline=True
        )

        embed.set_footer(text=f"Roblox ID: {roblox_id} | Join the game to complete verification")

        cancel_view = GameVerifyCancelView(user_id=str(interaction.user.id))
        msg = await interaction.followup.send(embed=embed, view=cancel_view, ephemeral=True)

        # Store message reference for later editing on success
        if str(interaction.user.id) in pending_game_verifications:
            pending_game_verifications[str(interaction.user.id)]["message"] = msg

        # Log it
        log_embed = discord.Embed(
            title="🎮 Game Verification Started",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc)
        )
        log_embed.add_field(name="User", value=interaction.user.mention, inline=True)
        log_embed.add_field(name="Roblox Username", value=f"`{roblox_name}`", inline=True)
        log_embed.add_field(name="Roblox ID", value=f"`{roblox_id}`", inline=True)
        await send_verification_log(interaction.client, log_embed)


class GameVerifyCancelView(discord.ui.View):
    def __init__(self, user_id: str):
        super().__init__(timeout=VERIFICATION_TIMEOUT)
        self.user_id = user_id

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger, emoji="❌")
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.user_id:
            return await interaction.response.send_message(
                "❌ This isn't your verification.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        if self.user_id in pending_game_verifications:
            roblox_name = pending_game_verifications[self.user_id]["roblox_name"]
            del pending_game_verifications[self.user_id]

            embed = interaction.message.embeds[0]
            embed.color = discord.Color.red()
            embed.title = "❌ Verification Cancelled"
            embed.description = "You cancelled the game verification."

            for item in self.children:
                item.disabled = True
            await interaction.message.edit(embed=embed, view=self)

            await interaction.followup.send("✅ Verification cancelled.", ephemeral=True)

            log_embed = discord.Embed(
                title="❌ Game Verification Cancelled",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc)
            )
            log_embed.add_field(name="User", value=interaction.user.mention, inline=True)
            log_embed.add_field(name="Roblox", value=f"`{roblox_name}`", inline=True)
            await send_verification_log(interaction.client, log_embed)
        else:
            await interaction.followup.send("⚠️ No pending verification to cancel.", ephemeral=True)


# ── Helper: complete a game verification (called from bot.py web callback) ──
async def complete_game_verification(bot: commands.Bot, roblox_id: int, roblox_name: str):
    """
    Called by the aiohttp callback in bot.py when a player joins the RHVerif game.
    Looks up the pending game verification matching this roblox_id, finalizes it,
    and updates the user's waiting embed.
    """
    roblox_id = int(roblox_id)  # normalize always
    discord_user_id = None
    print(f"🔍 Looking for roblox_id={roblox_id} in pending_game_verifications: {list(pending_game_verifications.keys())}")
    for uid, data in list(pending_game_verifications.items()):
        stored_id = int(data.get("roblox_id", -1))
        print(f"   Checking uid={uid}, stored_id={stored_id}")
        if stored_id == roblox_id:
            discord_user_id = uid
            break

    if not discord_user_id:
        print(f"⚠️ Game join received for {roblox_name} (ID: {roblox_id}) but no pending game verification found.")
        return False

    pending = pending_game_verifications.pop(discord_user_id)

    guild = bot.get_guild(GUILD_ID)
    if not guild:
        print("❌ Guild not found during game verification completion.")
        return False

    member = guild.get_member(int(discord_user_id))
    if not member:
        print(f"❌ Member {discord_user_id} not found in guild.")
        return False

    # Edit the waiting embed to show success
    try:
        msg = pending.get("message")
        if msg:
            success_embed = discord.Embed(
                title="✅ Game Verification Successful!",
                description=f"Welcome, **{roblox_name}**! Your Roblox account has been linked.",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc)
            )
            success_embed.set_footer(text="You can now close this. Enjoy Raid Hub!")
            # Disable the cancel button by passing an empty view
            done_view = discord.ui.View()
            await msg.edit(embed=success_embed, view=done_view)
    except Exception as e:
        print(f"⚠️ Could not edit game verify message: {e}")

    # Send a DM too for good measure
    try:
        dm_embed = discord.Embed(
            title="✅ Roblox Account Linked!",
            description=f"Your Roblox account **{roblox_name}** has been verified via the RHVerif game.",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc)
        )
        dm_embed.add_field(name="Welcome!", value="You now have full access to Raid Hub. Enjoy!", inline=False)
        await member.send(embed=dm_embed)
    except Exception:
        pass

    # Actually finalize the verification in DB + give roles
    # We create a dummy interaction wrapper since finalize_verification expects one
    # but for game-join we pass None and handle it inside finalize
    await _finalize_game_verification(bot, member, roblox_name, roblox_id)

    print(f"✅ Game verification complete: {roblox_name} -> {member.display_name}")
    return True


async def _finalize_game_verification(bot: commands.Bot, member: discord.Member, roblox: str, roblox_id: int):
    """
    A version of finalize_verification that doesn't need an interaction object,
    used specifically for the automated game-join flow.
    """
    roblox = roblox.strip()
    now = datetime.now(timezone.utc).isoformat()

    guild = member.guild
    linked_role = guild.get_role(LINKED_ROLE_ID)
    if not linked_role:
        print("❌ Linked role not found during game finalization.")
        return

    existing = await d1_query(
        "SELECT discord_id, roblox_users, invited_by FROM users WHERE discord_id = ?",
        [str(member.id)]
    )

    is_new_user = False
    inviter_id = None

    if existing["results"]:
        current = json.loads(existing["results"][0]["roblox_users"] or "[]")
        if roblox not in current:
            current.append(roblox)
            await d1_query(
                "UPDATE users SET roblox_users = ?, updated_at = ? WHERE discord_id = ?",
                [json.dumps(current), now, str(member.id)]
            )
        if linked_role not in member.roles:
            await member.add_roles(linked_role, reason="Verified via RHVerif game")
    else:
        is_new_user = True

        # Check invite tracking
        invite_tracking = await d1_query(
            "SELECT inviter_id FROM invite_tracking WHERE invitee_id = ? AND used = 0",
            [str(member.id)]
        )
        if invite_tracking["results"]:
            inviter_id = int(invite_tracking["results"][0]["inviter_id"])
            await d1_query(
                "UPDATE invite_tracking SET used = 1, used_at = ? WHERE invitee_id = ?",
                [now, str(member.id)]
            )

        await d1_query(
            """INSERT INTO users 
            (discord_id, roblox_users, ps_codes, in_raid, hoster_points, weekly_points,
             global_waves, raids_completed, risk_value, level, exp, invited_by, created_at, updated_at)
            VALUES (?, ?, '[]', 0, 0, 0, 0, 0, 0, 1, 0, ?, ?, ?)""",
            [str(member.id), json.dumps([roblox]), str(inviter_id) if inviter_id else None, now, now]
        )

        await member.add_roles(linked_role, reason="Verified via RHVerif game")

        if inviter_id:
            await process_invite_reward(bot, member, inviter_id)

    # Verification log
    log_embed = discord.Embed(
        title="🔗 Account Linked (Game Join)",
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc)
    )
    log_embed.add_field(name="Discord", value=member.mention, inline=True)
    log_embed.add_field(name="Roblox Username", value=f"`{roblox}`", inline=True)
    log_embed.add_field(name="Roblox ID", value=f"`{roblox_id}`", inline=True)
    log_embed.add_field(name="Method", value="🎮 RHVerif Game Join", inline=True)
    if inviter_id:
        log_embed.add_field(name="Invited By", value=f"<@{inviter_id}>", inline=True)
    if is_new_user:
        log_embed.add_field(name="New User", value="✅ Yes", inline=True)
    log_embed.set_footer(text=f"User ID: {member.id}")
    await send_verification_log(bot, log_embed)

    # Main log
    main_embed = discord.Embed(
        title="🔗 Verification Complete (Game Join)",
        description=f"{member.mention} verified via the RHVerif Roblox game!",
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc)
    )
    main_embed.add_field(name="Roblox", value=f"`{roblox}`", inline=True)

    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        try:
            await log_channel.send(embed=main_embed)
        except Exception as e:
            print(f"Error sending main log: {e}")


class VerifyPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Start Verification",
        style=discord.ButtonStyle.success,
        emoji="🔐",
        custom_id="verify_start_button"
    )
    async def verify_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_verified(interaction.user):
            return await interaction.response.send_message(
                "❌ You must be verified with Double Counter first before you can link your Roblox account.",
                ephemeral=True
            )

        embed = discord.Embed(
            title="🔐 Choose Verification Method",
            description="How would you like to prove ownership of your Roblox account?",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc)
        )

        embed.add_field(
            name="📝 Bio Code",
            value=(
                "Add a short code to your Roblox profile's **About** section.\n"
                "Great if you don't want to join a game."
            ),
            inline=False
        )

        embed.add_field(
            name="🎮 Join Game",
            value=(
                f"Join **{RHVERIF_GAME_NAME}** on Roblox and the bot verifies you automatically.\n"
                "Fastest method — no profile editing required!"
            ),
            inline=False
        )

        embed.set_footer(text="Select a method from the dropdown below")

        await interaction.response.send_message(
            embed=embed,
            view=VerifyMethodView(),
            ephemeral=True
        )


# ── Verify Cog ──────────────────────────────────────────────────────────────
class Verify(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.cleanup_pending.start()

    def cog_unload(self):
        self.cleanup_pending.cancel()

    @tasks.loop(hours=1)
    async def cleanup_pending(self):
        """Clean up expired pending verifications"""
        now = datetime.now(timezone.utc).timestamp()

        expired = [uid for uid, data in pending_verifications.items()
                   if now - data["timestamp"] > VERIFICATION_TIMEOUT]
        for uid in expired:
            del pending_verifications[uid]
            print(f"Cleaned up expired bio verification for user {uid}")

        expired_game = [uid for uid, data in pending_game_verifications.items()
                        if now - data["timestamp"] > VERIFICATION_TIMEOUT]
        for uid in expired_game:
            del pending_game_verifications[uid]
            print(f"Cleaned up expired game verification for user {uid}")

    @app_commands.command(
        name="startverification",
        description="Link your Roblox account to Raid Hub"
    )
    @app_commands.checks.cooldown(1, 10)
    async def verify(self, interaction: discord.Interaction):
        if not is_verified(interaction.user):
            return await interaction.response.send_message(
                "❌ You must be verified with Double Counter first.",
                ephemeral=True
            )

        embed = discord.Embed(
            title="🔐 Choose Verification Method",
            description="How would you like to prove ownership of your Roblox account?",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc)
        )

        embed.add_field(
            name="📝 Bio Code",
            value=(
                "Add a short code to your Roblox profile's **About** section.\n"
                "Great if you don't want to join a game."
            ),
            inline=False
        )

        embed.add_field(
            name="🎮 Join Game",
            value=(
                f"Join **{RHVERIF_GAME_NAME}** on Roblox and the bot verifies you automatically.\n"
                "Fastest method — no profile editing required!"
            ),
            inline=False
        )

        embed.set_footer(text="Select a method from the dropdown below")

        await interaction.response.send_message(
            embed=embed,
            view=VerifyMethodView(),
            ephemeral=True
        )

    @app_commands.command(
        name="forceunverify",
        description="Force a user to reverify (Head Staff only)"
    )
    @app_commands.describe(user="The user to force unverify")
    @app_commands.checks.cooldown(1, 10)
    async def forceunverify(self, interaction: discord.Interaction, user: discord.Member):
        if not is_head_staff_or_founder(interaction.user):
            return await interaction.response.send_message(
                "❌ You do not have permission. Only Founder and Head Staff can use this.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        try:
            existing = await d1_query(
                "SELECT discord_id, roblox_users FROM users WHERE discord_id = ?",
                [str(user.id)]
            )

            if not existing["results"]:
                return await interaction.followup.send(
                    f"❌ {user.mention} has no record in the database.",
                    ephemeral=True
                )

            roblox_users = json.loads(existing["results"][0]["roblox_users"] or "[]")

            # Remove linked role
            linked_role = interaction.guild.get_role(LINKED_ROLE_ID)
            if linked_role and linked_role in user.roles:
                await user.remove_roles(linked_role, reason=f"Forced unverify by {interaction.user}")

            # Clear their linked accounts
            await d1_query(
                "UPDATE users SET roblox_users = '[]', updated_at = ? WHERE discord_id = ?",
                [datetime.now(timezone.utc).isoformat(), str(user.id)]
            )

            # Remove from pending verifications
            if str(user.id) in pending_verifications:
                del pending_verifications[str(user.id)]

            embed = discord.Embed(
                title="🔓 Force Unverify",
                description=f"{user.mention} has been forced to reverify.",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="Removed By", value=interaction.user.mention, inline=True)
            embed.add_field(
                name="Previously Linked Accounts",
                value="\n".join([f"`{r}`" for r in roblox_users]) if roblox_users else "None",
                inline=False
            )
            embed.add_field(
                name="Next Steps",
                value=f"{user.mention} must now use `/verify` to relink their Roblox account.",
                inline=False
            )

            # DM the user
            try:
                dm_embed = discord.Embed(
                    title="🔓 Verification Reset",
                    description=f"You have been forced to reverify by {interaction.user.mention}.",
                    color=discord.Color.red(),
                    timestamp=datetime.now(timezone.utc)
                )
                dm_embed.add_field(
                    name="Next Steps",
                    value="Please run `/verify <your_roblox_username>` to relink your account.",
                    inline=False
                )
                await user.send(embed=dm_embed)
            except Exception:
                pass

            await interaction.followup.send(embed=embed, ephemeral=True)

            # Log the action
            log_embed = discord.Embed(
                title="🔓 Force Unverify",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc)
            )
            log_embed.add_field(name="User", value=user.mention, inline=True)
            log_embed.add_field(name="By", value=interaction.user.mention, inline=True)
            log_embed.add_field(
                name="Previously Linked Accounts",
                value="\n".join([f"`{r}`" for r in roblox_users]) if roblox_users else "None",
                inline=False
            )
            await send_log(self.bot, log_embed)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @app_commands.command(
        name="manualverif",
        description="Manually link a Roblox account to a user (Head Staff only)"
    )
    @app_commands.describe(user="The Discord user to link", roblox="The Roblox username")
    @app_commands.checks.cooldown(1, 10)
    async def manualverif(self, interaction: discord.Interaction, user: discord.Member, roblox: str):
        if not is_head_staff_or_founder(interaction.user):
            return await interaction.response.send_message(
                "❌ You do not have permission. Only Founder and Head Staff can use this.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        roblox_data = await get_roblox_user_id(roblox)
        if not roblox_data:
            return await interaction.followup.send(
                f"❌ Roblox user `{roblox}` does not exist.",
                ephemeral=True
            )

        roblox_name = roblox_data["name"]
        roblox_id = roblox_data["id"]

        # Check if already linked
        claimed = await d1_query(
            "SELECT discord_id FROM users WHERE roblox_users LIKE ?",
            [f'%"{roblox_name}"%']
        )
        if claimed["results"]:
            owner_id = claimed["results"][0]["discord_id"]
            if str(owner_id) != str(user.id):
                return await interaction.followup.send(
                    f"❌ `{roblox_name}` is already linked to another account.",
                    ephemeral=True
                )

        await finalize_verification(self.bot, interaction, user, roblox_name, roblox_id, manual=True)

        # Log the manual verification
        log_embed = discord.Embed(
            title="🔗 Manual Verification",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc)
        )
        log_embed.add_field(name="Verified By", value=interaction.user.mention, inline=True)
        log_embed.add_field(name="User", value=user.mention, inline=True)
        log_embed.add_field(name="Roblox Username", value=f"`{roblox_name}`", inline=True)
        log_embed.add_field(name="Roblox ID", value=f"`{roblox_id}`", inline=True)
        await send_log(self.bot, log_embed)

    @app_commands.command(
        name="invites",
        description="Check how many people you've invited"
    )
    @app_commands.checks.cooldown(1, 10)
    async def invites(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            # Get verified invites
            verified_result = await d1_query(
                "SELECT COUNT(*) as count FROM users WHERE invited_by = ? AND invited_by IS NOT NULL",
                [str(interaction.user.id)]
            )
            verified_count = verified_result["results"][0]["count"] if verified_result["results"] else 0

            # Get pending invites
            pending_result = await d1_query(
                "SELECT COUNT(*) as count FROM invite_tracking WHERE inviter_id = ? AND used = 0",
                [str(interaction.user.id)]
            )
            pending_count = pending_result["results"][0]["count"] if pending_result["results"] else 0

            embed = discord.Embed(
                title="📊 Your Invite Stats",
                color=discord.Color.blurple(),
                timestamp=datetime.now(timezone.utc)
            )

            embed.add_field(
                name="✅ Verified Invites",
                value=f"**{verified_count}**",
                inline=True
            )
            embed.add_field(
                name="⏳ Pending Invites",
                value=f"**{pending_count}**",
                inline=True
            )
            embed.add_field(
                name="XP per Invite",
                value=f"+**{INVITE_XP_REWARD}** XP",
                inline=True
            )

            if verified_count > 0:
                total_xp = verified_count * INVITE_XP_REWARD
                embed.add_field(
                    name="Total XP from Invites (Base)",
                    value=f"**{total_xp}** XP",
                    inline=True
                )
                embed.add_field(
                    name="💡 Bonus Info",
                    value="XP bonuses (Linked/Booster/Elite Hoster) apply to invite rewards!",
                    inline=False
                )

                # Get recent invitees
                invitees_result = await d1_query(
                    """SELECT u.discord_id, u.created_at 
                       FROM users u 
                       WHERE u.invited_by = ? 
                       ORDER BY u.created_at DESC 
                       LIMIT 5""",
                    [str(interaction.user.id)]
                )

                if invitees_result["results"]:
                    invitee_list = []
                    for row in invitees_result["results"]:
                        member = interaction.guild.get_member(int(row["discord_id"]))
                        name = member.mention if member else f"<@{row['discord_id']}>"
                        time = datetime.fromisoformat(row["created_at"])
                        invitee_list.append(f"{name} — <t:{int(time.timestamp())}:R>")

                    embed.add_field(
                        name="Recent Verified Invitees",
                        value="\n".join(invitee_list),
                        inline=False
                    )

            embed.set_footer(text="Keep inviting to earn more XP!")
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    # ── panelverify REMOVED - Covered by /sendpanels ─────────────────────────

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Track when a user joins via an invite"""
        try:
            # Check if there's an invite tracking entry for this user
            tracking_result = await d1_query(
                "SELECT inviter_id FROM invite_tracking WHERE invitee_id = ? AND used = 0",
                [str(member.id)]
            )

            if tracking_result["results"]:
                # Update the tracking to mark it as used
                now = datetime.now(timezone.utc).isoformat()
                await d1_query(
                    "UPDATE invite_tracking SET used = 1, used_at = ? WHERE invitee_id = ?",
                    [now, str(member.id)]
                )
                print(f"✅ Tracked join for {member.id} (invite already tracked)")

        except Exception as e:
            print(f"Error tracking member join: {e}")

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        """Track when an invite is created"""
        try:
            # Check if invite already exists
            existing = await d1_query(
                "SELECT invite_code FROM invite_codes WHERE invite_code = ?",
                [invite.code]
            )

            if not existing["results"]:
                await d1_query(
                    """INSERT INTO invite_codes 
                    (invite_code, inviter_id, channel_id, created_at, uses)
                    VALUES (?, ?, ?, ?, 0)""",
                    [
                        invite.code,
                        str(invite.inviter.id) if invite.inviter else None,
                        str(invite.channel.id),
                        datetime.now(timezone.utc).isoformat()
                    ]
                )
                print(f"✅ Tracked invite creation: {invite.code}")
        except Exception as e:
            print(f"Error tracking invite creation: {e}")


async def setup(bot: commands.Bot):
    """Setup function for the cog"""
    await bot.add_cog(Verify(bot))
    print("✅ Verify cog setup complete")