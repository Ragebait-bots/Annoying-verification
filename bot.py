import asyncio
import hashlib
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
from urllib.parse import quote

import discord
from discord.ext import commands, tasks
from flask import Flask, abort, jsonify, render_template_string, request
from werkzeug.serving import make_server

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
ALLOWED_ROLE_ID = int(os.environ["ALLOWED_ROLE_ID"])
VERIFIED_ROLE_ID = int(os.environ["VERIFIED_ROLE_ID"])
BASE_URL = os.environ["BASE_URL"].rstrip("/")
PORT = int(os.environ.get("PORT", "10000"))
DATABASE_PATH = os.environ.get("DATABASE_PATH", "verification.sqlite3")
DEADLINE_SECONDS = 15 * 60
CYCLE_SECONDS = 60 * 60

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

async def activate_verified(guild_id, user_id):
    guild = bot.get_guild(guild_id)
    if guild is None:
        raise RuntimeError(f"Guild {guild_id} not found")

    member = guild.get_member(user_id)
    if member is None:
        member = await guild.fetch_member(user_id)

    allowed_role = guild.get_role(ALLOWED_ROLE_ID)
    verified_role = guild.get_role(VERIFIED_ROLE_ID)

    if allowed_role is None or verified_role is None:
        raise RuntimeError("Allowed or Verified role not found")

    if allowed_role in member.roles:
        await member.remove_roles(
            allowed_role, reason="Member completed verification"
        )

    if verified_role not in member.roles:
        await member.add_roles(
            verified_role, reason="Member completed verification"
        )


app = Flask(__name__)
db_lock = threading.RLock()


