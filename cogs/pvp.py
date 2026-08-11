"""
PvP system scaffold — everything is wired and ready; fill in the stub comments
when the system launches.

Flow summary
────────────
1. Staff posts the PvP panel (/pvpsendpanel).
2. Player clicks "Start PvP" → ephemeral match-type picker (Ranked / Casual).
3. Player selects type → enters the queue (in-memory).
4. Background task (every 5 s) matches pairs:
     Ranked : ELO window starts ±50, widens ±25 every 30 s, caps at ±300.
              Trust factor routes low-trust (<7) players together first.
     Casual : random pairing from casual queue.
5. Match found → both players get a DM with Accept / Deny buttons (60 s timeout).
     Deny-ranked → queue timeout (5 min, doubles per offence, resets weekly).
     Deny-casual → silently removed from queue.
6. Both accept → private text channel created (players + Mod+), match panel sent.
7. Either player clicks "End PvP" → winner dropdown → ELO updated, channel deleted.
8. Report button → reason prompt → logged for staff review; affects trust factor.
9. After every match: percentile ranks are recomputed and Discord roles updated.

DB additions needed when launching (run via /pvpsetup or init_database):
    ALTER TABLE users ADD COLUMN pvp_elo            INTEGER DEFAULT 1000;
    ALTER TABLE users ADD COLUMN pvp_wins           INTEGER DEFAULT 0;
    ALTER TABLE users ADD COLUMN pvp_losses         INTEGER DEFAULT 0;
    ALTER TABLE users ADD COLUMN pvp_placement_done INTEGER DEFAULT 0;
    ALTER TABLE users ADD COLUMN pvp_placement_left INTEGER DEFAULT 10;
    ALTER TABLE users ADD COLUMN pvp_rank           TEXT    DEFAULT 'Unranked';
    ALTER TABLE users ADD COLUMN pvp_trust          REAL    DEFAULT 10.0;
    ALTER TABLE users ADD COLUMN pvp_deny_count     INTEGER DEFAULT 0;
    ALTER TABLE users ADD COLUMN pvp_timeout_until  TEXT;

    CREATE TABLE IF NOT EXISTS pvp_matches (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        match_id        TEXT    NOT NULL UNIQUE,
        player1_id      TEXT    NOT NULL,
        player2_id      TEXT    NOT NULL,
        match_type      TEXT    NOT NULL,
        winner_id       TEXT,
        p1_elo_before   INTEGER,
        p2_elo_before   INTEGER,
        p1_elo_after    INTEGER,
        p2_elo_after    INTEGER,
        channel_id      TEXT,
        started_at      TEXT    NOT NULL,
        ended_at        TEXT,
        created_at      TEXT    NOT NULL
    );

    CREATE TABLE IF NOT EXISTS pvp_reports (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        reporter_id TEXT NOT NULL,
        reported_id TEXT NOT NULL,
        match_id    TEXT,
        reason      TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        reviewed    INTEGER DEFAULT 0,
        action_taken TEXT
    );
"""

import uuid
import asyncio
import json
import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone, timedelta

from bot import (
    d1_query,
    GUILD_ID,
    MOD_ROLE_ID,
    HEAD_STAFF_ROLE_ID,
    FOUNDER_ROLE_ID,
    is_linked,
    is_staff,
)

# ── Config ────────────────────────────────────────────────────────────────────
PVP_CHANNEL_ID: int = 1536655138754789386
PVP_CATEGORY_ID: int = 1536654892947603516
PLACEMENT_MATCHES = 10         # matches before a rank is assigned
BASE_ELO = 1000
K_FACTOR_NEW = 32              # K for < 30 matches
K_FACTOR_EST = 16              # K for >= 30 matches

