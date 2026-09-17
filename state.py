"""
Хранение состояния модерации в Redis (Upstash), а не в локальном файле —
на Render (и большинстве хостингов) файловая система эфемерна и
сбрасывается при каждом передеплое/перезапуске. Redis живёт отдельно
от контейнера бота и переживает любые передеплои.

Нужны переменные окружения (заданные в .env локально и в Render →
Environment на сервере):
    UPSTASH_REDIS_REST_URL
    UPSTASH_REDIS_REST_TOKEN

Разделы больше НЕ прописываются в коде — они регистрируются
командой /register прямо внутри нужной темы группы.

Структура состояния (хранится как один JSON-объект под ключом STATE_KEY):
{
    "moderation_enabled": false,       # глобальный предохранитель
    "sections": {
        "123456": {                     # ключ — thread_id темы (строкой, так требует JSON)
            "name": "Мемы",
            "enabled": true,
            "admin_only": false,        # "ультра"-режим: писать может только админ
            "allowed_types": ["photo", "video", "link"],
            "allowed_dice_emojis": []   # если пусто — разрешены любые dice-эмодзи из allowed_types
        }
    }
}
"""

import json
import os
import time

from upstash_redis import Redis

STATE_KEY = "moderator_bot_state"

_redis = Redis(
    url=os.getenv("UPSTASH_REDIS_REST_URL"),
    token=os.getenv("UPSTASH_REDIS_REST_TOKEN"),
)

DEFAULT_STATE = {
    "moderation_enabled": False,  # предохранитель: по умолчанию ВЫКЛЮЧЕНО
    "sections": {},
    "muted_users": {},  # {"<chat_id>": {"<user_id>": {"name": "...", "until": <ts|None>}}}
    "blacklist": [],    # список запрещённых слов (в нижнем регистре)
    "flood": {
        "enabled": False,       # тумблер антифлуда
        "mute_minutes": 5,      # на сколько минут мьютить после N-го предупреждения
        "max_messages": 5,      # порог: столько сообщений...
        "window_seconds": 10,   # ...за столько секунд считается флудом
        "warnings_before_mute": 3,  # сколько предупреждений даётся перед мутом
    },
    "flood_warnings": {},  # {"<chat_id>": {"<user_id>": count}}
    "presets": {},  # {"<название>": {снимок moderation_enabled/sections/blacklist/flood}}
}


def load_state() -> dict:
    raw = _redis.get(STATE_KEY)
    if raw is None:
        save_state(DEFAULT_STATE)
        return json.loads(json.dumps(DEFAULT_STATE))

    data = json.loads(raw)
    data.setdefault("moderation_enabled", False)
    data.setdefault("sections", {})
    data.setdefault("muted_users", {})
    data.setdefault("blacklist", [])
    data.setdefault("flood", {
        "enabled": False,
        "mute_minutes": 5,
        "max_messages": 5,
        "window_seconds": 10,
        "warnings_before_mute": 3,
    })
    data["flood"].setdefault("warnings_before_mute", 3)
    data.setdefault("flood_warnings", {})
    data.setdefault("presets", {})
    return data


def save_state(state: dict) -> None:
    _redis.set(STATE_KEY, json.dumps(state, ensure_ascii=False))


# ---------------- глобальный предохранитель ----------------

def is_moderation_enabled(state: dict) -> bool:
    return state.get("moderation_enabled", False)


def set_moderation_enabled(state: dict, value: bool) -> None:
    state["moderation_enabled"] = value
    save_state(state)


# ---------------- разделы ----------------

def get_section(state: dict, thread_id) -> dict | None:
    if thread_id is None:
        return None
    return state["sections"].get(str(thread_id))


def register_section(state: dict, thread_id: int, name: str) -> None:
    state["sections"][str(thread_id)] = {
        "name": name,
        "enabled": True,
        "admin_only": False,
        "allowed_types": [],
        "allowed_dice_emojis": [],
    }
    save_state(state)


