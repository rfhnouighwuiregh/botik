import asyncio
import logging
import os
import time

from dotenv import load_dotenv
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.filters import Command, CommandObject

from rules import CONTENT_TYPES, get_content_types, is_message_allowed
from state import (
    load_state,
    is_moderation_enabled,
    set_moderation_enabled,
    get_section,
    register_section,
    unregister_section,
    set_section_enabled,
    set_admin_only,
    allow_type,
    deny_type,
    allow_dice_emoji,
    deny_dice_emoji,
    get_muted_users,
    mute_user,
    unmute_user,
    is_user_muted,
    get_blacklist,
    add_blacklist_word,
    remove_blacklist_word,
    find_blacklisted_word,
    is_flood_enabled,
    set_flood_enabled,
    get_flood_mute_minutes,
    set_flood_mute_minutes,
    get_flood_limits,
    get_flood_warnings_before_mute,
    set_flood_warnings_before_mute,
    increment_flood_warning,
    reset_flood_warning,
    save_preset,
    load_preset,
    list_presets,
    delete_preset,
)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN не найден. Создай файл .env рядом с bot.py и добавь в него строку:\n"
        "BOT_TOKEN=твой_токен_от_botfather"
    )

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

state = load_state()

# ============================================================
# КЭШ АДМИНОВ ЧАТА (чтобы не дёргать Telegram API на каждое сообщение)
# ============================================================
ADMIN_CACHE_TTL = 300  # секунд
_admin_cache: dict[int, tuple[float, set]] = {}


async def get_admin_ids(chat_id: int) -> set:
    now = time.time()
    cached = _admin_cache.get(chat_id)
    if cached and now - cached[0] < ADMIN_CACHE_TTL:
        return cached[1]
    admins = await bot.get_chat_administrators(chat_id)
    ids = {a.user.id for a in admins}
    _admin_cache[chat_id] = (now, ids)
    return ids


async def is_admin(message: Message) -> bool:
    ids = await get_admin_ids(message.chat.id)
    return message.from_user.id in ids


# ============================================================
# ДЕТЕКЦИЯ ФЛУДА (временные метки в памяти, не в state.json —
# они не должны переживать перезапуск, это просто скользящее окно)
# ============================================================
_flood_timestamps: dict[tuple[int, int], list] = {}
_repeat_tracker: dict[tuple[int, int], tuple] = {}   # (chat,user) -> (текст, подряд)
_mention_tracker: dict[tuple[int, int], tuple] = {}  # (chat,user) -> (@юзернейм, подряд)


def _is_flooding(chat_id: int, user_id: int) -> bool:
    max_messages, window_seconds = get_flood_limits(state)
    now = time.time()
    key = (chat_id, user_id)
    timestamps = _flood_timestamps.setdefault(key, [])
    timestamps.append(now)
    timestamps[:] = [t for t in timestamps if now - t <= window_seconds]
    return len(timestamps) > max_messages


def _is_repeated_text(chat_id: int, user_id: int, message: Message) -> bool:
    """Три (и больше) одинаковых подряд сообщения — тоже флуд."""
    text = message.text or message.caption
    if not text:
        return False
    normalized = text.strip().lower()
    key = (chat_id, user_id)
    last_text, count = _repeat_tracker.get(key, (None, 0))
    count = count + 1 if normalized == last_text else 1
    _repeat_tracker[key] = (normalized, count)
    return count >= 3


def _extract_mentions(message: Message) -> set:
    text = message.text or message.caption or ""
    entities = message.entities or message.caption_entities or []
    mentions = set()
    for e in entities:
        if e.type == "mention":
            mentions.add(text[e.offset: e.offset + e.length].lower())
        elif e.type == "text_mention" and e.user and e.user.username:
            mentions.add(f"@{e.user.username}".lower())
    return mentions


