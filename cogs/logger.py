import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone

# Import shared functions from bot.py
from bot import (
    d1_query,
    GUILD_ID,
    LOG_CHANNEL_ID,
    is_staff,
    is_head_staff_or_founder
)

# ── Channel IDs ──────────────────────────────────────────────────────────────
MEMBER_LOG_CHANNEL_ID = 1535994819183251556
MESSAGE_LOG_CHANNEL_ID = 1535995158531801179


class Logger(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        print("✅ Logger cog initialized")

    # ── Member Join Log ──────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Log when a member joins the server"""
        # Only log for the main guild
        if member.guild.id != GUILD_ID:
            return

        channel = self.bot.get_channel(MEMBER_LOG_CHANNEL_ID)
        if not channel:
            return

        # Get account age
        now = datetime.now(timezone.utc)
        account_age = now - member.created_at
        account_age_days = account_age.days
        account_age_str = f"{account_age_days} days" if account_age_days > 0 else "Today"

        # Check if account is new (less than 7 days old)
        is_new = account_age_days < 7
        new_warning = " ⚠️ **NEW ACCOUNT**" if is_new else ""

        embed = discord.Embed(
            title="📥 Member Joined",
            description=f"{member.mention} has joined the server!{new_warning}",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc)
        )

        if member.display_avatar:
            embed.set_thumbnail(url=member.display_avatar.url)

        embed.add_field(name="User", value=f"{member.name}#{member.discriminator}", inline=True)
        embed.add_field(name="ID", value=f"`{member.id}`", inline=True)
        embed.add_field(name="Account Created", value=f"{member.created_at.strftime('%b %d, %Y')}\n({account_age_str} ago)", inline=False)

        # Get member count
        embed.set_footer(text=f"Member #{member.guild.member_count}")

        await channel.send(embed=embed)

    # ── Member Leave Log ──────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        """Log when a member leaves the server"""
        # Only log for the main guild
        if member.guild.id != GUILD_ID:
            return

        channel = self.bot.get_channel(MEMBER_LOG_CHANNEL_ID)
        if not channel:
            return

        # Get how long they were in the server
        if member.joined_at:
            now = datetime.now(timezone.utc)
            time_in_server = now - member.joined_at
            days = time_in_server.days
            time_str = f"{days} days" if days > 0 else "Less than a day"
        else:
            time_str = "Unknown"

        embed = discord.Embed(
            title="📤 Member Left",
            description=f"{member.mention} has left the server.",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc)
        )

        if member.display_avatar:
            embed.set_thumbnail(url=member.display_avatar.url)

        embed.add_field(name="User", value=f"{member.name}#{member.discriminator}", inline=True)
        embed.add_field(name="ID", value=f"`{member.id}`", inline=True)
        embed.add_field(name="Time in Server", value=time_str, inline=False)

        # Get roles they had
        roles = [r.mention for r in member.roles if r.name != "@everyone"]
        if roles:
            embed.add_field(name="Roles", value=" ".join(roles[:10]) + (f" (+{len(roles)-10} more)" if len(roles) > 10 else ""), inline=False)

        embed.set_footer(text=f"Member #{member.guild.member_count}")

        await channel.send(embed=embed)

    # ── Message Delete Log ───────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        """Log when a message is deleted"""
        # Ignore DMs and bot messages
        if not message.guild:
            return
        
        # Ignore bot messages (including RaidBot)
        if message.author.bot:
            return

        # Only log for the main guild
        if message.guild.id != GUILD_ID:
            return

        channel = self.bot.get_channel(MESSAGE_LOG_CHANNEL_ID)
        if not channel:
            return

        # Get the message content (or show attachment count)
        content = message.content or "*No text content*"
        if len(content) > 1000:
            content = content[:1000] + "... (truncated)"

        embed = discord.Embed(
            title="🗑️ Message Deleted",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc)
        )

        embed.add_field(name="Author", value=f"{message.author.mention} (`{message.author.id}`)", inline=False)
        embed.add_field(name="Channel", value=f"{message.channel.mention}", inline=True)
        embed.add_field(name="Message ID", value=f"`{message.id}`", inline=True)

        # Show content if not empty
        if content and content != "*No text content*":
            embed.add_field(name="Content", value=content, inline=False)

        # Show attachments if any
        if message.attachments:
            attachment_names = "\n".join([f"• {a.filename}" for a in message.attachments[:5]])
            if len(message.attachments) > 5:
                attachment_names += f"\n... and {len(message.attachments)-5} more"
            embed.add_field(name="Attachments", value=attachment_names, inline=False)

        # Show if it was a reply
        if message.reference and message.reference.resolved:
            replied_to = message.reference.resolved
            if replied_to and not replied_to.author.bot:
                embed.add_field(
                    name="Reply to",
                    value=f"{replied_to.author.mention}: {replied_to.content[:100]}{'...' if len(replied_to.content) > 100 else ''}",
                    inline=False
                )

        embed.set_footer(text=f"Message sent at {message.created_at.strftime('%b %d, %Y %I:%M %p')}")

        await channel.send(embed=embed)

    # ── Bulk Message Delete Log ─────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages: list):
        """Log when multiple messages are deleted at once"""
        if not messages or not messages[0].guild:
            return

        guild = messages[0].guild
        if guild.id != GUILD_ID:
            return

        channel = self.bot.get_channel(MESSAGE_LOG_CHANNEL_ID)
        if not channel:
            return

        # Filter out bot messages
        user_messages = [msg for msg in messages if not msg.author.bot]
        if not user_messages:
            return

        # Get the channel from the first message
        first_msg = user_messages[0]
        channel_name = first_msg.channel.mention

        embed = discord.Embed(
            title="🗑️ Bulk Messages Deleted",
            description=f"**{len(user_messages)}** user messages were deleted in {channel_name}",
            color=discord.Color.dark_red(),
            timestamp=datetime.now(timezone.utc)
        )

        # Show a sample of deleted messages (up to 5)
        sample_messages = []
        for msg in user_messages[:5]:
            author_name = msg.author.display_name if msg.author else "Unknown"
            content = msg.content[:100] + "..." if len(msg.content) > 100 else msg.content
            sample_messages.append(f"**{author_name}:** {content or '*No text*'}")

        if sample_messages:
            embed.add_field(
                name="Sample of Deleted Messages",
                value="\n".join(sample_messages),
                inline=False
            )

        if len(user_messages) > 5:
            embed.add_field(name="And more...", value=f"{len(user_messages)-5} additional messages deleted", inline=False)

        embed.set_footer(text=f"Total: {len(user_messages)} messages")

        await channel.send(embed=embed)

    # ── Message Edit Log ────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        """Log when a message is edited"""
        # Ignore if content didn't actually change (e.g., embed updates)
        if before.content == after.content:
            return

        # Ignore DMs and bot messages
        if not before.guild:
            return
        
        # Ignore bot messages (including RaidBot)
        if before.author.bot:
            return

        # Only log for the main guild
        if before.guild.id != GUILD_ID:
            return

        channel = self.bot.get_channel(MESSAGE_LOG_CHANNEL_ID)
        if not channel:
            return

        # Truncate long messages
        before_content = before.content or "*No text content*"
        after_content = after.content or "*No text content*"

        if len(before_content) > 500:
            before_content = before_content[:500] + "... (truncated)"
        if len(after_content) > 500:
            after_content = after_content[:500] + "... (truncated)"

        embed = discord.Embed(
            title="📝 Message Edited",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc)
        )

        embed.add_field(name="Author", value=f"{before.author.mention} (`{before.author.id}`)", inline=False)
        embed.add_field(name="Channel", value=f"{before.channel.mention}", inline=True)
        embed.add_field(name="Message ID", value=f"`{before.id}`", inline=True)
        embed.add_field(name="Jump to Message", value=f"[Click here]({before.jump_url})", inline=True)

        if before_content and before_content != "*No text content*":
            embed.add_field(
                name="Before",
                value=f"```{before_content}```" if len(before_content) < 500 else f"```{before_content[:500]}```\n*(truncated)*",
                inline=False
            )
        else:
            embed.add_field(name="Before", value="*No text content*", inline=False)

        if after_content and after_content != "*No text content*":
            embed.add_field(
                name="After",
                value=f"```{after_content}```" if len(after_content) < 500 else f"```{after_content[:500]}```\n*(truncated)*",
                inline=False
            )
        else:
            embed.add_field(name="After", value="*No text content*", inline=False)

        embed.set_footer(text=f"Edited at {after.edited_at.strftime('%b %d, %Y %I:%M %p') if after.edited_at else 'Unknown'}")

        await channel.send(embed=embed)

    # ── Command to setup logger channels ─────────────────────────────────────
    @app_commands.command(
        name="setuplogger",
        description="Send test logs to verify logger channels are working (Staff only)"
    )
    @app_commands.checks.cooldown(1, 30)
    async def setuplogger(self, interaction: discord.Interaction):
        """Send test logs to verify logger channels are working"""
        if not is_staff(interaction.user):
            return await interaction.response.send_message(
                "❌ You do not have permission to use this command.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            member_channel = self.bot.get_channel(MEMBER_LOG_CHANNEL_ID)
            message_channel = self.bot.get_channel(MESSAGE_LOG_CHANNEL_ID)

            if not member_channel:
                return await interaction.followup.send(
                    f"❌ Member log channel <#{MEMBER_LOG_CHANNEL_ID}> not found.",
                    ephemeral=True
                )

            if not message_channel:
                return await interaction.followup.send(
                    f"❌ Message log channel <#{MESSAGE_LOG_CHANNEL_ID}> not found.",
                    ephemeral=True
                )

            # Send test message to member log channel
            test_embed = discord.Embed(
                title="✅ Logger Test",
                description="This is a test message to verify the member logger is working.",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc)
            )
            test_embed.add_field(name="Test", value="If you can see this, the logger is set up correctly!", inline=False)
            await member_channel.send(embed=test_embed)

            # Send test message to message log channel
            test_embed2 = discord.Embed(
                title="✅ Logger Test",
                description="This is a test message to verify the message logger is working.",
                color=discord.Color.gold(),
                timestamp=datetime.now(timezone.utc)
            )
            test_embed2.add_field(name="Test", value="If you can see this, the logger is set up correctly!", inline=False)
            await message_channel.send(embed=test_embed2)

            await interaction.followup.send(
                "✅ Test logs sent to both logging channels!\n"
                f"📥 Member log: {member_channel.mention}\n"
                f"📝 Message log: {message_channel.mention}",
                ephemeral=True
            )

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


async def setup(bot: commands.Bot):
    """Setup function for the cog"""
    await bot.add_cog(Logger(bot))
    print("✅ Logger cog setup complete")