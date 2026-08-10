import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone

from bot import (
    d1_query,
    GUILD_ID,
    LOG_CHANNEL_ID,
    BOOSTER_ROLE_ID,
    HEAD_STAFF_ROLE_ID,
    FOUNDER_ROLE_ID,
    is_staff,
    send_log
)

CUSTOM_ROLE_CHANNEL_ID = 1536249281923653674
ELIGIBLE_ROLES = {BOOSTER_ROLE_ID, HEAD_STAFF_ROLE_ID, FOUNDER_ROLE_ID}


# ── Database helpers ──────────────────────────────────────
async def get_custom_role(user_id: int):
    result = await d1_query(
        "SELECT role_id FROM custom_roles WHERE user_id = ?",
        [str(user_id)]
    )
    if result["results"]:
        return int(result["results"][0]["role_id"])
    return None


async def create_custom_role_entry(user_id: int, role_id: int):
    now = datetime.now(timezone.utc).isoformat()
    await d1_query(
        "INSERT INTO custom_roles (user_id, role_id, created_at) VALUES (?, ?, ?)",
        [str(user_id), str(role_id), now]
    )


async def delete_custom_role_entry(user_id: int):
    await d1_query(
        "DELETE FROM custom_roles WHERE user_id = ?",
        [str(user_id)]
    )


# ── Modals ─────────────────────────────────────────────────
class CreateRoleModal(discord.ui.Modal, title="Create Custom Role"):
    name = discord.ui.TextInput(
        label="Role Name",
        placeholder="Max 16 characters",
        max_length=16,
        required=True
    )
    color = discord.ui.TextInput(
        label="Color (hex)",
        placeholder="#5865F2",
        required=False,
        max_length=7,
        default="#5865F2"
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if not any(role.id in ELIGIBLE_ROLES for role in interaction.user.roles):
            return await interaction.followup.send(
                "❌ You are not eligible for a custom role.",
                ephemeral=True
            )

        if await get_custom_role(interaction.user.id):
            return await interaction.followup.send(
                "❌ You already have a custom role. Use Edit or Delete first.",
                ephemeral=True
            )

        color_hex = self.color.value.strip()
        try:
            if color_hex.startswith("#"):
                color_hex = color_hex[1:]
            color_int = int(color_hex, 16)
            color = discord.Color(color_int)
        except ValueError:
            color = discord.Color.blurple()

        guild = interaction.guild
        try:
            role = await guild.create_role(
                name=self.name.value,
                color=color,
                reason=f"Custom role for {interaction.user}"
            )
        except Exception as e:
            return await interaction.followup.send(
                f"❌ Failed to create role: {e}",
                ephemeral=True
            )

        bot_top = max(guild.me.roles, key=lambda r: r.position)
        await role.edit(position=bot_top.position - 1)

        await interaction.user.add_roles(role)
        await create_custom_role_entry(interaction.user.id, role.id)

        embed = discord.Embed(
            title="🆕 Custom Role Created",
            color=color,
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="User", value=interaction.user.mention, inline=True)
        embed.add_field(name="Role", value=role.mention, inline=True)
        embed.add_field(name="Color", value=f"#{color_hex.upper()}", inline=True)
        await send_log(interaction.client, embed)

        await interaction.followup.send(
            f"✅ Your custom role **{role.name}** has been created!",
            ephemeral=True
        )


class EditRoleModal(discord.ui.Modal, title="Edit Custom Role"):
    def __init__(self, current_name: str, current_color: discord.Color):
        super().__init__()
        self.name = discord.ui.TextInput(
            label="Role Name",
            placeholder="Max 16 characters",
            max_length=16,
            required=True,
            default=current_name
        )
        self.color = discord.ui.TextInput(
            label="Color (hex)",
            placeholder="#5865F2",
            required=False,
            max_length=7,
            default=f"#{current_color.to_rgb():06x}"
        )
        self.add_item(self.name)
        self.add_item(self.color)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        role_id = await get_custom_role(interaction.user.id)
        if not role_id:
            return await interaction.followup.send(
                "❌ You don't have a custom role. Create one first.",
                ephemeral=True
            )

        role = interaction.guild.get_role(role_id)
        if not role:
            return await interaction.followup.send(
                "❌ Your custom role no longer exists. Please contact staff.",
                ephemeral=True
            )

        color_hex = self.color.value.strip()
        try:
            if color_hex.startswith("#"):
                color_hex = color_hex[1:]
            color_int = int(color_hex, 16)
            color = discord.Color(color_int)
        except ValueError:
            color = discord.Color.blurple()

        try:
            await role.edit(name=self.name.value, color=color)
        except Exception as e:
            return await interaction.followup.send(
                f"❌ Failed to update role: {e}",
                ephemeral=True
            )

        embed = discord.Embed(
            title="✏️ Custom Role Updated",
            color=color,
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="User", value=interaction.user.mention, inline=True)
        embed.add_field(name="New Name", value=role.name, inline=True)
        embed.add_field(name="New Color", value=f"#{color_hex.upper()}", inline=True)
        await send_log(interaction.client, embed)

        await interaction.followup.send(
            f"✅ Your custom role has been updated to **{role.name}**!",
            ephemeral=True
        )


# ── Views ──────────────────────────────────────────────────
class ConfirmDeleteView(discord.ui.View):
    def __init__(self, role_id: int, user_id: int):
        super().__init__(timeout=60)
        self.role_id = role_id
        self.user_id = user_id

    @discord.ui.button(label="Yes, delete", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                "❌ This is not your confirmation.",
                ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)

        role = interaction.guild.get_role(self.role_id)
        if role:
            await role.delete(reason=f"Custom role deleted by {interaction.user}")

        await delete_custom_role_entry(self.user_id)

        embed = discord.Embed(
            title="🗑️ Custom Role Deleted",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="User", value=interaction.user.mention, inline=True)
        embed.add_field(name="Role ID", value=str(self.role_id), inline=True)
        await send_log(interaction.client, embed)

        await interaction.followup.send("🗑️ Your custom role has been deleted.", ephemeral=True)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                "❌ This is not your confirmation.",
                ephemeral=True
            )
        await interaction.response.send_message("✅ Deletion cancelled.", ephemeral=True)
        self.stop()


class CustomRolePanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🆕 Create", style=discord.ButtonStyle.success, custom_id="cr_create")
    async def create_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not any(role.id in ELIGIBLE_ROLES for role in interaction.user.roles):
            return await interaction.response.send_message(
                "❌ You are not eligible.",
                ephemeral=True
            )
        await interaction.response.send_modal(CreateRoleModal())

    @discord.ui.button(label="✏️ Edit", style=discord.ButtonStyle.primary, custom_id="cr_edit")
    async def edit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not any(role.id in ELIGIBLE_ROLES for role in interaction.user.roles):
            return await interaction.response.send_message(
                "❌ You are not eligible.",
                ephemeral=True
            )
        role_id = await get_custom_role(interaction.user.id)
        if not role_id:
            return await interaction.response.send_message(
                "❌ You don't have a custom role. Create one first.",
                ephemeral=True
            )
        role = interaction.guild.get_role(role_id)
        if not role:
            return await interaction.response.send_message(
                "❌ Your custom role no longer exists. Please contact staff.",
                ephemeral=True
            )
        await interaction.response.send_modal(EditRoleModal(role.name, role.color))

    @discord.ui.button(label="👁️ Preview", style=discord.ButtonStyle.secondary, custom_id="cr_preview")
    async def preview_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not any(role.id in ELIGIBLE_ROLES for role in interaction.user.roles):
            return await interaction.response.send_message(
                "❌ You are not eligible.",
                ephemeral=True
            )
        role_id = await get_custom_role(interaction.user.id)
        if not role_id:
            return await interaction.response.send_message(
                "❌ You don't have a custom role.",
                ephemeral=True
            )
        role = interaction.guild.get_role(role_id)
        if not role:
            return await interaction.response.send_message(
                "❌ Your custom role no longer exists.",
                ephemeral=True
            )

        embed = discord.Embed(
            title="👁️ Role Preview",
            description=f"**{role.mention}**",
            color=role.color,
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(
            name="Color",
            value=f"`#{role.color.to_rgb():06x}`",
            inline=False
        )
        embed.set_footer(text="This is how your role will appear.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="🗑️ Delete", style=discord.ButtonStyle.danger, custom_id="cr_delete")
    async def delete_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not any(role.id in ELIGIBLE_ROLES for role in interaction.user.roles):
            return await interaction.response.send_message(
                "❌ You are not eligible.",
                ephemeral=True
            )
        role_id = await get_custom_role(interaction.user.id)
        if not role_id:
            return await interaction.response.send_message(
                "❌ You don't have a custom role.",
                ephemeral=True
            )
        role = interaction.guild.get_role(role_id)
        if not role:
            return await interaction.response.send_message(
                "❌ Your custom role no longer exists.",
                ephemeral=True
            )

        confirm_view = ConfirmDeleteView(role.id, interaction.user.id)
        await interaction.response.send_message(
            "⚠️ Are you sure you want to delete your custom role? This action cannot be undone.",
            view=confirm_view,
            ephemeral=True
        )


def build_custom_role_panel():
    """Return the embed and view for the custom role panel."""
    embed = discord.Embed(
        title="🎨 Custom Role Creator",
        description=(
            "**Boosters**, **Head Staff**, and **Founders** can create a unique role "
            "that sits at the very top of the role list.\n"
            "You may have **one** custom role at a time."
        ),
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(
        name="Instructions",
        value=(
            "🆕 **Create** – set a name (max 16 chars) and a hex color.\n"
            "✏️ **Edit** – change the name or color.\n"
            "👁️ **Preview** – see how your role looks.\n"
            "🗑️ **Delete** – remove your role."
        ),
        inline=False
    )
    embed.set_footer(text="Role colors are cosmetic – they do not grant any permissions.")
    view = CustomRolePanelView()
    return embed, view


# ── Cog ────────────────────────────────────────────────────
class CustomRoles(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.loop.create_task(self.register_view())

    async def register_view(self):
        await self.bot.wait_until_ready()
        self.bot.add_view(CustomRolePanelView())
        print("✅ Registered persistent Custom Role panel view")


async def setup(bot: commands.Bot):
    await bot.add_cog(CustomRoles(bot))
    print("✅ Custom Roles cog loaded")