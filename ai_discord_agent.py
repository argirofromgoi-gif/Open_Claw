import asyncio
import json
import base64
import discord
import aiohttp
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone

from openai import AsyncOpenAI
from googleapiclient.discovery import build

from config import (
    DISCORD_TOKEN,
    OPENAI_API_KEY,
    BRAVE_API_KEY,
    SCOPES,
    CHANNEL_MODELS,
    DEFAULT_MODEL,
    CHANNEL_PROMPTS,
    DEFAULT_PROMPT,
)
from auth import get_user_creds, handle_auth_commands
from translator import handle_translate_command
import anthropic
from config import ANTHROPIC_API_KEY
from logger import (
    log_message_received,
    log_tool_called,
    log_reply_sent,
    log_error,
    log_bot_started,
    log_api_call,
)
from video_handler import handle_video_request
from image_generator import handle_image_request
from claude_code_bridge import handle_claude_code_channel, CLAUDE_CODE_CHANNEL_ID
from file_handler import handle_file_attachment
from memory import add_to_history, get_history, clear_history
client = AsyncOpenAI(api_key=OPENAI_API_KEY)
anthropic_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)


# =========================
# RATE LIMITING
# =========================

_rate_limit = {}  # {user_id: [timestamp, ...]}
RATE_LIMIT_MAX = 10   # max requests
RATE_LIMIT_WINDOW = 60  # per 60 seconds
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB


def _is_rate_limited(user_id: int) -> bool:
    now = datetime.now(timezone.utc).timestamp()
    timestamps = [t for t in _rate_limit.get(user_id, []) if now - t < RATE_LIMIT_WINDOW]
    _rate_limit[user_id] = timestamps
    if len(timestamps) >= RATE_LIMIT_MAX:
        return True
    _rate_limit[user_id].append(now)
    return False


# =========================
# MEMORY
# =========================

file_registry = {}


def get_registry(channel_id):
    if channel_id not in file_registry:
        file_registry[channel_id] = {
            "last_doc": None,
            "last_sheet": None,
            "docs": {},
            "sheets": {},
        }
    return file_registry[channel_id]


def register_doc(channel_id, title, doc_id):
    reg = get_registry(channel_id)
    reg["docs"][title.lower()] = doc_id
    reg["last_doc"] = {"title": title, "id": doc_id}


def register_sheet(channel_id, title, sheet_id):
    reg = get_registry(channel_id)
    reg["sheets"][title.lower()] = sheet_id
    reg["last_sheet"] = {"title": title, "id": sheet_id}


def find_doc_id(channel_id, title=None):
    reg = get_registry(channel_id)
    if title:
        return reg["docs"].get(title.lower())
    if reg["last_doc"]:
        return reg["last_doc"]["id"]
    return None


def find_sheet_id(channel_id, title=None):
    reg = get_registry(channel_id)
    if title:
        return reg["sheets"].get(title.lower())
    if reg["last_sheet"]:
        return reg["last_sheet"]["id"]
    return None


def build_context_note(channel_id):
    reg = get_registry(channel_id)
    lines = []
    if reg["docs"]:
        lines.append(f"Known Docs: {', '.join(reg['docs'].keys())}.")
    if reg["last_doc"]:
        lines.append(f"Last Doc: \"{reg['last_doc']['title']}\".")
    if reg["sheets"]:
        lines.append(f"Known Sheets: {', '.join(reg['sheets'].keys())}.")
    if reg["last_sheet"]:
        lines.append(f"Last Sheet: \"{reg['last_sheet']['title']}\".")
    return " ".join(lines)


# =========================
# GOOGLE SERVICES
# =========================


def get_drive(user_id):
    return build("drive", "v3", credentials=get_user_creds(user_id))


def get_docs(user_id):
    return build("docs", "v1", credentials=get_user_creds(user_id))


def get_sheets(user_id):
    return build("sheets", "v4", credentials=get_user_creds(user_id))


def get_gmail(user_id):
    return build("gmail", "v1", credentials=get_user_creds(user_id))


def get_calendar(user_id):
    return build("calendar", "v3", credentials=get_user_creds(user_id))