def db():
    connection = sqlite3.connect(DATABASE_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    with db_lock, db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS member_state (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                last_verified REAL,
                last_prompt REAL,
                token_hash TEXT,
                deadline REAL,
                PRIMARY KEY (guild_id, user_id)
            )
        """)


def token_digest(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def state_for(guild_id, user_id):
    with db_lock, db() as con:
        row = con.execute(
            "SELECT * FROM member_state WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        ).fetchone()
        return dict(row) if row else None


def create_challenge(guild_id, user_id, now):
    token = secrets.token_urlsafe(32)
    deadline = now + DEADLINE_SECONDS
    with db_lock, db() as con:
        con.execute("""
            INSERT INTO member_state
                (guild_id, user_id, last_verified, last_prompt, token_hash, deadline)
            VALUES (?, ?, NULL, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                last_prompt=excluded.last_prompt,
                token_hash=excluded.token_hash,
                deadline=excluded.deadline
        """, (guild_id, user_id, now, token_digest(token), deadline))
    return token, deadline


def clear_expired(guild_id, user_id):
    with db_lock, db() as con:
        con.execute("""
            UPDATE member_state SET token_hash=NULL, deadline=NULL
            WHERE guild_id=? AND user_id=?
        """, (guild_id, user_id))


def complete_challenge(token):
    digest = token_digest(token)
    now = time.time()
    with db_lock, db() as con:
        row = con.execute("""
            SELECT guild_id, user_id, deadline FROM member_state
            WHERE token_hash=?
        """, (digest,)).fetchone()
        if not row:
            return False, "This verification link is invalid or has already been used."
        if row["deadline"] is None or now > row["deadline"]:
            con.execute("""
                UPDATE member_state SET token_hash=NULL, deadline=NULL
                WHERE guild_id=? AND user_id=?
            """, (row["guild_id"], row["user_id"]))
            return False, "This verification link has expired. Wait for your next prompt."
        con.execute("""
            UPDATE member_state
            SET last_verified=?, token_hash=NULL, deadline=NULL
            WHERE guild_id=? AND user_id=? AND token_hash=?
        """, (now, row["guild_id"], row["user_id"], digest))
    return True, "Verification complete! You are verified for this cycle."


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>Discord verification</title>
<style>
:root { color-scheme: dark; }
body { font-family: system-ui, sans-serif; background:#17191f; color:#f4f4f5;
       margin:0; min-height:100vh; display:grid; place-items:center; }
main { width:min(680px, 92vw); padding:24px; box-sizing:border-box; }
h1 { margin-bottom:8px; }
p { color:#c9cbd1; line-height:1.5; }
#grid { display:grid; grid-template-columns:repeat(10, minmax(18px, 1fr)); gap:9px;
        margin:24px 0; }
.circle { width:100%; aspect-ratio:1; border-radius:50%; border:0;
          background:#fff; cursor:pointer; padding:0; min-width:0; }
.circle.done { background:#555b66; box-shadow:inset 0 0 0 3px #aab0bc; }
.circle:focus-visible { outline:3px solid #8ab4ff; outline-offset:3px; }
#status { font-weight:600; min-height:1.5em; }
button#finish { padding:12px 18px; border:0; border-radius:9px; cursor:pointer;
                 background:#5865f2; color:white; font-weight:700; font-size:1rem; }
button:disabled { opacity:.5; cursor:not-allowed; }
.small { font-size:.9rem; }
</style>
</head>
<body>
<main>
  <h1>Hourly verification</h1>
  <p>Click all 100 white circles. You have 15 minutes to complete this challenge.
     This page only records clicks on the circles below.</p>
  <div id="grid" aria-label="100 white circles"></div>
  <p id="status" role="status" aria-live="polite">0 / 100 completed</p>
  <button id="finish" disabled>Finish verification</button>
  <p id="result" class="small" role="status"></p>
</main>
<script>
const token = {{ token|tojson }};
const grid = document.getElementById("grid");
const status = document.getElementById("status");
const finish = document.getElementById("finish");
const result = document.getElementById("result");
let completed = 0;
const clicked = new Set();

for (let i = 0; i < 100; i++) {
  const circle = document.createElement("button");
  circle.type = "button";
  circle.className = "circle";
  circle.setAttribute("aria-label", "Circle " + (i + 1));
  circle.addEventListener("click", () => {
    if (clicked.has(i)) return;
    clicked.add(i);
    completed++;
    circle.classList.add("done");
    circle.setAttribute("aria-pressed", "true");
    status.textContent = completed + " / 100 completed";
    finish.disabled = completed !== 100;
  });
  grid.appendChild(circle);
}
finish.addEventListener("click", async () => {
  finish.disabled = true;
  result.textContent = "Checking verification…";
  try {
    const response = await fetch("/api/complete", {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({token, completed})
    });
    const data = await response.json();
    result.textContent = data.message;
    if (data.ok) {
      grid.querySelectorAll("button").forEach(b => b.disabled = true);
      status.textContent = "100 / 100 completed";
    } else {
      finish.disabled = completed !== 100;
    }
  } catch {
    result.textContent = "Could not contact the server. Please try again.";
    finish.disabled = completed !== 100;
  }
});
</script>
</body>
</html>"""


@app.get("/")
def home():
    return (
        "<h2>Hourly role verification service</h2>"
        "<p>The service is running. Verification links are sent by the Discord bot.</p>"
    )


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.get("/verify/<token>")
def verify_page(token):
    if len(token) > 100:
        abort(404)
    digest = token_digest(token)
    with db_lock, db() as con:
        row = con.execute(
            "SELECT deadline FROM member_state WHERE token_hash=?", (digest,)
        ).fetchone()
    if not row or row["deadline"] is None or time.time() > row["deadline"]:
        return "<h2>Link expired or already used</h2><p>Wait for your next Discord prompt.</p>", 410
    response = app.make_response(render_template_string(PAGE, token=token))
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    )
    return response


@app.post("/api/complete")
def api_complete():
    data = request.get_json(silent=True) or {}
    token = data.get("token", "")
    completed = data.get("completed")
    if not isinstance(token, str) or len(token) > 100 or completed != 100:
        return jsonify({"ok": False, "message": "Please click all 100 circles first."}), 400
    ok, message = complete_challenge(token)
    return jsonify({"ok": ok, "message": message}), (200 if ok else 400)


class FlaskThread(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.server = make_server("0.0.0.0", PORT, app, threaded=True)

    def run(self):
        self.server.serve_forever()


async def send_challenge(member, now):
    token, deadline = create_challenge(member.guild.id, member.id, now)
    link = f"{BASE_URL}/verify/{quote(token, safe='')}"
    deadline_text = datetime.fromtimestamp(deadline, timezone.utc).strftime("%H:%M UTC")
    embed = discord.Embed(
        title="Hourly verification required",
        description=(
            "Click the link below and complete the 100-circle challenge within **15 minutes**.\n\n"
            f"[Open verification challenge]({link})\n\n"
            f"Deadline: **{deadline_text}**. If you miss it, you'll receive a warning; "
            "your role will not be removed."
        ),
        color=discord.Color.blurple(),
    )
    try:
        await member.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        # Keep the challenge timer; tell staff in the service logs instead of spamming.
        print(f"Could not DM member {member.id}; they may have DMs disabled.")


@tasks.loop(minutes=1)
async def verification_scheduler():
    await bot.wait_until_ready()
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        print(f"Guild {GUILD_ID} is unavailable; check GUILD_ID and bot membership.")
        return
    now = time.time()
    role = guild.get_role(ROLE_ID)
    if role is None:
        print(f"Role {ROLE_ID} not found in guild {GUILD_ID}.")
        return

    for member in list(role.members):
        state = state_for(guild.id, member.id)

        if state and state["token_hash"] and state["deadline"] and now > state["deadline"]:
            clear_expired(guild.id, member.id)
            try:
                await member.send(
                    "⚠️ You missed your 15-minute verification deadline. "
                    "Your role has **not** been removed. You'll be prompted again next cycle."
                )
            except (discord.Forbidden, discord.HTTPException):
                print(f"Could not DM deadline warning to member {member.id}.")
            # State has a recent last_prompt, so don't immediately send another challenge.
            continue

        if state and state["token_hash"]:
            continue  # an active challenge already exists

        last_verified = state["last_verified"] if state else None
        last_prompt = state["last_prompt"] if state else None
        anchor = max(x for x in (last_verified, last_prompt) if x is not None) if (
            last_verified is not None or last_prompt is not None
        ) else None

        if anchor is None or now - anchor >= CYCLE_SECONDS:
            await send_challenge(member, now)
            await asyncio.sleep(0.25)  # modestly space out DMs


@verification_scheduler.before_loop
async def before_scheduler():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    if not verification_scheduler.is_running():
        verification_scheduler.start()


@bot.event
async def on_member_remove(member):
    with db_lock, db() as con:
        con.execute(
            "DELETE FROM member_state WHERE guild_id=? AND user_id=?",
            (member.guild.id, member.id),
        )


def main():
    init_db()
    FlaskThread().start()
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
