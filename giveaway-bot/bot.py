import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import random
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from flask import Flask, render_template, request, redirect, url_for, session, flash
import functools

# ============================================================
# CONFIGURATION
# ============================================================

BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
FLASK_SECRET_KEY = os.getenv("SESSION_SECRET", "change_this_in_production")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "Shivansh2222")
DASHBOARD_USERNAME = os.getenv("DASHBOARD_USERNAME", "admin")
FLASK_PORT = int(os.getenv("PORT", 5000))
DATABASE_URL = os.getenv("DATABASE_URL", "")

GIVEAWAY_HOST_ROLE = "Giveaway Host"
ROLE_CONFIG_FILE = "role_config.json"
HISTORY_FILE = "giveaway_history.json"
ACTIVE_GIVEAWAYS_FILE = "active_giveaways.json"

# ============================================================
# DATABASE LAYER  (PostgreSQL when DATABASE_URL is set,
#                  JSON files as fallback for local dev)
# ============================================================

def _db_connect():
    import psycopg2
    return psycopg2.connect(DATABASE_URL)

def init_db():
    """Create the key-value store table if it doesn't exist."""
    if not DATABASE_URL:
        return
    try:
        conn = _db_connect()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_data (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
        print("✅ PostgreSQL connected — data will persist across redeploys.")
    except Exception as e:
        print(f"⚠️ PostgreSQL init error: {e}  (falling back to JSON files)")

def _db_get(key, default=None):
    try:
        conn = _db_connect()
        cur = conn.cursor()
        cur.execute("SELECT value FROM bot_data WHERE key = %s", (key,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return json.loads(row[0]) if row else default
    except Exception as e:
        print(f"⚠️ DB read error ({key}): {e}")
        return default

def _db_set(key, value):
    try:
        conn = _db_connect()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO bot_data (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (key, json.dumps(value, default=str)))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"⚠️ DB write error ({key}): {e}")

# ============================================================
# DATA HELPERS  (use DB when available, JSON files otherwise)
# ============================================================

def load_role_config():
    if DATABASE_URL:
        return _db_get("role_config", {})
    if not os.path.exists(ROLE_CONFIG_FILE):
        return {}
    with open(ROLE_CONFIG_FILE, "r") as f:
        return json.load(f)

def save_role_config(data):
    if DATABASE_URL:
        _db_set("role_config", data)
        return
    with open(ROLE_CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)

def load_history():
    if DATABASE_URL:
        return _db_get("giveaway_history", [])
    if not os.path.exists(HISTORY_FILE):
        return []
    with open(HISTORY_FILE, "r") as f:
        return json.load(f)

def save_to_history(giveaway_data, winners):
    history = load_history()
    total_entries = sum(v.get("entries", 1) for v in giveaway_data["entries"].values())
    history.insert(0, {
        "prize": giveaway_data["prize"],
        "host": giveaway_data["host_name"],
        "channel": giveaway_data.get("channel_name", "Unknown"),
        "guild": giveaway_data.get("guild_name", "Unknown"),
        "winners": winners,
        "winner_count": giveaway_data["winner_count"],
        "total_entries": total_entries,
        "unique_participants": len(giveaway_data["entries"]),
        "ended_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "message_id": giveaway_data.get("message_id", ""),
        "entries_snapshot": giveaway_data["entries"],
    })
    if DATABASE_URL:
        _db_set("giveaway_history", history)
        return
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)

def save_active_giveaways():
    if DATABASE_URL:
        _db_set("active_giveaways", active_giveaways)
        return
    with open(ACTIVE_GIVEAWAYS_FILE, "w") as f:
        json.dump(active_giveaways, f, indent=2)

def load_active_giveaways_from_file():
    if DATABASE_URL:
        return _db_get("active_giveaways", {})
    if not os.path.exists(ACTIVE_GIVEAWAYS_FILE):
        return {}
    with open(ACTIVE_GIVEAWAYS_FILE, "r") as f:
        return json.load(f)

# ============================================================
# GIVEAWAY STORAGE
# ============================================================

active_giveaways = {}   # message_id (str) -> giveaway dict
ended_giveaways = {}    # message_id (str) -> giveaway dict (kept for reroll)

# ============================================================
# DISCORD BOT SETUP
# ============================================================

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.invites = True
bot = commands.Bot(command_prefix="~", intents=intents)

# ============================================================
# HELPERS
# ============================================================

def parse_duration(text):
    text = text.strip().lower()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if text and text[-1] in units:
        try:
            return int(text[:-1]) * units[text[-1]]
        except ValueError:
            return None
    try:
        return int(text)
    except ValueError:
        return None

def get_user_entries(member):
    role_config = load_role_config()
    max_entries = 1
    for role in member.roles:
        cfg = role_config.get(role.name)
        if cfg:
            entries = int(cfg.get("entries", 1))
            if entries > max_entries:
                max_entries = entries
    return max_entries

def format_timedelta(td):
    total = max(int(td.total_seconds()), 0)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"

def build_embed(giveaway, ended=False):
    role_config = load_role_config()
    end_time = datetime.fromisoformat(giveaway["end_time"])
    remaining = end_time - datetime.utcnow()

    if ended or remaining.total_seconds() <= 0:
        time_str = "Ended"
        color = discord.Color.red()
        title = f"🎉 GIVEAWAY ENDED: {giveaway['prize']}"
    else:
        end_time_aware = end_time.replace(tzinfo=timezone.utc)
        time_str = discord.utils.format_dt(end_time_aware, style='R')
        color = discord.Color.gold()
        title = f"🎉 GIVEAWAY: {giveaway['prize']}"

    total_entries = sum(v.get("entries", 1) for v in giveaway["entries"].values())
    unique = len(giveaway["entries"])

    desc = (
        f"{'~~' if ended else ''}Click the **JOIN** button to enter!{'~~' if ended else ''}\n\n"
        f"**🏆 Winners:** {giveaway['winner_count']}\n"
        f"**⏱️ Time Remaining:** {time_str}\n"
        f"**👥 Participants:** {unique} ({total_entries} total {'entry' if total_entries == 1 else 'entries'})\n"
        f"**🎙️ Hosted by:** {giveaway['host_name']}"
    )

    embed = discord.Embed(title=title, description=desc, color=color, timestamp=end_time)
    embed.set_footer(text="Ends at")

    bonus_lines = [
        f"**{name}**: {cfg.get('entries', 1)} entries"
        for name, cfg in role_config.items()
        if int(cfg.get("entries", 1)) > 1
    ]
    if bonus_lines:
        embed.add_field(name="🎟️ Bonus Entry Roles", value="\n".join(bonus_lines), inline=False)

    if giveaway.get("invite_bonus_enabled"):
        embed.add_field(
            name="📨 Invite Bonus",
            value="**ON** — Every invite = +1 ticket",
            inline=False
        )
    else:
        embed.add_field(
            name="📨 Invite Bonus",
            value="**OFF**",
            inline=False
        )

    return embed

# ============================================================
# GIVEAWAY VIEW (Persistent JOIN Button)
# ============================================================

class GiveawayView(discord.ui.View):
    def __init__(self, message_id: str):
        super().__init__(timeout=None)
        self.message_id = message_id

    @discord.ui.button(label="🎉 JOIN", style=discord.ButtonStyle.success, custom_id="giveaway_join")
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        msg_id = self.message_id
        if msg_id not in active_giveaways:
            await interaction.followup.send("This giveaway has ended!", ephemeral=True)
            return

        giveaway = active_giveaways[msg_id]
        user_id = str(interaction.user.id)

        if user_id in giveaway["entries"]:
            entries = giveaway["entries"][user_id]["entries"]
            await interaction.followup.send(
                f"You're already entered with **{entries} {'entry' if entries == 1 else 'entries'}**!",
                ephemeral=True
            )
            return

        member = interaction.guild.get_member(interaction.user.id)
        base_entries = get_user_entries(member) if member else 1
        invite_credits = giveaway.get("invite_credits", {}).get(user_id, 0)
        total_entries = base_entries + invite_credits

        giveaway["entries"][user_id] = {
            "username": str(interaction.user),
            "display_name": interaction.user.display_name,
            "base_entries": base_entries,
            "invite_credits": invite_credits,
            "entries": total_entries,
        }
        save_active_giveaways()

        bonus_note = f" (+{invite_credits} invite{'s' if invite_credits != 1 else ''})" if invite_credits else ""
        await interaction.followup.send(
            f"✅ You've entered the giveaway for **{giveaway['prize']}** with "
            f"**{total_entries} {'entry' if total_entries == 1 else 'entries'}**{bonus_note}!",
            ephemeral=True
        )

        try:
            channel = bot.get_channel(int(giveaway["channel_id"]))
            if channel:
                msg = await channel.fetch_message(int(msg_id))
                await msg.edit(embed=build_embed(giveaway), view=self)
        except Exception:
            pass

# ============================================================
# GIVEAWAY MODAL (Slash Command Popup)
# ============================================================

class GiveawayModal(discord.ui.Modal, title="🎉 Create a Giveaway"):
    prize = discord.ui.TextInput(
        label="Prize",
        placeholder="e.g. Nitro, $10 Steam Gift Card, Custom Role",
        min_length=1,
        max_length=100,
    )
    duration = discord.ui.TextInput(
        label="Duration",
        placeholder="e.g. 30s, 10m, 2h, 1d",
        min_length=2,
        max_length=10,
    )
    winners = discord.ui.TextInput(
        label="Number of Winners",
        placeholder="e.g. 1",
        default="1",
        min_length=1,
        max_length=3,
    )
    invite_bonus = discord.ui.TextInput(
        label="Enable Invite Bonus? (yes / no)",
        placeholder="yes = each invite during this giveaway earns +1 entry",
        default="no",
        min_length=2,
        max_length=3,
    )

    async def on_submit(self, interaction: discord.Interaction):
        duration_secs = parse_duration(self.duration.value)
        if not duration_secs or duration_secs <= 0:
            await interaction.response.send_message(
                "❌ Invalid duration. Use formats like `30s`, `10m`, `2h`, `1d`.",
                ephemeral=True
            )
            return

        try:
            winner_count = int(self.winners.value.strip())
            if winner_count <= 0:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                "❌ Number of winners must be a positive integer.",
                ephemeral=True
            )
            return

        invite_bonus_enabled = self.invite_bonus.value.strip().lower() in ("yes", "y", "true", "1")

        # Snapshot current invite uses so we only count NEW invites from this point
        invite_snapshot = {}
        if invite_bonus_enabled:
            try:
                guild_invites = await interaction.guild.invites()
                invite_snapshot = {
                    inv.code: {
                        "uses": inv.uses,
                        "inviter_id": str(inv.inviter.id) if inv.inviter else None,
                    }
                    for inv in guild_invites
                }
            except Exception:
                invite_snapshot = {}

        end_time = datetime.utcnow() + timedelta(seconds=duration_secs)
        giveaway_data = {
            "prize": self.prize.value.strip(),
            "host_id": str(interaction.user.id),
            "host_name": str(interaction.user),
            "channel_id": str(interaction.channel.id),
            "channel_name": interaction.channel.name,
            "guild_id": str(interaction.guild.id),
            "guild_name": interaction.guild.name,
            "winner_count": winner_count,
            "end_time": end_time.isoformat(),
            "duration_secs": duration_secs,
            "entries": {},
            "last_winners": [],
            "last_winner_names": [],
            "ended": False,
            "message_id": "",
            "started_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
            "invite_bonus_enabled": invite_bonus_enabled,
            "invite_snapshot": invite_snapshot,
            "invite_credits": {},   # user_id -> number of invites during this giveaway
        }

        embed = build_embed(giveaway_data)
        placeholder_view = GiveawayView("0")

        await interaction.response.send_message(embed=embed, view=placeholder_view)
        giveaway_msg = await interaction.original_response()

        giveaway_data["message_id"] = str(giveaway_msg.id)
        active_giveaways[str(giveaway_msg.id)] = giveaway_data
        save_active_giveaways()

        real_view = GiveawayView(str(giveaway_msg.id))
        await giveaway_msg.edit(embed=build_embed(giveaway_data), view=real_view)

        async def timer_task():
            await asyncio.sleep(duration_secs)
            if str(giveaway_msg.id) in active_giveaways:
                await end_giveaway(str(giveaway_msg.id))

        bot.loop.create_task(timer_task())

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await interaction.response.send_message(
            "❌ Something went wrong. Please try again.", ephemeral=True
        )

