"""Бот, который добавляет музыку к видеосообщениям («кружочкам»).

Как работает:
  * бот подключён к аккаунту в Telegram Business -> Чат-боты;
  * владелец отправляет кружочек из телефона в личный чат, бот получает
    business_message, скачивает mp4, микширует голос с треком через ffmpeg,
    отправляет новую версию от имени аккаунта и удаляет оригинал;
  * кружочек, отправленный самому боту, возвращается с музыкой — так можно
    проверить настройки без Business;
  * /music — трек, громкость, голос, удаление оригинала.

Настройки хранятся в settings.json рядом с main.py, треки — в папке music/.

Доступ к Bot API:
  api.telegram.org в сети блокируется, поэтому есть два варианта —
  обычный SOCKS5/HTTP-прокси (PROXY_URL) или «прибитый» рабочий IP
  Telegram (API_IP). MTProto-прокси для Bot API не годится: он умеет
  пропускать только MTProto, а Bot API — обычный HTTPS.

Запуск: py main.py
"""

import asyncio
import html
import json
import shutil
import socket
import tempfile
from datetime import datetime
from pathlib import Path

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    LinkPreviewOptions,
    Message,
    Update,
)
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import video_note_music as music_tools
from config import env, env_int

# --- Настройки бота ---------------------------------------------------------

TOKEN = env("BOT_TOKEN")

# Твой user_id (узнать у @userinfobot) — задаётся в .env как OWNER_ID
OWNER_ID = env_int("OWNER_ID")

# --- Сеть -------------------------------------------------------------------

# Обычный SOCKS5/HTTP-прокси, если есть:
#   "socks5://127.0.0.1:1080"  или  "http://127.0.0.1:8080"
# Пусто — работаем через рабочий IP Telegram (API_IP).
PROXY_URL = env("PROXY_URL", "")

# api.telegram.org не отвечает, а этот адрес Bot API — отвечает.
API_IP = env("API_IP", "149.154.167.220")


def pin_telegram_ip(ip: str) -> None:
    """Направляет запросы к api.telegram.org на рабочий IP.

    SNI и сертификат остаются прежними (api.telegram.org), подменяется
    только адрес, куда идёт TCP-соединение.
    """
    original = socket.getaddrinfo

    def patched(host, port, *args, **kwargs):
        if host in ("api.telegram.org", b"api.telegram.org"):
            host = ip
        return original(host, port, *args, **kwargs)

    socket.getaddrinfo = patched


if not PROXY_URL:
    pin_telegram_ip(API_IP)

# --- Пути -------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent

# Папка с треками (mp3, m4a, aac, ogg, opus, wav, flac)
MUSIC_DIR = Path(env("MUSIC_DIR", "music"))
if not MUSIC_DIR.is_absolute():
    MUSIC_DIR = BASE_DIR / MUSIC_DIR

# Можно указать путь к ffmpeg целиком или к папке bin.
# Пусто — ищем в PATH, а потом в стандартной папке winget.
FFMPEG_PATH = env("FFMPEG_PATH", "")
FFPROBE_PATH = env("FFPROBE_PATH", "")

# Настройки: выбранный трек, громкость, что делать с оригиналом
SETTINGS_PATH = Path(env("SETTINGS_PATH", "settings.json"))
if not SETTINGS_PATH.is_absolute():
    SETTINGS_PATH = BASE_DIR / SETTINGS_PATH

# --- Настройки в JSON -------------------------------------------------------

DEFAULT_SETTINGS = {
    "enabled": True,          # накладывать музыку
    "volume": 0.18,           # громкость музыки относительно голоса
    "keep_voice": True,       # оставлять голос из кружочка
    "delete_original": True,  # удалять исходный кружочек после переотправки
    "track": "random",        # имя файла из music/ или "random"
    "sent_circles": [],       # [[chat_id, message_id], ...] чтобы не ловить эхо
}

settings: dict = dict(DEFAULT_SETTINGS)

MAX_SENT_CIRCLES = 200
VOLUME_PRESETS = (0.10, 0.18, 0.35)


