"""
PvP system — Bo3 format, score-weighted ELO, placement calibration,
match auto-timeout, rank DMs, leaderboard, stats, moderation tools,
score dispute system, rematch button.

DB handled by bot.py init_database.
"""

import uuid
import asyncio
import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timezone, timedelta

from bot import (
    d1_query,
    GUILD_ID,
    LOG_CHANNELS,
    MOD_ROLE_ID,
    HEAD_STAFF_ROLE_ID,
    FOUNDER_ROLE_ID,
    is_linked,
    is_staff,
)

# ── Config ─────────────────────────────────────────────────────────────────────
PVP_CHANNEL_ID: int    = 1537778521428992022
PVP_CATEGORY_NAME      = "◆ ＰＶＰ ◆"
PLACEMENT_MATCHES      = 10
BASE_ELO               = 1000
K_FACTOR_NEW           = 32
K_FACTOR_EST           = 16
MATCH_TIMEOUT_WARN_S   = 1800   # 30 min → ping warning in channel
MATCH_TIMEOUT_DELETE_S = 2400   # 40 min → ELO penalty + delete
TIMEOUT_ELO_PENALTY    = 75     # ELO lost by each player on ranked timeout
ELO_DECAY_AMOUNT       = 10     # ELO lost per inactive week (above 1000)
ELO_DECAY_INACTIVE_WKS = 4      # weeks of inactivity before decay starts

# Bo3 result weights: (winner_result, loser_result) used in Elo formula
SCORE_WEIGHTS = {
    "2-0": (1.00, 0.00),
    "2-1": (0.75, 0.25),
}

# ── Rank tiers ─────────────────────────────────────────────────────────────────
RANK_TIERS = [
    (0.01, "Master",   1537778520217096225),
    (0.05, "Amethyst", 1537778520217096224),
    (0.15, "Platinum", 1537778520217096223),
    (0.30, "Gold",     1537778520217096222),
    (0.55, "Silver",   1537778519843672144),
    (1.00, "Bronze",   1537778519843672143),
]
UNRANKED_ROLE_ID  = 1537778519843672142
ALL_RANK_ROLE_IDS = {t[2] for t in RANK_TIERS} | {UNRANKED_ROLE_ID}

_RANK_ORDER = ["Unranked", "Bronze", "Silver", "Gold", "Platinum", "Amethyst", "Master"]

# ── In-memory state ────────────────────────────────────────────────────────────
pvp_queue: dict[int, dict[int, dict]]   = {}   # guild_id → {user_id → entry}
pvp_active_matches: dict[str, dict]     = {}   # match_id → state
pvp_pending: dict[str, dict]            = {}   # match_id → confirmation state
pvp_pending_scores: dict[str, dict]     = {}   # match_id → submitted score state


# ── ELO helpers ────────────────────────────────────────────────────────────────