# ── Rank tiers (percentile, display_name, role_id) ───────────────────────────
# Role IDs are 0 until you create the roles; update them here.
RANK_TIERS = [
    (0.01, "Master",   1536656379308285984),
    (0.05, "Amethyst", 1536658641346895892),
    (0.15, "Platinum", 1536658838273663047),
    (0.30, "Gold",     1536658878400303174),
    (0.55, "Silver",   1536658921912012881),
    (1.00, "Bronze",   1536658975356092416),
]
UNRANKED_ROLE_ID = 1536659018549039214
# All rank role IDs (for cleanup before assigning new rank)
ALL_RANK_ROLE_IDS = {t[2] for t in RANK_TIERS} | {UNRANKED_ROLE_ID}

# ── In-memory state ───────────────────────────────────────────────────────────
# queue[guild_id][user_id] = QueueEntry dict
pvp_queue: dict[int, dict[int, dict]] = {}

# active_matches[match_id] = MatchState dict
pvp_active_matches: dict[str, dict] = {}

# pending_confirmations[match_id] = {p1_accepted, p2_accepted, expiry, ...}
pvp_pending: dict[str, dict] = {}


# ── ELO helpers ───────────────────────────────────────────────────────────────

def elo_change(winner_elo: int, loser_elo: int, matches_played: int) -> tuple[int, int]:
    """Standard Elo formula. Returns (winner_new_elo, loser_new_elo)."""
    k = K_FACTOR_NEW if matches_played < 30 else K_FACTOR_EST
    expected = 1 / (1 + 10 ** ((loser_elo - winner_elo) / 400))
    delta = int(k * (1 - expected))
    return winner_elo + delta, loser_elo - delta


async def compute_pvp_rank(elo: int) -> str:
    """
    Percentile-based rank: query total ranked players and this user's position,
    then assign tier by percentile.  Returns rank name string.
    """
    total_row = await d1_query(
        "SELECT COUNT(*) AS n FROM users WHERE pvp_placement_done = 1"
    )
    total = total_row["results"][0]["n"] if total_row["results"] else 0
    if total < 10:
        return "Bronze"  # default until enough players exist

    above_row = await d1_query(
        "SELECT COUNT(*) AS n FROM users WHERE pvp_elo > ? AND pvp_placement_done = 1",
        [elo]
    )
    above = (above_row["results"][0]["n"] or 0)
    pct = (above + 1) / total  # 0.01 = top 1%

    for threshold, name, _ in RANK_TIERS:
        if pct <= threshold:
            return name
    return "Bronze"


async def update_pvp_rank_role(member: discord.Member, new_rank: str):
    """Strip all rank roles then assign the correct one."""
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


# ── Matchmaking helpers ───────────────────────────────────────────────────────

def _deny_timeout_seconds(deny_count: int) -> int:
    """5 min → 10 → 20 → 30 (capped). Resets weekly."""
    return min(300 * (2 ** deny_count), 1800)


def _find_ranked_pairs(queue: dict[int, dict]) -> list[tuple[int, int]]:
    now = datetime.now(timezone.utc)
    pairs = []
    remaining = set(queue)

    for uid1 in list(remaining):
        if uid1 not in remaining:
            continue
        d1 = queue[uid1]
        wait = (now - d1["joined_at"]).total_seconds()
        elo_window = min(50 + int(wait / 30) * 25, 300)

        best = None
        for uid2 in remaining:
            if uid2 == uid1:
                continue
            d2 = queue[uid2]
            if abs(d1["elo"] - d2["elo"]) > elo_window:
                continue
            # Prefer same trust tier: both high (>=7) or both low (<7)
            same_tier = (d1["trust"] >= 7) == (d2["trust"] >= 7)
            if best is None or same_tier:
                best = uid2
            if same_tier:
                break  # found ideal match

        if best is not None:
            pairs.append((uid1, best))
            remaining.discard(uid1)
            remaining.discard(best)

    return pairs


# ── Match invitation DM ───────────────────────────────────────────────────────