def load_settings() -> None:
    global settings
    settings = dict(DEFAULT_SETTINGS)
    if not SETTINGS_PATH.exists():
        return
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"[!] Не прочитал {SETTINGS_PATH.name}: {exc}; "
            "беру значения по умолчанию."
        )
        return
    if isinstance(data, dict):
        settings.update(
            {key: value for key, value in data.items() if key in settings}
        )


def save_settings() -> None:
    tmp = SETTINGS_PATH.with_name(SETTINGS_PATH.name + ".tmp")
    try:
        tmp.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(SETTINGS_PATH)
    except OSError as exc:
        print(f"[!] Не удалось сохранить настройки: {exc}")


def set_setting(key: str, value) -> None:
    settings[key] = value
    save_settings()


# --- Музыка -----------------------------------------------------------------

def circle_music_enabled() -> bool:
    return bool(settings.get("enabled", True))


def circle_music_volume() -> float:
    try:
        value = float(settings.get("volume", music_tools.DEFAULT_VOLUME))
    except (TypeError, ValueError):
        return music_tools.DEFAULT_VOLUME
    return min(max(value, 0.0), 1.0)


def circle_music_keep_voice() -> bool:
    return bool(settings.get("keep_voice", True))


def circle_music_delete_original() -> bool:
    return bool(settings.get("delete_original", True))


def circle_music_track() -> str:
    return str(settings.get("track") or "random")


def music_tracks() -> list[Path]:
    return music_tools.list_tracks(MUSIC_DIR)


def remember_sent_circle(chat_id: int, message_id: int) -> None:
    """Запоминает свой кружочек, чтобы не обработать его эхо по второму разу."""
    items = settings.get("sent_circles")
    if not isinstance(items, list):
        items = []
    items.append([chat_id, message_id])
    settings["sent_circles"] = items[-MAX_SENT_CIRCLES:]
    save_settings()


def is_sent_circle(chat_id: int, message_id: int) -> bool:
    items = settings.get("sent_circles")
    if not isinstance(items, list):
        return False
    return [chat_id, message_id] in items


# --- Обработка кружочка -----------------------------------------------------

class CircleMusicError(RuntimeError):
    """Кружочек не получилось обработать — причину показываем владельцу."""


async def delete_circle_source(
    context: ContextTypes.DEFAULT_TYPE,
    msg: Message,
    chat_id: int,
    connection_id: str | None,
) -> None:
    """Убирает исходный кружочек: в чате должна остаться версия с музыкой."""
    try:
        if chat_id == OWNER_ID:
            await context.bot.delete_message(
                chat_id=OWNER_ID, message_id=msg.message_id
            )
        elif connection_id:
            await context.bot.delete_business_messages(
                business_connection_id=connection_id,
                message_ids=[msg.message_id],
            )
        else:
            return
    except Exception as exc:
        # Не критично: просто останутся две версии кружочка. Чаще всего
        # причина — у бота нет права «Удалять все сообщения» в Business.
        print(f"[!] Не удалось удалить исходный кружочек: {exc}")
        if chat_id != OWNER_ID:
            await warn_owner(
                context,
                "🎵 Не удалось удалить исходный кружочек: нужно право "
                "«Удалять все сообщения» у Business-бота. В чате остались "
                "обе версии.",
                key="circle_delete",
                cooldown=3600,
            )


