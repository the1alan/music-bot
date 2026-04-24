import asyncio
import os
import re
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.filters import CommandStart
from aiohttp import web
import yt_dlp

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN не задан в переменных окружения")

bot = Bot(token=TOKEN)
dp = Dispatcher()

search_results = {}
current_page = {}

SEARCH_OPTS = {
    'quiet': True,
    'extract_flat': True,
}

DOWNLOAD_OPTS = {
    'format': 'bestaudio/best',
    'outtmpl': 'downloads/%(id)s.%(ext)s',
    'quiet': True,
    'ignoreerrors': True,
}


def is_url(text: str) -> bool:
    url_pattern = re.compile(r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+[/\w\.-]*(\?[\w=&%]*)?')
    return bool(url_pattern.match(text.strip()))


async def search_all(query: str, limit: int = 10) -> list[dict]:
    def _search():
        results = []
        with yt_dlp.YoutubeDL(SEARCH_OPTS) as ydl:
            try:
                yt_info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
                for entry in yt_info.get('entries', []) or []:
                    if entry:
                        results.append({
                            'title': f"[YT] {entry['title']}",
                            'url': f"https://www.youtube.com/watch?v={entry['id']}",
                        })
            except Exception:
                pass
            try:
                sc_info = ydl.extract_info(f"scsearch{limit}:{query}", download=False)
                for entry in sc_info.get('entries', []) or []:
                    if entry:
                        results.append({
                            'title': f"[SC] {entry['title']}",
                            'url': entry['url'],
                        })
            except Exception:
                pass
        seen = set()
        unique = []
        for r in results:
            if r['url'] not in seen:
                seen.add(r['url'])
                unique.append(r)
        return unique
    return await asyncio.to_thread(_search)


async def download_audio(url: str):
    def _download():
        os.makedirs("downloads", exist_ok=True)
        with yt_dlp.YoutubeDL(DOWNLOAD_OPTS) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                return None, None
        video_id = info.get('id')
        title = info.get('title', 'Трек')
        if not video_id:
            return None, title
        for f in Path("downloads").glob(f"{video_id}.*"):
            return str(f), title
        return None, title
    return await asyncio.to_thread(_download)


def build_page_keyboard(tracks, page=0, per_page=10):
    total_pages = max(1, -(-len(tracks) // per_page))
    start = page * per_page
    end = start + per_page
    page_tracks = tracks[start:end]
    buttons = []
    for i, t in enumerate(page_tracks, start=start):
        buttons.append([InlineKeyboardButton(text=f"{i+1}. {t['title'][:50]}", callback_data=f"dl_{i}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"page_{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"page_{page+1}"))
    if nav:
        buttons.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "🎵 Привет. Я нахожу и скачиваю музыку.\n\n"
        "▫️ Отправь ссылку — сразу получишь MP3.\n"
        "▫️ Напиши название или исполнителя — покажу список треков со всех площадок.\n\n"
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
            return await state.edit_text("❌ Не удалось скачать.")
        return await _send_and_clean(message.chat.id, path, state, title)
    await message.answer(f"🔍 Ищу везде: {text}...")
    tracks = await search_all(text, limit=10)
    if not tracks:
        return await message.answer("😔 Ничего не найдено.")
    search_results[message.chat.id] = tracks
    current_page[message.chat.id] = 0
    await message.answer("🎶 Результаты поиска:", reply_markup=build_page_keyboard(tracks, 0))


@dp.callback_query()
async def handle_callback(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    data = callback.data
    if data.startswith("dl_"):
        tracks = search_results.get(chat_id)
        if not tracks:
            return await callback.answer("Результаты устарели.")
        try:
            idx = int(data[3:])
        except ValueError:
            return await callback.answer("Ошибка.")
        if idx < 0 or idx >= len(tracks):
            return await callback.answer("Трек не найден.")
        url = tracks[idx]['url']
        await callback.answer()
        state = await callback.message.answer(f"⏳ Скачиваю...")
        path, title = await download_audio(url)
        if not path:
            return await state.edit_text("❌ Не удалось скачать трек.")
        return await _send_and_clean(chat_id, path, state, title)
    if data.startswith("page_"):
        try:
            page = int(data[5:])
        except ValueError:
            return await callback.answer("Неверная страница.")
        tracks = search_results.get(chat_id)
        if not tracks:
            return await callback.answer("Результаты устарели.")
        current_page[chat_id] = page
        await callback.message.edit_text(
            f"🎶 Результаты поиска (страница {page+1}):",
            reply_markup=build_page_keyboard(tracks, page),
        )
        await callback.answer()


async def _send_and_clean(chat_id, path, status, title):
    try:
        size = os.path.getsize(path)
    except OSError:
        return await status.edit_text("❌ Файл недоступен.")
    if size > 45 * 1024 * 1024:
        await status.edit_text("⚠️ Файл слишком большой (>45 МБ).")
        os.remove(path)
        return
    try:
        await bot.send_audio(chat_id, FSInputFile(path), title=title)
    except Exception as e:
        await status.edit_text(f"❌ Ошибка отправки: {e}")
    else:
        await status.delete()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# Заглушка для Render
async def health(request):
    return web.Response(text="OK")


async def main():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 10000)
    await site.start()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())