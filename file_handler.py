import base64
import io
import os
import aiohttp
import anthropic
from PIL import Image

from config import ANTHROPIC_API_KEY
from logger import log_api_call

anthropic_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
IMAGE_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

SUPPORTED_TYPES = IMAGE_TYPES | {".pdf", ".docx", ".xlsx", ".csv", ".txt", ".json", ".py", ".js"}


def _ext(filename: str) -> str:
    return os.path.splitext(filename.lower())[1]


async def _download(url: str) -> bytes:
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.read()


def _extract_pdf(data: bytes) -> str:
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = [page.extract_text() or "" for page in pdf.pages]
        return "\n\n".join(pages).strip()
    except Exception:
        import PyPDF2
        reader = PyPDF2.PdfReader(io.BytesIO(data))
        return "\n\n".join(
            page.extract_text() or "" for page in reader.pages
        ).strip()


def _extract_docx(data: bytes) -> str:
    from docx import Document
    doc = Document(io.BytesIO(data))
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())


def _extract_spreadsheet(data: bytes, ext: str) -> str:
    import pandas as pd
    if ext == ".csv":
        df = pd.read_csv(io.BytesIO(data))
    else:
        df = pd.read_excel(io.BytesIO(data))
    return df.to_string(index=False)


# Base64 encoding inflates size by ~33%, so target 3 MB raw to stay under the 5 MB API limit.
_MAX_IMAGE_BYTES = 3 * 1024 * 1024  # 3 MB raw ≈ 4 MB base64


def _compress_image(data: bytes, ext: str) -> tuple[bytes, str]:
    """Resize and compress an image until it fits under 3 MB raw. Returns (data, ext)."""
    img = Image.open(io.BytesIO(data))

    # Always convert to JPEG for compression (much smaller than PNG for photos).
    # Only keep PNG if the image has meaningful transparency.
    has_transparency = img.mode in ("RGBA", "P") and ext == ".png"
    if has_transparency:
        # Check if alpha channel is actually used
        if img.mode == "P":
            img = img.convert("RGBA")
        alpha = img.split()[-1]
        has_transparency = any(p < 255 for p in alpha.getdata())

    if has_transparency:
        save_format = "PNG"
    else:
        save_format = "JPEG"
        if img.mode in ("RGBA", "P", "L"):
            img = img.convert("RGB")

    quality = 85

    while True:
        buf = io.BytesIO()
        if save_format == "JPEG":
            img.save(buf, format="JPEG", quality=quality, optimize=True)
        else:
            img.save(buf, format="PNG", optimize=True)

        if len(buf.getvalue()) <= _MAX_IMAGE_BYTES:
            break

        # Shrink dimensions by 15% each pass; also reduce JPEG quality
        new_w = max(1, int(img.width * 0.85))
        new_h = max(1, int(img.height * 0.85))
        img = img.resize((new_w, new_h), Image.LANCZOS)

        if save_format == "JPEG" and quality > 40:
            quality -= 10

    result_ext = ".jpg" if save_format == "JPEG" else ".png"
    return buf.getvalue(), result_ext


async def _ask_claude_vision(image_data: bytes, ext: str, user_prompt: str, channel_id=None) -> str:
    if len(image_data) > _MAX_IMAGE_BYTES:
        image_data, ext = _compress_image(image_data, ext)

    media_type = IMAGE_MIME.get(ext, "image/jpeg")
    b64 = base64.standard_b64encode(image_data).decode("utf-8")
    prompt = user_prompt.strip() if user_prompt.strip() else "Describe this image in detail."
    response = await anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    )
    if channel_id is not None and response.usage:
        log_api_call(channel_id, response.usage.input_tokens, response.usage.output_tokens)
    return next((b.text for b in response.content if b.type == "text"), "")


async def _ask_claude_text(content: str, filename: str, user_prompt: str, channel_id=None) -> str:
    prompt = user_prompt.strip() if user_prompt.strip() else f"Summarize and analyze the following file: {filename}"
    full_prompt = f"{prompt}\n\n--- File: {filename} ---\n{content}"
    response = await anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": full_prompt}],
    )
    if channel_id is not None and response.usage:
        log_api_call(channel_id, response.usage.input_tokens, response.usage.output_tokens)
    return next((b.text for b in response.content if b.type == "text"), "")


async def handle_file_attachment(attachment, user_message: str, channel_id=None) -> str:
    """
    Download and process a Discord attachment, then query Claude.
    Returns Claude's response as a string, or an error message.
    """
    filename = attachment.filename
    ext = _ext(filename)

    if ext not in SUPPORTED_TYPES:
        return (
            f"Unsupported file type `{ext}`. Supported: "
            + ", ".join(sorted(SUPPORTED_TYPES))
        )

    try:
        data = await _download(attachment.url)
    except Exception as e:
        return f"Failed to download `{filename}`: {e}"

    try:
        if ext in IMAGE_TYPES:
            return await _ask_claude_vision(data, ext, user_message, channel_id)

        if ext == ".pdf":
            text = _extract_pdf(data)
            if not text:
                return "Could not extract text from the PDF."
        elif ext == ".docx":
            text = _extract_docx(data)
            if not text:
                return "Could not extract text from the DOCX file."
        elif ext in {".xlsx", ".csv"}:
            text = _extract_spreadsheet(data, ext)
        else:
            # .txt, .json, .py, .js
            text = data.decode("utf-8", errors="replace")

        # Truncate very large files to avoid exceeding context limits
        if len(text) > 60_000:
            text = text[:60_000] + "\n\n[...file truncated at 60,000 characters...]"

        return await _ask_claude_text(text, filename, user_message, channel_id)

    except Exception as e:
        return f"Error processing `{filename}`: {e}"