async def circle_with_music(
    context: ContextTypes.DEFAULT_TYPE,
    msg: Message,
    chat_id: int,
    connection_id: str | None = None,
    *,
    delete_original: bool = False,
) -> Message:
    """Скачивает кружочек, добавляет музыку и отправляет новую версию.

    Бросает CircleMusicError, если обработать нельзя (нет ffmpeg или
    треков, файл больше лимита Bot API и т. п.). Удаление оригинала —
    не критичная операция: если Telegram не разрешит, останутся обе версии.
    """
    note = msg.video_note
    if note is None:
        raise CircleMusicError("это не видеосообщение")
    if not circle_music_enabled():
        raise CircleMusicError("наложение музыки выключено — включите в /music")

    tracks = music_tracks()
    if not tracks:
        raise CircleMusicError(
            f"в папке «{MUSIC_DIR}» нет музыки (mp3, m4a, ogg, wav, flac)"
        )
    if note.file_size and note.file_size > music_tools.MAX_VIDEO_NOTE_BYTES:
        raise CircleMusicError(
            "кружочек больше 20 МБ — Bot API такие файлы не отдаёт"
        )

    track = music_tools.pick_track(tracks, circle_music_track())
    if track is None:
        raise CircleMusicError("не удалось выбрать трек")

    workdir = Path(tempfile.mkdtemp(prefix="circle_music_"))
    try:
        source = workdir / "source.mp4"
        target = workdir / "result.mp4"

        try:
            telegram_file = await context.bot.get_file(note.file_id)
            await telegram_file.download_to_drive(custom_path=str(source))
        except Exception as exc:
            raise CircleMusicError(f"не удалось скачать кружочек: {exc}") from exc

        try:
            result = await asyncio.to_thread(
                music_tools.mix_video,
                source,
                target,
                track,
                volume=circle_music_volume(),
                keep_voice=circle_music_keep_voice(),
                ffmpeg_path=FFMPEG_PATH or None,
                ffprobe_path=FFPROBE_PATH or None,
            )
        except music_tools.FFmpegMissing as exc:
            raise CircleMusicError(str(exc)) from exc
        except music_tools.FFmpegError as exc:
            raise CircleMusicError(f"ffmpeg: {exc}") from exc

        kwargs: dict = {
            "duration": max(1, round(result.duration)),
            "length": note.length or 384,
        }
        if connection_id:
            kwargs["business_connection_id"] = connection_id

        try:
            with target.open("rb") as handle:
                sent = await context.bot.send_video_note(
                    chat_id=chat_id,
                    video_note=InputFile(handle, filename="circle.mp4"),
                    **kwargs,
                )
        except Exception as exc:
            raise CircleMusicError(f"Telegram не принял кружочек: {exc}") from exc
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    remember_sent_circle(chat_id, sent.message_id)

    if delete_original:
        await delete_circle_source(context, msg, chat_id, connection_id)

    return sent


async def warn_owner(
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    key: str | None = None,
    cooldown: int = 120,
) -> None:
    """Предупреждение владельцу, но не чаще раза в пару минут."""
    stamps = context.bot_data.setdefault("warn_at", {})
    now = datetime.now().timestamp()
    stamp_key = key or text
    if now - stamps.get(stamp_key, 0.0) < cooldown:
        return
    stamps[stamp_key] = now
    try:
        await context.bot.send_message(OWNER_ID, text, disable_notification=True)
    except Exception:
        pass


async def auto_circle_music(
    context: ContextTypes.DEFAULT_TYPE, msg: Message
) -> None:
    """Кружочек, отправленный владельцем из телефона в Business-чате."""
    if not circle_music_enabled():
        return

    if not music_tracks():
        await warn_owner(
            context,
            f"🎵 В папке «{MUSIC_DIR}» нет треков — кружочек ушёл без музыки.\n"
            "Положите mp3/m4a/ogg/wav/flac и откройте /music.",
            key="no_tracks",
            cooldown=600,
        )
        return

    try:
        await circle_with_music(
            context,
            msg,
            msg.chat_id,
            msg.business_connection_id,
            delete_original=circle_music_delete_original(),
        )
    except CircleMusicError as exc:
        await warn_owner(context, f"🎵 Кружочек ушёл без музыки: {exc}.")
        return
    except Exception as exc:
        await warn_owner(context, f"🎵 Не удалось обработать кружочек: {exc}")
        return

    print(f"[circle] {msg.chat_id}: музыка добавлена")


# --- Экран настроек ---------------------------------------------------------

def clip(text: str, limit: int) -> str:
    value = (text or "").strip()
    return value if len(value) <= limit else value[:limit] + "…"


