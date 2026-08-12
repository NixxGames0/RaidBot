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

# ── PvP config ─────────────────────────────────────────────────────────────────
PLACEMENT_MATCHES = 10

# Priority-ordered rank role IDs — first match wins regardless of Discord role position
RANK_PRIORITY = [
    1535553078852325506,   # Founder
    1535558010431340604,   # Head Staff
    1536740055643594782,   # Staff
    1535558108590383184,   # Mod
    1535558050532827186,   # Trial Mod
    1535556016865808444,   # Hoster
    1535554357762850817,   # Verified
]

# Gradient color pairs (start, end) for each rank role — used when no Discord role icon is set
ROLE_GRADIENTS: dict[int, tuple[tuple, tuple]] = {
    1535553078852325506: ((0, 191, 165),   (0, 121, 107)),    # Founder:     teal
    1535558010431340604: ((124, 58, 237),  (76, 29, 149)),    # Head Staff:  deep purple
    1536740055643594782: ((249, 115, 22),  (194, 65, 12)),    # Staff:       orange
    1535558108590383184: ((239, 68, 68),   (153, 27, 27)),    # Mod:         crimson
    1535558050532827186: ((251, 79, 123),  (190, 24, 93)),    # Trial Mod:   rose
    1535556016865808444: ((132, 204, 22),  (63, 98, 18)),     # Hoster:      lime
    1535554357762850817: ((16, 185, 129),  (6, 95, 70)),      # Verified:    emerald
}

PVP_TIERS = [
    ("Master",   (255, 107, 107), (139,   0,   0)),
    ("Amethyst", (192, 132, 252), (109,  40, 217)),
    ("Platinum", (103, 232, 249), (  8, 145, 178)),
    ("Gold",     (252, 211,  77), (183, 121,  31)),
    ("Silver",   (229, 231, 235), (107, 114, 128)),
    ("Bronze",   (217, 119,   6), (124,  45,  18)),
    ("Unranked", ( 75,  85,  99), ( 55,  65,  81)),
]

def _clean_text(name: str) -> str:
    """Remove emoji/symbols that Liberation/DejaVu fonts cannot render."""
    def _keep(c: str) -> bool:
        cp = ord(c)
        return (
            0x0020 <= cp <= 0x007E   # printable ASCII
            or 0x00C0 <= cp <= 0x024F  # Latin Extended (accented letters)
            or 0x0370 <= cp <= 0x03FF  # Greek
            or 0x0400 <= cp <= 0x04FF  # Cyrillic
        )
    return ''.join(c for c in name if _keep(c)).strip()


def _pvp_rank_colors(rank_name: str) -> tuple[tuple, tuple]:
    for name, color, dark in PVP_TIERS:
        if name == rank_name:
            return color, dark
    return (75, 85, 99), (55, 65, 81)

_RANKS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'ranks')
_ICONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'icons')

def _load_role_icon(role_name: str) -> "Image.Image | None":
    name = role_name.lower().replace(" ", "")
    path = os.path.join(_ICONS_DIR, f"{name}.png")
    if os.path.exists(path):
        try:
            img = Image.open(path)
            img.load()
            return img.convert("RGBA")
        except Exception:
            pass
    return None

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

def _fc_list_font(style: str) -> "str | None":
    """Ask fc-list for the first available sans-serif font of the given style."""
    try:
        import subprocess
        for family in ("Liberation Sans", "DejaVu Sans", "Noto Sans", "Ubuntu", "FreeSans"):
            res = subprocess.run(
                ["fc-list", f":family={family}:style={style}", "--format=%{file}\n"],
                capture_output=True, text=True, timeout=3,
            )
            for line in res.stdout.strip().splitlines():
                p = line.strip()
                if p.endswith((".ttf", ".otf")) and os.path.exists(p):
                    return p
    except Exception:
        pass
    return None

# Run fc-list once at module load so the result is cached.
_FC_BOLD    = _fc_list_font("Bold")
_FC_REGULAR = _fc_list_font("Regular")
if _FC_BOLD:
    print(f"[Profile] fc-list bold font: {_FC_BOLD}")
if _FC_REGULAR:
    print(f"[Profile] fc-list regular font: {_FC_REGULAR}")