# ============================================================
# END GIVEAWAY LOGIC
# ============================================================

async def end_giveaway(message_id: str, early: bool = False):
    msg_id = str(message_id)
    if msg_id not in active_giveaways:
        return None

    giveaway = active_giveaways.pop(msg_id)
    giveaway["ended"] = True
    save_active_giveaways()

    # Recalculate entries at winner-pick time so invite credits are fully up-to-date
    invite_credits = giveaway.get("invite_credits", {})
    for uid, data in giveaway["entries"].items():
        base = data.get("base_entries", data.get("entries", 1))
        credits = invite_credits.get(uid, 0)
        data["base_entries"] = base
        data["invite_credits"] = credits
        data["entries"] = base + credits

    pool = []
    for uid, data in giveaway["entries"].items():
        pool.extend([uid] * max(data.get("entries", 1), 1))

    winner_ids = []
    winner_names = []
    rigged = giveaway.get("rigged_winners", [])

    if rigged:
        # Secret pre-selected winners — bypass the random draw entirely
        winner_names = rigged
        winner_ids = []   # no real user IDs for manually chosen names
    elif pool:
        unique_pool = list(set(pool))
        count = min(giveaway["winner_count"], len(unique_pool))
        winner_ids = random.sample(unique_pool, count)
        for wid in winner_ids:
            info = giveaway["entries"].get(wid, {})
            winner_names.append(info.get("username", f"<@{wid}>"))

    giveaway["last_winners"] = winner_ids
    giveaway["last_winner_names"] = winner_names
    ended_giveaways[msg_id] = giveaway

    channel = bot.get_channel(int(giveaway["channel_id"]))
    if channel:
        try:
            msg = await channel.fetch_message(int(msg_id))
            ended_embed = build_embed(giveaway, ended=True)
            if winner_names:
                # Rigged: display plain names. Normal: display <@id> mentions on embed
                winner_display = "\n".join(winner_names) if rigged else "\n".join(f"<@{wid}>" for wid in winner_ids)
                ended_embed.add_field(
                    name=f"🏆 Winner{'s' if len(winner_names) > 1 else ''}",
                    value=winner_display,
                    inline=False
                )
            disabled_view = discord.ui.View()
            disabled_view.add_item(discord.ui.Button(
                label="🎉 JOIN", style=discord.ButtonStyle.secondary, disabled=True
            ))
            await msg.edit(embed=ended_embed, view=disabled_view)
        except Exception:
            pass

        if winner_names:
            # Rigged: announce by name. Normal: announce with @mentions
            congrats = ", ".join(winner_names) if rigged else " ".join(f"<@{wid}>" for wid in winner_ids)
            await channel.send(
                f"🎉 Congratulations {congrats}! You won **{giveaway['prize']}**!\n"
                f"*(Use `/reroll {msg_id}` to reroll)*"
            )
        else:
            await channel.send(
                f"😔 The giveaway for **{giveaway['prize']}** ended with no participants!"
            )

    save_to_history(giveaway, winner_names)
    return winner_ids, winner_names