def unregister_section(state: dict, thread_id: int) -> None:
    state["sections"].pop(str(thread_id), None)
    save_state(state)


def set_section_enabled(state: dict, thread_id: int, value: bool) -> None:
    section = get_section(state, thread_id)
    if section:
        section["enabled"] = value
        save_state(state)


def set_admin_only(state: dict, thread_id: int, value: bool) -> None:
    section = get_section(state, thread_id)
    if section:
        section["admin_only"] = value
        save_state(state)


def allow_type(state: dict, thread_id: int, content_type: str) -> None:
    section = get_section(state, thread_id)
    if section and content_type not in section["allowed_types"]:
        section["allowed_types"].append(content_type)
        save_state(state)


def deny_type(state: dict, thread_id: int, content_type: str) -> None:
    section = get_section(state, thread_id)
    if section and content_type in section["allowed_types"]:
        section["allowed_types"].remove(content_type)
        save_state(state)


def allow_dice_emoji(state: dict, thread_id: int, emoji: str) -> None:
    section = get_section(state, thread_id)
    if section:
        if "dice" not in section["allowed_types"]:
            section["allowed_types"].append("dice")
        if emoji not in section["allowed_dice_emojis"]:
            section["allowed_dice_emojis"].append(emoji)
        save_state(state)


def deny_dice_emoji(state: dict, thread_id: int, emoji: str) -> None:
    section = get_section(state, thread_id)
    if section and emoji in section.get("allowed_dice_emojis", []):
        section["allowed_dice_emojis"].remove(emoji)
        save_state(state)


# ---------------- мьют отдельных пользователей ----------------
# Мьют действует на весь чат (во всех разделах/темах сразу), а не на
# конкретный раздел — если человека замьютили, его сообщения удаляются
# везде, пока админ явно не снимет мьют.

def get_muted_users(state: dict, chat_id: int) -> dict:
    return state.setdefault("muted_users", {}).setdefault(str(chat_id), {})


def mute_user(state: dict, chat_id: int, user_id: int, name: str, until: float | None = None) -> None:
    """until — unix-время, когда мьют истекает сам. None — бессрочно (ручной /mute)."""
    chat_muted = state.setdefault("muted_users", {}).setdefault(str(chat_id), {})
    chat_muted[str(user_id)] = {"name": name, "until": until}
    save_state(state)


def unmute_user(state: dict, chat_id: int, user_id: int) -> bool:
    chat_muted = state.get("muted_users", {}).get(str(chat_id), {})
    if str(user_id) in chat_muted:
        del chat_muted[str(user_id)]
        save_state(state)
        return True
    return False


def is_user_muted(state: dict, chat_id: int, user_id: int) -> bool:
    chat_muted = state.get("muted_users", {}).get(str(chat_id), {})
    entry = chat_muted.get(str(user_id))
    if not entry:
        return False
    until = entry.get("until")
    if until is not None and time.time() >= until:
        # временный мьют истёк — снимаем автоматически
        del chat_muted[str(user_id)]
        save_state(state)
        return False
    return True


# ---------------- чёрный список слов ----------------
# Один общий список на весь бот (не привязан к конкретному разделу),
# срабатывает в любом зарегистрированном и включённом разделе.

def get_blacklist(state: dict) -> list:
    return state.setdefault("blacklist", [])


def add_blacklist_word(state: dict, word: str) -> bool:
    """Возвращает True, если слово добавлено (False — уже было в списке)."""
    word = word.strip().lower()
    blacklist = state.setdefault("blacklist", [])
    if word in blacklist:
        return False
    blacklist.append(word)
    save_state(state)
    return True


def remove_blacklist_word(state: dict, word: str) -> bool:
    """Возвращает True, если слово было в списке и удалено."""
    word = word.strip().lower()
    blacklist = state.setdefault("blacklist", [])
    if word not in blacklist:
        return False
    blacklist.remove(word)
    save_state(state)
    return True