# =========================
# BRAVE SEARCH
# =========================


async def brave_search(query):
    url = "https://api.search.brave.com/res/v1/web/search"
    headers = {"Accept": "application/json", "X-Subscription-Token": BRAVE_API_KEY}
    params = {"q": query, "count": 5}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, params=params) as r:
            data = await r.json()
    results = []
    for item in data.get("web", {}).get("results", []):
        results.append(item["title"] + " - " + item.get("description", ""))
    return "\n".join(results) if results else "No results found."


# =========================
# GOOGLE DOCS
# =========================


async def create_doc(title, user_id):
    loop = asyncio.get_event_loop()
    drive = get_drive(user_id)
    file = await loop.run_in_executor(
        None,
        lambda: drive.files()
        .create(
            body={"name": title, "mimeType": "application/vnd.google-apps.document"},
            fields="id",
        )
        .execute(),
    )
    return file["id"]


async def write_doc(doc_id, text, user_id):
    if not text or not text.strip():
        return
    loop = asyncio.get_event_loop()
    docs = get_docs(user_id)
    await loop.run_in_executor(
        None,
        lambda: docs.documents()
        .batchUpdate(
            documentId=doc_id,
            body={
                "requests": [{"insertText": {"location": {"index": 1}, "text": text}}]
            },
        )
        .execute(),
    )


async def drive_find_file(name, user_id):
    loop = asyncio.get_event_loop()
    drive = get_drive(user_id)
    res = await loop.run_in_executor(
        None,
        lambda: drive.files()
        .list(q=f"name='{name}' and trashed=false", fields="files(id,name,mimeType)")
        .execute(),
    )
    files = res.get("files", [])
    return files[0] if files else None


async def read_doc_by_url(url, user_id):
    import re

    match = re.search(r"/document/d/([a-zA-Z0-9_-]+)", url)
    doc_id = match.group(1) if match else url.strip()
    loop = asyncio.get_event_loop()
    docs = get_docs(user_id)
    doc = await loop.run_in_executor(
        None, lambda: docs.documents().get(documentId=doc_id).execute()
    )
    parts = []
    for el in doc.get("body", {}).get("content", []):
        para = el.get("paragraph")
        if para:
            for pe in para.get("elements", []):
                tr = pe.get("textRun")
                if tr:
                    parts.append(tr.get("content", ""))
    return "".join(parts)


# =========================
# GOOGLE SHEETS
# =========================


async def create_sheet(title, user_id):
    loop = asyncio.get_event_loop()
    drive = get_drive(user_id)
    file = await loop.run_in_executor(
        None,
        lambda: drive.files()
        .create(
            body={"name": title, "mimeType": "application/vnd.google-apps.spreadsheet"},
            fields="id",
        )
        .execute(),
    )
    return file["id"]


async def write_sheet(sheet_id, values, user_id):
    loop = asyncio.get_event_loop()
    sheets = get_sheets(user_id)
    await loop.run_in_executor(
        None,
        lambda: sheets.spreadsheets()
        .values()
        .update(
            spreadsheetId=sheet_id,
            range="A1",
            valueInputOption="RAW",
            body={"values": values},
        )
        .execute(),
    )


# =========================
# GMAIL
# =========================


async def gmail_send(to, subject, body, user_id, reply_to_id=None):
    loop = asyncio.get_event_loop()
    gmail = get_gmail(user_id)
    msg = MIMEMultipart()
    msg["To"] = to
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    payload = {"raw": raw}
    if reply_to_id:
        payload["threadId"] = reply_to_id
    result = await loop.run_in_executor(
        None, lambda: gmail.users().messages().send(userId="me", body=payload).execute()
    )
    return result.get("id")