def music_view() -> tuple[str, InlineKeyboardMarkup]:
    """Экран настройки музыки."""
    tracks = music_tracks()
    enabled = circle_music_enabled()
    current = circle_music_track()
    keep = circle_music_keep_voice()
    delete = circle_music_delete_original()
    volume = circle_music_volume()

    if current == "random":
        current_name = "🎲 случайный"
    elif current not in {track.name for track in tracks}:
        current_name = f"🎲 случайный (файл «{current}» не найден)"
    else:
        current_name = current

    lines = [
        "🎵 <b>Музыка для кружочков</b>",
        "",
        f"Статус: <b>{'вкл' if enabled else 'выкл'}</b>",
        f"Папка: <code>{html.escape(str(MUSIC_DIR))}</code>",
        f"Треков: {len(tracks)}",
        f"Трек: <b>{current_name}</b>",
        f"Громкость музыки: {volume:.0%}",
        f"Голос в кружке: {'сохраняется' if keep else 'заменяется музыкой'}",
        f"Оригинал: {'удаляется' if delete else 'остаётся в чате'}",
    ]
    if not tracks:
        lines += [
            "",
            "Положите mp3/m4a/ogg/wav/flac в папку <code>music</code> "
            "рядом с <code>main.py</code> и нажмите «🔄 Обновить».",
        ]

    buttons = [
        [
            InlineKeyboardButton(
                f"🎵 Наложение: {'вкл' if enabled else 'выкл'}",
                callback_data="music:enable",
            )
        ],
        [
            InlineKeyboardButton(
                f"{'✅' if current == 'random' else '🎲'} Случайный трек",
                callback_data="music:random",
            )
        ],
    ]

    for index, track in enumerate(tracks[:10]):
        mark = "✅" if track.name == current else "🎵"
        buttons.append(
            [
                InlineKeyboardButton(
                    f"{mark} {clip(track.stem, 28)}",
                    callback_data=f"music:track:{index}",
                )
            ]
        )
    if len(tracks) > 10:
        lines.append(f"В списке первые 10 треков из {len(tracks)}.")

    buttons.append(
        [
            InlineKeyboardButton(
                f"{'✅ ' if abs(volume - preset) < 0.005 else ''}{preset:.0%}",
                callback_data=f"music:volume:{preset:.2f}",
            )
            for preset in VOLUME_PRESETS
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(
                f"🎙 Голос: {'вкл' if keep else 'выкл'}",
                callback_data="music:voice",
            ),
            InlineKeyboardButton(
                f"🗑 Оригинал: {'удалять' if delete else 'оставлять'}",
                callback_data="music:delete",
            ),
        ]
    )
    buttons.append(
        [InlineKeyboardButton("🔄 Обновить", callback_data="music:refresh")]
    )
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def help_text() -> str:
    tracks = music_tracks()
    return (
        "🎵 <b>Музыка для кружочков</b>\n\n"
        "Отправьте кружочек в личный чат (Business) — вернётся версия "
        "с музыкой, оригинал удалится.\n\n"
        "Проверка: пришлите кружочек мне — верну его же с музыкой.\n\n"
        f"Треков в папке <code>{html.escape(str(MUSIC_DIR))}</code>: {len(tracks)}.\n"
        "Настройки: /music"
    )


# --- Обработчики ------------------------------------------------------------

async def on_business_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Сообщение в личном чате бизнес-аккаунта."""
    msg = update.business_message
    if msg is None or msg.chat.type != ChatType.PRIVATE:
        return

    sender = msg.from_user
    if sender is None or sender.is_bot or sender.id != OWNER_ID:
        return

    # Эхо собственного кружочка с музыкой: второй раз накладывать нельзя,
    # иначе бот будет переотправлять его бесконечно.
    if is_sent_circle(msg.chat_id, msg.message_id):
        return

    if msg.video_note:
        context.application.create_task(auto_circle_music(context, msg))


async def send_circle_to_self(
    context: ContextTypes.DEFAULT_TYPE, msg: Message
) -> None:
    """Кружочек боту — вернуть его же с музыкой (проверка настроек)."""
    if not music_tracks():
        await msg.reply_text(
            f"🎵 В папке «{MUSIC_DIR}» нет треков.\n"
            "Положите mp3/m4a/ogg/wav/flac и пришлите кружочек ещё раз."
        )
        return

    status = await msg.reply_text("🎵 Накладываю музыку…")
    try:
        await circle_with_music(context, msg, OWNER_ID)
    except CircleMusicError as exc:
        await status.edit_text(f"⚠️ Не получилось: {exc}")
        return
    except Exception as exc:
        await status.edit_text(f"⚠️ Ошибка: {exc}")
        return

    await status.edit_text(
        "🎵 Готово — кружочек с музыкой ниже.\n"
        "Перешлите его в нужный чат. Настройки: /music"
    )


async def on_owner_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Владелец пишет боту."""
    msg = update.effective_message
    if msg is None:
        return
    if msg.text and msg.text.startswith("/"):
        return
    if msg.video_note:
        await send_circle_to_self(context, msg)
        return
    await msg.reply_text(
        "Пришлите кружочек 🎥 — верну его с музыкой.\n"
        "Настройки: /music"
    )


