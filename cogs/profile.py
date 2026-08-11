"""
/profile — image card.
Layout: large username + role on one line, stat columns, PvP row.
Rank icon = large semi-transparent watermark on the far right.
"""

import io
import os
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone
from PIL import Image, ImageDraw, ImageFont

from bot import d1_query

# ── PvP rank tiers ─────────────────────────────────────────────────────────────
PVP_TIERS = [
    ("Master",   (255, 107, 107), (139,   0,   0)),
    ("Amethyst", (192, 132, 252), (109,  40, 217)),
    ("Platinum", (103, 232, 249), (  8, 145, 178)),
    ("Gold",     (252, 211,  77), (183, 121,  31)),
    ("Silver",   (229, 231, 235), (107, 114, 128)),
    ("Bronze",   (217, 119,   6), (124,  45,  18)),
    ("Unranked", ( 75,  85,  99), ( 55,  65,  81)),
]

def _pvp_rank_colors(rank_name: str) -> tuple[tuple, tuple]:
    for name, color, dark in PVP_TIERS:
        if name == rank_name:
            return color, dark
    return (75, 85, 99), (55, 65, 81)

_RANKS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'ranks')

def _load_rank_icon(rank_name: str) -> "Image.Image | None":
    name = rank_name.lower()
    for filename in (f"{name}-3.png", f"{name}.png"):
        path = os.path.join(_RANKS_DIR, filename)
        if os.path.exists(path):
            try:
                return Image.open(path).convert("RGBA")
            except Exception:
                pass
    return None

def _xp_needed(level: int) -> int:
    return 5 * (level ** 2) + 50 * level + 100

def _total_xp_for_level(level: int) -> int:
    return sum(_xp_needed(l) for l in range(level))

# ── Fonts ──────────────────────────────────────────────────────────────────────
_font_cache: dict = {}

def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    key = (size, bold)
    if key in _font_cache:
        return _font_cache[key]
    candidates = (
        [
            "C:/Windows/Fonts/arialbd.ttf",
            "C:/Windows/Fonts/calibrib.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
        ] if bold else [
            "C:/Windows/Fonts/arial.ttf",
            "C:/Windows/Fonts/calibri.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        ]
    )
    f = None
    for path in candidates:
        if os.path.exists(path):
            try:
                f = ImageFont.truetype(path, size)
                break
            except Exception:
                continue
    _font_cache[key] = f or ImageFont.load_default()
    return _font_cache[key]

def _tsize(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]

def _rrect(draw: ImageDraw.ImageDraw, bbox, radius: int, fill):
    try:
        draw.rounded_rectangle(bbox, radius=radius, fill=fill)
    except AttributeError:
        draw.rectangle(bbox, fill=fill)

# ── Card constants ─────────────────────────────────────────────────────────────
W, H        = 960, 280
BG          = (13, 17, 23)
BG2         = (22, 27, 34)
MUTED       = (110, 118, 129)
WHITE       = (255, 255, 255)
BAR_BG      = (33, 38, 45)
SEP         = (33, 40, 50)
AV_SIZE     = 130
AV_X, AV_Y = 20, (H - AV_SIZE) // 2

# Font sizes
SZ_NAME  = 30   # username + role (same line)
SZ_SUB   = 16   # sub-line info
SZ_LABEL = 14   # stat labels
SZ_VAL   = 23   # stat values
SZ_PVP   = 21   # PvP rank value


async def _fetch_avatar(url: str) -> Image.Image | None:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(str(url), timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status == 200:
                    return Image.open(io.BytesIO(await r.read())).convert("RGBA")
    except Exception:
        return None


async def _fetch_role_icon(role: discord.Role) -> Image.Image | None:
    if not role.icon:
        return None
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(str(role.icon.url), timeout=aiohttp.ClientTimeout(total=4)) as r:
                if r.status == 200:
                    return Image.open(io.BytesIO(await r.read())).convert("RGBA")
    except Exception:
        return None


def _circle(img: Image.Image, size: int) -> Image.Image:
    img = img.resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, size - 1, size - 1], fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, mask=mask)
    return out


def _apply_alpha(img: Image.Image, opacity: float) -> Image.Image:
    """Return a copy of img with alpha multiplied by opacity (0.0–1.0)."""
    r, g, b, a = img.split()
    a = a.point(lambda x: int(x * opacity))
    return Image.merge("RGBA", (r, g, b, a))


