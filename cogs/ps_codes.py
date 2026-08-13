"""
PS code management — unified /ps command group for both raid and PvP code pools.

Raid pool  → `codes` table          (code, is_using, held_by, claimed_at, created_at)
PvP pool   → `pvp_ps_codes` table   (code, match_id)
"""

import json
import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone

from bot import (
    d1_query,
    LOG_CHANNELS,
    is_staff,
    is_head_staff_or_founder,
)

TYPE_RAID = "raid"
TYPE_PVP  = "pvp"

TYPE_CHOICES = [
    app_commands.Choice(name="Raid", value=TYPE_RAID),
    app_commands.Choice(name="PvP",  value=TYPE_PVP),
]

TYPE_LABELS = {TYPE_RAID: "Raid", TYPE_PVP: "PvP"}


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _log(bot: commands.Bot, embed: discord.Embed):
    ch = bot.get_channel(LOG_CHANNELS.get("general", 0))
    if ch:
        try:
            await ch.send(embed=embed)
        except Exception:
            pass


def _parse_codes(raw: str) -> list[str]:
    return [c.strip().upper() for c in raw.split(",") if c.strip()]


def _cap(text: str, limit: int = 1020) -> str:
    return text[:limit] + "…" if len(text) > limit else text


def _pill(codes: list[str]) -> str:
    return _cap("  ".join(f"`{c}`" for c in codes))


# ── DB accessors ──────────────────────────────────────────────────────────────

async def _code_exists_anywhere(code: str) -> str | None:
    """Returns 'raid', 'pvp', or None indicating where the code already exists."""
    if (await d1_query("SELECT code FROM codes WHERE code = ?", [code]))["results"]:
        return TYPE_RAID
    if (await d1_query("SELECT code FROM pvp_ps_codes WHERE code = ?", [code]))["results"]:
        return TYPE_PVP
    return None


async def _raid_add(code: str) -> tuple[bool, str | None]:
    """Returns (inserted, conflict_pool). conflict_pool is set when code exists elsewhere."""
    exists_in = await _code_exists_anywhere(code)
    if exists_in == TYPE_RAID:
        return False, None          # dupe in same pool
    if exists_in == TYPE_PVP:
        return False, TYPE_PVP     # conflict with other pool
    now = datetime.now(timezone.utc).isoformat()
    await d1_query(
        "INSERT INTO codes (code, is_using, held_by, claimed_at, created_at) VALUES (?, 0, NULL, NULL, ?)",
        [code, now],
    )
    return True, None


async def _pvp_add(code: str) -> tuple[bool, str | None]:
    exists_in = await _code_exists_anywhere(code)
    if exists_in == TYPE_PVP:
        return False, None
    if exists_in == TYPE_RAID:
        return False, TYPE_RAID
    await d1_query("INSERT INTO pvp_ps_codes (code, match_id) VALUES (?, NULL)", [code])
    return True, None


async def _raid_info(code: str) -> dict | None:
    r = await d1_query("SELECT code, is_using, held_by FROM codes WHERE code = ?", [code])
    return r["results"][0] if r["results"] else None


async def _pvp_info(code: str) -> dict | None:
    r = await d1_query("SELECT code, match_id FROM pvp_ps_codes WHERE code = ?", [code])
    return r["results"][0] if r["results"] else None


async def _raid_delete(code: str):
    await d1_query("DELETE FROM codes WHERE code = ?", [code])


async def _pvp_delete(code: str):
    await d1_query("DELETE FROM pvp_ps_codes WHERE code = ?", [code])


async def _raid_force_delete(bot: commands.Bot, code: str, held_by: str | None):
    if held_by:
        user_row = await d1_query("SELECT ps_codes FROM users WHERE discord_id = ?", [held_by])
        if user_row["results"]:
            current = json.loads(user_row["results"][0]["ps_codes"] or "[]")
            if code in current:
                current.remove(code)
                await d1_query(
                    "UPDATE users SET ps_codes = ?, updated_at = ? WHERE discord_id = ?",
                    [json.dumps(current), datetime.now(timezone.utc).isoformat(), held_by],
                )
    await d1_query("DELETE FROM codes WHERE code = ?", [code])


# ── Cog ───────────────────────────────────────────────────────────────────────

