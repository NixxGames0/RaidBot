import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import json
from datetime import datetime, timezone

# Import shared functions from bot.py
from bot import (
    d1_query,
    GUILD_ID,
    FOUNDER_ROLE_ID,
    HEAD_STAFF_ROLE_ID,
    MOD_ROLE_ID,
    TRIAL_MOD_ROLE_ID,
    BLACKLIST_ROLE_ID,
    LINKED_ROLE_ID,
    LOG_CHANNEL_ID,
    is_staff,
    is_head_staff_or_founder,
    is_linked,
    is_blacklisted
)

# ── Panel Channel IDs ──────────────────────────────────────────────────────
PS_PANEL_CHANNEL_ID = 1535598731007623249
VERIFY_PANEL_CHANNEL_ID = 1535662578993340507
RAID_PANEL_CHANNEL_ID = 1535562445286801438
TICKET_PANEL_CHANNEL_ID = 1535906243435040788
SHOP_PANEL_CHANNEL_ID = 1535562484788891658
RAID_GATE_CHANNEL_ID = 1535561934534082610
RULES_CHANNEL_ID = 1535979621193883648
PVP_PANEL_CHANNEL_ID = 1536655138754789386

# ── Import custom role panel builder ─────────────────────────────────────
from cogs.custom_roles import CUSTOM_ROLE_CHANNEL_ID, build_custom_role_panel

# ── Staff Roles ──────────────────────────────────────────────────────────────
STAFF_ROLES = {FOUNDER_ROLE_ID, HEAD_STAFF_ROLE_ID, MOD_ROLE_ID, TRIAL_MOD_ROLE_ID}


# ── Logging Helper ───────────────────────────────────────────────────────────
async def send_log(bot: commands.Bot, embed: discord.Embed):
    """Send a log message to the log channel"""
    channel = bot.get_channel(LOG_CHANNEL_ID)
    if channel:
        try:
            await channel.send(embed=embed)
        except Exception as e:
            print(f"Error sending log: {e}")


