import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, FSInputFile, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart
from yt_dlp import YoutubeDL
import subprocess


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)


BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Environment variable BOT_TOKEN is not set")


TELEGRAM_MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB


bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
router = Router()
dp.include_router(router)


def build_yt_dlp_opts(output_dir: Path) -> dict:
    return {
        "outtmpl": str(output_dir / "%(title).200s.%(ext)s"),
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }


def _download_video_sync(url: str, output_dir: Path) -> Path:
    ydl_opts = build_yt_dlp_opts(output_dir)
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        # get final file path
        if "requested_downloads" in info and info["requested_downloads"]:
            filepath = info["requested_downloads"][0]["filepath"]
        else:
            filepath = ydl.prepare_filename(info)
    return Path(filepath)


async def download_video(url: str, output_dir: Path) -> Path:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _download_video_sync, url, output_dir)


def _compress_video_sync(input_path: Path, output_path: Path) -> None:
    # Simple re-encode to H.264/AAC with limited resolution to reduce size
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-vf",
        "scale='min(1280,iw)':-2",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "28",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        str(output_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def compress_if_needed(path: Path) -> Optional[Path]:
    size = path.stat().st_size
    if size <= TELEGRAM_MAX_FILE_SIZE:
        return path

    logger.info("Video size %s bytes > 50MB, trying to compress", size)
    compressed_path = path.with_name(path.stem + "_compressed.mp4")

    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, _compress_video_sync, path, compressed_path)
    except Exception as e:
        logger.exception("Compression failed: %s", e)
        return None

    if compressed_path.exists() and compressed_path.stat().st_size <= TELEGRAM_MAX_FILE_SIZE:
        return compressed_path

    logger.info(
        "Compressed video is still too large: %s bytes",
        compressed_path.stat().st_size if compressed_path.exists() else "missing",
    )
    return None


def is_url(text: str) -> bool:
    return text.startswith("http://") or text.startswith("https://")


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📚 Помощь", callback_data="help")],
        ]
    )
    text = (
        "👋 <b>Привет!</b>\n\n"
        "Я бот для скачивания видео с YouTube, TikTok, VK, OK и других сервисов.\n"
        "Просто отправь мне <b>ссылку на видео</b>, а я скачаю и пришлю файл 📥\n\n"
        "Максимальный размер отправляемого файла: <b>50 МБ</b>.\n"
    )
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data == "help")
async def on_help(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "📘 <b>Как пользоваться ботом</b>\n\n"
            "1. Скопируй ссылку на видео (YouTube, TikTok, VK, OK и др.).\n"
            "2. Отправь эту ссылку боту.\n"
            "3. Дождись, пока я скачаю и подготовлю файл.\n"
            "4. Полученное видео придёт как файл-документ, который можно сохранить на телефон."
        )


@router.message(F.text)
async def handle_video_message(message: Message) -> None:
    text = (message.text or "").strip()
    if not is_url(text):
        return

    status = await message.answer("Скачиваю...")
    tmp_dir: Optional[str] = None
    original_path: Optional[Path] = None
    final_path: Optional[Path] = None

    try:
        tmp_dir = tempfile.mkdtemp(prefix="video_dl_")
        output_dir = Path(tmp_dir)

        original_path = await download_video(text, output_dir)
        final_path = await compress_if_needed(original_path)

        if final_path is None:
            await status.edit_text(
                "Не удалось подготовить видео: файл больше 50 МБ даже после сжатия."
            )
            return

        await status.edit_text("Отправляю файл...")

        video_file = FSInputFile(path=str(final_path))
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📚 Помощь", callback_data="help")]
            ]
        )
        await message.answer_document(
            document=video_file,
            caption="Готово! 🎬 Ваше видеофайл.\nНажми на него, чтобы скачать или сохранить.",
            reply_markup=keyboard,
        )

        try:
            await status.delete()
        except Exception:
            pass

    except Exception as e:
        logger.exception("Error while processing video: %s", e)
        await status.edit_text("Произошла ошибка при скачивании или обработке видео.")
    finally:
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)


app = FastAPI()


@app.get("/health")
async def health() -> str:
    return "OK"


@app.get("/")
async def root() -> str:
    return "Bot is running"


async def _start_bot() -> None:
    await dp.start_polling(bot)


@app.on_event("startup")
async def on_startup() -> None:
    asyncio.create_task(_start_bot())


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=False)

