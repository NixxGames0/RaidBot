"""
Announcement system — /announce opens a title + body editor and posts the
announcement to the server announcements channel with the author and time.
"""

import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone

from bot import GUILD_ID, is_staff

# The announcements channel every /announce is posted to.
ANNOUNCE_CHANNEL_ID = 1543481308150243429

# Colour used for announcement embeds.
ANNOUNCE_COLOR = discord.Color.brand_green()


class AnnounceModal(discord.ui.Modal, title="📢 New Announcement"):
    """Title + body editor. The Send button posts the announcement."""

    title_input = discord.ui.TextInput(
        label="Announcement Title",
        placeholder="e.g. Raid Night — Join now!",
        required=True,
        max_length=256,
        style=discord.TextStyle.short,
    )
    body_input = discord.ui.TextInput(
        label="Announcement Body",
        placeholder="Write the announcement message here...",
        required=True,
        max_length=4000,
        style=discord.TextStyle.paragraph,
    )

    def __init__(self, announce_cog: "Announce"):
        super().__init__()
        self.announce_cog = announce_cog

    async def on_submit(self, interaction: discord.Interaction):
        title = self.title_input.value.strip()
        body = self.body_input.value.strip()

        if not title or not body:
            return await interaction.response.send_message(
                "❌ Title and body cannot be empty.", ephemeral=True
            )

        channel = await self.announce_cog._get_announce_channel(interaction)
        if channel is None:
            return await interaction.response.send_message(
                "❌ Could not find the announcements channel.", ephemeral=True
            )

        embed = discord.Embed(
            title=title,
            description=body,
            color=ANNOUNCE_COLOR,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(
            name=interaction.user.display_name,
            icon_url=interaction.user.display_avatar.url,
        )
        embed.set_footer(text=f"Announced by {interaction.user.display_name}")

        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            return await interaction.response.send_message(
                "❌ I don't have permission to send in the announcements channel.",
                ephemeral=True,
            )
        except discord.HTTPException as e:
            return await interaction.response.send_message(
                f"❌ Failed to send announcement: {e}", ephemeral=True
            )

        await interaction.response.send_message(
            f"✅ Announcement sent to <#{ANNOUNCE_CHANNEL_ID}>!", ephemeral=True
        )


class Announce(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _get_announce_channel(self, interaction: discord.Interaction):
        """Return the announcements channel (from cache first, then fetch)."""
        channel = self.bot.get_channel(ANNOUNCE_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(ANNOUNCE_CHANNEL_ID)
            except discord.HTTPException:
                channel = None
        return channel

    @app_commands.command(
        name="announce",
        description="Create and post an announcement (Staff only)",
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.checks.cooldown(1, 10)
    async def announce(self, interaction: discord.Interaction):
        if not is_staff(interaction.user):
            return await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
        await interaction.response.send_modal(AnnounceModal(self))


async def setup(bot: commands.Bot):
    await bot.add_cog(Announce(bot))
    print("✅ Announce cog loaded")
