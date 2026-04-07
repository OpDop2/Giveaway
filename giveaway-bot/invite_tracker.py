"""
invite_tracker.py — Advanced invite tracking & staff invite management.

Uses Supabase exclusively for all data storage.
Does NOT touch or interfere with the DATABASE_URL / PostgreSQL system.

Required Supabase table (run once in your Supabase SQL editor):
─────────────────────────────────────────────────────────────────
    CREATE TABLE invites (
        "userId"    TEXT PRIMARY KEY,
        invites     INTEGER NOT NULL DEFAULT 0,
        "left"      INTEGER NOT NULL DEFAULT 0,
        fake        INTEGER NOT NULL DEFAULT 0,
        "invitedBy" TEXT
    );
─────────────────────────────────────────────────────────────────

Environment variables used (Railway):
  SUPABASE_URL           — your Supabase project URL
  SUPABASE_KEY           — your Supabase anon/service-role key
  STAFF_ROLE_NAME        — Discord role name for staff (default: "Staff")
  INVITE_LOG_CHANNEL_ID  — channel ID for staff action logs (optional)
  FAKE_INVITE_MINUTES    — minutes threshold for fake detection (default: 10)
"""

import os
import discord
from discord import app_commands
from datetime import datetime

# ─────────────────────────────────────────────────────────────────────────────
# Supabase client
# ─────────────────────────────────────────────────────────────────────────────

try:
    from supabase import create_client, Client as _SupabaseClient
    _SB_URL = os.getenv("SUPABASE_URL", "")
    _SB_KEY = os.getenv("SUPABASE_KEY", "")
    supabase: "_SupabaseClient | None" = (
        create_client(_SB_URL, _SB_KEY) if _SB_URL and _SB_KEY else None
    )
    if supabase:
        print("✅ Invite tracker: Supabase connected.")
    else:
        print("⚠️  Invite tracker: SUPABASE_URL / SUPABASE_KEY not set — invite tracking disabled.")
except Exception as _init_err:
    print(f"⚠️  Invite tracker: Supabase init failed — {_init_err}")
    supabase = None

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

STAFF_ROLE_NAME    = os.getenv("STAFF_ROLE_NAME", "Staff")
LOG_CHANNEL_ID     = int(os.getenv("INVITE_LOG_CHANNEL_ID", "0") or "0") or None
FAKE_THRESHOLD_MIN = int(os.getenv("FAKE_INVITE_MINUTES", "10"))
TABLE              = "invites"

REWARD_MILESTONES = [5, 10, 25, 50, 100, 200, 500]

# ─────────────────────────────────────────────────────────────────────────────
# In-memory state  (per-process, resets on bot restart — intentional)
# ─────────────────────────────────────────────────────────────────────────────

# guild_id -> { invite_code -> uses }
_invite_cache: dict = {}

# (guild_id, member_id) -> (joined_at: datetime, inviter_id: str | None)
_join_registry: dict = {}

# ─────────────────────────────────────────────────────────────────────────────
# Supabase helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get(user_id: str) -> dict | None:
    """Fetch a user's invite record. Returns None if missing or Supabase unavailable."""
    if not supabase:
        return None
    try:
        res = supabase.table(TABLE).select("*").eq("userId", user_id).execute()
        return res.data[0] if res.data else None
    except Exception as e:
        print(f"⚠️  Invite DB get error ({user_id}): {e}")
        return None


def _ensure(user_id: str) -> dict:
    """Return existing record or create a blank one and return that."""
    rec = _get(user_id)
    if rec:
        return rec
    blank = {"userId": user_id, "invites": 0, "left": 0, "fake": 0, "invitedBy": None}
    if supabase:
        try:
            supabase.table(TABLE).insert(blank).execute()
        except Exception as e:
            print(f"⚠️  Invite DB insert error ({user_id}): {e}")
    return blank


def _patch(user_id: str, data: dict):
    """Upsert arbitrary fields. Always includes userId so upsert works correctly."""
    if not supabase:
        return
    data["userId"] = user_id
    try:
        supabase.table(TABLE).upsert(data, on_conflict="userId").execute()
    except Exception as e:
        print(f"⚠️  Invite DB patch error ({user_id}): {e}")


