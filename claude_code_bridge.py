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

try:
    from config import CHANNEL_PROMPTS as _CHANNEL_PROMPTS
except ImportError:
    _CHANNEL_PROMPTS = {}

try:
    from memory import get_history as _get_history
except ImportError:
    _get_history = None

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
    """
    Εκτελεί το Claude Code με stream-json και επιστρέφει το output του script
    (τα ✅/❌ lines) χωρίς να περιμένει το τελικό Claude summary.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "/home/ubuntu/.npm-global/bin/claude",
            "-p", prompt,
            "--dangerously-skip-permissions",
            "--output-format", "stream-json",
            cwd=working_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        script_outputs = []
        final_result = ""
        input_tokens = 0
        output_tokens = 0

        async def read_stream():
            nonlocal input_tokens, output_tokens, final_result
            async for raw_line in proc.stdout:
                line = raw_line.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    etype = event.get("type", "")

                    # Collect tool result outputs (actual script stdout with ✅/❌)
                    if etype == "user":
                        content = event.get("message", {}).get("content", [])
                        for block in content:
                            if block.get("type") == "tool_result":
                                tc = block.get("content", "")
                                if isinstance(tc, list):
                                    for c in tc:
                                        if c.get("type") == "text" and c.get("text", "").strip():
                                            script_outputs.append(c["text"])
                                elif isinstance(tc, str) and tc.strip():
                                    script_outputs.append(tc)

                    # Collect assistant text responses
                    elif etype == "assistant":
                        msg_content = event.get("message", {}).get("content", [])
                        for block in msg_content:
                            if block.get("type") == "text" and block.get("text", "").strip():
                                final_result = block["text"]

                    # Grab final text result and token usage
                    elif etype == "result":
                        result_text = event.get("result", "")
                        if result_text:
                            final_result = result_text
                        usage = event.get("usage", {})
                        input_tokens = int(usage.get("input_tokens", 0))
                        output_tokens = int(usage.get("output_tokens", 0))

                except (json.JSONDecodeError, TypeError):
                    pass

        try:
            await asyncio.wait_for(read_stream(), timeout=TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            if script_outputs:
                return "\n".join(script_outputs) + f"\n\n⏱️ Timeout after {TIMEOUT}s — partial results above"
            return f"⏱️ Timeout after {TIMEOUT}s"

        await proc.wait()

        if _log_api_call is not None and (input_tokens or output_tokens):
            try:
                _log_api_call(CLAUDE_CODE_CHANNEL_ID, input_tokens, output_tokens)
            except Exception:
                pass

        if script_outputs:
            return "\n".join(script_outputs)
        if final_result:
            return final_result
        return "✅ Done"

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

        # Build full prompt with system context + history + user message
        channel_id = message.channel.id
        system_prompt = _CHANNEL_PROMPTS.get(channel_id, "")

        history_text = ""
        if _get_history is not None:
            history = _get_history(channel_id)[-10:]  # last 10 messages
            if history:
                history_text = "\n\n## CONVERSATION HISTORY\n"
                for h in history:
                    history_text += f"{h['role'].upper()}: {h['content'][:500]}\n"

        if system_prompt or history_text:
            full_prompt = f"{system_prompt}{history_text}\n\n## USER REQUEST\n{content}"
        else:
            full_prompt = content

        output = await run_claude_code_simple(full_prompt, WORKSPACE)

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