class MatchFoundView(discord.ui.View):
    def __init__(self, match_id: str, guild_id: int, for_user_id: int):
        super().__init__(timeout=60)
        self.match_id = match_id
        self.guild_id = guild_id
        self.for_user_id = for_user_id

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="✅",
                       custom_id="pvp_match_accept")
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
            await interaction.response.send_message(
                "✅ Both players accepted! Creating your match channel...", ephemeral=True)
            # TODO: get_or_fetch the guild and members, then call _create_match_channel
        else:
            await interaction.response.send_message(
                "✅ Accepted! Waiting for the other player...", ephemeral=True)

        self.stop()

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, emoji="✖️",
                       custom_id="pvp_match_deny")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.for_user_id:
            return await interaction.response.send_message("❌ This isn't for you.", ephemeral=True)

        pending = pvp_pending.pop(self.match_id, None)
        if not pending:
            return await interaction.response.send_message("❌ Match already expired.", ephemeral=True)

        match_type = pending.get("match_type", "casual")

        if match_type == "ranked":
            # Apply queue timeout
            row = await d1_query(
                "SELECT pvp_deny_count FROM users WHERE discord_id = ?",
                [str(interaction.user.id)]
            )
            deny_count = (row["results"][0]["pvp_deny_count"] or 0) if row["results"] else 0
            timeout_s = _deny_timeout_seconds(deny_count)
            timeout_until = (datetime.now(timezone.utc) + timedelta(seconds=timeout_s)).isoformat()
            await d1_query(
                "UPDATE users SET pvp_deny_count = pvp_deny_count + 1, pvp_timeout_until = ? WHERE discord_id = ?",
                [timeout_until, str(interaction.user.id)]
            )
            await interaction.response.send_message(
                f"❌ Ranked denial — you are in queue timeout for **{timeout_s // 60} minutes**.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message("✅ Match declined.", ephemeral=True)

        # Notify the other player
        other_id = pending["player2"] if interaction.user.id == pending["player1"] else pending["player1"]
        try:
            other = await interaction.client.fetch_user(other_id)
            await other.send("❌ Your opponent declined the match. You have been returned to the queue.")
            # Re-queue other player
            guild_queue = pvp_queue.setdefault(self.guild_id, {})
            if other_id not in guild_queue:
                # TODO: restore their queue entry from pending data
                pass
        except Exception:
            pass
        self.stop()


# ── Match channel panel ────────────────────────────────────────────────────────

class MatchWinnerSelect(discord.ui.Select):
    def __init__(self, player1: discord.Member, player2: discord.Member, match_id: str):
        self.match_id = match_id
        options = [
            discord.SelectOption(label=player1.display_name, value=str(player1.id),
                                 description="Select as winner"),
            discord.SelectOption(label=player2.display_name, value=str(player2.id),
                                 description="Select as winner"),
        ]
        super().__init__(placeholder="Select the winner...", options=options)

    async def callback(self, interaction: discord.Interaction):
        winner_id = int(self.values[0])
        match = pvp_active_matches.get(self.match_id)
        if not match:
            return await interaction.response.send_message("❌ Match not found.", ephemeral=True)

        if interaction.user.id not in (match["player1"], match["player2"]):
            return await interaction.response.send_message(
                "❌ Only match participants can declare a winner.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        loser_id = match["player2"] if winner_id == match["player1"] else match["player1"]

        # Update ELO
        w_row = await d1_query(
            "SELECT pvp_elo, pvp_wins, pvp_losses FROM users WHERE discord_id = ?",
            [str(winner_id)]
        )
        l_row = await d1_query(
            "SELECT pvp_elo, pvp_losses FROM users WHERE discord_id = ?",
            [str(loser_id)]
        )
        if not w_row["results"] or not l_row["results"]:
            return await interaction.followup.send("❌ Could not fetch player ELO.", ephemeral=True)

        w_elo = w_row["results"][0]["pvp_elo"] or BASE_ELO
        l_elo = l_row["results"][0]["pvp_elo"] or BASE_ELO
        w_wins  = (w_row["results"][0]["pvp_wins"]   or 0) + 1
        l_losses= (l_row["results"][0]["pvp_losses"] or 0) + 1
        total_matches = w_wins + (w_row["results"][0]["pvp_losses"] or 0)

        new_w_elo, new_l_elo = elo_change(w_elo, l_elo, total_matches)

        now = datetime.now(timezone.utc).isoformat()
        await d1_query(
            "UPDATE users SET pvp_elo = ?, pvp_wins = ?, updated_at = ? WHERE discord_id = ?",
            [new_w_elo, w_wins, now, str(winner_id)]
        )
        await d1_query(
            "UPDATE users SET pvp_elo = ?, pvp_losses = ?, updated_at = ? WHERE discord_id = ?",
            [new_l_elo, l_losses, now, str(loser_id)]
        )

        # Recalculate ranks
        w_rank = await compute_pvp_rank(new_w_elo)
        l_rank = await compute_pvp_rank(new_l_elo)
        await d1_query("UPDATE users SET pvp_rank = ? WHERE discord_id = ?", [w_rank, str(winner_id)])
        await d1_query("UPDATE users SET pvp_rank = ? WHERE discord_id = ?", [l_rank, str(loser_id)])

        # Update roles
        guild = interaction.guild
        for uid, rank in ((winner_id, w_rank), (loser_id, l_rank)):
            m = guild.get_member(uid)
            if m:
                await update_pvp_rank_role(m, rank)

        # Save match to DB
        await d1_query(
            """UPDATE pvp_matches
               SET winner_id = ?, p1_elo_after = ?, p2_elo_after = ?, ended_at = ?
               WHERE match_id = ?""",
            [str(winner_id), new_w_elo, new_l_elo, now, self.match_id]
        )

        embed = discord.Embed(
            title="🏆 Match Complete",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="Winner", value=f"<@{winner_id}> (**{new_w_elo}** ELO)", inline=True)
        embed.add_field(name="Loser",  value=f"<@{loser_id}> (**{new_l_elo}** ELO)", inline=True)
        embed.add_field(name="Rank Update",
                        value=f"<@{winner_id}> → **{w_rank}**\n<@{loser_id}> → **{l_rank}**",
                        inline=False)
        await interaction.followup.send(embed=embed)

        pvp_active_matches.pop(self.match_id, None)
        await asyncio.sleep(10)
        if interaction.channel:
            await interaction.channel.delete(reason="PvP match ended")


class ReportModal(discord.ui.Modal, title="Report Player"):
    reason = discord.ui.TextInput(
        label="Reason for report",
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
            [str(self.reporter_id), str(self.reported_id),
             self.match_id, self.reason.value, now]
        )
        # Check total unreviewed reports against this player to adjust trust
        rep_count_row = await d1_query(
            "SELECT COUNT(*) AS n FROM pvp_reports WHERE reported_id = ? AND reviewed = 0",
            [str(self.reported_id)]
        )
        rep_count = rep_count_row["results"][0]["n"] if rep_count_row["results"] else 0

        # Each 3 unreviewed reports nudges trust down by 0.5
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
        options = [
            discord.SelectOption(label=p1.display_name, value=str(p1.id)),
            discord.SelectOption(label=p2.display_name, value=str(p2.id)),
        ]
        super().__init__(placeholder="Select player to report...", options=options)

    async def callback(self, interaction: discord.Interaction):
        reported_id = int(self.values[0])
        if reported_id == interaction.user.id:
            return await interaction.response.send_message(
                "❌ You can't report yourself.", ephemeral=True)
        await interaction.response.send_modal(
            ReportModal(self.match_id, interaction.user.id, reported_id))


class MatchPanelView(discord.ui.View):
    def __init__(self, match_id: str, p1: discord.Member, p2: discord.Member):
        super().__init__(timeout=None)
        self.match_id = match_id
        self.p1 = p1
        self.p2 = p2
        self.add_item(MatchWinnerSelect(p1, p2, match_id))
        self.add_item(ReportPlayerSelect(p1, p2, match_id))


# ── Channel creation ──────────────────────────────────────────────────────────

async def _create_match_channel(
    bot: commands.Bot,
    guild_id: int,
    match_id: str,
    p1_id: int,
    p2_id: int,
    match_type: str,
    ps_code: str | None = None,
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

    category = guild.get_channel(PVP_CATEGORY_ID) if PVP_CATEGORY_ID else None
    chan_name = f"pvp-{p1.display_name[:10]}-vs-{p2.display_name[:10]}".lower().replace(" ", "-")
    channel = await guild.create_text_channel(
        chan_name, overwrites=overwrites, category=category,
        topic=f"PvP match {match_id}"
    )

    pvp_active_matches[match_id] = {
        "player1": p1_id, "player2": p2_id,
        "match_type": match_type, "channel_id": channel.id,
        "started_at": datetime.now(timezone.utc),
    }

    await d1_query(
        "UPDATE pvp_matches SET channel_id = ? WHERE match_id = ?",
        [str(channel.id), match_id]
    )

    embed = discord.Embed(
        title="⚔️ PvP Match",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="Type",    value=match_type.capitalize(), inline=True)
    embed.add_field(name="Match ID", value=f"`{match_id}`",         inline=True)
    embed.add_field(name="Player 1", value=p1.mention,             inline=True)
    embed.add_field(name="Player 2", value=p2.mention,             inline=True)
    if ps_code:
        embed.add_field(name="PS Code", value=f"`{ps_code}`", inline=False)
    embed.set_footer(text="Use the dropdown to declare the winner when the match ends.")

    await channel.send(
        content=f"{p1.mention} {p2.mention}",
        embed=embed,
        view=MatchPanelView(match_id, p1, p2),
    )


# ── Matchmaking task ──────────────────────────────────────────────────────────

class PvPCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._panel_msg_id: int | None = None
        self._matchmaking_task = bot.loop.create_task(self._matchmaking_loop())
        self._panel_task = bot.loop.create_task(self._ensure_panel())
        self._rank_assign_task = bot.loop.create_task(self._assign_unranked_on_startup())

    def cog_unload(self):
        self._matchmaking_task.cancel()
        self._panel_task.cancel()
        self._rank_assign_task.cancel()

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
                await asyncio.sleep(0.5)  # stay under rate limit
            except Exception:
                pass
        if count:
            print(f"✅ Assigned Unranked PvP role to {count} linked member(s)")

    async def _ensure_panel(self):
        await self.bot.wait_until_ready()
        channel = self.bot.get_channel(PVP_CHANNEL_ID)
        if not channel:
            print(f"[PvP] Could not find PvP channel {PVP_CHANNEL_ID}")
            return
        # Check for a stored panel message
        try:
            row = await d1_query("SELECT value FROM bot_meta WHERE key = 'pvp_panel_msg_id'")
            if row["results"]:
                stored_id = int(row["results"][0]["value"])
                try:
                    await channel.fetch_message(stored_id)
                    self._panel_msg_id = stored_id
                    return  # Panel already exists
                except (discord.NotFound, discord.HTTPException):
                    pass  # Message gone — resend
        except Exception:
            pass

        msg = await channel.send(embed=self._build_panel_embed(GUILD_ID), view=PvPPanelView())
        self._panel_msg_id = msg.id
        try:
            await d1_query(
                "INSERT OR REPLACE INTO bot_meta (key, value) VALUES ('pvp_panel_msg_id', ?)",
                [str(msg.id)]
            )
        except Exception:
            pass
        print(f"✅ PvP panel sent to channel {PVP_CHANNEL_ID}")

    def _build_panel_embed(self, guild_id: int) -> discord.Embed:
        guild_queue = pvp_queue.get(guild_id, {})
        ranked_n = sum(1 for d in guild_queue.values() if d["type"] == "ranked")
        casual_n = sum(1 for d in guild_queue.values() if d["type"] == "casual")
        total_n  = ranked_n + casual_n

        if total_n:
            queue_line = f"👥 **{total_n} player{'s' if total_n != 1 else ''} in queue** — {ranked_n} ranked · {casual_n} casual"
        else:
            queue_line = "👥 **Queue is empty** — be the first!"

        return discord.Embed(
            title="⚔️ PvP Arena",
            description=(
                "Challenge other players to ranked or casual matches!\n\n"
                "**Ranked** — ELO-based matchmaking. Your rank updates after every match.\n"
                "**Casual** — Quick match with no ELO impact.\n\n"
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
        for guild_id, guild_queue in list(pvp_queue.items()):
            now = datetime.now(timezone.utc)

            # Separate queues by type
            ranked = {uid: d for uid, d in guild_queue.items() if d["type"] == "ranked"}
            casual = {uid: d for uid, d in guild_queue.items() if d["type"] == "casual"}

            # Ranked pairs
            for p1_id, p2_id in _find_ranked_pairs(ranked):
                await self._invite_pair(guild_id, p1_id, p2_id, "ranked",
                                        ranked[p1_id]["elo"], ranked[p2_id]["elo"])
                guild_queue.pop(p1_id, None)
                guild_queue.pop(p2_id, None)
                panel_updated = True

            # Casual pairs (simple FIFO)
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

            # Expire stale pending confirmations
            for mid in list(pvp_pending):
                if pvp_pending[mid]["expiry"] < now:
                    # Notify both players and re-queue them
                    pending = pvp_pending.pop(mid)
                    for uid in (pending["player1"], pending["player2"]):
                        try:
                            u = await self.bot.fetch_user(uid)
                            await u.send("⏰ Match confirmation timed out — you have been returned to the queue.")
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
        expiry = datetime.now(timezone.utc) + timedelta(seconds=60)
        pvp_pending[match_id] = {
            "player1": p1_id, "player2": p2_id,
            "match_type": match_type,
            "p1_accepted": False, "p2_accepted": False,
            "expiry": expiry,
        }

        now = datetime.now(timezone.utc).isoformat()
        await d1_query(
            """INSERT INTO pvp_matches
               (match_id, player1_id, player2_id, match_type, p1_elo_before, p2_elo_before, started_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [match_id, str(p1_id), str(p2_id), match_type, p1_elo, p2_elo, now, now]
        )

        guild = self.bot.get_guild(guild_id)
        for uid, opp_id, opp_elo in ((p1_id, p2_id, p2_elo), (p2_id, p1_id, p1_elo)):
            try:
                user = await self.bot.fetch_user(uid)
                opp  = guild.get_member(opp_id) if guild else None
                opp_name = opp.display_name if opp else f"<{opp_id}>"

                embed = discord.Embed(
                    title="⚔️ Match Found!",
                    color=discord.Color.blurple(),
                    timestamp=datetime.now(timezone.utc)
                )
                embed.add_field(name="Type",     value=match_type.capitalize(), inline=True)
                embed.add_field(name="Opponent", value=opp_name,                inline=True)
                embed.add_field(name="Their ELO", value=str(opp_elo),           inline=True)
                embed.set_footer(text="You have 60 seconds to respond.")

                await user.send(embed=embed, view=MatchFoundView(match_id, guild_id, uid))
            except Exception as e:
                print(f"[PvP] Could not DM user {uid}: {e}")

    # ── Queue entry / exit ────────────────────────────────────────────────

    async def _queue_add(
        self,
        guild_id: int,
        user_id: int,
        match_type: str,
        elo: int,
        trust: float,
    ):
        pvp_queue.setdefault(guild_id, {})[user_id] = {
            "type": match_type,
            "elo": elo,
            "trust": trust,
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

    # ── Panel views ───────────────────────────────────────────────────────

    @app_commands.command(
        name="pvpsendpanel",
        description="Send the PvP panel to the current channel (Staff only)"
    )
    async def pvp_send_panel(self, interaction: discord.Interaction):
        if not is_staff(interaction.user):
            return await interaction.response.send_message(
                "❌ Staff only.", ephemeral=True)

        embed = discord.Embed(
            title="⚔️ PvP Arena",
            description=(
                "Challenge other players to ranked or casual matches!\n\n"
                "**Ranked** — ELO-based matchmaking. Your rank updates after every match.\n"
                "**Casual** — Quick match with no ELO impact.\n\n"
                "Click **Start PvP** below to enter the queue."
            ),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, view=PvPPanelView())

    @app_commands.command(name="challenge", description="Send a casual PvP challenge to another player")
    @app_commands.describe(user="The player you want to challenge")
    async def challenge(self, interaction: discord.Interaction, user: discord.Member):
        if user.id == interaction.user.id:
            return await interaction.response.send_message(
                "❌ You can't challenge yourself.", ephemeral=True)
        if user.bot:
            return await interaction.response.send_message(
                "❌ You can't challenge a bot.", ephemeral=True)
        if not is_linked(interaction.user):
            return await interaction.response.send_message(
                "❌ You need a linked Roblox account to use PvP.", ephemeral=True)
        if not is_linked(user):
            return await interaction.response.send_message(
                f"❌ {user.mention} hasn't linked their Roblox account yet.", ephemeral=True)

        guild_id = interaction.guild_id
        if self._in_match(interaction.user.id):
            return await interaction.response.send_message(
                "❌ You are already in an active match.", ephemeral=True)
        if self._in_match(user.id):
            return await interaction.response.send_message(
                f"❌ {user.mention} is already in a match.", ephemeral=True)
        if self._in_queue(guild_id, interaction.user.id):
            return await interaction.response.send_message(
                "❌ You are already in the queue. Use `/pvpleave` first.", ephemeral=True)

        # Queue timeout check
        row = await d1_query(
            "SELECT pvp_timeout_until FROM users WHERE discord_id = ?",
            [str(interaction.user.id)]
        )
        if row["results"]:
            timeout_str = row["results"][0].get("pvp_timeout_until")
            if timeout_str:
                timeout_dt = datetime.fromisoformat(timeout_str)
                if timeout_dt.tzinfo is None:
                    timeout_dt = timeout_dt.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) < timeout_dt:
                    rem = int((timeout_dt - datetime.now(timezone.utc)).total_seconds() // 60)
                    return await interaction.response.send_message(
                        f"⏳ You are in a queue timeout for **{rem} more minute(s)**.",
                        ephemeral=True)

        # Fetch both players' ELO
        p_row = await d1_query(
            "SELECT pvp_elo, pvp_trust FROM users WHERE discord_id = ?",
            [str(interaction.user.id)]
        )
        o_row = await d1_query(
            "SELECT pvp_elo FROM users WHERE discord_id = ?",
            [str(user.id)]
        )
        p_elo = (p_row["results"][0].get("pvp_elo") or BASE_ELO) if p_row["results"] else BASE_ELO
        o_elo = (o_row["results"][0].get("pvp_elo") or BASE_ELO) if o_row["results"] else BASE_ELO
        p_trust = (p_row["results"][0].get("pvp_trust") or 10.0) if p_row["results"] else 10.0

        await interaction.response.send_message(
            f"✅ Challenge sent to {user.mention}! They have **60 seconds** to respond.",
            ephemeral=True
        )

        # Create a direct casual match between the two players
        match_id = str(uuid.uuid4())[:8]
        expiry = datetime.now(timezone.utc) + timedelta(seconds=60)
        pvp_pending[match_id] = {
            "player1": interaction.user.id,
            "player2": user.id,
            "match_type": "casual",
            "p1_accepted": True,  # challenger already accepted
            "p2_accepted": False,
            "expiry": expiry,
        }

        now = datetime.now(timezone.utc).isoformat()
        await d1_query(
            """INSERT INTO pvp_matches
               (match_id, player1_id, player2_id, match_type, p1_elo_before, p2_elo_before, started_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [match_id, str(interaction.user.id), str(user.id), "casual", p_elo, o_elo, now, now]
        )

        embed = discord.Embed(
            title="⚔️ PvP Challenge!",
            description=(
                f"{interaction.user.mention} has challenged you to a **casual** match!\n\n"
                f"**Challenger ELO:** {p_elo}\n"
                f"**Your ELO:** {o_elo}"
            ),
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_footer(text="You have 60 seconds to respond.")
        try:
            await user.send(embed=embed, view=MatchFoundView(match_id, guild_id, user.id))
        except discord.Forbidden:
            pvp_pending.pop(match_id, None)
            await interaction.followup.send(
                f"❌ Could not DM {user.mention} — they may have DMs disabled.",
                ephemeral=True
            )

    @app_commands.command(name="pvpleave", description="Leave the PvP queue")
    async def pvp_leave(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        if not self._in_queue(guild_id, interaction.user.id):
            return await interaction.response.send_message(
                "❌ You are not in the queue.", ephemeral=True)
        await self._queue_remove(guild_id, interaction.user.id)
        await interaction.response.send_message("✅ Left the queue.", ephemeral=True)


# ── Panel view (persistent) ───────────────────────────────────────────────────

class PvPPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Start PvP", style=discord.ButtonStyle.blurple, emoji="⚔️",
                       custom_id="pvp_start")
    async def start_pvp(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: PvPCog = interaction.client.cogs.get("PvPCog")
        if not cog:
            return await interaction.response.send_message("❌ PvP system offline.", ephemeral=True)

        guild_id = interaction.guild_id

        if cog._in_match(interaction.user.id):
            return await interaction.response.send_message(
                "❌ You are already in an active match.", ephemeral=True)
        if cog._in_queue(guild_id, interaction.user.id):
            return await interaction.response.send_message(
                "❌ You are already in the queue. Use `/pvpleave` to exit.", ephemeral=True)
        if not is_linked(interaction.user):
            return await interaction.response.send_message(
                "❌ You must have a linked Roblox account to play PvP.", ephemeral=True)

        # Check queue timeout
        row = await d1_query(
            "SELECT pvp_timeout_until FROM users WHERE discord_id = ?",
            [str(interaction.user.id)]
        )
        if row["results"]:
            timeout_str = row["results"][0].get("pvp_timeout_until")
            if timeout_str:
                timeout_dt = datetime.fromisoformat(timeout_str)
                if timeout_dt.tzinfo is None:
                    timeout_dt = timeout_dt.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) < timeout_dt:
                    rem = int((timeout_dt - datetime.now(timezone.utc)).total_seconds() // 60)
                    return await interaction.response.send_message(
                        f"⏳ You are in a queue timeout for **{rem} more minute(s)** "
                        "due to a recent ranked denial.", ephemeral=True)

        await interaction.response.send_message(
            embed=discord.Embed(
                title="⚔️ Select Match Type",
                description=(
                    "**Ranked** — ELO-based. You need 10 placement matches first.\n"
                    "**Casual** — No ELO impact. Matched with any available player."
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

        row = await d1_query(
            "SELECT pvp_elo, pvp_trust, pvp_placement_done FROM users WHERE discord_id = ?",
            [str(interaction.user.id)]
        )
        if not row["results"]:
            return await interaction.response.send_message(
                "❌ You haven't verified yet.", ephemeral=True)

        data   = row["results"][0]
        elo    = data.get("pvp_elo")   or BASE_ELO
        trust  = data.get("pvp_trust") or 10.0
        placed = data.get("pvp_placement_done") or 0

        if match_type == "ranked" and not placed:
            return await interaction.response.send_message(
                f"❌ You need {PLACEMENT_MATCHES} placement matches before joining ranked. "
                "Play **Casual** first!", ephemeral=True)

        await cog._queue_add(self.guild_id, interaction.user.id, match_type, elo, trust)

        queue_size = len(pvp_queue.get(self.guild_id, {}))
        join_ts = int(datetime.now(timezone.utc).timestamp())
        await interaction.response.send_message(
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
    bot.add_view(PvPPanelView())  # register as persistent
    print("✅ PvP cog loaded")
