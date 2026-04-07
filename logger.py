"""
OpenClaw GOI — Activity Logger
Αποθηκεύει activity data για το dashboard σε:
  /home/ubuntu/bot_stats.json
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

STATS_FILE = Path("/home/ubuntu/bot_stats.json")
DAILY_FILE = Path("/home/ubuntu/usage_daily.json")
_lock      = Lock()

# Channel ID → agent name mapping
CHANNEL_NAMES = {
    "1484174547400921219": "marketing",
    "1484174643517718528": "tech-support",
    "1488438587778269304": "dev",
}

# Pricing per 1M tokens (input, output) in USD
MODEL_PRICING = {
    "claude-sonnet-4-6": {"input": 3.00,  "output": 15.00},
    "gpt-4o-mini":       {"input": 0.15,  "output": 0.60},
}

DEFAULT_STATS = {
    "agents": {
        "general": {
            "name":               "General 🤖",
            "model":              "gpt-4o-mini",
            "channelId":          None,
            "totalMessages":      0,
            "totalToolCalls":     0,
            "totalErrors":        0,
            "totalInputTokens":   0,
            "totalOutputTokens":  0,
            "totalCost":          0.0,
            "lastMessage":        None,
            "lastUser":           None,
            "lastAction":         None,
            "lastReply":          None,
            "lastActivityAt":     None,
            "recentActivity":     [],
        },
        "marketing": {
            "name":               "Marketing 📈",
            "model":              "claude-sonnet-4-6",
            "channelId":          "1484174547400921219",
            "totalMessages":      0,
            "totalToolCalls":     0,
            "totalErrors":        0,
            "totalInputTokens":   0,
            "totalOutputTokens":  0,
            "totalCost":          0.0,
            "lastMessage":        None,
            "lastUser":           None,
            "lastAction":         None,
            "lastReply":          None,
            "lastActivityAt":     None,
            "recentActivity":     [],
        },
        "tech-support": {
            "name":               "Tech Support ⚙️",
            "model":              "claude-sonnet-4-6",
            "channelId":          "1484174643517718528",
            "totalMessages":      0,
            "totalToolCalls":     0,
            "totalErrors":        0,
            "totalInputTokens":   0,
            "totalOutputTokens":  0,
            "totalCost":          0.0,
            "lastMessage":        None,
            "lastUser":           None,
            "lastAction":         None,
            "lastReply":          None,
            "lastActivityAt":     None,
            "recentActivity":     [],
        },
        "dev": {
            "name":               "Dev Claude 🛠️",
            "model":              "claude-sonnet-4-6",
            "channelId":          "1488438587778269304",
            "totalMessages":      0,
            "totalToolCalls":     0,
            "totalErrors":        0,
            "totalInputTokens":   0,
            "totalOutputTokens":  0,
            "totalCost":          0.0,
            "lastMessage":        None,
            "lastUser":           None,
            "lastAction":         None,
            "lastReply":          None,
            "lastActivityAt":     None,
            "recentActivity":     [],
        },
    },
    "global": {
        "totalMessages":      0,
        "totalToolCalls":     0,
        "totalErrors":        0,
        "totalInputTokens":   0,
        "totalOutputTokens":  0,
        "totalCost":          0.0,
        "botStartedAt":       datetime.now(timezone.utc).isoformat(),
        "lastActivityAt":     None,
    },
    "recentErrors": [],
    "updatedAt": None,
}

MAX_RECENT_ACTIVITY = 20
MAX_RECENT_ERRORS   = 10


# =========================
# HELPERS
# =========================

_NUMERIC_AGENT_FIELDS = (
    "totalMessages", "totalToolCalls", "totalErrors",
    "totalInputTokens", "totalOutputTokens",
)
_NUMERIC_GLOBAL_FIELDS = _NUMERIC_AGENT_FIELDS + ("totalCost",)


def _repair(stats: dict) -> dict:
    """Ensure all expected agents exist and numeric fields are not None."""
    # Add missing agents
    for key, defaults in DEFAULT_STATS["agents"].items():
        if key not in stats.get("agents", {}):
            stats.setdefault("agents", {})[key] = json.loads(json.dumps(defaults))
    # Fix None numeric values in each agent
    for agent in stats["agents"].values():
        for field in _NUMERIC_AGENT_FIELDS:
            if agent.get(field) is None:
                agent[field] = 0
        if agent.get("totalCost") is None:
            agent["totalCost"] = 0.0
    # Fix None numeric values in global
    for field in _NUMERIC_GLOBAL_FIELDS:
        if stats.get("global", {}).get(field) is None:
            stats["global"][field] = 0 if field != "totalCost" else 0.0
    return stats


def _load() -> dict:
    if STATS_FILE.exists():
        try:
            stats = json.loads(STATS_FILE.read_text())
            return _repair(stats)
        except Exception:
            pass
    return json.loads(json.dumps(DEFAULT_STATS))  # deep copy


def _save(stats: dict):
    stats["updatedAt"] = datetime.now(timezone.utc).isoformat()
    STATS_FILE.write_text(json.dumps(stats, indent=2, ensure_ascii=False))


def _update_daily(cost: float):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        daily = json.loads(DAILY_FILE.read_text()) if DAILY_FILE.exists() else {"days": []}
    except Exception:
        daily = {"days": []}
    days = daily.get("days", [])
    for d in days:
        if d.get("date") == today:
            d["cost"]  = round(d.get("cost", 0.0) + cost, 6)
            d["calls"] = d.get("calls", 0) + 1
            DAILY_FILE.write_text(json.dumps(daily, indent=2, ensure_ascii=False))
            return
    days.insert(0, {"date": today, "cost": round(cost, 6), "calls": 1})
    daily["days"] = days[:90]
    DAILY_FILE.write_text(json.dumps(daily, indent=2, ensure_ascii=False))


def _get_agent_key(channel_id: int) -> str:
    return CHANNEL_NAMES.get(str(channel_id), "general")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _add_activity(agent_stats: dict, activity: dict):
    """Προσθέτει activity και κρατά μόνο τα τελευταία MAX_RECENT_ACTIVITY."""
    recent = agent_stats.get("recentActivity", [])
    recent.insert(0, activity)
    agent_stats["recentActivity"] = recent[:MAX_RECENT_ACTIVITY]


# =========================
# PUBLIC API
# =========================

def log_message_received(channel_id: int, user_name: str, message: str):
    """Καλείται όταν το bot λαμβάνει μήνυμα."""
    with _lock:
        stats     = _load()
        agent_key = _get_agent_key(channel_id)
        agent     = stats["agents"][agent_key]
        now       = _now()

        agent["totalMessages"]  = agent.get("totalMessages", 0) + 1
        agent["lastMessage"]    = message[:200]
        agent["lastUser"]       = user_name
        agent["lastActivityAt"] = now
        agent["status"]         = "Working"

        stats["global"]["totalMessages"]  = stats["global"].get("totalMessages", 0) + 1
        stats["global"]["lastActivityAt"] = now

        _add_activity(agent, {
            "type":    "message",
            "icon":    "💬",
            "text":    f"{user_name}: {message[:100]}",
            "time":    now,
        })

        _save(stats)


def log_tool_called(channel_id: int, tool_name: str, result_preview: str = ""):
    """Καλείται όταν το bot χρησιμοποιεί ένα tool."""
    with _lock:
        stats     = _load()
        agent_key = _get_agent_key(channel_id)
        agent     = stats["agents"][agent_key]
        now       = _now()

        agent["totalToolCalls"]  = agent.get("totalToolCalls", 0) + 1
        agent["lastAction"]      = tool_name
        agent["lastActivityAt"]  = now

        stats["global"]["totalToolCalls"] = stats["global"].get("totalToolCalls", 0) + 1

        # Icon ανάλογα με το tool
        icons = {
            "create_document":      "📄",
            "append_to_document":   "✏️",
            "create_sheet":         "📊",
            "write_sheet":          "📊",
            "gmail_send":           "📧",
            "gmail_list":           "📬",
            "gmail_read":           "📨",
            "calendar_create":      "📅",
            "calendar_list":        "📅",
            "calendar_delete":      "🗑️",
            "search_web":           "🔍",
            "read_own_code":        "👀",
            "edit_own_code":        "🔧",
            "read_document_by_url": "🔗",
        }
        icon = icons.get(tool_name, "⚙️")

        _add_activity(agent, {
            "type":    "tool",
            "icon":    icon,
            "text":    f"Used {tool_name}" + (f": {result_preview[:80]}" if result_preview else ""),
            "time":    now,
        })

        _save(stats)


def log_reply_sent(channel_id: int, reply: str):
    """Καλείται όταν το bot στέλνει απάντηση."""
    with _lock:
        stats     = _load()
        agent_key = _get_agent_key(channel_id)
        agent     = stats["agents"][agent_key]
        now       = _now()

        agent["lastReply"]      = reply[:300]
        agent["lastActivityAt"] = now
        agent["status"]         = "Idle"

        _save(stats)


def log_error(channel_id: int, error: str):
    """Καλείται όταν συμβαίνει error."""
    with _lock:
        stats     = _load()
        agent_key = _get_agent_key(channel_id)
        agent     = stats["agents"][agent_key]
        now       = _now()

        agent["totalErrors"]    = agent.get("totalErrors", 0) + 1
        agent["lastActivityAt"] = now
        agent["status"]         = "Error"

        stats["global"]["totalErrors"] = stats["global"].get("totalErrors", 0) + 1

        _add_activity(agent, {
            "type": "error",
            "icon": "❌",
            "text": error[:150],
            "time": now,
        })

        # Global errors list
        recent_errors = stats.get("recentErrors", [])
        recent_errors.insert(0, {
            "agent": agent_key,
            "error": error[:150],
            "time":  now,
        })
        stats["recentErrors"] = recent_errors[:MAX_RECENT_ERRORS]

        _save(stats)


def log_api_call(channel_id: int, input_tokens: int, output_tokens: int):
    """Καλείται μετά από κάθε API call για να καταγράψει token usage και κόστος."""
    with _lock:
        stats     = _load()
        agent_key = _get_agent_key(channel_id)
        agent     = stats["agents"][agent_key]

        model   = agent.get("model", "gpt-4o-mini")
        pricing = MODEL_PRICING.get(model, {"input": 0.0, "output": 0.0})
        cost    = (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000

        agent["totalInputTokens"]  = agent.get("totalInputTokens", 0)  + input_tokens
        agent["totalOutputTokens"] = agent.get("totalOutputTokens", 0) + output_tokens
        agent["totalCost"]         = round(agent.get("totalCost", 0.0) + cost, 6)

        stats["global"]["totalInputTokens"]  = stats["global"].get("totalInputTokens", 0)  + input_tokens
        stats["global"]["totalOutputTokens"] = stats["global"].get("totalOutputTokens", 0) + output_tokens
        stats["global"]["totalCost"]         = round(stats["global"].get("totalCost", 0.0) + cost, 6)

        _save(stats)
        _update_daily(cost)


def log_bot_started():
    """Καλείται όταν ξεκινά το bot."""
    with _lock:
        stats = _load()
        stats["global"]["botStartedAt"] = _now()
        for agent in stats["agents"].values():
            agent["status"] = "Idle"
        _save(stats)


def get_stats() -> dict:
    """Επιστρέφει τα τρέχοντα stats."""
    with _lock:
        return _load()

