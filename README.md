# Hourly Discord Role Verification Bot

A Python Discord bot plus a small Flask verification website. Members who hold a configured role receive a DM every hour and must click 100 white circles within 15 minutes. Missing the deadline sends a warning; it does not remove their role.

## Important Render free-plan limitation

Render free web services can spin down after 15 minutes without inbound traffic, and their local filesystem is ephemeral. A Discord Gateway connection alone may not keep the web service awake. This means the bot may miss hourly checks or lose its SQLite state after a restart/spin-down. Free hosting is useful for testing, but it is **not reliable for an always-on hourly bot**. For reliable operation, use an always-on host and persistent storage (or adapt this project to a database). See Render's official free-service limitations: https://render.com/docs/free

## 1. Create the Discord bot

1. Open https://discord.com/developers/applications and create an application.
2. Go to **Bot** and add a bot if needed.
3. Copy its token. Keep it secret; never commit it to GitHub or send it to anyone.
4. Under **Privileged Gateway Intents**, enable **Server Members Intent**.
5. Under **OAuth2 → URL Generator**, select the `bot` scope and at least `View Channels` if desired. The bot needs no server role-management permission because it only warns members by DM. Invite it to your server.
6. Make sure the bot and members share the server, and note the server ID and target role ID. In Discord, enable Developer Mode under Advanced, then right-click the server and role to copy IDs.

## 2. Configure environment variables

Set these in your local shell or in Render's Environment page:

- `DISCORD_TOKEN` — bot token from the Developer Portal
- `GUILD_ID` — numeric server ID
- `ROLE_ID` — numeric role ID whose members must verify
- `BASE_URL` — public HTTPS URL of the deployed Render service, e.g. `https://your-service.onrender.com`
- `PORT` — optional; Render supplies this automatically
- `DATABASE_PATH` — optional; defaults to `verification.sqlite3`

Do not include quotes around values in Render's environment-variable form. Set `BASE_URL` after Render gives you the service URL, then redeploy.

## 3. Deploy on Render

1. Put `bot.py`, `requirements.txt`, `render.yaml`, and `.gitignore` in a GitHub repository.
2. In Render, choose **New → Blueprint** and connect the repository, or create a Web Service manually.
3. Use build command `pip install -r requirements.txt` and start command `python bot.py`.
4. Choose the Free plan for testing.
5. Add the environment variables listed above. `BASE_URL` must be the HTTPS URL Render assigns to the service.
6. Deploy and inspect logs. You should see `Logged in as ...`.
7. Open `https://your-service.onrender.com/health`; it should return `{"ok":true}`.

## 4. Local test (Windows PowerShell)

Install Python 3.11 or newer, then in the project folder:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:DISCORD_TOKEN="paste-your-bot-token-here"
$env:GUILD_ID="123456789012345678"
$env:ROLE_ID="123456789012345678"
$env:BASE_URL="http://127.0.0.1:10000"
py bot.py
```

For local testing, open `http://127.0.0.1:10000`. Members can only access a localhost link from their own computer, so use the deployed HTTPS URL for real members.

## Behavior and notes

- The scheduler checks every minute and sends a challenge when a role member is due.
- The first challenge is sent on the next scheduler check after startup.
- Each challenge link is random, member-specific, expires after 15 minutes, and is single-use.
- If DMs are disabled, the bot logs that it could not deliver the prompt. The member will not see the challenge.
- The browser reports completion after the 100 circles have been clicked. This is a lightweight engagement challenge, not strong proof against a technically savvy user.
- The database uses SQLite. On Render Free, SQLite data can disappear after restarts or spin-downs. Do not treat this as durable verification history.
- Keep your token private. If it leaks, reset it in the Discord Developer Portal immediately.