def _is_repeated_mention(chat_id: int, user_id: int, message: Message) -> bool:
    """Спам упоминаниями одного и того же человека в разных сообщениях подряд."""
    mentions = _extract_mentions(message)
    if not mentions:
        return False
    target = sorted(mentions)[0]
    key = (chat_id, user_id)
    last_target, count = _mention_tracker.get(key, (None, 0))
    count = count + 1 if target == last_target else 1
    _mention_tracker[key] = (target, count)
    return count >= 3


# ============================================================
# ПОИСК РАЗДЕЛА ПО ИМЕНИ (чтобы управлять из любой другой темы,
# например из отдельной служебной темы "Модерация")
# ============================================================
def find_section_by_name(name: str):
    """Возвращает (thread_id, section) по названию раздела, без учёта регистра."""
    name_lower = name.strip().lower()
    for tid, sec in state["sections"].items():
        if sec["name"].strip().lower() == name_lower:
            return tid, sec
    return None, None


def resolve_section(message: Message, name: str | None):
    """
    Если name передан — ищем раздел по имени (можно управлять из любой темы).
    Если name пустой — берём раздел текущей темы, где написана команда.
    Возвращает (thread_id, section) или (None, None), если не найден.
    """
    if name:
        return find_section_by_name(name)
    thread_id = message.message_thread_id
    return thread_id, get_section(state, thread_id)


def _not_found_reply(name: str | None) -> str:
    if name:
        return f"Раздел '{name}' не найден. Посмотри точные названия: /sections"
    return (
        "Раздел не зарегистрирован. Либо зайди в его тему и напиши /register <название>, "
        "либо укажи имя раздела в конце команды, например: /allow text Мемы"
    )


# ============================================================
# ОПРЕДЕЛЕНИЕ ПОЛЬЗОВАТЕЛЯ ДЛЯ /mute И /unmute
# ============================================================
async def resolve_target_user(message: Message, arg: str | None):
    """
    Возвращает (user_id, display_name) или (None, None), если не удалось определить.
    Приоритет:
    1. Reply этой командой на сообщение нужного пользователя — самый надёжный способ.
    2. Числовой user_id в аргументе.
    3. @username в аргументе (резолвится через Telegram, работает только если
       у пользователя есть публичный @username).
    """
    if message.reply_to_message and message.reply_to_message.from_user:
        u = message.reply_to_message.from_user
        name = f"@{u.username}" if u.username else u.full_name
        return u.id, name

    if not arg:
        return None, None

    arg = arg.strip()
    if arg.lstrip("-").isdigit():
        return int(arg), arg

    username = arg.lstrip("@")
    try:
        chat = await bot.get_chat(f"@{username}")
        return chat.id, f"@{username}"
    except Exception:
        return None, None


# ============================================================
# ПРЕДОХРАНИТЕЛЬ: при добавлении бота модерация выключена,
# пока админ явно не включит /start
# ============================================================
@dp.message(F.new_chat_members)
async def on_bot_added(message: Message):
    bot_info = await bot.get_me()
    if any(u.id == bot_info.id for u in message.new_chat_members):
        set_moderation_enabled(state, False)
        await message.reply(
            "Бот добавлен. Модерация ВЫКЛЮЧЕНА по умолчанию.\n"
            "1. Зайди в раздел, который нужно модерировать\n"
            "2. Зарегистрируй его: /register <название>\n"
            "3. Разреши нужные типы контента: /allow photo\n"
            "4. Включи модерацию глобально: /start"
        )


# ============================================================
# ГЛОБАЛЬНЫЕ КОМАНДЫ
# ============================================================
@dp.message(Command("start"))
async def cmd_start(message: Message):
    if not await is_admin(message):
        await message.reply("Только админ может включать модерацию.")
        return
    set_moderation_enabled(state, True)
    await message.reply("Модерация включена во всех зарегистрированных разделах.")


@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    if not await is_admin(message):
        await message.reply("Только админ может выключать модерацию.")
        return
    set_moderation_enabled(state, False)
    await message.reply("Модерация полностью выключена (предохранитель).")


