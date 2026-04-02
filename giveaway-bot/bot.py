import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import random
import json
import os
import threading
from datetime import datetime, timedelta
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

GIVEAWAY_HOST_ROLE = "Giveaway Host"
ROLE_CONFIG_FILE = "role_config.json"
HISTORY_FILE = "giveaway_history.json"

# ============================================================
# DATA HELPERS
# ============================================================

def load_role_config():
    if not os.path.exists(ROLE_CONFIG_FILE):
        return {}
    with open(ROLE_CONFIG_FILE, "r") as f:
        return json.load(f)

def save_role_config(data):
    with open(ROLE_CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)

def load_history():
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
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)

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
        time_str = format_timedelta(remaining)
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
        msg_id = self.message_id
        if msg_id not in active_giveaways:
            await interaction.response.send_message("This giveaway has ended!", ephemeral=True)
            return

        giveaway = active_giveaways[msg_id]
        user_id = str(interaction.user.id)

        if user_id in giveaway["entries"]:
            entries = giveaway["entries"][user_id]["entries"]
            await interaction.response.send_message(
                f"You're already entered with **{entries} {'entry' if entries == 1 else 'entries'}**!",
                ephemeral=True
            )
            return

        member = interaction.guild.get_member(interaction.user.id)
        entries = get_user_entries(member) if member else 1

        giveaway["entries"][user_id] = {
            "username": str(interaction.user),
            "display_name": interaction.user.display_name,
            "entries": entries,
        }

        await interaction.response.send_message(
            f"✅ You've entered the giveaway for **{giveaway['prize']}** with "
            f"**{entries} {'entry' if entries == 1 else 'entries'}**!",
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
        }

        embed = build_embed(giveaway_data)
        placeholder_view = GiveawayView("0")

        await interaction.response.send_message(embed=embed, view=placeholder_view)
        giveaway_msg = await interaction.original_response()

        giveaway_data["message_id"] = str(giveaway_msg.id)
        active_giveaways[str(giveaway_msg.id)] = giveaway_data

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

    pool = []
    for uid, data in giveaway["entries"].items():
        pool.extend([uid] * data.get("entries", 1))

    winner_ids = []
    winner_names = []
    if pool:
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
                ended_embed.add_field(
                    name=f"🏆 Winner{'s' if len(winner_names) > 1 else ''}",
                    value="\n".join(f"<@{wid}>" for wid in winner_ids),
                    inline=False
                )
            disabled_view = discord.ui.View()
            disabled_view.add_item(discord.ui.Button(
                label="🎉 JOIN", style=discord.ButtonStyle.secondary, disabled=True
            ))
            await msg.edit(embed=ended_embed, view=disabled_view)
        except Exception:
            pass

        if winner_ids:
            mentions = " ".join(f"<@{wid}>" for wid in winner_ids)
            await channel.send(
                f"🎉 Congratulations {mentions}! You won **{giveaway['prize']}**!\n"
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

deft run_flask():
    app.run(host="0.0.0.0", port=FLASK_PORT, debug=False, use_reloader=False)

def main():
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

if __name__ == "__main__":
    main()
