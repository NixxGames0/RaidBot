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

    # Create gradient background
    grad_img = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(grad_img)

    r, g, b = color.r, color.g, color.b
    # Lighter version (50% brighter)
    r2 = min(255, int(r * 1.5))
    g2 = min(255, int(g * 1.5))
    b2 = min(255, int(b * 1.5))

    for x in range(width):
        ratio = x / width
        cr = int(r + (r2 - r) * ratio)
        cg = int(g + (g2 - g) * ratio)
        cb = int(b + (b2 - b) * ratio)
        draw.line([(x, 0), (x, height)], fill=(cr, cg, cb, 255), width=1)

    mask = img.split()[3]  # alpha channel
    final = Image.composite(grad_img, Image.new("RGBA", img.size, (0, 0, 0, 0)), mask)

    with io.BytesIO() as output:
        final.save(output, format="PNG")
        return output.getvalue()


# ─── Database helpers ─────────────────────────────────────
async def ensure_db():
    """Create custom_roles table and add emoji_id column if missing."""
    try:
        await d1_query(
            """CREATE TABLE IF NOT EXISTS custom_roles (
                user_id TEXT PRIMARY KEY,
                role_id TEXT NOT NULL,
                emoji_id TEXT,
                created_at TEXT NOT NULL
            )"""
        )
        # Try to add emoji_id column (safe if already exists)
        await d1_query("ALTER TABLE custom_roles ADD COLUMN emoji_id TEXT")
    except Exception:
        pass  # Column already exists


async def get_custom_role(user_id: int):
    result = await d1_query(
        "SELECT role_id, emoji_id FROM custom_roles WHERE user_id = ?",
        [str(user_id)]
    )
    if result["results"]:
        row = result["results"][0]
        return {
            "role_id": int(row["role_id"]),
            "emoji_id": int(row["emoji_id"]) if row["emoji_id"] else None
        }
    return None


async def create_custom_role_entry(user_id: int, role_id: int, emoji_id: int = None):
    now = datetime.now(timezone.utc).isoformat()
    await d1_query(
        """INSERT INTO custom_roles (user_id, role_id, emoji_id, created_at)
           VALUES (?, ?, ?, ?)""",
        [str(user_id), str(role_id), str(emoji_id) if emoji_id else None, now]
    )