# ── PS Panel View ────────────────────────────────────────────────────────────
class PSPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Get Raid Code",
        style=discord.ButtonStyle.success,
        emoji="🎮",
        custom_id="ps_get_code"
    )
    async def get_code(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.NotFound:
            return

        member = interaction.user
        bot = interaction.client

        # Blacklist check
        if is_blacklisted(member):
            return await interaction.followup.send(
                "🚫 You are blacklisted and cannot claim a PS code.",
                ephemeral=True
            )

        # Linked check
        if not is_linked(member):
            return await interaction.followup.send(
                "❌ You need to link your Roblox account with `/verify` before claiming a code.",
                ephemeral=True
            )

        try:
            now = datetime.now(timezone.utc).isoformat()

            # Check if user already has a code
            user_row = await d1_query(
                "SELECT ps_codes FROM users WHERE discord_id = ?",
                [str(member.id)]
            )
            if user_row["results"]:
                current_codes = json.loads(user_row["results"][0]["ps_codes"] or "[]")
                if current_codes:
                    return await interaction.followup.send(
                        f"⚠️ You already have an active code: `{current_codes[0]}`\n"
                        f"Release it first before claiming a new one.",
                        ephemeral=True
                    )
            else:
                return await interaction.followup.send(
                    "❌ You need to verify first with `/verify`.",
                    ephemeral=True
                )

            # Get an available code
            available = await d1_query(
                "SELECT code FROM codes WHERE is_using = 0 ORDER BY RANDOM() LIMIT 1"
            )
            if not available["results"]:
                return await interaction.followup.send(
                    "😔 No codes are available right now. Check back later!",
                    ephemeral=True
                )

            code = available["results"][0]["code"]

            # Mark code as in use
            await d1_query(
                "UPDATE codes SET is_using = 1, held_by = ?, claimed_at = ? WHERE code = ?",
                [str(member.id), now, code]
            )

            # Add code to user's ps_codes
            if user_row["results"]:
                current_codes = json.loads(user_row["results"][0]["ps_codes"] or "[]")
                current_codes.append(code)
                await d1_query(
                    "UPDATE users SET ps_codes = ?, updated_at = ? WHERE discord_id = ?",
                    [json.dumps(current_codes), now, str(member.id)]
                )

            # Send ephemeral code message
            embed = discord.Embed(
                title="✅ Raid PS Code",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.description = f"### `{code}`"
            embed.add_field(
                name="\u200b",
                value="🕐 This code is assigned to you for **12 hours**.\n"
                      "💡 Click **Release My Code** when you're done to return it early!",
                inline=False
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

            # Log it
            log_embed = discord.Embed(
                title="🎮 PS Code Claimed",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc)
            )
            log_embed.add_field(name="User", value=member.mention, inline=True)
            log_embed.add_field(name="Code", value=f"`{code}`", inline=True)
            log_embed.set_footer(text=f"User ID: {member.id}")
            await send_log(bot, log_embed)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @discord.ui.button(
        label="Release My Code",
        style=discord.ButtonStyle.danger,
        emoji="🔓",
        custom_id="ps_release_code"
    )
    async def release_code(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        member = interaction.user
        bot = interaction.client

        try:
            now = datetime.now(timezone.utc).isoformat()

            # Get user's current codes
            user_row = await d1_query(
                "SELECT ps_codes FROM users WHERE discord_id = ?",
                [str(member.id)]
            )
            if not user_row["results"]:
                return await interaction.followup.send(
                    "❌ You have no active codes to release.",
                    ephemeral=True
                )

            current_codes = json.loads(user_row["results"][0]["ps_codes"] or "[]")
            if not current_codes:
                return await interaction.followup.send(
                    "❌ You have no active codes to release.",
                    ephemeral=True
                )

            released = current_codes.pop(0)

            # Free the code
            await d1_query(
                "UPDATE codes SET is_using = 0, held_by = NULL, claimed_at = NULL WHERE code = ?",
                [released]
            )

            # Update user
            await d1_query(
                "UPDATE users SET ps_codes = ?, updated_at = ? WHERE discord_id = ?",
                [json.dumps(current_codes), now, str(member.id)]
            )

            await interaction.followup.send(
                f"✅ Code `{released}` has been released. Thanks!",
                ephemeral=True
            )

            # Log it
            log_embed = discord.Embed(
                title="🔓 PS Code Released",
                color=discord.Color.orange(),
                timestamp=datetime.now(timezone.utc)
            )
            log_embed.add_field(name="User", value=member.mention, inline=True)
            log_embed.add_field(name="Code", value=f"`{released}`", inline=True)
            log_embed.set_footer(text=f"User ID: {member.id}")
            await send_log(bot, log_embed)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @discord.ui.button(
        label="Reveal My Code",
        style=discord.ButtonStyle.secondary,
        emoji="🔍",
        custom_id="ps_reveal_code"
    )
    async def reveal_code(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        member = interaction.user

        try:
            user_row = await d1_query(
                "SELECT ps_codes FROM users WHERE discord_id = ?",
                [str(member.id)]
            )

            if not user_row["results"]:
                return await interaction.followup.send(
                    "❌ You don't have an active PS code.",
                    ephemeral=True
                )

            current_codes = json.loads(user_row["results"][0]["ps_codes"] or "[]")
            if not current_codes:
                return await interaction.followup.send(
                    "❌ You don't have an active PS code. Claim one with **Get Raid Code**!",
                    ephemeral=True
                )

            code = current_codes[0]

            embed = discord.Embed(
                title="🔍 Your Active PS Code",
                color=discord.Color.blurple(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.description = f"### `{code}`"
            embed.add_field(
                name="\u200b",
                value="🕐 This code is still active and assigned to you.\n"
                      "🔓 Click **Release My Code** when you're done!",
                inline=False
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


# ── Panel Cog ──────────────────────────────────────────────────────────────────
class Panel(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Register persistent views on startup
        self.bot.loop.create_task(self.register_persistent_views())

    async def register_persistent_views(self):
        """Register persistent views owned by this cog only.
        Raid, Ticket, Shop, CustomRole views are registered by their own cogs."""
        try:
            await self.bot.wait_until_ready()

            # Register PS Panel View (owned by panel.py)
            self.bot.add_view(PSPanelView())
            print("✅ Registered persistent PS Panel view")

            # Register Verify Panel View (owned by panel.py)
            try:
                from cogs.verify import VerifyPanelView
                self.bot.add_view(VerifyPanelView())
                print("✅ Registered persistent Verify Panel view")
            except Exception as e:
                print(f"Could not register Verify Panel view: {e}")

            # NOTE: Raid, Ticket, Shop, CustomRole views are registered
            # by their respective cogs — do NOT add_view them here.

            try:
                from cogs.custom_roles import CustomRolePanelView
                custom_view = CustomRolePanelView()
                self.bot.add_view(custom_view)
                print("✅ Registered persistent Custom Role Panel view")
            except Exception as e:
                print(f"Could not register Custom Role Panel view: {e}")

            print("✅ All persistent views registered")

        except Exception as e:
            print(f"Error registering persistent views: {e}")

    # ── /sendpanels ──────────────────────────────────────────────────────────
    @app_commands.command(
        name="sendpanels",
        description="Clear and resend all panels (Staff only)"
    )
    @app_commands.checks.cooldown(1, 60)
    async def sendpanels(self, interaction: discord.Interaction):
        """Clear all panel channels and resend the panels"""
        # Staff only - Trial Mod+
        if not is_staff(interaction.user):
            return await interaction.response.send_message(
                "❌ You do not have permission to use this command.",
                ephemeral=True
            )

        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except Exception:
            return  # Interaction expired (Discord 3s timeout) - user can retry

        try:
            # Get all the channels
            ps_channel = self.bot.get_channel(PS_PANEL_CHANNEL_ID)
            verify_channel = self.bot.get_channel(VERIFY_PANEL_CHANNEL_ID)
            raid_channel = self.bot.get_channel(RAID_PANEL_CHANNEL_ID)
            ticket_channel = self.bot.get_channel(TICKET_PANEL_CHANNEL_ID)
            shop_channel = self.bot.get_channel(SHOP_PANEL_CHANNEL_ID)
            gate_channel = self.bot.get_channel(RAID_GATE_CHANNEL_ID)
            rules_channel = self.bot.get_channel(RULES_CHANNEL_ID)
            custom_channel = self.bot.get_channel(CUSTOM_ROLE_CHANNEL_ID)
            pvp_channel = self.bot.get_channel(PVP_PANEL_CHANNEL_ID)

            missing = []
            if not ps_channel:
                missing.append("PS Panel")
            if not verify_channel:
                missing.append("Verify Panel")
            if not raid_channel:
                missing.append("Raid Panel")
            if not ticket_channel:
                missing.append("Ticket Panel")
            if not shop_channel:
                missing.append("Shop Panel")
            if not gate_channel:
                missing.append("Raid Gate")
            if not rules_channel:
                missing.append("Rules Channel")
            if not custom_channel:
                missing.append("Custom Role Panel")
            if not pvp_channel:
                missing.append("PvP Panel")

            if missing:
                return await interaction.followup.send(
                    f"❌ Could not find the following channels: {', '.join(missing)}",
                    ephemeral=True
                )

            # Clear all messages from each channel (skip pinned messages)
            await ps_channel.purge(limit=1000, check=lambda m: not m.pinned)
            await verify_channel.purge(limit=1000, check=lambda m: not m.pinned)
            await raid_channel.purge(limit=1000, check=lambda m: not m.pinned)
            await ticket_channel.purge(limit=1000, check=lambda m: not m.pinned)
            await shop_channel.purge(limit=1000, check=lambda m: not m.pinned)
            await gate_channel.purge(limit=1000, check=lambda m: not m.pinned)
            await rules_channel.purge(limit=1000, check=lambda m: not m.pinned)
            await custom_channel.purge(limit=1000, check=lambda m: not m.pinned)
            await pvp_channel.purge(limit=1000, check=lambda m: not m.pinned)

            # ── Send Rules Panel ──────────────────────────────────────────────
            rules_embed = discord.Embed(
                title="📜 **Raid Hub Server Rules**",
                description=(
                    "Welcome to **Raid Hub**! Please read these rules carefully.\n"
                    "Violating any rule may result in **warnings, mutes, or bans**.\n\n"
                    f"**Last Updated:** <t:{int(datetime.now(timezone.utc).timestamp())}:F>"
                ),
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc)
            )

            if interaction.guild.icon:
                rules_embed.set_thumbnail(url=interaction.guild.icon.url)
            rules_embed.set_footer(
                text="Raid Hub • Read all rules before participating",
                icon_url=interaction.guild.icon.url if interaction.guild.icon else None
            )

            rules_embed.add_field(
                name="📌 **1. General Conduct**",
                value=(
                    "• **Be respectful** to all members. No toxicity, harassment, or bullying.\n"
                    "• **No discrimination** based on race, gender, sexuality, religion, or nationality.\n"
                    "• **No inappropriate content** (NSFW, gore, or offensive material).\n"
                    "• **No spamming** in any channel. Use appropriate channels for topics.\n"
                    "• **English only** in all public channels for moderation purposes.\n"
                    "• **No advertising** other servers, products, or services without permission.\n"
                    "• **Be respectful to mods and staff** - they are here to help, not to be argued with."
                ),
                inline=False
            )

            rules_embed.add_field(
                name="🗣️ **2. Language Rules**",
                value=(
                    "• **Swearing is allowed** but keep it moderate and respectful.\n"
                    "• **No severe words** - slurs, extreme profanity, or hate speech.\n"
                    "• **No targeting** others with offensive language.\n"
                    "• **Staff discretion** applies - if it crosses the line, it's not allowed.\n"
                    "• **Remember:** Keep it fun for everyone!"
                ),
                inline=False
            )

            rules_embed.add_field(
                name="⚔️ **3. Raid Rules**",
                value=(
                    "• **All accounts must be registered** to join raids. You must be verified and linked.\n"
                    "• **PS Codes** are for raid use only. Do not share them outside.\n"
                    "• **Sharing raid codes** with anyone outside the server is strictly prohibited.\n"
                    "• **Follow the host's instructions** during the raid.\n"
                    "• **Don't join raids** if you're not going to participate.\n"
                    "• **Releasing your PS code** when you're done allows others to use it.\n"
                    "• **No PvP is allowed** during raids - focus on the objective, not fighting each other."
                ),
                inline=False
            )

            rules_embed.add_field(
                name="🏠 **4. Hosting Rules**",
                value=(
                    "• **Hosters must earn 100 weekly points** to maintain their role.\n"
                    "• **Warnings are sent** when you're below 100 points with <24h left.\n"
                    "• **Elite Hoster** (Top 3) gets **25% XP bonus** on all raids.\n"
                    "• **Suspicious raid speeds** (under 1.5 min/wave) will be flagged for review.\n"
                    "• **Release your PS code** when you're done so others can use it."
                ),
                inline=False
            )

            rules_embed.add_field(
                name="🛒 **5. Shop Rules**",
                value=(
                    "• **Earn points** by hosting raids (1 point per wave).\n"
                    "• **Spend points** in the shop on various items.\n"
                    "• **Balance** = points you can spend.\n"
                    "• **Total Earned** = points you've earned (never decreases).\n"
                    "• **Leaderboard** is based on Total Earned.\n"
                    "• **No abusing** the shop system."
                ),
                inline=False
            )

            rules_embed.add_field(
                name="⭐ **6. XP System**",
                value=(
                    "• **Earn XP** from: messages, voice chat, reactions, and raids.\n"
                    "• **Bonuses stack!**\n"
                    "  ─ **Linked Account**: +10% XP\n"
                    "  ─ **Server Booster**: +25% XP\n"
                    "  ─ **Elite Hoster**: +25% XP\n"
                    "  ─ **First message of the day**: 100 XP\n"
                    "  ─ **Raid participation**: 50 XP\n"
                    "• **Level up** to unlock new roles and rewards!\n"
                    "• **Check your rank** with `/rank`"
                ),
                inline=False
            )

            rules_embed.add_field(
                name="🔐 **7. Verification**",
                value=(
                    "• **Verify with Double Counter** first.\n"
                    "• **Link your Roblox account** using `/verify <username>`.\n"
                    "• **Add the verification code** to your Roblox profile.\n"
                    "• **Click 'I've Added It'** to complete verification.\n"
                    "• **Need help?** Open a ticket in <#1535906243435040788>."
                ),
                inline=False
            )

            rules_embed.add_field(
                name="🎫 **8. Tickets**",
                value=(
                    "• **Open a ticket** for support issues.\n"
                    "• **Ticket types:** Verification, Blacklist Appeal, Other.\n"
                    "• **Only open one ticket** at a time.\n"
                    "• **Staff will assist you** as soon as possible.\n"
                    "• **Close your ticket** when the issue is resolved."
                ),
                inline=False
            )

            rules_embed.add_field(
                name="⚖️ **9. Moderation**",
                value=(
                    "• **Staff decisions** are final.\n"
                    "• **Respect staff** and their authority.\n"
                    "• **Appeals** can be made via tickets.\n"
                    "• **No arguing** with staff in public channels.\n"
                    "• **Reports** should be made via tickets, not DMs."
                ),
                inline=False
            )

            rules_embed.add_field(
                name="🔒 **10. Privacy & Safety**",
                value=(
                    "• **Do not leak personal information** of any member.\n"
                    "• **No sharing** of DMs, real names, locations, or contact details.\n"
                    "• **Respect everyone's privacy** - what happens in DMs stays in DMs.\n"
                    "• **Report privacy violations** to staff immediately."
                ),
                inline=False
            )

            rules_embed.add_field(
                name="⚠️ **11. Consequences**",
                value=(
                    "**Violations will result in:**\n"
                    "• **Warning** - Verbal/formal warning\n"
                    "• **Mute** - Temporary loss of speaking privileges\n"
                    "• **Kick** - Removal from the server\n"
                    "• **Blacklist** - Permanent ban from all activities\n"
                    "• **Permaban** - Permanent removal from the server\n\n"
                    "**Severe violations** (hate speech, harassment, doxxing) may result in **immediate blacklist/ban**."
                ),
                inline=False
            )

            rules_embed.add_field(
                name="📞 **12. Need Help?**",
                value=(
                    "• **Staff Contact:** Open a ticket in <#1535906243435040788>\n"
                    "• **Report a Player:** Open a ticket with evidence\n"
                    "• **Blacklist Appeal:** Open a 'Blacklist Appeal' ticket\n\n"
                    "**Thank you for being part of Raid Hub!** 🎮"
                ),
                inline=False
            )

            await rules_channel.send(embed=rules_embed)

            # ── Send PS Panel ─────────────────────────────────────────────────
            ps_embed = discord.Embed(
                title="🎮 Private Server Codes",
                description="Click a button below to get a PS code or release your current one.",
                color=discord.Color.dark_embed(),
            )
            ps_embed.add_field(
                name="Rules",
                value=(
                    "• You can only have **1 active code** at a time\n"
                    "• Click 🔓 **Release My Code** when done to return it early\n"
                    "• Codes are automatically released after **12 hours**"
                ),
                inline=False
            )
            ps_view = PSPanelView()
            await ps_channel.send(embed=ps_embed, view=ps_view)

            # ── Send Verify Panel ────────────────────────────────────────────
            verify_embed = discord.Embed(
                title="🔐 Verification Instructions",
                description="Follow the steps below to verify your account and link your Roblox profile!",
                color=discord.Color.blue(),
                timestamp=datetime.now(timezone.utc)
            )

            verify_embed.add_field(
                name="📌 Step 1: Double Counter Verification",
                value=(
                    "You must first verify your Discord account using **Double Counter**.\n"
                    "This ensures your account is legitimate and not a bot."
                ),
                inline=False
            )

            verify_embed.add_field(
                name="📌 Step 2: Start Verification",
                value=(
                    "Click the **Start Verification** button below or use the command:\n"
                    "`/verify <your_roblox_username>`\n\n"
                    "**Example:** `/verify Nixx`"
                ),
                inline=False
            )

            verify_embed.add_field(
                name="📌 Step 3: Add Code to Roblox Profile",
                value=(
                    "1. Go to your Roblox profile settings\n"
                    "2. Edit your **About** section\n"
                    "3. Add the verification code provided by the bot\n"
                    "4. Save your profile\n\n"
                    "💡 **Tip:** If the code gets censored, click **🔄 Regenerate Code** for a new one!"
                ),
                inline=False
            )

            verify_embed.add_field(
                name="📌 Step 4: Confirm Verification",
                value=(
                    "After adding the code to your profile, click the **✅ I've Added It** button.\n"
                    "The bot will check if the code is in your profile and complete the verification."
                ),
                inline=False
            )

            verify_embed.add_field(
                name="🔒 Why This Is Required",
                value=(
                    "This ensures that you actually own the Roblox account you're linking.\n"
                    "This prevents impersonation and keeps the community safe!"
                ),
                inline=False
            )

            verify_embed.add_field(
                name="🎁 Rewards for Verifying",
                value=(
                    "• **Access to all channels**\n"
                    "• **Earn XP** by chatting and hosting raids\n"
                    "• **Level up** and unlock new roles\n"
                    "• **Invite friends** and earn bonus XP!\n"
                    "• **Host raids** and earn hoster points"
                ),
                inline=False
            )

            verify_embed.add_field(
                name="❓ Need Help?",
                value=(
                    "If you're having trouble verifying, please contact a staff member.\n"
                    "Make sure your Roblox username is spelled correctly!"
                ),
                inline=False
            )

            if interaction.guild.icon:
                verify_embed.set_footer(
                    text="Raid Hub • Verification System",
                    icon_url=interaction.guild.icon.url
                )
            else:
                verify_embed.set_footer(text="Raid Hub • Verification System")

            await verify_channel.send(embed=verify_embed)

            from cogs.verify import VerifyPanelView
            verify_view = VerifyPanelView()
            await verify_channel.send(
                "**Click the button below to start verification!**",
                view=verify_view
            )

            # ── Send Raid Panel ──────────────────────────────────────────────
            raid_embed = discord.Embed(
                title="⚔️ Raid Hub",
                description="Click the button below to start a raid.\nMake sure you have an active PS code before starting.",
                color=discord.Color.dark_red(),
            )
            from cogs.raid import StartRaidPanelView
            raid_view = StartRaidPanelView()
            await raid_channel.send(embed=raid_embed, view=raid_view)

            # ── Send Ticket Panel ────────────────────────────────────────────
            ticket_embed = discord.Embed(
                title="🎫 Support Tickets",
                description=(
                    "Select a ticket type below to create a support ticket.\n\n"
                    "**Verification** - Issues with verifying your Roblox account\n"
                    "**Blacklist Appeal** - Appeal a blacklist or ban\n"
                    "**Other** - Any other questions or concerns"
                ),
                color=discord.Color.blue(),
                timestamp=datetime.now(timezone.utc)
            )
            ticket_embed.set_footer(text="Only open one ticket at a time")

            from cogs.ticket import TicketPanelView
            ticket_view = TicketPanelView()
            await ticket_channel.send(embed=ticket_embed, view=ticket_view)

            # ── Send Shop Panel ──────────────────────────────────────────────
            from cogs.shop import ShopPanelView, get_shop_items
            shop_view = ShopPanelView()

            items = await get_shop_items()
            items_text = ""
            if items:
                for item in items:
                    items_text += f"• **{item['name']}** - {item['cost']} points\n"
            else:
                items_text = "*No items available yet.*"

            shop_embed = discord.Embed(
                title="🛒 Shop",
                description="Click the button below to browse and purchase items.",
                color=discord.Color.gold(),
                timestamp=datetime.now(timezone.utc)
            )
            shop_embed.add_field(
                name="📋 Available Items",
                value=items_text,
                inline=False
            )
            shop_embed.add_field(
                name="💰 How to Earn Points",
                value=(
                    "• Host raids to earn hoster points\n"
                    "• Higher waves = more points!\n"
                    "• Check your points with `/points`\n"
                    "• Your balance is your **lifetime** points"
                ),
                inline=False
            )
            shop_embed.set_footer(text="Points spent here come from your lifetime balance")

            await shop_channel.send(embed=shop_embed, view=shop_view)

            # ── Send Custom Role Panel ──────────────────────────────────────
            custom_embed, custom_view = build_custom_role_panel()
            await custom_channel.send(embed=custom_embed, view=custom_view)

            # ── Check for Active Raid ────────────────────────────────────────
            # Import raid functions only when needed
            from cogs.raid import get_raid, get_active_raid_from_db, RaidControlView, JoinRaidView, build_gate_embed

            # Try to get active raid from memory first
            active_raid = get_raid(interaction.guild_id)

            # If no raid in memory, check database
            if not active_raid:
                active_raid = await get_active_raid_from_db(interaction.guild_id)

            if active_raid:
                # Send control panel to raid channel
                ctrl_embed = discord.Embed(
                    title="⚔️ Raid Controls",
                    description="Raid is **ACTIVE**. Use the buttons below to manage it.",
                    color=discord.Color.green(),
                    timestamp=datetime.now(timezone.utc)
                )
                ctrl_embed.add_field(name="Raid ID", value=f"`{active_raid['raid_id']}`", inline=True)
                ctrl_embed.add_field(name="Host", value=f"<@{active_raid['host_id']}>", inline=True)
                ctrl_embed.add_field(name="Code", value=f"`{active_raid['code']}`", inline=True)
                ctrl_embed.add_field(name="Wave", value=f"{active_raid['wave']}/20", inline=True)

                raid_control_view = RaidControlView()
                await raid_channel.send(embed=ctrl_embed, view=raid_control_view)

                # Send gate status
                gate_embed = await build_gate_embed(interaction.guild, active_raid)
                join_view = JoinRaidView()
                await gate_channel.send(embed=gate_embed, view=join_view)

                # Log that the control panel was re-sent
                log_embed = discord.Embed(
                    title="⚔️ Active Raid Control Panel Re-sent",
                    description=f"Control panel for raid `{active_raid['raid_id']}` has been re-sent.",
                    color=discord.Color.green(),
                    timestamp=datetime.now(timezone.utc)
                )
                await send_log(self.bot, log_embed)

            # ── Send PvP Panel ──────────────────────────────────────────────
            pvp_cog = self.bot.cogs.get("PvPCog")
            if pvp_cog:
                pvp_embed = pvp_cog._build_panel_embed(interaction.guild_id)
                from cogs.pvp import PvPPanelView
                pvp_view = PvPPanelView()
                pvp_msg = await pvp_channel.send(embed=pvp_embed, view=pvp_view)
                pvp_cog._panel_msg_id = pvp_msg.id
                try:
                    await d1_query(
                        "INSERT OR REPLACE INTO bot_meta (key, value) VALUES ('pvp_panel_msg_id', ?)",
                        [str(pvp_msg.id)]
                    )
                except Exception:
                    pass

            # ── Log the action ──────────────────────────────────────────────
            log_embed = discord.Embed(
                title="🔄 Panels Reset",
                description=f"All panels have been reset by {interaction.user.mention}",
                color=discord.Color.blue(),
                timestamp=datetime.now(timezone.utc)
            )
            log_embed.add_field(name="Rules Panel", value=f"<#{RULES_CHANNEL_ID}>", inline=True)
            log_embed.add_field(name="PS Panel", value=f"<#{PS_PANEL_CHANNEL_ID}>", inline=True)
            log_embed.add_field(name="Verify Panel", value=f"<#{VERIFY_PANEL_CHANNEL_ID}>", inline=True)
            log_embed.add_field(name="Raid Panel", value=f"<#{RAID_PANEL_CHANNEL_ID}>", inline=True)
            log_embed.add_field(name="Ticket Panel", value=f"<#{TICKET_PANEL_CHANNEL_ID}>", inline=True)
            log_embed.add_field(name="Shop Panel", value=f"<#{SHOP_PANEL_CHANNEL_ID}>", inline=True)
            log_embed.add_field(name="Custom Role Panel", value=f"<#{CUSTOM_ROLE_CHANNEL_ID}>", inline=True)
            log_embed.add_field(name="PvP Panel", value=f"<#{PVP_PANEL_CHANNEL_ID}>", inline=True)
            if active_raid:
                log_embed.add_field(name="Active Raid", value=f"`{active_raid['raid_id']}`", inline=True)
            await send_log(self.bot, log_embed)

            await interaction.followup.send(
                "✅ All panels have been cleared and resent successfully!",
                ephemeral=True
            )

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


async def setup(bot: commands.Bot):
    """Setup function for the cog"""
    await bot.add_cog(Panel(bot))
    print("✅ Panel cog setup complete")