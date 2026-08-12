import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import json
import uuid
from datetime import datetime, timezone, timedelta

# Import shared functions from bot.py
from bot import (
    d1_query,
    GUILD_ID,
    FOUNDER_ROLE_ID,
    HEAD_STAFF_ROLE_ID,
    MOD_ROLE_ID,
    TRIAL_MOD_ROLE_ID,
    HOSTER_ROLE_ID,
    ELITE_HOSTER_ROLE_ID,
    TOP_HOSTER_ROLE_ID,
    BLACKLIST_ROLE_ID,
    LINKED_ROLE_ID,
    PING_ROLE_ID,
    LOG_CHANNELS,
    is_hoster,
    is_elite_hoster,
    is_hoster_or_higher,
    is_staff,
    is_mod_or_higher,
    is_head_staff_or_founder,
    get_hoster_bonus,
    is_blacklisted,
    is_linked
)

# ── Import XP functions from levels.py ──────────────────────────────────────
try:
    from cogs.levels import award_xp as levels_award_xp
except ImportError:
    async def levels_award_xp(user_id, amount, member=None):
        return None

from cogs.hoster import update_top_hoster_roles

async def award_xp(user_id: int, amount: int, member: discord.Member = None) -> dict:
    try:
        return await levels_award_xp(user_id, amount, member)
    except Exception as e:
        print(f"Error in award_xp: {e}")
        return None


# ── Channels ──────────────────────────────────────────────────────────────────
HOST_CHANNEL_ID = 1535562445286801438
RAID_GATE_ID = 1535561934534082610

PING_ROLE_ID = 1535696574150090772

RAID_TIMEOUT_MINUTES = 120

active_hosts = set()
setup_messages = {}


def is_user_hosting(user_id: int) -> bool:
    return user_id in active_hosts


def add_hosting_user(user_id: int):
    active_hosts.add(user_id)


def remove_hosting_user(user_id: int):
    active_hosts.discard(user_id)


def get_xp_for_wave_sync(wave: int) -> int:
    return int(25 * (1.12 ** (wave - 1)))


def get_total_xp_for_raid_sync(final_wave: int) -> int:
    total = 0
    for wave in range(1, final_wave + 1):
        total += get_xp_for_wave_sync(wave)
    return total


async def send_log(bot: commands.Bot, embed: discord.Embed):
    channel = bot.get_channel(LOG_CHANNELS.get("raids", 0))
    if channel:
        try:
            await channel.send(embed=embed)
        except Exception as e:
            print(f"Error sending log: {e}")


async def send_raid_log(bot: commands.Bot, embed: discord.Embed, view=None):
    channel = bot.get_channel(LOG_CHANNELS.get("raids", 0))
    if channel:
        try:
            if view:
                await channel.send(embed=embed, view=view)
            else:
                await channel.send(embed=embed)
        except Exception as e:
            print(f"Error sending raid log: {e}")


raids = {}
raid_timeout_tasks = {}


def get_raid(guild_id: int) -> dict:
    return raids.get(guild_id)


def raid_exists(guild_id: int) -> bool:
    return guild_id in raids


async def cleanup_setup_messages_except(guild_id: int, channel: discord.TextChannel, keep_message_id: int = None):
    row = await d1_query("SELECT value FROM bot_meta WHERE key = ?", [f"setup_msgs_{guild_id}"])
    stored = json.loads(row["results"][0]["value"]) if row["results"] else []
    mem = setup_messages.get(guild_id, [])
    all_ids = list(set(stored + mem))
    remaining = []
    for msg_id in all_ids:
        if keep_message_id and msg_id == keep_message_id:
            remaining.append(msg_id)
            continue
        try:
            msg = await channel.fetch_message(msg_id)
            await msg.delete()
        except Exception:
            pass
    setup_messages[guild_id] = remaining
    await d1_query(
        "INSERT OR REPLACE INTO bot_meta (key, value) VALUES (?, ?)",
        [f"setup_msgs_{guild_id}", json.dumps(remaining)]
    )


async def track_setup_message(guild_id: int, message_id: int):
    if guild_id not in setup_messages:
        setup_messages[guild_id] = []
    setup_messages[guild_id].append(message_id)
    await d1_query(
        "INSERT OR REPLACE INTO bot_meta (key, value) VALUES (?, ?)",
        [f"setup_msgs_{guild_id}", json.dumps(setup_messages[guild_id])]
    )


async def get_active_raid_from_db(guild_id: int) -> dict:
    try:
        result = await d1_query(
            """SELECT raid_id, host_discord_ids, joined_users, code_used, wave_reached, time_started
               FROM raids
               WHERE is_completed = 0
               ORDER BY created_at DESC
               LIMIT 1""",
            []
        )
        if not result["results"]:
            return None
        row = result["results"][0]
        try:
            host_ids = json.loads(row["host_discord_ids"] or "[]")
            player_ids = json.loads(row["joined_users"] or "[]")
        except:
            host_ids = []
            player_ids = []
        players = []
        for pid in player_ids:
            user_result = await d1_query(
                "SELECT roblox_users FROM users WHERE discord_id = ?",
                [str(pid)]
            )
            if user_result["results"]:
                try:
                    roblox_users = json.loads(user_result["results"][0]["roblox_users"] or "[]")
                    if roblox_users:
                        players.append(roblox_users[0])
                    else:
                        players.append(f"<@{pid}>")
                except:
                    players.append(f"<@{pid}>")
            else:
                players.append(f"<@{pid}>")
        return {
            "raid_id": row["raid_id"],
            "host_id": host_ids[0] if host_ids else None,
            "hosters": host_ids,
            "players": players,
            "player_ids": player_ids,
            "code": row["code_used"],
            "wave": row["wave_reached"] or 1,
            "start_time": datetime.fromisoformat(row["time_started"]),
            "confirmed": True,
            "is_active": True,
            "gate_message_id": None,
            "control_msg_id": None,
            "last_activity": datetime.now(timezone.utc),
        }
    except Exception as e:
        print(f"Error getting active raid from database: {e}")
        return None


async def is_raid_active_in_db(guild_id: int) -> bool:
    try:
        result = await d1_query(
            "SELECT COUNT(*) as count FROM raids WHERE is_completed = 0",
            []
        )
        if result["results"]:
            return result["results"][0]["count"] > 0
        return False
    except Exception as e:
        print(f"Error checking active raid in database: {e}")
        return False


async def build_gate_embed(guild: discord.Guild, raid: dict) -> discord.Embed:
    hosters = []
    for hid in raid.get("hosters", []):
        m = guild.get_member(hid)
        hosters.append(m.mention if m else f"<@{hid}>")
    cap = 16 - len(raid.get("hosters", []))
    players = raid.get("players", [])
    player_count = len(players)
    embed = discord.Embed(
        title="🟢 LIVE RAID STATUS | ACTIVE",
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="📣 THERE IS AN ONGOING RAID", value="*Read The Info Below To Join!*", inline=False)
    embed.add_field(name="👑 Host", value=" | ".join(hosters) if hosters else "N/A", inline=False)
    embed.add_field(name="🌊 Current Wave", value=f"{raid.get('wave', 1)}/20", inline=True)
    embed.add_field(name="👥 Players Inside", value=f"{player_count}/{cap}", inline=True)
    embed.set_footer(text="Click Join Raid to get the code!")
    return embed


