import re
import asyncio
import base64
import io
import aiohttp

from openai import AsyncOpenAI
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

from config import OPENAI_API_KEY
from auth import get_user_creds
from memory import add_to_history, save_last_image, get_last_image

_openai = AsyncOpenAI(api_key=OPENAI_API_KEY)

_TRIGGER_PATTERN = re.compile(
    r'\b(create|generate|make|draw|produce)\s+(an?\s+)?image\b',
    re.IGNORECASE,
)

_FOLLOWUP_PATTERN = re.compile(
    r'\b(change|modify|edit|update|alter)\s+(it|that|this|the\s+image)\b'
    r'|\b(now\s+(make|change|add|give)\s+it)\b'
    r'|\b(same\s+image\s+but)\b',
    re.IGNORECASE,
)

_STYLE_PATTERN = re.compile(
    r'\bin\s+(?:a\s+)?([a-zA-Z\s]+?)\s+style\b',
    re.IGNORECASE,
)


def _is_image_request(text: str) -> bool:
    return bool(_TRIGGER_PATTERN.search(text))


def _is_followup_request(text: str) -> bool:
    return bool(_FOLLOWUP_PATTERN.search(text))


def _parse_request(text: str) -> tuple:
    """Return (description, style_or_None) extracted from the message."""
    description = _TRIGGER_PATTERN.sub("", text).strip()
    description = re.sub(r"^(of|with|a|an|for)\s+", "", description, flags=re.IGNORECASE).strip()

    style_match = _STYLE_PATTERN.search(description)
    style = style_match.group(1).strip() if style_match else None
    if style_match:
        description = (description[: style_match.start()] + description[style_match.end() :]).strip()

    return (description or text, style)


async def _fetch_image_bytes(url: str) -> bytes:
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.read()


async def _generate_image(prompt: str, reference_image_bytes: bytes | None = None) -> bytes:
    if reference_image_bytes is not None:
        ref_file = io.BytesIO(reference_image_bytes)
        ref_file.name = "reference.png"
        response = await _openai.images.edit(
            model="gpt-image-1",
            image=ref_file,
            prompt=prompt,
            size="1024x1024",
            n=1,
        )
    else:
        response = await _openai.images.generate(
            model="gpt-image-1",
            prompt=prompt,
            size="1024x1024",
            quality="auto",
            n=1,
        )
    return base64.b64decode(response.data[0].b64_json)


async def _upload_to_drive(image_bytes: bytes, filename: str, user_id: int) -> str:
    """Upload image to Google Drive and return a shareable link."""
    loop = asyncio.get_event_loop()
    creds = get_user_creds(user_id)
    if not creds:
        raise RuntimeError("No Google account connected. Type `!connect` first.")

    drive = build("drive", "v3", credentials=creds)
    file_metadata = {"name": filename, "mimeType": "image/png"}
    media = MediaIoBaseUpload(io.BytesIO(image_bytes), mimetype="image/png")

    file = await loop.run_in_executor(
        None,
        lambda: drive.files()
        .create(body=file_metadata, media_body=media, fields="id")
        .execute(),
    )
    file_id = file["id"]

    await loop.run_in_executor(
        None,
        lambda: drive.permissions()
        .create(fileId=file_id, body={"type": "anyone", "role": "reader"})
        .execute(),
    )

    return f"https://drive.google.com/file/d/{file_id}/view"


async def handle_image_request(message) -> bool:
    """
    Detect and handle image generation requests in a Discord message.
    Returns True if the message was handled, False otherwise.
    """
    channel_id = message.channel.id
    is_new = _is_image_request(message.content)
    is_followup = _is_followup_request(message.content)

    if not is_new and not is_followup:
        return False

    description, style = _parse_request(message.content)
    prompt = f"{description}, in {style} style" if style else description

    # Collect a reference image from attachments, message URLs, or cached last image.
    reference_image_bytes = None
    if message.attachments:
        for attachment in message.attachments:
            if attachment.content_type and attachment.content_type.startswith("image/"):
                reference_image_bytes = await _fetch_image_bytes(attachment.url)
                break
    if reference_image_bytes is None:
        url_match = re.search(r'https?://\S+\.(?:png|jpg|jpeg|webp|gif)', message.content, re.IGNORECASE)
        if url_match:
            reference_image_bytes = await _fetch_image_bytes(url_match.group(0))
    if reference_image_bytes is None and is_followup:
        cached = get_last_image(channel_id)
        if cached:
            try:
                reference_image_bytes = await _fetch_image_bytes(cached["url"])
                if not prompt.strip():
                    prompt = cached["prompt"]
            except Exception:
                pass

    add_to_history(channel_id, "user", message.content)

    async with message.channel.typing():
        try:
            image_bytes = await _generate_image(prompt, reference_image_bytes)

            safe_name = re.sub(r"[^\w\s-]", "", description)[:50].strip().replace(" ", "_")
            filename = f"{safe_name or 'image'}.png"

            drive_link = await _upload_to_drive(image_bytes, filename, message.author.id)
            reply = f"Here is your generated image:\n{drive_link}"
            await message.channel.send(reply)
            save_last_image(channel_id, drive_link, prompt)
            add_to_history(channel_id, "assistant", reply)

        except RuntimeError as e:
            await message.channel.send(str(e))
        except Exception as e:
            await message.channel.send(f"Failed to generate image: {e}")

    return True