async def _build_card(
    target: discord.Member,
    level: int, exp: int, global_rank,
    weekly_pts: int, lifetime_pts: int, raids: int,
    pvp_rank: str, pvp_elo: int, pvp_wins: int, pvp_losses: int,
    tenure: str, accent: tuple,
    top_role: discord.Role | None = None,
    role_icon: Image.Image | None = None,
) -> io.BytesIO:

    # ── Background ─────────────────────────────────────────────────────────
    card = Image.new("RGBA", (W, H), (*BG, 255))
    d = ImageDraw.Draw(card)

    for y in range(H):
        t = y / H
        row_col = tuple(int(BG[i] + (BG2[i] - BG[i]) * t) for i in range(3))
        d.line([(0, y), (W, y)], fill=(*row_col, 255))

    # ── Rank icon watermark (far right, behind everything) ─────────────────
    rank_icon = _load_rank_icon(pvp_rank)
    WM_SIZE = 250
    if rank_icon:
        wm = rank_icon.resize((WM_SIZE, WM_SIZE), Image.LANCZOS)
        wm = _apply_alpha(wm, 0.30)          # 30% opacity — "slightly visible"
        wm_x = W - WM_SIZE + 15              # slightly clipped into right edge
        wm_y = (H - WM_SIZE) // 2
        card.paste(wm, (wm_x, wm_y), wm)

    # ── Left accent strip ──────────────────────────────────────────────────
    d.rectangle([0, 0, 7, H], fill=(*accent, 255))

    # ── Avatar ─────────────────────────────────────────────────────────────
    av_img = await _fetch_avatar(target.display_avatar.url)
    if av_img:
        ring_sz = AV_SIZE + 8
        ring = Image.new("RGBA", (ring_sz, ring_sz), (0, 0, 0, 0))
        ImageDraw.Draw(ring).ellipse([0, 0, ring_sz - 1, ring_sz - 1], fill=(*accent, 255))
        card.paste(ring, (AV_X - 4, AV_Y - 4), ring)
        av_circle = _circle(av_img, AV_SIZE)
        card.paste(av_circle, (AV_X, AV_Y), av_circle)
    else:
        d.ellipse([AV_X, AV_Y, AV_X + AV_SIZE, AV_Y + AV_SIZE], fill=(*BG2, 255))

    # ── Fonts ──────────────────────────────────────────────────────────────
    fn_name  = _font(SZ_NAME,  bold=True)
    fn_sub   = _font(SZ_SUB)
    fn_label = _font(SZ_LABEL)
    fn_val   = _font(SZ_VAL,   bold=True)
    fn_pvp   = _font(SZ_PVP,   bold=True)

    CX    = AV_X + AV_SIZE + 26
    COL_W = 170

    # ── Line 1: Username + [role icon] + Role name ─────────────────────────
    name_str = target.display_name[:22]
    d.text((CX, 16), name_str, font=fn_name, fill=WHITE)
    name_w, _ = _tsize(d, name_str, fn_name)

    if top_role and top_role.name != "@everyone":
        role_color = (
            (top_role.color.r, top_role.color.g, top_role.color.b)
            if top_role.color.value else MUTED
        )
        ICON_SZ = SZ_NAME - 4   # 26 px — matches cap height of name font
        gap     = 14
        icon_x  = CX + name_w + gap
        icon_y  = 16 + 4        # +4 to vertically centre icon with text cap height

        if role_icon:
            ri = _circle(role_icon, ICON_SZ)
            card.paste(ri, (icon_x, icon_y), ri)
        else:
            d.ellipse(
                [icon_x, icon_y, icon_x + ICON_SZ, icon_y + ICON_SZ],
                fill=(*role_color, 255),
            )

        role_x = icon_x + ICON_SZ + 8
        d.text((role_x, 16), top_role.name[:18], font=fn_name, fill=role_color)

    # ── Sub-line ───────────────────────────────────────────────────────────
    rank_str = f"#{global_rank}" if str(global_rank) != "N/A" else "-"
    d.text(
        (CX, 58),
        f"Level {level}  \xb7  {rank_str} Global  \xb7  Member for {tenure}  \xb7  {exp:,} XP",
        font=fn_sub,
        fill=MUTED,
    )

    # ── XP bar ─────────────────────────────────────────────────────────────
    xp_cur  = _total_xp_for_level(level)
    xp_next = _total_xp_for_level(level + 1)
    needed  = xp_next - xp_cur
    prog    = max(0, exp - xp_cur)
    pct     = min(100, int(prog / needed * 100)) if needed else 100

    BAR_X, BAR_Y = CX, 90
    BAR_W = COL_W * 3 - 10
    BAR_H = 14

    _rrect(d, [BAR_X, BAR_Y, BAR_X + BAR_W, BAR_Y + BAR_H], BAR_H // 2, BAR_BG)
    fill_w = max(BAR_H, int(BAR_W * pct / 100))
    _rrect(d, [BAR_X, BAR_Y, BAR_X + fill_w, BAR_Y + BAR_H], BAR_H // 2, accent)
    fn_pct = _font(SZ_LABEL, bold=True)
    d.text((BAR_X + BAR_W + 10, BAR_Y), f"{pct}% to Level {level + 1}", font=fn_pct, fill=MUTED)

    # ── Divider 1 ──────────────────────────────────────────────────────────
    d.line([(CX, 120), (W - 18, 120)], fill=SEP, width=1)

    # ── Hoster stats ────────────────────────────────────────────────────────
    for i, (label, value) in enumerate([
        ("Weekly Points",   f"{weekly_pts:,}"),
        ("Lifetime Points", f"{lifetime_pts:,}"),
        ("Raids Hosted",    f"{raids:,}"),
    ]):
        sx = CX + i * COL_W
        d.text((sx, 128), label, font=fn_label, fill=MUTED)
        d.text((sx, 147), value, font=fn_val,   fill=WHITE)

    # ── Divider 2 ──────────────────────────────────────────────────────────
    d.line([(CX, 188), (W - 18, 188)], fill=SEP, width=1)

    # ── PvP row — no inline icon, just colored text ─────────────────────────
    pvp_col, _ = _pvp_rank_colors(pvp_rank)
    pvp_rec = f"{pvp_wins} / {pvp_losses}" if (pvp_wins or pvp_losses) else "- / -"
    elo_str = str(pvp_elo) if pvp_elo else "-"

    for i, (label, value, color) in enumerate([
        ("PvP Rank", pvp_rank, pvp_col),
        ("ELO",      elo_str,  WHITE),
        ("W / L",    pvp_rec,  WHITE),
    ]):
        sx = CX + i * COL_W
        d.text((sx, 196), label, font=fn_label, fill=MUTED)
        d.text((sx, 215), value, font=fn_pvp,   fill=color)

    # ── Render ─────────────────────────────────────────────────────────────
    buf = io.BytesIO()
    card.convert("RGB").save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


def _tenure(created_at: str) -> str:
    try:
        joined = datetime.fromisoformat(created_at)
        if joined.tzinfo is None:
            joined = joined.replace(tzinfo=timezone.utc)
        days = (datetime.now(timezone.utc) - joined).days
        months = days // 30
        if months >= 12:
            y, m = divmod(months, 12)
            return f"{y}y {m}m" if m else f"{y}y"
        return f"{months}m" if months else f"{days}d"
    except Exception:
        return "Unknown"


class ProfileCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="profile", description="View a user's profile card")
    @app_commands.describe(user="User to view (leave empty for yourself)")
    @app_commands.checks.cooldown(1, 5)
    async def profile(self, interaction: discord.Interaction, user: discord.Member = None):
        target = user or interaction.user
        await interaction.response.defer(ephemeral=False)

        row = await d1_query(
            """SELECT level, exp, total_points_earned, weekly_points,
                      raids_completed, created_at,
                      pvp_rank, pvp_elo, pvp_wins, pvp_losses
               FROM users WHERE discord_id = ?""",
            [str(target.id)]
        )
        if not row["results"]:
            return await interaction.followup.send(
                f"❌ {target.mention} hasn't verified yet.", ephemeral=True
            )

        data         = row["results"][0]
        level        = data.get("level")               or 1
        exp          = data.get("exp")                 or 0
        weekly_pts   = data.get("weekly_points")       or 0
        lifetime_pts = data.get("total_points_earned") or 0
        raids        = data.get("raids_completed")     or 0
        created_at   = data.get("created_at")          or ""
        pvp_rank     = data.get("pvp_rank")            or "Unranked"
        pvp_elo      = data.get("pvp_elo")             or 0
        pvp_wins     = data.get("pvp_wins")            or 0
        pvp_losses   = data.get("pvp_losses")          or 0

        rank_row = await d1_query(
            "SELECT COUNT(*) + 1 AS rank FROM users WHERE exp > ?", [exp]
        )
        global_rank = rank_row["results"][0]["rank"] if rank_row["results"] else "N/A"

        # Highest non-@everyone role
        top_role: discord.Role | None = None
        for role in reversed(target.roles):
            if role.name != "@everyone":
                top_role = role
                break

        # Accent from top role color
        accent = (88, 101, 242)
        if top_role and top_role.color.value:
            accent = (top_role.color.r, top_role.color.g, top_role.color.b)
        else:
            for role in reversed(target.roles):
                if role.color.value:
                    accent = (role.color.r, role.color.g, role.color.b)
                    break

        role_icon: Image.Image | None = None
        if top_role and top_role.icon:
            role_icon = await _fetch_role_icon(top_role)

        try:
            buf = await _build_card(
                target, level, exp, global_rank,
                weekly_pts, lifetime_pts, raids,
                pvp_rank, pvp_elo, pvp_wins, pvp_losses,
                _tenure(created_at), accent,
                top_role=top_role,
                role_icon=role_icon,
            )
            await interaction.followup.send(
                file=discord.File(buf, filename=f"profile_{target.id}.png")
            )
        except Exception as e:
            print(f"Profile card error: {e}")
            embed = discord.Embed(
                description=(
                    f"**Level {level}** · **{exp:,} XP** · **#{global_rank}** Global\n"
                    f"Weekly: **{weekly_pts:,}** · Lifetime: **{lifetime_pts:,}** · Raids: **{raids:,}**"
                ),
                color=discord.Color.from_rgb(*accent),
            )
            embed.set_author(name=target.display_name,
                             icon_url=target.display_avatar.url if target.display_avatar else None)
            await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(ProfileCog(bot))
    print("✅ Profile cog loaded")