class PSCodes(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    ps = app_commands.Group(name="ps", description="PS code pool management (Staff)")

    # ── /ps add ───────────────────────────────────────────────────────────────

    @ps.command(name="add", description="Add one or more PS codes to the raid or PvP pool (Head Staff+)")
    @app_commands.describe(type="Which pool to add to", codes="Comma-separated codes")
    @app_commands.choices(type=TYPE_CHOICES)
    async def ps_add(self, interaction: discord.Interaction, type: str, codes: str):
        if not is_head_staff_or_founder(interaction.user):
            return await interaction.response.send_message("❌ Head Staff+ only.", ephemeral=True)

        items = _parse_codes(codes)
        if not items:
            return await interaction.response.send_message("❌ No valid codes provided.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        added, dupes, conflicts = [], [], []
        for code in items:
            ok, conflict_pool = await (_pvp_add(code) if type == TYPE_PVP else _raid_add(code))
            if ok:
                added.append(code)
            elif conflict_pool:
                conflicts.append((code, TYPE_LABELS[conflict_pool]))
            else:
                dupes.append(code)

        label = TYPE_LABELS[type]
        embed = discord.Embed(
            title=f"📥 PS Codes — Add ({label})",
            color=discord.Color.green() if added else discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        if added:
            embed.add_field(name=f"✅ Added ({len(added)})", value=_pill(added), inline=False)
        if dupes:
            embed.add_field(name=f"⚠️ Already in {label} pool ({len(dupes)})", value=_pill(dupes), inline=False)
        if conflicts:
            lines = [f"`{c}` — already in **{p}** pool" for c, p in conflicts]
            embed.add_field(
                name=f"🚫 Exists in other pool ({len(conflicts)})",
                value=_cap("\n".join(lines)),
                inline=False,
            )
        embed.set_footer(text=f"By {interaction.user.display_name} · {label} pool")
        await interaction.followup.send(embed=embed, ephemeral=True)

        if added:
            log = discord.Embed(
                title=f"📥 PS Codes Added — {label}",
                description=_pill(added),
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc),
            )
            log.add_field(name="By", value=interaction.user.mention)
            await _log(self.bot, log)

    # ── /ps remove ────────────────────────────────────────────────────────────

    @ps.command(name="remove", description="Remove PS codes from the raid or PvP pool (Head Staff+)")
    @app_commands.describe(type="Which pool to remove from", codes="Comma-separated codes")
    @app_commands.choices(type=TYPE_CHOICES)
    async def ps_remove(self, interaction: discord.Interaction, type: str, codes: str):
        if not is_head_staff_or_founder(interaction.user):
            return await interaction.response.send_message("❌ Head Staff+ only.", ephemeral=True)

        items = _parse_codes(codes)
        if not items:
            return await interaction.response.send_message("❌ No valid codes provided.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        removed, in_use, missing = [], [], []

        for code in items:
            if type == TYPE_PVP:
                row = await _pvp_info(code)
                if not row:
                    missing.append(code)
                elif row["match_id"]:
                    in_use.append((code, f"match `{row['match_id']}`"))
                else:
                    await _pvp_delete(code)
                    removed.append(code)
            else:
                row = await _raid_info(code)
                if not row:
                    missing.append(code)
                elif row["is_using"]:
                    in_use.append((code, f"<@{row['held_by']}>"))
                else:
                    await _raid_delete(code)
                    removed.append(code)

        label = TYPE_LABELS[type]
        embed = discord.Embed(
            title=f"🗑️ PS Codes — Remove ({label})",
            color=discord.Color.red() if removed else discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        if removed:
            embed.add_field(name=f"✅ Removed ({len(removed)})", value=_pill(removed), inline=False)
        if in_use:
            lines = [f"`{c}` — in use by {h}" for c, h in in_use]
            embed.add_field(
                name=f"🔒 In Use — skipped ({len(in_use)})",
                value=_cap("\n".join(lines)),
                inline=False,
            )
            embed.set_footer(text="Use /ps force to remove codes that are in use.")
        if missing:
            embed.add_field(name=f"❌ Not Found ({len(missing)})", value=_pill(missing), inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

        if removed:
            log = discord.Embed(
                title=f"🗑️ PS Codes Removed — {label}",
                description=_pill(removed),
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc),
            )
            log.add_field(name="By", value=interaction.user.mention)
            await _log(self.bot, log)

    # ── /ps force ─────────────────────────────────────────────────────────────

    @ps.command(name="force", description="Force-remove codes even if in use (Head Staff+)")
    @app_commands.describe(type="Which pool to target", codes="Comma-separated codes")
    @app_commands.choices(type=TYPE_CHOICES)
    async def ps_force(self, interaction: discord.Interaction, type: str, codes: str):
        if not is_head_staff_or_founder(interaction.user):
            return await interaction.response.send_message("❌ Head Staff+ only.", ephemeral=True)

        items = _parse_codes(codes)
        if not items:
            return await interaction.response.send_message("❌ No valid codes provided.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        removed, missing = [], []

        for code in items:
            if type == TYPE_PVP:
                row = await _pvp_info(code)
                if not row:
                    missing.append(code)
                    continue
                match_id = row.get("match_id")
                await _pvp_delete(code)
                removed.append((code, f"match `{match_id}`" if match_id else None))
            else:
                row = await _raid_info(code)
                if not row:
                    missing.append(code)
                    continue
                held_by = row.get("held_by")
                await _raid_force_delete(self.bot, code, held_by)
                removed.append((code, f"<@{held_by}>" if held_by else None))

        label = TYPE_LABELS[type]
        embed = discord.Embed(
            title=f"🔨 PS Codes — Force Remove ({label})",
            color=discord.Color.dark_red() if removed else discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        if removed:
            lines = [f"`{c}`" + (f" — was held by {h}" if h else "") for c, h in removed]
            embed.add_field(
                name=f"✅ Force Removed ({len(removed)})",
                value=_cap("\n".join(lines)),
                inline=False,
            )
        if missing:
            embed.add_field(name=f"❌ Not Found ({len(missing)})", value=_pill(missing), inline=False)
        embed.set_footer(text=f"By {interaction.user.display_name} · {label} pool")
        await interaction.followup.send(embed=embed, ephemeral=True)

        if removed:
            log = discord.Embed(
                title=f"🔨 PS Codes Force Removed — {label}",
                description=_cap("\n".join(
                    f"`{c}`" + (f" — held by {h}" if h else "") for c, h in removed
                )),
                color=discord.Color.dark_red(),
                timestamp=datetime.now(timezone.utc),
            )
            log.add_field(name="By", value=interaction.user.mention)
            await _log(self.bot, log)

    # ── /ps list ──────────────────────────────────────────────────────────────

    @ps.command(name="list", description="View PS code pools (Staff+)")
    @app_commands.describe(type="Filter by pool — leave blank to show both")
    @app_commands.choices(type=TYPE_CHOICES)
    async def ps_list(self, interaction: discord.Interaction, type: str | None = None):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("❌ Staff only.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        pools = [type] if type else [TYPE_RAID, TYPE_PVP]
        embed = discord.Embed(
            title="🎮 PS Code Pools",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )

        for pool in pools:
            label = TYPE_LABELS[pool]

            if pool == TYPE_PVP:
                res  = await d1_query("SELECT code, match_id FROM pvp_ps_codes ORDER BY code")
                rows = res.get("results", [])
                avail   = [r["code"] for r in rows if not r["match_id"]]
                in_use  = [(r["code"], r["match_id"]) for r in rows if r["match_id"]]
            else:
                res  = await d1_query("SELECT code, is_using, held_by FROM codes ORDER BY code")
                rows = res.get("results", [])
                avail   = [r["code"] for r in rows if not r["is_using"]]
                in_use  = [(r["code"], r["held_by"]) for r in rows if r["is_using"]]

            total     = len(rows)
            usage_pct = f"{int(len(in_use) / total * 100)}%" if total else "0%"

            header = (
                f"**{label} Pool** — {total} total · "
                f"{len(avail)} available · {len(in_use)} in use · {usage_pct} utilisation"
            )

            avail_body = _cap("  ".join(f"`{c}`" for c in avail)) if avail else "*None*"
            embed.add_field(
                name=f"✅ {label} — Available ({len(avail)})",
                value=avail_body,
                inline=False,
            )

            if in_use:
                if pool == TYPE_PVP:
                    lines = [f"`{c}` → match `{m}`" for c, m in in_use]
                else:
                    lines = [f"`{c}` → <@{h}>" for c, h in in_use]
                embed.add_field(
                    name=f"🔴 {label} — In Use ({len(in_use)})",
                    value=_cap("\n".join(lines)),
                    inline=False,
                )

            # Separator between pools when showing both
            if len(pools) > 1 and pool == TYPE_RAID:
                embed.add_field(name="​", value=f"**{header}**", inline=False)

        embed.set_footer(text=f"Requested by {interaction.user.display_name}")
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(PSCodes(bot))
    print("✅ PS Codes cog loaded")