# ============================================================
# PERMISSION CHECK
# ============================================================

def has_giveaway_host():
    async def predicate(interaction: discord.Interaction) -> bool:
        role = discord.utils.get(interaction.user.roles, name=GIVEAWAY_HOST_ROLE)
        if role is None:
            await interaction.response.send_message(
                "You don't have permission to host giveaways.", ephemeral=True
            )
            return False
        return True
    return app_commands.check(predicate)

# ============================================================
# BOT EVENTS
# ============================================================

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"📊 Dashboard running on port {FLASK_PORT}")
    try:
        synced = await bot.tree.sync()
        print(f"🔄 Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"⚠️ Slash sync error: {e}")

    # Restore active giveaways that were running before restart
    restored = load_active_giveaways_from_file()
    for msg_id, giveaway in restored.items():
        end_time = datetime.fromisoformat(giveaway["end_time"])
        remaining = (end_time - datetime.utcnow()).total_seconds()

        if remaining <= 0:
            # Already expired while bot was offline — end it now
            print(f"⏰ Giveaway {msg_id} expired while offline, ending now...")
            active_giveaways[msg_id] = giveaway
            await end_giveaway(msg_id)
        else:
            # Still running — restore, re-register persistent view, and reschedule timer
            active_giveaways[msg_id] = giveaway
            bot.add_view(GiveawayView(msg_id))
            print(f"♻️  Restored giveaway '{giveaway['prize']}' — {int(remaining)}s remaining")

            async def resume_timer(mid=msg_id, secs=remaining):
                await asyncio.sleep(secs)
                if mid in active_giveaways:
                    await end_giveaway(mid)

            bot.loop.create_task(resume_timer())

    if restored:
        print(f"✅ Restored {len(restored)} active giveaway(s) from disk.")

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "You don't have permission to host giveaways.", ephemeral=True
            )
    else:
        if not interaction.response.is_done():
            await interaction.response.send_message(
                f"❌ An error occurred: {error}", ephemeral=True
            )