@dp.message(Command("status"))
async def cmd_status(message: Message, command: CommandObject):
    global_status = "ВКЛ" if is_moderation_enabled(state) else "ВЫКЛ"
    name = command.args.strip() if command.args else None
    thread_id, section = resolve_section(message, name)

    if section:
        if section["admin_only"]:
            mode_line = "Режим: ТОЛЬКО АДМИН (ультра)"
        else:
            allowed = ", ".join(section["allowed_types"]) or "ничего не разрешено"
            mode_line = f"Разрешено: {allowed}"
            if "dice" in section["allowed_types"] and section["allowed_dice_emojis"]:
                mode_line += f"\nРазрешённые dice-эмодзи: {', '.join(section['allowed_dice_emojis'])}"

        await message.reply(
            f"Глобально: {global_status}\n"
            f"Раздел '{section['name']}': {'вкл' if section['enabled'] else 'выкл'}\n"
            f"{mode_line}"
        )
    else:
        count = len(state["sections"])
        await message.reply(
            f"Глобально: {global_status}\n"
            f"Зарегистрированных разделов: {count}\n"
            f"Список всех разделов: /sections"
        )


@dp.message(Command("sections"))
async def cmd_sections(message: Message):
    if not state["sections"]:
        await message.reply("Ни один раздел ещё не зарегистрирован. Используй /register внутри нужной темы.")
        return

    lines = []
    for tid, sec in state["sections"].items():
        status = "вкл" if sec["enabled"] else "выкл"
        if sec["admin_only"]:
            rule = "только админ"
        else:
            rule = "разрешено: " + (", ".join(sec["allowed_types"]) or "—")
        lines.append(f"• {sec['name']} (id={tid}) — {status}, {rule}")

    await message.reply("\n".join(lines))


# ============================================================
# ПРЕСЕТЫ СЦЕНАРИЕВ — сохранить/загрузить все настройки разом
# ============================================================
@dp.message(Command("preset_save"))
async def cmd_preset_save(message: Message, command: CommandObject):
    if not await is_admin(message):
        await message.reply("Только админ может это делать.")
        return
    if not command.args:
        await message.reply("Формат: /preset_save <название>\nНапример: /preset_save вечеринка")
        return
    name = command.args.strip()
    save_preset(state, name)
    await message.reply(
        f"Пресет '{name}' сохранён: текущие разделы, чёрный список, антифлуд и статус модерации.\n"
        f"Применить позже: /preset_load {name}"
    )


@dp.message(Command("preset_load"))
async def cmd_preset_load(message: Message, command: CommandObject):
    if not await is_admin(message):
        await message.reply("Только админ может это делать.")
        return
    if not command.args:
        await message.reply("Формат: /preset_load <название>\nСписок сохранённых: /presets")
        return
    name = command.args.strip()
    if load_preset(state, name):
        await message.reply(f"Пресет '{name}' применён — разделы, чёрный список и антифлуд заменены на сохранённые.")
    else:
        await message.reply(f"Пресета '{name}' нет. Список сохранённых: /presets")


@dp.message(Command("presets"))
async def cmd_presets_list(message: Message):
    names = list_presets(state)
    if not names:
        await message.reply("Пресетов пока нет. Сохранить текущие настройки: /preset_save <название>")
        return
    await message.reply("Сохранённые пресеты:\n" + "\n".join(f"• {n}" for n in names))


@dp.message(Command("preset_delete"))
async def cmd_preset_delete(message: Message, command: CommandObject):
    if not await is_admin(message):
        await message.reply("Только админ может это делать.")
        return
    if not command.args:
        await message.reply("Формат: /preset_delete <название>")
        return
    name = command.args.strip()
    if delete_preset(state, name):
        await message.reply(f"Пресет '{name}' удалён.")
    else:
        await message.reply(f"Пресета '{name}' не было.")


@dp.message(Command("types"))
async def cmd_types(message: Message):
    await message.reply(
        "Доступные типы контента для /allow и /deny:\n"
        + "\n".join(f"- {t}" for t in CONTENT_TYPES)
    )