def _increment(user_id: str, **deltas: int) -> dict:
    """
    Read current values, add deltas, clamp to >= 0, write back.
    Returns the updated record dict.
    """
    rec = _ensure(user_id)
    updates: dict = {}
    for field, delta in deltas.items():
        current = rec.get(field) or 0
        updates[field] = max(0, current + delta)
    if updates:
        _patch(user_id, updates)
    return {**rec, **updates}

# ─────────────────────────────────────────────────────────────────────────────
# Staff log helper
# ─────────────────────────────────────────────────────────────────────────────

async def _log(bot: discord.Client, action: str, staff: discord.Member,
               target: discord.Member, detail: str):
    print(f"[INVITE LOG] {action} | Staff: {staff} ({staff.id}) | Target: {target} ({target.id}) | {detail}")
    if not LOG_CHANNEL_ID:
        return
    ch = bot.get_channel(LOG_CHANNEL_ID)
    if not ch:
        return
    embed = discord.Embed(
        title=f"📋 Invite Action: {action}",
        color=discord.Color.orange(),
        timestamp=datetime.utcnow(),
    )
    embed.add_field(name="Staff",  value=f"{staff} (`{staff.id}`)",   inline=True)
    embed.add_field(name="Target", value=f"{target} (`{target.id}`)", inline=True)
    embed.add_field(name="Detail", value=detail,                       inline=False)
    try:
        await ch.send(embed=embed)
    except Exception as e:
        print(f"⚠️  Invite log channel send failed: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# Permission check
# ─────────────────────────────────────────────────────────────────────────────

def _is_staff(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    return any(r.name == STAFF_ROLE_NAME for r in member.roles)

# ─────────────────────────────────────────────────────────────────────────────
# Reward milestone helper
# ─────────────────────────────────────────────────────────────────────────────

def _next_milestone(net: int) -> int | None:
    for m in REWARD_MILESTONES:
        if net < m:
            return m
    return None

# ─────────────────────────────────────────────────────────────────────────────
# Embed builders
# ─────────────────────────────────────────────────────────────────────────────

def _stats_embed(rec: dict, member: discord.Member) -> discord.Embed:
    total  = rec.get("invites", 0) or 0
    left   = rec.get("left",    0) or 0
    fake   = rec.get("fake",    0) or 0
    net    = max(0, total - left - fake)
    inv_by = rec.get("invitedBy")

    embed = discord.Embed(
        title=f"🎟️ Invite Stats — {member.display_name}",
        color=discord.Color.blurple(),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Total Invites", value=str(total),     inline=True)
    embed.add_field(name="Left",          value=str(left),      inline=True)
    embed.add_field(name="Fake",          value=str(fake),      inline=True)
    embed.add_field(name="Net Invites",   value=f"**{net}**",   inline=True)
    embed.add_field(
        name="Invited By",
        value=f"<@{inv_by}>" if inv_by else "Unknown / Direct join",
        inline=True,
    )
    return embed


def _progress_embed(rec: dict, member: discord.Member) -> discord.Embed:
    total = rec.get("invites", 0) or 0
    left  = rec.get("left",    0) or 0
    fake  = rec.get("fake",    0) or 0
    net   = max(0, total - left - fake)
    next_m = _next_milestone(net)

    embed = discord.Embed(
        title=f"📈 Invite Progress — {member.display_name}",
        color=discord.Color.green(),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Current Net Invites", value=str(net), inline=True)

    if next_m:
        needed = next_m - net
        filled = int((net / next_m) * 20)
        bar = "█" * filled + "░" * (20 - filled)
        embed.add_field(name="Next Milestone", value=str(next_m), inline=True)
        embed.add_field(name="Still Needed",   value=str(needed), inline=True)
        embed.add_field(name="Progress", value=f"`{bar}` {net}/{next_m}", inline=False)
    else:
        embed.add_field(
            name="Status",
            value=f"🏆 All milestones reached! (max: {REWARD_MILESTONES[-1]})",
            inline=False,
        )

    milestones_str = " → ".join(str(m) for m in REWARD_MILESTONES)
    embed.set_footer(text=f"Milestones: {milestones_str}")
    return embed

# ─────────────────────────────────────────────────────────────────────────────
# Event handlers  (called from setup() via bot.add_listener)
# ─────────────────────────────────────────────────────────────────────────────

async def _cache_guild(guild: discord.Guild):
    try:
        invites = await guild.invites()
        _invite_cache[guild.id] = {inv.code: inv.uses for inv in invites}
    except Exception as e:
        print(f"⚠️  Invite tracker: could not cache invites for '{guild.name}': {e}")


async def _handle_ready(bot: discord.Client):
    for guild in bot.guilds:
        await _cache_guild(guild)
    print(f"✅ Invite tracker: cache loaded for {len(bot.guilds)} guild(s).")


async def _handle_guild_join(guild: discord.Guild):
    await _cache_guild(guild)


async def _handle_invite_create(invite: discord.Invite):
    if invite.guild:
        _invite_cache.setdefault(invite.guild.id, {})[invite.code] = invite.uses or 0


async def _handle_invite_delete(invite: discord.Invite):
    if invite.guild:
        _invite_cache.get(invite.guild.id, {}).pop(invite.code, None)


async def _handle_member_join(member: discord.Member):
    guild = member.guild
    cached = _invite_cache.get(guild.id, {})
    inviter_id: str | None = None

    # Fetch current invite list and diff against cache to find used code
    try:
        current_invites = await guild.invites()
    except Exception:
        current_invites = []

    for inv in current_invites:
        prev_uses = cached.get(inv.code, 0)
        if inv.uses > prev_uses and inv.inviter:
            inviter_id = str(inv.inviter.id)
            break

    # Refresh cache with latest counts
    _invite_cache[guild.id] = {inv.code: inv.uses for inv in current_invites}

    # Record join so we can detect fake leaves later
    _join_registry[(guild.id, member.id)] = (datetime.utcnow(), inviter_id)

    if not supabase:
        return

    # Ensure joiner has a record and set/update invitedBy
    existing = _get(str(member.id))
    joiner_patch: dict = {"invitedBy": inviter_id}
    if not existing:
        joiner_patch.update({"invites": 0, "left": 0, "fake": 0})
    _patch(str(member.id), joiner_patch)

    # Credit the inviter
    if inviter_id:
        _increment(inviter_id, invites=1)


async def _handle_member_remove(member: discord.Member):
    key = (member.guild.id, member.id)
    join_info = _join_registry.pop(key, None)

    if not supabase:
        return

    # Look up who invited this person
    rec = _get(str(member.id))
    if not rec:
        return
    inviter_id = rec.get("invitedBy")
    if not inviter_id:
        return

    # If they left within the fake threshold → fake invite, not a normal leave
    if join_info:
        joined_at, _ = join_info
        elapsed_minutes = (datetime.utcnow() - joined_at).total_seconds() / 60
        if elapsed_minutes <= FAKE_THRESHOLD_MIN:
            _increment(inviter_id, fake=1)
            return  # Do NOT also increment left — fake and left are mutually exclusive

    _increment(inviter_id, left=1)

# ─────────────────────────────────────────────────────────────────────────────
# setup()  — called once from bot.py to wire everything in
# ─────────────────────────────────────────────────────────────────────────────

def setup(bot):
    """
    Registers all invite-tracker event listeners and slash commands on the bot.
    Call this ONCE, before bot.run(), from bot.py.
    """

    # ── Event listeners ───────────────────────────────────────────────────────
    # We use bot.add_listener() so we don't overwrite the existing @bot.event
    # handlers for on_member_join / on_member_remove / on_invite_create that
    # the giveaway system already registers.

    async def _ready_wrapper():
        await _handle_ready(bot)

    async def _member_join_wrapper(member: discord.Member):
        await _handle_member_join(member)

    async def _member_remove_wrapper(member: discord.Member):
        await _handle_member_remove(member)

    bot.add_listener(_ready_wrapper,           "on_ready")
    bot.add_listener(_handle_invite_create,    "on_invite_create")
    bot.add_listener(_handle_invite_delete,    "on_invite_delete")
    bot.add_listener(_handle_guild_join,       "on_guild_join")
    bot.add_listener(_member_join_wrapper,     "on_member_join")
    bot.add_listener(_member_remove_wrapper,   "on_member_remove")

    # ── Slash commands ────────────────────────────────────────────────────────

    # ── /invites [user] ───────────────────────────────────────────────────────
    @bot.tree.command(name="invites", description="Check invite stats for yourself or another user")
    @app_commands.describe(user="User to check (leave blank for yourself)")
    async def cmd_invites(interaction: discord.Interaction, user: discord.Member = None):
        target = user or interaction.user
        if not supabase:
            await interaction.response.send_message(
                "❌ Invite tracking is not configured (missing Supabase credentials).", ephemeral=True
            )
            return
        rec = _ensure(str(target.id))
        await interaction.response.send_message(embed=_stats_embed(rec, target))

    # ── /progress [user] ─────────────────────────────────────────────────────
    @bot.tree.command(name="progress", description="Show progress toward next invite reward milestone")
    @app_commands.describe(user="User to check (leave blank for yourself)")
    async def cmd_progress(interaction: discord.Interaction, user: discord.Member = None):
        target = user or interaction.user
        if not supabase:
            await interaction.response.send_message(
                "❌ Invite tracking is not configured (missing Supabase credentials).", ephemeral=True
            )
            return
        rec = _ensure(str(target.id))
        await interaction.response.send_message(embed=_progress_embed(rec, target))

    # ── /leaderboard ──────────────────────────────────────────────────────────
    @bot.tree.command(name="leaderboard", description="Top users ranked by net invites")
    async def cmd_leaderboard(interaction: discord.Interaction):
        if not supabase:
            await interaction.response.send_message(
                "❌ Invite tracking is not configured (missing Supabase credentials).", ephemeral=True
            )
            return
        await interaction.response.defer()
        try:
            res = supabase.table(TABLE).select("*").execute()
            records = res.data or []
        except Exception as e:
            await interaction.followup.send(f"❌ Failed to fetch leaderboard: {e}", ephemeral=True)
            return

        # Sort by net invites descending
        def net_of(r):
            return max(0, (r.get("invites") or 0) - (r.get("left") or 0) - (r.get("fake") or 0))

        ranked = sorted(records, key=net_of, reverse=True)[:10]

        embed = discord.Embed(
            title="🏆 Invite Leaderboard",
            color=discord.Color.gold(),
            timestamp=datetime.utcnow(),
        )
        if not ranked:
            embed.description = "No invite data recorded yet."
        else:
            medals = ["🥇", "🥈", "🥉"]
            lines = []
            for i, r in enumerate(ranked):
                uid  = r["userId"]
                net  = net_of(r)
                tot  = r.get("invites") or 0
                lft  = r.get("left")    or 0
                fake = r.get("fake")    or 0
                prefix = medals[i] if i < 3 else f"`{i + 1}.`"
                lines.append(
                    f"{prefix} <@{uid}> — **{net}** net "
                    f"({tot} total · {lft} left · {fake} fake)"
                )
            embed.description = "\n".join(lines)

        await interaction.followup.send(embed=embed)

    # ── /addinvites user amount [reason] ──────────────────────────────────────
    @bot.tree.command(name="addinvites", description="Add invites to a user [Staff only]")
    @app_commands.describe(user="Target user", amount="Number of invites to add", reason="Reason (optional)")
    async def cmd_addinvites(
        interaction: discord.Interaction,
        user: discord.Member,
        amount: int,
        reason: str = None,
    ):
        if not _is_staff(interaction.user):
            await interaction.response.send_message("❌ You need the Staff role for this command.", ephemeral=True)
            return
        if amount <= 0:
            await interaction.response.send_message("❌ Amount must be a positive number.", ephemeral=True)
            return
        if not supabase:
            await interaction.response.send_message("❌ Invite tracking not configured.", ephemeral=True)
            return

        updated = _increment(str(user.id), invites=amount)
        net = max(0, (updated.get("invites") or 0) - (updated.get("left") or 0) - (updated.get("fake") or 0))
        detail = f"+{amount} invites" + (f" | Reason: {reason}" if reason else "")

        await interaction.response.send_message(
            f"✅ Added **{amount}** invite(s) to {user.mention}. "
            f"New net: **{net}**",
            ephemeral=True,
        )
        await _log(bot, "addinvites", interaction.user, user, detail)

    # ── /removeinvites user amount [reason] ───────────────────────────────────
    @bot.tree.command(name="removeinvites", description="Remove invites from a user [Staff only]")
    @app_commands.describe(user="Target user", amount="Number of invites to remove", reason="Reason (optional)")
    async def cmd_removeinvites(
        interaction: discord.Interaction,
        user: discord.Member,
        amount: int,
        reason: str = None,
    ):
        if not _is_staff(interaction.user):
            await interaction.response.send_message("❌ You need the Staff role for this command.", ephemeral=True)
            return
        if amount <= 0:
            await interaction.response.send_message("❌ Amount must be a positive number.", ephemeral=True)
            return
        if not supabase:
            await interaction.response.send_message("❌ Invite tracking not configured.", ephemeral=True)
            return

        rec = _ensure(str(user.id))
        current = rec.get("invites") or 0
        new_val = max(0, current - amount)
        actually_removed = current - new_val
        _patch(str(user.id), {"invites": new_val})

        net = max(0, new_val - (rec.get("left") or 0) - (rec.get("fake") or 0))
        detail = f"-{actually_removed} invites (requested -{amount})" + (f" | Reason: {reason}" if reason else "")

        await interaction.response.send_message(
            f"✅ Removed **{actually_removed}** invite(s) from {user.mention}. "
            f"New net: **{net}**",
            ephemeral=True,
        )
        await _log(bot, "removeinvites", interaction.user, user, detail)

    # ── /setinvites user amount ────────────────────────────────────────────────
    @bot.tree.command(name="setinvites", description="Set a user's total invite count [Staff only]")
    @app_commands.describe(user="Target user", amount="New total invite count")
    async def cmd_setinvites(
        interaction: discord.Interaction,
        user: discord.Member,
        amount: int,
    ):
        if not _is_staff(interaction.user):
            await interaction.response.send_message("❌ You need the Staff role for this command.", ephemeral=True)
            return
        if amount < 0:
            await interaction.response.send_message("❌ Amount cannot be negative.", ephemeral=True)
            return
        if not supabase:
            await interaction.response.send_message("❌ Invite tracking not configured.", ephemeral=True)
            return

        rec = _ensure(str(user.id))
        _patch(str(user.id), {"invites": amount})
        net = max(0, amount - (rec.get("left") or 0) - (rec.get("fake") or 0))

        await interaction.response.send_message(
            f"✅ Set {user.mention}'s invite count to **{amount}**. Net: **{net}**",
            ephemeral=True,
        )
        await _log(bot, "setinvites", interaction.user, user, f"set to {amount}")

    # ── /resetinvites user ────────────────────────────────────────────────────
    @bot.tree.command(name="resetinvites", description="Reset all invite data for a user [Staff only]")
    @app_commands.describe(user="Target user")
    async def cmd_resetinvites(
        interaction: discord.Interaction,
        user: discord.Member,
    ):
        if not _is_staff(interaction.user):
            await interaction.response.send_message("❌ You need the Staff role for this command.", ephemeral=True)
            return
        if not supabase:
            await interaction.response.send_message("❌ Invite tracking not configured.", ephemeral=True)
            return

        _patch(str(user.id), {"invites": 0, "left": 0, "fake": 0, "invitedBy": None})

        await interaction.response.send_message(
            f"✅ All invite data for {user.mention} has been reset to zero.",
            ephemeral=True,
        )
        await _log(bot, "resetinvites", interaction.user, user, "full reset — all fields set to 0")

    print("✅ Invite tracker: all slash commands registered.")