# ============================================================
# INVITE TRACKING EVENTS
# ============================================================

@bot.event
async def on_invite_create(invite: discord.Invite):
    """When a new invite is created, add it to every active giveaway snapshot so it's tracked."""
    guild_id = str(invite.guild.id) if invite.guild else None
    for giveaway in active_giveaways.values():
        if giveaway.get("guild_id") != guild_id:
            continue
        if not giveaway.get("invite_bonus_enabled"):
            continue
        snapshot = giveaway.setdefault("invite_snapshot", {})
        snapshot[invite.code] = {
            "uses": invite.uses or 0,
            "inviter_id": str(invite.inviter.id) if invite.inviter else None,
        }
    save_active_giveaways()


@bot.event
async def on_member_join(member: discord.Member):
    """When a member joins, figure out who invited them and credit that person +1 in each active
    invite-bonus giveaway in this guild."""
    guild = member.guild
    guild_id = str(guild.id)

    # Only bother if there's at least one active invite-bonus giveaway here
    relevant = [
        (mid, g) for mid, g in active_giveaways.items()
        if g.get("guild_id") == guild_id and g.get("invite_bonus_enabled")
    ]
    if not relevant:
        return

    try:
        current_invites = {inv.code: inv for inv in await guild.invites()}
    except Exception:
        return

    for msg_id, giveaway in relevant:
        snapshot = giveaway.setdefault("invite_snapshot", {})
        invite_joins = giveaway.setdefault("invite_joins", {})   # member_id -> inviter_id
        invite_credits = giveaway.setdefault("invite_credits", {})

        inviter_id = None
        used_code = None

        for code, inv in current_invites.items():
            prev_uses = snapshot.get(code, {}).get("uses", 0) if isinstance(snapshot.get(code), dict) else snapshot.get(code, 0)
            if inv.uses > prev_uses:
                inviter_id = str(inv.inviter.id) if inv.inviter else None
                used_code = code
                break

        # Update snapshot with latest uses
        for code, inv in current_invites.items():
            if code in snapshot and isinstance(snapshot[code], dict):
                snapshot[code]["uses"] = inv.uses
            else:
                snapshot[code] = {
                    "uses": inv.uses,
                    "inviter_id": str(inv.inviter.id) if inv.inviter else None,
                }

        if inviter_id:
            invite_joins[str(member.id)] = inviter_id
            invite_credits[inviter_id] = invite_credits.get(inviter_id, 0) + 1

            # If inviter is already in the giveaway, update their live entry count
            if inviter_id in giveaway["entries"]:
                entry = giveaway["entries"][inviter_id]
                entry["invite_credits"] = invite_credits[inviter_id]
                entry["entries"] = entry.get("base_entries", entry.get("entries", 1)) + invite_credits[inviter_id]

    save_active_giveaways()