# ============================================================
# РЕГИСТРАЦИЯ РАЗДЕЛА (можно только находясь внутри его темы —
# отсюда бот и берёт thread_id)
# ============================================================
@dp.message(Command("register"))
async def cmd_register(message: Message, command: CommandObject):
    if not await is_admin(message):
        await message.reply("Только админ может регистрировать разделы.")
        return
    if message.message_thread_id is None:
        await message.reply("Команду нужно писать внутри конкретного раздела (темы), который регистрируешь.")
        return

    thread_id = message.message_thread_id
    name = command.args.strip() if command.args else f"раздел {thread_id}"
    register_section(state, thread_id, name)
    await message.reply(
        f"Раздел '{name}' зарегистрирован (id={thread_id}).\n"
        f"Пока ничего не разрешено — добавляй типы командой /allow <тип> [название раздела].\n"
        f"Теперь этим разделом можно управлять из любой другой темы, указывая '{name}' последним аргументом."
    )


# ============================================================
# УПРАВЛЕНИЕ РАЗДЕЛОМ — можно писать И внутри самого раздела
# (тогда название в конце не нужно), И из любой другой темы,
# указав название раздела последним словом/словами команды.
# ============================================================
@dp.message(Command("unregister"))
async def cmd_unregister(message: Message, command: CommandObject):
    if not await is_admin(message):
        await message.reply("Только админ может это делать.")
        return
    name = command.args.strip() if command.args else None
    thread_id, section = resolve_section(message, name)
    if section is None:
        await message.reply(_not_found_reply(name))
        return
    unregister_section(state, thread_id)
    await message.reply(f"Раздел '{section['name']}' снят с модерации, настройки удалены.")


@dp.message(Command("section_on"))
async def cmd_section_on(message: Message, command: CommandObject):
    if not await is_admin(message):
        await message.reply("Только админ может это менять.")
        return
    name = command.args.strip() if command.args else None
    thread_id, section = resolve_section(message, name)
    if section is None:
        await message.reply(_not_found_reply(name))
        return
    set_section_enabled(state, thread_id, True)
    await message.reply(f"Модерация раздела '{section['name']}' включена.")


@dp.message(Command("section_off"))
async def cmd_section_off(message: Message, command: CommandObject):
    if not await is_admin(message):
        await message.reply("Только админ может это менять.")
        return
    name = command.args.strip() if command.args else None
    thread_id, section = resolve_section(message, name)
    if section is None:
        await message.reply(_not_found_reply(name))
        return
    set_section_enabled(state, thread_id, False)
    await message.reply(f"Модерация раздела '{section['name']}' выключена (бот его не трогает).")


@dp.message(Command("adminonly_on"))
async def cmd_adminonly_on(message: Message, command: CommandObject):
    if not await is_admin(message):
        await message.reply("Только админ может это менять.")
        return
    name = command.args.strip() if command.args else None
    thread_id, section = resolve_section(message, name)
    if section is None:
        await message.reply(_not_found_reply(name))
        return
    set_admin_only(state, thread_id, True)
    await message.reply(f"Ультра-режим включён для '{section['name']}': писать теперь может только админ.")


@dp.message(Command("adminonly_off"))
async def cmd_adminonly_off(message: Message, command: CommandObject):
    if not await is_admin(message):
        await message.reply("Только админ может это менять.")
        return
    name = command.args.strip() if command.args else None
    thread_id, section = resolve_section(message, name)
    if section is None:
        await message.reply(_not_found_reply(name))
        return
    set_admin_only(state, thread_id, False)
    await message.reply(f"Ультра-режим выключен для '{section['name']}', снова действуют разрешённые типы.")


@dp.message(Command("allow"))
async def cmd_allow(message: Message, command: CommandObject):
    if not await is_admin(message):
        await message.reply("Только админ может это менять.")
        return
    if not command.args:
        await message.reply(
            f"Формат: /allow <тип> [название раздела]\n"
            f"Например: /allow text Мемы\n"
            f"Доступные типы: {', '.join(CONTENT_TYPES)}"
        )
        return

    parts = command.args.strip().split(maxsplit=1)
    ctype = parts[0].lower()
    name = parts[1] if len(parts) > 1 else None

    if ctype not in CONTENT_TYPES:
        await message.reply(f"Неизвестный тип '{ctype}'. Доступные: {', '.join(CONTENT_TYPES)}")
        return

    thread_id, section = resolve_section(message, name)
    if section is None:
        await message.reply(_not_found_reply(name))
        return

    allow_type(state, thread_id, ctype)
    await message.reply(f"В разделе '{section['name']}' теперь разрешено: {ctype}")