async def on_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query is None or query.from_user is None:
        return
    if query.from_user.id != OWNER_ID:
        await query.answer("Недоступно")
        return

    action, _, arg = (query.data or "").partition(":")
    if action != "music":
        await query.answer()
        return

    if arg == "random":
        set_setting("track", "random")
    elif arg == "enable":
        set_setting("enabled", not circle_music_enabled())
    elif arg == "voice":
        set_setting("keep_voice", not circle_music_keep_voice())
    elif arg == "delete":
        set_setting("delete_original", not circle_music_delete_original())
    elif arg.startswith("volume:"):
        try:
            value = float(arg.partition(":")[2])
        except ValueError:
            value = music_tools.DEFAULT_VOLUME
        set_setting("volume", round(min(max(value, 0.0), 1.0), 2))
    elif arg.startswith("track:"):
        try:
            index = int(arg.partition(":")[2])
        except ValueError:
            index = -1
        tracks = music_tracks()
        if 0 <= index < len(tracks):
            set_setting("track", tracks[index].name)

    await query.answer()
    text, markup = music_view()
    try:
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except Exception as exc:
        if "not modified" not in str(exc).lower():
            print(f"[!] Не удалось обновить экран: {exc}")


async def cmd_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await update.effective_message.reply_text(
        help_text(),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🎵 Настроить музыку", callback_data="music:refresh"
                    )
                ]
            ]
        ),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def cmd_music(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    text, markup = music_view()
    await update.effective_message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=markup,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Чтобы ошибки не сыпали простынёй трейсбеков в консоль."""
    error = context.error
    if error is None:
        return
    if "Conflict" in str(error):
        print("[!] Уже запущен другой экземпляр бота — останови его и перезапусти.")
        return
    print(f"[!] Ошибка: {type(error).__name__}: {error}")


async def post_init(application) -> None:
    """Показывает при запуске, всё ли готово к работе."""
    try:
        ffmpeg = music_tools.find_tool("ffmpeg", FFMPEG_PATH or None)
        print(f"[i] ffmpeg: {ffmpeg}")
    except music_tools.FFmpegMissing as exc:
        print(f"[!] {exc}")

    tracks = music_tracks()
    print(f"[i] треков в {MUSIC_DIR}: {len(tracks)}")
    if not tracks:
        print("[!] Положите mp3/m4a/ogg/wav/flac в папку music/.")


# --- Запуск ----------------------------------------------------------------

def main() -> None:
    load_settings()

    builder = ApplicationBuilder().token(TOKEN)
    builder = builder.post_init(post_init)
    if PROXY_URL:
        builder = builder.proxy(PROXY_URL).get_updates_proxy(PROXY_URL)
        print(f"Подключение через прокси: {PROXY_URL}")
    else:
        print(f"Подключение через рабочий IP Telegram: {API_IP}")

    app = builder.build()

    app.add_handler(
        CommandHandler(
            ["start", "help"], cmd_start, filters=filters.Chat(OWNER_ID)
        )
    )
    app.add_handler(
        CommandHandler("music", cmd_music, filters=filters.Chat(OWNER_ID))
    )
    app.add_handler(
        MessageHandler(
            filters.UpdateType.BUSINESS_MESSAGE, on_business_message
        )
    )
    app.add_handler(
        MessageHandler(
            filters.Chat(OWNER_ID)
            & ~filters.UpdateType.BUSINESS_MESSAGES
            & ~filters.UpdateType.EDITED,
            on_owner_message,
        )
    )
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_error_handler(on_error)

    print("Бот с музыкой для кружочков запущен. Ctrl+C — остановить.")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
