import discord
from discord import app_commands
from discord.ext import commands
import asyncio
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
BOT_ROLE_ID = 1535635733791121443

logger = logging.getLogger(__name__)

# ─── Mapping: base name → role ID ──────────────────────
ROLE_ICON_MAP = {
    "bot": 1535558679959576688,
    "founder": 1535553078852325506,
    "headstaff": 1535558010431340604,
    "staff": 1536740055643594782,
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
# Neutral grey used when a role has no colour set at all.
_DEFAULT_ICON_COLOR = (153, 170, 181)  # Discord "greyple"


def make_lighter(rgb: tuple, factor: float = 0.6) -> tuple:
    """Blend an RGB colour toward white by `factor` (0=unchanged, 1=white)."""
    r, g, b = rgb
    return (
        min(255, r + int((255 - r) * factor)),
        min(255, g + int((255 - g) * factor)),
        min(255, b + int((255 - b) * factor)),
    )


def role_colors(role: discord.Role) -> tuple:
    """Return (primary_rgb, secondary_rgb) for icon generation.
    primary  = the role's flat colour (or default grey if unset).
    secondary = a lighter version of primary, used as the gradient stop.
    """
    if role.color.value:
        primary = (role.color.r, role.color.g, role.color.b)
    else:
        primary = _DEFAULT_ICON_COLOR
    return primary, make_lighter(primary)


def generate_gradient_image(base_path: Path, primary_rgb, secondary_rgb=None) -> bytes:
    """
    primary_rgb  : (r, g, b) — the sole colour or the left gradient stop.
    secondary_rgb: (r, g, b) — the right gradient stop, or None for a flat fill.
    """
    if not HAS_PIL:
        raise RuntimeError("Pillow is not installed.")

    img = Image.open(base_path).convert("RGBA")
    width, height = img.size
    grad_img = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(grad_img)

    if secondary_rgb is not None:
        # Two-colour gradient using Discord's actual gradient stops
        r1, g1, b1 = primary_rgb
        r2, g2, b2 = secondary_rgb
        for x in range(width):
            t = x / width
            draw.line(
                [(x, 0), (x, height)],
                fill=(int(r1 + (r2 - r1) * t), int(g1 + (g2 - g1) * t), int(b1 + (b2 - b1) * t), 255),
                width=1,
            )
    else:
        # Single flat colour
        r, g, b = primary_rgb
        draw.rectangle([(0, 0), (width, height)], fill=(r, g, b, 255))

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
        val = result["results"][0]["value"]
        if val and val != "None":
            try:
                return int(val)
            except (ValueError, TypeError):
                pass
    return None


async def store_emoji_id(basename: str, emoji_id: int):
    if emoji_id is None:
        await d1_query(
            "DELETE FROM bot_meta WHERE key = ?",
            [f"role_emoji_{basename}"]
        )
        return
    await d1_query(
        "INSERT OR REPLACE INTO bot_meta (key, value) VALUES (?, ?)",
        [f"role_emoji_{basename}", str(emoji_id)]
    )


# ─── Emoji management ─────────────────────────────────────
async def ensure_role_emoji(
    guild: discord.Guild,
    basename: str,
    primary_rgb: tuple = None,
    secondary_rgb: tuple = None,
    role_id: int = None,
    force: bool = False,
) -> discord.Emoji:
    """
    Create or reuse a gradient guild emoji for a role.

    primary_rgb / secondary_rgb are pre-resolved (r, g, b) tuples.
    Set force=True to delete and regenerate so colour changes are
    always reflected (used by /syncemojis).
    """
    basename = basename.lower()
    if basename not in AVAILABLE_ICONS:
        raise ValueError(f"Icon '{basename}' not found.")

    emoji_name = f"role_{basename}"
    existing = discord.utils.get(guild.emojis, name=emoji_name)

    if existing and not force:
        await store_emoji_id(basename, existing.id)
        logger.info(f"✅ Reusing existing emoji '{emoji_name}' (ID: {existing.id})")
        return existing

    if existing and force:
        try:
            await existing.delete(reason="Regenerating role emoji with updated colours")
            await store_emoji_id(basename, None)
            logger.info(f"🗑️ Deleted old emoji '{emoji_name}' for regeneration")
        except Exception as e:
            logger.warning(f"Could not delete old emoji '{emoji_name}': {e}")

    if not force:
        stored_id = await get_stored_emoji_id(basename)
        if stored_id:
            cached = discord.utils.get(guild.emojis, id=stored_id)
            if cached and cached.name == emoji_name:
                logger.info(f"✅ Using cached emoji '{emoji_name}' (ID: {stored_id})")
                return cached
            await store_emoji_id(basename, None)

    if primary_rgb is None:
        primary_rgb = _DEFAULT_ICON_COLOR

    try:
        image_data = generate_gradient_image(AVAILABLE_ICONS[basename], primary_rgb, secondary_rgb)
    except Exception as e:
        raise RuntimeError(f"Failed to generate image: {e}")

    def rgb_hex(t): return f"#{(t[0] << 16 | t[1] << 8 | t[2]):06x}"
    color_desc = rgb_hex(primary_rgb) + (f" → {rgb_hex(secondary_rgb)}" if secondary_rgb else "")
    try:
        emoji = await guild.create_custom_emoji(
            name=emoji_name,
            image=image_data,
            reason=f"Generated emoji ({color_desc})",
        )
        logger.info(f"✅ Uploaded emoji '{emoji_name}' ({color_desc})")
        await store_emoji_id(basename, emoji.id)
        return emoji
    except discord.Forbidden:
        logger.error("Bot lacks 'manage_emojis_and_stickers' permission.")
        raise
    except discord.HTTPException as e:
        logger.error(f"Failed to upload emoji: {e}")
        raise


# ─── Sync built‑in roles ──────────────────────────────────
async def sync_all_role_emojis(guild: discord.Guild, force_emoji: bool = False):
    """
    Update role icons for every entry in ROLE_ICON_MAP.

    Native display_icon is always refreshed (role-edit endpoint, no rate limit).
    Guild emojis are only created when missing; pass force_emoji=True to delete
    and recreate them (costs emoji-creation quota — use sparingly).
    """
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

            primary, secondary = role_colors(role)
            img_bytes = generate_gradient_image(AVAILABLE_ICONS[basename], primary, secondary)

            # Always update the native role icon (role-edit endpoint, no emoji quota)
            icon_note = ""
            try:
                await role.edit(display_icon=img_bytes)
                icon_note = " 🖼️"
            except (discord.Forbidden, discord.HTTPException):
                icon_note = ""

            # Only (re)create the guild emoji if missing or force_emoji=True
            emoji = await ensure_role_emoji(
                guild, basename,
                primary_rgb=primary, secondary_rgb=secondary,
                role_id=role_id, force=force_emoji,
            )

            def rgb_hex(t): return f"#{(t[0] << 16 | t[1] << 8 | t[2]):06x}"
            results.append(f"✅ `{basename}` → {emoji}{icon_note} ({rgb_hex(primary)} → {rgb_hex(secondary)})")

            await asyncio.sleep(0.3)

        except Exception as e:
            results.append(f"❌ `{basename}` – {e}")
    return results


async def _sync_all_icons_bg(guild: discord.Guild):
    """Background icon sync — only updates native icons, never recreates emojis."""
    try:
        await sync_all_role_emojis(guild, force_emoji=False)
        print(f"✅ Background icon sync completed for {guild.name}")
    except Exception as e:
        logger.error(f"Background icon sync failed: {e}")


# ─── Apply icon to a single role ──────────────────────────
async def apply_icon_to_role(
    guild: discord.Guild,
    role: discord.Role,
    icon_name: str,
    primary_rgb: tuple = None,
    secondary_rgb: tuple = None,
) -> str:
    """
    Apply an icon to a role. Tries native display_icon first (Level 2+),
    falls back to generating a gradient emoji.

    primary_rgb / secondary_rgb override the auto-derived colours.
    If omitted, colours come from role.color + make_lighter().
    """
    if icon_name not in AVAILABLE_ICONS:
        return f"❌ Icon `{icon_name}` not found."
    try:
        if primary_rgb is None:
            primary_rgb, secondary_rgb = role_colors(role)
        elif secondary_rgb is None:
            secondary_rgb = make_lighter(primary_rgb)

        img_bytes = generate_gradient_image(AVAILABLE_ICONS[icon_name], primary_rgb, secondary_rgb)
        try:
            await role.edit(display_icon=img_bytes)
            return f"✅ Native icon **{icon_name}** applied to {role.mention}."
        except (discord.Forbidden, discord.HTTPException):
            emoji = await ensure_role_emoji(
                guild, icon_name,
                primary_rgb=primary_rgb, secondary_rgb=secondary_rgb,
                role_id=role.id, force=True,
            )
            return f"✅ Emoji {emoji} applied to {role.mention} (server not Level 2)."
    except Exception as e:
        return f"❌ Failed to apply icon: {e}"


# ─── Helper to safely respond ─────────────────────────────
async def safe_respond(interaction, *args, **kwargs):
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(*args, **kwargs)
        else:
            await interaction.followup.send(*args, **kwargs)
    except discord.NotFound:
        pass


# ─── Icon Picker ───────────────────────────────────────────
class IconPickerView(discord.ui.View):
    """Ephemeral Select-based picker for choosing an icon design."""

    def __init__(
        self,
        guild: discord.Guild,
        user_id: int,
        role_color: discord.Color,
        target_role: discord.Role = None,
        builder_view: "RoleBuilderView" = None,
    ):
        super().__init__(timeout=60)
        self.guild = guild
        self.user_id = user_id
        self.role_color = role_color
        self.target_role = target_role
        self.builder_view = builder_view

        icon_names = sorted(AVAILABLE_ICONS.keys())[:25]
        options = [
            discord.SelectOption(label=name.replace("_", " ").title(), value=name)
            for name in icon_names
        ]

        self._select = discord.ui.Select(
            placeholder="Choose an icon design…",
            options=options,
            min_values=1,
            max_values=1,
        )
        self._select.callback = self._on_select
        self.add_item(self._select)

    async def _on_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ Not your session.", ephemeral=True)

        chosen = self._select.values[0]
        await interaction.response.defer()

        # Store in builder data and refresh its embed
        if self.builder_view:
            self.builder_view.data["icon"] = chosen
            if self.builder_view.original_interaction:
                try:
                    await self.builder_view.original_interaction.edit_original_response(
                        embed=self.builder_view.build_embed(),
                        view=self.builder_view,
                    )
                except Exception:
                    pass

        # Apply to existing role immediately (edit mode or panel button)
        apply_msg = ""
        if self.target_role:
            apply_msg = "\n" + await apply_icon_to_role(self.guild, self.target_role, chosen)

        # Sync all built-in role icons in background
        asyncio.create_task(_sync_all_icons_bg(self.guild))

        lines = [
            f"✅ Icon set to **{chosen}**.{apply_msg}",
            "🔄 Syncing icons for all built-in roles in the background…",
        ]
        if not self.target_role and self.builder_view:
            lines.append("Click **Apply Changes** in the builder to save your role.")

        await interaction.followup.send("\n".join(lines), ephemeral=True)
        self.stop()


# ─── Role Builder Views ────────────────────────────────────
class RoleBuilderView(discord.ui.View):
    def __init__(
        self,
        user_id: int,
        mode: str = "create",
        existing_data: dict = None,
        original_interaction: discord.Interaction = None,
    ):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.mode = mode
        self.original_interaction = original_interaction
        self.data = {
            "name": "",
            "color": discord.Color.blurple(),
            "color2": None,   # optional icon gradient stop (auto-lightened if None)
            "icon": None,
        }
        if existing_data:
            self.data["name"] = existing_data.get("name", "")
            self.data["color"] = existing_data.get("color", discord.Color.blurple())
            self.data["color2"] = existing_data.get("color2")
            self.data["icon"] = existing_data.get("icon")

    def build_embed(self) -> discord.Embed:
        color = self.data["color"]
        color2 = self.data.get("color2")

        color_val = f"`#{color.value:06x}`"
        if color2 is not None:
            color_val += f"  →  `#{color2.value:06x}` *(icon gradient)*"
        else:
            # Show the auto-derived lighter stop as a hint
            p = (color.r, color.g, color.b)
            s = make_lighter(p)
            lighter_hex = f"#{(s[0] << 16 | s[1] << 8 | s[2]):06x}"
            color_val += f"  →  `{lighter_hex}` *(auto)*"

        embed = discord.Embed(
            title="🎨 Custom Role Builder",
            description=(
                "**Mode:** " + ("Creating" if self.mode == "create" else "Editing") + " your custom role.\n"
                "Use the buttons below to change each field, then click **Apply Changes** to save.\n\n"
                "**Color:** `#rrggbb` sets the role colour. Add `, #rrggbb` to set a custom icon gradient stop.\n"
                "**Note:** Role icons require the server to be Level 2+."
            ),
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Name", value=self.data["name"] or "*(not set)*", inline=True)
        embed.add_field(name="Icon Color", value=color_val, inline=False)
        if self.data.get("icon"):
            embed.add_field(name="Icon", value=f"`{self.data['icon']}`", inline=True)
        embed.set_footer(text="Changes are not applied until you click 'Apply'.")
        return embed

    async def update_embed(self, interaction: discord.Interaction):
        """
        Update the builder embed.

        Component interactions (buttons/selects): edit_message works directly.
        Modal submissions: edit_message raises ClientException (no message);
          defer the modal then edit via the stored original_interaction token.
        """
        embed = self.build_embed()

        if interaction.type == discord.InteractionType.modal_submit:
            # Acknowledge the modal so Discord doesn't show "interaction failed"
            if not interaction.response.is_done():
                try:
                    await interaction.response.defer()
                except Exception:
                    pass

            # Edit the original builder message via the panel-button token
            if self.original_interaction:
                try:
                    await self.original_interaction.edit_original_response(embed=embed, view=self)
                    return
                except discord.NotFound:
                    # Token expired (>15 min) — let the user know
                    try:
                        await interaction.followup.send(
                            "⚠️ Builder session expired. Please reopen the role panel.",
                            ephemeral=True,
                        )
                    except Exception:
                        pass
                    return
                except Exception as e:
                    logger.error(f"update_embed modal edit failed: {e}")
                    try:
                        await interaction.followup.send(
                            f"⚠️ Could not update preview: {e}", ephemeral=True
                        )
                    except Exception:
                        pass
            return

        # Component interaction (button / select) — edit the message in place
        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(embed=embed, view=self)
            else:
                await interaction.edit_original_response(embed=embed, view=self)
        except Exception as e:
            logger.error(f"update_embed component edit failed: {e}")

    async def _check_user(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ This is not your builder session.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Change Name", style=discord.ButtonStyle.primary)
    async def change_name(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_user(interaction):
            return
        await interaction.response.send_modal(NameModal(self))

    @discord.ui.button(label="Change Color", style=discord.ButtonStyle.primary)
    async def change_color(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_user(interaction):
            return
        await interaction.response.send_modal(ColorModal(self))

    @discord.ui.button(label="Change Icon", style=discord.ButtonStyle.secondary)
    async def change_icon(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_user(interaction):
            return
        if not AVAILABLE_ICONS:
            return await interaction.response.send_message(
                "❌ No icons available. Add PNG files to the `icons/` folder.", ephemeral=True
            )

        target_role = None
        if self.mode == "edit":
            existing_id = await get_custom_role(interaction.user.id)
            if existing_id:
                target_role = interaction.guild.get_role(existing_id)

        view = IconPickerView(
            guild=interaction.guild,
            user_id=self.user_id,
            role_color=self.data["color"],
            target_role=target_role,
            builder_view=self,
        )
        await interaction.response.send_message("🎨 **Select an icon for your custom role:**", view=view, ephemeral=True)

    @discord.ui.button(label="Apply Changes", style=discord.ButtonStyle.success)
    async def apply_changes(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_user(interaction):
            return

        if not self.data["name"]:
            return await interaction.response.send_message("❌ Please set a name first.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        try:
            if self.mode == "create":
                role = await guild.create_role(
                    name=self.data["name"],
                    color=self.data["color"],
                    reason=f"Custom role created by {interaction.user}",
                )

                bot_role = guild.get_role(BOT_ROLE_ID)
                if bot_role:
                    target_position = max(1, bot_role.position - 1)
                    try:
                        await role.edit(position=target_position)
                    except Exception:
                        try:
                            bot_top = max(guild.me.roles, key=lambda r: r.position)
                            await role.edit(position=max(1, bot_top.position - 1))
                        except Exception:
                            pass
                else:
                    try:
                        bot_top = max(guild.me.roles, key=lambda r: r.position)
                        await role.edit(position=max(1, bot_top.position - 1))
                    except Exception:
                        pass

                await interaction.user.add_roles(role)
                await create_custom_role_entry(interaction.user.id, role.id)

                # Apply icon if one was chosen
                icon_msg = ""
                if self.data.get("icon") and self.data["icon"] in AVAILABLE_ICONS:
                    p = (self.data["color"].r, self.data["color"].g, self.data["color"].b)
                    c2 = self.data.get("color2")
                    s = (c2.r, c2.g, c2.b) if c2 is not None else make_lighter(p)
                    icon_msg = "\n" + await apply_icon_to_role(guild, role, self.data["icon"], p, s)

                embed = discord.Embed(
                    title="✅ Custom Role Created",
                    color=self.data["color"],
                    timestamp=datetime.now(timezone.utc),
                )
                embed.add_field(name="Role", value=role.mention, inline=True)
                embed.add_field(name="Name", value=self.data["name"], inline=True)
                embed.add_field(name="Color", value=f"#{self.data['color'].value:06x}", inline=True)
                if self.data.get("icon"):
                    embed.add_field(name="Icon", value=self.data["icon"], inline=True)
                await send_log(interaction.client, embed)

                for child in self.children:
                    child.disabled = True
                await interaction.edit_original_response(embed=embed, view=self)
                await interaction.followup.send(
                    f"✅ Your custom role **{role.name}** has been created!{icon_msg}",
                    ephemeral=True,
                )

            else:  # edit
                existing = await get_custom_role(interaction.user.id)
                if not existing:
                    return await interaction.followup.send("❌ No custom role found to edit.", ephemeral=True)
                role = guild.get_role(existing)
                if not role:
                    return await interaction.followup.send("❌ Your custom role no longer exists.", ephemeral=True)

                await role.edit(name=self.data["name"], color=self.data["color"])

                # Apply icon if one was chosen
                icon_msg = ""
                if self.data.get("icon") and self.data["icon"] in AVAILABLE_ICONS:
                    p = (self.data["color"].r, self.data["color"].g, self.data["color"].b)
                    c2 = self.data.get("color2")
                    s = (c2.r, c2.g, c2.b) if c2 is not None else make_lighter(p)
                    icon_msg = "\n" + await apply_icon_to_role(guild, role, self.data["icon"], p, s)

                embed = discord.Embed(
                    title="✏️ Custom Role Updated",
                    color=self.data["color"],
                    timestamp=datetime.now(timezone.utc),
                )
                embed.add_field(name="Role", value=role.mention, inline=True)
                embed.add_field(name="New Name", value=self.data["name"], inline=True)
                embed.add_field(name="New Color", value=f"#{self.data['color'].value:06x}", inline=True)
                if self.data.get("icon"):
                    embed.add_field(name="Icon", value=self.data["icon"], inline=True)
                await send_log(interaction.client, embed)

                for child in self.children:
                    child.disabled = True
                await interaction.edit_original_response(embed=embed, view=self)
                await interaction.followup.send(
                    f"✅ Your custom role has been updated to **{role.name}**!{icon_msg}",
                    ephemeral=True,
                )

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_user(interaction):
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
            default=view.data["name"],
        )
        self.add_item(self.name_input)

    async def on_submit(self, interaction: discord.Interaction):
        self.view.data["name"] = self.name_input.value
        await self.view.update_embed(interaction)


class ColorModal(discord.ui.Modal, title="Change Role Color"):
    def __init__(self, view: RoleBuilderView):
        super().__init__()
        self.view = view

        default = f"#{view.data['color'].value:06x}"
        if view.data.get("color2") is not None:
            default += f", #{view.data['color2'].value:06x}"

        self.color_input = discord.ui.TextInput(
            label="Role color  (+ optional icon gradient stop)",
            placeholder="#5865F2  or  #5865F2, #ff79c6",
            required=True,
            max_length=17,   # "#xxxxxx, #xxxxxx"
            default=default,
        )
        self.add_item(self.color_input)

    @staticmethod
    def _parse_hex(raw: str):
        """Strip whitespace and # then parse as RGB hex integer."""
        raw = raw.strip().lstrip("#").strip()
        if len(raw) not in (3, 6):
            return None
        if len(raw) == 3:          # expand shorthand e.g. fff → ffffff
            raw = raw[0]*2 + raw[1]*2 + raw[2]*2
        try:
            return discord.Color(int(raw, 16))
        except ValueError:
            return None

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.color_input.value.strip()
        parts = [p.strip() for p in raw.split(",")]

        color1 = self._parse_hex(parts[0]) if parts else None
        if color1 is None:
            await interaction.response.send_message(
                f"❌ `{parts[0] if parts else ''}` is not a valid hex colour.\n"
                "Use `#rrggbb` for the role colour, or `#rrggbb, #rrggbb` to also set a custom icon gradient stop.",
                ephemeral=True,
            )
            return

        color2 = None
        if len(parts) > 1 and parts[1]:
            color2 = self._parse_hex(parts[1])
            if color2 is None:
                await interaction.response.send_message(
                    f"❌ `{parts[1]}` is not a valid hex colour for the icon gradient stop.",
                    ephemeral=True,
                )
                return

        self.view.data["color"] = color1
        self.view.data["color2"] = color2   # None → icon gradient uses auto-lighter
        await self.view.update_embed(interaction)


# ─── Panel View ────────────────────────────────────────────
class CustomRolePanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🆕 Manage Role", style=discord.ButtonStyle.success, custom_id="cr_manage")
    async def manage_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not any(role.id in ELIGIBLE_ROLES for role in interaction.user.roles):
                return await interaction.response.send_message("❌ You are not eligible.", ephemeral=True)
            await interaction.response.defer(ephemeral=True)
        except discord.NotFound:
            return

        existing = await get_custom_role(interaction.user.id)
        mode = "edit" if existing else "create"

        if existing:
            role = interaction.guild.get_role(existing)
            if not role:
                return await interaction.followup.send("❌ Your custom role no longer exists.", ephemeral=True)
            existing_data = {"name": role.name, "color": role.color}
        else:
            existing_data = None

        view = RoleBuilderView(
            user_id=interaction.user.id,
            mode=mode,
            existing_data=existing_data,
            original_interaction=interaction,
        )
        embed = view.build_embed()
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="🎨 Change Icon", style=discord.ButtonStyle.secondary, custom_id="cr_change_icon")
    async def change_icon(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not any(role.id in ELIGIBLE_ROLES for role in interaction.user.roles):
                return await interaction.response.send_message("❌ You are not eligible.", ephemeral=True)
            if not AVAILABLE_ICONS:
                return await interaction.response.send_message(
                    "❌ No icons available. Add PNG files to the `icons/` folder.",
                    ephemeral=True,
                )
            await interaction.response.defer(ephemeral=True)
        except discord.NotFound:
            return

        existing = await get_custom_role(interaction.user.id)
        target_role = None
        role_color = discord.Color.blurple()

        if existing:
            target_role = interaction.guild.get_role(existing)
            if target_role:
                role_color = target_role.color

        view = IconPickerView(
            guild=interaction.guild,
            user_id=interaction.user.id,
            role_color=role_color,
            target_role=target_role,
        )
        await interaction.followup.send("🎨 **Select an icon for your custom role:**", view=view, ephemeral=True)

    @discord.ui.button(label="👁️ Preview", style=discord.ButtonStyle.secondary, custom_id="cr_preview")
    async def preview_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not any(role.id in ELIGIBLE_ROLES for role in interaction.user.roles):
                return await interaction.response.send_message("❌ You are not eligible.", ephemeral=True)
            await interaction.response.defer(ephemeral=True)
        except discord.NotFound:
            return

        existing = await get_custom_role(interaction.user.id)
        if not existing:
            return await interaction.followup.send("❌ You don't have a custom role.", ephemeral=True)
        role = interaction.guild.get_role(existing)
        if not role:
            return await interaction.followup.send("❌ Your custom role no longer exists.", ephemeral=True)

        embed = discord.Embed(
            title="👁️ Role Preview",
            description=f"**{role.mention}**",
            color=role.color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Color", value=f"`#{role.color.value:06x}`", inline=False)
        embed.set_footer(text="This is how your role will appear.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="🗑️ Delete", style=discord.ButtonStyle.danger, custom_id="cr_delete")
    async def delete_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not any(role.id in ELIGIBLE_ROLES for role in interaction.user.roles):
                return await interaction.response.send_message("❌ You are not eligible.", ephemeral=True)
            await interaction.response.defer(ephemeral=True)
        except discord.NotFound:
            return

        existing = await get_custom_role(interaction.user.id)
        if not existing:
            return await interaction.followup.send("❌ You don't have a custom role.", ephemeral=True)
        role = interaction.guild.get_role(existing)
        if not role:
            return await interaction.followup.send("❌ Your custom role no longer exists.", ephemeral=True)

        confirm_view = ConfirmDeleteView(role.id, interaction.user.id)
        await interaction.followup.send(
            "⚠️ Are you sure you want to delete your custom role? This action cannot be undone.",
            view=confirm_view,
            ephemeral=True,
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
            timestamp=datetime.now(timezone.utc),
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
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="Instructions",
        value=(
            "🆕 **Manage Role** – create or edit your custom role (name, color, icon).\n"
            "🎨 **Change Icon** – instantly apply an icon design to your custom role\n"
            "   and sync icons for all built-in roles.\n"
            "👁️ **Preview** – see how your role currently looks.\n"
            "🗑️ **Delete** – remove your role.\n\n"
            "🔹 **Native role icons** require the server to reach Level 2.\n"
            "🔹 **Gradient emojis** are generated for all roles automatically."
        ),
        inline=False,
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
        print("✅ Custom Roles database ready (no auto‑sync to avoid rate limits).")

    @app_commands.command(
        name="syncemojis",
        description="Update role icons for all built-in roles (Staff only)"
    )
    @app_commands.describe(
        force="Delete and recreate guild emojis (uses emoji quota — only if icons look wrong)"
    )
    @app_commands.checks.cooldown(1, 60)
    async def syncemojis(self, interaction: discord.Interaction, force: bool = False):
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            else:
                await interaction.followup.send("Something went wrong, please try again.", ephemeral=True)
                return
        except discord.NotFound:
            return

        if not is_staff(interaction.user):
            return await interaction.followup.send("❌ You do not have permission.", ephemeral=True)

        guild = interaction.guild
        results = await sync_all_role_emojis(guild, force_emoji=force)

        embed = discord.Embed(
            title="🔄 Role Emoji Generation Results",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="Details",
            value="\n".join(results[:25]) + (f"\n... and {len(results)-25} more" if len(results) > 25 else ""),
            inline=False,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(CustomRoles(bot))
    print("✅ Custom Roles cog loaded")
