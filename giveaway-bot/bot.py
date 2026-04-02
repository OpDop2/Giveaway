import discord
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
bot = commands.Bot(command_prefix="!", intents=intents)

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
# GIVEAWAY VIEW (Persistent Button)
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
                f"*(Host can use `!reroll {msg_id}` to reroll)*"
            )
        else:
            await channel.send(
                f"😔 The giveaway for **{giveaway['prize']}** ended with no participants!"
            )

    save_to_history(giveaway, winner_names)
    return winner_ids, winner_names

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

# ============================================================
# PERMISSION DECORATOR
# ============================================================

def giveaway_host_only():
    async def predicate(ctx):
        role = discord.utils.get(ctx.author.roles, name=GIVEAWAY_HOST_ROLE)
        if role is None:
            await ctx.reply("You don't have permission to host giveaways.")
            return False
        return True
    return commands.check(predicate)

# ============================================================
# COMMANDS
# ============================================================

@bot.command(name="giveaway", aliases=["gstart", "gcreate"])
@giveaway_host_only()
async def giveaway_cmd(ctx):
    """Start a new giveaway interactively."""
    def check(m):
        return m.author == ctx.author and m.channel == ctx.channel

    setup_msg = await ctx.send(
        "🎉 **Giveaway Setup** — I'll ask you a few questions.\n\n"
        "**Step 1/3** — What is the **prize**?"
    )

    try:
        msg = await bot.wait_for("message", check=check, timeout=60)
        prize = msg.content.strip()
        if not prize:
            await ctx.send("❌ Prize cannot be empty. Setup cancelled.")
            return
    except asyncio.TimeoutError:
        await ctx.send("⏰ Setup timed out. Please run `!giveaway` again.")
        return

    await ctx.send(
        "**Step 2/3** — How long should the giveaway last?\n"
        "*(e.g. `30s`, `10m`, `2h`, `1d`)*"
    )
    try:
        msg = await bot.wait_for("message", check=check, timeout=60)
        duration_secs = parse_duration(msg.content)
        if not duration_secs or duration_secs <= 0:
            await ctx.send("❌ Invalid duration. Setup cancelled.")
            return
    except asyncio.TimeoutError:
        await ctx.send("⏰ Setup timed out. Please run `!giveaway` again.")
        return

    await ctx.send("**Step 3/3** — How many **winners**?")
    try:
        msg = await bot.wait_for("message", check=check, timeout=60)
        try:
            winner_count = int(msg.content.strip())
            if winner_count <= 0:
                raise ValueError
        except ValueError:
            await ctx.send("❌ Invalid number. Setup cancelled.")
            return
    except asyncio.TimeoutError:
        await ctx.send("⏰ Setup timed out. Please run `!giveaway` again.")
        return

    try:
        await setup_msg.delete()
    except Exception:
        pass

    end_time = datetime.utcnow() + timedelta(seconds=duration_secs)
    giveaway_data = {
        "prize": prize,
        "host_id": str(ctx.author.id),
        "host_name": str(ctx.author),
        "channel_id": str(ctx.channel.id),
        "channel_name": ctx.channel.name,
        "guild_id": str(ctx.guild.id),
        "guild_name": ctx.guild.name,
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
    giveaway_msg = await ctx.send(embed=embed, view=placeholder_view)

    giveaway_data["message_id"] = str(giveaway_msg.id)
    active_giveaways[str(giveaway_msg.id)] = giveaway_data

    real_view = GiveawayView(str(giveaway_msg.id))
    await giveaway_msg.edit(embed=build_embed(giveaway_data), view=real_view)

    confirm = await ctx.send(
        f"✅ Giveaway started! Message ID: `{giveaway_msg.id}`\n"
        f"Use `!gend {giveaway_msg.id}` to end early.",
        delete_after=15
    )

    async def timer_task():
        await asyncio.sleep(duration_secs)
        if str(giveaway_msg.id) in active_giveaways:
            await end_giveaway(str(giveaway_msg.id))

    bot.loop.create_task(timer_task())


@bot.command(name="gend")
@giveaway_host_only()
async def gend_cmd(ctx, message_id: str = None):
    """End a giveaway early. Usage: !gend [message_id]"""
    if not message_id:
        if not active_giveaways:
            await ctx.reply("❌ No active giveaways to end.")
            return
        message_id = list(active_giveaways.keys())[-1]

    if message_id not in active_giveaways:
        await ctx.reply(f"❌ No active giveaway found with ID `{message_id}`.")
        return

    await ctx.reply("⏹️ Ending the giveaway early...")
    result = await end_giveaway(message_id, early=True)

    if result:
        _, winner_names = result
        if winner_names:
            await ctx.send(f"✅ Done! Winners: {', '.join(winner_names)}")
        else:
            await ctx.send("✅ Giveaway ended — no entries, no winners.")


@bot.command(name="reroll")
@giveaway_host_only()
async def reroll_cmd(ctx, message_id: str = None):
    """Reroll winners for a recently ended giveaway. Usage: !reroll [message_id]"""
    giveaway = None

    if message_id and message_id in ended_giveaways:
        giveaway = ended_giveaways[message_id]
    elif not message_id and ended_giveaways:
        msg_id = list(ended_giveaways.keys())[-1]
        giveaway = ended_giveaways[msg_id]
        message_id = msg_id
    else:
        history = load_history()
        if history:
            target = None
            if message_id:
                for g in history:
                    if g.get("message_id") == message_id:
                        target = g
                        break
            else:
                target = history[0] if history else None

            if target and target.get("entries_snapshot"):
                entries_snap = target["entries_snapshot"]
                prize = target["prize"]
                winner_count = target["winner_count"]
            else:
                await ctx.reply("❌ Couldn't find giveaway data to reroll.")
                return

            pool = []
            for uid, data in entries_snap.items():
                pool.extend([uid] * data.get("entries", 1))

            if not pool:
                await ctx.reply("❌ No entries to reroll.")
                return

            unique_pool = list(set(pool))
            count = min(winner_count, len(unique_pool))
            winner_ids = random.sample(unique_pool, count)
            winner_mentions = " ".join(f"<@{wid}>" for wid in winner_ids)

            await ctx.send(
                f"🔄 **Reroll!** New winner{'s' if count > 1 else ''} for **{prize}**: {winner_mentions} 🎉"
            )
            return
        else:
            await ctx.reply("❌ No ended giveaways found to reroll.")
            return

    if giveaway:
        entries = giveaway.get("entries", {})
        pool = []
        for uid, data in entries.items():
            pool.extend([uid] * data.get("entries", 1))

        if not pool:
            await ctx.reply("❌ No entries to reroll from.")
            return

        unique_pool = list(set(pool))
        count = min(giveaway["winner_count"], len(unique_pool))
        winner_ids = random.sample(unique_pool, count)
        winner_names = [entries[wid].get("username", wid) for wid in winner_ids]
        winner_mentions = " ".join(f"<@{wid}>" for wid in winner_ids)

        await ctx.send(
            f"🔄 **Reroll!** New winner{'s' if count > 1 else ''} for **{giveaway['prize']}**: {winner_mentions} 🎉"
        )

        channel = bot.get_channel(int(giveaway["channel_id"]))
        if channel and message_id:
            try:
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


@bot.command(name="glist")
@giveaway_host_only()
async def glist_cmd(ctx):
    """List all active giveaways."""
    if not active_giveaways:
        await ctx.reply("📭 No active giveaways right now.")
        return

    lines = []
    for msg_id, g in active_giveaways.items():
        end = datetime.fromisoformat(g["end_time"])
        remaining = end - datetime.utcnow()
        lines.append(
            f"• **{g['prize']}** — `{msg_id}` — ends in {format_timedelta(remaining)} — "
            f"{len(g['entries'])} participant(s)"
        )

    embed = discord.Embed(
        title=f"🎉 Active Giveaways ({len(active_giveaways)})",
        description="\n".join(lines),
        color=discord.Color.blue()
    )
    await ctx.reply(embed=embed)


@bot.command(name="ghelp")
async def ghelp_cmd(ctx):
    """Show giveaway bot help."""
    embed = discord.Embed(
        title="🎉 Giveaway Bot — Help",
        description="All commands require the **Giveaway Host** role.",
        color=discord.Color.blurple()
    )
    embed.add_field(name="`!giveaway`", value="Start a new giveaway (interactive setup)", inline=False)
    embed.add_field(name="`!gend [id]`", value="End a giveaway early", inline=False)
    embed.add_field(name="`!reroll [id]`", value="Reroll winners for a recent giveaway", inline=False)
    embed.add_field(name="`!glist`", value="List all active giveaways", inline=False)
    embed.add_field(
        name="📊 Dashboard",
        value="Manage roles and view history at the web dashboard.",
        inline=False
    )
    await ctx.reply(embed=embed)


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
    """UptimeRobot keep-alive endpoint."""
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

def run_flask():
    app.run(host="0.0.0.0", port=FLASK_PORT, debug=False, use_reloader=False)

def main():
    if not BOT_TOKEN:
        print("❌ ERROR: DISCORD_BOT_TOKEN environment variable is not set.")
        print("   Set it in your environment before starting the bot.")
        return

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    print(f"🌐 Flask dashboard starting on port {FLASK_PORT}...")
    bot.run(BOT_TOKEN)

if __name__ == "__main__":
    main()