@dp.message(Command("deny"))
async def cmd_deny(message: Message, command: CommandObject):
    if not await is_admin(message):
        await message.reply("Только админ может это менять.")
        return
    if not command.args:
        await message.reply(
            f"Формат: /deny <тип> [название раздела]\n"
            f"Например: /deny text Мемы\n"
            f"Доступные типы: {', '.join(CONTENT_TYPES)}"
        )
        return

    parts = command.args.strip().split(maxsplit=1)
    ctype = parts[0].lower()
    name = parts[1] if len(parts) > 1 else None

    thread_id, section = resolve_section(message, name)
    if section is None:
        await message.reply(_not_found_reply(name))
        return

    deny_type(state, thread_id, ctype)
    await message.reply(f"В разделе '{section['name']}' теперь запрещено: {ctype}")


@dp.message(Command("allow_dice"))
async def cmd_allow_dice(message: Message, command: CommandObject):
    if not await is_admin(message):
        await message.reply("Только админ может это менять.")
        return
    if not command.args:
        await message.reply("Формат: /allow_dice <эмодзи> [название раздела]\nНапример: /allow_dice 🎰 Слоты")
        return

    parts = command.args.strip().split(maxsplit=1)
    emoji = parts[0]
    name = parts[1] if len(parts) > 1 else None

    thread_id, section = resolve_section(message, name)
    if section is None:
        await message.reply(_not_found_reply(name))
        return

    allow_dice_emoji(state, thread_id, emoji)
    await message.reply(
        f"В разделе '{section['name']}' теперь разрешён dice-эмодзи {emoji}.\n"
        f"Если это первый разрешённый dice-эмодзи — остальные dice больше не проходят."
    )


@dp.message(Command("deny_dice"))
async def cmd_deny_dice(message: Message, command: CommandObject):
    if not await is_admin(message):
        await message.reply("Только админ может это менять.")
        return
    if not command.args:
        await message.reply("Формат: /deny_dice <эмодзи> [название раздела]\nНапример: /deny_dice 🎲 Слоты")
        return

    parts = command.args.strip().split(maxsplit=1)
    emoji = parts[0]
    name = parts[1] if len(parts) > 1 else None

    thread_id, section = resolve_section(message, name)
    if section is None:
        await message.reply(_not_found_reply(name))
        return

    deny_dice_emoji(state, thread_id, emoji)
    await message.reply(f"Dice-эмодзи {emoji} больше не в списке разрешённых для '{section['name']}'.")


# ============================================================
# МЬЮТ ОТДЕЛЬНЫХ ПОЛЬЗОВАТЕЛЕЙ (действует во всей группе, во всех
# разделах сразу, включая незарегистрированные — пока не снят /unmute)
# ============================================================
@dp.message(Command("mute"))
async def cmd_mute(message: Message, command: CommandObject):
    if not await is_admin(message):
        await message.reply("Только админ может мьютить.")
        return

    user_id, name = await resolve_target_user(message, command.args)
    if user_id is None:
        await message.reply(
            "Не понял, кого мьютить. Либо ответь (reply) этой командой на сообщение "
            "нужного пользователя, либо укажи @username или числовой user_id.\n"
            "Примеры: /mute (реплаем на сообщение) | /mute @spammer | /mute 123456789"
        )
        return

    admin_ids = await get_admin_ids(message.chat.id)
    if user_id in admin_ids:
        await message.reply("Нельзя замьютить админа.")
        return

    mute_user(state, message.chat.id, user_id, name)
    await message.reply(
        f"{name} замьючен: его сообщения будут удаляться во всей группе, "
        f"во всех разделах, пока не выполнишь /unmute."
    )