async def save_raid_to_history(guild_id: int, raid_data: dict, final_wave: int,
                               flagged: bool, points_awarded: int, xp_awarded: int):
    try:
        raid_id = raid_data.get("raid_id", str(uuid.uuid4())[:8])
        now = datetime.now(timezone.utc).isoformat()
        hoster_ids = json.dumps(raid_data.get("hosters", []))
        player_ids = json.dumps(raid_data.get("player_ids", []))
        existing = await d1_query(
            "SELECT raid_id FROM raids WHERE raid_id = ?",
            [raid_id]
        )
        if existing["results"]:
            await d1_query(
                """UPDATE raids SET
                    host_discord_ids = ?, joined_users = ?, is_completed = 1,
                    wave_reached = ?, flagged = ?, created_at = ?
                WHERE raid_id = ?""",
                [hoster_ids, player_ids, final_wave, 1 if flagged else 0, now, raid_id]
            )
        else:
            await d1_query(
                """INSERT INTO raids
                (raid_id, host_discord_ids, host_roblox_names, is_completed,
                 time_started, code_used, flagged, joined_users, wave_reached, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    raid_id,
                    hoster_ids,
                    "[]",
                    1,
                    raid_data["start_time"].isoformat(),
                    raid_data.get("code", ""),
                    1 if flagged else 0,
                    player_ids,
                    final_wave,
                    now
                ]
            )
        return raid_id
    except Exception as e:
        print(f"Error saving raid history: {e}")
        return None


async def add_hoster_points(user_id: int, amount: int) -> dict:
    try:
        now = datetime.now(timezone.utc).isoformat()
        result = await d1_query(
            "SELECT hoster_points, weekly_points, total_points_earned FROM users WHERE discord_id = ?",
            [str(user_id)]
        )
        if not result["results"]:
            return None
        current_lifetime = result["results"][0]["hoster_points"] or 0
        current_weekly = result["results"][0]["weekly_points"] or 0
        current_total_earned = result["results"][0]["total_points_earned"] or 0
        new_lifetime = current_lifetime + amount
        new_weekly = current_weekly + amount
        new_total_earned = current_total_earned + amount
        await d1_query(
            "UPDATE users SET hoster_points = ?, weekly_points = ?, total_points_earned = ?, updated_at = ? WHERE discord_id = ?",
            [new_lifetime, new_weekly, new_total_earned, now, str(user_id)]
        )
        return {
            "old_lifetime": current_lifetime,
            "new_lifetime": new_lifetime,
            "old_weekly": current_weekly,
            "new_weekly": new_weekly,
            "old_total_earned": current_total_earned,
            "new_total_earned": new_total_earned,
            "points_added": amount,
        }
    except Exception as e:
        print(f"Error adding hoster points: {e}")
        return None


async def auto_end_raid(guild_id: int, bot: commands.Bot):
    raid = get_raid(guild_id)
    if not raid:
        return
    guild = bot.get_guild(guild_id)
    if not guild:
        return
    host_ch = guild.get_channel(HOST_CHANNEL_ID)
    gate_ch = guild.get_channel(RAID_GATE_ID)
    if host_ch:
        embed = discord.Embed(
            title="⏰ Raid Auto-Ended",
            description=f"The raid has been automatically ended after {RAID_TIMEOUT_MINUTES} minutes of inactivity.",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="Final Wave", value=f"{raid.get('wave', 1)}/20", inline=True)
        embed.add_field(name="Hosts", value=" | ".join([f"<@{h}>" for h in raid["hosters"]]), inline=False)
        await host_ch.send(embed=embed)
    await end_raid_logic(guild, raid, raid.get('wave', 1), flagged=False, bot=bot)
    if guild_id in raid_timeout_tasks:
        raid_timeout_tasks[guild_id].cancel()
        del raid_timeout_tasks[guild_id]


async def end_raid_logic(guild: discord.Guild, raid: dict, final_wave: int, flagged: bool, bot: commands.Bot):
    now = datetime.now(timezone.utc)
    duration = (now - raid["start_time"]).total_seconds()
    mins_per_wave = duration / 60 / max(final_wave, 1)
    points_each = final_wave
    xp_total = get_total_xp_for_raid_sync(final_wave)
    xp_each = xp_total
    hoster_lines = []
    xp_lines = []
    level_ups = []
    if not flagged:
        for hid in raid["hosters"]:
            point_result = await add_hoster_points(hid, points_each)
            if point_result:
                hoster_lines.append(f"<@{hid}> → +**{points_each}** pts")
            member = guild.get_member(hid)
            xp_result = await award_xp(hid, xp_each, member)
            if xp_result:
                bonus_tag = " ✨(+25%)" if xp_result.get("bonus_applied", False) else ""
                xp_lines.append(f"<@{hid}> → +**{xp_each}** XP{bonus_tag}")
                if xp_result.get("leveled_up", False):
                    level_ups.append(f"<@{hid}> → Level **{xp_result['new_level']}**! 🎉")
        th_cog = bot.cogs.get("TrialHoster")
        if th_cog:
            for hid in raid["hosters"]:
                await th_cog.check_user_promotion(guild, hid)
    raid_id = await save_raid_to_history(
        guild.id, raid, final_wave, flagged,
        points_each if not flagged else None,
        xp_each if not flagged else None
    )
    for uid in raid["hosters"] + raid["player_ids"]:
        try:
            m = guild.get_member(uid)
            if m:
                if uid in raid["hosters"] and not flagged:
                    is_elite = is_elite_hoster(m)
                    bonus_tag = " (+25% XP bonus!)" if is_elite else ""
                    await m.send(
                        f"✅ **Raid Hub** — The raid has ended at wave **{final_wave}/20**.\n"
                        f"🏆 You earned **{xp_each}** XP{bonus_tag} and **{points_each}** hoster points!"
                    )
                else:
                    await m.send(
                        f"✅ **Raid Hub** — The raid has ended at wave **{final_wave}/20**. Thanks for joining!"
                    )
        except Exception:
            pass
    gate_ch = guild.get_channel(RAID_GATE_ID)
    if gate_ch:
        await gate_ch.purge(limit=100)
        finished_embed = discord.Embed(
            title="🏁 Raid Finished" + (" ⏰ Auto-Ended" if not flagged else ""),
            description=f"The raid has ended at wave **{final_wave}/20**.\nThanks to all hosters and participants!",
            color=discord.Color.greyple(),
            timestamp=now
        )
        hosters_mentions = " | ".join([f"<@{h}>" for h in raid["hosters"]])
        finished_embed.add_field(name="Hosters", value=hosters_mentions, inline=False)
        player_count = len(raid.get("player_ids", []))
        finished_embed.add_field(name="👥 Players Joined", value=f"**{player_count}** players", inline=True)
        duration_minutes = int(duration // 60)
        duration_seconds = int(duration % 60)
        finished_embed.add_field(
            name="⏱️ Duration",
            value=f"**{duration_minutes}m {duration_seconds}s**",
            inline=True
        )
        finished_embed.add_field(name="🌊 Final Wave", value=f"**{final_wave}/20**", inline=True)
        if not flagged:
            finished_embed.add_field(name="⭐ Points Awarded", value=f"**{points_each}** pts each", inline=True)
            finished_embed.add_field(name="✨ XP Awarded", value=f"**{xp_each}** XP each", inline=True)
            if level_ups:
                finished_embed.add_field(name="🎉 Level Ups!", value="\n".join(level_ups), inline=False)
        else:
            finished_embed.add_field(
                name="⚠️ Flagged - Pending Review",
                value=f"Avg **{mins_per_wave:.2f}** min/wave — under 1.0 min threshold.\nCheck the raid log channel.",
                inline=False
            )
        if raid_id:
            finished_embed.set_footer(text=f"Raid ID: {raid_id}")
        await gate_ch.send(embed=finished_embed)
    if raid.get("control_msg_id"):
        try:
            host_ch = guild.get_channel(HOST_CHANNEL_ID)
            if host_ch:
                msg = await host_ch.fetch_message(raid["control_msg_id"])
                await msg.delete()
        except Exception as e:
            print(f"Failed to delete control panel: {e}")
    await update_top_hoster_roles(guild, bot)
    for hid in raid["hosters"]:
        remove_hosting_user(hid)
    if guild.id in raids:
        del raids[guild.id]
    if guild.id in raid_timeout_tasks:
        raid_timeout_tasks[guild.id].cancel()
        del raid_timeout_tasks[guild.id]
    log_embed = discord.Embed(
        title="🏁 Raid Ended" + (" ⚠️ FLAGGED" if flagged else ""),
        color=discord.Color.orange() if flagged else discord.Color.greyple(),
        timestamp=now
    )
    log_embed.add_field(name="Raid ID", value=f"`{raid_id or 'N/A'}`", inline=True)
    log_embed.add_field(name="Host", value=f"<@{raid['host_id']}>", inline=True)
    log_embed.add_field(name="Final Wave", value=f"{final_wave}/20", inline=True)
    log_embed.add_field(name="Duration", value=f"{int(duration // 60)}m {int(duration % 60)}s", inline=True)
    log_embed.add_field(name="Players", value=f"{len(raid['players'])} joined", inline=True)
    log_embed.add_field(name="Avg Min/Wave", value=f"{mins_per_wave:.2f}", inline=True)
    log_embed.add_field(name="Hosters", value=" | ".join([f"<@{h}>" for h in raid["hosters"]]), inline=False)
    if not flagged:
        log_embed.add_field(
            name="Points & XP Awarded",
            value=f"**{points_each}** pts each\n**{xp_each}** XP each",
            inline=True
        )
        if level_ups:
            log_embed.add_field(name="🎉 Level Ups", value="\n".join(level_ups), inline=False)
    else:
        log_embed.add_field(
            name="⚠️ Flagged - Pending Review",
            value=f"Avg **{mins_per_wave:.2f}** min/wave — under 1.0 min threshold.\nApprove or deny below.",
            inline=False
        )
    duration_str = f"{int(duration // 60)}m {int(duration % 60)}s"
    if flagged:
        await d1_query(
            "INSERT OR REPLACE INTO flagged_raids (raid_id, guild_id, hosters, points_each, xp_each, final_wave, duration, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [raid_id, guild.id, json.dumps(list(raid["hosters"])), points_each, xp_each, final_wave, duration_str, datetime.now(timezone.utc).isoformat()]
        )
        approval_view = FlaggedApprovalView(
            hosters=list(raid["hosters"]),
            points_each=points_each,
            final_wave=final_wave,
            guild_id=guild.id,
            raid_id=raid_id,
            xp_total=xp_each,
            duration=duration_str
        )
        await send_raid_log(bot, log_embed, view=approval_view)
    else:
        await send_raid_log(bot, log_embed)


def schedule_raid_timeout(guild_id: int, bot: commands.Bot):
    if guild_id in raid_timeout_tasks:
        raid_timeout_tasks[guild_id].cancel()
        del raid_timeout_tasks[guild_id]
    task = asyncio.create_task(delayed_raid_timeout(guild_id, bot))
    raid_timeout_tasks[guild_id] = task


async def delayed_raid_timeout(guild_id: int, bot: commands.Bot):
    await asyncio.sleep(RAID_TIMEOUT_MINUTES * 60)
    raid = get_raid(guild_id)
    if not raid:
        return
    last_activity = raid.get("last_activity", raid["start_time"])
    time_since_activity = (datetime.now(timezone.utc) - last_activity).total_seconds()
    if time_since_activity >= RAID_TIMEOUT_MINUTES * 60:
        await auto_end_raid(guild_id, bot)


def update_raid_activity(guild_id: int):
    raid = get_raid(guild_id)
    if raid:
        raid["last_activity"] = datetime.now(timezone.utc)


# ── Views ────────────────────────────────────────────────────────────────────
class FlaggedApprovalView(discord.ui.View):
    def __init__(self, hosters=None, points_each=None, final_wave=None,
                 guild_id=None, raid_id=None, xp_total=None, duration=None):
        super().__init__(timeout=None)
        self.hosters = hosters
        self.points_each = points_each
        self.final_wave = final_wave
        self.guild_id = guild_id
        self.raid_id = raid_id
        self.xp_total = xp_total
        self.duration = duration

    async def _load_from_db(self, raid_id: str):
        if self.hosters is not None:
            return True
        row = await d1_query(
            "SELECT * FROM flagged_raids WHERE raid_id = ?",
            [raid_id]
        )
        if not row["results"]:
            return False
        r = row["results"][0]
        self.hosters = json.loads(r["hosters"])
        self.points_each = r["points_each"]
        self.final_wave = r["final_wave"]
        self.guild_id = r["guild_id"]
        self.raid_id = r["raid_id"]
        self.xp_total = r["xp_each"]
        self.duration = r["duration"]
        return True

    @discord.ui.button(label="Approve Points & XP", style=discord.ButtonStyle.success, emoji="✅", custom_id="flag_approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed = {HEAD_STAFF_ROLE_ID, FOUNDER_ROLE_ID}
        if not any(r.id in allowed for r in interaction.user.roles):
            try:
                await interaction.response.send_message("❌ You do not have permission.", ephemeral=True)
            except discord.HTTPException:
                pass
            return
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            return
        raid_id = interaction.message.embeds[0].fields[-1].value.strip("`") if self.raid_id is None else self.raid_id
        if not await self._load_from_db(raid_id or self.raid_id):
            return await interaction.followup.send("❌ Flagged raid data not found in DB. It may have already been processed.", ephemeral=True)
        hoster_lines = []
        xp_lines = []
        level_ups = []
        for hid in self.hosters:
            point_result = await add_hoster_points(hid, self.points_each)
            if point_result:
                hoster_lines.append(f"<@{hid}> → +**{self.points_each}** pts")
            member = interaction.guild.get_member(hid)
            xp_result = await award_xp(hid, self.xp_total, member)
            if xp_result:
                bonus_tag = " ✨(+25%)" if xp_result.get("bonus_applied", False) else ""
                xp_lines.append(f"<@{hid}> → +**{self.xp_total}** XP{bonus_tag}")
                if xp_result.get("leveled_up", False):
                    level_ups.append(f"<@{hid}> → Level **{xp_result['new_level']}**! 🎉")
        guild = interaction.guild
        th_cog = interaction.client.cogs.get("TrialHoster")
        if th_cog:
            for hid in self.hosters:
                await th_cog.check_user_promotion(guild, hid)
        await update_top_hoster_roles(guild, interaction.client)
        embed = discord.Embed(
            title="✅ Flagged Raid Approved",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="Approved By", value=interaction.user.mention, inline=False)
        embed.add_field(name="Points Awarded", value="\n".join(hoster_lines) or "None", inline=False)
        embed.add_field(name="XP Awarded", value="\n".join(xp_lines) or "None", inline=False)
        if level_ups:
            embed.add_field(name="🎉 Level Ups!", value="\n".join(level_ups), inline=False)
        embed.add_field(name="Wave", value=f"{self.final_wave}/20", inline=True)
        embed.add_field(name="Raid ID", value=f"`{self.raid_id}`", inline=True)
        embed.add_field(name="Duration", value=self.duration, inline=True)
        self.stop()
        await interaction.message.edit(embed=embed, view=None)
        await interaction.followup.send("✅ Raid approved and points awarded.", ephemeral=True)
        await d1_query("DELETE FROM flagged_raids WHERE raid_id = ?", [self.raid_id])
        log_embed = discord.Embed(
            title="✅ Flagged Raid Approved",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc)
        )
        log_embed.add_field(name="Raid ID", value=f"`{self.raid_id}`", inline=True)
        log_embed.add_field(name="Approved By", value=interaction.user.mention, inline=True)
        log_embed.add_field(name="Wave", value=f"{self.final_wave}/20", inline=True)
        log_embed.add_field(name="Points Each", value=str(self.points_each), inline=True)
        log_embed.add_field(name="XP Each", value=str(self.xp_total), inline=True)
        log_embed.add_field(name="Duration", value=self.duration, inline=True)
        await send_raid_log(interaction.client, log_embed)

    @discord.ui.button(label="Deny Points & XP", style=discord.ButtonStyle.danger, emoji="🚫", custom_id="flag_deny")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed = {HEAD_STAFF_ROLE_ID, FOUNDER_ROLE_ID}
        if not any(r.id in allowed for r in interaction.user.roles):
            try:
                await interaction.response.send_message("❌ You do not have permission.", ephemeral=True)
            except discord.HTTPException:
                pass
            return
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            return
        raid_id = interaction.message.embeds[0].fields[-1].value.strip("`") if self.raid_id is None else self.raid_id
        if not await self._load_from_db(raid_id or self.raid_id):
            return await interaction.followup.send("❌ Flagged raid data not found in DB. It may have already been processed.", ephemeral=True)
        embed = discord.Embed(
            title="🚫 Flagged Raid Denied",
            description="Points and XP were **not** awarded due to suspicious wave speed.",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="Denied By", value=interaction.user.mention, inline=False)
        embed.add_field(name="Hosters", value=" | ".join([f"<@{h}>" for h in self.hosters]), inline=False)
        embed.add_field(name="Raid ID", value=f"`{self.raid_id}`", inline=True)
        embed.add_field(name="Wave", value=f"{self.final_wave}/20", inline=True)
        embed.add_field(name="Duration", value=self.duration, inline=True)
        self.stop()
        await interaction.message.edit(embed=embed, view=None)
        await interaction.followup.send("❌ Raid denied. No points awarded.", ephemeral=True)
        await d1_query("DELETE FROM flagged_raids WHERE raid_id = ?", [self.raid_id])
        log_embed = discord.Embed(
            title="🚫 Flagged Raid Denied",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc)
        )
        log_embed.add_field(name="Raid ID", value=f"`{self.raid_id}`", inline=True)
        log_embed.add_field(name="Denied By", value=interaction.user.mention, inline=True)
        log_embed.add_field(name="Wave", value=f"{self.final_wave}/20", inline=True)
        log_embed.add_field(name="Duration", value=self.duration, inline=True)
        log_embed.add_field(name="Reason", value="Suspicious wave speed (under 1.0 min/wave)", inline=True)
        await send_raid_log(interaction.client, log_embed)


# ── Modals ────────────────────────────────────────────────────────────────────
class StartRaidModal(discord.ui.Modal, title="Start Raid"):
    code = discord.ui.TextInput(
        label="Your PS Code",
        placeholder="Enter your PS code...",
        max_length=50
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        code = self.code.value.strip().upper()
        guild_id = interaction.guild_id
        host_channel = interaction.channel

        if is_user_hosting(interaction.user.id):
            return await interaction.followup.send(
                "❌ You are already hosting a raid in another server! Finish that raid first.",
                ephemeral=True
            )
        if raid_exists(guild_id):
            return await interaction.followup.send(
                "❌ A raid is already active in this server.",
                ephemeral=True
            )
        user_row = await d1_query(
            "SELECT ps_codes FROM users WHERE discord_id = ?",
            [str(interaction.user.id)]
        )
        if not user_row["results"]:
            return await interaction.followup.send(
                "❌ You are not verified.",
                ephemeral=True
            )
        user_codes = json.loads(user_row["results"][0]["ps_codes"] or "[]")
        if code not in user_codes:
            return await interaction.followup.send(
                "❌ That code does not belong to you.",
                ephemeral=True
            )
        await cleanup_setup_messages_except(guild_id, host_channel)
        embed = discord.Embed(
            title="⚔️ Raid Setup",
            color=discord.Color.blurple(),
            description=f"{interaction.user.mention} has started raid setup!",
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="Status", value="⏳ PENDING...", inline=True)
        embed.add_field(name="Hosters", value=f"✅ {interaction.user.mention} (Host)", inline=False)
        embed.set_footer(text="Live status updates automatically.")
        view = RaidSetupView(host_id=interaction.user.id, code=code, host_mention=interaction.user.mention)
        msg = await host_channel.send(embed=embed, view=view)
        view.setup_msg = msg
        await track_setup_message(guild_id, msg.id)
        await interaction.followup.send("✅ Raid setup panel created! Check the channel.", ephemeral=True)
        gate_ch = interaction.guild.get_channel(RAID_GATE_ID)
        if gate_ch:
            ping_role = interaction.guild.get_role(PING_ROLE_ID)
            ping_msg = ping_role.mention if ping_role else ""
            announce_embed = discord.Embed(
                title="🔔 Raid Setup Started",
                description=f"{interaction.user.mention} is setting up a raid! Stay tuned...",
                color=discord.Color.orange(),
                timestamp=datetime.now(timezone.utc)
            )
            await gate_ch.send(content=ping_msg, embed=announce_embed)


class WaveEndModal(discord.ui.Modal, title="End Raid"):
    wave = discord.ui.TextInput(
        label="Final Wave Reached (1-20)",
        placeholder="e.g. 20",
        max_length=2
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        raid = get_raid(guild_id)
        if not raid:
            return await interaction.followup.send("❌ No active raid.", ephemeral=True)
        if interaction.user.id not in raid["hosters"]:
            return await interaction.followup.send(
                "❌ Only hosters can end the raid.",
                ephemeral=True
            )
        try:
            final_wave = int(self.wave.value.strip())
            if not 1 <= final_wave <= 20:
                raise ValueError
        except ValueError:
            return await interaction.followup.send(
                "❌ Please enter a number between 1 and 20.",
                ephemeral=True
            )
        update_raid_activity(guild_id)
        await end_raid(interaction, final_wave)


class UpdateWaveModal(discord.ui.Modal, title="Update Wave"):
    wave = discord.ui.TextInput(
        label="Current Wave (1-20)",
        placeholder="e.g. 10",
        max_length=2
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        raid = get_raid(guild_id)
        if not raid:
            return await interaction.followup.send("❌ No active raid.", ephemeral=True)
        if interaction.user.id not in raid["hosters"]:
            return await interaction.followup.send(
                "❌ Only hosters can update the wave.",
                ephemeral=True
            )
        try:
            w = int(self.wave.value.strip())
            if not 1 <= w <= 20:
                raise ValueError
        except ValueError:
            return await interaction.followup.send(
                "❌ Please enter a number between 1 and 20.",
                ephemeral=True
            )
        raid["wave"] = w
        update_raid_activity(guild_id)
        gate_ch = interaction.guild.get_channel(RAID_GATE_ID)
        if gate_ch and raid.get("gate_message_id"):
            try:
                msg = await gate_ch.fetch_message(raid["gate_message_id"])
                embed = await build_gate_embed(interaction.guild, raid)
                await msg.edit(embed=embed)
            except Exception:
                pass
        host_ch = interaction.guild.get_channel(HOST_CHANNEL_ID)
        if host_ch and raid.get("control_msg_id"):
            try:
                ctrl_msg = await host_ch.fetch_message(raid["control_msg_id"])
                await ctrl_msg.edit(embed=build_control_embed(raid))
            except Exception:
                pass
        await interaction.followup.send(f"✅ Wave updated to **{w}/20**.", ephemeral=True)


class JoinRaidModal(discord.ui.Modal, title="Join Raid"):
    roblox_name = discord.ui.TextInput(
        label="Roblox Username",
        placeholder="Enter the username you're joining on..."
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        raid = get_raid(guild_id)
        if not raid:
            raid = await get_active_raid_from_db(guild_id)
            if raid:
                raids[guild_id] = raid
        if not raid:
            return await interaction.followup.send("❌ No active raid.", ephemeral=True)
        member = interaction.user
        roblox = self.roblox_name.value.strip()
        if is_blacklisted(member):
            return await interaction.followup.send("🚫 You are blacklisted.", ephemeral=True)
        user_row = await d1_query(
            "SELECT roblox_users FROM users WHERE discord_id = ?",
            [str(member.id)]
        )
        if not user_row["results"]:
            return await interaction.followup.send(
                "❌ You are not verified.",
                ephemeral=True
            )
        linked_accounts = json.loads(user_row["results"][0]["roblox_users"] or "[]")
        if roblox.lower() not in [a.lower() for a in linked_accounts]:
            return await interaction.followup.send(
                f"❌ `{roblox}` is not linked to your account. Link it with `/verify` first.",
                ephemeral=True
            )
        cap = 16 - len(raid["hosters"])
        if len(raid["players"]) >= cap:
            return await interaction.followup.send("❌ The raid is full.", ephemeral=True)
        if member.id in raid["player_ids"]:
            return await interaction.followup.send(
                "⚠️ You are already in this raid.",
                ephemeral=True
            )
        raid["players"].append(roblox)
        raid["player_ids"].append(member.id)
        update_raid_activity(guild_id)
        gate_ch = interaction.guild.get_channel(RAID_GATE_ID)
        if gate_ch and raid.get("gate_message_id"):
            try:
                msg = await gate_ch.fetch_message(raid["gate_message_id"])
                embed = await build_gate_embed(interaction.guild, raid)
                await msg.edit(embed=embed)
            except Exception:
                pass
        host_ch = interaction.guild.get_channel(HOST_CHANNEL_ID)
        if host_ch and raid.get("control_msg_id"):
            try:
                ctrl_msg = await host_ch.fetch_message(raid["control_msg_id"])
                await ctrl_msg.edit(embed=build_control_embed(raid))
            except Exception:
                pass
        code_embed = discord.Embed(
            title="✅ Raid Code",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc)
        )
        code_embed.description = f"### `{raid['code']}`\n*Tap to copy*"
        code_embed.add_field(name="\u200b", value="Good luck in the raid! 🗡️", inline=False)
        await interaction.followup.send(embed=code_embed, ephemeral=True)


class InviteHosterModal(discord.ui.Modal, title="Invite Hoster"):
    user_id = discord.ui.TextInput(
        label="User ID to Invite",
        placeholder="Right-click user → Copy ID"
    )

    def __init__(self, setup_view=None):
        super().__init__()
        self.setup_view = setup_view

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        raid = get_raid(interaction.guild_id)
        if raid and raid.get("confirmed"):
            return await interaction.followup.send(
                "❌ Raid already confirmed, cannot invite more hosters.",
                ephemeral=True
            )
        try:
            uid = int(self.user_id.value.strip())
        except ValueError:
            return await interaction.followup.send("❌ Invalid user ID.", ephemeral=True)
        member = interaction.guild.get_member(uid)
        if not member:
            return await interaction.followup.send(
                "❌ User not found in this server.",
                ephemeral=True
            )
        if not is_hoster_or_higher(member):
            return await interaction.followup.send(
                f"❌ {member.mention} does not have the Hoster or Trial Hoster role.",
                ephemeral=True
            )
        if not is_linked(member):
            return await interaction.followup.send(
                f"❌ {member.mention} is not verified.",
                ephemeral=True
            )
        sv = self.setup_view
        if sv and uid in sv.pending_hosters:
            return await interaction.followup.send(
                f"⚠️ {member.mention} is already a hoster.",
                ephemeral=True
            )
        if sv:
            sv.pending_invites[uid] = True

        try:
            host_id = sv.host_id if sv else interaction.user.id
            dm_embed = discord.Embed(
                title="⚔️ Raid Hoster Invite",
                description=f"**<@{host_id}>** is inviting you to co-host a raid in **Raid Hub**.\n\nAccept or deny below.",
                color=discord.Color.blurple(),
                timestamp=datetime.now(timezone.utc)
            )
            await member.send(embed=dm_embed, view=HosterInviteView(
                host_id=host_id,
                invitee_id=uid,
                guild_id=interaction.guild_id,
                setup_view=sv
            ))
            await interaction.followup.send(
                f"✅ Invite sent to {member.mention}.",
                ephemeral=True
            )
            # Update the setup embed to show pending invite
            if sv:
                await sv.update_setup_embed(interaction)
        except discord.Forbidden:
            await interaction.followup.send(
                f"❌ Could not DM {member.mention}. They may have DMs closed.",
                ephemeral=True
            )


async def end_raid(interaction: discord.Interaction, final_wave: int):
    guild_id = interaction.guild_id
    raid = get_raid(guild_id)
    if not raid:
        return await interaction.followup.send("❌ No active raid.", ephemeral=True)
    for hid in raid["hosters"]:
        remove_hosting_user(hid)
    raid["wave"] = final_wave
    now = datetime.now(timezone.utc)
    duration = (now - raid["start_time"]).total_seconds()
    mins_per_wave = duration / 60 / max(final_wave, 1)
    flagged = mins_per_wave < 1.0
    points_each = final_wave
    xp_total = get_total_xp_for_raid_sync(final_wave)
    xp_each = xp_total
    hoster_lines = []
    xp_lines = []
    level_ups = []
    if not flagged:
        for hid in raid["hosters"]:
            point_result = await add_hoster_points(hid, points_each)
            if point_result:
                hoster_lines.append(f"<@{hid}> → +**{points_each}** pts")
            member = interaction.guild.get_member(hid)
            xp_result = await award_xp(hid, xp_each, member)
            if xp_result:
                bonus_tag = " ✨(+25%)" if xp_result.get("bonus_applied", False) else ""
                xp_lines.append(f"<@{hid}> → +**{xp_each}** XP{bonus_tag}")
                if xp_result.get("leveled_up", False):
                    level_ups.append(f"<@{hid}> → Level **{xp_result['new_level']}**! 🎉")
        th_cog = interaction.client.cogs.get("TrialHoster")
        if th_cog:
            for hid in raid["hosters"]:
                await th_cog.check_user_promotion(interaction.guild, hid)
    raid_id = await save_raid_to_history(
        guild_id, raid, final_wave, flagged,
        points_each if not flagged else None,
        xp_each if not flagged else None
    )
    for uid in raid["hosters"] + raid["player_ids"]:
        try:
            m = interaction.guild.get_member(uid)
            if m:
                if uid in raid["hosters"] and not flagged:
                    is_elite = is_elite_hoster(m)
                    bonus_tag = " (+25% XP bonus!)" if is_elite else ""
                    await m.send(
                        f"✅ **Raid Hub** — The raid has ended at wave **{final_wave}/20**.\n"
                        f"🏆 You earned **{xp_each}** XP{bonus_tag} and **{points_each}** hoster points!"
                    )
                else:
                    await m.send(
                        f"✅ **Raid Hub** — The raid has ended at wave **{final_wave}/20**. Thanks for joining!"
                    )
        except Exception:
            pass
    gate_ch = interaction.guild.get_channel(RAID_GATE_ID)
    if gate_ch:
        await gate_ch.purge(limit=100)
        finished_embed = discord.Embed(
            title="🏁 Raid Finished",
            description=f"The raid has ended at wave **{final_wave}/20**.\nThanks to all hosters and participants!",
            color=discord.Color.greyple(),
            timestamp=now
        )
        hosters_mentions = " | ".join([f"<@{h}>" for h in raid["hosters"]])
        finished_embed.add_field(name="Hosters", value=hosters_mentions, inline=False)
        player_count = len(raid.get("player_ids", []))
        finished_embed.add_field(name="👥 Players Joined", value=f"**{player_count}** players", inline=True)
        duration_minutes = int(duration // 60)
        duration_seconds = int(duration % 60)
        finished_embed.add_field(
            name="⏱️ Duration",
            value=f"**{duration_minutes}m {duration_seconds}s**",
            inline=True
        )
        finished_embed.add_field(name="🌊 Final Wave", value=f"**{final_wave}/20**", inline=True)
        if not flagged:
            finished_embed.add_field(name="⭐ Points Awarded", value=f"**{points_each}** pts each", inline=True)
            finished_embed.add_field(name="✨ XP Awarded", value=f"**{xp_each}** XP each", inline=True)
            if level_ups:
                finished_embed.add_field(name="🎉 Level Ups!", value="\n".join(level_ups), inline=False)
        else:
            finished_embed.add_field(
                name="⚠️ Flagged - Pending Review",
                value=f"Avg **{mins_per_wave:.2f}** min/wave — under 1.0 min threshold.\nCheck the raid log channel.",
                inline=False
            )
        if raid_id:
            finished_embed.set_footer(text=f"Raid ID: {raid_id}")
        await gate_ch.send(embed=finished_embed)
    if raid.get("control_msg_id"):
        try:
            host_ch = interaction.guild.get_channel(HOST_CHANNEL_ID)
            if host_ch:
                msg = await host_ch.fetch_message(raid["control_msg_id"])
                await msg.delete()
        except Exception as e:
            print(f"Failed to delete control panel: {e}")
    await update_top_hoster_roles(interaction.guild, interaction.client)
    duration_str = f"{int(duration // 60)}m {int(duration % 60)}s"
    log_embed = discord.Embed(
        title="🏁 Raid Ended" + (" ⚠️ FLAGGED" if flagged else ""),
        color=discord.Color.orange() if flagged else discord.Color.greyple(),
        timestamp=now
    )
    log_embed.add_field(name="Raid ID", value=f"`{raid_id or 'N/A'}`", inline=True)
    log_embed.add_field(name="Host", value=f"<@{raid['host_id']}>", inline=True)
    log_embed.add_field(name="Final Wave", value=f"{final_wave}/20", inline=True)
    log_embed.add_field(name="Duration", value=duration_str, inline=True)
    log_embed.add_field(name="Players", value=f"{len(raid['players'])} joined", inline=True)
    log_embed.add_field(name="Avg Min/Wave", value=f"{mins_per_wave:.2f}", inline=True)
    log_embed.add_field(name="Hosters", value=" | ".join([f"<@{h}>" for h in raid["hosters"]]), inline=False)
    if not flagged:
        log_embed.add_field(
            name="Points & XP Awarded",
            value=f"**{points_each}** pts each\n**{xp_each}** XP each",
            inline=True
        )
        if level_ups:
            log_embed.add_field(name="🎉 Level Ups", value="\n".join(level_ups), inline=False)
    else:
        log_embed.add_field(
            name="⚠️ Flagged - Pending Review",
            value=f"Avg **{mins_per_wave:.2f}** min/wave — under 1.0 min threshold.\nApprove or deny below.",
            inline=False
        )
    if flagged:
        await d1_query(
            "INSERT OR REPLACE INTO flagged_raids (raid_id, guild_id, hosters, points_each, xp_each, final_wave, duration, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [raid_id, guild_id, json.dumps(list(raid["hosters"])), points_each, xp_each, final_wave, duration_str, datetime.now(timezone.utc).isoformat()]
        )
        approval_view = FlaggedApprovalView(
            hosters=list(raid["hosters"]),
            points_each=points_each,
            final_wave=final_wave,
            guild_id=guild_id,
            raid_id=raid_id,
            xp_total=xp_each,
            duration=duration_str
        )
        await send_raid_log(interaction.client, log_embed, view=approval_view)
        main_log_embed = discord.Embed(
            title="⚠️ Raid Flagged for Review",
            description=f"A raid has been flagged for suspicious speed.",
            color=discord.Color.orange(),
            timestamp=now
        )
        main_log_embed.add_field(name="Raid ID", value=f"`{raid_id}`", inline=True)
        main_log_embed.add_field(name="Host", value=f"<@{raid['host_id']}>", inline=True)
        main_log_embed.add_field(name="Final Wave", value=f"{final_wave}/20", inline=True)
        main_log_embed.add_field(name="Duration", value=duration_str, inline=True)
        main_log_embed.add_field(name="Avg Min/Wave", value=f"{mins_per_wave:.2f}", inline=True)
        main_log_embed.add_field(name="Review", value="Check the raid log channel to approve or deny.", inline=False)
        await send_log(interaction.client, main_log_embed)
    else:
        await send_raid_log(interaction.client, log_embed)
    host_ch = interaction.guild.get_channel(HOST_CHANNEL_ID)
    if host_ch:
        await cleanup_setup_messages_except(guild_id, host_ch)
    del raids[guild_id]
    msg = f"✅ Raid ended at wave **{final_wave}**."
    if flagged:
        msg += "\n\n⚠️ **This raid has been flagged** — points and XP are held pending staff review in the raid log channel."
    else:
        msg += f"\n**{points_each}** pts and **{xp_each}** XP awarded to each hoster."
    await interaction.followup.send(msg, ephemeral=True)


def build_control_embed(raid: dict) -> discord.Embed:
    ctrl_embed = discord.Embed(
        title="⚔️ Raid Controls",
        description="Raid is **ACTIVE**. Use the buttons below to manage it.",
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc),
    )
    ctrl_embed.add_field(name="Raid ID", value=f"`{raid['raid_id']}`", inline=True)
    ctrl_embed.add_field(name="Host", value=f"<@{raid['host_id']}>", inline=True)
    ctrl_embed.add_field(name="Wave", value=f"{raid['wave']}/20", inline=True)
    ctrl_embed.add_field(name="Players", value=f"{len(raid.get('player_ids', []))}", inline=True)
    ctrl_embed.add_field(name="Auto-Timeout", value=f"{RAID_TIMEOUT_MINUTES} minutes", inline=True)
    return ctrl_embed


class RaidSetupView(discord.ui.View):
    def __init__(self, host_id, code, host_mention):
        super().__init__(timeout=300)
        self.host_id = host_id
        self.code = code
        self.host_mention = host_mention
        self.pending_invites = {}
        self.pending_hosters = [host_id]
        self.message_id = None
        self.setup_msg = None

    async def update_setup_embed(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="⚔️ Raid Setup",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.description = f"{self.host_mention} has started raid setup!"
        embed.add_field(name="Status", value="⏳ PENDING...", inline=True)
        hoster_lines = []
        for hid in self.pending_hosters:
            if hid == self.host_id:
                hoster_lines.append(f"✅ <@{hid}> (Host)")
            else:
                hoster_lines.append(f"⏳ <@{hid}> (Pending)")
        embed.add_field(name="Hosters", value="\n".join(hoster_lines) if hoster_lines else "None", inline=False)
        pending_count = len(self.pending_invites)
        embed.add_field(name="📨 Pending Invites", value=str(pending_count), inline=True)
        embed.set_footer(text="Live status updates automatically.")
        if self.setup_msg:
            try:
                await self.setup_msg.edit(embed=embed)
            except Exception as e:
                print(f"Failed to edit setup embed: {e}")
        else:
            await interaction.message.edit(embed=embed)

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.host_id:
            return await interaction.response.send_message(
                "❌ Only the host can confirm.",
                ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        if is_user_hosting(self.host_id):
            return await interaction.followup.send(
                "❌ You are already hosting a raid in another server!",
                ephemeral=True
            )
        if raid_exists(guild_id):
            return await interaction.followup.send(
                "❌ A raid is already active in this server.",
                ephemeral=True
            )
        add_hosting_user(self.host_id)
        now = datetime.now(timezone.utc)
        raid_id = str(uuid.uuid4())[:8]
        raids[guild_id] = {
            "raid_id": raid_id,
            "host_id": self.host_id,
            "hosters": list(self.pending_hosters),
            "players": [],
            "player_ids": [],
            "code": self.code,
            "wave": 1,
            "confirmed": True,
            "start_time": now,
            "wave_start_time": now,
            "gate_message_id": None,
            "pending_invites": {},
            "control_msg_id": None,
            "last_activity": now,
        }
        raid = raids[guild_id]
        try:
            hoster_ids = json.dumps(raid["hosters"])
            await d1_query(
                """INSERT INTO raids
                (raid_id, host_discord_ids, host_roblox_names, is_completed,
                 time_started, code_used, flagged, joined_users, wave_reached, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    raid_id,
                    hoster_ids,
                    "[]",
                    0,
                    now.isoformat(),
                    self.code,
                    0,
                    "[]",
                    1,
                    now.isoformat()
                ]
            )
        except Exception as e:
            print(f"Error saving initial raid: {e}")
        for uid in list(self.pending_invites.keys()):
            m = interaction.guild.get_member(uid)
            if m:
                try:
                    await m.send(
                        "⏰ **Raid Hub** — Your hoster invite has expired as the raid has been confirmed."
                    )
                except Exception:
                    pass
        embed = discord.Embed(
            title="⚔️ Raid Setup",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.description = f"{self.host_mention} has started raid setup!"
        embed.add_field(name="Status", value="✅ CONFIRMED!", inline=True)
        hoster_lines = []
        for hid in self.pending_hosters:
            if hid == self.host_id:
                hoster_lines.append(f"✅ <@{hid}> (Host)")
            else:
                hoster_lines.append(f"✅ <@{hid}> (Co-Hoster)")
        embed.add_field(name="Hosters", value="\n".join(hoster_lines) if hoster_lines else "None", inline=False)
        embed.set_footer(text="Raid confirmed! Control panel below.")
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(embed=embed, view=self)
        host_ch = interaction.guild.get_channel(HOST_CHANNEL_ID)
        if host_ch:
            if guild_id in setup_messages:
                for msg_id in setup_messages[guild_id]:
                    if msg_id != interaction.message.id:
                        try:
                            msg = await host_ch.fetch_message(msg_id)
                            await msg.delete()
                        except Exception:
                            pass
                setup_messages[guild_id] = [interaction.message.id]
        gate_ch = interaction.guild.get_channel(RAID_GATE_ID)
        if gate_ch:
            await gate_ch.purge(limit=100)
            ping_role = interaction.guild.get_role(PING_ROLE_ID)
            ping_msg = ping_role.mention if ping_role else ""
            gate_embed = await build_gate_embed(interaction.guild, raid)
            gate_msg = await gate_ch.send(content=ping_msg, embed=gate_embed, view=JoinRaidView())
            raid["gate_message_id"] = gate_msg.id
        if host_ch:
            ctrl_msg = await host_ch.send(embed=build_control_embed(raid), view=RaidControlView())
            raid["control_msg_id"] = ctrl_msg.id

        # ─── Send the PS code as an ephemeral message in the control channel ──
        await interaction.followup.send(
            f"🔑 {interaction.user.mention} Your raid code: `{self.code}`\n"
            "Keep this private! Share it only with your co-hosters.",
            ephemeral=True
        )

        schedule_raid_timeout(guild_id, interaction.client)

        log_embed = discord.Embed(
            title="⚔️ Raid Started",
            color=discord.Color.green(),
            timestamp=now
        )
        log_embed.add_field(name="Raid ID", value=f"`{raid_id}`", inline=True)
        log_embed.add_field(name="Host", value=f"<@{self.host_id}>", inline=True)
        log_embed.add_field(name="Auto-Timeout", value=f"{RAID_TIMEOUT_MINUTES} minutes", inline=True)
        await send_raid_log(interaction.client, log_embed)
        await interaction.followup.send(
            f"✅ Raid confirmed and gate is open! (ID: `{raid_id}`)",
            ephemeral=True
        )

    @discord.ui.button(label="Invite Hosters", style=discord.ButtonStyle.primary, emoji="📨")
    async def invite(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.host_id:
            return await interaction.response.send_message(
                "❌ Only the host can invite.",
                ephemeral=True
            )
        await interaction.response.send_modal(InviteHosterModal(setup_view=self))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.host_id:
            return await interaction.response.send_message(
                "❌ Only the host can cancel.",
                ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        for child in self.children:
            child.disabled = True
        new_embed = discord.Embed(
            title="⚔️ Raid Setup",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc)
        )
        new_embed.description = f"{self.host_mention} has started raid setup!"
        new_embed.add_field(name="Status", value="❌ CANCELLED", inline=True)
        hoster_lines = []
        for hid in self.pending_hosters:
            if hid == self.host_id:
                hoster_lines.append(f"❌ <@{hid}> (Host)")
            else:
                hoster_lines.append(f"❌ <@{hid}>")
        new_embed.add_field(name="Hosters", value="\n".join(hoster_lines) if hoster_lines else "None", inline=False)
        new_embed.set_footer(text="Raid setup has been cancelled.")
        await interaction.message.edit(embed=new_embed, view=self)
        await interaction.followup.send("❌ Raid setup cancelled.", ephemeral=True)
        host_ch = interaction.guild.get_channel(HOST_CHANNEL_ID)
        if host_ch:
            if guild_id in setup_messages:
                for msg_id in setup_messages[guild_id]:
                    if msg_id != interaction.message.id:
                        try:
                            msg = await host_ch.fetch_message(msg_id)
                            await msg.delete()
                        except Exception:
                            pass
                setup_messages[guild_id] = [interaction.message.id]
        gate_ch = interaction.guild.get_channel(RAID_GATE_ID)
        if gate_ch:
            cancel_embed = discord.Embed(
                title="❌ Raid Setup Cancelled",
                description=f"{interaction.user.mention} has cancelled the raid setup.",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc)
            )
            await gate_ch.send(embed=cancel_embed)


class JoinRaidView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Join Raid",
        style=discord.ButtonStyle.success,
        emoji="⚔️",
        custom_id="join_raid_btn"
    )
    async def join_raid(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if is_blacklisted(interaction.user):
                return await interaction.response.send_message(
                    "🚫 You are blacklisted.",
                    ephemeral=True
                )
            await interaction.response.send_modal(JoinRaidModal())
        except discord.HTTPException:
            return


class RaidControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Update Wave",
        style=discord.ButtonStyle.primary,
        emoji="🔄",
        custom_id="update_wave_btn"
    )
    async def update_wave(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            raid = get_raid(interaction.guild_id)
            if not raid:
                raid = await get_active_raid_from_db(interaction.guild_id)
                if raid:
                    raids[interaction.guild_id] = raid
            if not raid or interaction.user.id not in raid["hosters"]:
                return await interaction.response.send_message(
                    "❌ Only hosters can update the wave.",
                    ephemeral=True
                )
            if not interaction.response.is_done():
                await interaction.response.send_modal(UpdateWaveModal())
            else:
                await interaction.followup.send("⏰ Interaction expired. Please try again.", ephemeral=True)
        except discord.HTTPException:
            return

    @discord.ui.button(
        label="End Raid",
        style=discord.ButtonStyle.danger,
        emoji="🏁",
        custom_id="end_raid_btn"
    )
    async def end_raid_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            raid = get_raid(interaction.guild_id)
            if not raid:
                raid = await get_active_raid_from_db(interaction.guild_id)
                if raid:
                    raids[interaction.guild_id] = raid
            if not raid or interaction.user.id not in raid["hosters"]:
                return await interaction.response.send_message(
                    "❌ Only hosters can end the raid.",
                    ephemeral=True
                )
            if not interaction.response.is_done():
                await interaction.response.send_modal(WaveEndModal())
            else:
                await interaction.followup.send("⏰ Interaction expired. Please try again.", ephemeral=True)
        except discord.HTTPException:
            return


class HosterInviteView(discord.ui.View):
    def __init__(self, host_id, invitee_id, guild_id, setup_view=None):
        super().__init__(timeout=None)   # persistent now
        self.host_id = host_id
        self.invitee_id = invitee_id
        self.guild_id = guild_id
        self.setup_view = setup_view

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="✅", custom_id="hoster_invite_accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if interaction.user.id != self.invitee_id:
                return await interaction.response.send_message(
                    "❌ This invite is not for you.",
                    ephemeral=True
                )
            raid = raids.get(self.guild_id)
            if raid and raid.get("confirmed"):
                return await interaction.response.send_message(
                    "❌ This raid has already been confirmed. Invite expired.",
                    ephemeral=True
                )
            sv = self.setup_view
            if sv:
                if self.invitee_id in sv.pending_hosters:
                    return await interaction.response.send_message(
                        "⚠️ You are already a hoster.",
                        ephemeral=True
                    )
            elif raid:
                if self.invitee_id in raid["hosters"]:
                    return await interaction.response.send_message(
                        "⚠️ You are already a hoster.",
                        ephemeral=True
                    )
            # Defer before async operations (update_setup_embed, DM send)
            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException:
            return

        # State updates
        if sv:
            sv.pending_hosters.append(self.invitee_id)
            sv.pending_invites.pop(self.invitee_id, None)
            await sv.update_setup_embed(interaction)
        elif raid:
            raid["hosters"].append(self.invitee_id)
            raid["pending_invites"].pop(self.invitee_id, None)

        # Resolve the PS code — sv.code works during setup before confirm;
        # fall back to the live raid dict once the raid is confirmed.
        _sv = self.setup_view
        code = _sv.code if _sv else None
        if code is None:
            _raid_now = raids.get(self.guild_id)
            code = _raid_now.get("code") if _raid_now else None

        # Send the PS code immediately – try DM first, fallback to ephemeral
        code_sent = False
        try:
            if code:
                invitee = interaction.client.get_user(self.invitee_id)
                if invitee:
                    code_embed = discord.Embed(
                        title="⚔️ Raid Code",
                        description=f"### `{code}`",
                        color=discord.Color.green(),
                        timestamp=datetime.now(timezone.utc)
                    )
                    code_embed.add_field(name="​", value="You've been accepted as a co-hoster! Keep this code private.", inline=False)
                    await invitee.send(embed=code_embed)
                    code_sent = True
                    print(f"✅ DM sent code to {invitee} (ID: {self.invitee_id})")
        except Exception as e:
            print(f"❌ Could not DM co-hoster code: {e}")

        # Followup response (interaction was deferred above)
        if code_sent:
            await interaction.followup.send(
                "✅ You have joined the raid as a co-hoster! Check your DMs for the raid code.",
                ephemeral=True
            )
        elif code:
            await interaction.followup.send(
                f"✅ You have joined the raid as a co-hoster!\n🔑 Your raid code: `{code}`\nKeep this private!",
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                "✅ You have joined the raid as a co-hoster! (Code not available – contact the host.)",
                ephemeral=True
            )

        # Notify the host
        try:
            host = interaction.client.get_user(self.host_id)
            if host is None:
                host = await interaction.client.fetch_user(self.host_id)
            if host:
                await host.send(
                    f"✅ **Raid Hub** — <@{self.invitee_id}> has accepted your hoster invite!"
                )
        except Exception as e:
            print(f"Could not notify host: {e}")

        self.stop()

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, emoji="✖️", custom_id="hoster_invite_deny")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if interaction.user.id != self.invitee_id:
                return await interaction.response.send_message(
                    "❌ This invite is not for you.",
                    ephemeral=True
                )
            sv = self.setup_view
            if sv:
                sv.pending_invites.pop(self.invitee_id, None)
            else:
                raid = raids.get(self.guild_id)
                if raid:
                    raid["pending_invites"].pop(self.invitee_id, None)
            self.stop()
            await interaction.response.send_message(
                "❌ You have denied the hoster invite.",
                ephemeral=True
            )
        except discord.HTTPException:
            return
        try:
            host = interaction.client.get_user(self.host_id)
            if host is None:
                host = await interaction.client.fetch_user(self.host_id)
            if host:
                await host.send(
                    f"❌ **Raid Hub** — <@{self.invitee_id}> has denied your hoster invite."
                )
        except Exception:
            pass


class StartRaidPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Start Raid",
        style=discord.ButtonStyle.danger,
        emoji="⚔️",
        custom_id="start_raid_btn"
    )
    async def start_raid(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not is_hoster_or_higher(interaction.user):
                return await interaction.response.send_message(
                    "❌ You need the Hoster or Trial Hoster role to start a raid.",
                    ephemeral=True
                )
            if is_user_hosting(interaction.user.id):
                return await interaction.response.send_message(
                    "❌ You are already hosting a raid in another server! Finish that raid first.",
                    ephemeral=True
                )
            if raid_exists(interaction.guild_id):
                return await interaction.response.send_message(
                    "❌ A raid is already active.",
                    ephemeral=True
                )
            await interaction.response.send_modal(StartRaidModal())
        except discord.HTTPException:
            return


# ── Raid Cog ──────────────────────────────────────────────────────────────────
class Raid(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        bot.add_view(StartRaidPanelView())
        bot.add_view(JoinRaidView())
        bot.add_view(RaidControlView())
        bot.add_view(FlaggedApprovalView())
        bot.add_view(HosterInviteView(0, 0, 0))  # dummy for persistence
        print("✅ Raid cog initialized")

    @commands.Cog.listener()
    async def on_ready(self):
        """Restore active raid into memory if the bot restarted mid-raid."""
        if raid_exists(GUILD_ID):
            return
        try:
            raid = await get_active_raid_from_db(GUILD_ID)
            if raid:
                raids[GUILD_ID] = raid
                schedule_raid_timeout(GUILD_ID, self.bot)
                print(f"[Raid] Restored active raid {raid['raid_id']} from DB after restart")
        except Exception as e:
            print(f"[Raid] Failed to restore active raid on ready: {e}")

    @app_commands.command(
        name="raidhistory",
        description="List the last 10 raids (Staff only)"
    )
    @app_commands.checks.cooldown(1, 10)
    async def raidhistory(self, interaction: discord.Interaction):
        if not is_staff(interaction.user):
            return await interaction.response.send_message(
                "❌ You do not have permission.",
                ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        try:
            result = await d1_query(
                """SELECT raid_id, host_discord_ids, wave_reached, flagged,
                          time_started, created_at
                   FROM raids
                   WHERE is_completed = 1
                   ORDER BY created_at DESC
                   LIMIT 10"""
            )
            rows = result["results"]
            if not rows:
                return await interaction.followup.send(
                    "📭 No raid history found.",
                    ephemeral=True
                )
            table_rows = []
            for row in rows:
                try:
                    host_ids = json.loads(row["host_discord_ids"] or "[]")
                    host = interaction.guild.get_member(int(host_ids[0])) if host_ids else None
                    host_name = host.display_name if host else f"<@{host_ids[0]}>" if host_ids else "Unknown"
                except:
                    host_name = "Unknown"
                status = "⚠️" if row["flagged"] else "✅"
                time_started = datetime.fromisoformat(row["time_started"])
                time_created = datetime.fromisoformat(row["created_at"])
                duration = (time_created - time_started).total_seconds()
                duration_str = f"{int(duration // 60)}m {int(duration % 60)}s"
                xp_total = get_total_xp_for_raid_sync(row["wave_reached"])
                xp_each = xp_total
                points_each = row["wave_reached"]
                table_rows.append(
                    f"`{row['raid_id'][:6]}` {status} **{host_name}** · {row['wave_reached']}/20 · "
                    f"{duration_str} · {xp_each} XP · {points_each} pts · "
                    f"<t:{int(time_created.timestamp())}:R>"
                )
            embed = discord.Embed(
                title="📜 Raid History (Last 10)",
                color=discord.Color.blurple(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(
                name="━━━━━━━━━━━━━━━━━━━━━━",
                value="\n".join(table_rows),
                inline=False
            )
            embed.set_footer(text="Use /raidinfo <id> for detailed info")
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @app_commands.command(
        name="raidinfo",
        description="View detailed information about a specific raid (Staff only)"
    )
    @app_commands.describe(raid_id="The raid ID to look up")
    @app_commands.checks.cooldown(1, 5)
    async def raidinfo(self, interaction: discord.Interaction, raid_id: str):
        if not is_staff(interaction.user):
            return await interaction.response.send_message(
                "❌ You do not have permission.",
                ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        try:
            result = await d1_query(
                """SELECT * FROM raids
                   WHERE raid_id = ? AND is_completed = 1""",
                [raid_id]
            )
            rows = result["results"]
            if not rows:
                return await interaction.followup.send(
                    f"❌ Raid `{raid_id}` not found.",
                    ephemeral=True
                )
            row = rows[0]
            try:
                host_ids = json.loads(row["host_discord_ids"] or "[]")
                player_ids = json.loads(row["joined_users"] or "[]")
            except:
                host_ids = []
                player_ids = []
            host_mentions = " | ".join([f"<@{h}>" for h in host_ids]) if host_ids else "None"
            player_mentions = " | ".join([f"<@{p}>" for p in player_ids]) if player_ids else "None"
            time_started = datetime.fromisoformat(row["time_started"])
            time_created = datetime.fromisoformat(row["created_at"])
            duration = (time_created - time_started).total_seconds()
            duration_str = f"{int(duration // 60)}m {int(duration % 60)}s"
            xp_total = get_total_xp_for_raid_sync(row["wave_reached"])
            xp_each = xp_total
            points_each = row["wave_reached"]
            embed = discord.Embed(
                title=f"📊 Raid Details: `{raid_id}`",
                color=discord.Color.gold(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(
                name="Status",
                value="✅ Completed" if not row["flagged"] else "⚠️ Flagged" + (" (Pending Review)" if row["flagged"] else ""),
                inline=True
            )
            embed.add_field(name="Wave Reached", value=f"{row['wave_reached']}/20", inline=True)
            embed.add_field(name="Duration", value=duration_str, inline=True)
            embed.add_field(name="Host(s)", value=host_mentions, inline=False)
            embed.add_field(name="Code Used", value=f"`{row['code_used']}`", inline=True)
            embed.add_field(name="XP Earned", value=f"{xp_each} each", inline=True)
            embed.add_field(name="Points Earned", value=f"{points_each} each", inline=True)
            embed.add_field(name="Players Joined", value=str(len(player_ids)), inline=True)
            if player_ids:
                embed.add_field(name="Players", value=player_mentions, inline=False)
            embed.add_field(
                name="Started",
                value=f"<t:{int(time_started.timestamp())}:F>",
                inline=True
            )
            embed.add_field(
                name="Ended",
                value=f"<t:{int(time_created.timestamp())}:F>",
                inline=True
            )
            embed.set_footer(text="Raid History")
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @app_commands.command(
        name="endraid",
        description="Force end an active raid (Staff only)"
    )
    @app_commands.describe(user="The host of the raid to end")
    @app_commands.checks.cooldown(1, 10)
    async def endraid(self, interaction: discord.Interaction, user: discord.Member):
        if not is_staff(interaction.user):
            return await interaction.response.send_message(
                "❌ You do not have permission. Staff only.",
                ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        raid = get_raid(guild_id)
        if not raid:
            return await interaction.followup.send("❌ No active raid.", ephemeral=True)
        if raid["host_id"] != user.id and user.id not in raid["hosters"]:
            return await interaction.followup.send(
                f"❌ {user.mention} is not a host of the active raid.",
                ephemeral=True
            )
        final_wave = raid["wave"]
        now = datetime.now(timezone.utc)
        points_each = final_wave
        xp_total = get_total_xp_for_raid_sync(final_wave)
        xp_each = xp_total
        hoster_lines = []
        xp_lines = []
        level_ups = []
        for hid in raid["hosters"]:
            point_result = await add_hoster_points(hid, points_each)
            if point_result:
                hoster_lines.append(f"<@{hid}> → +**{points_each}** pts")
            member = interaction.guild.get_member(hid)
            xp_result = await award_xp(hid, xp_each, member)
            if xp_result:
                bonus_tag = " ✨(+25%)" if xp_result.get("bonus_applied", False) else ""
                xp_lines.append(f"<@{hid}> → +**{xp_each}** XP{bonus_tag}")
                if xp_result.get("leveled_up", False):
                    level_ups.append(f"<@{hid}> → Level **{xp_result['new_level']}**! 🎉")
        th_cog = interaction.client.cogs.get("TrialHoster")
        if th_cog:
            for hid in raid["hosters"]:
                await th_cog.check_user_promotion(interaction.guild, hid)
        await save_raid_to_history(guild_id, raid, final_wave, False, points_each, xp_each)
        for uid in raid["hosters"] + raid["player_ids"]:
            try:
                m = interaction.guild.get_member(uid)
                if m:
                    if uid in raid["hosters"]:
                        is_elite = is_elite_hoster(m)
                        bonus_tag = " (+25% XP bonus!)" if is_elite else ""
                        await m.send(
                            f"✅ **Raid Hub** — The raid has been force ended at wave **{final_wave}/20** by staff.\n"
                            f"🏆 You earned **{xp_each}** XP{bonus_tag} and **{points_each}** hoster points!"
                        )
                    else:
                        await m.send(
                            f"✅ **Raid Hub** — The raid has been force ended at wave **{final_wave}/20** by staff. Thanks for joining!"
                        )
            except Exception:
                pass
        gate_ch = interaction.guild.get_channel(RAID_GATE_ID)
        if gate_ch:
            await gate_ch.purge(limit=100)
            finished_embed = discord.Embed(
                title="🏁 Raid Finished",
                description=f"The raid has ended at wave **{final_wave}/20**.\nThanks to all hosters and participants!",
                color=discord.Color.greyple(),
                timestamp=now
            )
            hosters_mentions = " | ".join([f"<@{h}>" for h in raid["hosters"]])
            finished_embed.add_field(name="Hosters", value=hosters_mentions, inline=False)
            finished_embed.add_field(name="👥 Players Joined", value=f"**{len(raid['player_ids'])}** players", inline=True)
            duration_minutes = int((datetime.now(timezone.utc) - raid["start_time"]).total_seconds() // 60)
            duration_seconds = int((datetime.now(timezone.utc) - raid["start_time"]).total_seconds() % 60)
            finished_embed.add_field(name="⏱️ Duration", value=f"**{duration_minutes}m {duration_seconds}s**", inline=True)
            finished_embed.add_field(name="⭐ Points Awarded", value=f"**{points_each}** pts each", inline=True)
            finished_embed.add_field(name="✨ XP Awarded", value=f"**{xp_each}** XP each", inline=True)
            if level_ups:
                finished_embed.add_field(name="🎉 Level Ups!", value="\n".join(level_ups), inline=False)
            finished_embed.add_field(name="🌊 Final Wave", value=f"**{final_wave}/20**", inline=True)
            await gate_ch.send(embed=finished_embed)
        if raid.get("control_msg_id"):
            try:
                host_ch = interaction.guild.get_channel(HOST_CHANNEL_ID)
                if host_ch:
                    msg = await host_ch.fetch_message(raid["control_msg_id"])
                    await msg.delete()
            except Exception:
                pass
        await update_top_hoster_roles(interaction.guild, interaction.client)
        for hid in raid["hosters"]:
            remove_hosting_user(hid)
        if guild_id in raid_timeout_tasks:
            raid_timeout_tasks[guild_id].cancel()
            del raid_timeout_tasks[guild_id]
        host_ch = interaction.guild.get_channel(HOST_CHANNEL_ID)
        if host_ch:
            await cleanup_setup_messages_except(guild_id, host_ch)
        log_embed = discord.Embed(
            title="🏁 Raid Force Ended",
            color=discord.Color.orange(),
            timestamp=now
        )
        log_embed.add_field(name="Host", value=f"<@{raid['host_id']}>", inline=True)
        log_embed.add_field(name="Final Wave", value=str(final_wave), inline=True)
        log_embed.add_field(name="Points Each", value=str(points_each), inline=True)
        log_embed.add_field(name="XP Each", value=str(xp_each), inline=True)
        log_embed.add_field(name="Ended By", value=interaction.user.mention, inline=True)
        if level_ups:
            log_embed.add_field(name="🎉 Level Ups", value="\n".join(level_ups), inline=False)
        await send_raid_log(self.bot, log_embed)
        del raids[guild_id]
        await interaction.followup.send(
            f"✅ Raid force ended at wave **{final_wave}**. "
            f"**{points_each}** pts and **{xp_each}** XP awarded to each hoster.",
            ephemeral=True
        )

    @app_commands.command(
        name="members",
        description="List all players in the current raid (Staff only)"
    )
    @app_commands.checks.cooldown(1, 5)
    async def members(self, interaction: discord.Interaction):
        if not is_staff(interaction.user):
            return await interaction.response.send_message(
                "❌ You do not have permission to use this command.",
                ephemeral=True
            )
        guild_id = interaction.guild_id
        raid = get_raid(guild_id)
        if not raid:
            return await interaction.response.send_message(
                "❌ No active raid in this server.",
                ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        all_members = []
        for hid in raid.get("hosters", []):
            member = interaction.guild.get_member(hid)
            if member:
                user_result = await d1_query(
                    "SELECT roblox_users FROM users WHERE discord_id = ?",
                    [str(hid)]
                )
                roblox_name = "Unknown"
                if user_result["results"]:
                    try:
                        roblox_users = json.loads(user_result["results"][0]["roblox_users"] or "[]")
                        if roblox_users:
                            roblox_name = roblox_users[0]
                    except:
                        pass
                all_members.append({
                    "discord": member,
                    "roblox": roblox_name,
                    "is_hoster": True,
                    "is_host": hid == raid.get("host_id")
                })
        for pid in raid.get("player_ids", []):
            if pid in raid.get("hosters", []):
                continue
            member = interaction.guild.get_member(pid)
            if member:
                user_result = await d1_query(
                    "SELECT roblox_users FROM users WHERE discord_id = ?",
                    [str(pid)]
                )
                roblox_name = "Unknown"
                if user_result["results"]:
                    try:
                        roblox_users = json.loads(user_result["results"][0]["roblox_users"] or "[]")
                        if roblox_users:
                            roblox_name = roblox_users[0]
                    except:
                        pass
                all_members.append({
                    "discord": member,
                    "roblox": roblox_name,
                    "is_hoster": False,
                    "is_host": False
                })
        if not all_members:
            return await interaction.followup.send(
                "📭 No members found in the raid.",
                ephemeral=True
            )
        all_members.sort(key=lambda x: (
            0 if x["is_hoster"] and x["is_host"] else 1 if x["is_hoster"] else 2,
            x["discord"].display_name.lower()
        ))
        embed = discord.Embed(
            title=f"👥 Raid Members",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(
            name="📊 Raid Info",
            value=(
                f"**Raid ID:** `{raid.get('raid_id', 'N/A')}`\n"
                f"**Wave:** {raid.get('wave', 1)}/20\n"
                f"**Code:** `{raid.get('code', 'N/A')}`\n"
                f"**Total Members:** {len(all_members)}/{16}"
            ),
            inline=False
        )
        hosters = [m for m in all_members if m["is_hoster"]]
        if hosters:
            hoster_lines = []
            for m in hosters:
                host_tag = " 👑" if m["is_host"] else ""
                hoster_lines.append(f"**{m['discord'].mention}** - `{m['roblox']}`{host_tag}")
            embed.add_field(
                name=f"👑 Hosters ({len(hosters)})",
                value="\n".join(hoster_lines) if hoster_lines else "None",
                inline=False
            )
        players = [m for m in all_members if not m["is_hoster"]]
        if players:
            player_lines = []
            for m in players:
                player_lines.append(f"**{m['discord'].mention}** - `{m['roblox']}`")
            embed.add_field(
                name=f"👤 Players ({len(players)})",
                value="\n".join(player_lines) if player_lines else "None",
                inline=False
            )
        embed.set_footer(text=f"Total: {len(all_members)} members • Staff-only command")
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Raid(bot))
    print("✅ Raid cog setup complete")