import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import io
import time
import uuid
from datetime import datetime, timezone, timedelta

from cogs.transcript import generate_transcript

# Import shared functions from bot.py
from bot import (
    d1_query,
    GUILD_ID,
    FOUNDER_ROLE_ID,
    HEAD_STAFF_ROLE_ID,
    MOD_ROLE_ID,
    TRIAL_MOD_ROLE_ID,
    LOG_CHANNELS,
    is_staff,
    is_mod_or_higher,
    is_head_staff_or_founder
)

# ── Channel IDs ──────────────────────────────────────────────────────────────
TICKET_PANEL_CHANNEL_ID = 1535906243435040788
TICKET_CATEGORY_ID = 1535905899854430222

# ── Staff Roles ──────────────────────────────────────────────────────────────
STAFF_ROLES = {
    FOUNDER_ROLE_ID,
    HEAD_STAFF_ROLE_ID,
    MOD_ROLE_ID,
    TRIAL_MOD_ROLE_ID
}

# ── Roles to ping in order ──────────────────────────────────────────────────
PING_ROLES = [
    HEAD_STAFF_ROLE_ID,
    MOD_ROLE_ID,
    TRIAL_MOD_ROLE_ID,
]


# ── Logging Helper ───────────────────────────────────────────────────────────
async def send_log(bot: commands.Bot, embed: discord.Embed):
    """Send a log message to the ticket log channel"""
    channel = bot.get_channel(LOG_CHANNELS.get("tickets", 0))
    if channel:
        try:
            await channel.send(embed=embed)
        except Exception as e:
            print(f"Error sending log: {e}")


