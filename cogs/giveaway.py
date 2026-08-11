import discord
from discord import app_commands
from discord.ext import commands, tasks
import asyncio
import json
import random
import uuid
import re
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
    is_founder,
    send_log
)

# ─── Constants ──────────────────────────────────────────────
GIVEAWAY_HOST_ROLE_ID = 1536325001584578621
GIVEAWAY_PING_ROLE_ID = 1536324869422063626
GIVEAWAY_LOG_CHANNEL_ID = 1536329270354378783
TICKET_CATEGORY_ID = 1535905899854430222
DEFAULT_WINNERS = 1

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
            channel_id TEXT,
            message_id TEXT,
            bonus_entries TEXT,
            entrants TEXT DEFAULT '[]',
            status TEXT DEFAULT 'active',
            last_reroll TEXT,
            winners_count INTEGER DEFAULT 1
        )"""
    )
    await d1_query(
        """CREATE TABLE IF NOT EXISTS giveaway_tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT NOT NULL,
            giveaway_id TEXT NOT NULL,
            winner_id TEXT NOT NULL,
            status TEXT DEFAULT 'open',
            created_at TEXT NOT NULL,
            closed_at TEXT
        )"""
    )
    for col_sql in [
        "ALTER TABLE giveaways ADD COLUMN required_role_id TEXT",
        "ALTER TABLE giveaways ADD COLUMN last_reroll TEXT",
        "ALTER TABLE giveaways ADD COLUMN winners_count INTEGER DEFAULT 1",
    ]:
        try:
            await d1_query(col_sql)
        except Exception:
            pass


# ─── Helper functions ──────────────────────────────────────
async def log_giveaway(bot: commands.Bot, embed: discord.Embed):
    channel = bot.get_channel(GIVEAWAY_LOG_CHANNEL_ID)
    if channel:
        try:
            await channel.send(embed=embed)
        except Exception as e:
            print(f"Error sending giveaway log: {e}")


def generate_giveaway_id() -> str:
    return f"GIV-{uuid.uuid4().hex[:6].upper()}"


def parse_duration(duration_str: str) -> int:
    duration_str = duration_str.strip().lower()
    if not duration_str:
        return None
    total_seconds = 0
    pattern = re.compile(r'(\d+(?:\.\d+)?)\s*([hdm])')
    matches = pattern.findall(duration_str)
    if not matches:
        try:
            return int(float(duration_str)) * 60
        except:
            return None
    for value, unit in matches:
        val = float(value)
        if unit == 'h':
            total_seconds += val * 3600
        elif unit == 'd':
            total_seconds += val * 86400
        elif unit == 'm':
            total_seconds += val * 60
    return int(total_seconds)


def format_duration(seconds: int) -> str:
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    return " ".join(parts) if parts else "0m"


# ─── Ticket Views ───────────────────────────────────────────
class GiveawayTicketControlView(discord.ui.View):
    def __init__(self, channel_id: int, giveaway_id: str, winner_id: int, host_id: int):
        super().__init__(timeout=None)
        self.channel_id = channel_id
        self.giveaway_id = giveaway_id
        self.winner_id = winner_id
        self.host_id = host_id

    @discord.ui.button(label="✅ Delivered", style=discord.ButtonStyle.success, custom_id="giveaway_ticket_deliver")
    async def deliver_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            allowed = (interaction.user.id == self.host_id or
                       any(role.id in (HEAD_STAFF_ROLE_ID, FOUNDER_ROLE_ID) for role in interaction.user.roles))
            if not allowed:
                return await interaction.response.send_message("❌ Only the host or Head Staff can mark as delivered.", ephemeral=True)

            await interaction.response.send_message(
                "⚠️ Are you sure the prize has been delivered? This will delete the ticket channel.",
                view=ConfirmDeliverView(self.channel_id, self.giveaway_id),
                ephemeral=True
            )
        except discord.NotFound:
            return


class ConfirmDeliverView(discord.ui.View):
    def __init__(self, channel_id: int, giveaway_id: str):
        super().__init__(timeout=60)
        self.channel_id = channel_id
        self.giveaway_id = giveaway_id

    @discord.ui.button(label="Yes, deliver", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        channel = interaction.guild.get_channel(self.channel_id)
        if channel:
            try:
                await channel.delete(reason=f"Giveaway {self.giveaway_id} delivered")
            except Exception as e:
                await interaction.followup.send(f"❌ Failed to delete channel: {e}", ephemeral=True)
                return

        now = datetime.now(timezone.utc).isoformat()
        await d1_query(
            "UPDATE giveaway_tickets SET status = 'closed', closed_at = ? WHERE channel_id = ?",
            [now, str(self.channel_id)]
        )

        await interaction.followup.send("✅ Prize marked as delivered. Ticket channel deleted.", ephemeral=True)

        embed = discord.Embed(
            title="📦 Giveaway Prize Delivered",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="Giveaway ID", value=self.giveaway_id, inline=True)
        embed.add_field(name="Channel", value=f"<#{self.channel_id}>", inline=True)
        embed.add_field(name="Marked by", value=interaction.user.mention, inline=True)
        await log_giveaway(interaction.client, embed)
        await send_log(interaction.client, embed)

        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("✅ Delivery cancelled.", ephemeral=True)
        self.stop()


# ─── Ticket creation ────────────────────────────────────────
async def create_giveaway_ticket(bot: commands.Bot, guild: discord.Guild, giveaway_data: dict, winner_id: int):
    """Create a ticket channel for the giveaway winner."""
    category = guild.get_channel(TICKET_CATEGORY_ID)
    if category:
        print(f"✅ Found ticket category {category.name} ({category.id})")
    else:
        print(f"⚠️ Ticket category {TICKET_CATEGORY_ID} not found. Will create channel without category.")

    host = guild.get_member(int(giveaway_data["host_id"]))
    winner = guild.get_member(winner_id)
    sponsor_name = giveaway_data.get("sponsor") or "N/A"

    winner_name = winner.display_name if winner else str(winner_id)
    channel_name = f"giveaway-{winner_name[:10]}-{giveaway_data['giveaway_id'][-4:]}".lower().replace(" ", "-")

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
    }
    if host:
        overwrites[host] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    if winner:
        overwrites[winner] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

    head_staff_role = guild.get_role(HEAD_STAFF_ROLE_ID)
    if head_staff_role:
        overwrites[head_staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

    host_role = guild.get_role(GIVEAWAY_HOST_ROLE_ID)
    if host_role:
        overwrites[host_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

    try:
        if category:
            channel = await guild.create_text_channel(
                name=channel_name,
                category=category,
                overwrites=overwrites,
                reason=f"Giveaway ticket for {giveaway_data['giveaway_id']}"
            )
        else:
            channel = await guild.create_text_channel(
                name=channel_name,
                overwrites=overwrites,
                reason=f"Giveaway ticket for {giveaway_data['giveaway_id']} (no category)"
            )
        print(f"✅ Created ticket channel {channel.name} ({channel.id})")
    except Exception as e:
        print(f"❌ Failed to create ticket channel: {e}")
        original_channel = guild.get_channel(int(giveaway_data["channel_id"]))
        if original_channel:
            await original_channel.send(
                f"⚠️ Could not create a ticket for the winner. Please contact staff. (Error: {e})"
            )
        return

    now = datetime.now(timezone.utc).isoformat()
    try:
        await d1_query(
            "INSERT INTO giveaway_tickets (channel_id, giveaway_id, winner_id, status, created_at) VALUES (?, ?, ?, ?, ?)",
            [str(channel.id), giveaway_data["giveaway_id"], str(winner_id), "open", now]
        )
    except Exception as e:
        print(f"❌ Failed to insert ticket into DB: {e}")
        try:
            await channel.delete(reason="Failed to insert ticket into DB")
        except:
            pass
        return

    embed = discord.Embed(
        title="🎉 Giveaway Ticket",
        description=f"**Giveaway:** {giveaway_data['name']}\n"
                    f"**Prize:** {giveaway_data['prize']}\n"
                    f"**Sponsor:** {sponsor_name}\n"
                    f"**Host:** <@{giveaway_data['host_id']}>\n"
                    f"**Winner:** <@{winner_id}>\n"
                    f"**Total Entrants:** {len(json.loads(giveaway_data['entrants']))}\n"
                    f"**Giveaway ID:** {giveaway_data['giveaway_id']}",
        color=discord.Color.gold(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_footer(text="Click 'Delivered' when the prize has been claimed.")
    view = GiveawayTicketControlView(
        channel_id=channel.id,
        giveaway_id=giveaway_data["giveaway_id"],
        winner_id=winner_id,
        host_id=int(giveaway_data["host_id"])
    )
    await channel.send(embed=embed, view=view)

    mention_parts = []
    if host:
        mention_parts.append(host.mention)
    if winner:
        mention_parts.append(winner.mention)
    if head_staff_role:
        mention_parts.append(head_staff_role.mention)
    if mention_parts:
        await channel.send(f"{' '.join(mention_parts)} – Ticket opened for prize delivery.")


# ─── Restore tickets on startup ──────────────────────────
async def restore_giveaway_tickets(bot: commands.Bot):
    await bot.wait_until_ready()
    await asyncio.sleep(2)

    try:
        rows = await d1_query(
            "SELECT channel_id, giveaway_id, winner_id FROM giveaway_tickets WHERE status = 'open'"
        )
    except Exception as e:
        print(f"Error querying giveaway_tickets: {e}")
        return

    restored = 0
    for row in rows["results"]:
        channel_id = int(row["channel_id"])
        giveaway_id = row["giveaway_id"]
        winner_id = int(row["winner_id"])
        g_row = await d1_query(
            "SELECT host_id FROM giveaways WHERE giveaway_id = ?",
            [giveaway_id]
        )
        if not g_row["results"]:
            continue
        host_id = int(g_row["results"][0]["host_id"])
        channel = bot.get_channel(channel_id)
        if not channel:
            continue
        try:
            async for message in channel.history(limit=50):
                if message.author == bot.user and message.embeds:
                    if message.embeds and message.embeds[0].title and "Giveaway Ticket" in message.embeds[0].title:
                        view = GiveawayTicketControlView(
                            channel_id=channel_id,
                            giveaway_id=giveaway_id,
                            winner_id=winner_id,
                            host_id=host_id
                        )
                        await message.edit(view=view)
                        restored += 1
                        break
        except Exception as e:
            print(f"Error restoring giveaway ticket {giveaway_id}: {e}")
    print(f"✅ Restored {restored} giveaway ticket views.")


# ─── Bonus Entry Views ────────────────────────────────────
class BonusEntryModal(discord.ui.Modal, title="Add Bonus Entry"):
    def __init__(self, builder_view: "GiveawayBuilderView"):
        super().__init__()
        self.builder_view = builder_view
        self.role_id_input = discord.ui.TextInput(
            label="Role ID",
            placeholder="Paste the role ID",
            required=True,
            max_length=20
        )
        self.count_input = discord.ui.TextInput(
            label="Bonus Entries Count",
            placeholder="e.g., 2",
            required=True,
            max_length=10,
            default="1"
        )
        self.add_item(self.role_id_input)
        self.add_item(self.count_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            role_id = int(self.role_id_input.value)
            count = int(self.count_input.value)
            if count < 0:
                raise ValueError
        except ValueError:
            return await interaction.response.send_message("❌ Invalid role ID or count.", ephemeral=True)

        role = interaction.guild.get_role(role_id)
        if not role:
            return await interaction.response.send_message("❌ Role not found in this server.", ephemeral=True)

        self.builder_view.bonus_entries[role_id] = count
        await self.builder_view.update_embed(interaction)
        await interaction.followup.send(f"✅ Added {role.mention} with {count} bonus entries.", ephemeral=True)


class RemoveBonusView(discord.ui.View):
    def __init__(self, builder_view: "GiveawayBuilderView"):
        super().__init__(timeout=60)
        self.builder_view = builder_view
        options = []
        for role_id, count in builder_view.bonus_entries.items():
            role = builder_view.bot.get_guild(GUILD_ID).get_role(role_id)
            if role:
                options.append(discord.SelectOption(
                    label=role.name[:100],
                    value=str(role_id),
                    description=f"Bonus: {count}"
                ))
        if options:
            select = discord.ui.Select(
                placeholder="Select a bonus entry to remove...",
                min_values=1,
                max_values=1,
                options=options[:25]
            )
            select.callback = self._remove_callback
            self.add_item(select)
        else:
            self.add_item(discord.ui.Button(label="No bonus entries to remove", disabled=True, style=discord.ButtonStyle.secondary))

    async def _remove_callback(self, interaction: discord.Interaction):
        select = self.children[0]
        role_id = int(select.values[0])
        if role_id in self.builder_view.bonus_entries:
            del self.builder_view.bonus_entries[role_id]
            await self.builder_view.update_embed(interaction)
            await interaction.response.send_message("✅ Removed bonus entry.", ephemeral=True)
            self.stop()
        else:
            await interaction.response.send_message("❌ Role not found.", ephemeral=True)


# ─── Giveaway Builder View ─────────────────────────────────
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
            "duration": 86400,
            "required_role_id": VERIFIED_ROLE_ID,
            "bonus_entries": {},
            "giveaway_id": None,
            "winners": DEFAULT_WINNERS,
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
        embed.add_field(
            name="Winners",
            value=str(self.data["winners"]),
            inline=True
        )
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

    @discord.ui.button(label="Change Winners", style=discord.ButtonStyle.primary)
    async def change_winners(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_user(interaction):
            return
        modal = discord.ui.Modal(title="Number of Winners")
        modal.add_item(discord.ui.TextInput(
            label="Winners Count",
            placeholder="Enter number of winners (1-10)",
            max_length=2,
            required=True,
            default=str(self.data["winners"])
        ))
        async def on_submit(interaction: discord.Interaction):
            try:
                val = int(modal.children[0].value)
                if val < 1 or val > 10:
                    raise ValueError
                self.data["winners"] = val
                await self.update_embed(interaction)
            except:
                await interaction.response.send_message("❌ Invalid number. Please enter 1-10.", ephemeral=True)
        modal.on_submit = on_submit
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Add Bonus Entry", style=discord.ButtonStyle.primary)
    async def add_bonus(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_user(interaction):
            return
        await interaction.response.send_modal(BonusEntryModal(self))

    @discord.ui.button(label="Remove Bonus Entry", style=discord.ButtonStyle.danger)
    async def remove_bonus(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_user(interaction):
            return
        if not self.bonus_entries:
            return await interaction.response.send_message("❌ No bonus entries to remove.", ephemeral=True)
        view = RemoveBonusView(self)
        await interaction.response.send_message("Select a bonus entry to remove:", view=view, ephemeral=True)

    @discord.ui.button(label="Preview", style=discord.ButtonStyle.secondary)
    async def preview(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_user(interaction):
            return
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
                        f"**Entrants:** 0\n"
                        f"**Winners:** {self.data['winners']}",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc)
        )
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

        if not self.data["name"]:
            return await interaction.response.send_message("❌ Please set a name.", ephemeral=True)
        if not self.data["prize"]:
            return await interaction.response.send_message("❌ Please set a prize.", ephemeral=True)
        if not self.data["duration"]:
            return await interaction.response.send_message("❌ Please set a duration.", ephemeral=True)
        if self.data["winners"] < 1 or self.data["winners"] > 10:
            return await interaction.response.send_message("❌ Winners count must be between 1 and 10.", ephemeral=True)

        giveaway_id = self.data.get("giveaway_id")
        if self.mode == "create":
            giveaway_id = generate_giveaway_id()
            self.data["giveaway_id"] = giveaway_id

            ping_role = interaction.guild.get_role(GIVEAWAY_PING_ROLE_ID)
            if ping_role:
                await interaction.channel.send(f"{ping_role.mention} 🎉 New giveaway starting!")

            await self.post_public_embed(interaction, giveaway_id)

            now = datetime.now(timezone.utc).isoformat()
            end_time = (datetime.now(timezone.utc) + timedelta(seconds=self.data["duration"])).isoformat()
            await d1_query(
                """INSERT INTO giveaways
                (giveaway_id, name, sponsor, prize, host_id, required_role_id, created_at, end_time,
                 status, bonus_entries, entrants, channel_id, message_id, winners_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    "0",
                    "0",
                    self.data["winners"]
                ]
            )
            await interaction.response.send_message(f"✅ Giveaway posted! ID: {giveaway_id}", ephemeral=True)
            embed = discord.Embed(
                title="🎉 Giveaway Created",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="Giveaway ID", value=giveaway_id, inline=True)
            embed.add_field(name="Name", value=self.data["name"], inline=True)
            embed.add_field(name="Host", value=f"<@{self.user_id}>", inline=True)
            embed.add_field(name="Winners", value=str(self.data["winners"]), inline=True)
            await log_giveaway(self.bot, embed)
            await send_log(self.bot, embed)
        else:
            # Edit mode
            await self.update_public_embed(interaction, giveaway_id)
            end_time = (datetime.now(timezone.utc) + timedelta(seconds=self.data["duration"])).isoformat()
            await d1_query(
                """UPDATE giveaways SET
                    name = ?, sponsor = ?, prize = ?, required_role_id = ?,
                    end_time = ?, bonus_entries = ?, winners_count = ?
                    WHERE giveaway_id = ?""",
                [
                    self.data["name"],
                    self.data["sponsor"] or "",
                    self.data["prize"],
                    str(self.data["required_role_id"]),
                    end_time,
                    json.dumps(self.bonus_entries),
                    self.data["winners"],
                    giveaway_id
                ]
            )
            await interaction.response.send_message(f"✅ Giveaway updated! ID: {giveaway_id}", ephemeral=True)
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

    async def post_public_embed(self, interaction: discord.Interaction, giveaway_id: str):
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
                        f"**Entrants:** 0\n"
                        f"**Winners:** {self.data['winners']}",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc)
        )
        bonus_text = ""
        if self.bonus_entries:
            for role_id, count in self.bonus_entries.items():
                role = guild.get_role(role_id)
                if role:
                    bonus_text += f"{role.mention}: {count} entries\n"
        if bonus_text:
            embed.add_field(name="⭐ Bonus Entries", value=bonus_text, inline=False)
        embed.set_footer(text=f"Giveaway ID: {giveaway_id}")

        view = GiveawayPublicView(self.bot, giveaway_id)
        message = await channel.send(embed=embed, view=view)
        await d1_query(
            "UPDATE giveaways SET channel_id = ?, message_id = ? WHERE giveaway_id = ?",
            [str(channel.id), str(message.id), giveaway_id]
        )

    async def update_public_embed(self, interaction: discord.Interaction, giveaway_id: str):
        row = await d1_query(
            "SELECT channel_id, message_id FROM giveaways WHERE giveaway_id = ?",
            [giveaway_id]
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

        required_role = interaction.guild.get_role(self.data["required_role_id"]) if self.data["required_role_id"] else None
        end_timestamp = int((datetime.now(timezone.utc) + timedelta(seconds=self.data["duration"])).timestamp())
        row2 = await d1_query("SELECT entrants, winners_count FROM giveaways WHERE giveaway_id = ?", [giveaway_id])
        entrants = json.loads(row2["results"][0]["entrants"]) if row2["results"] else []
        winners_count = row2["results"][0]["winners_count"] if row2["results"] else DEFAULT_WINNERS

        embed = discord.Embed(
            title=f"🎉 {self.data['name']}",
            description=f"**Prize:** {self.data['prize']}\n"
                        f"**Sponsor:** {self.data['sponsor'] or 'N/A'}\n"
                        f"**Host:** <@{self.user_id}>\n"
                        f"**Required Role:** {required_role.mention if required_role else 'None'}\n"
                        f"**Ends:** <t:{end_timestamp}:R> (<t:{end_timestamp}:F>)\n"
                        f"**Entrants:** {len(entrants)}\n"
                        f"**Winners:** {winners_count}",
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
        embed.set_footer(text=f"Giveaway ID: {giveaway_id}")

        view = GiveawayPublicView(self.bot, giveaway_id)
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
        default_val = format_duration(view.data["duration"]) if view.data["duration"] else "1d"
        self.duration_input = discord.ui.TextInput(
            label="Duration",
            placeholder="e.g., 1h, 2d, 30m, 1d 12h",
            max_length=20,
            required=True,
            default=default_val
        )
        self.add_item(self.duration_input)

    async def on_submit(self, interaction: discord.Interaction):
        seconds = parse_duration(self.duration_input.value)
        if seconds is None or seconds <= 0:
            return await interaction.response.send_message("❌ Invalid duration format. Use e.g., 1h, 2d, 30m, 1d 12h.", ephemeral=True)
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
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.NotFound:
            return

        row = await d1_query(
            "SELECT required_role_id, entrants, status, host_id, giveaway_id, winners_count FROM giveaways WHERE giveaway_id = ?",
            [self.giveaway_id]
        )
        if not row["results"]:
            return await interaction.followup.send("❌ Giveaway not found.", ephemeral=True)
        data = row["results"][0]
        if data["status"] != "active":
            return await interaction.followup.send("⏰ This giveaway has ended.", ephemeral=True)

        required_role_id = int(data["required_role_id"]) if data["required_role_id"] else 0
        if required_role_id:
            role = interaction.guild.get_role(required_role_id)
            if role and role not in interaction.user.roles:
                return await interaction.followup.send(f"❌ You need the {role.mention} role to join this giveaway.", ephemeral=True)

        entrants = json.loads(data["entrants"])
        user_id_str = str(interaction.user.id)
        if user_id_str in entrants:
            return await interaction.followup.send("⚠️ You are already entered in this giveaway!", ephemeral=True)

        entrants.append(user_id_str)
        await d1_query(
            "UPDATE giveaways SET entrants = ? WHERE giveaway_id = ?",
            [json.dumps(entrants), self.giveaway_id]
        )

        await self.update_embed(interaction)
        await interaction.followup.send("✅ You have been entered! Good luck!", ephemeral=True)

        embed = discord.Embed(
            title="🎁 User Joined Giveaway",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="Giveaway", value=data["giveaway_id"], inline=True)
        embed.add_field(name="User", value=interaction.user.mention, inline=True)
        await log_giveaway(self.bot, embed)

    async def update_embed(self, interaction: discord.Interaction):
        row = await d1_query(
            "SELECT name, sponsor, prize, host_id, required_role_id, end_time, entrants, bonus_entries, winners_count FROM giveaways WHERE giveaway_id = ?",
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
                        f"**Entrants:** {len(entrants)}\n"
                        f"**Winners:** {data['winners_count'] or 1}",
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

        msg = interaction.message
        await msg.edit(embed=embed, view=self)


# ─── Cog ────────────────────────────────────────────────────
class Giveaway(commands.Cog):
    giveaway = app_commands.Group(name="giveaway", description="Giveaway commands")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.loop.create_task(self.init_db())
        self.bot.loop.create_task(self.restore_tickets())
        self.check_expired_giveaways.start()

    async def init_db(self):
        await self.bot.wait_until_ready()
        await init_giveaway_db()

    async def restore_tickets(self):
        await self.bot.wait_until_ready()
        await restore_giveaway_tickets(self.bot)

    def cog_unload(self):
        self.check_expired_giveaways.cancel()

    @tasks.loop(seconds=60)
    async def check_expired_giveaways(self):
        await self.bot.wait_until_ready()
        now = datetime.now(timezone.utc).isoformat()
        rows = await d1_query(
            "SELECT giveaway_id FROM giveaways WHERE status = 'active' AND end_time < ?",
            [now]
        )
        for row in rows["results"]:
            await self.end_giveaway(row["giveaway_id"], auto=True)

    async def pick_winners(self, entrants: list, bonus_entries: dict, winners_count: int, exclude: list = None):
        if exclude is None:
            exclude = []
        available = [uid for uid in entrants if uid not in exclude]
        if not available:
            return []
        weights = []
        for user_id in available:
            weight = 1
            for role_id, count in bonus_entries.items():
                role = discord.utils.get(self.bot.get_guild(GUILD_ID).roles, id=int(role_id))
                if role:
                    member = self.bot.get_guild(GUILD_ID).get_member(int(user_id))
                    if member and role in member.roles:
                        weight += count
            weights.append(weight)
        winners = []
        temp_available = available.copy()
        temp_weights = weights.copy()
        for _ in range(min(winners_count, len(temp_available))):
            idx = random.choices(range(len(temp_available)), weights=temp_weights, k=1)[0]
            winners.append(int(temp_available[idx]))
            temp_available.pop(idx)
            temp_weights.pop(idx)
        return winners

    async def end_giveaway(self, giveaway_id: str, auto: bool = False, early: bool = False, interaction: discord.Interaction = None):
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
        winners_count = int(data.get("winners_count") or 1)
        bonus_entries = json.loads(data["bonus_entries"]) if data["bonus_entries"] else {}

        if not entrants:
            await d1_query("UPDATE giveaways SET status = 'cancelled' WHERE giveaway_id = ?", [giveaway_id])
            await self.update_giveaway_embed(giveaway_id, cancelled=True)
            return

        winners = await self.pick_winners(entrants, bonus_entries, winners_count)
        if not winners:
            await d1_query("UPDATE giveaways SET status = 'cancelled' WHERE giveaway_id = ?", [giveaway_id])
            await self.update_giveaway_embed(giveaway_id, cancelled=True)
            return

        await d1_query(
            "UPDATE giveaways SET status = 'ended', winner_ids = ? WHERE giveaway_id = ?",
            [json.dumps(winners), giveaway_id]
        )

        await self.update_giveaway_embed(giveaway_id, winners=winners, early=early)

        channel = self.bot.get_guild(GUILD_ID).get_channel(int(data["channel_id"])) if data["channel_id"] else None
        if channel:
            winner_mentions = ", ".join([f"<@{w}>" for w in winners])
            embed = discord.Embed(
                title="🏆 Giveaway Ended!",
                description=f"**Giveaway:** {data['name']}\n"
                            f"**Prize:** {data['prize']}\n"
                            f"**Winner(s):** {winner_mentions}\n"
                            f"**Total Entrants:** {len(entrants)}\n"
                            f"**Ended:** {'Early' if early else 'Automatically'}",
                color=discord.Color.gold() if not early else discord.Color.orange(),
                timestamp=datetime.now(timezone.utc)
            )
            await channel.send(embed=embed)

        for winner_id in winners:
            await create_giveaway_ticket(self.bot, self.bot.get_guild(GUILD_ID), data, winner_id)

        for winner_id in winners:
            winner = self.bot.get_guild(GUILD_ID).get_member(winner_id)
            if winner:
                try:
                    dm_embed = discord.Embed(
                        title="🎉 You Won the Giveaway!",
                        description=f"You won **{data['prize']}** in the giveaway **{data['name']}**!",
                        color=discord.Color.gold(),
                        timestamp=datetime.now(timezone.utc)
                    )
                    dm_embed.add_field(name="Host", value=f"<@{data['host_id']}>", inline=True)
                    dm_embed.add_field(name="Giveaway ID", value=giveaway_id, inline=True)
                    dm_embed.add_field(name="Ticket", value="A ticket has been opened for you to claim your prize.", inline=False)
                    await winner.send(embed=dm_embed)
                except Exception as e:
                    print(f"Could not DM winner {winner_id}: {e}")

        ping_role = self.bot.get_guild(GUILD_ID).get_role(GIVEAWAY_PING_ROLE_ID)
        if channel and ping_role:
            await channel.send(f"{ping_role.mention} 🎉 The giveaway has ended!")

        log_embed = discord.Embed(
            title="🏁 Giveaway Ended" + (" Early" if early else ""),
            color=discord.Color.gold() if not early else discord.Color.orange(),
            timestamp=datetime.now(timezone.utc)
        )
        log_embed.add_field(name="Giveaway ID", value=giveaway_id, inline=True)
        log_embed.add_field(name="Name", value=data["name"], inline=True)
        log_embed.add_field(name="Winners", value=", ".join([f"<@{w}>" for w in winners]), inline=False)
        log_embed.add_field(name="Total Entrants", value=len(entrants), inline=True)
        log_embed.add_field(name="Ended By", value="Auto" if auto else interaction.user.mention if interaction else "Manual")
        await log_giveaway(self.bot, log_embed)
        await send_log(self.bot, log_embed)

    async def update_giveaway_embed(self, giveaway_id: str, winners: list = None, cancelled: bool = False, early: bool = False):
        row = await d1_query(
            "SELECT channel_id, message_id, name, prize, sponsor, host_id, status, entrants, winners_count FROM giveaways WHERE giveaway_id = ?",
            [giveaway_id]
        )
        if not row["results"]:
            return
        data = row["results"][0]
        if not data["channel_id"] or not data["message_id"]:
            return
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
        if cancelled:
            embed = discord.Embed(
                title=f"🚫 {data['name']} (Cancelled)",
                description=f"**Prize:** {data['prize']}\n"
                            f"**Sponsor:** {data['sponsor'] or 'N/A'}\n"
                            f"**Host:** <@{data['host_id']}>\n"
                            f"**Status:** Cancelled",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc)
            )
        elif winners is not None:
            winner_mentions = ", ".join([f"<@{w}>" for w in winners])
            embed = discord.Embed(
                title=f"🏆 {data['name']} ({'Ended Early' if early else 'Ended'})",
                description=f"**Prize:** {data['prize']}\n"
                            f"**Sponsor:** {data['sponsor'] or 'N/A'}\n"
                            f"**Host:** <@{data['host_id']}>\n"
                            f"**Status:** {'Ended Early' if early else 'Ended'}\n"
                            f"**Winner(s):** {winner_mentions}",
                color=discord.Color.orange() if early else discord.Color.gold(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="Total Entrants", value=len(entrants), inline=True)
        else:
            embed = discord.Embed(
                title=f"🎉 {data['name']}",
                description=f"**Prize:** {data['prize']}\n"
                            f"**Sponsor:** {data['sponsor'] or 'N/A'}\n"
                            f"**Host:** <@{data['host_id']}>\n"
                            f"**Status:** Active",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc)
            )

        embed.set_footer(text=f"Giveaway ID: {giveaway_id}")

        view = None
        if winners is not None or cancelled:
            view = discord.ui.View(timeout=None)
        else:
            view = GiveawayPublicView(self.bot, giveaway_id)

        await message.edit(embed=embed, view=view)

    # ─── Commands ────────────────────────────────────────────
    @giveaway.command(name="start", description="Start a new giveaway (Requires Giveaway Host role)")
    @app_commands.checks.cooldown(1, 10)
    async def giveaway_start(self, interaction: discord.Interaction):
        # Immediate defer to avoid timeout
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            else:
                await interaction.followup.send("Something went wrong, please try again.", ephemeral=True)
                return
        except discord.NotFound:
            # Interaction expired, can't respond
            return

        if not any(role.id == GIVEAWAY_HOST_ROLE_ID for role in interaction.user.roles) and not is_founder(interaction.user):
            return await interaction.followup.send("❌ You need the Giveaway Host role to create giveaways.", ephemeral=True)

        view = GiveawayBuilderView(self.bot, interaction.user.id, mode="create", interaction=interaction)
        embed = view.build_embed()
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @giveaway.command(name="edit", description="Edit an existing giveaway")
    @app_commands.describe(giveaway_id="The giveaway ID to edit")
    @app_commands.checks.cooldown(1, 10)
    async def giveaway_edit(self, interaction: discord.Interaction, giveaway_id: str):
        await interaction.response.defer(ephemeral=True)

        row = await d1_query(
            "SELECT host_id, name, sponsor, prize, required_role_id, bonus_entries, status, winners_count FROM giveaways WHERE giveaway_id = ?",
            [giveaway_id]
        )
        if not row["results"]:
            return await interaction.followup.send("❌ Giveaway not found.", ephemeral=True)
        data = row["results"][0]
        if data["status"] != "active":
            return await interaction.followup.send("❌ This giveaway is not active (ended/cancelled).", ephemeral=True)

        host_id = int(data["host_id"])
        if interaction.user.id != host_id and not is_head_staff_or_founder(interaction.user):
            return await interaction.followup.send("❌ You don't have permission to edit this giveaway.", ephemeral=True)

        row2 = await d1_query("SELECT created_at, end_time FROM giveaways WHERE giveaway_id = ?", [giveaway_id])
        if row2["results"]:
            created_at = datetime.fromisoformat(row2["results"][0]["created_at"])
            end_time = datetime.fromisoformat(row2["results"][0]["end_time"])
            duration_seconds = int((end_time - created_at).total_seconds())
        else:
            duration_seconds = 86400

        builder_data = {
            "name": data["name"],
            "sponsor": data["sponsor"] or "",
            "prize": data["prize"],
            "required_role_id": int(data["required_role_id"]),
            "bonus_entries": json.loads(data["bonus_entries"]) if data["bonus_entries"] else {},
            "duration": duration_seconds,
            "giveaway_id": giveaway_id,
            "winners": data.get("winners_count") or DEFAULT_WINNERS,
        }
        view = GiveawayBuilderView(self.bot, interaction.user.id, mode="edit", giveaway_data=builder_data, interaction=interaction)
        embed = view.build_embed()
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @giveaway.command(name="list", description="List all active giveaways")
    @app_commands.checks.cooldown(1, 10)
    async def giveaway_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        rows = await d1_query(
            "SELECT giveaway_id, name, prize, end_time, entrants FROM giveaways WHERE status = 'active' ORDER BY created_at DESC"
        )
        if not rows["results"]:
            return await interaction.followup.send("📭 No active giveaways at the moment.", ephemeral=True)

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
        await interaction.followup.send(embed=embed, ephemeral=True)

    @giveaway.command(name="end", description="Force end a giveaway early (Head Staff+ only)")
    @app_commands.describe(giveaway_id="The giveaway ID to end")
    @app_commands.checks.cooldown(1, 10)
    async def giveaway_end(self, interaction: discord.Interaction, giveaway_id: str):
        await interaction.response.defer(ephemeral=True)
        if not is_head_staff_or_founder(interaction.user):
            return await interaction.followup.send("❌ Only Head Staff and Founder can end giveaways.", ephemeral=True)

        row = await d1_query(
            "SELECT status FROM giveaways WHERE giveaway_id = ?",
            [giveaway_id]
        )
        if not row["results"]:
            return await interaction.followup.send("❌ Giveaway not found.", ephemeral=True)
        if row["results"][0]["status"] != "active":
            return await interaction.followup.send("❌ This giveaway is not active.", ephemeral=True)

        await self.end_giveaway(giveaway_id, auto=False, early=True, interaction=interaction)
        await interaction.followup.send(f"✅ Giveaway `{giveaway_id}` ended early.", ephemeral=True)

    @giveaway.command(name="reroll", description="Pick a new winner for an ended giveaway")
    @app_commands.describe(giveaway_id="The giveaway ID to reroll", user="Optional: replace a specific winner")
    @app_commands.checks.cooldown(1, 10)
    async def giveaway_reroll(self, interaction: discord.Interaction, giveaway_id: str, user: discord.Member = None):
        await interaction.response.defer(ephemeral=True)

        row = await d1_query(
            "SELECT host_id, status, winner_ids, entrants, bonus_entries, name, prize, winners_count FROM giveaways WHERE giveaway_id = ?",
            [giveaway_id]
        )
        if not row["results"]:
            return await interaction.followup.send("❌ Giveaway not found.", ephemeral=True)
        data = row["results"][0]
        if data["status"] != "ended":
            return await interaction.followup.send("❌ Giveaway must be ended before rerolling.", ephemeral=True)

        host_id = int(data["host_id"])
        if interaction.user.id != host_id and not is_head_staff_or_founder(interaction.user):
            return await interaction.followup.send("❌ You don't have permission to reroll this giveaway.", ephemeral=True)

        last_reroll_row = await d1_query(
            "SELECT last_reroll FROM giveaways WHERE giveaway_id = ?",
            [giveaway_id]
        )
        if last_reroll_row["results"] and last_reroll_row["results"][0]["last_reroll"]:
            last = datetime.fromisoformat(last_reroll_row["results"][0]["last_reroll"])
            if datetime.now(timezone.utc) - last < timedelta(minutes=60):
                remaining = 60 - (datetime.now(timezone.utc) - last).seconds // 60
                return await interaction.followup.send(f"⏳ Reroll cooldown. Please wait {remaining} minutes.", ephemeral=True)

        winners = json.loads(data["winner_ids"]) if data["winner_ids"] else []
        entrants = json.loads(data["entrants"])
        bonus_entries = json.loads(data["bonus_entries"]) if data["bonus_entries"] else {}

        if user:
            if user.id not in winners:
                return await interaction.followup.send(f"❌ {user.mention} is not a winner of this giveaway.", ephemeral=True)
            winners.remove(user.id)
            available = [uid for uid in entrants if uid not in winners and uid != user.id]
            if not available:
                return await interaction.followup.send("❌ No new entrants available to reroll.", ephemeral=True)
            weights = []
            for uid in available:
                weight = 1
                for role_id, count in bonus_entries.items():
                    role = discord.utils.get(interaction.guild.roles, id=int(role_id))
                    if role:
                        member = interaction.guild.get_member(int(uid))
                        if member and role in member.roles:
                            weight += count
                weights.append(weight)
            idx = random.choices(range(len(available)), weights=weights, k=1)[0]
            new_winner = int(available[idx])
            winners.append(new_winner)
            new_winner_mention = f"<@{new_winner}>"
            replaced_user_mention = user.mention
            message = f"🔄 Replaced {replaced_user_mention} with {new_winner_mention} as the new winner."
        else:
            winners_count = int(data.get("winners_count") or 1)
            new_winners = await self.pick_winners(entrants, bonus_entries, winners_count, exclude=winners)
            if not new_winners:
                return await interaction.followup.send("❌ No new winners could be picked.", ephemeral=True)
            winners = new_winners
            new_winner_mentions = ", ".join([f"<@{w}>" for w in winners])
            message = f"🔄 New winner(s) picked: {new_winner_mentions}"

        await d1_query(
            "UPDATE giveaways SET winner_ids = ?, last_reroll = ? WHERE giveaway_id = ?",
            [json.dumps(winners), datetime.now(timezone.utc).isoformat(), giveaway_id]
        )

        await self.update_giveaway_embed(giveaway_id, winners=winners)

        channel = interaction.guild.get_channel(int(data["channel_id"])) if data.get("channel_id") else None
        if channel:
            embed = discord.Embed(
                title="🔄 Giveaway Rerolled",
                description=message,
                color=discord.Color.gold(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="Giveaway", value=data["name"], inline=True)
            embed.add_field(name="Prize", value=data["prize"], inline=True)
            await channel.send(embed=embed)

        for w in winners:
            if user and w == new_winner:
                member = interaction.guild.get_member(w)
                if member:
                    try:
                        dm_embed = discord.Embed(
                            title="🎉 You Won the Giveaway (Reroll)!",
                            description=f"You won **{data['prize']}** in the giveaway **{data['name']}**!",
                            color=discord.Color.gold(),
                            timestamp=datetime.now(timezone.utc)
                        )
                        dm_embed.add_field(name="Host", value=f"<@{data['host_id']}>", inline=True)
                        dm_embed.add_field(name="Giveaway ID", value=giveaway_id, inline=True)
                        dm_embed.add_field(name="Ticket", value="A ticket has been opened for you to claim your prize.", inline=False)
                        await member.send(embed=dm_embed)
                    except:
                        pass

        log_embed = discord.Embed(
            title="🔄 Giveaway Rerolled",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc)
        )
        log_embed.add_field(name="Giveaway ID", value=giveaway_id, inline=True)
        log_embed.add_field(name="New Winner(s)", value=", ".join([f"<@{w}>" for w in winners]), inline=True)
        log_embed.add_field(name="Rerolled By", value=interaction.user.mention, inline=True)
        await log_giveaway(self.bot, log_embed)
        await send_log(self.bot, log_embed)

        await interaction.followup.send(f"✅ Rerolled winners!", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Giveaway(bot))
    print("✅ Giveaway cog loaded")