def _k_factor(placement_left: int, total_matches: int) -> int:
    """K-factor: high during placements (250 → halving), then standard K."""
    if placement_left > 0:
        idx = PLACEMENT_MATCHES - placement_left  # 0 on first placement match
        return max(K_FACTOR_EST, 250 // (2 ** idx))
    return K_FACTOR_NEW if total_matches < 30 else K_FACTOR_EST


def elo_change_bo3(
    winner_elo: int,
    loser_elo: int,
    score: str,
    w_placement_left: int,
    l_placement_left: int,
    w_total: int,
    l_total: int,
    match_type: str,
) -> tuple[int, int, int, int]:
    """
    Returns (new_winner_elo, new_loser_elo, winner_delta, loser_delta).

    Score-weighted result values (2-0 win = full credit; 2-1 = partial).
    Standard relative ELO: beating higher-ranked = bigger gain; losing to
    lower-ranked = bigger loss.
    Post-placement casual matches return 0 delta for the non-placing player.
    """
    # Casual matches have zero ELO and placement impact — ranked only
    w_in_elo = match_type == "ranked"
    l_in_elo = match_type == "ranked"

    w_result, l_result = SCORE_WEIGHTS.get(score, (1.0, 0.0))
    expected_w = 1 / (1 + 10 ** ((loser_elo - winner_elo) / 400))
    expected_l = 1 - expected_w

    k_w = _k_factor(w_placement_left, w_total) if w_in_elo else 0
    k_l = _k_factor(l_placement_left, l_total) if l_in_elo else 0

    w_delta = int(k_w * (w_result - expected_w))
    l_delta = int(k_l * (l_result - expected_l))

    return (
        max(0, winner_elo + w_delta),
        max(0, loser_elo + l_delta),
        w_delta,
        l_delta,
    )


async def compute_pvp_rank(elo: int) -> str:
    total_row = await d1_query(
        "SELECT COUNT(*) AS n FROM users WHERE pvp_placement_done = 1"
    )
    total = total_row["results"][0]["n"] if total_row["results"] else 0
    if total < 10:
        return "Bronze"

    above_row = await d1_query(
        "SELECT COUNT(*) AS n FROM users WHERE pvp_elo > ? AND pvp_placement_done = 1",
        [elo]
    )
    above = above_row["results"][0]["n"] or 0
    pct = (above + 1) / total

    for threshold, name, _ in RANK_TIERS:
        if pct <= threshold:
            return name
    return "Bronze"


async def update_pvp_rank_role(member: discord.Member, new_rank: str):
    to_remove = [r for r in member.roles if r.id in ALL_RANK_ROLE_IDS]
    if to_remove:
        await member.remove_roles(*to_remove, reason="PvP rank update")
    if new_rank == "Unranked":
        role = member.guild.get_role(UNRANKED_ROLE_ID)
        if role:
            await member.add_roles(role, reason="PvP rank: Unranked")
        return
    for _, name, role_id in RANK_TIERS:
        if name == new_rank:
            role = member.guild.get_role(role_id)
            if role:
                await member.add_roles(role, reason=f"PvP rank: {new_rank}")
            break


async def _send_rank_change_dm(bot: commands.Bot, user_id: int, old_rank: str, new_rank: str):
    if old_rank == new_rank or user_id < 0:
        return
    try:
        user = await bot.fetch_user(user_id)
        old_idx = _RANK_ORDER.index(old_rank) if old_rank in _RANK_ORDER else -1
        new_idx = _RANK_ORDER.index(new_rank) if new_rank in _RANK_ORDER else -1
        arrow = "📈 Promoted to" if new_idx > old_idx else "📉 Demoted to"
        await user.send(f"{arrow} **{new_rank}**! (was {old_rank})")
    except Exception:
        pass


# ── Boosting / win-trade detection ────────────────────────────────────────────

async def _check_boost_flag(p1_id: int, p2_id: int) -> str | None:
    """
    Returns a reason string if this pair looks suspicious, else None.
    Checks:
      1. Played each other 4+ times in the last 24 h.
      2. Same player has lost to the same opponent 3+ times in the last 24 h.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    pair_row = await d1_query(
        """SELECT COUNT(*) AS n FROM pvp_matches
           WHERE ((player1_id = ? AND player2_id = ?) OR (player1_id = ? AND player2_id = ?))
           AND ended_at IS NOT NULL AND ended_at > ?""",
        [str(p1_id), str(p2_id), str(p2_id), str(p1_id), cutoff],
    )
    pair_count = pair_row["results"][0]["n"] if pair_row["results"] else 0
    if pair_count >= 4:
        return f"Played each other {pair_count}x in 24 h"

    for winner_id, loser_id in ((p1_id, p2_id), (p2_id, p1_id)):
        loss_row = await d1_query(
            """SELECT COUNT(*) AS n FROM pvp_matches
               WHERE winner_id = ? AND (player1_id = ? OR player2_id = ?)
               AND ended_at IS NOT NULL AND ended_at > ?""",
            [str(winner_id), str(loser_id), str(loser_id), cutoff],
        )
        losses = loss_row["results"][0]["n"] if loss_row["results"] else 0
        if losses >= 3:
            return f"<@{loser_id}> lost to <@{winner_id}> {losses}x in 24 h"

    return None


async def _flag_boost(winner_id: int, loser_id: int, match_id: str, reason: str):
    """Insert an auto-report for potential boosting."""
    now = datetime.now(timezone.utc).isoformat()
    await d1_query(
        """INSERT INTO pvp_reports
           (reporter_id, reported_id, match_id, reason, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        ["0", str(winner_id), match_id,
         f"[AUTO BOOST FLAG] {reason}", now],
    )


# ── PS code pool ───────────────────────────────────────────────────────────────

async def _claim_ps_code(match_id: str) -> str | None:
    row = await d1_query(
        "SELECT code FROM pvp_ps_codes WHERE match_id IS NULL LIMIT 1", []
    )
    if not row["results"]:
        return None
    code = row["results"][0]["code"]
    await d1_query(
        "UPDATE pvp_ps_codes SET match_id = ? WHERE code = ?",
        [match_id, code],
    )
    return code


async def _release_ps_code(match_id: str):
    await d1_query(
        "UPDATE pvp_ps_codes SET match_id = NULL WHERE match_id = ?",
        [match_id],
    )


# ── Match result application ───────────────────────────────────────────────────

async def _apply_match_result(
    bot: commands.Bot,
    match_id: str,
    winner_id: int,
    loser_id: int,
    score: str,
    guild: discord.Guild | None = None,
) -> discord.Embed | None:
    """
    Apply ELO, W/L, placement, ranks, and roles for a completed match.
    Returns a summary embed (caller sends it; caller handles channel deletion).
    """
    match = pvp_active_matches.get(match_id)
    match_type = match.get("match_type", "casual") if match else "casual"

    w_row = await d1_query(
        "SELECT pvp_elo, pvp_wins, pvp_losses, pvp_placement_left, "
        "pvp_placement_done, pvp_rank FROM users WHERE discord_id = ?",
        [str(winner_id)]
    )
    l_row = await d1_query(
        "SELECT pvp_elo, pvp_wins, pvp_losses, pvp_placement_left, "
        "pvp_placement_done, pvp_rank FROM users WHERE discord_id = ?",
        [str(loser_id)]
    )
    if not w_row["results"] or not l_row["results"]:
        return None

    wd = w_row["results"][0]
    ld = l_row["results"][0]

    w_elo           = wd.get("pvp_elo")            or BASE_ELO
    l_elo           = ld.get("pvp_elo")            or BASE_ELO
    w_wins          = wd.get("pvp_wins")           or 0
    w_losses        = wd.get("pvp_losses")         or 0
    l_wins          = ld.get("pvp_wins")           or 0
    l_losses        = ld.get("pvp_losses")         or 0
    w_pleft         = wd.get("pvp_placement_left") or 0
    l_pleft         = ld.get("pvp_placement_left") or 0
    w_pdone         = bool(wd.get("pvp_placement_done"))
    l_pdone         = bool(ld.get("pvp_placement_done"))
    w_old_rank      = wd.get("pvp_rank")           or "Unranked"
    l_old_rank      = ld.get("pvp_rank")           or "Unranked"

    new_w_elo, new_l_elo, w_delta, l_delta = elo_change_bo3(
        w_elo, l_elo, score,
        w_pleft, l_pleft,
        w_wins + w_losses, l_wins + l_losses,
        match_type,
    )

    now = datetime.now(timezone.utc).isoformat()

    # Placement progress only advances in ranked matches
    new_w_pleft = max(0, w_pleft - 1) if (match_type == "ranked" and w_pleft > 0) else w_pleft
    new_l_pleft = max(0, l_pleft - 1) if (match_type == "ranked" and l_pleft > 0) else l_pleft
    new_w_pdone = 1 if (new_w_pleft == 0 or w_pdone) else 0
    new_l_pdone = 1 if (new_l_pleft == 0 or l_pdone) else 0

    await d1_query(
        """UPDATE users SET pvp_elo = ?, pvp_wins = pvp_wins + 1,
           pvp_placement_left = ?, pvp_placement_done = ?, updated_at = ?
           WHERE discord_id = ?""",
        [new_w_elo, new_w_pleft, new_w_pdone, now, str(winner_id)]
    )
    await d1_query(
        """UPDATE users SET pvp_elo = ?, pvp_losses = pvp_losses + 1,
           pvp_placement_left = ?, pvp_placement_done = ?, updated_at = ?
           WHERE discord_id = ?""",
        [new_l_elo, new_l_pleft, new_l_pdone, now, str(loser_id)]
    )

    # Compute new ranks (only if placement done)
    w_new_rank = await compute_pvp_rank(new_w_elo) if new_w_pdone else "Unranked"
    l_new_rank = await compute_pvp_rank(new_l_elo) if new_l_pdone else "Unranked"

    await d1_query("UPDATE users SET pvp_rank = ? WHERE discord_id = ?", [w_new_rank, str(winner_id)])
    await d1_query("UPDATE users SET pvp_rank = ? WHERE discord_id = ?", [l_new_rank, str(loser_id)])

    if guild:
        for uid, rank in ((winner_id, w_new_rank), (loser_id, l_new_rank)):
            m = guild.get_member(uid)
            if m:
                try:
                    await update_pvp_rank_role(m, rank)
                except Exception:
                    pass

    await _send_rank_change_dm(bot, winner_id, w_old_rank, w_new_rank)
    await _send_rank_change_dm(bot, loser_id,  l_old_rank, l_new_rank)

    await d1_query(
        """UPDATE pvp_matches
           SET winner_id = ?, score = ?, p1_elo_after = ?, p2_elo_after = ?, ended_at = ?
           WHERE match_id = ?""",
        [str(winner_id), score, new_w_elo, new_l_elo, now, match_id]
    )

    await _release_ps_code(match_id)
    pvp_active_matches.pop(match_id, None)
    pvp_pending_scores.pop(match_id, None)

    w_sign = f"+{w_delta}" if w_delta >= 0 else str(w_delta)
    l_sign = f"+{l_delta}" if l_delta >= 0 else str(l_delta)
    affects_elo = match_type == "ranked" or w_pleft > 0 or l_pleft > 0

    embed = discord.Embed(
        title=f"🏆 Match Complete — {score}",
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Winner", value=f"<@{winner_id}>", inline=True)
    embed.add_field(name="Score",  value=score,             inline=True)
    embed.add_field(name="​", value="​",          inline=True)

    if affects_elo:
        embed.add_field(
            name="ELO Changes",
            value=(
                f"<@{winner_id}> **{new_w_elo}** ({w_sign})\n"
                f"<@{loser_id}> **{new_l_elo}** ({l_sign})"
            ),
            inline=False,
        )

    rank_lines = []
    if w_new_rank != w_old_rank:
        rank_lines.append(f"<@{winner_id}> {w_old_rank} → **{w_new_rank}**")
    if l_new_rank != l_old_rank:
        rank_lines.append(f"<@{loser_id}> {l_old_rank} → **{l_new_rank}**")
    if rank_lines:
        embed.add_field(name="Rank Change", value="\n".join(rank_lines), inline=False)

    if not new_w_pdone:
        embed.set_footer(text=f"Placement: {new_w_pleft} ranked match(es) remaining for winner")

    # Boost / win-trade check (fire-and-forget, never blocks the result)
    boost_reason = await _check_boost_flag(winner_id, loser_id)
    if boost_reason:
        await _flag_boost(winner_id, loser_id, match_id, boost_reason)

    return embed


async def _apply_timeout_penalty(
    bot: commands.Bot,
    match_id: str,
    guild: discord.Guild | None = None,
) -> str:
    """
    Deduct ELO from both players and record the timeout in DB.
    Returns a status message string.
    """
    match = pvp_active_matches.get(match_id)
    if not match:
        return "Match not found."

    p1_id = match["player1"]
    p2_id = match["player2"]
    match_type = match.get("match_type", "casual")

    now = datetime.now(timezone.utc).isoformat()
    note = ""

    if match_type == "ranked":
        for uid in (p1_id, p2_id):
            await d1_query(
                "UPDATE users SET pvp_elo = MAX(0, pvp_elo - ?), "
                "pvp_trust = MAX(0, pvp_trust - 1), updated_at = ? WHERE discord_id = ?",
                [TIMEOUT_ELO_PENALTY, now, str(uid)]
            )
        # Recalculate ranks
        if guild:
            for uid in (p1_id, p2_id):
                row = await d1_query(
                    "SELECT pvp_elo, pvp_placement_done FROM users WHERE discord_id = ?",
                    [str(uid)]
                )
                if row["results"]:
                    r = row["results"][0]
                    if r.get("pvp_placement_done"):
                        new_elo  = r["pvp_elo"] or BASE_ELO
                        new_rank = await compute_pvp_rank(new_elo)
                        await d1_query(
                            "UPDATE users SET pvp_rank = ? WHERE discord_id = ?",
                            [new_rank, str(uid)]
                        )
                        m = guild.get_member(uid)
                        if m:
                            try:
                                await update_pvp_rank_role(m, new_rank)
                            except Exception:
                                pass
        note = f" Both players lost **{TIMEOUT_ELO_PENALTY} ELO** and **1 trust point**."
    else:
        # Casual timeout: reduce trust only
        for uid in (p1_id, p2_id):
            await d1_query(
                "UPDATE users SET pvp_trust = MAX(0, pvp_trust - 0.5), updated_at = ? WHERE discord_id = ?",
                [now, str(uid)]
            )

    await d1_query(
        "UPDATE pvp_matches SET ended_at = ?, timeout_flag = 1 WHERE match_id = ?",
        [now, match_id]
    )
    await _release_ps_code(match_id)
    pvp_active_matches.pop(match_id, None)
    pvp_pending_scores.pop(match_id, None)
    return note


# ── Score dispute / confirmation view ─────────────────────────────────────────

class ScoreResultView(discord.ui.View):
    """
    Sent in the match channel after one player submits a score.
    The other player can Confirm (auto-applies) or Dispute (pings mods).
    Auto-applies after 5 minutes if no response.
    """
    def __init__(
        self,
        bot: commands.Bot,
        match_id: str,
        winner_id: int,
        loser_id: int,
        score: str,
        channel: discord.TextChannel,
        guild: discord.Guild,
        p1_id: int,
        p2_id: int,
        guild_id: int,
        submitter_id: int,
    ):
        super().__init__(timeout=300)
        self._bot         = bot
        self.match_id     = match_id
        self.winner_id    = winner_id
        self.loser_id     = loser_id
        self.score        = score
        self._channel     = channel
        self._guild       = guild
        self._p1_id       = p1_id
        self._p2_id       = p2_id
        self._guild_id    = guild_id
        self._submitter   = submitter_id
        self._resolved    = False

    async def _resolve(self, interaction: discord.Interaction | None = None):
        if self._resolved:
            return
        self._resolved = True
        self.stop()

        for child in self.children:
            child.disabled = True

        if interaction:
            await interaction.response.defer()

        embed = await _apply_match_result(
            self._bot, self.match_id,
            self.winner_id, self.loser_id, self.score, self._guild
        )
        if self._channel:
            try:
                view = RematchView(self._p1_id, self._p2_id, self._guild_id)
                await self._channel.send(embed=embed, view=view)
                await asyncio.sleep(15)
                await self._channel.delete(reason="PvP match ended")
            except Exception:
                pass

    @discord.ui.button(label="Confirm Result", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in (self._p1_id, self._p2_id):
            return await interaction.response.send_message("❌ Not a match participant.", ephemeral=True)
        await self._resolve(interaction)

    @discord.ui.button(label="Dispute", style=discord.ButtonStyle.danger, emoji="🚫")
    async def dispute(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in (self._p1_id, self._p2_id):
            return await interaction.response.send_message("❌ Not a match participant.", ephemeral=True)
        if self._resolved:
            return await interaction.response.send_message("Already resolved.", ephemeral=True)
        self._resolved = True
        self.stop()
        for child in self.children:
            child.disabled = True
        await interaction.response.send_message(
            f"🚫 **Score disputed by {interaction.user.mention}.** "
            f"<@&{MOD_ROLE_ID}> please review this match (`{self.match_id}`).",
        )
        pvp_pending_scores.pop(self.match_id, None)

    async def on_timeout(self):
        await self._resolve()


# ── Rematch button ─────────────────────────────────────────────────────────────

class RematchView(discord.ui.View):
    def __init__(self, p1_id: int, p2_id: int, guild_id: int):
        super().__init__(timeout=60)
        self._p1_id           = p1_id
        self._p2_id           = p2_id
        self._guild_id        = guild_id
        self._p1_ready        = False
        self._p2_ready        = False
        self._p1_ranked_ready = False
        self._p2_ranked_ready = False

    async def _start_rematch(self, interaction: discord.Interaction, match_type: str):
        cog: PvPCog | None = interaction.client.cogs.get("PvPCog")
        if cog and (cog._in_match(self._p1_id) or cog._in_match(self._p2_id)):
            return await interaction.response.send_message(
                "❌ A player is already in an active match.", ephemeral=True
            )
        self.stop()
        await interaction.response.send_message(
            f"⚔️ Creating {'ranked ' if match_type == 'ranked' else ''}rematch channel..."
        )
        match_id = str(uuid.uuid4())[:8]
        now_iso  = datetime.now(timezone.utc).isoformat()
        await d1_query(
            """INSERT INTO pvp_matches
               (match_id, player1_id, player2_id, match_type, started_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [match_id, str(self._p1_id), str(self._p2_id), match_type, now_iso, now_iso]
        )
        asyncio.ensure_future(_create_match_channel(
            interaction.client, self._guild_id, match_id,
            self._p1_id, self._p2_id, match_type
        ))

    @discord.ui.button(label="Rematch (casual)", style=discord.ButtonStyle.blurple, emoji="🔄")
    async def rematch(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in (self._p1_id, self._p2_id):
            return await interaction.response.send_message("❌ Not for you.", ephemeral=True)

        if interaction.user.id == self._p1_id:
            self._p1_ready = True
        else:
            self._p2_ready = True

        if self._p1_ready and self._p2_ready:
            await self._start_rematch(interaction, "casual")
        else:
            await interaction.response.send_message(
                "✅ Waiting for your opponent to accept the rematch...", ephemeral=True
            )

    @discord.ui.button(label="Rematch (ranked)", style=discord.ButtonStyle.danger, emoji="🏆")
    async def rematch_ranked(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in (self._p1_id, self._p2_id):
            return await interaction.response.send_message("❌ Not for you.", ephemeral=True)

        if interaction.user.id == self._p1_id:
            self._p1_ranked_ready = True
        else:
            self._p2_ranked_ready = True

        if self._p1_ranked_ready and self._p2_ranked_ready:
            await self._start_rematch(interaction, "ranked")
        else:
            await interaction.response.send_message(
                "✅ Waiting for your opponent to accept the ranked rematch...", ephemeral=True
            )


# ── Match invitation DM ────────────────────────────────────────────────────────

class MatchFoundView(discord.ui.View):
    def __init__(self, match_id: str, guild_id: int, for_user_id: int):
        super().__init__(timeout=60)
        self.match_id     = match_id
        self.guild_id     = guild_id
        self.for_user_id  = for_user_id

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="✅")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.for_user_id:
            return await interaction.response.send_message("❌ This isn't for you.", ephemeral=True)

        pending = pvp_pending.get(self.match_id)
        if not pending:
            return await interaction.response.send_message(
                "❌ Match expired or was cancelled.", ephemeral=True)

        if interaction.user.id == pending["player1"]:
            pending["p1_accepted"] = True
        else:
            pending["p2_accepted"] = True

        if pending["p1_accepted"] and pending["p2_accepted"]:
            pending_data = pvp_pending.pop(self.match_id, None)
            await interaction.response.send_message(
                "✅ Both players accepted! Creating your match channel...", ephemeral=True)
            if pending_data:
                asyncio.ensure_future(_create_match_channel(
                    interaction.client,
                    self.guild_id,
                    self.match_id,
                    pending_data["player1"],
                    pending_data["player2"],
                    pending_data.get("match_type", "casual"),
                ))
        else:
            await interaction.response.send_message(
                "✅ Accepted! Waiting for the other player...", ephemeral=True)
        self.stop()

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, emoji="✖️")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.for_user_id:
            return await interaction.response.send_message("❌ This isn't for you.", ephemeral=True)

        pending = pvp_pending.pop(self.match_id, None)
        if not pending:
            return await interaction.response.send_message("❌ Match already expired.", ephemeral=True)

        match_type = pending.get("match_type", "casual")

        if match_type == "ranked":
            row = await d1_query(
                "SELECT pvp_deny_count FROM users WHERE discord_id = ?",
                [str(interaction.user.id)]
            )
            deny_count   = (row["results"][0]["pvp_deny_count"] or 0) if row["results"] else 0
            timeout_s    = _deny_timeout_seconds(deny_count)
            timeout_until = (datetime.now(timezone.utc) + timedelta(seconds=timeout_s)).isoformat()
            await d1_query(
                "UPDATE users SET pvp_deny_count = pvp_deny_count + 1, pvp_timeout_until = ? WHERE discord_id = ?",
                [timeout_until, str(interaction.user.id)]
            )
            await interaction.response.send_message(
                f"❌ Ranked denial — queue timeout for **{timeout_s // 60} minutes**.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message("✅ Match declined.", ephemeral=True)

        # Notify and re-queue the other player
        other_id  = pending["player2"] if interaction.user.id == pending["player1"] else pending["player1"]
        guild_id  = pending.get("guild_id", self.guild_id)
        try:
            other = await interaction.client.fetch_user(other_id)
            await other.send("❌ Your opponent declined the match. You've been returned to the queue.")
        except Exception:
            pass

        guild_queue = pvp_queue.setdefault(guild_id, {})
        if other_id not in guild_queue:
            o_row = await d1_query(
                "SELECT pvp_elo, pvp_trust FROM users WHERE discord_id = ?", [str(other_id)]
            )
            if o_row["results"]:
                guild_queue[other_id] = {
                    "type": match_type,
                    "elo":   o_row["results"][0].get("pvp_elo")   or BASE_ELO,
                    "trust": o_row["results"][0].get("pvp_trust") or 10.0,
                    "joined_at": datetime.now(timezone.utc),
                }
        self.stop()


# ── Bo3 score submission ───────────────────────────────────────────────────────

class Bo3ScoreSelect(discord.ui.Select):
    def __init__(self, p1: discord.Member, p2: discord.Member, match_id: str):
        self.match_id = match_id
        self._p1 = p1
        self._p2 = p2
        options = [
            discord.SelectOption(
                label=f"{p1.display_name[:40]} wins 2-0",
                value=f"{p1.id}_2-0",
                description="2-0 victory for " + p1.display_name[:40],
                emoji="🏆",
            ),
            discord.SelectOption(
                label=f"{p1.display_name[:40]} wins 2-1",
                value=f"{p1.id}_2-1",
                description="2-1 victory for " + p1.display_name[:40],
                emoji="🏆",
            ),
            discord.SelectOption(
                label=f"{p2.display_name[:40]} wins 2-0",
                value=f"{p2.id}_2-0",
                description="2-0 victory for " + p2.display_name[:40],
                emoji="🏆",
            ),
            discord.SelectOption(
                label=f"{p2.display_name[:40]} wins 2-1",
                value=f"{p2.id}_2-1",
                description="2-1 victory for " + p2.display_name[:40],
                emoji="🏆",
            ),
        ]
        super().__init__(
            placeholder="📊 Submit Bo3 result...",
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        match = pvp_active_matches.get(self.match_id)
        if not match:
            return await interaction.response.send_message("❌ Match not found.", ephemeral=True)

        if interaction.user.id not in (match["player1"], match["player2"]):
            return await interaction.response.send_message(
                "❌ Only match participants can submit the result.", ephemeral=True)

        if self.match_id in pvp_pending_scores:
            return await interaction.response.send_message(
                "❌ A score is already pending confirmation. Use Confirm or Dispute.",
                ephemeral=True,
            )

        winner_id_str, score = self.values[0].rsplit("_", 1)
        winner_id = int(winner_id_str)
        loser_id  = match["player2"] if winner_id == match["player1"] else match["player1"]

        pvp_pending_scores[self.match_id] = {
            "winner_id":    winner_id,
            "loser_id":     loser_id,
            "score":        score,
            "submitter_id": interaction.user.id,
        }

        winner_mention = f"<@{winner_id}>"
        loser_mention  = f"<@{loser_id}>"

        embed = discord.Embed(
            title="📊 Score Submitted",
            description=(
                f"{interaction.user.mention} reported: {winner_mention} **wins {score}**\n\n"
                f"**{loser_mention}** — confirm or dispute below.\n"
                "Auto-confirms in 5 minutes if no response."
            ),
            color=discord.Color.orange(),
        )

        view = ScoreResultView(
            bot=interaction.client,
            match_id=self.match_id,
            winner_id=winner_id,
            loser_id=loser_id,
            score=score,
            channel=interaction.channel,
            guild=interaction.guild,
            p1_id=match["player1"],
            p2_id=match["player2"],
            guild_id=interaction.guild_id,
            submitter_id=interaction.user.id,
        )
        await interaction.response.send_message(embed=embed, view=view)


# ── Report flow ────────────────────────────────────────────────────────────────

class ReportModal(discord.ui.Modal, title="Report Player"):
    reason = discord.ui.TextInput(
        label="Reason",
        placeholder="Describe the issue...",
        style=discord.TextStyle.paragraph,
        max_length=500,
    )

    def __init__(self, match_id: str, reporter_id: int, reported_id: int):
        super().__init__()
        self.match_id    = match_id
        self.reporter_id = reporter_id
        self.reported_id = reported_id

    async def on_submit(self, interaction: discord.Interaction):
        now = datetime.now(timezone.utc).isoformat()
        await d1_query(
            """INSERT INTO pvp_reports
               (reporter_id, reported_id, match_id, reason, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            [str(self.reporter_id), str(self.reported_id), self.match_id, self.reason.value, now]
        )
        rep_row = await d1_query(
            "SELECT COUNT(*) AS n FROM pvp_reports WHERE reported_id = ? AND reviewed = 0",
            [str(self.reported_id)]
        )
        rep_count = rep_row["results"][0]["n"] if rep_row["results"] else 0
        if rep_count % 3 == 0:
            await d1_query(
                "UPDATE users SET pvp_trust = MAX(0, pvp_trust - 0.5) WHERE discord_id = ?",
                [str(self.reported_id)]
            )
        await interaction.response.send_message(
            "✅ Report submitted. Staff will review it.", ephemeral=True
        )


class ReportPlayerSelect(discord.ui.Select):
    def __init__(self, p1: discord.Member, p2: discord.Member, match_id: str):
        self.match_id = match_id
        super().__init__(
            placeholder="🚩 Report a player...",
            options=[
                discord.SelectOption(label=p1.display_name[:100], value=str(p1.id)),
                discord.SelectOption(label=p2.display_name[:100], value=str(p2.id)),
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        reported_id = int(self.values[0])
        if reported_id == interaction.user.id:
            return await interaction.response.send_message(
                "❌ You can't report yourself.", ephemeral=True)
        await interaction.response.send_modal(
            ReportModal(self.match_id, interaction.user.id, reported_id))


class ForfeitConfirmView(discord.ui.View):
    def __init__(self, bot: commands.Bot, match_id: str, forfeiter_id: int,
                 winner_id: int, channel: discord.TextChannel, guild: discord.Guild,
                 p1_id: int, p2_id: int):
        super().__init__(timeout=30)
        self._bot         = bot
        self.match_id     = match_id
        self.forfeiter_id = forfeiter_id
        self.winner_id    = winner_id
        self._channel     = channel
        self._guild       = guild
        self._p1_id       = p1_id
        self._p2_id       = p2_id
        self._done        = False

    async def _execute(self, interaction: discord.Interaction):
        if self._done:
            return
        self._done = True
        self.stop()
        await interaction.response.edit_message(
            content="✅ Forfeit confirmed. Processing...", view=None
        )
        await self._channel.send(
            f"🏳️ <@{self.forfeiter_id}> has **forfeited** the match. "
            f"<@{self.winner_id}> wins **(2-0)**!"
        )
        embed = await _apply_match_result(
            self._bot, self.match_id,
            self.winner_id, self.forfeiter_id, "2-0", self._guild
        )
        try:
            view = RematchView(self._p1_id, self._p2_id, self._guild.id)
            await self._channel.send(embed=embed, view=view)
            await asyncio.sleep(15)
            await self._channel.delete(reason="PvP match ended — forfeit")
        except Exception:
            pass

    @discord.ui.button(label="Yes, forfeit", style=discord.ButtonStyle.danger, emoji="🏳️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.forfeiter_id:
            return await interaction.response.send_message("❌ Not for you.", ephemeral=True)
        await self._execute(interaction)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.forfeiter_id:
            return await interaction.response.send_message("❌ Not for you.", ephemeral=True)
        self._done = True
        self.stop()
        await interaction.response.edit_message(content="Forfeit cancelled.", view=None)

    async def on_timeout(self):
        self._done = True


class ForfeitButton(discord.ui.Button):
    def __init__(self, match_id: str, p1_id: int, p2_id: int):
        super().__init__(
            label="Forfeit",
            style=discord.ButtonStyle.danger,
            emoji="🏳️",
            row=2,
        )
        self.match_id = match_id
        self._p1_id   = p1_id
        self._p2_id   = p2_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id not in (self._p1_id, self._p2_id):
            return await interaction.response.send_message(
                "❌ Only match participants can forfeit.", ephemeral=True
            )
        match = pvp_active_matches.get(self.match_id)
        if not match:
            return await interaction.response.send_message(
                "❌ Match not found or already ended.", ephemeral=True
            )
        winner_id = self._p2_id if interaction.user.id == self._p1_id else self._p1_id
        view = ForfeitConfirmView(
            bot=interaction.client,
            match_id=self.match_id,
            forfeiter_id=interaction.user.id,
            winner_id=winner_id,
            channel=interaction.channel,
            guild=interaction.guild,
            p1_id=self._p1_id,
            p2_id=self._p2_id,
        )
        await interaction.response.send_message(
            "⚠️ Are you sure you want to **forfeit** this match? "
            "Your opponent will be awarded the win **(2-0)**.",
            view=view,
            ephemeral=True,
        )


class MatchPanelView(discord.ui.View):
    def __init__(self, match_id: str, p1: discord.Member, p2: discord.Member):
        super().__init__(timeout=None)
        self.match_id = match_id
        self.add_item(Bo3ScoreSelect(p1, p2, match_id))
        self.add_item(ReportPlayerSelect(p1, p2, match_id))
        self.add_item(ForfeitButton(match_id, p1.id, p2.id))


# ── Staff: /pvpreports ─────────────────────────────────────────────────────────

class ReportActionModal(discord.ui.Modal, title="Report Action"):
    report_id = discord.ui.TextInput(
        label="Report ID",
        placeholder="Enter the report ID number...",
        max_length=10,
    )
    action_note = discord.ui.TextInput(
        label="Note (optional)",
        placeholder="Action taken or dismissal reason...",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=300,
    )

    def __init__(self, action: str):
        super().__init__(title=f"{'Resolve' if action == 'resolve' else 'Dismiss'} Report")
        self.action = action

    async def on_submit(self, interaction: discord.Interaction):
        try:
            rid = int(self.report_id.value.strip())
        except ValueError:
            return await interaction.response.send_message("❌ Invalid report ID.", ephemeral=True)

        row = await d1_query("SELECT * FROM pvp_reports WHERE id = ? AND reviewed = 0", [rid])
        if not row["results"]:
            return await interaction.response.send_message(
                f"❌ Report #{rid} not found or already reviewed.", ephemeral=True)

        action_taken = self.action_note.value or ("Resolved" if self.action == "resolve" else "Dismissed")
        await d1_query(
            "UPDATE pvp_reports SET reviewed = 1, action_taken = ? WHERE id = ?",
            [action_taken, rid]
        )

        if self.action == "resolve":
            report = row["results"][0]
            await d1_query(
                "UPDATE users SET pvp_trust = MAX(0, pvp_trust - 1) WHERE discord_id = ?",
                [report["reported_id"]]
            )

        await interaction.response.send_message(
            f"✅ Report #{rid} {'resolved' if self.action == 'resolve' else 'dismissed'}.",
            ephemeral=True,
        )


class PvpReportsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="Resolve Report", style=discord.ButtonStyle.success, emoji="✅")
    async def resolve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        await interaction.response.send_modal(ReportActionModal("resolve"))

    @discord.ui.button(label="Dismiss Report", style=discord.ButtonStyle.secondary, emoji="🗑️")
    async def dismiss(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        await interaction.response.send_modal(ReportActionModal("dismiss"))


# ── Channel creation ──────────────────────────────────────────────────────────

async def _create_match_channel(
    bot: commands.Bot,
    guild_id: int,
    match_id: str,
    p1_id: int,
    p2_id: int,
    match_type: str,
):
    guild = bot.get_guild(guild_id)
    if not guild:
        return
    p1 = guild.get_member(p1_id)
    p2 = guild.get_member(p2_id)
    if not p1 or not p2:
        return

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        p1: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        p2: discord.PermissionOverwrite(read_messages=True, send_messages=True),
    }
    for role_id in (MOD_ROLE_ID, HEAD_STAFF_ROLE_ID, FOUNDER_ROLE_ID):
        role = guild.get_role(role_id)
        if role:
            overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

    category  = discord.utils.get(guild.categories, name=PVP_CATEGORY_NAME) or await guild.create_category(PVP_CATEGORY_NAME)
    chan_name  = f"pvp-{p1.display_name[:10]}-vs-{p2.display_name[:10]}".lower().replace(" ", "-")

    try:
        channel = await guild.create_text_channel(
            chan_name, overwrites=overwrites, category=category,
            topic=f"PvP match {match_id}"
        )
    except Exception as e:
        print(f"[PvP] Failed to create match channel: {e}")
        return

    ps_code = await _claim_ps_code(match_id)

    p1_pr = await d1_query(
        "SELECT pvp_placement_left, pvp_placement_done FROM users WHERE discord_id = ?",
        [str(p1_id)]
    )
    p2_pr = await d1_query(
        "SELECT pvp_placement_left, pvp_placement_done FROM users WHERE discord_id = ?",
        [str(p2_id)]
    )
    p1_pd = p1_pr["results"][0] if p1_pr["results"] else {}
    p2_pd = p2_pr["results"][0] if p2_pr["results"] else {}

    pvp_active_matches[match_id] = {
        "player1":      p1_id,
        "player2":      p2_id,
        "match_type":   match_type,
        "channel_id":   channel.id,
        "started_at":   datetime.now(timezone.utc),
        "warned":       False,
        "ps_code":      ps_code,
    }

    await d1_query(
        "UPDATE pvp_matches SET channel_id = ? WHERE match_id = ?",
        [str(channel.id), match_id]
    )

    embed = discord.Embed(
        title="⚔️ PvP Match — Best of 3",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Type",     value=match_type.capitalize(), inline=True)
    embed.add_field(name="Match ID", value=f"`{match_id}`",         inline=True)
    embed.add_field(name="Player 1", value=p1.mention,              inline=True)
    embed.add_field(name="Player 2", value=p2.mention,              inline=True)
    if ps_code:
        embed.add_field(name="🎮 PS Code", value=f"`{ps_code}`", inline=False)
    else:
        embed.add_field(name="🎮 PS Code", value="No codes available — host manually.", inline=False)

    if match_type == "ranked":
        placement_lines = []
        if not bool(p1_pd.get("pvp_placement_done")):
            left = p1_pd.get("pvp_placement_left") or 0
            placement_lines.append(f"{p1.mention}: **{left}** placement match(es) left")
        if not bool(p2_pd.get("pvp_placement_done")):
            left = p2_pd.get("pvp_placement_left") or 0
            placement_lines.append(f"{p2.mention}: **{left}** placement match(es) left")
        if placement_lines:
            embed.add_field(name="📊 Placement Progress", value="\n".join(placement_lines), inline=False)

    embed.set_footer(text="Use the score dropdown once the match ends.")

    await channel.send(
        content=f"{p1.mention} {p2.mention}",
        embed=embed,
        view=MatchPanelView(match_id, p1, p2),
    )


# ── Matchmaking helpers ───────────────────────────────────────────────────────

def _deny_timeout_seconds(deny_count: int) -> int:
    return min(300 * (2 ** deny_count), 1800)


def _find_ranked_pairs(queue: dict[int, dict]) -> list[tuple[int, int]]:
    now   = datetime.now(timezone.utc)
    pairs = []
    remaining = set(queue)

    for uid1 in list(remaining):
        if uid1 not in remaining:
            continue
        d1 = queue[uid1]
        wait = (now - d1["joined_at"]).total_seconds()
        if wait >= 300:          # 5+ min: match anyone
            elo_window = 9999
        elif wait >= 120:        # 2–5 min: expand fast (+50 per 20 s)
            elo_window = 200 + int((wait - 120) / 20) * 50
        else:                    # < 2 min: tight window
            elo_window = 75 + int(wait / 30) * 25

        best = None
        for uid2 in remaining:
            if uid2 == uid1:
                continue
            d2 = queue[uid2]
            if abs(d1["elo"] - d2["elo"]) > elo_window:
                continue
            same_tier = (d1["trust"] >= 7) == (d2["trust"] >= 7)
            if best is None or same_tier:
                best = uid2
            if same_tier:
                break

        if best is not None:
            pairs.append((uid1, best))
            remaining.discard(uid1)
            remaining.discard(best)

    return pairs


# ── PvP Cog ───────────────────────────────────────────────────────────────────

class PvPCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._panel_msg_id: int | None = None
        self._matchmaking_task  = bot.loop.create_task(self._matchmaking_loop())
        self._rank_assign_task  = bot.loop.create_task(self._assign_unranked_on_startup())
        self._weekly_reset.start()
        self._elo_decay.start()
        self._match_timeout_check.start()

    def cog_unload(self):
        self._matchmaking_task.cancel()
        self._rank_assign_task.cancel()
        self._weekly_reset.cancel()
        self._elo_decay.cancel()
        self._match_timeout_check.cancel()

    # ── Background tasks ───────────────────────────────────────────────────

    @tasks.loop(hours=168)  # weekly
    async def _weekly_reset(self):
        await self.bot.wait_until_ready()
        await d1_query(
            "UPDATE users SET pvp_deny_count = 0, pvp_timeout_until = NULL "
            "WHERE pvp_deny_count > 0 OR pvp_timeout_until IS NOT NULL"
        )
        print("[PvP] Weekly deny count and queue timeouts reset")

    @tasks.loop(hours=168)  # weekly ELO decay check
    async def _elo_decay(self):
        await self.bot.wait_until_ready()
        cutoff = (datetime.now(timezone.utc) - timedelta(weeks=ELO_DECAY_INACTIVE_WKS)).isoformat()
        await d1_query(
            """UPDATE users
               SET pvp_elo = MAX(1000, pvp_elo - ?)
               WHERE pvp_placement_done = 1
               AND pvp_elo > 1000
               AND (updated_at IS NULL OR updated_at < ?)""",
            [ELO_DECAY_AMOUNT, cutoff]
        )
        print("[PvP] ELO decay applied to inactive players")

    @tasks.loop(minutes=1)
    async def _match_timeout_check(self):
        await self.bot.wait_until_ready()
        now   = datetime.now(timezone.utc)
        guild = self.bot.get_guild(GUILD_ID)

        for match_id, match in list(pvp_active_matches.items()):
            started  = match.get("started_at")
            if not started:
                continue
            elapsed  = (now - started).total_seconds()
            channel  = self.bot.get_channel(match.get("channel_id", 0))

            if elapsed >= MATCH_TIMEOUT_DELETE_S:
                note = await _apply_timeout_penalty(self.bot, match_id, guild)
                if channel:
                    try:
                        await channel.send(
                            f"⏰ **Match timed out — no winner declared.**{note}\n"
                            "This channel will be deleted in 10 seconds."
                        )
                        await asyncio.sleep(10)
                        await channel.delete(reason="PvP match timeout")
                    except Exception:
                        pass

            elif elapsed >= MATCH_TIMEOUT_WARN_S and not match.get("warned"):
                match["warned"] = True
                if channel:
                    p1 = guild.get_member(match["player1"]) if guild else None
                    p2 = guild.get_member(match["player2"]) if guild else None
                    try:
                        await channel.send(
                            f"⚠️ {p1.mention if p1 else ''} {p2.mention if p2 else ''} "
                            "This match has been inactive for **30 minutes**. "
                            "Please submit the score or the match will be auto-forfeited in **10 minutes** "
                            f"and both players will lose **{TIMEOUT_ELO_PENALTY} ELO** (ranked)."
                        )
                    except Exception:
                        pass

        # Release PS codes stuck on matches that ended 3+ hours ago or never got cleaned up
        cutoff = (now - timedelta(hours=3)).isoformat()
        try:
            await d1_query(
                """UPDATE pvp_ps_codes
                   SET match_id = NULL
                   WHERE match_id IS NOT NULL
                   AND match_id IN (
                       SELECT match_id FROM pvp_matches
                       WHERE started_at < ? AND ended_at IS NULL
                   )""",
                [cutoff]
            )
        except Exception:
            pass

    # ── Startup helpers ────────────────────────────────────────────────────

    async def _assign_unranked_on_startup(self):
        await self.bot.wait_until_ready()
        guild = self.bot.get_guild(GUILD_ID)
        if not guild:
            return
        unranked_role = guild.get_role(UNRANKED_ROLE_ID)
        if not unranked_role:
            print(f"[PvP] Unranked role {UNRANKED_ROLE_ID} not found")
            return
        count = 0
        for member in guild.members:
            if not is_linked(member):
                continue
            if any(r.id in ALL_RANK_ROLE_IDS for r in member.roles):
                continue
            try:
                await member.add_roles(unranked_role, reason="PvP: auto-assign Unranked")
                count += 1
                await asyncio.sleep(0.5)
            except Exception:
                pass
        if count:
            print(f"✅ Assigned Unranked PvP role to {count} linked member(s)")

    def _build_panel_embed(self, guild_id: int) -> discord.Embed:
        guild_queue = pvp_queue.get(guild_id, {})
        ranked_n    = sum(1 for d in guild_queue.values() if d["type"] == "ranked")
        casual_n    = sum(1 for d in guild_queue.values() if d["type"] == "casual")
        total_n     = ranked_n + casual_n

        queue_line = (
            f"👥 **{total_n} player{'s' if total_n != 1 else ''} in queue** — "
            f"{ranked_n} ranked · {casual_n} casual"
        ) if total_n else "👥 **Queue is empty** — be the first!"

        return discord.Embed(
            title="⚔️ PvP Arena",
            description=(
                "Challenge other players to ranked or casual matches!\n\n"
                "**Ranked** — ELO-based. First 10 matches are placement (boosted ELO gains). Earns you a rank.\n"
                "**Casual** — No ELO or placement impact. Pure fun.\n\n"
                f"{queue_line}\n\n"
                "Click **Start PvP** below to enter the queue."
            ),
            color=discord.Color.blurple(),
        )

    async def _update_panel_count(self, guild_id: int):
        if not self._panel_msg_id:
            return
        channel = self.bot.get_channel(PVP_CHANNEL_ID)
        if not channel:
            return
        try:
            msg = await channel.fetch_message(self._panel_msg_id)
            await msg.edit(embed=self._build_panel_embed(guild_id))
        except Exception:
            pass

    # ── Matchmaking loop ───────────────────────────────────────────────────

    async def _matchmaking_loop(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                await self._run_matchmaking()
            except Exception as e:
                print(f"[PvP matchmaking error] {e}")
            await asyncio.sleep(5)

    async def _run_matchmaking(self):
        panel_updated = False
        now = datetime.now(timezone.utc)

        for guild_id, guild_queue in list(pvp_queue.items()):
            ranked = {uid: d for uid, d in guild_queue.items() if d["type"] == "ranked"}
            casual = {uid: d for uid, d in guild_queue.items() if d["type"] == "casual"}

            for p1_id, p2_id in _find_ranked_pairs(ranked):
                await self._invite_pair(guild_id, p1_id, p2_id, "ranked",
                                        ranked[p1_id]["elo"], ranked[p2_id]["elo"])
                guild_queue.pop(p1_id, None)
                guild_queue.pop(p2_id, None)
                panel_updated = True

            casual_ids = list(casual)
            while len(casual_ids) >= 2:
                p1_id, p2_id = casual_ids.pop(0), casual_ids.pop(0)
                await self._invite_pair(guild_id, p1_id, p2_id, "casual",
                                        casual[p1_id]["elo"], casual[p2_id]["elo"])
                guild_queue.pop(p1_id, None)
                guild_queue.pop(p2_id, None)
                panel_updated = True

            if panel_updated:
                await self._update_panel_count(guild_id)

            # 5-minute ranked queue DM — fires once per player per queue session
            for uid, entry in list(guild_queue.items()):
                if entry.get("type") != "ranked":
                    continue
                wait_s = (now - entry["joined_at"]).total_seconds()
                if wait_s >= 300 and not entry.get("five_min_dm_sent"):
                    entry["five_min_dm_sent"] = True
                    try:
                        user = await self.bot.fetch_user(uid)
                        await user.send(
                            "⏳ You've been in the **ranked queue** for **5 minutes**.\n"
                            "ELO restrictions are now lifted — you'll match with anyone.\n"
                            "Use `/pvpleave` if you want to leave the queue."
                        )
                    except Exception:
                        pass

        # Expire stale pending confirmations
        for mid in list(pvp_pending):
            pending = pvp_pending[mid]
            if pending["expiry"] >= now:
                continue

            pending = pvp_pending.pop(mid)
            match_type = pending.get("match_type", "casual")
            guild_id   = pending.get("guild_id", GUILD_ID)

            for uid in (pending["player1"], pending["player2"]):
                accepted_key = "p1_accepted" if uid == pending["player1"] else "p2_accepted"
                did_accept   = pending.get(accepted_key, False)

                # No-response ranked penalty (neither player responded)
                if match_type == "ranked" and not did_accept:
                    row = await d1_query(
                        "SELECT pvp_deny_count FROM users WHERE discord_id = ?", [str(uid)]
                    )
                    deny_count    = (row["results"][0]["pvp_deny_count"] or 0) if row["results"] else 0
                    timeout_s     = _deny_timeout_seconds(deny_count)
                    timeout_until = (now + timedelta(seconds=timeout_s)).isoformat()
                    await d1_query(
                        "UPDATE users SET pvp_deny_count = pvp_deny_count + 1, pvp_timeout_until = ? WHERE discord_id = ?",
                        [timeout_until, str(uid)]
                    )

                try:
                    u   = await self.bot.fetch_user(uid)
                    msg = "⏰ Match confirmation timed out."
                    if match_type == "ranked" and not did_accept:
                        msg += " You received a queue timeout for not responding to a ranked invite."
                    elif not did_accept:
                        msg += " You have been returned to the queue."

                    await u.send(msg)

                    # Re-queue player if not penalised (casual, or they accepted but partner didn't)
                    if match_type == "casual" or did_accept:
                        o_row = await d1_query(
                            "SELECT pvp_elo, pvp_trust FROM users WHERE discord_id = ?", [str(uid)]
                        )
                        if o_row["results"]:
                            pvp_queue.setdefault(guild_id, {})[uid] = {
                                "type":      match_type,
                                "elo":       o_row["results"][0].get("pvp_elo")   or BASE_ELO,
                                "trust":     o_row["results"][0].get("pvp_trust") or 10.0,
                                "joined_at": now,
                            }
                except Exception:
                    pass

    async def _invite_pair(
        self,
        guild_id: int,
        p1_id: int,
        p2_id: int,
        match_type: str,
        p1_elo: int,
        p2_elo: int,
    ):
        match_id = str(uuid.uuid4())[:8]
        expiry   = datetime.now(timezone.utc) + timedelta(seconds=60)
        pvp_pending[match_id] = {
            "player1":    p1_id,
            "player2":    p2_id,
            "guild_id":   guild_id,
            "match_type": match_type,
            "p1_accepted": False,
            "p2_accepted": False,
            "expiry":      expiry,
        }

        now = datetime.now(timezone.utc).isoformat()
        await d1_query(
            """INSERT INTO pvp_matches
               (match_id, player1_id, player2_id, match_type,
                p1_elo_before, p2_elo_before, started_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [match_id, str(p1_id), str(p2_id), match_type, p1_elo, p2_elo, now, now]
        )

        guild = self.bot.get_guild(guild_id)
        for uid, opp_id, opp_elo in ((p1_id, p2_id, p2_elo), (p2_id, p1_id, p1_elo)):
            try:
                user     = await self.bot.fetch_user(uid)
                opp      = guild.get_member(opp_id) if guild else None
                opp_name = opp.display_name if opp else f"<{opp_id}>"

                embed = discord.Embed(
                    title="⚔️ Match Found!",
                    color=discord.Color.blurple(),
                    timestamp=datetime.now(timezone.utc),
                )
                embed.add_field(name="Type",      value=match_type.capitalize(), inline=True)
                embed.add_field(name="Opponent",  value=opp_name,                inline=True)
                embed.add_field(name="Their ELO", value=str(opp_elo),            inline=True)
                embed.set_footer(text="You have 60 seconds to respond. Best of 3.")

                await user.send(embed=embed, view=MatchFoundView(match_id, guild_id, uid))
            except Exception as e:
                print(f"[PvP] Could not DM user {uid}: {e}")

    # ── Queue helpers ──────────────────────────────────────────────────────

    async def _queue_add(self, guild_id: int, user_id: int, match_type: str, elo: int, trust: float):
        pvp_queue.setdefault(guild_id, {})[user_id] = {
            "type":      match_type,
            "elo":       elo,
            "trust":     trust,
            "joined_at": datetime.now(timezone.utc),
        }
        await self._update_panel_count(guild_id)

    async def _queue_remove(self, guild_id: int, user_id: int):
        pvp_queue.get(guild_id, {}).pop(user_id, None)
        await self._update_panel_count(guild_id)

    def _in_queue(self, guild_id: int, user_id: int) -> bool:
        return user_id in pvp_queue.get(guild_id, {})

    def _in_match(self, user_id: int) -> bool:
        return any(
            m["player1"] == user_id or m["player2"] == user_id
            for m in pvp_active_matches.values()
        )

    # ── Queue timeout check helper ────────────────────────────────────────

    async def _check_queue_timeout(self, user_id: int) -> str | None:
        """Returns an error message if the user is timed out, else None."""
        row = await d1_query(
            "SELECT pvp_timeout_until, pvp_banned FROM users WHERE discord_id = ?",
            [str(user_id)]
        )
        if not row["results"]:
            return None
        data = row["results"][0]
        if data.get("pvp_banned"):
            return "❌ You are suspended from PvP."
        timeout_str = data.get("pvp_timeout_until")
        if timeout_str:
            timeout_dt = datetime.fromisoformat(timeout_str)
            if timeout_dt.tzinfo is None:
                timeout_dt = timeout_dt.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) < timeout_dt:
                rem = int((timeout_dt - datetime.now(timezone.utc)).total_seconds() // 60) + 1
                return f"⏳ You are in a queue timeout for **{rem} more minute(s)**."
        return None

    # ── Commands ──────────────────────────────────────────────────────────

    @app_commands.command(name="challenge", description="Challenge another player to a casual PvP match")
    @app_commands.describe(user="The player you want to challenge")
    async def challenge(self, interaction: discord.Interaction, user: discord.Member):
        if user.id == interaction.user.id:
            return await interaction.response.send_message("❌ You can't challenge yourself.", ephemeral=True)
        if user.bot:
            return await interaction.response.send_message("❌ You can't challenge a bot.", ephemeral=True)
        if not is_linked(interaction.user):
            return await interaction.response.send_message(
                "❌ Link your Roblox account first.", ephemeral=True)
        if not is_linked(user):
            return await interaction.response.send_message(
                f"❌ {user.mention} hasn't linked their account.", ephemeral=True)

        guild_id = interaction.guild_id

        err = await self._check_queue_timeout(interaction.user.id)
        if err:
            return await interaction.response.send_message(err, ephemeral=True)

        if self._in_match(interaction.user.id):
            return await interaction.response.send_message("❌ You are already in a match.", ephemeral=True)
        if self._in_match(user.id):
            return await interaction.response.send_message(
                f"❌ {user.mention} is already in a match.", ephemeral=True)
        if self._in_queue(guild_id, interaction.user.id):
            return await interaction.response.send_message(
                "❌ You are in the queue. Use `/pvpleave` first.", ephemeral=True)

        p_row = await d1_query(
            "SELECT pvp_elo, pvp_trust FROM users WHERE discord_id = ?", [str(interaction.user.id)]
        )
        o_row = await d1_query(
            "SELECT pvp_elo FROM users WHERE discord_id = ?", [str(user.id)]
        )
        p_elo   = (p_row["results"][0].get("pvp_elo")   or BASE_ELO) if p_row["results"] else BASE_ELO
        o_elo   = (o_row["results"][0].get("pvp_elo")   or BASE_ELO) if o_row["results"] else BASE_ELO

        await interaction.response.send_message(
            f"✅ Challenge sent to {user.mention}! They have **60 seconds** to respond.",
            ephemeral=True,
        )

        match_id = str(uuid.uuid4())[:8]
        expiry   = datetime.now(timezone.utc) + timedelta(seconds=60)
        pvp_pending[match_id] = {
            "player1":    interaction.user.id,
            "player2":    user.id,
            "guild_id":   guild_id,
            "match_type": "casual",
            "p1_accepted": True,
            "p2_accepted": False,
            "expiry":      expiry,
        }

        now = datetime.now(timezone.utc).isoformat()
        await d1_query(
            """INSERT INTO pvp_matches
               (match_id, player1_id, player2_id, match_type,
                p1_elo_before, p2_elo_before, started_at, created_at)
               VALUES (?, ?, ?, 'casual', ?, ?, ?, ?)""",
            [match_id, str(interaction.user.id), str(user.id), p_elo, o_elo, now, now]
        )

        embed = discord.Embed(
            title="⚔️ PvP Challenge!",
            description=(
                f"{interaction.user.mention} challenged you to a **casual** match!\n\n"
                f"**Challenger ELO:** {p_elo}\n"
                f"**Your ELO:** {o_elo}\n\n"
                "Best of 3."
            ),
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text="You have 60 seconds to respond.")
        try:
            await user.send(embed=embed, view=MatchFoundView(match_id, guild_id, user.id))
        except discord.Forbidden:
            pvp_pending.pop(match_id, None)
            await interaction.followup.send(
                f"❌ Could not DM {user.mention} — they may have DMs disabled.", ephemeral=True
            )

    @app_commands.command(name="pvpleave", description="Leave the PvP queue")
    async def pvp_leave(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        if not self._in_queue(guild_id, interaction.user.id):
            return await interaction.response.send_message("❌ You are not in the queue.", ephemeral=True)
        await self._queue_remove(guild_id, interaction.user.id)
        await interaction.response.send_message("✅ Left the queue.", ephemeral=True)

    @app_commands.command(name="pvpstats", description="View PvP stats for yourself or another player")
    @app_commands.describe(user="Player to check (defaults to you)")
    async def pvp_stats(self, interaction: discord.Interaction, user: discord.Member | None = None):
        target = user or interaction.user
        await interaction.response.defer()

        row = await d1_query(
            "SELECT pvp_elo, pvp_wins, pvp_losses, pvp_rank, pvp_placement_done, pvp_placement_left "
            "FROM users WHERE discord_id = ?",
            [str(target.id)]
        )
        if not row["results"]:
            return await interaction.followup.send(
                f"❌ {target.mention} hasn't linked their account.", ephemeral=True
            )

        data   = row["results"][0]
        elo    = data.get("pvp_elo")    or BASE_ELO
        wins   = data.get("pvp_wins")   or 0
        losses = data.get("pvp_losses") or 0
        rank   = data.get("pvp_rank")   or "Unranked"
        pdone  = bool(data.get("pvp_placement_done"))
        pleft  = data.get("pvp_placement_left") or 0
        total  = wins + losses
        winrate = f"{wins/total*100:.1f}%" if total else "—"

        # Last 5 matches
        matches_row = await d1_query(
            """SELECT winner_id, player1_id, player2_id, score, p1_elo_after, p2_elo_after, ended_at
               FROM pvp_matches
               WHERE (player1_id = ? OR player2_id = ?) AND ended_at IS NOT NULL
               ORDER BY ended_at DESC LIMIT 5""",
            [str(target.id), str(target.id)]
        )

        embed = discord.Embed(
            title=f"⚔️ PvP Stats — {target.display_name}",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Rank",    value=rank,         inline=True)
        embed.add_field(name="ELO",     value=str(elo),     inline=True)
        embed.add_field(name="W / L",   value=f"{wins} / {losses}", inline=True)
        embed.add_field(name="Win Rate", value=winrate,     inline=True)
        embed.add_field(name="Matches", value=str(total),  inline=True)
        if not pdone:
            embed.add_field(name="Placement", value=f"{pleft} left", inline=True)

        history = []
        for m in matches_row.get("results", []):
            won    = str(target.id) == str(m.get("winner_id"))
            opp_id = m["player2_id"] if str(target.id) == m["player1_id"] else m["player1_id"]
            score  = m.get("score") or "—"
            icon   = "🟢" if won else "🔴"
            history.append(f"{icon} vs <@{opp_id}> `{score}`")

        if history:
            embed.add_field(name="Last 5 Matches", value="\n".join(history), inline=False)

        embed.set_thumbnail(url=target.display_avatar.url)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="pvpleaderboard", description="Top 10 players by ELO")
    async def pvp_leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer()

        row = await d1_query(
            """SELECT discord_id, pvp_elo, pvp_wins, pvp_losses, pvp_rank
               FROM users
               WHERE pvp_placement_done = 1
               ORDER BY pvp_elo DESC LIMIT 10"""
        )

        embed = discord.Embed(
            title="🏆 PvP Leaderboard — Top 10",
            color=discord.Color.gold(),
        )

        lines = []
        medals = ["🥇", "🥈", "🥉"]
        for i, r in enumerate(row.get("results", [])):
            medal  = medals[i] if i < 3 else f"**#{i+1}**"
            uid    = r["discord_id"]
            elo    = r.get("pvp_elo")    or BASE_ELO
            rank   = r.get("pvp_rank")   or "Unranked"
            wins   = r.get("pvp_wins")   or 0
            losses = r.get("pvp_losses") or 0
            lines.append(f"{medal} <@{uid}> — **{elo} ELO** · {rank} · {wins}W/{losses}L")

        embed.description = "\n".join(lines) if lines else "No ranked players yet."
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="pvpreports", description="View unreviewed PvP reports (Staff only)")
    async def pvp_reports(self, interaction: discord.Interaction):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)

        row = await d1_query(
            """SELECT id, reporter_id, reported_id, match_id, reason, created_at
               FROM pvp_reports WHERE reviewed = 0 ORDER BY created_at DESC LIMIT 10"""
        )
        results = row.get("results", [])

        embed = discord.Embed(
            title="🚩 Unreviewed PvP Reports",
            color=discord.Color.red(),
        )
        if not results:
            embed.description = "✅ No unreviewed reports."
            return await interaction.followup.send(embed=embed, ephemeral=True)

        for r in results:
            ts = r.get("created_at", "")[:10]
            embed.add_field(
                name=f"Report #{r['id']} — {ts}",
                value=(
                    f"**Reporter:** <@{r['reporter_id']}>\n"
                    f"**Reported:** <@{r['reported_id']}>\n"
                    f"**Match:** `{r.get('match_id') or 'N/A'}`\n"
                    f"**Reason:** {r['reason'][:100]}"
                ),
                inline=False,
            )

        embed.set_footer(text="Use Resolve/Dismiss buttons and enter the Report ID.")
        await interaction.followup.send(embed=embed, view=PvpReportsView(), ephemeral=True)

    @app_commands.command(name="pvpsetelo", description="Manually set a player's ELO (Staff only)")
    @app_commands.describe(user="Target player", elo="New ELO value", reason="Reason for adjustment")
    async def pvp_set_elo(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        elo: app_commands.Range[int, 0, 9999],
        reason: str = "Manual adjustment",
    ):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("❌ Staff only.", ephemeral=True)

        now = datetime.now(timezone.utc).isoformat()
        await d1_query(
            "UPDATE users SET pvp_elo = ?, updated_at = ? WHERE discord_id = ?",
            [elo, now, str(user.id)]
        )
        new_rank = await compute_pvp_rank(elo)
        await d1_query(
            "UPDATE users SET pvp_rank = ? WHERE discord_id = ?", [new_rank, str(user.id)]
        )
        await update_pvp_rank_role(user, new_rank)

        embed = discord.Embed(
            title="✏️ ELO Updated",
            description=(
                f"{user.mention}'s ELO set to **{elo}** (rank: **{new_rank}**).\n"
                f"Reason: {reason}"
            ),
            color=discord.Color.blue(),
        )
        await interaction.response.send_message(embed=embed)

        # Log to audit channel (best-effort)
        try:
            log_ch = interaction.guild.get_channel(LOG_CHANNELS.get("pvp", 0))
            if log_ch:
                await log_ch.send(
                    f"[PvP ELO] {interaction.user.mention} set {user.mention}'s ELO → **{elo}** | {reason}"
                )
        except Exception:
            pass

    @app_commands.command(name="pvpsuspend", description="Suspend a player from PvP (Staff only)")
    @app_commands.describe(user="Player to suspend")
    async def pvp_suspend(self, interaction: discord.Interaction, user: discord.Member):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        await d1_query(
            "UPDATE users SET pvp_banned = 1 WHERE discord_id = ?", [str(user.id)]
        )
        await interaction.response.send_message(
            f"🚫 {user.mention} has been suspended from PvP.", ephemeral=True
        )

    @app_commands.command(name="pvpunsuspend", description="Lift a player's PvP suspension (Staff only)")
    @app_commands.describe(user="Player to unsuspend")
    async def pvp_unsuspend(self, interaction: discord.Interaction, user: discord.Member):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        await d1_query(
            "UPDATE users SET pvp_banned = 0 WHERE discord_id = ?", [str(user.id)]
        )
        await interaction.response.send_message(
            f"✅ {user.mention}'s PvP suspension lifted.", ephemeral=True
        )

    @app_commands.command(name="pvphistory", description="View recent match history for yourself or another player")
    @app_commands.describe(user="Player to check (defaults to you)")
    async def pvp_history(self, interaction: discord.Interaction, user: discord.Member | None = None):
        target = user or interaction.user
        await interaction.response.defer()

        matches_row = await d1_query(
            """SELECT match_id, winner_id, player1_id, player2_id, score,
                      p1_elo_before, p2_elo_before, p1_elo_after, p2_elo_after,
                      match_type, ended_at
               FROM pvp_matches
               WHERE (player1_id = ? OR player2_id = ?) AND ended_at IS NOT NULL
               ORDER BY ended_at DESC LIMIT 10""",
            [str(target.id), str(target.id)]
        )

        embed = discord.Embed(
            title=f"📜 PvP History — {target.display_name}",
            color=discord.Color.blurple(),
        )
        embed.set_thumbnail(url=target.display_avatar.url)

        results = matches_row.get("results", [])
        if not results:
            embed.description = "No match history found."
            return await interaction.followup.send(embed=embed)

        lines = []
        for m in results:
            tid   = str(target.id)
            won   = tid == str(m.get("winner_id"))
            is_p1 = tid == str(m["player1_id"])
            opp_id = m["player2_id"] if is_p1 else m["player1_id"]

            elo_before = m.get("p1_elo_before") if is_p1 else m.get("p2_elo_before")
            elo_after  = m.get("p1_elo_after")  if is_p1 else m.get("p2_elo_after")

            score      = m.get("score") or "—"
            mtype      = m.get("match_type") or "casual"
            icon       = "🟢" if won else "🔴"
            type_icon  = "🏆" if mtype == "ranked" else "🎮"
            date_str   = (m.get("ended_at") or "")[:10]

            delta_str = ""
            if elo_before is not None and elo_after is not None:
                delta = elo_after - elo_before
                sign  = "+" if delta >= 0 else ""
                delta_str = f" `{sign}{delta} ELO`"

            lines.append(f"{icon} {type_icon} vs <@{opp_id}> **{score}**{delta_str} · {date_str}")

        embed.description = "\n".join(lines)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="pvpsetresult", description="Staff: manually set the result of an active match")
    @app_commands.describe(
        match_id="Match ID (shown in the channel topic or /pvpstats)",
        winner="The winning player",
        score="Score — 2-0 or 2-1",
    )
    @app_commands.choices(score=[
        app_commands.Choice(name="2-0", value="2-0"),
        app_commands.Choice(name="2-1", value="2-1"),
    ])
    async def pvp_set_result(
        self,
        interaction: discord.Interaction,
        match_id: str,
        winner: discord.Member,
        score: str,
    ):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("❌ Staff only.", ephemeral=True)

        match = pvp_active_matches.get(match_id)
        if not match:
            return await interaction.response.send_message(
                f"❌ No active match found with ID `{match_id}`.", ephemeral=True
            )
        if winner.id not in (match["player1"], match["player2"]):
            return await interaction.response.send_message(
                "❌ The winner must be a participant in that match.", ephemeral=True
            )

        loser_id = match["player2"] if winner.id == match["player1"] else match["player1"]
        p1_id, p2_id = match["player1"], match["player2"]

        await interaction.response.defer(ephemeral=True)

        channel = interaction.client.get_channel(match.get("channel_id", 0))
        if channel:
            try:
                await channel.send(
                    f"📋 **Staff result override** by {interaction.user.mention}: "
                    f"{winner.mention} wins **{score}**."
                )
            except Exception:
                pass

        embed = await _apply_match_result(
            interaction.client, match_id, winner.id, loser_id, score, interaction.guild
        )
        if channel and embed:
            try:
                view = RematchView(p1_id, p2_id, interaction.guild_id)
                await channel.send(embed=embed, view=view)
                await asyncio.sleep(15)
                await channel.delete(reason="PvP match ended — staff override")
            except Exception:
                pass

        await interaction.followup.send(
            f"✅ Match `{match_id}` resolved: {winner.mention} wins **{score}**.", ephemeral=True
        )
        try:
            log_ch = interaction.guild.get_channel(LOG_CHANNELS.get("pvp", 0))
            if log_ch:
                await log_ch.send(
                    f"[PvP Override] {interaction.user.mention} set match `{match_id}` → "
                    f"{winner.mention} wins {score}"
                )
        except Exception:
            pass

    @app_commands.command(name="pvpforfeit", description="Staff: force-forfeit a player in their active match")
    @app_commands.describe(user="Player to forfeit")
    async def pvp_forfeit_staff(self, interaction: discord.Interaction, user: discord.Member):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("❌ Staff only.", ephemeral=True)

        match_id   = None
        match_data = None
        for mid, m in pvp_active_matches.items():
            if user.id in (m["player1"], m["player2"]):
                match_id   = mid
                match_data = m
                break

        if not match_id:
            return await interaction.response.send_message(
                f"❌ {user.mention} is not in an active match.", ephemeral=True
            )

        winner_id = match_data["player2"] if user.id == match_data["player1"] else match_data["player1"]
        p1_id, p2_id = match_data["player1"], match_data["player2"]

        await interaction.response.defer(ephemeral=True)

        channel = interaction.client.get_channel(match_data.get("channel_id", 0))
        if channel:
            try:
                await channel.send(
                    f"🏳️ **Staff forfeit** by {interaction.user.mention}: "
                    f"{user.mention} has been forfeited. <@{winner_id}> wins **(2-0)**."
                )
            except Exception:
                pass

        embed = await _apply_match_result(
            interaction.client, match_id, winner_id, user.id, "2-0", interaction.guild
        )
        if channel and embed:
            try:
                view = RematchView(p1_id, p2_id, interaction.guild_id)
                await channel.send(embed=embed, view=view)
                await asyncio.sleep(15)
                await channel.delete(reason="PvP match ended — staff forfeit")
            except Exception:
                pass

        await interaction.followup.send(
            f"✅ {user.mention} forfeited match `{match_id}`. <@{winner_id}> wins.", ephemeral=True
        )
        try:
            log_ch = interaction.guild.get_channel(LOG_CHANNELS.get("pvp", 0))
            if log_ch:
                await log_ch.send(
                    f"[PvP Forfeit] {interaction.user.mention} force-forfeited {user.mention} "
                    f"from match `{match_id}` — <@{winner_id}> wins"
                )
        except Exception:
            pass



# ── Panel views (persistent) ───────────────────────────────────────────────────

class PvPPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Start PvP", style=discord.ButtonStyle.blurple, emoji="⚔️",
                       custom_id="pvp_start")
    async def start_pvp(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            cog: PvPCog = interaction.client.cogs.get("PvPCog")
            if not cog:
                return await interaction.response.send_message("❌ PvP system offline.", ephemeral=True)

            guild_id = interaction.guild_id

            if not is_linked(interaction.user):
                return await interaction.response.send_message(
                    "❌ Link your Roblox account to play PvP.", ephemeral=True)

            if cog._in_match(interaction.user.id):
                return await interaction.response.send_message(
                    "❌ You are already in an active match.", ephemeral=True)
            if cog._in_queue(guild_id, interaction.user.id):
                return await interaction.response.send_message(
                    "❌ You are already in the queue. Use `/pvpleave` to exit.", ephemeral=True)

            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException:
            return
        except Exception as e:
            print(f"[PvP] start_pvp error: {e}")
            try:
                return await interaction.response.send_message(
                    "❌ Something went wrong while entering PvP. Please try again.", ephemeral=True)
            except discord.HTTPException:
                return

        try:
            err = await cog._check_queue_timeout(interaction.user.id)
        except Exception as e:
            print(f"[PvP] queue timeout check failed: {e}")
            return await interaction.followup.send(
                "❌ Could not check your PvP status right now. Please try again.", ephemeral=True)

        if err:
            return await interaction.followup.send(err, ephemeral=True)

        await interaction.followup.send(
            embed=discord.Embed(
                title="⚔️ Select Match Type",
                description=(
                    "**Ranked** — ELO-based. First 10 matches are placement (boosted gains). Earns a rank.\n"
                    "**Casual** — No ELO or placement impact. Play for fun."
                ),
                color=discord.Color.blurple(),
            ),
            view=MatchTypeView(guild_id),
            ephemeral=True,
        )


class MatchTypeView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=60)
        self.guild_id = guild_id

    @discord.ui.button(label="Ranked", style=discord.ButtonStyle.danger, emoji="🏆")
    async def ranked(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._enter_queue(interaction, "ranked")

    @discord.ui.button(label="Casual", style=discord.ButtonStyle.secondary, emoji="🎮")
    async def casual(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._enter_queue(interaction, "casual")

    async def _enter_queue(self, interaction: discord.Interaction, match_type: str):
        cog: PvPCog = interaction.client.cogs.get("PvPCog")
        if not cog:
            return await interaction.response.send_message("❌ PvP offline.", ephemeral=True)

        # Defer before D1 query to stay within the 3-second response window
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException:
            return

        try:
            row = await d1_query(
                "SELECT pvp_elo, pvp_trust, pvp_placement_done, pvp_banned FROM users WHERE discord_id = ?",
                [str(interaction.user.id)]
            )
        except Exception as e:
            print(f"[PvP] enter queue query failed: {e}")
            return await interaction.followup.send(
                "❌ Could not reach the database right now. Please try again.", ephemeral=True)
        if not row["results"]:
            return await interaction.followup.send("❌ You haven't verified yet.", ephemeral=True)

        data    = row["results"][0]
        elo     = data.get("pvp_elo")            or BASE_ELO
        trust   = data.get("pvp_trust")          or 10.0
        placed  = bool(data.get("pvp_placement_done"))
        banned  = bool(data.get("pvp_banned"))

        if banned:
            return await interaction.followup.send(
                "❌ You are suspended from PvP.", ephemeral=True)

        await cog._queue_add(self.guild_id, interaction.user.id, match_type, elo, trust)

        queue_size = len(pvp_queue.get(self.guild_id, {}))
        join_ts    = int(datetime.now(timezone.utc).timestamp())
        await interaction.followup.send(
            embed=discord.Embed(
                title=f"✅ Entered {match_type.capitalize()} Queue",
                description=(
                    f"Looking for an opponent...\n"
                    f"Players in queue: **{queue_size}**\n"
                    f"Time in queue: <t:{join_ts}:R>\n\n"
                    "You will receive a DM when a match is found.\n"
                    "Use `/pvpleave` to exit the queue."
                ),
                color=discord.Color.green(),
            ),
            ephemeral=True,
        )
        self.stop()


async def setup(bot: commands.Bot):
    await bot.add_cog(PvPCog(bot))
    bot.add_view(PvPPanelView())
    print("✅ PvP cog loaded")
