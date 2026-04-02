# Discord Giveaway Bot

A professional Discord giveaway bot with a Flask web dashboard.

## Features

- `!giveaway` — Start a giveaway (interactive: prize, duration, winners)
- `!gend [id]` — End a giveaway early
- `!reroll [id]` — Reroll winners
- `!glist` — List active giveaways
- JOIN button (discord.py Views)
- Bonus entry roles (configured via dashboard)
- Web dashboard with login, role management, live monitor, history

## Setup

### 1. Create a Discord Bot

1. Go to https://discord.com/developers/applications
2. Create a new application → Bot section → copy the **Token**
3. Under **OAuth2 → URL Generator**, select:
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: `Send Messages`, `Embed Links`, `Read Message History`, `Manage Messages`, `Use External Emojis`
4. Use the generated URL to invite the bot to your server

### 2. Enable Intents

In the Discord Developer Portal → Bot section, enable:
- **Presence Intent**
- **Server Members Intent**
- **Message Content Intent**

### 3. Create the "Giveaway Host" Role

In your Discord server, create a role named exactly **`Giveaway Host`** and assign it to users who should be able to run giveaways.

### 4. Environment Variables

| Variable | Description |
|---|---|
| `DISCORD_BOT_TOKEN` | Your bot token from Discord Developer Portal |
| `SESSION_SECRET` | Random secret for Flask sessions |
| `DASHBOARD_PASSWORD` | Login password for the web dashboard |
| `DASHBOARD_USERNAME` | Login username (default: `admin`) |
| `PORT` | Port for the Flask dashboard (default: `5000`) |

### 5. Local Run

```bash
pip install -r requirements.txt
export DISCORD_BOT_TOKEN=your_token_here
export DASHBOARD_PASSWORD=Shivansh2222
python bot.py
```

## Deploy to Render

1. Push this folder to a GitHub repository
2. Go to https://render.com → New → Web Service
3. Connect your repository
4. Render will auto-detect `render.yaml`
5. Set the `DISCORD_BOT_TOKEN` and `DASHBOARD_PASSWORD` environment variables in Render's dashboard

The `/ping` endpoint is pre-configured for **UptimeRobot** to keep the free Render service alive. Add a monitor in UptimeRobot pointing to `https://your-app.onrender.com/ping`.

## Dashboard

Access the dashboard at `https://your-app.onrender.com/` and log in with your configured credentials.

- **Overview** — Stats and recent giveaways
- **Active Giveaways** — Monitor live giveaways with participant details (auto-refreshes every 30s)
- **Bonus Entry Roles** — Add/edit/delete role configurations
- **Giveaway History** — Full history with winners and entry breakdowns

## Bonus Entry System

Role configurations are stored in `role_config.json`. Users with multiple bonus roles receive the **highest** entry count among their roles. All roles are displayed in the giveaway embed automatically.

Default example:
- `VIP` → 3 entries
- `Server Booster` → 2 entries
- Everyone else → 1 entry (default)
