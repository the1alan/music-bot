# bot.py
import asyncio
import os
import re
import traceback
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
)
from aiogram.filters import CommandStart
from aiohttp import web
from dotenv import load_dotenv

import yt_dlp

# Загрузка .env файла, если есть
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN не задан в переменных окружения или .env файле")

bot = Bot(token=TOKEN)
dp = Dispatcher()

# Хранилище результатов поиска (в продакшене лучше FSM, но для простоты ок)
search_results = {}
current_page = {}

# Путь для временных файлов
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DOWNLOAD_DIR = DATA_DIR / "downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Используем системный ffmpeg (должен быть доступен в PATH)
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg")  # или None, чтобы yt-dlp сам искал

SEARCH_OPTS = {
    "quiet": True,
    "extract_flat": True,
    "noplaylist": True,
}

DOWNLOAD_OPTS = {
    "format": "bestaudio/best",
    "outtmpl": str(DOWNLOAD_DIR / "%(id)s.%(ext)s"),
    "quiet": False,
    "noplaylist": True,
    "ffmpeg_location": FFMPEG_PATH,
    "postprocessors": [
        {
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }
    ],
}


def is_url(text: str) -> bool:
    """Грубая проверка, является ли текст URL."""
    url_pattern = re.compile(
        r"https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+[/\w\.-]*(\?[\w=&%.-]*)?"
    )
    return bool(url_pattern.match(text.strip()))


async def search_all(query: str, limit: int = 10) -> list[dict]:
    """Ищет треки на YouTube и SoundCloud через yt-dlp."""
    def _search():
        results = []
        with yt_dlp.YoutubeDL(SEARCH_OPTS) as ydl:
            # YouTube
            try:
                yt_info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
                for entry in yt_info.get("entries", []) or []:
                    if entry and entry.get("id") and entry.get("title"):
                        results.append({
                            "title": f"[YT] {entry['title']}",
                            "url": f"https://www.youtube.com/watch?v={entry['id']}",
                        })
            except Exception as e:
                print("YOUTUBE SEARCH ERROR:", repr(e))
                traceback.print_exc()

            # SoundCloud
            try:
                sc_info = ydl.extract_info(f"scsearch{limit}:{query}", download=False)
                for entry in sc_info.get("entries", []) or []:
                    if entry and entry.get("url") and entry.get("title"):
                        results.append({
                            "title": f"[SC] {entry['title']}",
                            "url": entry["url"],
                        })
            except Exception as e:
                print("SOUNDCLOUD SEARCH ERROR:", repr(e))
                traceback.print_exc()

        # Убираем дубликаты по URL
        seen = set()
        unique = []
        for r in results:
            if r["url"] not in seen:
                seen.add(r["url"])
                unique.append(r)
        return unique

    return await asyncio.to_thread(_search)


async def download_audio(url: str) -> tuple[str | None, str | None]:
    """Скачивает аудио по ссылке, возвращает путь к mp3 и название."""
    def _download():
        try:
            with yt_dlp.YoutubeDL(DOWNLOAD_OPTS) as ydl:
                info = ydl.extract_info(url, download=True)
            if not info:
                return None, None
            video_id = info.get("id")
            title = info.get("title", "Трек")
            if not video_id:
                return None, title

            # Ищем получившийся mp3-файл
            mp3_path = DOWNLOAD_DIR / f"{video_id}.mp3"
            if mp3_path.exists():
                return str(mp3_path), title
            # Иногда yt-dlp называет файл иначе, ищем любой mp3 начинающийся с id
            for f in DOWNLOAD_DIR.glob(f"{video_id}*"):
                if f.suffix == ".mp3":
                    return str(f), title
            return None, title
        except Exception as e:
            print("DOWNLOAD ERROR:", repr(e))
            traceback.print_exc()
            return None, None

    return await asyncio.to_thread(_download)


