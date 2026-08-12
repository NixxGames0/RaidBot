import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone

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

# ── Staff Roles ──────────────────────────────────────────────────────────────────
STAFF_ROLES = {FOUNDER_ROLE_ID, HEAD_STAFF_ROLE_ID, MOD_ROLE_ID, TRIAL_MOD_ROLE_ID}


async def send_log(bot: commands.Bot, embed: discord.Embed):
    """Send a log message to the log channel"""
    channel = bot.get_channel(LOG_CHANNELS.get("general", 0))
    if channel:
        try:
            await channel.send(embed=embed)
        except Exception as e:
            print(f"Error sending log: {e}")


class PSCodes(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        print("✅ PS Codes cog initialized")

    @app_commands.command(
        name="addps",
        description="Add one or more PS codes to the database (Head Staff+)"
    )
    @app_commands.describe(codes="Comma-separated PS codes to add")
    @app_commands.checks.cooldown(1, 5)
    async def addps(self, interaction: discord.Interaction, codes: str):
        if not is_head_staff_or_founder(interaction.user):
            return await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )

        items = [c.strip().upper() for c in codes.split(",") if c.strip()]
        if not items:
            return await interaction.response.send_message(
                "❌ Please provide at least one valid code.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        added, dupes = [], []
        now = datetime.now(timezone.utc).isoformat()
        for code in items:
            try:
                check = await d1_query("SELECT code FROM codes WHERE code = ?", [code])
                if check["results"]:
                    dupes.append(code)
                    continue
                await d1_query(
                    "INSERT INTO codes (code, is_using, held_by, claimed_at, created_at) VALUES (?, 0, NULL, NULL, ?)",
                    [code, now]
                )
                added.append(code)
            except Exception as e:
                print(f"Error in addps for {code}: {e}")

        lines = []
        if added:
            lines.append("✅ Added: " + ", ".join(f"`{c}`" for c in added))
        if dupes:
            lines.append("⚠️ Already existed: " + ", ".join(f"`{c}`" for c in dupes))
        await interaction.followup.send("\n".join(lines) or "❌ No codes processed.", ephemeral=True)

        if added:
            embed = discord.Embed(
                title="📥 PS Codes Added",
                description=", ".join(f"`{c}`" for c in added),
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="Added By", value=interaction.user.mention, inline=True)
            embed.set_footer(text=f"User ID: {interaction.user.id}")
            await send_log(self.bot, embed)

    @app_commands.command(
        name="delps",
        description="Delete one or more PS codes from the database (Head Staff+)"
    )
    @app_commands.describe(codes="Comma-separated PS codes to delete")
    @app_commands.checks.cooldown(1, 5)
    async def delps(self, interaction: discord.Interaction, codes: str):
        if not is_head_staff_or_founder(interaction.user):
            return await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )

        items = [c.strip().upper() for c in codes.split(",") if c.strip()]
        if not items:
            return await interaction.response.send_message(
                "❌ Please provide at least one valid code.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        removed, in_use, missing = [], [], []
        for code in items:
            try:
                check = await d1_query(
                    "SELECT code, is_using, held_by FROM codes WHERE code = ?", [code]
                )
                if not check["results"]:
                    missing.append(code)
                    continue
                row = check["results"][0]
                if row["is_using"]:
                    in_use.append(f"`{code}` (held by <@{row['held_by']}>)")
                    continue
                await d1_query("DELETE FROM codes WHERE code = ?", [code])
                removed.append(code)
            except Exception as e:
                print(f"Error in delps for {code}: {e}")

        lines = []
        if removed:
            lines.append("🗑️ Removed: " + ", ".join(f"`{c}`" for c in removed))
        if in_use:
            lines.append("⚠️ In use (use `/forceps` to force remove): " + ", ".join(in_use))
        if missing:
            lines.append("❌ Not found: " + ", ".join(f"`{c}`" for c in missing))
        await interaction.followup.send("\n".join(lines) or "❌ No codes processed.", ephemeral=True)

        if removed:
            embed = discord.Embed(
                title="📤 PS Codes Removed",
                description=", ".join(f"`{c}`" for c in removed),
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="Removed By", value=interaction.user.mention, inline=True)
            embed.set_footer(text=f"User ID: {interaction.user.id}")
            await send_log(self.bot, embed)

    @app_commands.command(
        name="forceps",
        description="Forcefully remove a PS code (even if in use) (Head Staff+)"
    )
    @app_commands.describe(code="The PS code to force remove")
    @app_commands.checks.cooldown(1, 10)
    async def forceps(self, interaction: discord.Interaction, code: str):
        # Head Staff+ only
        if not is_head_staff_or_founder(interaction.user):
            return await interaction.response.send_message(
                "❌ You do not have permission to use this command.",
                ephemeral=True
            )

        code = code.strip().upper()
        if not code:
            return await interaction.response.send_message(
                "❌ Please provide a valid code.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        try:
            # Check if code exists
            check = await d1_query(
                "SELECT code, is_using, held_by FROM codes WHERE code = ?",
                [code]
            )

            if not check["results"]:
                return await interaction.followup.send(
                    f"⚠️ Code `{code}` not found in the database.",
                    ephemeral=True
                )

            row = check["results"][0]
            held_by = row["held_by"]

            # If code is in use, remove it from the user's ps_codes
            if row["is_using"] and held_by:
                user_result = await d1_query(
                    "SELECT ps_codes FROM users WHERE discord_id = ?",
                    [held_by]
                )

                if user_result["results"]:
                    import json
                    current_codes = json.loads(user_result["results"][0]["ps_codes"] or "[]")
                    if code in current_codes:
                        current_codes.remove(code)
                        now = datetime.now(timezone.utc).isoformat()
                        await d1_query(
                            "UPDATE users SET ps_codes = ?, updated_at = ? WHERE discord_id = ?",
                            [json.dumps(current_codes), now, held_by]
                        )

            # Delete the code
            await d1_query(
                "DELETE FROM codes WHERE code = ?",
                [code]
            )

            await interaction.followup.send(
                f"🔨 Force removed code `{code}`."
                + (f" (was held by <@{held_by}>)" if held_by else ""),
                ephemeral=True
            )

            # Log the force removal
            embed = discord.Embed(
                title="🔨 PS Code Force Removed",
                description=f"Code: `{code}`",
                color=discord.Color.dark_red(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="Removed By", value=interaction.user.mention, inline=True)
            if held_by:
                embed.add_field(name="Previously Held By", value=f"<@{held_by}>", inline=True)
            embed.set_footer(text=f"User ID: {interaction.user.id}")
            await send_log(self.bot, embed)

        except Exception as e:
            print(f"Error in forceps: {e}")
            await interaction.followup.send(
                f"❌ Error force removing code: {str(e)[:100]}",
                ephemeral=True
            )

    @app_commands.command(
        name="pslist",
        description="List all PS codes in the database (Staff only)"
    )
    @app_commands.checks.cooldown(1, 10)
    async def pslist(self, interaction: discord.Interaction):
        # Staff only
        if not is_staff(interaction.user):
            return await interaction.response.send_message(
                "❌ You do not have permission to use this command.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        try:
            result = await d1_query(
                "SELECT code, is_using, held_by FROM codes ORDER BY created_at ASC"
            )
            rows = result["results"]

            if not rows:
                return await interaction.followup.send(
                    "📭 No PS codes in the database.",
                    ephemeral=True
                )

            available = []
            in_use = []

            for row in rows:
                if row["is_using"]:
                    in_use.append(f"`{row['code']}` — <@{row['held_by']}>")
                else:
                    available.append(f"`{row['code']}`")

            embed = discord.Embed(
                title="📋 PS Code List",
                color=discord.Color.blurple(),
                timestamp=datetime.now(timezone.utc)
            )

            if available:
                embed.add_field(
                    name=f"✅ Available ({len(available)})",
                    value="\n".join(available[:25]) + (f"\n... and {len(available) - 25} more" if len(available) > 25 else ""),
                    inline=False
                )
            else:
                embed.add_field(name="✅ Available", value="No available codes", inline=False)

            if in_use:
                embed.add_field(
                    name=f"🔴 In Use ({len(in_use)})",
                    value="\n".join(in_use[:25]) + (f"\n... and {len(in_use) - 25} more" if len(in_use) > 25 else ""),
                    inline=False
                )
            else:
                embed.add_field(name="🔴 In Use", value="No codes in use", inline=False)

            embed.set_footer(text=f"Total: {len(rows)} codes")
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            print(f"Error in pslist: {e}")
            await interaction.followup.send(
                f"❌ Error listing codes: {str(e)[:100]}",
                ephemeral=True
            )

    @app_commands.command(
        name="psstats",
        description="Get statistics about PS codes (Staff only)"
    )
    @app_commands.checks.cooldown(1, 30)
    async def psstats(self, interaction: discord.Interaction):
        # Staff only
        if not is_staff(interaction.user):
            return await interaction.response.send_message(
                "❌ You do not have permission to use this command.",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        try:
            # Get total codes
            total_result = await d1_query("SELECT COUNT(*) as count FROM codes")
            total = total_result["results"][0]["count"] if total_result["results"] else 0

            # Get available codes
            avail_result = await d1_query("SELECT COUNT(*) as count FROM codes WHERE is_using = 0")
            available = avail_result["results"][0]["count"] if avail_result["results"] else 0

            # Get in-use codes
            inuse_result = await d1_query("SELECT COUNT(*) as count FROM codes WHERE is_using = 1")
            in_use = inuse_result["results"][0]["count"] if inuse_result["results"] else 0

            # Get recent additions (last 7 days)
            from datetime import timedelta
            week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            recent_result = await d1_query(
                "SELECT COUNT(*) as count FROM codes WHERE created_at > ?",
                [week_ago]
            )
            recent = recent_result["results"][0]["count"] if recent_result["results"] else 0

            embed = discord.Embed(
                title="📊 PS Code Statistics",
                color=discord.Color.blue(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="Total Codes", value=f"**{total}**", inline=True)
            embed.add_field(name="Available", value=f"✅ **{available}**", inline=True)
            embed.add_field(name="In Use", value=f"🔴 **{in_use}**", inline=True)
            embed.add_field(name="Added in Last 7 Days", value=f"📈 **{recent}**", inline=True)

            if total > 0:
                usage_pct = int((in_use / total) * 100)
                embed.add_field(name="Usage Rate", value=f"{usage_pct}%", inline=True)

            embed.set_footer(text=f"Guild: {interaction.guild.name}")

            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            print(f"Error in psstats: {e}")
            await interaction.followup.send(
                f"❌ Error getting statistics: {str(e)[:100]}",
                ephemeral=True
            )


async def setup(bot: commands.Bot):
    """Setup function for the cog"""
    await bot.add_cog(PSCodes(bot))
    print("✅ PS Codes cog setup complete")