@dp.message(Command("unmute"))
async def cmd_unmute(message: Message, command: CommandObject):
    if not await is_admin(message):
        await message.reply("Только админ может это делать.")
        return

    user_id, name = await resolve_target_user(message, command.args)
    if user_id is None:
        await message.reply(
            "Не понял, кого размьютить. Ответь (reply) на его сообщение "
            "или укажи @username / user_id."
        )
        return

    if unmute_user(state, message.chat.id, user_id):
        await message.reply(f"{name} размьючен, снова может писать.")
    else:
        await message.reply(f"{name} и так не был в списке замьюченных.")


@dp.message(Command("muted"))
async def cmd_muted(message: Message):
    muted = get_muted_users(state, message.chat.id)
    if not muted:
        await message.reply("Никто не замьючен.")
        return
    lines = []
    for uid, info in muted.items():
        until = info.get("until")
        if until:
            minutes_left = max(0, int((until - time.time()) / 60))
            lines.append(f"• {info['name']} (id={uid}) — флуд, ещё ~{minutes_left} мин.")
        else:
            lines.append(f"• {info['name']} (id={uid}) — бессрочно")
    await message.reply("Замьюченные пользователи:\n" + "\n".join(lines))


# ============================================================
# ЧЁРНЫЙ СПИСОК СЛОВ (общий на весь бот, действует в любом
# зарегистрированном и включённом разделе)
# ============================================================
@dp.message(Command("blacklist_add"))
async def cmd_blacklist_add(message: Message, command: CommandObject):
    if not await is_admin(message):
        await message.reply("Только админ может это менять.")
        return
    if not command.args:
        await message.reply("Формат: /blacklist_add <слово>")
        return

    word = command.args.strip()
    if add_blacklist_word(state, word):
        await message.reply(f"Слово «{word}» добавлено в чёрный список.")
    else:
        await message.reply(f"Слово «{word}» уже было в чёрном списке.")


@dp.message(Command("blacklist_remove"))
async def cmd_blacklist_remove(message: Message, command: CommandObject):
    if not await is_admin(message):
        await message.reply("Только админ может это менять.")
        return
    if not command.args:
        await message.reply("Формат: /blacklist_remove <слово>")
        return

    word = command.args.strip()
    if remove_blacklist_word(state, word):
        await message.reply(f"Слово «{word}» убрано из чёрного списка.")
    else:
        await message.reply(f"Слова «{word}» и не было в чёрном списке.")


@dp.message(Command("blacklist"))
async def cmd_blacklist_show(message: Message):
    words = get_blacklist(state)
    if not words:
        await message.reply("Чёрный список пуст.")
        return
    await message.reply("Запрещённые слова:\n" + "\n".join(f"• {w}" for w in words))


# ============================================================
# АНТИФЛУД (общий на весь бот, не касается админов)
# 3 предупреждения, дальше — мут на настраиваемое время каждый раз
# ============================================================
@dp.message(Command("flood_on"))
async def cmd_flood_on(message: Message):
    if not await is_admin(message):
        await message.reply("Только админ может это менять.")
        return
    set_flood_enabled(state, True)
    minutes = get_flood_mute_minutes(state)
    await message.reply(f"Антифлуд включён. После 3 предупреждений — мут на {minutes} мин.")


@dp.message(Command("flood_off"))
async def cmd_flood_off(message: Message):
    if not await is_admin(message):
        await message.reply("Только админ может это менять.")
        return
    set_flood_enabled(state, False)
    await message.reply("Антифлуд выключен.")


@dp.message(Command("flood_mute_time"))
async def cmd_flood_mute_time(message: Message, command: CommandObject):
    if not await is_admin(message):
        await message.reply("Только админ может это менять.")
        return
    if not command.args or not command.args.strip().isdigit():
        await message.reply("Формат: /flood_mute_time <минуты>\nНапример: /flood_mute_time 10")
        return
    minutes = int(command.args.strip())
    if minutes <= 0:
        await message.reply("Число минут должно быть больше нуля.")
        return
    set_flood_mute_minutes(state, minutes)
    await message.reply(f"Теперь после {get_flood_warnings_before_mute(state)} предупреждений за флуд — мут на {minutes} мин.")