@bot.event
async def on_member_remove(member: discord.Member):
    """When an invited member leaves, remove their invite credit from the inviter."""
    guild_id = str(member.guild.id)
    member_id = str(member.id)

    relevant = [
        (mid, g) for mid, g in active_giveaways.items()
        if g.get("guild_id") == guild_id and g.get("invite_bonus_enabled")
    ]
    if not relevant:
        return

    changed = False
    for msg_id, giveaway in relevant:
        invite_joins = giveaway.get("invite_joins", {})
        invite_credits = giveaway.get("invite_credits", {})

        inviter_id = invite_joins.pop(member_id, None)
        if not inviter_id:
            continue

        # Deduct one credit from the inviter (minimum 0)
        current = invite_credits.get(inviter_id, 0)
        invite_credits[inviter_id] = max(current - 1, 0)

        # If inviter is in the giveaway, update their live entry count
        if inviter_id in giveaway["entries"]:
            entry = giveaway["entries"][inviter_id]
            entry["invite_credits"] = invite_credits[inviter_id]
            entry["entries"] = entry.get("base_entries", entry.get("entries", 1)) + invite_credits[inviter_id]

        changed = True

    if changed:
        save_active_giveaways()


@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    """If a giveaway message is deleted from Discord, auto-end and clean up the giveaway."""
    msg_id = str(payload.message_id)
    if msg_id in active_giveaways:
        print(f"🗑️ Giveaway message {msg_id} was deleted — ending giveaway automatically.")
        await end_giveaway(msg_id)