def _ensure_fonts() -> None:
    """Download DejaVu fonts into fonts/ if not already present."""
    import urllib.request
    _base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    fonts_dir = os.path.join(_base, "fonts")
    os.makedirs(fonts_dir, exist_ok=True)
    _GH = "https://raw.githubusercontent.com/dejavu-fonts/dejavu-fonts/master/ttf/"
    downloads = {
        "bold.ttf":    _GH + "DejaVuSans-Bold.ttf",
        "regular.ttf": _GH + "DejaVuSans.ttf",
    }
    for filename, url in downloads.items():
        path = os.path.join(fonts_dir, filename)
        if not os.path.exists(path):
            try:
                print(f"[Profile] Downloading {filename} …")
                urllib.request.urlretrieve(url, path)
                print(f"[Profile] Downloaded {filename} ({os.path.getsize(path):,} bytes)")
            except Exception as e:
                print(f"[Profile] Font download failed ({filename}): {e}")


_ensure_fonts()

def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    key = (size, bold)
    if key in _font_cache:
        return _font_cache[key]
    _base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    candidates = (
        [
            os.path.join(_base, "fonts", "bold.ttf"),
            # Windows
            "C:/Windows/Fonts/arialbd.ttf",
            "C:/Windows/Fonts/calibrib.ttf",
            # Linux - Liberation (multiple paths across distros)
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
            # Linux - DejaVu
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
            # Linux - Noto
            "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
            "/usr/share/fonts/noto/NotoSans-Bold.ttf",
            "/usr/share/fonts/noto-cjk/NotoSans-Bold.ttf",
            # Linux - Ubuntu
            "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf",
            "/usr/share/fonts/truetype/ubuntu-font-family/Ubuntu-B.ttf",
            # macOS
            "/Library/Fonts/Arial Bold.ttf",
        ] + ([_FC_BOLD] if _FC_BOLD else []) if bold else [
            os.path.join(_base, "fonts", "regular.ttf"),
            # Windows
            "C:/Windows/Fonts/arial.ttf",
            "C:/Windows/Fonts/calibri.ttf",
            # Linux - Liberation
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
            # Linux - DejaVu
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans.ttf",
            # Linux - Noto
            "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
            "/usr/share/fonts/noto/NotoSans-Regular.ttf",
            # Linux - Ubuntu
            "/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf",
            "/usr/share/fonts/truetype/ubuntu-font-family/Ubuntu-R.ttf",
            # macOS
            "/Library/Fonts/Arial.ttf",
        ] + ([_FC_REGULAR] if _FC_REGULAR else [])
    )
    f = None
    for path in candidates:
        if path and os.path.exists(path):
            try:
                f = ImageFont.truetype(path, size)
                break
            except Exception:
                continue
    if f is None:
        print(f"[Profile] WARNING: no truetype font found for size={size} bold={bold}, falling back to default")
    _font_cache[key] = f or ImageFont.load_default()
    return _font_cache[key]


def _prewarm_fonts() -> None:
    for sz in (SZ_NAME, SZ_ROLE, SZ_SUB, SZ_LABEL, SZ_VAL, SZ_PVP):
        _font(sz)
        _font(sz, bold=True)

def _tsize(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]

def _rrect(draw: ImageDraw.ImageDraw, bbox, radius: int, fill):
    try:
        draw.rounded_rectangle(bbox, radius=radius, fill=fill)
    except AttributeError:
        draw.rectangle(bbox, fill=fill)

# ── Card constants ─────────────────────────────────────────────────────────────
W, H        = 960, 310
BG          = (13, 17, 23)
BG2         = (22, 27, 34)
MUTED       = (110, 118, 129)
WHITE       = (255, 255, 255)
BAR_BG      = (33, 38, 45)
SEP         = (33, 40, 50)
AV_SIZE     = 140
AV_X, AV_Y = 18, (H - AV_SIZE) // 2

# Font sizes — scaled for Discord's ~50% image compression
SZ_NAME  = 44   # username
SZ_ROLE  = 32   # role name (same line as username, slightly smaller)
SZ_SUB   = 22   # sub-line / XP info
SZ_LABEL = 19   # stat column labels
SZ_VAL   = 32   # stat values
SZ_PVP   = 28   # PvP row values

_prewarm_fonts()

