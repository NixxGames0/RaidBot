import discord
from discord import app_commands
from discord.ext import commands, tasks
import asyncio
import json
import random
import uuid
from datetime import datetime, timezone, timedelta

from bot import (
    d1_query,
    GUILD_ID,
    LOG_CHANNEL_ID,
    VERIFIED_ROLE_ID,
    HEAD_STAFF_ROLE_ID,
    FOUNDER_ROLE_ID,
    is_staff,
    is_head_staff_or_founder,
    send_log
)

# ─── Constants ──────────────────────────────────────────────
GIVEAWAY_HOST_ROLE_ID = 1536325001584578621
GIVEAWAY_PING_ROLE_ID = 1536324869422063626
GIVEAWAY_LOG_CHANNEL_ID = 1536329270354378783

# ─── Database setup ─────────────────────────────────────────
async def init_giveaway_db():
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
    # Try adding new columns if missing (safe to run)
    for col_sql in [
        "ALTER TABLE giveaways ADD COLUMN required_role_id TEXT",
        "ALTER TABLE giveaways ADD COLUMN last_reroll TEXT",
    ]:
        try:
            await d1_query(col_sql)
        except Exception:
            pass


# ─── Helper functions ──────────────────────────────────────
async def log_giveaway(bot: commands.Bot, embed: discord.Embed):
    """Send log to the dedicated giveaway log channel."""
    channel = bot.get_channel(GIVEAWAY_LOG_CHANNEL_ID)
    if channel:
        try:
            await channel.send(embed=embed)
        except Exception as e:
            print(f"Error sending giveaway log: {e}")


def generate_giveaway_id() -> str:
    return f"GIV-{uuid.uuid4().hex[:6].upper()}"


def parse_duration(duration_str: str) -> int:
    """Convert a duration string like '1h', '2d', '30m' to seconds."""
    duration_str = duration_str.strip().lower()
    if duration_str.endswith("h"):
        return int(duration_str[:-1]) * 3600
    elif duration_str.endswith("d"):
        return int(duration_str[:-1]) * 86400
    elif duration_str.endswith("m"):
        return int(duration_str[:-1]) * 60
    else:
        try:
            return int(duration_str) * 60  # default to minutes
        except ValueError:
            return None


def format_duration(seconds: int) -> str:
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    if days:
        return f"{days}d {hours}h"
    elif hours:
        return f"{hours}h {minutes}m"
    else:
        return f"{minutes}m"


