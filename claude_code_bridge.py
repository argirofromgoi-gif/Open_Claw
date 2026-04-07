"""
Claude Code Discord Bridge
==========================
Ακούει μηνύματα από το #dev_claude κανάλι και τα εκτελεί μέσω Claude Code CLI.
Προσθέσε αυτό στο ai_discord_agent.py:

  from claude_code_bridge import handle_claude_code_channel

Και στο on_message πριν το run_agent:

  if message.channel.id == CLAUDE_CODE_CHANNEL_ID:
      await handle_claude_code_channel(message)
      return
"""

import asyncio
import json
import os
import subprocess
import tempfile
from pathlib import Path

try:
    from logger import log_api_call as _log_api_call
except ImportError:
    _log_api_call = None

# Discord Channel ID για το #dev_claude
CLAUDE_CODE_CHANNEL_ID = 1488438587778269304

# Φάκελος εργασίας για το Claude Code
WORKSPACE = "/home/ubuntu"

# Timeout σε δευτερόλεπτα
TIMEOUT = 3600


async def run_claude_code(prompt: str, working_dir: str = WORKSPACE) -> str:
    """Εκτελεί το Claude Code με το δοθέν prompt και επιστρέφει το αποτέλεσμα."""
    try:
        # Γράψε το prompt σε temp file για να αποφύγουμε προβλήματα με special chars
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write(prompt)
            prompt_file = f.name

        # Εκτέλεσε το Claude Code
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                ["claude", "--print", "--dangerously-skip-permissions", f"$(cat {prompt_file})"],
                cwd=working_dir,
                capture_output=True,
                text=True,
                timeout=3600,
                env={**os.environ, "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", "")}
            )
        )

        # Καθάρισε το temp file
        os.unlink(prompt_file)

        output = result.stdout.strip()
        if result.returncode != 0 and result.stderr:
            output += f"\n\n⚠️ Stderr:\n{result.stderr[:500]}"

        return output if output else "✅ Done (no output)"

    except subprocess.TimeoutExpired:
        return f"⏱️ Timeout after {TIMEOUT}s — task may still be running"
    except Exception as e:
        return f"❌ Error: {str(e)}"


async def run_claude_code_simple(prompt: str, working_dir: str = WORKSPACE) -> str:
    """Εκτελεί το Claude Code με απλό τρόπο χρησιμοποιώντας --print flag."""
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                [
                    "/home/ubuntu/.npm-global/bin/claude",
                    "-p", prompt,
                    "--dangerously-skip-permissions",
                    "--output-format", "json",
                ],
                cwd=working_dir,
                capture_output=True,
                text=True,
                timeout=3600,
            )
        )

        raw = result.stdout.strip()
        errors = result.stderr.strip()

        # Parse JSON output to extract text result and token usage
        output = raw
        input_tokens = 0
        output_tokens = 0

        if raw:
            try:
                data = json.loads(raw)
                output = data.get("result", raw)
                usage = data.get("usage", {})
                input_tokens = int(usage.get("input_tokens", 0))
                output_tokens = int(usage.get("output_tokens", 0))
            except (json.JSONDecodeError, TypeError, ValueError):
                pass  # raw text fallback

        # Log token usage and cost into bot_stats.json under the 'dev' agent
        if _log_api_call is not None and (input_tokens or output_tokens):
            try:
                _log_api_call(CLAUDE_CODE_CHANNEL_ID, input_tokens, output_tokens)
            except Exception:
                pass

        if output:
            return output
        elif errors:
            return f"⚠️ {errors[:1000]}"
        else:
            return "✅ Done"

    except subprocess.TimeoutExpired:
        return f"⏱️ Timeout after {TIMEOUT}s"
    except FileNotFoundError:
        return "❌ Claude Code not found. Run: npm install -g @anthropic-ai/claude-code"
    except Exception as e:
        return f"❌ Error: {str(e)}"


async def handle_claude_code_channel(message):
    """
    Handler για το #dev_claude κανάλι.
    Κάλεσέ το από το on_message όταν το channel ID ταιριάζει.
    """
    import discord

    content = message.content.strip()

    # Αγνόησε κενά μηνύματα
    if not content:
        return

    # Help command
    if content.lower() in ["!help", "help", "?"]:
        await message.channel.send(
            "🤖 **Claude Code Bridge**\n\n"
            "Γράψε οποιαδήποτε εντολή και θα την εκτελέσω με Claude Code.\n\n"
            "**Παραδείγματα:**\n"
            "• `Fix the Timeline tab in /home/ubuntu/dashboard/server.js`\n"
            "• `Read /home/ubuntu/dashboard/server.js and tell me what endpoints exist`\n"
            "• `Add error handling to the /api/agents endpoint`\n"
            "• `Check the bot_stats.json format and fix any issues`\n\n"
            f"**Working directory:** `{WORKSPACE}`\n"
            f"**Timeout:** {TIMEOUT}s"
        )
        return

    # Status command
    if content.lower() == "!status":
        try:
            result = subprocess.run(
                ["claude", "--version"],
                capture_output=True, text=True, timeout=10
            )
            version = result.stdout.strip() or result.stderr.strip()
            await message.channel.send(f"✅ Claude Code is running: `{version}`")
        except Exception as e:
            await message.channel.send(f"❌ Claude Code error: {str(e)}")
        return

    # Εκτέλεσε το prompt
    async with message.channel.typing():
        await message.channel.send(f"⚙️ Running Claude Code...\n> `{content[:100]}`")

        output = await run_claude_code_simple(content, WORKSPACE)

    # Στείλε το αποτέλεσμα
    if len(output) <= 1900:
        await message.channel.send(f"✅ **Result:**\n```\n{output}\n```")
    else:
        # Σπάσε σε chunks
        chunks = [output[i:i+1800] for i in range(0, len(output), 1800)]
        await message.channel.send(f"✅ **Result** ({len(chunks)} parts):")
        for i, chunk in enumerate(chunks[:5]):  # max 5 chunks
            await message.channel.send(f"```\n{chunk}\n```")
        if len(chunks) > 5:
            await message.channel.send(f"⚠️ Output truncated ({len(chunks)} total chunks)")
