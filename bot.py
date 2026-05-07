# bot.py
import asyncio
import base64
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
from aiogram.filters import Command, CommandStart
from aiohttp import web
from dotenv import load_dotenv

import yt_dlp
import imageio_ffmpeg


load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN не задан в переменных окружения или .env файле")

bot = Bot(token=TOKEN)
dp = Dispatcher()

search_results = {}
current_page = {}

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
DOWNLOAD_DIR = DATA_DIR / "downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

FFMPEG_PATH = os.getenv("FFMPEG_PATH") or imageio_ffmpeg.get_ffmpeg_exe()
COOKIES_FILE = Path(os.getenv("YTDLP_COOKIES_FILE", str(DATA_DIR / "cookies.txt")))


def prepare_cookies_file() -> None:
    cookies_b64 = os.getenv("YTDLP_COOKIES_B64")

    if not cookies_b64:
        return

    if COOKIES_FILE.exists():
        return

    try:
        COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
        cookies_text = base64.b64decode(cookies_b64).decode("utf-8")
        COOKIES_FILE.write_text(cookies_text, encoding="utf-8")
        os.chmod(COOKIES_FILE, 0o600)
        print(f"Created yt-dlp cookies file from YTDLP_COOKIES_B64: {COOKIES_FILE}")
    except Exception as e:
        print("COOKIES SETUP ERROR:", repr(e))
        traceback.print_exc()


def cookies_status_text() -> str:
    prepare_cookies_file()
    cookies_b64 = os.getenv("YTDLP_COOKIES_B64")
    exists = COOKIES_FILE.exists()
    size = COOKIES_FILE.stat().st_size if exists else 0

    return (
        f"DATA_DIR: {DATA_DIR}\n"
        f"DOWNLOAD_DIR: {DOWNLOAD_DIR}\n"
        f"FFMPEG_PATH: {FFMPEG_PATH}\n"
        f"YTDLP_COOKIES_FILE: {COOKIES_FILE}\n"
        f"cookies.txt exists: {exists}\n"
        f"cookies.txt size: {size} bytes\n"
        f"YTDLP_COOKIES_B64 set: {bool(cookies_b64)}\n"
        f"YTDLP_COOKIES_B64 length: {len(cookies_b64) if cookies_b64 else 0}"
    )


def build_ytdlp_opts(base_opts: dict) -> dict:
    prepare_cookies_file()
    opts = dict(base_opts)

    if COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)
        print(f"Using yt-dlp cookies file: {COOKIES_FILE}")
    else:
        print(f"yt-dlp cookies file not found: {COOKIES_FILE}")

    return opts


SEARCH_OPTS_BASE = {
    "quiet": True,
    "extract_flat": True,
    "noplaylist": True,
}

DOWNLOAD_OPTS_BASE = {
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


def short_error_text(error: Exception) -> str:
    text = str(error).strip()
    text = re.sub(r"\s+", " ", text)

    if "Sign in to confirm" in text or "not a bot" in text:
        return (
            "YouTube требует cookies: Sign in to confirm you’re not a bot. "
            "Значит cookies не найдены, невалидные или YouTube их не принял. "
            "Проверь /debug: cookies.txt exists должен быть True, size больше 0, "
            "или YTDLP_COOKIES_B64 должен быть set=True."
        )

    if "ffmpeg" in text.lower():
        return f"Ошибка ffmpeg: {text[:700]}"

    if "Unsupported URL" in text:
        return f"Неподдерживаемая ссылка: {text[:700]}"

    return text[:900] if text else repr(error)


def is_url(text: str) -> bool:
    url_pattern = re.compile(
        r"https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+[/\w\.-]*(\?[\w=&%.-]*)?"
    )
    return bool(url_pattern.match(text.strip()))


async def search_all(query: str, limit: int = 10) -> list[dict]:
    def _search():
        results = []
        search_opts = build_ytdlp_opts(SEARCH_OPTS_BASE)

        with yt_dlp.YoutubeDL(search_opts) as ydl:
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

        seen = set()
        unique = []
        for r in results:
            if r["url"] not in seen:
                seen.add(r["url"])
                unique.append(r)
        return unique

    return await asyncio.to_thread(_search)


async def download_audio(url: str) -> tuple[str | None, str | None, str | None]:
    def _download():
        try:
            download_opts = build_ytdlp_opts(DOWNLOAD_OPTS_BASE)

            with yt_dlp.YoutubeDL(download_opts) as ydl:
                info = ydl.extract_info(url, download=True)

            if not info:
                return None, None, "yt-dlp не вернул информацию о треке."

            video_id = info.get("id")
            title = info.get("title", "Трек")
            if not video_id:
                return None, title, "yt-dlp не вернул id трека."

            mp3_path = DOWNLOAD_DIR / f"{video_id}.mp3"
            if mp3_path.exists():
                return str(mp3_path), title, None

            for f in DOWNLOAD_DIR.glob(f"{video_id}*"):
                if f.suffix == ".mp3":
                    return str(f), title, None

            return None, title, f"Файл не найден после скачивания. Папка: {DOWNLOAD_DIR}"
        except Exception as e:
            print("DOWNLOAD ERROR:", repr(e))
            traceback.print_exc()
            return None, None, short_error_text(e)

    return await asyncio.to_thread(_download)


def build_page_keyboard(tracks: list, page: int = 0, per_page: int = 10) -> InlineKeyboardMarkup:
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


@dp.message(Command("debug"))
async def debug(message: Message):
    await message.answer(f"```\n{cookies_status_text()}\n```", parse_mode="Markdown")


@dp.message(F.text)
async def handle_message(message: Message):
    text = message.text.strip()
    if not text:
        return await message.answer("Введи название трека или ссылку.")

    if is_url(text):
        state = await message.answer("⏳ Скачиваю по ссылке...")
        path, title, error = await download_audio(text)
        if not path:
            return await state.edit_text(f"❌ Не удалось скачать.\n\nПричина: {error}")
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
        path, title, error = await download_audio(url)
        if not path:
            return await state.edit_text(f"❌ Не удалось скачать трек.\n\nПричина: {error}")
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
    try:
        size = os.path.getsize(file_path)
    except OSError:
        return await status_msg.edit_text("❌ Файл недоступен.")

    if size > 45 * 1024 * 1024:
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


async def health(request):
    return web.Response(text="OK")


async def main():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Healthcheck на порту {port}")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
