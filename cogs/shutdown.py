# cogs/shutdown.py
import discord
from discord import app_commands
from discord.ext import commands
import os
import sys

class Shutdown(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="shutdown",
        description="Shut down the bot immediately (owner only)"
    )
    @app_commands.default_permissions(administrator=True)  # hides from non‑admins
    async def shutdown(self, interaction: discord.Interaction):
        owner = self.bot.application.owner
        if owner and interaction.user.id == owner.id:
            await interaction.response.send_message("🛑 Shutting down... Goodbye!", ephemeral=True)
            await self.bot.close()
            os._exit(0)
        else:
            await interaction.response.send_message(
                "❌ You are not the bot owner. This command is restricted.",
                ephemeral=True
            )

async def setup(bot: commands.Bot):
    await bot.add_cog(Shutdown(bot))