async def gmail_list(max_results=5, query="", user_id=None):
    loop = asyncio.get_event_loop()
    gmail = get_gmail(user_id)
    q = query if query else "is:inbox"
    res = await loop.run_in_executor(
        None,
        lambda: gmail.users()
        .messages()
        .list(userId="me", q=q, maxResults=max_results)
        .execute(),
    )
    messages = res.get("messages", [])
    emails = []
    for m in messages:
        msg = await loop.run_in_executor(
            None,
            lambda mid=m["id"]: gmail.users()
            .messages()
            .get(
                userId="me",
                id=mid,
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            )
            .execute(),
        )
        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        emails.append(
            {
                "id": msg["id"],
                "thread": msg["threadId"],
                "from": headers.get("From", ""),
                "subject": headers.get("Subject", ""),
                "date": headers.get("Date", ""),
                "snippet": msg.get("snippet", ""),
            }
        )
    return emails


async def gmail_read(message_id, user_id):
    loop = asyncio.get_event_loop()
    gmail = get_gmail(user_id)
    msg = await loop.run_in_executor(
        None,
        lambda: gmail.users()
        .messages()
        .get(userId="me", id=message_id, format="full")
        .execute(),
    )
    headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
    body = ""
    parts = msg["payload"].get("parts", [])
    if parts:
        for part in parts:
            if part["mimeType"] == "text/plain":
                data = part["body"].get("data", "")
                body = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
                break
    else:
        data = msg["payload"]["body"].get("data", "")
        if data:
            body = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
    return {
        "id": msg["id"],
        "thread": msg["threadId"],
        "from": headers.get("From", ""),
        "subject": headers.get("Subject", ""),
        "date": headers.get("Date", ""),
        "body": body[:3000],
    }


# =========================
# GOOGLE CALENDAR
# =========================


async def calendar_list_events(max_results=5, user_id=None):
    loop = asyncio.get_event_loop()
    cal = get_calendar(user_id)
    now = datetime.now(timezone.utc).isoformat()
    res = await loop.run_in_executor(
        None,
        lambda: cal.events()
        .list(
            calendarId="primary",
            timeMin=now,
            maxResults=max_results,
            singleEvents=True,
            orderBy="startTime",
        )
        .execute(),
    )
    result = []
    for e in res.get("items", []):
        start = e["start"].get("dateTime", e["start"].get("date", ""))
        result.append(
            {
                "id": e["id"],
                "title": e.get("summary", "(no title)"),
                "start": start,
                "end": e["end"].get("dateTime", e["end"].get("date", "")),
                "location": e.get("location", ""),
            }
        )
    return result


async def calendar_create_event(
    title, start, end, user_id, description="", location=""
):
    loop = asyncio.get_event_loop()
    cal = get_calendar(user_id)
    event = {
        "summary": title,
        "description": description,
        "location": location,
        "start": {"dateTime": start, "timeZone": "Europe/Athens"},
        "end": {"dateTime": end, "timeZone": "Europe/Athens"},
    }
    result = await loop.run_in_executor(
        None, lambda: cal.events().insert(calendarId="primary", body=event).execute()
    )
    return result.get("id"), result.get("htmlLink", "")


async def calendar_delete_event(event_id, user_id):
    loop = asyncio.get_event_loop()
    cal = get_calendar(user_id)
    await loop.run_in_executor(
        None,
        lambda: cal.events().delete(calendarId="primary", eventId=event_id).execute(),
    )


_OWN_FILE = os.path.abspath(__file__)


async def read_own_code():
    with open(_OWN_FILE, "r") as f:
        return f.read()


async def edit_own_code(find_text, replace_text):
    import shutil
    import subprocess

    backup_dir = os.path.join(os.path.dirname(_OWN_FILE), "backup")
    os.makedirs(backup_dir, exist_ok=True)
    shutil.copy(_OWN_FILE, os.path.join(backup_dir, "ai_discord_agent_auto.py"))

    with open(_OWN_FILE, "r") as f:
        content = f.read()
    if find_text not in content:
        return f"❌ Text not found: {find_text[:50]}"
    new_content = content.replace(find_text, replace_text, 1)
    with open(_OWN_FILE, "w") as f:
        f.write(new_content)
    subprocess.Popen(["sudo", "systemctl", "restart", "discord-bot"])
    return "✅ Code updated and bot restarting..."