async def update_custom_role_emoji(user_id: int, emoji_id: int):
    await d1_query(
        "UPDATE custom_roles SET emoji_id = ? WHERE user_id = ?",
        [str(emoji_id), str(user_id)]
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
    """
    Generate and upload a gradient emoji for the given role/colour.
    Deletes any existing emoji with the same name.
    Stores the new emoji ID in bot_meta (for built‑in roles) or custom_roles table.
    """
    basename = basename.lower()
    if basename not in AVAILABLE_ICONS:
        raise ValueError(f"Icon '{basename}' not found.")

    # Generate the gradient image
    try:
        image_data = generate_gradient_image(AVAILABLE_ICONS[basename], color)
    except Exception as e:
        raise RuntimeError(f"Failed to generate gradient: {e}")

    emoji_name = f"role_{basename}"

    # Delete any existing emoji with the same name
    existing = discord.utils.get(guild.emojis, name=emoji_name)
    if existing:
        try:
            await existing.delete(reason="Refreshing role emoji")
        except:
            pass

    # Upload the new emoji
    try:
        emoji = await guild.create_custom_emoji(
            name=emoji_name,
            image=image_data,
            reason=f"Generated emoji for role colour #{color.value:06x}"
        )
        logger.info(f"✅ Uploaded emoji '{emoji_name}' for colour #{color.value:06x}")

        # Store the emoji ID
        if role_id and role_id in ROLE_ICON_MAP.values():
            # Built‑in role – store in bot_meta
            await store_emoji_id(basename, emoji.id)
        else:
            # Custom role – store in custom_roles table
            # We'll need to update the user's entry; we'll handle that separately
            pass
        return emoji
    except discord.Forbidden:
        logger.error("Bot lacks 'manage_emojis_and_stickers' permission.")
        raise
    except discord.HTTPException as e:
        logger.error(f"Failed to upload emoji: {e}")
        raise


# ─── Sync built‑in roles ──────────────────────────────────
async def sync_all_role_emojis(guild: discord.Guild):
    """Generate/update emojis for all built‑in roles."""
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


# ─── Modals ─────────────────────────────────────────────────
class CreateRoleModal(discord.ui.Modal, title="Create Custom Role"):
    name = discord.ui.TextInput(
        label="Role Name",
        placeholder="Max 50 characters",
        max_length=50,
        required=True
    )
    color = discord.ui.TextInput(
        label="Color (hex)",
        placeholder="#5865F2",
        required=False,
        max_length=7,
        default="#5865F2"
    )
    icon = discord.ui.TextInput(
        label="Icon (optional)",
        placeholder=f"Available: {', '.join(AVAILABLE_ICONS.keys()) or 'none'}",
        required=False,
        max_length=30,
        default=""
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        # Eligibility
        if not any(role.id in ELIGIBLE_ROLES for role in interaction.user.roles):
            return await interaction.followup.send("❌ You are not eligible.", ephemeral=True)

        # Check if they already have one
        existing = await get_custom_role(interaction.user.id)
        if existing:
            return await interaction.followup.send(
                "❌ You already have a custom role. Use Edit or Delete first.",
                ephemeral=True
            )

        # Parse colour
        color_hex = self.color.value.strip()
        try:
            if color_hex.startswith("#"):
                color_hex = color_hex[1:]
            color_int = int(color_hex, 16)
            color = discord.Color(color_int)
        except ValueError:
            color = discord.Color.blurple()

        guild = interaction.guild
        # Create the role
        try:
            role = await guild.create_role(
                name=self.name.value,
                color=color,
                reason=f"Custom role for {interaction.user}"
            )
        except Exception as e:
            return await interaction.followup.send(f"❌ Failed to create role: {e}", ephemeral=True)

        # Position at the top
        bot_top = max(guild.me.roles, key=lambda r: r.position)
        try:
            await role.edit(position=bot_top.position - 1)
        except:
            try:
                await role.edit(position=1)
            except:
                pass

        # Handle icon: generate gradient emoji
        emoji = None
        icon_name = self.icon.value.strip().lower()
        if icon_name:
            if icon_name not in AVAILABLE_ICONS:
                return await interaction.followup.send(
                    f"❌ Icon '{icon_name}' not found. Available: {', '.join(AVAILABLE_ICONS.keys())}",
                    ephemeral=True
                )
            try:
                # Generate gradient emoji based on the role's colour
                emoji = await ensure_role_emoji(guild, icon_name, role.color)
            except Exception as e:
                return await interaction.followup.send(f"❌ Failed to upload icon: {e}", ephemeral=True)

        # Assign the emoji as the role's icon (optional, but harmless even if not unlocked)
        if emoji:
            try:
                await role.edit(icon=emoji)
            except:
                pass

        await interaction.user.add_roles(role)
        await create_custom_role_entry(interaction.user.id, role.id, emoji.id if emoji else None)

        embed = discord.Embed(
            title="🆕 Custom Role Created",
            color=color,
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="User", value=interaction.user.mention, inline=True)
        embed.add_field(name="Role", value=role.mention, inline=True)
        embed.add_field(name="Color", value=f"#{color_hex.upper()}", inline=True)
        if emoji:
            embed.add_field(name="Icon", value=str(emoji), inline=True)
        await send_log(interaction.client, embed)

        await interaction.followup.send(
            f"✅ Your custom role **{role.name}** has been created!",
            ephemeral=True
        )


class EditRoleModal(discord.ui.Modal, title="Edit Custom Role"):
    def __init__(self, current_name: str, current_color: discord.Color, current_icon=None):
        super().__init__()
        hex_color = f"#{current_color.value:06x}"
        self.name = discord.ui.TextInput(
            label="Role Name",
            placeholder="Max 50 characters",
            max_length=50,
            required=True,
            default=current_name
        )
        self.color = discord.ui.TextInput(
            label="Color (hex)",
            placeholder="#5865F2",
            required=False,
            max_length=7,
            default=hex_color
        )
        self.icon = discord.ui.TextInput(
            label="Icon (optional)",
            placeholder=f"Available: {', '.join(AVAILABLE_ICONS.keys()) or 'none'}",
            required=False,
            max_length=30,
            default=""
        )
        self.add_item(self.name)
        self.add_item(self.color)
        self.add_item(self.icon)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        existing = await get_custom_role(interaction.user.id)
        if not existing:
            return await interaction.followup.send("❌ You don't have a custom role.", ephemeral=True)

        role = interaction.guild.get_role(existing["role_id"])
        if not role:
            return await interaction.followup.send("❌ Your custom role no longer exists.", ephemeral=True)

        # Parse colour
        color_hex = self.color.value.strip()
        try:
            if color_hex.startswith("#"):
                color_hex = color_hex[1:]
            color_int = int(color_hex, 16)
            color = discord.Color(color_int)
        except ValueError:
            color = discord.Color.blurple()

        # Handle icon: generate new gradient emoji if changed
        new_emoji = None
        icon_name = self.icon.value.strip().lower()
        if icon_name:
            if icon_name not in AVAILABLE_ICONS:
                return await interaction.followup.send(
                    f"❌ Icon '{icon_name}' not found. Available: {', '.join(AVAILABLE_ICONS.keys())}",
                    ephemeral=True
                )
            try:
                new_emoji = await ensure_role_emoji(interaction.guild, icon_name, color)
            except Exception as e:
                return await interaction.followup.send(f"❌ Failed to upload icon: {e}", ephemeral=True)

        # Update the role
        try:
            await role.edit(name=self.name.value, color=color, icon=new_emoji)
        except Exception as e:
            return await interaction.followup.send(f"❌ Failed to update role: {e}", ephemeral=True)

        # Update DB with new emoji ID if changed
        if new_emoji:
            await update_custom_role_emoji(interaction.user.id, new_emoji.id)

        embed = discord.Embed(
            title="✏️ Custom Role Updated",
            color=color,
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="User", value=interaction.user.mention, inline=True)
        embed.add_field(name="New Name", value=role.name, inline=True)
        embed.add_field(name="New Color", value=f"#{color_hex.upper()}", inline=True)
        if new_emoji:
            embed.add_field(name="New Icon", value=str(new_emoji), inline=True)
        await send_log(interaction.client, embed)

        await interaction.followup.send(
            f"✅ Your custom role has been updated to **{role.name}**!",
            ephemeral=True
        )


# ── Views ──────────────────────────────────────────────────
class ConfirmDeleteView(discord.ui.View):
    def __init__(self, role_id: int, user_id: int, emoji_id: int = None):
        super().__init__(timeout=60)
        self.role_id = role_id
        self.user_id = user_id
        self.emoji_id = emoji_id

    @discord.ui.button(label="Yes, delete", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ This is not your confirmation.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)

        role = interaction.guild.get_role(self.role_id)
        if role:
            await role.delete(reason=f"Custom role deleted by {interaction.user}")

        # Delete the emoji too, if any
        if self.emoji_id:
            emoji = discord.utils.get(interaction.guild.emojis, id=self.emoji_id)
            if emoji:
                try:
                    await emoji.delete(reason="Custom role deleted")
                except:
                    pass

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


class CustomRolePanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🆕 Create", style=discord.ButtonStyle.success, custom_id="cr_create")
    async def create_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not any(role.id in ELIGIBLE_ROLES for role in interaction.user.roles):
            return await safe_respond(interaction, "❌ You are not eligible.", ephemeral=True)
        await interaction.response.send_modal(CreateRoleModal())

    @discord.ui.button(label="✏️ Edit", style=discord.ButtonStyle.primary, custom_id="cr_edit")
    async def edit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not any(role.id in ELIGIBLE_ROLES for role in interaction.user.roles):
            return await safe_respond(interaction, "❌ You are not eligible.", ephemeral=True)
        existing = await get_custom_role(interaction.user.id)
        if not existing:
            return await safe_respond(interaction, "❌ You don't have a custom role.", ephemeral=True)
        role = interaction.guild.get_role(existing["role_id"])
        if not role:
            return await safe_respond(interaction, "❌ Your custom role no longer exists.", ephemeral=True)
        await interaction.response.send_modal(EditRoleModal(role.name, role.color, role.icon))

    @discord.ui.button(label="👁️ Preview", style=discord.ButtonStyle.secondary, custom_id="cr_preview")
    async def preview_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not any(role.id in ELIGIBLE_ROLES for role in interaction.user.roles):
            return await safe_respond(interaction, "❌ You are not eligible.", ephemeral=True)
        existing = await get_custom_role(interaction.user.id)
        if not existing:
            return await safe_respond(interaction, "❌ You don't have a custom role.", ephemeral=True)
        role = interaction.guild.get_role(existing["role_id"])
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
        if role.icon:
            embed.add_field(
                name="Icon",
                value=str(role.icon),
                inline=False
            )
        embed.set_footer(text="This is how your role will appear.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="🗑️ Delete", style=discord.ButtonStyle.danger, custom_id="cr_delete")
    async def delete_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not any(role.id in ELIGIBLE_ROLES for role in interaction.user.roles):
            return await safe_respond(interaction, "❌ You are not eligible.", ephemeral=True)
        existing = await get_custom_role(interaction.user.id)
        if not existing:
            return await safe_respond(interaction, "❌ You don't have a custom role.", ephemeral=True)
        role = interaction.guild.get_role(existing["role_id"])
        if not role:
            return await safe_respond(interaction, "❌ Your custom role no longer exists.", ephemeral=True)

        confirm_view = ConfirmDeleteView(role.id, interaction.user.id, existing["emoji_id"])
        await interaction.response.send_message(
            "⚠️ Are you sure you want to delete your custom role? This action cannot be undone.",
            view=confirm_view,
            ephemeral=True
        )


def build_custom_role_panel():
    """Return the embed and view for the custom role panel."""
    icon_list = ', '.join(AVAILABLE_ICONS.keys()) or 'none'
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
            f"🆕 **Create** – set a name (max 50 chars), a hex color, and an optional icon.\n"
            f"   Available icons: {icon_list}\n"
            "✏️ **Edit** – change the name, color, or icon.\n"
            "👁️ **Preview** – see how your role looks.\n"
            "🗑️ **Delete** – remove your role.\n\n"
            "🔹 **Gradient emojis** are automatically generated from your chosen colour."
        ),
        inline=False
    )
    embed.set_footer(text="Icons require server boosts (Level 2+) to display as role icons. Emojis are still usable.")
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
        # Ensure database table exists
        await ensure_db()

        guild = self.bot.get_guild(GUILD_ID)
        if not guild:
            return

        # Sync built‑in role emojis once on startup
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