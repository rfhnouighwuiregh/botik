"""
Определение типа контента сообщения и проверка, разрешён ли он
в конкретном разделе (по настройкам из state.py).
"""

from aiogram.types import Message

# Все типы контента, которые понимает бот. Используются в /allow, /deny, /types
CONTENT_TYPES = [
    "photo",     # фото
    "video",     # видео
    "gif",       # анимация/гифка
    "text",      # обычный текст (без ссылки)
    "link",      # сообщение содержит ссылку (обычную или гиперссылку в тексте)
    "sticker",   # стикер (любой, включая анимированные и видео-стикеры)
    "voice",     # голосовое сообщение
    "document",  # файл/документ
    "dice",      # анимированный dice-эмодзи: 🎰 🎲 🎯 🏀 ⚽ 🎳
]


def _has_link(message: Message) -> bool:
    entities = message.entities or message.caption_entities or []
    return any(e.type in ("url", "text_link") for e in entities)


def get_content_types(message: Message) -> set:
    """
    Возвращает множество типов контента, присутствующих в сообщении.
    Подпись (caption) к медиа не считается отдельным "text" —
    она едет вместе с медиа-типом и не мешает, если медиа разрешено.
    """
    types = set()

    if message.photo:
        types.add("photo")
    if message.video:
        types.add("video")
    if message.animation:
        types.add("gif")
    if message.sticker:
        types.add("sticker")
    if message.voice:
        types.add("voice")
    if message.document and not message.animation:
        # ВАЖНО: GIF (Animation) в Telegram Bot API технически хранится как
        # расширение Document, поэтому message.document тоже не пустой у гифок.
        # Если это animation — не считаем его ещё и document.
        types.add("document")
    if message.dice:
        types.add("dice")

    has_link = _has_link(message)
    if has_link:
        types.add("link")

    # "text" — только для голых текстовых сообщений без медиа и без ссылки
    if message.text and not has_link and not types:
        types.add("text")

    if not types:
        # что-то нераспознанное (опрос, геолокация, контакт и т.п.)
        types.add("other")

    return types


def is_message_allowed(message: Message, section: dict) -> bool:
    """
    section — словарь раздела из state.py (allowed_types, allowed_dice_emojis).
    Сообщение разрешено, если ВСЕ его типы контента входят в allowed_types.
    """
    allowed = set(section.get("allowed_types", []))
    types = get_content_types(message)

    if not types.issubset(allowed):
        return False

    if "dice" in types:
        allowed_emojis = section.get("allowed_dice_emojis", [])
        if allowed_emojis and message.dice.emoji not in allowed_emojis:
            return False

    return True