# =========================
# TOOLS
# =========================

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "Search the internet for current information.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_document",
            "description": "Create a NEW Google Doc with content inside. Only if it does not exist yet.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["title", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_to_document",
            "description": "Append text to an existing Google Doc. If no title given, uses the last doc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_sheet",
            "description": "Create a new Google Sheet.",
            "parameters": {
                "type": "object",
                "properties": {"title": {"type": "string"}},
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_sheet",
            "description": "Write data into a Google Sheet. Creates it if it does not exist.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "values": {
                        "type": "array",
                        "items": {"type": "array", "items": {}},
                    },
                },
                "required": ["values"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "gmail_send",
            "description": "Send an email via Gmail.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                    "reply_to_id": {"type": "string"},
                },
                "required": ["to", "subject", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "gmail_list",
            "description": "List recent emails from Gmail inbox.",
            "parameters": {
                "type": "object",
                "properties": {
                    "max_results": {"type": "integer"},
                    "query": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "gmail_read",
            "description": "Read the full content of a specific email by its ID.",
            "parameters": {
                "type": "object",
                "properties": {"message_id": {"type": "string"}},
                "required": ["message_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calendar_list",
            "description": "List upcoming events from Google Calendar.",
            "parameters": {
                "type": "object",
                "properties": {"max_results": {"type": "integer"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calendar_create",
            "description": "Create a new event in Google Calendar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "start": {
                        "type": "string",
                        "description": "ISO 8601 e.g. 2025-06-01T10:00:00",
                    },
                    "end": {
                        "type": "string",
                        "description": "ISO 8601 e.g. 2025-06-01T11:00:00",
                    },
                    "description": {"type": "string"},
                    "location": {"type": "string"},
                },
                "required": ["title", "start", "end"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calendar_delete",
            "description": "Delete a Google Calendar event by its ID.",
            "parameters": {
                "type": "object",
                "properties": {"event_id": {"type": "string"}},
                "required": ["event_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_document_by_url",
            "description": "Read the content of a Google Doc from a URL or document ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Google Doc URL or document ID",
                    }
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_own_code",
            "description": "Read the bot's own source code.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_own_code",
            "description": "Edit the bot's own source code with find and replace. Always makes a backup first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "find_text": {"type": "string"},
                    "replace_text": {"type": "string"},
                },
                "required": ["find_text", "replace_text"],
            },
        },
    },
]

# =========================
# TOOL EXECUTOR
# =========================


async def execute_tool(name, args, channel_id, user_id):
    log_tool_called(channel_id, name, "")

    if name == "search_web":
        return await brave_search(args["query"])

    if name == "create_document":
        title = args["title"]
        content = args.get("content", "")
        existing_id = find_doc_id(channel_id, title)
        if existing_id:
            await write_doc(existing_id, "\n\n" + content, user_id)
            register_doc(channel_id, title, existing_id)
            return f'Doc "{title}" already existed — content added.\nhttps://docs.google.com/document/d/{existing_id}/edit'
        doc_id = await create_doc(title, user_id)
        await write_doc(doc_id, content, user_id)
        register_doc(channel_id, title, doc_id)
        return f"https://docs.google.com/document/d/{doc_id}/edit"

    if name == "append_to_document":
        title = args.get("title", "")
        content = args.get("content", "")
        doc_id = find_doc_id(channel_id, title if title else None)
        if not doc_id and title:
            file = await drive_find_file(title, user_id)
            if file:
                doc_id = file["id"]
                register_doc(channel_id, title, doc_id)
        if not doc_id:
            return f'Could not find doc "{title}".'
        await write_doc(doc_id, "\n\n" + content, user_id)
        return f"Content added.\nhttps://docs.google.com/document/d/{doc_id}/edit"

    if name == "create_sheet":
        title = args["title"]
        sheet_id = await create_sheet(title, user_id)
        register_sheet(channel_id, title, sheet_id)
        return f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"

    if name == "write_sheet":
        title = args.get("title", "")
        values = args["values"]
        sheet_id = find_sheet_id(channel_id, title if title else None)
        if not sheet_id and title:
            file = await drive_find_file(title, user_id)
            if file:
                sheet_id = file["id"]
                register_sheet(channel_id, title, sheet_id)
        if not sheet_id:
            if not title:
                return "No sheet found and no title provided."
            sheet_id = await create_sheet(title, user_id)
            register_sheet(channel_id, title, sheet_id)
        await write_sheet(sheet_id, values, user_id)
        return f"Data written.\nhttps://docs.google.com/spreadsheets/d/{sheet_id}/edit"

    if name == "gmail_send":
        msg_id = await gmail_send(
            args["to"], args["subject"], args["body"], user_id, args.get("reply_to_id")
        )
        return f"Email sent! ID: `{msg_id}`"

    if name == "gmail_list":
        emails = await gmail_list(
            args.get("max_results", 5), args.get("query", ""), user_id
        )
        if not emails:
            return "No emails found."
        lines = []
        for e in emails:
            lines.append(
                f"**{e['subject']}**\n"
                f"From: {e['from']} | {e['date']}\n"
                f"ID: `{e['id']}`\n"
                f"{e['snippet'][:100]}"
            )
        return "\n\n".join(lines)

    if name == "gmail_read":
        e = await gmail_read(args["message_id"], user_id)
        return (
            f"**{e['subject']}**\n"
            f"From: {e['from']}\nDate: {e['date']}\n"
            f"Thread ID: `{e['thread']}`\n\n"
            f"{e['body']}"
        )

    if name == "calendar_list":
        events = await calendar_list_events(args.get("max_results", 5), user_id)
        if not events:
            return "No upcoming events."
        lines = []
        for e in events:
            lines.append(
                f"**{e['title']}**\n"
                f"Start: {e['start']} | End: {e['end']}\n"
                f"ID: `{e['id']}`" + (f"\n📍 {e['location']}" if e["location"] else "")
            )
        return "\n\n".join(lines)

    if name == "calendar_create":
        event_id, link = await calendar_create_event(
            args["title"],
            args["start"],
            args["end"],
            user_id,
            args.get("description", ""),
            args.get("location", ""),
        )
        return f"Event created: **{args['title']}**\nID: `{event_id}`\n{link}"

    if name == "calendar_delete":
        await calendar_delete_event(args["event_id"], user_id)
        return f"Event `{args['event_id']}` deleted."

    if name == "read_document_by_url":
        content = await read_doc_by_url(args["url"], user_id)
        return content[:4000]
    if name == "read_own_code":
        return await read_own_code()

    if name == "edit_own_code":
        return await edit_own_code(args["find_text"], args["replace_text"])
    log_tool_called(channel_id, name, "")
    return f"Unknown tool: {name}"


# =========================
# AGENTIC LOOP
# =========================


async def run_agent(user_text, channel_id, user_id):
    add_to_history(channel_id, "user", user_text)

    tool_was_used = False
    final_reply = None
    context_note = build_context_note(channel_id)
    from config import get_default_prompt

    system_content = CHANNEL_PROMPTS.get(channel_id, get_default_prompt())
    if context_note:
        system_content += f"\n\nContext: {context_note}"

    messages = [{"role": "system", "content": system_content}] + get_history(channel_id)
    model = CHANNEL_MODELS.get(channel_id, DEFAULT_MODEL)

    for _ in range(8):
        if model.startswith("claude"):
            response_msg = await anthropic_client.messages.create(
                model=model,
                max_tokens=8000,
                system=[{"type": "text", "text": system_content, "cache_control": {"type": "ephemeral"}}],
                messages=[m for m in messages if m["role"] != "system"],
                tools=[
                    {
                        "name": t["function"]["name"],
                        "description": t["function"]["description"],
                        "input_schema": t["function"]["parameters"],
                    }
                    for t in TOOLS
                ],
            )

            class FakeMsg:
                def __init__(self, content, tool_calls):
                    self.content = content
                    self.tool_calls = tool_calls

            class FakeTC:
                def __init__(self, tc):
                    self.id = tc.id
                    self.function = type(
                        "F", (), {"name": tc.name, "arguments": json.dumps(tc.input)}
                    )()

            tool_calls = [
                FakeTC(b) for b in response_msg.content if b.type == "tool_use"
            ]
            text = next(
                (b.text for b in response_msg.content if b.type == "text"), None
            )
            msg = FakeMsg(text, tool_calls if tool_calls else None)
            log_api_call(channel_id, response_msg.usage.input_tokens, response_msg.usage.output_tokens)
        else:
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                **(
                    {"max_completion_tokens": 8000}
                    if "5." in model
                    else {"max_tokens": 8000}
                ),
            )
            msg = response.choices[0].message
            if response.usage:
                log_api_call(channel_id, response.usage.prompt_tokens, response.usage.completion_tokens)
        if not msg.tool_calls:
            if tool_was_used:
                content = msg.content or ""
                if (
                    "token.json" in content
                    or "your mac" in content.lower()
                    or "run this" in content.lower()
                ):
                    break
                if content:
                    final_reply = (final_reply or "") + "\n\n" + content
            else:
                final_reply = msg.content or "Done."
            break
        if model.startswith("claude"):
            messages.append({"role": "assistant", "content": response_msg.content})
        else:
            messages.append(msg)
        tool_was_used = True

        for tc in msg.tool_calls:
            tool_name = tc.function.name
            tool_args = json.loads(tc.function.arguments)
            tool_result = await execute_tool(tool_name, tool_args, channel_id, user_id)
            result_str = str(tool_result) if tool_result else ""

            if result_str:
                final_reply = result_str

            if model.startswith("claude"):
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tc.id,
                                "content": result_str,
                            }
                        ],
                    }
                )
            else:
                messages.append(
                    {"role": "tool", "tool_call_id": tc.id, "content": result_str}
                )

    if not final_reply:
        final_reply = "Reached maximum processing steps."

    add_to_history(channel_id, "assistant", final_reply)
    return final_reply