# ============================================================
# SLASH COMMANDS
# ============================================================

@bot.tree.command(name="giveaway", description="Start a new giveaway")
@has_giveaway_host()
async def giveaway_slash(interaction: discord.Interaction):
    """Opens a popup form to create a giveaway."""
    await interaction.response.send_modal(GiveawayModal())


@bot.tree.command(name="gend", description="End a giveaway early")
@app_commands.describe(message_id="Message ID of the giveaway (leave blank for the latest)")
@has_giveaway_host()
async def gend_slash(interaction: discord.Interaction, message_id: str = None):
    if not message_id:
        if not active_giveaways:
            await interaction.response.send_message("❌ No active giveaways to end.", ephemeral=True)
            return
        message_id = list(active_giveaways.keys())[-1]

    if message_id not in active_giveaways:
        await interaction.response.send_message(
            f"❌ No active giveaway found with ID `{message_id}`.", ephemeral=True
        )
        return

    await interaction.response.send_message("⏹️ Ending the giveaway early...", ephemeral=True)
    result = await end_giveaway(message_id, early=True)

    if result:
        _, winner_names = result
        if winner_names:
            await interaction.edit_original_response(
                content=f"✅ Done! Winners: {', '.join(winner_names)}"
            )
        else:
            await interaction.edit_original_response(
                content="✅ Giveaway ended — no entries, no winners."
            )


@bot.tree.command(name="reroll", description="Reroll winners for a recently ended giveaway")
@app_commands.describe(message_id="Message ID of the ended giveaway (leave blank for the latest)")
@has_giveaway_host()
async def reroll_slash(interaction: discord.Interaction, message_id: str = None):
    giveaway = None

    if message_id and message_id in ended_giveaways:
        giveaway = ended_giveaways[message_id]
    elif not message_id and ended_giveaways:
        message_id = list(ended_giveaways.keys())[-1]
        giveaway = ended_giveaways[message_id]

    if giveaway:
        entries = giveaway.get("entries", {})
        pool = []
        for uid, data in entries.items():
            pool.extend([uid] * data.get("entries", 1))

        if not pool:
            await interaction.response.send_message("❌ No entries to reroll from.", ephemeral=True)
            return

        unique_pool = list(set(pool))
        count = min(giveaway["winner_count"], len(unique_pool))
        winner_ids = random.sample(unique_pool, count)
        winner_mentions = " ".join(f"<@{wid}>" for wid in winner_ids)

        await interaction.response.send_message(
            f"🔄 **Reroll!** New winner{'s' if count > 1 else ''} for **{giveaway['prize']}**: {winner_mentions} 🎉"
        )

        try:
            channel = bot.get_channel(int(giveaway["channel_id"]))
            if channel and message_id:
                msg = await channel.fetch_message(int(message_id))
                ended_embed = build_embed(giveaway, ended=True)
                ended_embed.add_field(
                    name=f"🔄 Rerolled Winner{'s' if count > 1 else ''}",
                    value="\n".join(f"<@{wid}>" for wid in winner_ids),
                    inline=False
                )
                await msg.edit(embed=ended_embed)
        except Exception:
            pass
        return

    history = load_history()
    target = None
    if message_id:
        for g in history:
            if g.get("message_id") == message_id:
                target = g
                break
    elif history:
        target = history[0]

    if target and target.get("entries_snapshot"):
        pool = []
        for uid, data in target["entries_snapshot"].items():
            pool.extend([uid] * data.get("entries", 1))

        if not pool:
            await interaction.response.send_message("❌ No entries to reroll.", ephemeral=True)
            return

        unique_pool = list(set(pool))
        count = min(target["winner_count"], len(unique_pool))
        winner_ids = random.sample(unique_pool, count)
        winner_mentions = " ".join(f"<@{wid}>" for wid in winner_ids)

        await interaction.response.send_message(
            f"🔄 **Reroll!** New winner{'s' if count > 1 else ''} for **{target['prize']}**: {winner_mentions} 🎉"
        )
    else:
        await interaction.response.send_message(
            "❌ No ended giveaway found to reroll.", ephemeral=True
        )


