import json
import time
import discord
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from config import (
    TOKEN_FILE, TOKENS_DIR, SCOPES,
    REDIRECT_URI, WEB_CLIENT_ID, WEB_CLIENT_SECRET
)

# Αποθηκεύει pending OAuth flows: {discord_user_id: {"flow": flow, "ts": timestamp}}
pending_flows = {}
FLOW_TTL = 600  # flows λήγουν μετά 10 λεπτά


def _cleanup_flows():
    now = time.time()
    expired = [uid for uid, v in pending_flows.items() if now - v["ts"] > FLOW_TTL]
    for uid in expired:
        del pending_flows[uid]


def token_path(user_id: int) -> Path:
    return TOKENS_DIR / f"{user_id}.json"


def get_user_creds(user_id: int):
    """Επιστρέφει credentials για τον χρήστη.
    Πρώτα ψάχνει στο tokens/ φάκελο (multi-user).
    Αν δεν βρει, χρησιμοποιεί το παλιό token.json (admin fallback)."""

    # Multi-user token
    path = token_path(user_id)
    if path.exists():
        try:
            creds = Credentials.from_authorized_user_info(
                json.loads(path.read_text()), SCOPES
            )
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                path.write_text(creds.to_json())
            if creds.valid:
                return creds
        except Exception:
            pass

    # Admin fallback
    if Path(TOKEN_FILE).exists():
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                with open(TOKEN_FILE, "w") as f:
                    f.write(creds.to_json())
            if creds.valid:
                return creds
        except Exception:
            pass

    return None


def save_user_creds(user_id: int, creds: Credentials):
    token_path(user_id).write_text(creds.to_json())


def create_oauth_flow() -> Flow:
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id":     WEB_CLIENT_ID,
                "client_secret": WEB_CLIENT_SECRET,
                "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
                "token_uri":     "https://oauth2.googleapis.com/token",
                "redirect_uris": [REDIRECT_URI],
            }
        },
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )
    return flow


async def handle_auth_commands(message: discord.Message) -> bool:
    """Χειρίζεται τα auth commands.
    Επιστρέφει True αν το μήνυμα ήταν auth command, False αν όχι."""

    _cleanup_flows()

    user_id = message.author.id
    content = message.content.strip()

    # ── !connect ──────────────────────────────────────────────────────
    if content.lower() == "!connect":
        flow = create_oauth_flow()
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            prompt="consent"
        )
        pending_flows[user_id] = {"flow": flow, "ts": time.time()}
        try:
            await message.author.send(
                f"🔗 **Connect your Google Account**\n\n"
                f"1. Click the link below\n"
                f"2. Sign in with your Google account\n"
                f"3. Copy the code from the URL bar after redirect\n"
                f"4. Come back here and type: `!code YOUR_CODE`\n\n"
                f"{auth_url}"
            )
            await message.channel.send(
                f"✅ {message.author.mention} I sent you instructions via DM!"
            )
        except discord.Forbidden:
            await message.channel.send(
                f"⚠️ {message.author.mention} I can't send you a DM. "
                f"Please enable Direct Messages and try again."
            )
        return True

    # ── !code ─────────────────────────────────────────────────────────
    if content.lower().startswith("!code "):
        code = content[6:].strip()
        entry = pending_flows.get(user_id)
        if not entry:
            await message.channel.send(
                "⚠️ Session expired or not found. Please type `!connect` again."
            )
            return True
        flow = entry["flow"]
        try:
            flow.fetch_token(code=code)
            save_user_creds(user_id, flow.credentials)
            del pending_flows[user_id]
            await message.channel.send(
                f"✅ {message.author.mention} Your Google account has been connected successfully!"
            )
        except Exception as e:
            await message.channel.send(
                f"❌ Error: {str(e)}\nPlease try again with `!connect`."
            )
        return True

    # ── !status ───────────────────────────────────────────────────────
    if content.lower() == "!status":
        creds = get_user_creds(user_id)
        if creds:
            await message.channel.send(
                f"✅ {message.author.mention} Your Google account is connected!"
            )
        else:
            await message.channel.send(
                f"⚠️ {message.author.mention} You haven't connected a Google account yet. Type `!connect`."
            )
        return True

    # ── !disconnect ───────────────────────────────────────────────────
    if content.lower() == "!disconnect":
        path = token_path(user_id)
        if path.exists():
            path.unlink()
            await message.channel.send(
                f"✅ {message.author.mention} Your Google account has been disconnected."
            )
        else:
            await message.channel.send(
                f"⚠️ {message.author.mention} You don't have a connected account."
            )
        return True

    return False