# =========================
# DISCORD BOT
# =========================

intents = discord.Intents.default()
intents.message_content = True

bot = discord.Client(intents=intents)


@bot.event
async def on_ready():
    print(f"Agent running as {bot.user}")
    log_bot_started()


@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if message.webhook_id:
        return

    # Έλεγξε αν είναι auth command
    if await handle_auth_commands(message):
        return

    if await handle_translate_command(message, client):
        return
    if message.channel.id == CLAUDE_CODE_CHANNEL_ID:
        await handle_claude_code_channel(message)
        return
    if _is_rate_limited(message.author.id):
        await message.channel.send("⚠️ Πολλά μηνύματα σε σύντομο χρονικό διάστημα. Περίμενε λίγο.")
        return
    if await handle_video_request(message):
        return
    if await handle_image_request(message):
        return
    # Log εισερχόμενο μήνυμα
    log_message_received(message.channel.id, str(message.author), message.content)

    # File attachment handler
    if message.attachments:
        for attachment in message.attachments:
            if attachment.size > MAX_FILE_SIZE:
                await message.channel.send(f"⚠️ Το αρχείο `{attachment.filename}` είναι πολύ μεγάλο (max 20MB).")
                continue
            try:
                async with message.channel.typing():
                    reply = await handle_file_attachment(attachment, message.content, message.channel.id)
                user_text = message.content or f"[attachment: {attachment.filename}]"
                add_to_history(message.channel.id, "user", user_text)
                add_to_history(message.channel.id, "assistant", reply)
                log_reply_sent(message.channel.id, reply)
                if len(reply) <= 2000:
                    await message.channel.send(reply)
                else:
                    for chunk in [reply[i : i + 1990] for i in range(0, len(reply), 1990)]:
                        await message.channel.send(chunk)
            except Exception as e:
                log_error(message.channel.id, str(e))
                raise
        return

    # Κανονικό μήνυμα → AI agent
    try:
        async with message.channel.typing():
            reply = await run_agent(
                message.content, message.channel.id, message.author.id
            )

        log_reply_sent(message.channel.id, reply)

        if len(reply) <= 2000:
            await message.channel.send(reply)
        else:
            for chunk in [reply[i : i + 1990] for i in range(0, len(reply), 1990)]:
                await message.channel.send(chunk)

    except Exception as e:
        log_error(message.channel.id, str(e))
        raise


bot.run(DISCORD_TOKEN)