@dp.message(Command("flood_warn_count"))
async def cmd_flood_warn_count(message: Message, command: CommandObject):
    if not await is_admin(message):
        await message.reply("Только админ может это менять.")
        return
    if not command.args or not command.args.strip().isdigit():
        await message.reply("Формат: /flood_warn_count <число>\nНапример: /flood_warn_count 2")
        return
    count = int(command.args.strip())
    if count <= 0:
        await message.reply("Число предупреждений должно быть больше нуля.")
        return
    set_flood_warnings_before_mute(state, count)
    await message.reply(f"Теперь мут выдаётся после {count} предупреждений за флуд.")


@dp.message(Command("flood_status"))
async def cmd_flood_status(message: Message):
    enabled = is_flood_enabled(state)
    minutes = get_flood_mute_minutes(state)
    max_msg, window = get_flood_limits(state)
    warn_count = get_flood_warnings_before_mute(state)
    await message.reply(
        f"Антифлуд: {'ВКЛ' if enabled else 'ВЫКЛ'}\n"
        f"Порог: {max_msg} сообщений за {window} сек.\n"
        f"Мут после {warn_count} предупреждений: {minutes} мин.\n"
        f"Действует на всех, включая админов."
    )


@dp.message(Command("flood_reset"))
async def cmd_flood_reset(message: Message, command: CommandObject):
    if not await is_admin(message):
        await message.reply("Только админ может это менять.")
        return
    user_id, name = await resolve_target_user(message, command.args)
    if user_id is None:
        await message.reply(
            "Не понял, кому сбросить предупреждения. Ответь (reply) на сообщение "
            "нужного пользователя или укажи @username / user_id."
        )
        return
    reset_flood_warning(state, message.chat.id, user_id)
    await message.reply(f"Счётчик предупреждений за флуд для {name} сброшен.")


# ============================================================
# СЛУЖЕБНЫЕ КОМАНДЫ
# ============================================================
@dp.message(Command("id"))
async def get_thread_id(message: Message):
    thread_id = message.message_thread_id
    await message.reply(f"thread_id этого раздела: {thread_id}")


@dp.message(Command("ids"))
async def get_sticker_id(message: Message):
    if message.reply_to_message and message.reply_to_message.sticker:
        sticker = message.reply_to_message.sticker
        await message.reply(
            f"file_unique_id стикера: {sticker.file_unique_id}\n"
            f"анимированный: {sticker.is_animated}"
        )
        return
    await message.reply("Ответь этой командой (reply) на сообщение со стикером, чтобы узнать его ID.")


@dp.message(Command("whatis"))
async def cmd_whatis(message: Message):
    """
    Диагностика: ответом (reply) на любое сообщение показывает,
    какими типами контента бот его считает (что уйдёт в проверку /allow).
    """
    target = message.reply_to_message
    if not target:
        await message.reply("Ответь этой командой (reply) на сообщение, которое нужно проверить.")
        return

    types = get_content_types(target)
    section = get_section(state, message.message_thread_id)
    lines = [f"Бот видит типы контента: {', '.join(sorted(types))}"]
    if section:
        allowed = set(section.get("allowed_types", []))
        missing = types - allowed
        if missing:
            lines.append(f"Не разрешено в '{section['name']}': {', '.join(sorted(missing))}")
        else:
            lines.append(f"В разделе '{section['name']}' это сообщение разрешено.")
    await message.reply("\n".join(lines))


