import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import uuid
from datetime import datetime, timezone

# Import shared functions from bot.py
from bot import (
    d1_query, 
    GUILD_ID,
    FOUNDER_ROLE_ID,
    HEAD_STAFF_ROLE_ID,
    MOD_ROLE_ID,
    TRIAL_MOD_ROLE_ID,
    is_staff,
    is_mod_or_higher,
    is_head_staff_or_founder
)

# ── Channel IDs ──────────────────────────────────────────────────────────────
TICKET_PANEL_CHANNEL_ID = 1535906243435040788
TICKET_CATEGORY_ID = 1535905899854430222
TICKET_LOG_CHANNEL_ID = 1535910317769629776

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
    channel = bot.get_channel(TICKET_LOG_CHANNEL_ID)
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
        # Check if user has permission (ticket owner or staff)
        if interaction.user.id != self.user_id and not is_staff(interaction.user):
            return await interaction.response.send_message(
                "❌ Only the ticket owner or staff can close this ticket.",
                ephemeral=True
            )

        try:
            await interaction.response.defer(ephemeral=True)
        except discord.NotFound:
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

        # Log the ticket closure
        log_embed = discord.Embed(
            title="🔒 Ticket Closed",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc)
        )
        log_embed.add_field(name="Ticket ID", value=self.ticket_id, inline=True)
        log_embed.add_field(name="Closed By", value=interaction.user.mention, inline=True)
        log_embed.add_field(name="User", value=f"<@{self.user_id}>", inline=True)
        await send_log(interaction.client, log_embed)

    @discord.ui.button(
        label="📋 Claim Ticket",
        style=discord.ButtonStyle.success,
        emoji="📋",
        custom_id="ticket_claim"
    )
    async def claim_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message(
                "❌ Only staff can claim tickets.",
                ephemeral=True
            )

        # Check if ticket is already claimed
        result = await d1_query(
            "SELECT claimed_by FROM tickets WHERE ticket_id = ?",
            [self.ticket_id]
        )

        if result["results"] and result["results"][0]["claimed_by"]:
            return await interaction.response.send_message(
                f"❌ This ticket has already been claimed by <@{result['results'][0]['claimed_by']}>.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

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


async def handle_ticket_creation(interaction: discord.Interaction, ticket_type: str):
    """Handle ticket creation with modal"""
    # Check if user already has an open ticket
    existing = await d1_query(
        "SELECT ticket_id FROM tickets WHERE user_id = ? AND status = 'open'",
        [str(interaction.user.id)]
    )

    if existing["results"]:
        return await interaction.response.send_message(
            "❌ You already have an open ticket! Please close your existing ticket before creating a new one.",
            ephemeral=True
        )

    # Show the modal
    await interaction.response.send_modal(TicketModal(ticket_type))


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

            # Register Ticket Panel View
            ticket_view = TicketPanelView()
            self.bot.add_view(ticket_view)
            print("✅ Registered persistent Ticket Panel view")

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