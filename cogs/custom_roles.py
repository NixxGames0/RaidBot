import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone
import logging
from pathlib import Path
import io

try:
    from PIL import Image, ImageDraw
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    print("⚠️ Pillow not installed. Install with: pip install Pillow")

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

# ─── Constants ────────────────────────────────────────────
CUSTOM_ROLE_CHANNEL_ID = 1536249281923653674
ELIGIBLE_ROLES = {BOOSTER_ROLE_ID, HEAD_STAFF_ROLE_ID, FOUNDER_ROLE_ID}
ICONS_FOLDER = Path("icons")
BOT_ROLE_ID = 1535635733791121443  # The bot's specific role for top positioning

logger = logging.getLogger(__name__)

# ─── Mapping: base name → role ID ──────────────────────
ROLE_ICON_MAP = {
    "bot": 1535558679959576688,
    "founder": 1535553078852325506,
    "headstaff": 1535558010431340604,
    "mod": 1535558108590383184,
    "trialmod": 1535558050532827186,
    "elitehoster": 1535557474042642432,
    "hoster": 1535556016865808444,
    "booster": 1535570767855624262,
    "blacklisted": 1535669616721002626,
    "linked": 1535663327085076572,
    "verified": 1535554357762850817,
    "ghoster": 1536325001584578621,
}

# ─── Icon cache ───────────────────────────────────────────
AVAILABLE_ICONS = {}  # name -> Path

def load_icons():
    if not ICONS_FOLDER.exists():
        logger.warning(f"Icons folder '{ICONS_FOLDER}' not found.")
        return
    for file in ICONS_FOLDER.glob("*.png"):
        if "_gradient" not in file.stem:
            AVAILABLE_ICONS[file.stem.lower()] = file
    logger.info(f"Loaded {len(AVAILABLE_ICONS)} white icons: {', '.join(AVAILABLE_ICONS.keys())}")

load_icons()


# ─── Image generation ─────────────────────────────────────
def generate_gradient_image(base_path: Path, color: discord.Color) -> bytes:
    """Fill the white PNG shape with a gradient based on the given colour."""
    if not HAS_PIL:
        raise RuntimeError("Pillow is not installed.")

    img = Image.open(base_path).convert("RGBA")
    width, height = img.size

    grad_img = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(grad_img)

    r, g, b = color.r, color.g, color.b
    r2 = min(255, int(r * 1.5))
    g2 = min(255, int(g * 1.5))
    b2 = min(255, int(b * 1.5))

    for x in range(width):
        ratio = x / width
        cr = int(r + (r2 - r) * ratio)
        cg = int(g + (g2 - g) * ratio)
        cb = int(b + (b2 - b) * ratio)
        draw.line([(x, 0), (x, height)], fill=(cr, cg, cb, 255), width=1)

    mask = img.split()[3]
    final = Image.composite(grad_img, Image.new("RGBA", img.size, (0, 0, 0, 0)), mask)

    with io.BytesIO() as output:
        final.save(output, format="PNG")
        return output.getvalue()


