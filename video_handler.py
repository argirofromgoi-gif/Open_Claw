"""
Video Handler — Remotion + Discord Integration
===============================================
Δημιουργεί videos μέσω Remotion όταν ο χρήστης ζητά από το Discord.

Triggers:
- "create a video"
- "make a promo video"
- "create an influencer video"
- "make a video for"
"""

import asyncio
import os
import re
import json
import subprocess
from pathlib import Path

# Remotion project directory
REMOTION_DIR = "/home/ubuntu/my-video"
OUTPUT_FILE  = f"{REMOTION_DIR}/out/promo.mp4"

# Active video sessions: {user_id: {params}}
video_sessions = {}

# Keywords that trigger video creation
VIDEO_KEYWORDS = [
    "create a video", "make a video", "generate a video",
    "create a promo", "make a promo", "create an influencer video",
    "make a promo video", "create promo video", "video for influencer",
    "δημιουργησε video", "φτιαξε video", "δημιουργησε βιντεο"
]

# Keywords that trigger video modification
MODIFY_KEYWORDS = [
    "make it shorter", "make it longer", "change the color",
    "change the background", "add logo", "change the text",
    "make it bigger", "make it smaller", "change the music",
    "αλλαξε", "προσθεσε", "κανε το"
]


def is_video_request(content: str) -> bool:
    """Ελέγχει αν το μήνυμα είναι αίτηση για video."""
    content_lower = content.lower()
    return any(keyword in content_lower for keyword in VIDEO_KEYWORDS)


def is_modify_request(content: str, user_id: int) -> bool:
    """Ελέγχει αν είναι αίτηση τροποποίησης υπάρχοντος video."""
    if user_id not in video_sessions:
        return False
    content_lower = content.lower()
    return any(keyword in content_lower for keyword in MODIFY_KEYWORDS)


def parse_video_params(content: str) -> dict:
    """Εξάγει παραμέτρους από το μήνυμα."""
    params = {
        "influencerName": None,
        "productName":    None,
        "ctaText":        "Shop Now",
        "primaryColor":   "#FF0000",
        "platform":       "instagram",
        "style":          "energetic",
        "duration":       15,
    }

    content_lower = content.lower()

    # Platform detection
    if "youtube" in content_lower:
        params["platform"] = "youtube"
    elif "tiktok" in content_lower:
        params["platform"] = "tiktok"
    elif "linkedin" in content_lower:
        params["platform"] = "linkedin"
    else:
        params["platform"] = "instagram"

    # Style detection
    if "calm" in content_lower or "relaxed" in content_lower:
        params["style"] = "calm"
    elif "professional" in content_lower:
        params["style"] = "professional"
    elif "fun" in content_lower or "playful" in content_lower:
        params["style"] = "playful"

    # Duration detection
    duration_match = re.search(r'(\d+)\s*(?:second|sec|δευτερ)', content_lower)
    if duration_match:
        params["duration"] = int(duration_match.group(1))

    # Influencer name detection
    for_match = re.search(r'for\s+@?([A-Za-z0-9_]+)', content, re.IGNORECASE)
    if for_match:
        params["influencerName"] = for_match.group(1)

    # Product detection
    product_match = re.search(r'product[:\s]+([^,\.]+)', content, re.IGNORECASE)
    if product_match:
        params["productName"] = product_match.group(1).strip()

    # CTA text detection
    cta_match = re.search(r'cta[:\s]+([^,\.]+)', content, re.IGNORECASE)
    if cta_match:
        params["ctaText"] = cta_match.group(1).strip()

    # Primary color detection
    color_match = re.search(r'color[:\s]+(#[0-9A-Fa-f]{6}|[a-z]+)', content, re.IGNORECASE)
    if color_match:
        params["primaryColor"] = color_match.group(1)

    return params


def build_render_props(params: dict) -> dict:
    """Builds the props dict for the PromoReel composition."""
    return {
        "influencerName": params.get("influencerName") or "Influencer",
        "productName":    params.get("productName") or "Product",
        "ctaText":        params.get("ctaText", "Shop Now"),
        "primaryColor":   params.get("primaryColor", "#FF0000"),
    }


async def run_remotion_render(props: dict) -> tuple[bool, str]:
    """Τρέχει το Remotion render και επιστρέφει success/error."""
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                [
                    "npx", "remotion", "render", "PromoReel", "out/promo.mp4",
                    "--props", json.dumps(props),
                    "--overwrite",
                ],
                cwd=REMOTION_DIR,
                capture_output=True,
                text=True,
                timeout=300
            )
        )

        if result.returncode == 0:
            return True, OUTPUT_FILE
        else:
            return False, result.stderr[:500]

    except subprocess.TimeoutExpired:
        return False, "Render timeout after 5 minutes"
    except Exception as e:
        return False, str(e)


async def handle_video_request(message) -> bool:
    """
    Κύριος handler για video requests.
    Επιστρέφει True αν το μήνυμα ήταν video request.
    """
    import discord

    user_id = message.author.id
    content = message.content.strip()

    # Έλεγξε αν είναι video request
    if not is_video_request(content) and not is_modify_request(content, user_id):
        return False

    async with message.channel.typing():

        # Parse παραμέτρους
        params = parse_video_params(content)

        # Αποθήκευσε session
        video_sessions[user_id] = params

        # Ενημέρωσε status
        platform_emoji = {
            "instagram": "📸",
            "tiktok":    "🎵",
            "youtube":   "▶️",
            "linkedin":  "💼"
        }.get(params["platform"], "🎬")

        await message.channel.send(
            f"🎬 **Creating video...**\n"
            f"{platform_emoji} Platform: **{params['platform'].capitalize()}**\n"
            f"⏱️ Duration: **{params['duration']}s**\n"
            f"🎨 Style: **{params['style']}**\n"
            + (f"👤 Influencer: **{params['influencerName']}**\n" if params['influencerName'] else "")
            + (f"📦 Product: **{params['productName']}**\n" if params['productName'] else "")
            + "\n⏳ Rendering... this may take 1-2 minutes."
        )

        # Build props and render
        render_props = build_render_props(params)
        success, result = await run_remotion_render(render_props)

        if success:
            # Στείλε το video
            video_file = Path(result)
            if video_file.exists() and video_file.stat().st_size > 0:
                try:
                    await message.channel.send(
                        f"✅ **Video ready!**\n"
                        f"You can now ask me to modify it:\n"
                        f"• 'make it shorter'\n"
                        f"• 'change the background color'\n"
                        f"• 'add a logo'\n"
                        f"• 'make it more energetic'",
                        file=discord.File(str(video_file), filename="promo_video.mp4")
                    )
                except Exception as e:
                    await message.channel.send(
                        f"✅ Video rendered successfully!\n"
                        f"📁 Saved to: `{result}`\n"
                        f"⚠️ Could not upload to Discord (file too large): {str(e)[:100]}"
                    )
            else:
                await message.channel.send("❌ Render completed but output file not found.")
        else:
            await message.channel.send(
                f"❌ **Render failed:**\n```\n{result[:500]}\n```\n"
                f"Try asking the dev agent to fix it in #dev_claude."
            )

    return True 