# ── Views ────────────────────────────────────────────────────────────────────
class TicketControlView(discord.ui.View):
    def __init__(self, ticket_id: str, user_id: int):
        super().__init__(timeout=None)
        self.ticket_id = ticket_id
        self.user_id = user_id

    @discord.ui.button(
        label="🔒 Close Ticket",
        style=discord.ButtonStyle.danger,
        emoji="🔒",
        custom_id="ticket_close"
    )
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id and not is_staff(interaction.user):
            try:
                await interaction.response.send_message("❌ Only the ticket owner or staff can close this ticket.", ephemeral=True)
            except discord.HTTPException:
                pass
            return

        try:
            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException:
            return

        # Update ticket status
        now = datetime.now(timezone.utc).isoformat()
        await d1_query(
            "UPDATE tickets SET status = 'closed', closed_at = ? WHERE ticket_id = ?",
            [now, self.ticket_id]
        )

        # Update the embed
        embed = interaction.message.embeds[0]
        embed.color = discord.Color.red()
        embed.add_field(
            name="🔒 Closed",
            value=f"Closed by {interaction.user.mention}",
            inline=False
        )

        # Disable buttons
        for child in self.children:
            child.disabled = True

        await interaction.message.edit(embed=embed, view=self)

        # Generate transcript BEFORE deleting the channel
        html_bytes = None
        try:
            html_bytes = await generate_transcript(
                interaction.channel, interaction.guild, self.ticket_id
            )
        except Exception as e:
            print(f"[Transcript] Error generating: {e}")

        # Delete the channel after 5 seconds
        try:
            await interaction.followup.send(
                "🔒 Ticket closed. Channel will be deleted in 5 seconds...",
                ephemeral=True
            )
            await asyncio.sleep(5)
            await interaction.channel.delete(
                reason=f"Ticket {self.ticket_id} closed by {interaction.user}"
            )
        except Exception as e:
            await interaction.followup.send(f"Error deleting channel: {e}", ephemeral=True)

        # Upload transcript file and get its URL
        log_ch = interaction.client.get_channel(LOG_CHANNELS.get("tickets", 0))
        transcript_url = None
        if html_bytes and log_ch:
            try:
                f = discord.File(
                    io.BytesIO(html_bytes),
                    filename=f"ticket-{self.ticket_id}.html"
                )
                file_msg = await log_ch.send(file=f)
                if file_msg.attachments:
                    transcript_url = file_msg.attachments[0].url
            except Exception as e:
                print(f"[Transcript] Error uploading: {e}")

        # Log the ticket closure with View Transcript button
        log_embed = discord.Embed(
            title="🔒 Ticket Closed",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc)
        )
        log_embed.add_field(name="Ticket ID", value=self.ticket_id, inline=True)
        log_embed.add_field(name="Closed By", value=interaction.user.mention, inline=True)
        log_embed.add_field(name="User", value=f"<@{self.user_id}>", inline=True)
        log_view = None
        if transcript_url:
            log_view = discord.ui.View()
            log_view.add_item(discord.ui.Button(
                label="View Transcript",
                style=discord.ButtonStyle.link,
                url=transcript_url,
                emoji="📜"
            ))
        if log_ch:
            try:
                await log_ch.send(embed=log_embed, view=log_view)
            except Exception as e:
                print(f"[Ticket] Error sending log embed: {e}")

        # DM the ticket opener with the transcript
        opener = interaction.client.get_user(self.user_id)
        if opener and html_bytes:
            try:
                dm_embed = discord.Embed(
                    title="🔒 Your ticket has been closed",
                    description=(
                        f"Your support ticket **{self.ticket_id}** has been closed by "
                        f"{interaction.user.mention}.\n\n"
                        "Your transcript is attached below."
                    ),
                    color=discord.Color.red(),
                    timestamp=datetime.now(timezone.utc)
                )
                dm_embed.set_footer(text=interaction.guild.name)
                dm_file = discord.File(
                    io.BytesIO(html_bytes),
                    filename=f"ticket-{self.ticket_id}.html"
                )
                dm_view = None
                if transcript_url:
                    dm_view = discord.ui.View()
                    dm_view.add_item(discord.ui.Button(
                        label="View Transcript",
                        style=discord.ButtonStyle.link,
                        url=transcript_url,
                        emoji="📜"
                    ))
                await opener.send(embed=dm_embed, file=dm_file, view=dm_view)
            except discord.HTTPException:
                pass

    @discord.ui.button(
        label="📋 Claim Ticket",
        style=discord.ButtonStyle.success,
        emoji="📋",
        custom_id="ticket_claim"
    )
    async def claim_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            try:
                await interaction.response.send_message("❌ Only staff can claim tickets.", ephemeral=True)
            except discord.HTTPException:
                pass
            return

        try:
            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException:
            return

        # Check if ticket is already claimed
        result = await d1_query(
            "SELECT claimed_by FROM tickets WHERE ticket_id = ?",
            [self.ticket_id]
        )

        if result["results"] and result["results"][0]["claimed_by"]:
            return await interaction.followup.send(
                f"❌ This ticket has already been claimed by <@{result['results'][0]['claimed_by']}>.",
                ephemeral=True
            )

        # Update ticket
        await d1_query(
            "UPDATE tickets SET claimed_by = ? WHERE ticket_id = ?",
            [str(interaction.user.id), self.ticket_id]
        )

        # Update the embed
        embed = interaction.message.embeds[0]

        # Find and update the claimed by field
        new_embed = discord.Embed(
            title=embed.title,
            description=embed.description + f"\n\n**Claimed By:** {interaction.user.mention}",
            color=discord.Color.green(),
            timestamp=embed.timestamp
        )
        if embed.footer:
            new_embed.set_footer(text=embed.footer.text)

        # Disable claim button
        for child in self.children:
            if child.custom_id == "ticket_claim":
                child.disabled = True

        await interaction.message.edit(embed=new_embed, view=self)

        await interaction.followup.send(
            f"✅ You have claimed this ticket! You are now handling it.",
            ephemeral=True
        )

        # Log the claim
        log_embed = discord.Embed(
            title="📋 Ticket Claimed",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc)
        )
        log_embed.add_field(name="Ticket ID", value=self.ticket_id, inline=True)
        log_embed.add_field(name="Claimed By", value=interaction.user.mention, inline=True)
        log_embed.add_field(name="User", value=f"<@{self.user_id}>", inline=True)
        await send_log(interaction.client, log_embed)

    @discord.ui.button(
        label="📝 Add Note",
        style=discord.ButtonStyle.secondary,
        emoji="📝",
        custom_id="ticket_note"
    )
    async def add_note(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message(
                "❌ Only staff can add notes.",
                ephemeral=True
            )

        # Create and send the modal directly
        modal = TicketNoteModal(ticket_id=self.ticket_id)
        await interaction.response.send_modal(modal)


class TicketNoteModal(discord.ui.Modal, title="Add Staff Note"):
    def __init__(self, ticket_id: str):
        super().__init__()
        self.ticket_id_value = ticket_id

    note = discord.ui.TextInput(
        label="Note",
        placeholder="Enter your note...",
        required=True,
        max_length=500,
        style=discord.TextStyle.paragraph
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        # Get the ticket channel
        result = await d1_query(
            "SELECT channel_id FROM tickets WHERE ticket_id = ?",
            [self.ticket_id_value]
        )

        if not result["results"]:
            return await interaction.followup.send(
                "❌ Ticket not found.",
                ephemeral=True
            )

        channel_id = int(result["results"][0]["channel_id"])
        channel = interaction.guild.get_channel(channel_id)

        if not channel:
            return await interaction.followup.send(
                "❌ Ticket channel not found.",
                ephemeral=True
            )

        # Send the note in the ticket channel
        embed = discord.Embed(
            title="📝 Staff Note",
            description=self.note.value,
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_footer(text=f"Added by {interaction.user.display_name}")

        await channel.send(embed=embed)

        await interaction.followup.send(
            "✅ Note added to ticket.",
            ephemeral=True
        )


class TicketModal(discord.ui.Modal, title="Create Ticket"):
    def __init__(self, ticket_type: str):
        super().__init__()
        self.ticket_type = ticket_type

    description = discord.ui.TextInput(
        label="Describe your issue",
        placeholder="Please provide details about your issue...",
        required=True,
        max_length=1000,
        style=discord.TextStyle.paragraph
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        # Check if user already has an open ticket
        existing = await d1_query(
            "SELECT ticket_id FROM tickets WHERE user_id = ? AND status = 'open'",
            [str(interaction.user.id)]
        )

        if existing["results"]:
            return await interaction.followup.send(
                "❌ You already have an open ticket! Please close your existing ticket before creating a new one.",
                ephemeral=True
            )

        # Generate ticket ID
        ticket_id = str(uuid.uuid4())[:8]

        # Get category
        category = interaction.guild.get_channel(TICKET_CATEGORY_ID)
        if not category:
            return await interaction.followup.send(
                "❌ Ticket category not found. Please contact staff.",
                ephemeral=True
            )

        # Create channel name
        user_name = interaction.user.display_name[:20].replace(" ", "-").lower()
        channel_name = f"{self.ticket_type[:8].lower()}-{user_name}"

        # Create the ticket channel with proper permissions
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True
            ),
            interaction.guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True,
                manage_permissions=True
            )
        }

        # Add all staff roles with access
        for role_id in STAFF_ROLES:
            role = interaction.guild.get_role(role_id)
            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True
                )

        try:
            channel = await interaction.guild.create_text_channel(
                name=channel_name,
                category=category,
                overwrites=overwrites,
                reason=f"Ticket created by {interaction.user} for {self.ticket_type}"
            )
        except Exception as e:
            return await interaction.followup.send(
                f"❌ Failed to create ticket channel: {e}",
                ephemeral=True
            )

        # Save ticket to database
        now = datetime.now(timezone.utc).isoformat()
        await d1_query(
            """INSERT INTO tickets 
            (ticket_id, user_id, channel_id, ticket_type, status, created_at, description)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [ticket_id, str(interaction.user.id), str(channel.id), self.ticket_type, "open", now, self.description.value]
        )

        # Send the ticket embed
        embed = discord.Embed(
            title=f"🎫 {self.ticket_type} Ticket",
            description=(
                f"**Ticket ID:** `{ticket_id}`\n"
                f"**Created by:** {interaction.user.mention}\n"
                f"**Type:** {self.ticket_type}\n\n"
                f"**Issue Description:**\n{self.description.value}"
            ),
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_footer(text="A staff member will assist you shortly")

        # Ping staff roles in the specified order
        role_mentions = []
        for role_id in PING_ROLES:
            role = interaction.guild.get_role(role_id)
            if role:
                role_mentions.append(role.mention)

        # Add @here for visibility
        role_ping_content = " ".join(role_mentions) + " @here"

        # Create the view
        view = TicketControlView(ticket_id=ticket_id, user_id=interaction.user.id)

        # Send the embed with the view in the channel
        message = await channel.send(
            content=f"{role_ping_content} {interaction.user.mention}",
            embed=embed,
            view=view
        )

        # Store the message ID in the database for persistence
        await d1_query(
            "UPDATE tickets SET message_id = ? WHERE ticket_id = ?",
            [str(message.id), ticket_id]
        )

        await interaction.followup.send(
            f"✅ Your {self.ticket_type} ticket has been created! Please check <#{channel.id}>.",
            ephemeral=True
        )

        # Log the ticket creation
        log_embed = discord.Embed(
            title="🎫 Ticket Created",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc)
        )
        log_embed.add_field(name="User", value=interaction.user.mention, inline=True)
        log_embed.add_field(name="Type", value=self.ticket_type, inline=True)
        log_embed.add_field(name="Ticket ID", value=ticket_id, inline=True)
        log_embed.add_field(name="Channel", value=f"<#{channel.id}>", inline=True)
        await send_log(interaction.client, log_embed)


# ── Staff Application Constants ───────────────────────────────────────────────
_APP_LABELS   = {"trial_hoster": "Trial Hoster", "trial_mod": "Trial Mod", "mod": "Moderator"}
_APP_PREFIX   = {"trial_hoster": "hoster-app",   "trial_mod": "trialmod-app", "mod": "mod-app"}
_APP_VIEWERS  = {
    "trial_hoster": [HEAD_STAFF_ROLE_ID, FOUNDER_ROLE_ID],
    "trial_mod":    [HEAD_STAFF_ROLE_ID, FOUNDER_ROLE_ID],
    "mod":          [FOUNDER_ROLE_ID],
}
_APP_PING     = {"trial_hoster": HEAD_STAFF_ROLE_ID, "trial_mod": HEAD_STAFF_ROLE_ID, "mod": FOUNDER_ROLE_ID}
_APP_ROLE     = {"trial_mod": TRIAL_MOD_ROLE_ID, "mod": MOD_ROLE_ID}
_COOLDOWN_DAYS = 14


class ApplicationControlView(discord.ui.View):
    """Persistent Accept / Deny view living inside an application ticket."""

    def __init__(self, app_id: int, app_type: str, user_id: int):
        super().__init__(timeout=None)
        self.app_id = app_id
        self.app_type = app_type
        self.user_id = user_id

        accept = discord.ui.Button(
            label="✅ Accept",
            style=discord.ButtonStyle.success,
            custom_id=f"app_accept_{app_id}"
        )
        deny = discord.ui.Button(
            label="❌ Deny",
            style=discord.ButtonStyle.danger,
            custom_id=f"app_deny_{app_id}"
        )
        accept.callback = self._accept
        deny.callback = self._deny
        self.add_item(accept)
        self.add_item(deny)

    def _can_act(self, member: discord.Member) -> bool:
        if self.app_type == "mod":
            return any(r.id == FOUNDER_ROLE_ID for r in member.roles)
        return any(r.id in (HEAD_STAFF_ROLE_ID, FOUNDER_ROLE_ID) for r in member.roles)

    async def _accept(self, interaction: discord.Interaction):
        if not self._can_act(interaction.user):
            try:
                await interaction.response.send_message(
                    "❌ You don't have permission to accept this application.", ephemeral=True
                )
            except discord.HTTPException:
                pass
            return

        try:
            await interaction.response.defer()
        except discord.HTTPException:
            return

        row = await d1_query(
            "SELECT user_id, app_type, status FROM staff_applications WHERE id = ?",
            [self.app_id]
        )
        if not row["results"] or row["results"][0]["status"] != "pending":
            return await interaction.followup.send(
                "❌ This application has already been processed.", ephemeral=True
            )

        uid = int(row["results"][0]["user_id"])
        app_type = row["results"][0]["app_type"]
        label = _APP_LABELS.get(app_type, app_type)

        member = interaction.guild.get_member(uid)
        if not member:
            return await interaction.followup.send("❌ Applicant not found in server.", ephemeral=True)

        if app_type == "trial_hoster":
            th_cog = interaction.client.cogs.get("TrialHoster")
            if th_cog:
                await th_cog.setup_trial(interaction.guild, member, given_by=interaction.user)
        else:
            role = interaction.guild.get_role(_APP_ROLE.get(app_type))
            if role:
                try:
                    await member.add_roles(role, reason=f"Application accepted by {interaction.user}")
                except discord.HTTPException:
                    pass

        now = int(time.time())
        await d1_query(
            "UPDATE staff_applications SET status = 'accepted', resolved_at = ? WHERE id = ?",
            [now, self.app_id]
        )

        extra = (
            " You have **7 days** to earn **100 points** to become a full **Hoster**!"
            if app_type == "trial_hoster" else ""
        )
        try:
            await member.send(
                f"✅ Your **{label}** application in **{interaction.guild.name}** has been **accepted**!\n"
                f"You have been given the **{label}** role.{extra}"
            )
        except discord.HTTPException:
            pass

        log_ch = interaction.guild.get_channel(LOG_CHANNELS.get("tickets", 0))
        if log_ch:
            embed = discord.Embed(
                title=f"✅ {label} Application Accepted",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="Applicant", value=f"{member.mention} ({member})", inline=True)
            embed.add_field(name="Accepted By", value=interaction.user.mention, inline=True)
            try:
                await log_ch.send(embed=embed)
            except discord.HTTPException:
                pass

        await interaction.followup.send(f"✅ Accepted {member.mention}'s **{label}** application.")
        await asyncio.sleep(3)
        try:
            await interaction.channel.delete(reason=f"Application accepted by {interaction.user}")
        except discord.HTTPException:
            pass

    async def _deny(self, interaction: discord.Interaction):
        if not self._can_act(interaction.user):
            try:
                await interaction.response.send_message(
                    "❌ You don't have permission to deny this application.", ephemeral=True
                )
            except discord.HTTPException:
                pass
            return
        try:
            await interaction.response.send_modal(
                DenyReasonModal(self.app_id, self.app_type, self.user_id)
            )
        except discord.HTTPException:
            pass


class DenyReasonModal(discord.ui.Modal, title="Deny Application"):
    reason = discord.ui.TextInput(
        label="Reason for denial (optional)",
        placeholder="Enter reason...",
        required=False,
        max_length=500,
        style=discord.TextStyle.paragraph
    )

    def __init__(self, app_id: int, app_type: str, user_id: int):
        super().__init__()
        self.app_id = app_id
        self.app_type = app_type
        self.user_id = user_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            return

        label = _APP_LABELS.get(self.app_type, self.app_type)
        now = int(time.time())
        await d1_query(
            "UPDATE staff_applications SET status = 'denied', resolved_at = ? WHERE id = ?",
            [now, self.app_id]
        )

        member = interaction.guild.get_member(self.user_id)
        if member:
            reason_text = f"\n**Reason:** {self.reason.value}" if self.reason.value else ""
            try:
                await member.send(
                    f"❌ Your **{label}** application in **{interaction.guild.name}** has been **denied**.{reason_text}\n"
                    f"You may reapply after **{_COOLDOWN_DAYS} days**."
                )
            except discord.HTTPException:
                pass

        log_ch = interaction.guild.get_channel(LOG_CHANNELS.get("tickets", 0))
        if log_ch:
            embed = discord.Embed(
                title=f"❌ {label} Application Denied",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="Applicant", value=f"<@{self.user_id}>", inline=True)
            embed.add_field(name="Denied By", value=interaction.user.mention, inline=True)
            if self.reason.value:
                embed.add_field(name="Reason", value=self.reason.value, inline=False)
            try:
                await log_ch.send(embed=embed)
            except discord.HTTPException:
                pass

        await interaction.followup.send(f"❌ Application denied.")
        await asyncio.sleep(3)
        try:
            await interaction.channel.delete(reason=f"Application denied by {interaction.user}")
        except discord.HTTPException:
            pass


class ApplicationTypeSelect(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.select(
        placeholder="Choose a position to apply for...",
        options=[
            discord.SelectOption(
                label="Trial Hoster",
                value="trial_hoster",
                description="Apply to host raids and earn the Hoster role",
                emoji="🎯"
            ),
            discord.SelectOption(
                label="Trial Mod",
                value="trial_mod",
                description="Apply to join the moderation team as Trial Mod",
                emoji="🛡️"
            ),
            discord.SelectOption(
                label="Moderator",
                value="mod",
                description="Apply to become a full Moderator",
                emoji="⚔️"
            ),
        ]
    )
    async def select_callback(self, interaction: discord.Interaction, select: discord.ui.Select):
        try:
            await interaction.response.send_modal(StaffApplicationModal(select.values[0]))
        except discord.HTTPException:
            pass


class StaffApplicationModal(discord.ui.Modal):
    why = discord.ui.TextInput(
        label="Why do you want this role?",
        placeholder="Tell us why you'd be a great fit...",
        required=True,
        max_length=1000,
        style=discord.TextStyle.paragraph
    )
    activity = discord.ui.TextInput(
        label="How active are you? (hours/week)",
        placeholder="e.g. 10-15 hours",
        required=True,
        max_length=50,
        style=discord.TextStyle.short
    )
    experience = discord.ui.TextInput(
        label="Previous experience?",
        placeholder="Any previous similar roles or experience...",
        required=False,
        max_length=500,
        style=discord.TextStyle.paragraph
    )
    timezone_field = discord.ui.TextInput(
        label="Timezone",
        placeholder="e.g. EST, PST, GMT+1",
        required=True,
        max_length=50,
        style=discord.TextStyle.short
    )

    def __init__(self, app_type: str):
        label = _APP_LABELS.get(app_type, app_type)
        super().__init__(title=f"Apply for {label}")
        self.app_type = app_type

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException:
            return

        # One active application per user
        existing = await d1_query(
            "SELECT id FROM staff_applications WHERE user_id = ? AND status = 'pending'",
            [str(interaction.user.id)]
        )
        if existing["results"]:
            return await interaction.followup.send(
                "❌ You already have an open application. Wait for it to be resolved first.",
                ephemeral=True
            )

        # 14-day cooldown after denial for this app type
        cooldown_cutoff = int(time.time()) - (_COOLDOWN_DAYS * 86400)
        recent = await d1_query(
            "SELECT resolved_at FROM staff_applications "
            "WHERE user_id = ? AND app_type = ? AND status = 'denied' AND resolved_at > ?",
            [str(interaction.user.id), self.app_type, cooldown_cutoff]
        )
        if recent["results"]:
            eligible_ts = recent["results"][0]["resolved_at"] + (_COOLDOWN_DAYS * 86400)
            return await interaction.followup.send(
                f"❌ You were recently denied for this position. You may reapply <t:{eligible_ts}:R>.",
                ephemeral=True
            )

        category = interaction.guild.get_channel(TICKET_CATEGORY_ID)
        if not category:
            return await interaction.followup.send("❌ Ticket category not found.", ephemeral=True)

        label = _APP_LABELS[self.app_type]
        prefix = _APP_PREFIX[self.app_type]
        viewer_ids = _APP_VIEWERS[self.app_type]
        ping_id = _APP_PING[self.app_type]

        user_name = interaction.user.display_name[:16].replace(" ", "-").lower()
        channel_name = f"{prefix}-{user_name}"

        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            ),
            interaction.guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True,
                read_message_history=True, manage_channels=True
            ),
        }
        for rid in viewer_ids:
            role = interaction.guild.get_role(rid)
            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True
                )

        try:
            channel = await interaction.guild.create_text_channel(
                name=channel_name,
                category=category,
                overwrites=overwrites,
                reason=f"{label} application by {interaction.user}"
            )
        except discord.HTTPException as e:
            return await interaction.followup.send(
                f"❌ Failed to create channel: {e}", ephemeral=True
            )

        now_ts = int(time.time())
        await d1_query(
            "INSERT INTO staff_applications (user_id, app_type, status, channel_id, created_at) "
            "VALUES (?, ?, 'pending', ?, ?)",
            [str(interaction.user.id), self.app_type, str(channel.id), now_ts]
        )
        id_row = await d1_query(
            "SELECT id FROM staff_applications WHERE channel_id = ?", [str(channel.id)]
        )
        app_id = id_row["results"][0]["id"]

        embed = discord.Embed(
            title=f"📋 {label} Application",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_author(
            name=str(interaction.user),
            icon_url=interaction.user.display_avatar.url
        )
        embed.add_field(name="👤 Applicant", value=interaction.user.mention, inline=True)
        embed.add_field(name="📋 Position",  value=label,                    inline=True)
        embed.add_field(name="🕐 Applied",   value=f"<t:{now_ts}:F>",        inline=True)
        embed.add_field(name="❓ Why do you want this role?", value=self.why.value, inline=False)
        embed.add_field(name="⏰ Activity",   value=self.activity.value,      inline=True)
        embed.add_field(name="🌍 Timezone",   value=self.timezone_field.value, inline=True)
        if self.experience.value:
            embed.add_field(name="📜 Experience", value=self.experience.value, inline=False)

        view = ApplicationControlView(app_id=app_id, app_type=self.app_type, user_id=interaction.user.id)
        interaction.client.add_view(view)

        ping_role = interaction.guild.get_role(ping_id)
        await channel.send(
            content=ping_role.mention if ping_role else "",
            embed=embed,
            view=view
        )

        try:
            await interaction.user.send(
                f"📋 Your **{label}** application in **{interaction.guild.name}** has been submitted!\n"
                f"Staff will review it in <#{channel.id}>. You'll be DM'd when a decision is made."
            )
        except discord.HTTPException:
            pass

        await interaction.followup.send(
            f"✅ Your **{label}** application has been submitted! Check <#{channel.id}>.",
            ephemeral=True
        )


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="📋 Verification",
        style=discord.ButtonStyle.primary,
        emoji="📋",
        custom_id="ticket_verify"
    )
    async def verify_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_ticket_creation(interaction, "Verification")

    @discord.ui.button(
        label="⚖️ Blacklist Appeal",
        style=discord.ButtonStyle.danger,
        emoji="⚖️",
        custom_id="ticket_appeal"
    )
    async def appeal_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_ticket_creation(interaction, "Blacklist Appeal")

    @discord.ui.button(
        label="❓ Other",
        style=discord.ButtonStyle.secondary,
        emoji="❓",
        custom_id="ticket_other"
    )
    async def other_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_ticket_creation(interaction, "Other")

    @discord.ui.button(
        label="Apply",
        style=discord.ButtonStyle.success,
        emoji="📝",
        custom_id="ticket_apply"
    )
    async def apply_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="📋 Staff Application",
            description=(
                "Select the position you'd like to apply for below.\n\n"
                "**Hoster apps** → reviewed by Head Staff & Founders\n"
                "**Trial Mod apps** → reviewed by Head Staff & Founders\n"
                "**Mod apps** → reviewed by Founders only"
            ),
            color=discord.Color.blue()
        )
        try:
            await interaction.response.send_message(embed=embed, view=ApplicationTypeSelect(), ephemeral=True)
        except discord.HTTPException:
            pass


async def handle_ticket_creation(interaction: discord.Interaction, ticket_type: str):
    """Handle ticket creation with modal — open modal immediately; duplicate check is inside on_submit."""
    try:
        await interaction.response.send_modal(TicketModal(ticket_type))
    except discord.HTTPException:
        return


# ── Ticket Cog ──────────────────────────────────────────────────────────────
class TicketSystem(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Register persistent views on startup
        self.bot.loop.create_task(self.register_persistent_views())

    async def register_persistent_views(self):
        """Register all persistent views on bot startup"""
        try:
            await self.bot.wait_until_ready()

            # Ensure staff_applications table exists
            await d1_query(
                "CREATE TABLE IF NOT EXISTS staff_applications "
                "(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, "
                "app_type TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', "
                "channel_id TEXT, created_at INTEGER NOT NULL, resolved_at INTEGER)"
            )

            # Register Ticket Panel View
            ticket_view = TicketPanelView()
            self.bot.add_view(ticket_view)
            print("✅ Registered persistent Ticket Panel view")

            # Restore pending application views
            apps = await d1_query(
                "SELECT id, app_type, user_id FROM staff_applications WHERE status = 'pending'"
            )
            app_count = 0
            for app in apps.get("results", []):
                view = ApplicationControlView(
                    app_id=app["id"],
                    app_type=app["app_type"],
                    user_id=int(app["user_id"])
                )
                self.bot.add_view(view)
                app_count += 1
            print(f"✅ Restored {app_count} pending application view(s)")

            # Restore all open tickets
            result = await d1_query(
                "SELECT ticket_id, user_id, channel_id, message_id FROM tickets WHERE status = 'open'"
            )

            restored_count = 0
            orphaned_count = 0

            for row in result["results"]:
                try:
                    ticket_id = row["ticket_id"]
                    user_id = int(row["user_id"])
                    channel_id = int(row["channel_id"])
                    message_id = row.get("message_id")

                    channel = self.bot.get_channel(channel_id)
                    if not channel:
                        # Channel doesn't exist - close the ticket
                        now = datetime.now(timezone.utc).isoformat()
                        await d1_query(
                            "UPDATE tickets SET status = 'closed', closed_at = ? WHERE ticket_id = ?",
                            [now, ticket_id]
                        )
                        orphaned_count += 1
                        print(f"🗑️ Closed orphaned ticket {ticket_id} (channel not found)")
                        continue

                    if message_id:
                        try:
                            message = await channel.fetch_message(int(message_id))
                            view = TicketControlView(ticket_id=ticket_id, user_id=user_id)
                            await message.edit(view=view)
                            restored_count += 1
                            print(f"✅ Restored view for ticket {ticket_id}")
                            continue
                        except Exception as e:
                            print(f"Could not restore view for ticket {ticket_id}: {e}")

                    # If message not found by ID, search for it
                    try:
                        async for message in channel.history(limit=200):
                            if message.embeds and "Ticket ID:" in message.embeds[0].description:
                                view = TicketControlView(ticket_id=ticket_id, user_id=user_id)
                                await message.edit(view=view)
                                await d1_query(
                                    "UPDATE tickets SET message_id = ? WHERE ticket_id = ?",
                                    [str(message.id), ticket_id]
                                )
                                restored_count += 1
                                print(f"✅ Found and restored view for ticket {ticket_id}")
                                break
                        else:
                            print(f"⚠️ Could not find ticket message for {ticket_id} in channel {channel.name}")
                    except Exception as e:
                        print(f"Error searching for ticket {ticket_id}: {e}")

                except Exception as e:
                    print(f"Error restoring ticket {row.get('ticket_id', 'unknown')}: {e}")

            print(f"✅ Restored {restored_count} tickets, closed {orphaned_count} orphaned tickets")

        except Exception as e:
            print(f"Error registering persistent views: {e}")

    @app_commands.command(
        name="fixtickets",
        description="Fix broken ticket buttons (Staff only)"
    )
    @app_commands.checks.cooldown(1, 30)
    async def fixtickets(self, interaction: discord.Interaction):
        """Manually fix all open tickets by re-adding views"""
        if not is_staff(interaction.user):
            return await interaction.response.send_message(
                "❌ You do not have permission to use this command.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            fixed_count = 0
            failed_count = 0
            orphaned_count = 0

            # Get all open tickets
            result = await d1_query(
                "SELECT ticket_id, user_id, channel_id, message_id FROM tickets WHERE status = 'open'"
            )

            status_lines = []

            for row in result["results"]:
                try:
                    ticket_id = row["ticket_id"]
                    user_id = int(row["user_id"])
                    channel_id = int(row["channel_id"])
                    message_id = row.get("message_id")

                    channel = self.bot.get_channel(channel_id)
                    if not channel:
                        # Channel doesn't exist - close the ticket
                        now = datetime.now(timezone.utc).isoformat()
                        await d1_query(
                            "UPDATE tickets SET status = 'closed', closed_at = ? WHERE ticket_id = ?",
                            [now, ticket_id]
                        )
                        orphaned_count += 1
                        status_lines.append(f"🗑️ `{ticket_id}` - Channel not found (closed)")
                        continue

                    message = None

                    if message_id:
                        try:
                            message = await channel.fetch_message(int(message_id))
                        except:
                            message = None

                    if not message:
                        # Search for the ticket message
                        try:
                            async for msg in channel.history(limit=200):
                                if msg.embeds and "Ticket ID:" in msg.embeds[0].description:
                                    message = msg
                                    await d1_query(
                                        "UPDATE tickets SET message_id = ? WHERE ticket_id = ?",
                                        [str(message.id), ticket_id]
                                    )
                                    break
                        except Exception:
                            pass

                    if message:
                        view = TicketControlView(ticket_id=ticket_id, user_id=user_id)
                        await message.edit(view=view)
                        fixed_count += 1
                        status_lines.append(f"✅ `{ticket_id}` - Fixed")
                    else:
                        failed_count += 1
                        status_lines.append(f"❌ `{ticket_id}` - Message not found")

                except Exception as e:
                    failed_count += 1
                    status_lines.append(f"❌ `{row.get('ticket_id', 'unknown')}` - Error: {str(e)[:30]}")

            # Create the result embed
            embed = discord.Embed(
                title="🔧 Ticket Fix Results",
                color=discord.Color.green() if fixed_count > 0 else discord.Color.orange(),
                timestamp=datetime.now(timezone.utc)
            )

            embed.add_field(
                name="Results",
                value=(
                    f"✅ Fixed: **{fixed_count}** tickets\n"
                    f"❌ Failed: **{failed_count}** tickets\n"
                    f"🗑️ Orphaned: **{orphaned_count}** tickets (closed)"
                ),
                inline=False
            )

            if status_lines:
                # Show first 20 status lines
                status_text = "\n".join(status_lines[:20])
                if len(status_lines) > 20:
                    status_text += f"\n... and {len(status_lines) - 20} more"
                embed.add_field(name="Details", value=status_text, inline=False)

            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @app_commands.command(
        name="ticketstats",
        description="Get ticket statistics (Staff only)"
    )
    @app_commands.checks.cooldown(1, 30)
    async def ticketstats(self, interaction: discord.Interaction):
        """Get statistics about tickets"""
        if not is_staff(interaction.user):
            return await interaction.response.send_message(
                "❌ You do not have permission to use this command.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        try:
            # Get open tickets
            open_result = await d1_query(
                "SELECT COUNT(*) as count FROM tickets WHERE status = 'open'"
            )
            open_count = open_result["results"][0]["count"] if open_result["results"] else 0

            # Get closed tickets (last 30 days)
            thirty_days_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
            closed_result = await d1_query(
                "SELECT COUNT(*) as count FROM tickets WHERE status = 'closed' AND closed_at > ?",
                [thirty_days_ago]
            )
            closed_count = closed_result["results"][0]["count"] if closed_result["results"] else 0

            # Get total tickets
            total_result = await d1_query("SELECT COUNT(*) as count FROM tickets")
            total_count = total_result["results"][0]["count"] if total_result["results"] else 0

            # Get tickets by type
            type_result = await d1_query(
                "SELECT ticket_type, COUNT(*) as count FROM tickets GROUP BY ticket_type"
            )

            embed = discord.Embed(
                title="📊 Ticket Statistics",
                color=discord.Color.blue(),
                timestamp=datetime.now(timezone.utc)
            )

            embed.add_field(name="🟢 Open Tickets", value=f"**{open_count}**", inline=True)
            embed.add_field(name="🔒 Closed (30 days)", value=f"**{closed_count}**", inline=True)
            embed.add_field(name="📊 Total Tickets", value=f"**{total_count}**", inline=True)

            # Add ticket types
            if type_result["results"]:
                type_lines = []
                for row in type_result["results"]:
                    type_lines.append(f"• **{row['ticket_type']}**: {row['count']}")
                embed.add_field(
                    name="Tickets by Type",
                    value="\n".join(type_lines),
                    inline=False
                )

            embed.set_footer(text=f"Guild: {interaction.guild.name}")

            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


async def setup(bot: commands.Bot):
    """Setup function for the cog"""
    await bot.add_cog(TicketSystem(bot))
    print("✅ Ticket cog setup complete")