# ─── Views ──────────────────────────────────────────────────
class BonusEntryView(discord.ui.View):
    def __init__(self, builder_view: "GiveawayBuilderView"):
        super().__init__(timeout=120)
        self.builder_view = builder_view
        self.bonus_entries = builder_view.bonus_entries.copy() if builder_view.bonus_entries else {}
        self.add_item(self._create_select())
        self.add_item(self._create_quantity_input())

    def _create_select(self) -> discord.ui.Select:
        # Get all roles in the guild
        guild = self.builder_view.interaction.guild if hasattr(self.builder_view, "interaction") else None
        if not guild:
            guild = self.builder_view.bot.get_guild(GUILD_ID)
        options = []
        # Add a "None" option to remove a role
        options.append(discord.SelectOption(label="Remove a role", value="remove", description="Select a role to remove"))
        for role in guild.roles:
            if role.name != "@everyone":
                options.append(discord.SelectOption(
                    label=role.name[:100],
                    value=str(role.id),
                    description=f"Add {role.name} as bonus entry"
                ))
        select = discord.ui.Select(
            placeholder="Select a role to add or remove...",
            min_values=1,
            max_values=1,
            options=options[:25]  # Discord limit
        )
        select.callback = self._select_callback
        return select

    def _create_quantity_input(self) -> discord.ui.Button:
        return discord.ui.Button(
            label="Set Bonus Count",
            style=discord.ButtonStyle.primary,
            emoji="✏️"
        )

    @discord.ui.button(label="Set Bonus Count", style=discord.ButtonStyle.primary, emoji="✏️")
    async def set_quantity(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Send a modal to input quantity
        modal = BonusQuantityModal(self)
        await interaction.response.send_modal(modal)

    async def _select_callback(self, interaction: discord.Interaction):
        select = self.children[0]
        value = select.values[0]
        if value == "remove":
            # Show a dropdown of current bonus roles to remove
            if not self.bonus_entries:
                return await interaction.response.send_message("No bonus entries to remove.", ephemeral=True)
            options = []
            for role_id, count in self.bonus_entries.items():
                role = interaction.guild.get_role(int(role_id))
                if role:
                    options.append(discord.SelectOption(
                        label=role.name[:100],
                        value=str(role_id),
                        description=f"Bonus count: {count}"
                    ))
            if not options:
                return await interaction.response.send_message("No bonus entries to remove.", ephemeral=True)
            view = RemoveBonusView(self, options)
            await interaction.response.send_message("Select a role to remove:", view=view, ephemeral=True)
        else:
            # Add role with default bonus count 1
            role_id = int(value)
            if role_id in self.bonus_entries:
                return await interaction.response.send_message("This role already has bonus entries.", ephemeral=True)
            self.bonus_entries[role_id] = 1
            await self.builder_view.update_embed(interaction)
            await interaction.response.send_message(f"✅ Added {interaction.guild.get_role(role_id).mention} with 1 bonus entry.", ephemeral=True)


class RemoveBonusView(discord.ui.View):
    def __init__(self, parent_view: BonusEntryView, options: list):
        super().__init__(timeout=60)
        self.parent_view = parent_view
        self.add_item(self._create_select(options))

    def _create_select(self, options: list) -> discord.ui.Select:
        select = discord.ui.Select(
            placeholder="Select a role to remove...",
            min_values=1,
            max_values=1,
            options=options[:25]
        )
        select.callback = self._remove_callback
        return select

    async def _remove_callback(self, interaction: discord.Interaction):
        select = self.children[0]
        role_id = int(select.values[0])
        if role_id in self.parent_view.bonus_entries:
            del self.parent_view.bonus_entries[role_id]
            await self.parent_view.builder_view.update_embed(interaction)
            await interaction.response.send_message("✅ Removed bonus entry.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Role not found.", ephemeral=True)


class BonusQuantityModal(discord.ui.Modal, title="Set Bonus Count"):
    def __init__(self, view: BonusEntryView):
        super().__init__()
        self.view = view
        self.role_id = None
        self.quantity = discord.ui.TextInput(
            label="Bonus Entries Count",
            placeholder="Enter number (e.g., 2)",
            required=True,
            max_length=10
        )
        self.add_item(self.quantity)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            count = int(self.quantity.value)
            if count < 0:
                raise ValueError
        except ValueError:
            return await interaction.response.send_message("❌ Please enter a valid positive number.", ephemeral=True)

        # We need to get the role that was last selected; we'll use a fallback: ask user to select again
        # For simplicity, we'll add a new role with the given count via a follow-up select
        # This is a bit complex; we can just add a modal that asks for role ID as well, but for now,
        # we'll let the user select the role first, then the count.
        # Actually we can add a select in the modal? Better: we'll add a button "Set Bonus Count" that opens a modal where they type role ID and count.
        # Let's redesign: the modal will have two fields: role ID and count.
        # But for now, we'll just set the count for the last selected role.
        # Since we don't have that, we'll use a simpler approach: the "Set Bonus Count" button will open a modal with role ID and count.
        # I'll update to that.

        # We'll implement a proper modal with role ID and count.
        await interaction.response.send_message("Please use the new modal with role ID and count.", ephemeral=True)


# Proper modal for adding bonus entries
class BonusEntryModal(discord.ui.Modal, title="Add Bonus Entry"):
    def __init__(self, builder_view: "GiveawayBuilderView"):
        super().__init__()
        self.builder_view = builder_view
        self.role_id = discord.ui.TextInput(
            label="Role ID",
            placeholder="Paste the role ID",
            required=True,
            max_length=20
        )
        self.count = discord.ui.TextInput(
            label="Bonus Entries Count",
            placeholder="e.g., 2",
            required=True,
            max_length=10,
            default="1"
        )
        self.add_item(self.role_id)
        self.add_item(self.count)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            role_id = int(self.role_id.value)
            count = int(self.count.value)
            if count < 0:
                raise ValueError
        except ValueError:
            return await interaction.response.send_message("❌ Invalid role ID or count.", ephemeral=True)

        # Check if role exists
        role = interaction.guild.get_role(role_id)
        if not role:
            return await interaction.response.send_message("❌ Role not found in this server.", ephemeral=True)

        # Add to builder
        self.builder_view.bonus_entries[role_id] = count
        await self.builder_view.update_embed(interaction)
        await interaction.response.send_message(f"✅ Added {role.mention} with {count} bonus entries.", ephemeral=True)


class GiveawayBuilderView(discord.ui.View):
    def __init__(self, bot: commands.Bot, user_id: int, mode: str = "create", giveaway_data: dict = None, interaction: discord.Interaction = None):
        super().__init__(timeout=300)
        self.bot = bot
        self.user_id = user_id
        self.mode = mode
        self.interaction = interaction
        self.data = {
            "name": "",
            "sponsor": "",
            "prize": "",
            "duration": 86400,  # default 1 day in seconds
            "required_role_id": VERIFIED_ROLE_ID,
            "bonus_entries": {},  # role_id -> count
            "giveaway_id": None,
        }
        if giveaway_data:
            self.data.update(giveaway_data)
        self.bonus_entries = self.data.get("bonus_entries", {})

    def build_embed(self) -> discord.Embed:
        guild = self.bot.get_guild(GUILD_ID)
        required_role = guild.get_role(self.data["required_role_id"]) if self.data["required_role_id"] else None
        required_role_mention = required_role.mention if required_role else "None"

        embed = discord.Embed(
            title="🎉 Giveaway Builder",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(
            name="Name",
            value=self.data["name"] or "*(not set)*",
            inline=True
        )
        embed.add_field(
            name="Sponsor",
            value=self.data["sponsor"] or "*(not set)*",
            inline=True
        )
        embed.add_field(
            name="Prize",
            value=self.data["prize"] or "*(not set)*",
            inline=True
        )
        embed.add_field(
            name="Duration",
            value=format_duration(self.data["duration"]) if self.data["duration"] else "*(not set)*",
            inline=True
        )
        embed.add_field(
            name="Required Role",
            value=required_role_mention,
            inline=True
        )
        # Bonus entries
        bonus_text = ""
        if self.bonus_entries:
            for role_id, count in self.bonus_entries.items():
                role = guild.get_role(role_id)
                if role:
                    bonus_text += f"{role.mention}: {count} entries\n"
                else:
                    bonus_text += f"Role {role_id}: {count} entries\n"
        else:
            bonus_text = "None"
        embed.add_field(
            name="Bonus Entries",
            value=bonus_text,
            inline=False
        )
        embed.add_field(
            name="Status",
            value="Draft" if self.mode == "create" else "Editing",
            inline=True
        )
        if self.data.get("giveaway_id"):
            embed.add_field(
                name="Giveaway ID",
                value=self.data["giveaway_id"],
                inline=True
            )
        embed.set_footer(text="Changes are not saved until you click 'Post' or 'Update'.")
        return embed

    async def update_embed(self, interaction: discord.Interaction):
        embed = self.build_embed()
        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(embed=embed, view=self)
            else:
                await interaction.edit_original_response(embed=embed, view=self)
        except discord.NotFound:
            await interaction.followup.send("⏰ Your session expired. Please start over.", ephemeral=True)

    def _check_user(self, interaction) -> bool:
        if interaction.user.id != self.user_id:
            asyncio.create_task(interaction.response.send_message("❌ This is not your builder.", ephemeral=True))
            return False
        return True

    @discord.ui.button(label="Change Name", style=discord.ButtonStyle.primary)
    async def change_name(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_user(interaction):
            return
        await interaction.response.send_modal(GiveawayNameModal(self))

    @discord.ui.button(label="Change Sponsor", style=discord.ButtonStyle.primary)
    async def change_sponsor(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_user(interaction):
            return
        await interaction.response.send_modal(GiveawaySponsorModal(self))

    @discord.ui.button(label="Change Prize", style=discord.ButtonStyle.primary)
    async def change_prize(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_user(interaction):
            return
        await interaction.response.send_modal(GiveawayPrizeModal(self))

    @discord.ui.button(label="Change Duration", style=discord.ButtonStyle.primary)
    async def change_duration(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_user(interaction):
            return
        await interaction.response.send_modal(GiveawayDurationModal(self))

    @discord.ui.button(label="Change Required Role", style=discord.ButtonStyle.primary)
    async def change_required_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_user(interaction):
            return
        # Show a dropdown of roles (select menu)
        guild = interaction.guild
        options = []
        for role in guild.roles:
            if role.name != "@everyone":
                options.append(discord.SelectOption(
                    label=role.name[:100],
                    value=str(role.id),
                    description=f"ID: {role.id}"
                ))
        view = discord.ui.View(timeout=60)
        select = discord.ui.Select(
            placeholder="Select required role...",
            min_values=1,
            max_values=1,
            options=options[:25]
        )
        select.callback = self._required_role_select
        view.add_item(select)
        await interaction.response.send_message("Select the required role to join this giveaway:", view=view, ephemeral=True)

    async def _required_role_select(self, interaction: discord.Interaction):
        select = interaction.data["values"][0]
        role_id = int(select)
        self.data["required_role_id"] = role_id
        await self.update_embed(interaction)
        await interaction.response.edit_message(content="✅ Required role updated.", view=None)

    @discord.ui.button(label="Manage Bonus Entries", style=discord.ButtonStyle.primary)
    async def manage_bonus(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_user(interaction):
            return
        # Show a modal to add bonus entries
        await interaction.response.send_modal(BonusEntryModal(self))

    @discord.ui.button(label="Preview", style=discord.ButtonStyle.secondary)
    async def preview(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_user(interaction):
            return
        # Build a public embed preview and send ephemeral
        embed = await self.build_preview_embed(interaction.guild)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def build_preview_embed(self, guild) -> discord.Embed:
        required_role = guild.get_role(self.data["required_role_id"]) if self.data["required_role_id"] else None
        embed = discord.Embed(
            title=f"🎉 {self.data['name'] or 'Giveaway'}",
            description=f"**Prize:** {self.data['prize'] or 'Not set'}\n"
                        f"**Sponsor:** {self.data['sponsor'] or 'N/A'}\n"
                        f"**Host:** <@{self.user_id}>\n"
                        f"**Required Role:** {required_role.mention if required_role else 'None'}\n"
                        f"**Ends:** <t:{int((datetime.now(timezone.utc) + timedelta(seconds=self.data['duration'])).timestamp())}:R>\n"
                        f"**Entrants:** 0",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc)
        )
        # Bonus entries
        bonus_text = ""
        if self.bonus_entries:
            for role_id, count in self.bonus_entries.items():
                role = guild.get_role(role_id)
                if role:
                    bonus_text += f"{role.mention}: {count} entries\n"
        if bonus_text:
            embed.add_field(name="Bonus Entries", value=bonus_text, inline=False)
        embed.set_footer(text="Giveaway preview - not yet posted")
        return embed

    @discord.ui.button(label="Post Giveaway", style=discord.ButtonStyle.success)
    async def post_giveaway(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_user(interaction):
            return

        # Validate required fields
        if not self.data["name"]:
            return await interaction.response.send_message("❌ Please set a name.", ephemeral=True)
        if not self.data["prize"]:
            return await interaction.response.send_message("❌ Please set a prize.", ephemeral=True)
        if not self.data["duration"]:
            return await interaction.response.send_message("❌ Please set a duration.", ephemeral=True)

        # Determine mode: create or edit
        if self.mode == "create":
            giveaway_id = generate_giveaway_id()
            self.data["giveaway_id"] = giveaway_id
            # Save to DB
            now = datetime.now(timezone.utc).isoformat()
            end_time = (datetime.now(timezone.utc) + timedelta(seconds=self.data["duration"])).isoformat()
            await d1_query(
                """INSERT INTO giveaways
                (giveaway_id, name, sponsor, prize, host_id, required_role_id, created_at, end_time,
                 status, bonus_entries, entrants, channel_id, message_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    giveaway_id,
                    self.data["name"],
                    self.data["sponsor"] or "",
                    self.data["prize"],
                    str(self.user_id),
                    str(self.data["required_role_id"]),
                    now,
                    end_time,
                    "active",
                    json.dumps(self.bonus_entries),
                    json.dumps([]),
                    None,
                    None
                ]
            )
            # Post public embed
            await self.post_public_embed(interaction)
            await interaction.response.send_message(f"✅ Giveaway posted! ID: {giveaway_id}", ephemeral=True)
            # Log
            embed = discord.Embed(
                title="🎉 Giveaway Created",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="Giveaway ID", value=giveaway_id, inline=True)
            embed.add_field(name="Name", value=self.data["name"], inline=True)
            embed.add_field(name="Host", value=f"<@{self.user_id}>", inline=True)
            await log_giveaway(self.bot, embed)
            await send_log(self.bot, embed)
        else:
            # Update existing giveaway
            giveaway_id = self.data["giveaway_id"]
            end_time = (datetime.now(timezone.utc) + timedelta(seconds=self.data["duration"])).isoformat()
            await d1_query(
                """UPDATE giveaways SET
                    name = ?, sponsor = ?, prize = ?, required_role_id = ?,
                    end_time = ?, bonus_entries = ?
                    WHERE giveaway_id = ?""",
                [
                    self.data["name"],
                    self.data["sponsor"] or "",
                    self.data["prize"],
                    str(self.data["required_role_id"]),
                    end_time,
                    json.dumps(self.bonus_entries),
                    giveaway_id
                ]
            )
            # Update public embed
            await self.update_public_embed(interaction)
            await interaction.response.send_message(f"✅ Giveaway updated! ID: {giveaway_id}", ephemeral=True)
            # Log
            embed = discord.Embed(
                title="✏️ Giveaway Updated",
                color=discord.Color.orange(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="Giveaway ID", value=giveaway_id, inline=True)
            embed.add_field(name="Name", value=self.data["name"], inline=True)
            embed.add_field(name="Updated By", value=interaction.user.mention, inline=True)
            await log_giveaway(self.bot, embed)
            await send_log(self.bot, embed)

    async def post_public_embed(self, interaction: discord.Interaction):
        guild = interaction.guild
        channel = interaction.channel
        required_role = guild.get_role(self.data["required_role_id"]) if self.data["required_role_id"] else None
        end_timestamp = int((datetime.now(timezone.utc) + timedelta(seconds=self.data["duration"])).timestamp())

        embed = discord.Embed(
            title=f"🎉 {self.data['name']}",
            description=f"**Prize:** {self.data['prize']}\n"
                        f"**Sponsor:** {self.data['sponsor'] or 'N/A'}\n"
                        f"**Host:** <@{self.user_id}>\n"
                        f"**Required Role:** {required_role.mention if required_role else 'None'}\n"
                        f"**Ends:** <t:{end_timestamp}:R> (<t:{end_timestamp}:F>)\n"
                        f"**Entrants:** 0",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc)
        )
        # Bonus entries
        bonus_text = ""
        if self.bonus_entries:
            for role_id, count in self.bonus_entries.items():
                role = guild.get_role(role_id)
                if role:
                    bonus_text += f"{role.mention}: {count} entries\n"
        if bonus_text:
            embed.add_field(name="⭐ Bonus Entries", value=bonus_text, inline=False)
        embed.set_footer(text=f"Giveaway ID: {self.data['giveaway_id']}")

        view = GiveawayPublicView(self.bot, self.data["giveaway_id"])
        message = await channel.send(embed=embed, view=view)
        # Store message ID
        await d1_query(
            "UPDATE giveaways SET channel_id = ?, message_id = ? WHERE giveaway_id = ?",
            [str(channel.id), str(message.id), self.data["giveaway_id"]]
        )

    async def update_public_embed(self, interaction: discord.Interaction):
        # Fetch the existing giveaway message
        row = await d1_query(
            "SELECT channel_id, message_id FROM giveaways WHERE giveaway_id = ?",
            [self.data["giveaway_id"]]
        )
        if not row["results"]:
            return
        channel_id = int(row["results"][0]["channel_id"])
        message_id = int(row["results"][0]["message_id"])
        channel = interaction.guild.get_channel(channel_id)
        if not channel:
            return
        try:
            message = await channel.fetch_message(message_id)
        except:
            return
        # Build new embed
        required_role = interaction.guild.get_role(self.data["required_role_id"]) if self.data["required_role_id"] else None
        end_timestamp = int((datetime.now(timezone.utc) + timedelta(seconds=self.data["duration"])).timestamp())
        # Get current entrants count from DB
        row2 = await d1_query("SELECT entrants FROM giveaways WHERE giveaway_id = ?", [self.data["giveaway_id"]])
        entrants = json.loads(row2["results"][0]["entrants"]) if row2["results"] else []

        embed = discord.Embed(
            title=f"🎉 {self.data['name']}",
            description=f"**Prize:** {self.data['prize']}\n"
                        f"**Sponsor:** {self.data['sponsor'] or 'N/A'}\n"
                        f"**Host:** <@{self.user_id}>\n"
                        f"**Required Role:** {required_role.mention if required_role else 'None'}\n"
                        f"**Ends:** <t:{end_timestamp}:R> (<t:{end_timestamp}:F>)\n"
                        f"**Entrants:** {len(entrants)}",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc)
        )
        bonus_text = ""
        if self.bonus_entries:
            for role_id, count in self.bonus_entries.items():
                role = interaction.guild.get_role(role_id)
                if role:
                    bonus_text += f"{role.mention}: {count} entries\n"
        if bonus_text:
            embed.add_field(name="⭐ Bonus Entries", value=bonus_text, inline=False)
        embed.set_footer(text=f"Giveaway ID: {self.data['giveaway_id']}")

        view = GiveawayPublicView(self.bot, self.data["giveaway_id"])
        await message.edit(embed=embed, view=view)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_user(interaction):
            return
        await interaction.response.send_message("❌ Giveaway builder cancelled.", ephemeral=True)
        for child in self.children:
            child.disabled = True
        await interaction.edit_original_response(view=self)


# ─── Modals for builder ────────────────────────────────────
class GiveawayNameModal(discord.ui.Modal, title="Giveaway Name"):
    def __init__(self, view: GiveawayBuilderView):
        super().__init__()
        self.view = view
        self.name_input = discord.ui.TextInput(
            label="Name",
            placeholder="Enter giveaway name",
            max_length=100,
            required=True,
            default=view.data["name"]
        )
        self.add_item(self.name_input)

    async def on_submit(self, interaction: discord.Interaction):
        self.view.data["name"] = self.name_input.value
        await self.view.update_embed(interaction)


class GiveawaySponsorModal(discord.ui.Modal, title="Sponsor"):
    def __init__(self, view: GiveawayBuilderView):
        super().__init__()
        self.view = view
        self.sponsor_input = discord.ui.TextInput(
            label="Sponsor",
            placeholder="Who is sponsoring this giveaway?",
            max_length=100,
            required=False,
            default=view.data["sponsor"]
        )
        self.add_item(self.sponsor_input)

    async def on_submit(self, interaction: discord.Interaction):
        self.view.data["sponsor"] = self.sponsor_input.value
        await self.view.update_embed(interaction)


class GiveawayPrizeModal(discord.ui.Modal, title="Prize"):
    def __init__(self, view: GiveawayBuilderView):
        super().__init__()
        self.view = view
        self.prize_input = discord.ui.TextInput(
            label="Prize",
            placeholder="What is the prize?",
            max_length=200,
            required=True,
            default=view.data["prize"]
        )
        self.add_item(self.prize_input)

    async def on_submit(self, interaction: discord.Interaction):
        self.view.data["prize"] = self.prize_input.value
        await self.view.update_embed(interaction)


class GiveawayDurationModal(discord.ui.Modal, title="Duration"):
    def __init__(self, view: GiveawayBuilderView):
        super().__init__()
        self.view = view
        self.duration_input = discord.ui.TextInput(
            label="Duration",
            placeholder="e.g., 1h, 2d, 30m",
            max_length=10,
            required=True,
            default=format_duration(view.data["duration"])
        )
        self.add_item(self.duration_input)

    async def on_submit(self, interaction: discord.Interaction):
        seconds = parse_duration(self.duration_input.value)
        if seconds is None:
            return await interaction.response.send_message("❌ Invalid duration format. Use e.g., 1h, 2d, 30m.", ephemeral=True)
        self.view.data["duration"] = seconds
        await self.view.update_embed(interaction)


# ─── Public Giveaway View ─────────────────────────────────
class GiveawayPublicView(discord.ui.View):
    def __init__(self, bot: commands.Bot, giveaway_id: str):
        super().__init__(timeout=None)
        self.bot = bot
        self.giveaway_id = giveaway_id

    @discord.ui.button(label="🎁 Join Giveaway", style=discord.ButtonStyle.success, custom_id="join_giveaway")
    async def join_giveaway(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Check if user has required role
        row = await d1_query(
            "SELECT required_role_id, entrants, status, host_id, giveaway_id FROM giveaways WHERE giveaway_id = ?",
            [self.giveaway_id]
        )
        if not row["results"]:
            return await interaction.response.send_message("❌ Giveaway not found.", ephemeral=True)
        data = row["results"][0]
        if data["status"] != "active":
            return await interaction.response.send_message("⏰ This giveaway has ended.", ephemeral=True)

        required_role_id = int(data["required_role_id"])
        if required_role_id:
            role = interaction.guild.get_role(required_role_id)
            if role and role not in interaction.user.roles:
                return await interaction.response.send_message(f"❌ You need the {role.mention} role to join this giveaway.", ephemeral=True)

        # Check if already entered
        entrants = json.loads(data["entrants"])
        user_id_str = str(interaction.user.id)
        if user_id_str in entrants:
            return await interaction.response.send_message("⚠️ You are already entered in this giveaway!", ephemeral=True)

        # Add user
        entrants.append(user_id_str)
        await d1_query(
            "UPDATE giveaways SET entrants = ? WHERE giveaway_id = ?",
            [json.dumps(entrants), self.giveaway_id]
        )

        # Update the embed with new count
        await self.update_embed(interaction)

        await interaction.response.send_message("✅ You have been entered! Good luck!", ephemeral=True)

        # Log
        embed = discord.Embed(
            title="🎁 User Joined Giveaway",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="Giveaway", value=data["giveaway_id"], inline=True)
        embed.add_field(name="User", value=interaction.user.mention, inline=True)
        await log_giveaway(self.bot, embed)

    async def update_embed(self, interaction: discord.Interaction):
        # Fetch current data
        row = await d1_query(
            "SELECT name, sponsor, prize, host_id, required_role_id, end_time, entrants, bonus_entries FROM giveaways WHERE giveaway_id = ?",
            [self.giveaway_id]
        )
        if not row["results"]:
            return
        data = row["results"][0]
        entrants = json.loads(data["entrants"])
        bonus_entries = json.loads(data["bonus_entries"]) if data["bonus_entries"] else {}
        end_time = datetime.fromisoformat(data["end_time"])
        required_role = interaction.guild.get_role(int(data["required_role_id"])) if data["required_role_id"] else None

        embed = discord.Embed(
            title=f"🎉 {data['name']}",
            description=f"**Prize:** {data['prize']}\n"
                        f"**Sponsor:** {data['sponsor'] or 'N/A'}\n"
                        f"**Host:** <@{data['host_id']}>\n"
                        f"**Required Role:** {required_role.mention if required_role else 'None'}\n"
                        f"**Ends:** <t:{int(end_time.timestamp())}:R> (<t:{int(end_time.timestamp())}:F>)\n"
                        f"**Entrants:** {len(entrants)}",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc)
        )
        bonus_text = ""
        if bonus_entries:
            for role_id, count in bonus_entries.items():
                role = interaction.guild.get_role(int(role_id))
                if role:
                    bonus_text += f"{role.mention}: {count} entries\n"
        if bonus_text:
            embed.add_field(name="⭐ Bonus Entries", value=bonus_text, inline=False)
        embed.set_footer(text=f"Giveaway ID: {self.giveaway_id}")

        # Get the original message
        msg = interaction.message
        await msg.edit(embed=embed, view=self)


# ─── Cog ────────────────────────────────────────────────────
class Giveaway(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.loop.create_task(self.init_db())
        self.check_expired_giveaways.start()

    async def init_db(self):
        await self.bot.wait_until_ready()
        await init_giveaway_db()

    def cog_unload(self):
        self.check_expired_giveaways.cancel()

    @tasks.loop(seconds=60)
    async def check_expired_giveaways(self):
        await self.bot.wait_until_ready()
        now = datetime.now(timezone.utc).isoformat()
        # Find active giveaways with end_time < now
        rows = await d1_query(
            "SELECT giveaway_id FROM giveaways WHERE status = 'active' AND end_time < ?",
            [now]
        )
        for row in rows["results"]:
            await self.end_giveaway(row["giveaway_id"], auto=True)

    async def end_giveaway(self, giveaway_id: str, auto: bool = False, interaction: discord.Interaction = None):
        # Fetch giveaway data
        row = await d1_query(
            "SELECT * FROM giveaways WHERE giveaway_id = ?",
            [giveaway_id]
        )
        if not row["results"]:
            return
        data = row["results"][0]
        if data["status"] != "active":
            return

        entrants = json.loads(data["entrants"])
        if not entrants:
            # No entrants: cancel
            await d1_query("UPDATE giveaways SET status = 'cancelled' WHERE giveaway_id = ?", [giveaway_id])
            await self.update_giveaway_embed(giveaway_id, cancelled=True)
            return

        # Build weighted list
        weights = []
        for user_id in entrants:
            weight = 1
            # Check bonus entries
            bonus_entries = json.loads(data["bonus_entries"]) if data["bonus_entries"] else {}
            for role_id, count in bonus_entries.items():
                role = discord.utils.get(self.bot.get_guild(GUILD_ID).roles, id=int(role_id))
                if role:
                    member = self.bot.get_guild(GUILD_ID).get_member(int(user_id))
                    if member and role in member.roles:
                        weight += count
            weights.append(weight)

        # Pick winner(s) - we'll pick one winner
        winner_id = int(random.choices(entrants, weights=weights, k=1)[0])
        winner = self.bot.get_guild(GUILD_ID).get_member(winner_id)
        winner_mention = winner.mention if winner else f"<@{winner_id}>"

        # Update DB
        await d1_query(
            "UPDATE giveaways SET status = 'ended', winner_ids = ? WHERE giveaway_id = ?",
            [json.dumps([winner_id]), giveaway_id]
        )

        # Update embed
        await self.update_giveaway_embed(giveaway_id, winner_id=winner_id)

        # DM the winner
        try:
            dm_embed = discord.Embed(
                title="🎉 You Won the Giveaway!",
                description=f"You won **{data['prize']}** in the giveaway **{data['name']}**!",
                color=discord.Color.gold(),
                timestamp=datetime.now(timezone.utc)
            )
            dm_embed.add_field(name="Host", value=f"<@{data['host_id']}>", inline=True)
            dm_embed.add_field(name="Giveaway ID", value=giveaway_id, inline=True)
            dm_embed.add_field(name="Instructions", value="Please contact the host to claim your prize.", inline=False)
            if winner:
                await winner.send(embed=dm_embed)
        except:
            pass

        # Ping giveaway ping role
        ping_role = self.bot.get_guild(GUILD_ID).get_role(GIVEAWAY_PING_ROLE_ID)
        channel = self.bot.get_guild(GUILD_ID).get_channel(int(data["channel_id"])) if data["channel_id"] else None
        if channel and ping_role:
            await channel.send(f"{ping_role.mention} 🎉 **{data['name']}** has ended! Winner: {winner_mention}")

        # Log
        embed = discord.Embed(
            title="🏁 Giveaway Ended",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="Giveaway ID", value=giveaway_id, inline=True)
        embed.add_field(name="Name", value=data["name"], inline=True)
        embed.add_field(name="Winner", value=winner_mention, inline=True)
        embed.add_field(name="Total Entrants", value=len(entrants), inline=True)
        embed.add_field(name="Ended By", value="Auto" if auto else interaction.user.mention if interaction else "Manual")
        await log_giveaway(self.bot, embed)
        await send_log(self.bot, embed)

    async def update_giveaway_embed(self, giveaway_id: str, winner_id: int = None, cancelled: bool = False):
        row = await d1_query(
            "SELECT channel_id, message_id, name, prize, sponsor, host_id, status, entrants FROM giveaways WHERE giveaway_id = ?",
            [giveaway_id]
        )
        if not row["results"]:
            return
        data = row["results"][0]
        channel_id = int(data["channel_id"])
        message_id = int(data["message_id"])
        channel = self.bot.get_channel(channel_id)
        if not channel:
            return
        try:
            message = await channel.fetch_message(message_id)
        except:
            return

        entrants = json.loads(data["entrants"])
        embed = discord.Embed(
            title=f"🏆 {data['name']}",
            description=f"**Prize:** {data['prize']}\n"
                        f"**Sponsor:** {data['sponsor'] or 'N/A'}\n"
                        f"**Host:** <@{data['host_id']}>\n"
                        f"**Status:** {'Ended' if winner_id or cancelled else 'Active'}",
            color=discord.Color.gold() if winner_id else discord.Color.red() if cancelled else discord.Color.green(),
            timestamp=datetime.now(timezone.utc)
        )
        if winner_id:
            embed.add_field(name="Winner", value=f"<@{winner_id}>", inline=True)
        if cancelled:
            embed.add_field(name="Cancelled", value="This giveaway was cancelled.", inline=False)
        embed.add_field(name="Total Entrants", value=len(entrants), inline=True)
        embed.set_footer(text=f"Giveaway ID: {giveaway_id}")

        # Disable buttons if ended/cancelled
        view = None
        if winner_id or cancelled:
            view = discord.ui.View(timeout=None)
        else:
            view = GiveawayPublicView(self.bot, giveaway_id)

        await message.edit(embed=embed, view=view)

    # ─── Commands ────────────────────────────────────────────
    @app_commands.command(name="sgiveaway", description="Start a new giveaway (Requires Giveaway Host role)")
    @app_commands.checks.cooldown(1, 10)
    async def sgiveaway(self, interaction: discord.Interaction):
        # Check permission: must have Giveaway Host role or be Founder
        if not any(role.id == GIVEAWAY_HOST_ROLE_ID for role in interaction.user.roles) and not is_founder(interaction.user):
            return await interaction.response.send_message("❌ You need the Giveaway Host role to create giveaways.", ephemeral=True)

        # Ensure they have a role above the default required role (Verified) or are founder
        # We'll just let them set it; they can choose any role.
        # No immediate check needed.

        view = GiveawayBuilderView(self.bot, interaction.user.id, mode="create", interaction=interaction)
        embed = view.build_embed()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="egiveaway", description="Edit an existing giveaway")
    @app_commands.describe(giveaway_id="The giveaway ID to edit")
    @app_commands.checks.cooldown(1, 10)
    async def egiveaway(self, interaction: discord.Interaction, giveaway_id: str):
        # Permission: must be host or Head Staff+ or Founder
        row = await d1_query(
            "SELECT host_id, name, sponsor, prize, required_role_id, bonus_entries, duration, status FROM giveaways WHERE giveaway_id = ?",
            [giveaway_id]
        )
        if not row["results"]:
            return await interaction.response.send_message("❌ Giveaway not found.", ephemeral=True)
        data = row["results"][0]
        if data["status"] != "active":
            return await interaction.response.send_message("❌ This giveaway is not active (ended/cancelled).", ephemeral=True)

        # Permission check
        host_id = int(data["host_id"])
        if interaction.user.id != host_id and not is_head_staff_or_founder(interaction.user):
            return await interaction.response.send_message("❌ You don't have permission to edit this giveaway.", ephemeral=True)

        # Build data dict for builder
        builder_data = {
            "name": data["name"],
            "sponsor": data["sponsor"] or "",
            "prize": data["prize"],
            "required_role_id": int(data["required_role_id"]),
            "bonus_entries": json.loads(data["bonus_entries"]) if data["bonus_entries"] else {},
            "duration": parse_duration(data["duration"]) if data["duration"] else 86400,
            "giveaway_id": giveaway_id,
        }
        # Parse duration from DB (stored as seconds? we need to store duration in seconds, or compute from end_time - created_at)
        # We'll store duration as seconds; we'll add it to DB.
        # Actually we need to store duration in the DB as seconds; we'll add a column later.
        # For now, we can compute from end_time and created_at.
        # Let's assume we have duration stored; if not, we'll compute.
        # I'll add a column 'duration_seconds' later.
        # For simplicity, we'll compute from end_time - created_at.
        row2 = await d1_query("SELECT created_at, end_time FROM giveaways WHERE giveaway_id = ?", [giveaway_id])
        if row2["results"]:
            created_at = datetime.fromisoformat(row2["results"][0]["created_at"])
            end_time = datetime.fromisoformat(row2["results"][0]["end_time"])
            duration_seconds = int((end_time - created_at).total_seconds())
            builder_data["duration"] = duration_seconds

        view = GiveawayBuilderView(self.bot, interaction.user.id, mode="edit", giveaway_data=builder_data, interaction=interaction)
        embed = view.build_embed()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="giveaways", description="List all active giveaways")
    @app_commands.checks.cooldown(1, 10)
    async def giveaways(self, interaction: discord.Interaction):
        rows = await d1_query(
            "SELECT giveaway_id, name, prize, end_time, entrants FROM giveaways WHERE status = 'active' ORDER BY created_at DESC"
        )
        if not rows["results"]:
            return await interaction.response.send_message("📭 No active giveaways at the moment.", ephemeral=True)

        embed = discord.Embed(
            title="🎉 Active Giveaways",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc)
        )
        for row in rows["results"]:
            entrants = json.loads(row["entrants"])
            end_time = datetime.fromisoformat(row["end_time"])
            embed.add_field(
                name=f"**{row['name']}**",
                value=f"🏆 Prize: {row['prize']}\n"
                      f"⏰ Ends: <t:{int(end_time.timestamp())}:R>\n"
                      f"👥 Entrants: {len(entrants)}\n"
                      f"🆔 ID: {row['giveaway_id']}",
                inline=False
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="endgiveaway", description="Force end a giveaway (Head Staff+ only)")
    @app_commands.describe(giveaway_id="The giveaway ID to end")
    @app_commands.checks.cooldown(1, 10)
    async def endgiveaway(self, interaction: discord.Interaction, giveaway_id: str):
        if not is_head_staff_or_founder(interaction.user):
            return await interaction.response.send_message("❌ Only Head Staff and Founder can end giveaways.", ephemeral=True)

        row = await d1_query(
            "SELECT status FROM giveaways WHERE giveaway_id = ?",
            [giveaway_id]
        )
        if not row["results"]:
            return await interaction.response.send_message("❌ Giveaway not found.", ephemeral=True)
        if row["results"][0]["status"] != "active":
            return await interaction.response.send_message("❌ This giveaway is not active.", ephemeral=True)

        await self.end_giveaway(giveaway_id, auto=False, interaction=interaction)
        await interaction.response.send_message(f"✅ Giveaway `{giveaway_id}` ended successfully.", ephemeral=True)

    @app_commands.command(name="reroll", description="Reroll a winner for an ended giveaway")
    @app_commands.describe(giveaway_id="The giveaway ID to reroll")
    @app_commands.checks.cooldown(1, 10)
    async def reroll(self, interaction: discord.Interaction, giveaway_id: str):
        # Permission: Head Staff+ or host
        row = await d1_query(
            "SELECT host_id, status, winner_ids, entrants, bonus_entries, name, prize FROM giveaways WHERE giveaway_id = ?",
            [giveaway_id]
        )
        if not row["results"]:
            return await interaction.response.send_message("❌ Giveaway not found.", ephemeral=True)
        data = row["results"][0]
        if data["status"] != "ended":
            return await interaction.response.send_message("❌ Giveaway must be ended before rerolling.", ephemeral=True)

        host_id = int(data["host_id"])
        if interaction.user.id != host_id and not is_head_staff_or_founder(interaction.user):
            return await interaction.response.send_message("❌ You don't have permission to reroll this giveaway.", ephemeral=True)

        # Check cooldown (60 min)
        last_reroll_row = await d1_query(
            "SELECT last_reroll FROM giveaways WHERE giveaway_id = ?",
            [giveaway_id]
        )
        if last_reroll_row["results"] and last_reroll_row["results"][0]["last_reroll"]:
            last = datetime.fromisoformat(last_reroll_row["results"][0]["last_reroll"])
            if datetime.now(timezone.utc) - last < timedelta(minutes=60):
                remaining = 60 - (datetime.now(timezone.utc) - last).seconds // 60
                return await interaction.response.send_message(f"⏳ Reroll cooldown. Please wait {remaining} minutes.", ephemeral=True)

        entrants = json.loads(data["entrants"])
        if not entrants:
            return await interaction.response.send_message("❌ No entrants to reroll.", ephemeral=True)

        # Exclude previous winners
        previous_winners = json.loads(data["winner_ids"]) if data["winner_ids"] else []
        available = [uid for uid in entrants if uid not in previous_winners]
        if not available:
            return await interaction.response.send_message("❌ No new entrants to reroll.", ephemeral=True)

        # Weighted selection
        weights = []
        bonus_entries = json.loads(data["bonus_entries"]) if data["bonus_entries"] else {}
        for user_id in available:
            weight = 1
            for role_id, count in bonus_entries.items():
                role = discord.utils.get(interaction.guild.roles, id=int(role_id))
                if role:
                    member = interaction.guild.get_member(int(user_id))
                    if member and role in member.roles:
                        weight += count
            weights.append(weight)

        new_winner_id = int(random.choices(available, weights=weights, k=1)[0])
        winner_mention = f"<@{new_winner_id}>"

        # Update winners list
        new_winners = previous_winners + [new_winner_id]
        await d1_query(
            "UPDATE giveaways SET winner_ids = ?, last_reroll = ? WHERE giveaway_id = ?",
            [json.dumps(new_winners), datetime.now(timezone.utc).isoformat(), giveaway_id]
        )

        # Update embed
        await self.update_giveaway_embed(giveaway_id, winner_id=new_winner_id)

        # DM new winner
        winner = interaction.guild.get_member(new_winner_id)
        if winner:
            try:
                dm_embed = discord.Embed(
                    title="🎉 You Won the Giveaway (Reroll)!",
                    description=f"You won **{data['prize']}** in the giveaway **{data['name']}**!",
                    color=discord.Color.gold(),
                    timestamp=datetime.now(timezone.utc)
                )
                dm_embed.add_field(name="Host", value=f"<@{data['host_id']}>", inline=True)
                dm_embed.add_field(name="Giveaway ID", value=giveaway_id, inline=True)
                await winner.send(embed=dm_embed)
            except:
                pass

        # Log
        embed = discord.Embed(
            title="🔄 Giveaway Rerolled",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="Giveaway ID", value=giveaway_id, inline=True)
        embed.add_field(name="New Winner", value=winner_mention, inline=True)
        embed.add_field(name="Rerolled By", value=interaction.user.mention, inline=True)
        await log_giveaway(self.bot, embed)
        await send_log(self.bot, embed)

        await interaction.response.send_message(f"✅ New winner: {winner_mention}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Giveaway(bot))
    print("✅ Giveaway cog loaded")