async def _fetch_image(session: aiohttp.ClientSession, url: str, timeout: int = 5) -> Image.Image | None:
    try:
        async with session.get(str(url), timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            if r.status == 200:
                data = await r.read()
                img = Image.open(io.BytesIO(data))
                img.load()
                return img.convert("RGBA")
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
    r, g, b, a = img.split()
    a = a.point(lambda x: int(x * opacity))
    return Image.merge("RGBA", (r, g, b, a))


def _gradient_text(card: Image.Image, xy: tuple, text: str, font,
                   gradient_src: Image.Image,
                   fallback_color: tuple = (255, 255, 255)) -> None:
    """Fill text with a horizontal gradient from icon colors (alpha-weighted, skips transparent)."""
    bbox = ImageDraw.Draw(Image.new("L", (1, 1))).textbbox(xy, text, font=font)
    x, y, x2, y2 = bbox
    w, h = max(1, x2 - x), max(1, y2 - y)

    # Scale icon to text width, keep natural height for column sampling
    icon = gradient_src.convert("RGBA").resize((w, max(1, gradient_src.height)), Image.LANCZOS)
    iw, ih = icon.size
    px   = icon.load()

    # Build a 1-row gradient: each column is the alpha-weighted average of that column's pixels.
    # This ignores transparent pixels so they don't pull colors toward black.
    row    = Image.new("RGB", (w, 1))
    row_px = row.load()
    for col in range(w):
        rs = gs = bs = aw = 0
        for row_i in range(ih):
            r, g, b, a = px[col, row_i]
            rs += r * a;  gs += g * a;  bs += b * a;  aw += a
        row_px[col, 0] = (rs // aw, gs // aw, bs // aw) if aw else fallback_color

    grad = row.resize((w, h), Image.NEAREST).convert("RGBA")

    mask = Image.new("L", card.size, 0)
    ImageDraw.Draw(mask).text(xy, text, font=font, fill=255)

    layer = Image.new("RGBA", card.size, (0, 0, 0, 0))
    layer.paste(grad, (x, y))
    card.paste(layer, mask=mask)


def _gradient_rect(c1: tuple, c2: tuple, w: int, h: int, radius: int = 0) -> Image.Image:
    """Horizontal gradient rectangle, optional rounded corners."""
    row = Image.new("RGB", (max(1, w), 1))
    px  = row.load()
    for i in range(w):
        t = i / max(1, w - 1)
        px[i, 0] = tuple(int(c1[j] + (c2[j] - c1[j]) * t) for j in range(3))
    img = row.resize((w, h), Image.NEAREST).convert("RGBA")
    if radius:
        mask = Image.new("L", (w, h), 0)
        try:
            ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, fill=255)
        except AttributeError:
            ImageDraw.Draw(mask).rectangle([0, 0, w - 1, h - 1], fill=255)
        img.putalpha(mask)
    return img


def _draw_placement_ring(
    draw: ImageDraw.ImageDraw,
    cx: int, cy: int,
    matches_done: int,
    total: int = PLACEMENT_MATCHES,
    accent: tuple = (88, 101, 242),
) -> None:
    """Segmented hollow ring: each arc = 1 placement match."""
    R_OUT   = 20
    STROKE  = 7
    GAP_DEG = 7.0
    seg_deg = (360.0 - total * GAP_DEG) / total
    EMPTY   = (45, 52, 63)

    for i in range(total):
        start = -90.0 + i * (seg_deg + GAP_DEG)
        end   = start + seg_deg
        bbox  = [cx - R_OUT, cy - R_OUT, cx + R_OUT, cy + R_OUT]
        draw.arc(bbox, start=start, end=end,
                 fill=accent if i < matches_done else EMPTY,
                 width=STROKE)


async def _build_card(
    session: aiohttp.ClientSession,
    target: discord.Member,
    level: int, exp: int, global_rank,
    weekly_pts: int, lifetime_pts: int, raids: int,
    pvp_rank: str, pvp_elo: int, pvp_wins: int, pvp_losses: int,
    pvp_placement_done: int, pvp_placement_left: int,
    tenure: str, accent: tuple,
    top_role: discord.Role | None = None,
) -> io.BytesIO:

    # ── Background gradient ────────────────────────────────────────────────
    card = Image.new("RGBA", (W, H), (*BG, 255))
    d = ImageDraw.Draw(card)

    for y in range(H):
        t = y / H
        row_col = tuple(int(BG[i] + (BG2[i] - BG[i]) * t) for i in range(3))
        d.line([(0, y), (W, y)], fill=(*row_col, 255))

    # ── Rank icon watermark — 2× card height, half off right edge ────────────
    rank_icon = _load_rank_icon(pvp_rank)
    if rank_icon:
        WM = H * 2
        wm = rank_icon.resize((WM, WM), Image.LANCZOS)
        wm = _apply_alpha(wm, 0.25)
        card.paste(wm, (W - WM // 2, (H - WM) // 2), wm)

    # ── Left accent strip ──────────────────────────────────────────────────
    d.rectangle([0, 0, 7, H], fill=(*accent, 255))

    # ── Avatar ─────────────────────────────────────────────────────────────
    av_img = await _fetch_image(session, str(target.display_avatar.url))
    if av_img:
        av_circle = _circle(av_img, AV_SIZE)
        ring_sz = AV_SIZE + 8
        ring = Image.new("RGBA", (ring_sz, ring_sz), (0, 0, 0, 0))
        ImageDraw.Draw(ring).ellipse([0, 0, ring_sz - 1, ring_sz - 1], fill=(*accent, 255))
        card.paste(ring, (AV_X - 4, AV_Y - 4), ring)
        card.paste(av_circle, (AV_X, AV_Y), av_circle)
    else:
        d.ellipse([AV_X, AV_Y, AV_X + AV_SIZE, AV_Y + AV_SIZE], fill=(*BG2, 255))

    # ── Fonts ──────────────────────────────────────────────────────────────
    fn_name  = _font(SZ_NAME,  bold=True)
    fn_sub   = _font(SZ_SUB)
    fn_label = _font(SZ_LABEL)
    fn_val   = _font(SZ_VAL,   bold=True)
    fn_pvp   = _font(SZ_PVP,   bold=True)

    CX    = AV_X + AV_SIZE + 24
    COL_W = 175

    # ── Line 1: Username + [role icon] + Role name (same line) ────────────
    name_str = _clean_text(target.display_name)[:20]
    d.text((CX, 12), name_str, font=fn_name, fill=WHITE)
    name_w, name_h = _tsize(d, name_str, fn_name)

    if top_role and top_role.name != "@everyone":
        role_color = (
            (top_role.color.r, top_role.color.g, top_role.color.b)
            if top_role.color.value else MUTED
        )
        fn_role = _font(SZ_ROLE, bold=True)
        role_x = CX + name_w + 14
        role_y = 12 + (SZ_NAME - SZ_ROLE) // 2
        role_display = _clean_text(top_role.name)[:18]
        if role_display:
            d.text((role_x, role_y), role_display, font=fn_role, fill=role_color)

    # ── Sub-line ───────────────────────────────────────────────────────────
    rank_str = f"#{global_rank}" if str(global_rank) != "N/A" else "-"
    d.text(
        (CX, 72),
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

    BAR_X, BAR_Y = CX, 104
    BAR_W = COL_W * 3 - 10
    BAR_H = 14

    _rrect(d, [BAR_X, BAR_Y, BAR_X + BAR_W, BAR_Y + BAR_H], BAR_H // 2, BAR_BG)
    fill_w = max(BAR_H, int(BAR_W * pct / 100))
    bar_c2 = tuple(min(255, int(c * 1.45)) for c in accent)   # brighten for right end
    bar_grad = _gradient_rect(accent, bar_c2, fill_w, BAR_H, radius=BAR_H // 2)
    card.paste(bar_grad, (BAR_X, BAR_Y), bar_grad)
    d.text((BAR_X + BAR_W + 10, BAR_Y), f"{pct}% to Level {level + 1}",
           font=_font(SZ_LABEL, bold=True), fill=MUTED)

    # ── Divider 1 ──────────────────────────────────────────────────────────
    d.line([(CX, 138), (W - 18, 138)], fill=SEP, width=1)

    # ── Hoster stat columns ────────────────────────────────────────────────
    for i, (label, value) in enumerate([
        ("Weekly Points",   f"{weekly_pts:,}"),
        ("Lifetime Points", f"{lifetime_pts:,}"),
        ("Raids Hosted",    f"{raids:,}"),
    ]):
        sx = CX + i * COL_W
        d.text((sx, 148), label, font=fn_label, fill=MUTED)
        d.text((sx, 168), value, font=fn_val,   fill=WHITE)

    # ── Divider 2 ──────────────────────────────────────────────────────────
    d.line([(CX, 215), (W - 18, 215)], fill=SEP, width=1)

    # ── PvP row ────────────────────────────────────────────────────────────
    pvp_col, _ = _pvp_rank_colors(pvp_rank)
    pvp_rec = f"{pvp_wins} / {pvp_losses}" if (pvp_wins or pvp_losses) else "- / -"
    elo_str = str(pvp_elo) if pvp_elo else "-"

    # Pre-calculate placement ring metrics so we know how much to push ELO/W-L right
    ring_offset = 0
    ring_cx = ring_cy = 0
    matches_done = 0
    if pvp_rank == "Unranked" and not pvp_placement_done:
        matches_done = max(0, PLACEMENT_MATCHES - (pvp_placement_left or PLACEMENT_MATCHES))
        rank_w, rank_h = _tsize(d, pvp_rank, fn_pvp)
        ring_cx = CX + rank_w + 16 + 20
        ring_cy = 244 + rank_h // 2 + 2
        ring_right = ring_cx + 20 + 14              # right edge of ring + padding
        col1_start = CX + COL_W
        ring_offset = max(0, ring_right - col1_start) + 45

    for i, (label, value, color) in enumerate([
        ("PvP Rank", pvp_rank, pvp_col),
        ("ELO",      elo_str,  WHITE),
        ("W / L",    pvp_rec,  WHITE),
    ]):
        sx = CX + i * COL_W + (ring_offset if i > 0 else 0)
        d.text((sx, 224), label, font=fn_label, fill=MUTED)
        d.text((sx, 244), value, font=fn_pvp,   fill=color)

    if pvp_rank == "Unranked" and not pvp_placement_done:
        _draw_placement_ring(d, ring_cx, ring_cy, matches_done, accent=accent)

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
        self._session: aiohttp.ClientSession | None = None

    async def cog_load(self) -> None:
        self._session = aiohttp.ClientSession()

    async def cog_unload(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    @app_commands.command(name="profile", description="View a user's profile card")
    @app_commands.describe(user="User to view (leave empty for yourself)")
    @app_commands.checks.cooldown(1, 5)
    async def profile(self, interaction: discord.Interaction, user: discord.Member = None):
        target = user or interaction.user
        try:
            await interaction.response.defer(ephemeral=False)
        except discord.NotFound:
            return

        row = await d1_query(
            """SELECT level, exp, total_points_earned, weekly_points,
                      raids_completed, created_at,
                      pvp_rank, pvp_elo, pvp_wins, pvp_losses,
                      pvp_placement_done, pvp_placement_left
               FROM users WHERE discord_id = ?""",
            [str(target.id)]
        )
        if not row["results"]:
            return await interaction.followup.send(
                f"❌ {target.mention} hasn't verified yet.", ephemeral=True
            )

        data               = row["results"][0]
        level              = data.get("level")               or 1
        exp                = data.get("exp")                 or 0
        weekly_pts         = data.get("weekly_points")       or 0
        lifetime_pts       = data.get("total_points_earned") or 0
        raids              = data.get("raids_completed")     or 0
        created_at         = data.get("created_at")          or ""
        pvp_rank           = data.get("pvp_rank")            or "Unranked"
        pvp_elo            = data.get("pvp_elo")             or 0
        pvp_wins           = data.get("pvp_wins")            or 0
        pvp_losses         = data.get("pvp_losses")          or 0
        pvp_placement_done = data.get("pvp_placement_done")  or 0
        pvp_placement_left = data.get("pvp_placement_left")  or PLACEMENT_MATCHES

        rank_row = await d1_query(
            "SELECT COUNT(*) + 1 AS rank FROM users WHERE exp > ?", [exp]
        )
        global_rank = rank_row["results"][0]["rank"] if rank_row["results"] else "N/A"

        role_map = {r.id: r for r in target.roles}
        top_role: discord.Role | None = next(
            (role_map[rid] for rid in RANK_PRIORITY if rid in role_map), None
        )
        # Fallback: highest-position non-@everyone role
        if top_role is None:
            top_role = next(
                (r for r in sorted(target.roles, key=lambda r: r.position, reverse=True)
                 if r.name != "@everyone"),
                None
            )

        accent = (88, 101, 242)
        if top_role and top_role.color.value:
            accent = (top_role.color.r, top_role.color.g, top_role.color.b)
        else:
            for role in reversed(target.roles):
                if role.color.value:
                    accent = (role.color.r, role.color.g, role.color.b)
                    break

        session = self._session or aiohttp.ClientSession()

        try:
            buf = await _build_card(
                session,
                target, level, exp, global_rank,
                weekly_pts, lifetime_pts, raids,
                pvp_rank, pvp_elo, pvp_wins, pvp_losses,
                pvp_placement_done, pvp_placement_left,
                _tenure(created_at), accent,
                top_role=top_role,
            )
            await interaction.followup.send(
                file=discord.File(buf, filename=f"profile_{target.id}.png")
            )
        except Exception as e:
            import traceback
            print(f"Profile card error: {e}\n{traceback.format_exc()}")
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