def find_blacklisted_word(state: dict, text: str | None) -> str | None:
    """Возвращает первое найденное запрещённое слово в тексте, либо None."""
    if not text:
        return None
    lowered = text.lower()
    for word in state.get("blacklist", []):
        if word in lowered:
            return word
    return None


# ---------------- антифлуд ----------------
# Работает во всей группе сразу (как мьют и чёрный список), не привязан
# к конкретному разделу. Админов не касается — это проверяется в bot.py.

def is_flood_enabled(state: dict) -> bool:
    return state.get("flood", {}).get("enabled", False)


def set_flood_enabled(state: dict, value: bool) -> None:
    state.setdefault("flood", {})["enabled"] = value
    save_state(state)


def get_flood_mute_minutes(state: dict) -> int:
    return state.get("flood", {}).get("mute_minutes", 5)


def set_flood_mute_minutes(state: dict, minutes: int) -> None:
    state.setdefault("flood", {})["mute_minutes"] = minutes
    save_state(state)


def get_flood_limits(state: dict) -> tuple:
    f = state.get("flood", {})
    return f.get("max_messages", 5), f.get("window_seconds", 10)


def get_flood_warnings_before_mute(state: dict) -> int:
    return state.get("flood", {}).get("warnings_before_mute", 3)


def set_flood_warnings_before_mute(state: dict, count: int) -> None:
    state.setdefault("flood", {})["warnings_before_mute"] = count
    save_state(state)


def get_flood_warning_count(state: dict, chat_id: int, user_id: int) -> int:
    return state.get("flood_warnings", {}).get(str(chat_id), {}).get(str(user_id), 0)


def increment_flood_warning(state: dict, chat_id: int, user_id: int) -> int:
    chat_warn = state.setdefault("flood_warnings", {}).setdefault(str(chat_id), {})
    count = chat_warn.get(str(user_id), 0) + 1
    chat_warn[str(user_id)] = count
    save_state(state)
    return count


def reset_flood_warning(state: dict, chat_id: int, user_id: int) -> None:
    chat_warn = state.get("flood_warnings", {}).get(str(chat_id), {})
    if str(user_id) in chat_warn:
        del chat_warn[str(user_id)]
        save_state(state)


# ---------------- пресеты сценариев ----------------
# Именованный снимок настроек модерации: разделы, чёрный список, антифлуд
# и включена ли модерация. Хранится в том же Redis-состоянии — переживает
# рестарты сервиса точно так же, как и текущие настройки.

def save_preset(state: dict, name: str) -> None:
    """Сохраняет ТЕКУЩИЕ настройки под именем name (перезаписывает, если уже было)."""
    snapshot = {
        "moderation_enabled": state.get("moderation_enabled", False),
        "sections": json.loads(json.dumps(state.get("sections", {}))),
        "blacklist": json.loads(json.dumps(state.get("blacklist", []))),
        "flood": json.loads(json.dumps(state.get("flood", {}))),
    }
    state.setdefault("presets", {})[name] = snapshot
    save_state(state)


def load_preset(state: dict, name: str) -> bool:
    """Применяет сохранённый пресет как текущие настройки. False, если пресета нет."""
    presets = state.get("presets", {})
    if name not in presets:
        return False
    snapshot = presets[name]
    state["moderation_enabled"] = snapshot.get("moderation_enabled", False)
    state["sections"] = json.loads(json.dumps(snapshot.get("sections", {})))
    state["blacklist"] = json.loads(json.dumps(snapshot.get("blacklist", [])))
    state["flood"] = json.loads(json.dumps(snapshot.get("flood", {})))
    save_state(state)
    return True


def list_presets(state: dict) -> list:
    return list(state.get("presets", {}).keys())


def delete_preset(state: dict, name: str) -> bool:
    presets = state.get("presets", {})
    if name in presets:
        del presets[name]
        save_state(state)
        return True
    return False