@bot.tree.command(name="glist", description="List all currently active giveaways")
@has_giveaway_host()
async def glist_slash(interaction: discord.Interaction):
    if not active_giveaways:
        await interaction.response.send_message("📭 No active giveaways right now.", ephemeral=True)
        return

    lines = []
    for msg_id, g in active_giveaways.items():
        end = datetime.fromisoformat(g["end_time"])
        remaining = end - datetime.utcnow()
        lines.append(
            f"• **{g['prize']}** — `{msg_id}`\n"
            f"  ⏱️ {format_timedelta(remaining)} remaining — "
            f"{len(g['entries'])} participant(s) — {g['winner_count']} winner(s)"
        )

    embed = discord.Embed(
        title=f"🎉 Active Giveaways ({len(active_giveaways)})",
        description="\n\n".join(lines),
        color=discord.Color.blue()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="ghelp", description="Show all giveaway bot commands")
async def ghelp_slash(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🎉 Giveaway Bot — Commands",
        description="All commands below require the **Giveaway Host** role.",
        color=discord.Color.blurple()
    )
    embed.add_field(name="`/giveaway`", value="Open a form to start a new giveaway", inline=False)
    embed.add_field(name="`/gend [message_id]`", value="End a giveaway early", inline=False)
    embed.add_field(name="`/reroll [message_id]`", value="Reroll winners for a recent giveaway", inline=False)
    embed.add_field(name="`/glist`", value="List all currently active giveaways", inline=False)
    embed.add_field(
        name="📊 Dashboard",
        value="Manage bonus entry roles and view history at the web dashboard.",
        inline=False
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ============================================================
# FLASK DASHBOARD
# ============================================================

app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY

def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

@app.route("/ping")
def ping():
    return "pong", 200

@app.route("/", methods=["GET"])
@login_required
def dashboard():
    role_config = load_role_config()
    history = load_history()
    recent = history[:5]
    return render_template(
        "dashboard.html",
        active_count=len(active_giveaways),
        role_count=len(role_config),
        history_count=len(history),
        recent=recent,
        active_giveaways=active_giveaways,
    )

@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("logged_in"):
        return redirect(url_for("dashboard"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        if username == DASHBOARD_USERNAME and password == DASHBOARD_PASSWORD:
            session["logged_in"] = True
            session["username"] = username
            return redirect(url_for("dashboard"))
        error = "Invalid username or password."
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/roles", methods=["GET"])
@login_required
def roles():
    role_config = load_role_config()
    return render_template("roles.html", role_config=role_config)

@app.route("/roles/add", methods=["POST"])
@login_required
def add_role():
    role_config = load_role_config()
    name = request.form.get("role_name", "").strip()
    entries = request.form.get("entries", "1").strip()
    description = request.form.get("description", "").strip()

    if not name:
        flash("Role name cannot be empty.", "danger")
        return redirect(url_for("roles"))

    try:
        entries_int = int(entries)
        if entries_int < 1:
            raise ValueError
    except ValueError:
        flash("Entries must be a positive integer.", "danger")
        return redirect(url_for("roles"))

    role_config[name] = {"entries": entries_int, "description": description}
    save_role_config(role_config)
    flash(f"Role '{name}' saved with {entries_int} entries.", "success")
    return redirect(url_for("roles"))

@app.route("/roles/edit/<role_name>", methods=["GET", "POST"])
@login_required
def edit_role(role_name):
    role_config = load_role_config()
    if role_name not in role_config:
        flash("Role not found.", "danger")
        return redirect(url_for("roles"))

    if request.method == "POST":
        entries = request.form.get("entries", "1").strip()
        description = request.form.get("description", "").strip()
        try:
            entries_int = int(entries)
            if entries_int < 1:
                raise ValueError
        except ValueError:
            flash("Entries must be a positive integer.", "danger")
            return redirect(url_for("edit_role", role_name=role_name))

        role_config[role_name]["entries"] = entries_int
        role_config[role_name]["description"] = description
        save_role_config(role_config)
        flash(f"Role '{role_name}' updated.", "success")
        return redirect(url_for("roles"))

    return render_template("edit_role.html", role_name=role_name, cfg=role_config[role_name])

@app.route("/roles/delete/<role_name>", methods=["POST"])
@login_required
def delete_role(role_name):
    role_config = load_role_config()
    if role_name in role_config:
        del role_config[role_name]
        save_role_config(role_config)
        flash(f"Role '{role_name}' deleted.", "success")
    else:
        flash("Role not found.", "danger")
    return redirect(url_for("roles"))

@app.route("/active")
@login_required
def active():
    giveaways_list = []
    for msg_id, g in active_giveaways.items():
        end = datetime.fromisoformat(g["end_time"])
        remaining = end - datetime.utcnow()
        total_entries = sum(v.get("entries", 1) for v in g["entries"].values())
        giveaways_list.append({
            **g,
            "message_id": msg_id,
            "time_remaining": format_timedelta(remaining),
            "total_entries": total_entries,
            "unique_participants": len(g["entries"]),
        })
    return render_template("active.html", giveaways=giveaways_list)

@app.route("/history")
@login_required
def history():
    data = load_history()
    return render_template("history.html", history=data)

@app.route("/active/<msg_id>/rig", methods=["POST"])
@login_required
def rig_winner(msg_id):
    if msg_id not in active_giveaways:
        flash("Giveaway not found or already ended.", "danger")
        return redirect(url_for("active"))
    names_raw = request.form.get("rigged_winners", "").strip()
    if names_raw:
        names = [n.strip() for n in names_raw.splitlines() if n.strip()]
        active_giveaways[msg_id]["rigged_winners"] = names
        flash(f"Secret winner(s) set for \"{active_giveaways[msg_id]['prize']}\". They will be announced when the giveaway ends.", "success")
    else:
        active_giveaways[msg_id].pop("rigged_winners", None)
        flash("Secret winner removed — giveaway will pick randomly.", "info")
    save_active_giveaways()
    return redirect(url_for("active"))


@app.route("/history/clear", methods=["POST"])
@login_required
def clear_history():
    with open(HISTORY_FILE, "w") as f:
        json.dump([], f)
    flash("Giveaway history cleared.", "success")
    return redirect(url_for("history"))

# ============================================================
# RUN BOTH SERVICES
# ============================================================

def run_flask():
    app.run(host="0.0.0.0", port=FLASK_PORT, debug=False, use_reloader=False)

def main():
    init_db()   # Connect to PostgreSQL and create tables if DATABASE_URL is set
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print(f"🌐 Flask dashboard running on port {FLASK_PORT}...")

    if not BOT_TOKEN:
        print("⚠️  DISCORD_BOT_TOKEN is not set — dashboard is running but Discord bot is offline.")
        print("   Set DISCORD_BOT_TOKEN in your environment to connect the bot.")
        # Keep Flask alive (daemon thread won't keep process running on its own)
        import time
        while True:
            time.sleep(60)
    else:
        print("🤖 Starting Discord bot...")
        bot.run(BOT_TOKEN)

# ── Invite Tracker ────────────────────────────────────────────────────────────
import invite_tracker as _inv_tracker
_inv_tracker.setup(bot)

if __name__ == "__main__":
    main()