# ─── Database helpers ─────────────────────────────────────
async def ensure_db():
    try:
        await d1_query(
            """CREATE TABLE IF NOT EXISTS custom_roles (
                user_id TEXT PRIMARY KEY,
                role_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
    except Exception:
        pass


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


async def get_stored_emoji_id(basename: str) -> int:
    result = await d1_query(
        "SELECT value FROM bot_meta WHERE key = ?",
        [f"role_emoji_{basename}"]
    )
    if result["results"]:
        return int(result["results"][0]["value"])
    return None


async def store_emoji_id(basename: str, emoji_id: int):
    await d1_query(
        "INSERT OR REPLACE INTO bot_meta (key, value) VALUES (?, ?)",
        [f"role_emoji_{basename}", str(emoji_id)]
    )


# ─── Emoji management ─────────────────────────────────────
async def ensure_role_emoji(guild: discord.Guild, basename: str, color: discord.Color, role_id: int = None) -> discord.Emoji:
    basename = basename.lower()
    if basename not in AVAILABLE_ICONS:
        raise ValueError(f"Icon '{basename}' not found.")

    try:
        image_data = generate_gradient_image(AVAILABLE_ICONS[basename], color)
    except Exception as e:
        raise RuntimeError(f"Failed to generate gradient: {e}")

    emoji_name = f"role_{basename}"

    existing = discord.utils.get(guild.emojis, name=emoji_name)
    if existing:
        try:
            await existing.delete(reason="Refreshing role emoji")
        except:
            pass

    try:
        emoji = await guild.create_custom_emoji(
            name=emoji_name,
            image=image_data,
            reason=f"Generated emoji for role colour #{color.value:06x}"
        )
        logger.info(f"✅ Uploaded emoji '{emoji_name}' for colour #{color.value:06x}")
        if role_id and role_id in ROLE_ICON_MAP.values():
            await store_emoji_id(basename, emoji.id)
        return emoji
    except discord.Forbidden:
        logger.error("Bot lacks 'manage_emojis_and_stickers' permission.")
        raise
    except discord.HTTPException as e:
        logger.error(f"Failed to upload emoji: {e}")
        raise


# ─── Sync built‑in roles ──────────────────────────────────
async def sync_all_role_emojis(guild: discord.Guild):
    results = []
    for basename, role_id in ROLE_ICON_MAP.items():
        try:
            role = guild.get_role(role_id)
            if not role:
                results.append(f"❌ `{basename}` – role not found")
                continue
            if basename not in AVAILABLE_ICONS:
                results.append(f"❌ `{basename}` – white icon missing")
                continue
            emoji = await ensure_role_emoji(guild, basename, role.color, role_id)
            results.append(f"✅ `{basename}` → {emoji} (colour #{role.color.value:06x})")
        except Exception as e:
            results.append(f"❌ `{basename}` – {e}")
    return results


# ─── Helper to safely respond ─────────────────────────────
async def safe_respond(interaction, *args, **kwargs):
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(*args, **kwargs)
        else:
            await interaction.followup.send(*args, **kwargs)
    except discord.NotFound:
        pass


# ─── Role Builder Views ────────────────────────────────────
class RoleBuilderView(discord.ui.View):
    def __init__(self, user_id: int, mode: str = "create", existing_data: dict = None):
        super().__init__(timeout=300)  # 5 minutes
        self.user_id = user_id
        self.mode = mode
        self.data = {
            "name": "",
            "color": discord.Color.blurple(),
        }
        if existing_data:
            self.data["name"] = existing_data.get("name", "")
            self.data["color"] = existing_data.get("color", discord.Color.blurple())

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="🎨 Custom Role Builder",
            description=(
                "**Mode:** " + ("Creating" if self.mode == "create" else "Editing") + " your custom role.\n"
                "Use the buttons below to change each field, then click **Apply Changes** to save.\n\n"
                "**Note:** Role icons are not available until the server reaches Level 2.\n"
                "Gradient emojis for built‑in roles are automatically generated."
            ),
            color=self.data["color"],
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(
            name="Name",
            value=self.data["name"] or "*(not set)*",
            inline=True
        )
        embed.add_field(
            name="Color",
            value=f"`#{self.data['color'].value:06x}`",
            inline=True
        )
        embed.set_footer(text="Changes are not applied until you click 'Apply'.")
        return embed

    async def update_embed(self, interaction: discord.Interaction):
        embed = self.build_embed()
        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(embed=embed, view=self)
            else:
                await interaction.edit_original_response(embed=embed, view=self)
        except discord.NotFound:
            await interaction.followup.send("⏰ Your session expired. Please click 'Manage Role' again.", ephemeral=True)

    def _check_user(self, interaction) -> bool:
        if interaction.user.id != self.user_id:
            interaction.response.send_message("❌ This is not your builder session.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Change Name", style=discord.ButtonStyle.primary)
    async def change_name(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_user(interaction):
            return
        await interaction.response.send_modal(NameModal(self))

    @discord.ui.button(label="Change Color", style=discord.ButtonStyle.primary)
    async def change_color(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_user(interaction):
            return
        await interaction.response.send_modal(ColorModal(self))

    @discord.ui.button(label="Apply Changes", style=discord.ButtonStyle.success)
    async def apply_changes(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_user(interaction):
            return

        if not self.data["name"]:
            return await interaction.response.send_message("❌ Please set a name first.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        try:
            if self.mode == "create":
                # Create the role
                role = await guild.create_role(
                    name=self.data["name"],
                    color=self.data["color"],
                    reason=f"Custom role created by {interaction.user}"
                )

                # ─── Place the role at the VERY TOP ──────────────────────────
                # Try to position it just below the bot's specific role
                bot_role = guild.get_role(BOT_ROLE_ID)
                if bot_role:
                    target_position = bot_role.position - 1
                    if target_position < 1:
                        target_position = 1
                    try:
                        await role.edit(position=target_position)
                        logger.info(f"✅ Custom role {role.name} placed at position {target_position}")
                    except Exception as e:
                        logger.warning(f"Failed to set role position: {e}")
                        # Fallback: use bot's highest role
                        bot_top = max(guild.me.roles, key=lambda r: r.position)
                        target_position = bot_top.position - 1
                        try:
                            await role.edit(position=target_position)
                            logger.info(f"✅ Custom role placed at position {target_position} (fallback)")
                        except Exception as e2:
                            logger.warning(f"Fallback also failed: {e2}")
                else:
                    # No bot role found; use the bot's highest role
                    bot_top = max(guild.me.roles, key=lambda r: r.position)
                    target_position = bot_top.position - 1
                    try:
                        await role.edit(position=target_position)
                        logger.info(f"✅ Custom role placed at position {target_position}")
                    except Exception as e:
                        logger.warning(f"Failed to set role position: {e}")
                        # Last resort: try position 1
                        try:
                            await role.edit(position=1)
                            logger.info("✅ Custom role placed at position 1 as last resort")
                        except Exception as e3:
                            logger.warning(f"All positioning attempts failed: {e3}")

                await interaction.user.add_roles(role)
                await create_custom_role_entry(interaction.user.id, role.id)

                embed = discord.Embed(
                    title="✅ Custom Role Created",
                    color=self.data["color"],
                    timestamp=datetime.now(timezone.utc)
                )
                embed.add_field(name="Role", value=role.mention, inline=True)
                embed.add_field(name="Name", value=self.data["name"], inline=True)
                embed.add_field(name="Color", value=f"#{self.data['color'].value:06x}", inline=True)
                await send_log(interaction.client, embed)

                # Disable buttons and update the original message
                for child in self.children:
                    child.disabled = True
                await interaction.edit_original_response(embed=embed, view=self)
                await interaction.followup.send(
                    f"✅ Your custom role **{role.name}** has been created!",
                    ephemeral=True
                )

            else:  # edit
                existing = await get_custom_role(interaction.user.id)
                if not existing:
                    return await interaction.followup.send("❌ No custom role found to edit.", ephemeral=True)
                role = guild.get_role(existing)
                if not role:
                    return await interaction.followup.send("❌ Your custom role no longer exists.", ephemeral=True)

                await role.edit(name=self.data["name"], color=self.data["color"])

                embed = discord.Embed(
                    title="✏️ Custom Role Updated",
                    color=self.data["color"],
                    timestamp=datetime.now(timezone.utc)
                )
                embed.add_field(name="Role", value=role.mention, inline=True)
                embed.add_field(name="New Name", value=self.data["name"], inline=True)
                embed.add_field(name="New Color", value=f"#{self.data['color'].value:06x}", inline=True)
                await send_log(interaction.client, embed)

                for child in self.children:
                    child.disabled = True
                await interaction.edit_original_response(embed=embed, view=self)
                await interaction.followup.send(
                    f"✅ Your custom role has been updated to **{role.name}**!",
                    ephemeral=True
                )

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_user(interaction):
            return
        await interaction.response.send_message("❌ Role builder cancelled.", ephemeral=True)
        for child in self.children:
            child.disabled = True
        embed = self.build_embed()
        await interaction.edit_original_response(embed=embed, view=self)


class NameModal(discord.ui.Modal, title="Change Role Name"):
    def __init__(self, view: RoleBuilderView):
        super().__init__()
        self.view = view
        self.name_input = discord.ui.TextInput(
            label="New Name",
            placeholder="Max 50 characters",
            max_length=50,
            required=True,
            default=view.data["name"]
        )
        self.add_item(self.name_input)

    async def on_submit(self, interaction: discord.Interaction):
        self.view.data["name"] = self.name_input.value
        await self.view.update_embed(interaction)


class ColorModal(discord.ui.Modal, title="Change Role Color"):
    def __init__(self, view: RoleBuilderView):
        super().__init__()
        self.view = view
        self.color_input = discord.ui.TextInput(
            label="Hex Color",
            placeholder="#5865F2",
            required=False,
            max_length=7,
            default=f"#{view.data['color'].value:06x}"
        )
        self.add_item(self.color_input)

    async def on_submit(self, interaction: discord.Interaction):
        color_hex = self.color_input.value.strip()
        try:
            if color_hex.startswith("#"):
                color_hex = color_hex[1:]
            color_int = int(color_hex, 16)
            color = discord.Color(color_int)
        except ValueError:
            color = discord.Color.blurple()
        self.view.data["color"] = color
        await self.view.update_embed(interaction)


# ─── Panel View ────────────────────────────────────────────
class CustomRolePanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🆕 Manage Role", style=discord.ButtonStyle.success, custom_id="cr_manage")
    async def manage_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not any(role.id in ELIGIBLE_ROLES for role in interaction.user.roles):
            return await safe_respond(interaction, "❌ You are not eligible.", ephemeral=True)

        existing = await get_custom_role(interaction.user.id)
        mode = "edit" if existing else "create"

        if existing:
            role = interaction.guild.get_role(existing)
            if not role:
                return await safe_respond(interaction, "❌ Your custom role no longer exists.", ephemeral=True)
            existing_data = {
                "name": role.name,
                "color": role.color,
            }
        else:
            existing_data = None

        view = RoleBuilderView(interaction.user.id, mode, existing_data)
        embed = view.build_embed()
        await safe_respond(interaction, embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="👁️ Preview", style=discord.ButtonStyle.secondary, custom_id="cr_preview")
    async def preview_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not any(role.id in ELIGIBLE_ROLES for role in interaction.user.roles):
            return await safe_respond(interaction, "❌ You are not eligible.", ephemeral=True)
        existing = await get_custom_role(interaction.user.id)
        if not existing:
            return await safe_respond(interaction, "❌ You don't have a custom role.", ephemeral=True)
        role = interaction.guild.get_role(existing)
        if not role:
            return await safe_respond(interaction, "❌ Your custom role no longer exists.", ephemeral=True)

        embed = discord.Embed(
            title="👁️ Role Preview",
            description=f"**{role.mention}**",
            color=role.color,
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(
            name="Color",
            value=f"`#{role.color.value:06x}`",
            inline=False
        )
        embed.set_footer(text="This is how your role will appear.")
        await safe_respond(interaction, embed=embed, ephemeral=True)

    @discord.ui.button(label="🗑️ Delete", style=discord.ButtonStyle.danger, custom_id="cr_delete")
    async def delete_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not any(role.id in ELIGIBLE_ROLES for role in interaction.user.roles):
            return await safe_respond(interaction, "❌ You are not eligible.", ephemeral=True)
        existing = await get_custom_role(interaction.user.id)
        if not existing:
            return await safe_respond(interaction, "❌ You don't have a custom role.", ephemeral=True)
        role = interaction.guild.get_role(existing)
        if not role:
            return await safe_respond(interaction, "❌ Your custom role no longer exists.", ephemeral=True)

        confirm_view = ConfirmDeleteView(role.id, interaction.user.id)
        await safe_respond(interaction,
            "⚠️ Are you sure you want to delete your custom role? This action cannot be undone.",
            view=confirm_view,
            ephemeral=True
        )


class ConfirmDeleteView(discord.ui.View):
    def __init__(self, role_id: int, user_id: int):
        super().__init__(timeout=60)
        self.role_id = role_id
        self.user_id = user_id

    @discord.ui.button(label="Yes, delete", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ This is not your confirmation.", ephemeral=True)
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
            return await interaction.response.send_message("❌ This is not your confirmation.", ephemeral=True)
        await interaction.response.send_message("✅ Deletion cancelled.", ephemeral=True)
        self.stop()


def build_custom_role_panel():
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
            "🔹 **Manage Role** – create or edit your custom role.\n"
            "   You can set a name (max 50 chars) and a hex color.\n"
            "👁️ **Preview** – see how your role currently looks.\n"
            "🗑️ **Delete** – remove your role.\n\n"
            "🔹 **Gradient emojis** for built‑in roles are automatically generated.\n"
            "🔹 **Role icons** will be available when the server reaches Level 2."
        ),
        inline=False
    )
    embed.set_footer(text="Custom roles are cosmetic and do not grant permissions.")
    view = CustomRolePanelView()
    return embed, view


# ─── Cog ──────────────────────────────────────────────────
class CustomRoles(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.loop.create_task(self.register_view())
        self.bot.loop.create_task(self.startup_tasks())

    async def register_view(self):
        await self.bot.wait_until_ready()
        self.bot.add_view(CustomRolePanelView())
        print("✅ Registered persistent Custom Role panel view")

    async def startup_tasks(self):
        await self.bot.wait_until_ready()
        await ensure_db()

        guild = self.bot.get_guild(GUILD_ID)
        if not guild:
            return

        if not hasattr(self, "_synced"):
            self._synced = True
            print("🔄 Generating built‑in role emojis...")
            results = await sync_all_role_emojis(guild)
            for line in results:
                print(f"  {line}")
            print("✅ Built‑in role emojis ready.")

    @app_commands.command(
        name="syncemojis",
        description="Generate/update emojis for all built‑in roles (Staff only)"
    )
    @app_commands.checks.cooldown(1, 60)
    async def syncemojis(self, interaction: discord.Interaction):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("❌ You do not have permission.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        results = await sync_all_role_emojis(guild)

        embed = discord.Embed(
            title="🔄 Role Emoji Generation Results",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(
            name="Details",
            value="\n".join(results[:25]) + (f"\n... and {len(results)-25} more" if len(results) > 25 else ""),
            inline=False
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(CustomRoles(bot))
    print("✅ Custom Roles cog loaded")