def build_page_keyboard(tracks: list, page: int = 0, per_page: int = 10) -> InlineKeyboardMarkup:
    """Клавиатура с треками и пагинацией."""
    total_pages = max(1, -(-len(tracks) // per_page))
    start = page * per_page
    end = start + per_page
    page_tracks = tracks[start:end]

    buttons = []
    for i, track in enumerate(page_tracks, start=start):
        buttons.append([
            InlineKeyboardButton(
                text=f"{i + 1}. {track['title'][:50]}",
                callback_data=f"dl_{i}",
            )
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"page_{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"page_{page + 1}"))
    if nav:
        buttons.append(nav)

    return InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "🎵 Привет. Я нахожу и скачиваю музыку.\n\n"
        "▫️ Отправь ссылку — сразу получишь MP3.\n"
        "▫️ Напиши название или исполнителя — покажу список треков с YouTube и SoundCloud.\n\n"
        "Листай результаты стрелками ⬅️➡️ и выбирай."
    )


@dp.message(F.text)
async def handle_message(message: Message):
    text = message.text.strip()
    if not text:
        return await message.answer("Введи название трека или ссылку.")

    if is_url(text):
        state = await message.answer("⏳ Скачиваю по ссылке...")
        path, title = await download_audio(text)
        if not path:
            return await state.edit_text("❌ Не удалось скачать. Проверь логи сервера.")
        return await _send_and_clean(message.chat.id, path, state, title)

    await message.answer(f"🔍 Ищу везде: {text}...")
    tracks = await search_all(text, limit=10)
    if not tracks:
        return await message.answer("😔 Ничего не найдено.")

    search_results[message.chat.id] = tracks
    current_page[message.chat.id] = 0
    await message.answer(
        "🎶 Результаты поиска:",
        reply_markup=build_page_keyboard(tracks, 0),
    )


@dp.callback_query()
async def handle_callback(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    data = callback.data

    if data.startswith("dl_"):
        tracks = search_results.get(chat_id)
        if not tracks:
            return await callback.answer("Результаты устарели. Повтори поиск.")
        try:
            idx = int(data[3:])
        except ValueError:
            return await callback.answer("Ошибка индекса.")
        if idx < 0 or idx >= len(tracks):
            return await callback.answer("Трек не найден.")

        url = tracks[idx]["url"]
        await callback.answer()
        state = await callback.message.answer("⏳ Скачиваю...")
        path, title = await download_audio(url)
        if not path:
            return await state.edit_text("❌ Не удалось скачать трек. Проверь логи сервера.")
        return await _send_and_clean(chat_id, path, state, title)

    elif data.startswith("page_"):
        try:
            page = int(data[5:])
        except ValueError:
            return await callback.answer("Неверная страница.")
        tracks = search_results.get(chat_id)
        if not tracks:
            return await callback.answer("Результаты устарели.")
        current_page[chat_id] = page
        await callback.message.edit_text(
            f"🎶 Результаты поиска (страница {page + 1}):",
            reply_markup=build_page_keyboard(tracks, page),
        )
        await callback.answer()


async def _send_and_clean(chat_id: int, file_path: str, status_msg: Message, title: str):
    """Отправляет аудио, удаляет временный файл и обновляет статус."""
    try:
        size = os.path.getsize(file_path)
    except OSError:
        return await status_msg.edit_text("❌ Файл недоступен.")

    if size > 45 * 1024 * 1024:  # 45 МБ
        await status_msg.edit_text("⚠️ Файл слишком большой (>45 МБ).")
        try:
            Path(file_path).unlink(missing_ok=True)
        except OSError:
            pass
        return

    try:
        await bot.send_audio(chat_id, FSInputFile(file_path), title=title)
        await status_msg.delete()
    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка отправки: {e}")
    finally:
        try:
            Path(file_path).unlink(missing_ok=True)
        except OSError:
            pass


# Простая проверка здоровья для хостинга
async def health(request):
    return web.Response(text="OK")


async def main():
    # Запускаем лёгкий HTTP-сервер для healthcheck
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Healthcheck на порту {port}")

    # Запускаем поллинг
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