# ============================================================
# ОСНОВНАЯ МОДЕРАЦИЯ
# ============================================================
@dp.message(F.chat.type == "supergroup")
async def moderate(message: Message):
    if not is_moderation_enabled(state):
        return  # предохранитель: модерация выключена целиком

    if message.from_user and is_user_muted(state, message.chat.id, message.from_user.id):
        try:
            await message.delete()
        except Exception as e:
            logging.warning(f"Не удалось удалить сообщение замьюченного пользователя: {e}")
        return  # замьюченный не может писать нигде в группе, независимо от раздела

    # Чёрный список слов — работает во всей группе, в любом разделе,
    # даже незарегистрированном под остальную модерацию
    thread_id = message.message_thread_id
    text_to_check = message.text or message.caption
    bad_word = find_blacklisted_word(state, text_to_check)
    if bad_word:
        try:
            await message.delete()
        except Exception as e:
            logging.warning(f"Не удалось удалить сообщение: {e}")
        try:
            await bot.send_message(
                message.chat.id,
                f"Сообщение удалено. Причина: запрещённое слово «{bad_word}».",
                message_thread_id=thread_id,
            )
        except Exception as e:
            logging.warning(f"Не удалось отправить уведомление об удалении: {e}")
        return

    # Антифлуд — теперь касается всех, включая админов
    if is_flood_enabled(state) and message.from_user:
        is_flood = (
            _is_flooding(message.chat.id, message.from_user.id)
            or _is_repeated_text(message.chat.id, message.from_user.id, message)
            or _is_repeated_mention(message.chat.id, message.from_user.id, message)
        )
        if is_flood:
            name = f"@{message.from_user.username}" if message.from_user.username else message.from_user.full_name
            warn_count = increment_flood_warning(state, message.chat.id, message.from_user.id)
            warnings_limit = get_flood_warnings_before_mute(state)

            try:
                await message.delete()
            except Exception as e:
                logging.warning(f"Не удалось удалить сообщение: {e}")

            if warn_count < warnings_limit:
                try:
                    await bot.send_message(
                        message.chat.id,
                        f"{name}, предупреждение за флуд ({warn_count}/{warnings_limit}). "
                        f"Ещё немного — и мут на {get_flood_mute_minutes(state)} мин.",
                        message_thread_id=thread_id,
                    )
                except Exception as e:
                    logging.warning(f"Не удалось отправить предупреждение: {e}")
            else:
                minutes = get_flood_mute_minutes(state)
                until = time.time() + minutes * 60
                mute_user(state, message.chat.id, message.from_user.id, name, until=until)
                reset_flood_warning(state, message.chat.id, message.from_user.id)
                try:
                    await bot.send_message(
                        message.chat.id,
                        f"{name} замьючен на {minutes} мин. за флуд. "
                        f"После мута счётчик предупреждений обнулён — снова 3 попытки.",
                        message_thread_id=thread_id,
                    )
                except Exception as e:
                    logging.warning(f"Не удалось отправить уведомление о муте: {e}")
            return

    section = get_section(state, thread_id)

    if section is None or not section.get("enabled", True):
        return  # раздел не зарегистрирован или выключен — не трогаем

    if section.get("admin_only"):
        admin_ids = await get_admin_ids(message.chat.id)
        if message.from_user.id in admin_ids:
            return  # админу можно всё
        try:
            await message.delete()
        except Exception as e:
            logging.warning(f"Не удалось удалить сообщение: {e}")
        return

    if not is_message_allowed(message, section):
        detected = get_content_types(message)
        logging.info(
            f"Удаляю сообщение в разделе '{section['name']}': "
            f"обнаружено={detected}, разрешено={section.get('allowed_types')}"
        )
        try:
            await message.delete()
        except Exception as e:
            logging.warning(f"Не удалось удалить сообщение: {e}")


async def health(request):
    return web.Response(text="OK")


async def start_web_server():
    """
    Render (Web Service) требует открытый HTTP-порт, иначе считает
    сервис нерабочим. Всё в одном процессе с ботом: сервер поднимается,
    сразу отдаёт управление дальше — на polling.
    """
    app = web.Application()
    app.router.add_get("/health", health)
    runner = web.AppRunner(app, access_log=logging.getLogger("aiohttp.access"))
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port, reuse_address=True, reuse_port=True)
    await site.start()
    logging.info(f"Health-сервер запущен на порту {port}")


async def main():
    logging.basicConfig(level=logging.INFO)
    await start_web_server()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
