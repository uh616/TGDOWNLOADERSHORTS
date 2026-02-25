import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import (
    Message,
    FSInputFile,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
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
    opts: dict = {
        "outtmpl": str(output_dir / "%(title).200s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        # Игнорируем глобальные конфиги yt-dlp (где может быть жёсткий format)
        "ignoreconfig": True,
    }

    # Прокси (опционально)
    proxy = os.getenv("YTDLP_PROXY")
    if proxy:
        # Поддержка HTTP/SOCKS5 прокси, например:
        # YTDLP_PROXY=http://user:pass@host:port
        # YTDLP_PROXY=socks5://user:pass@host:port
        opts["proxy"] = proxy

    return opts


def _download_video_sync(url: str, output_dir: Path) -> Path:
    # Чистое поведение yt-dlp без кук, как при ручном `yt-dlp --ignore-config URL`
    ydl_opts = build_yt_dlp_opts(output_dir)
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
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


def _has_video_stream_sync(path: Path) -> bool:
    """
    Returns True if ffprobe detects at least one video stream.
    If ffprobe isn't available or fails, assume it's a video file.
    """
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_type",
        "-of",
        "csv=p=0",
        str(path),
    ]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode("utf-8", "ignore").strip()
        return bool(out)
    except Exception:
        return True


def _convert_audio_to_mp3_sync(input_path: Path, output_path: Path) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-vn",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "192k",
        str(output_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _reencode_video_to_mp4_sync(input_path: Path, output_path: Path) -> None:
    """
    Перекодировать любое видео в mp4 (H.264 + AAC), чтобы Telegram сразу его понимал.
    Без изменения разрешения, только перекодирование контейнера/кодеков.
    """
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        str(output_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def prepare_media(path: Path) -> tuple[Path, str]:
    """
    Returns (final_path, kind) where kind is 'video' or 'audio'.
    - If file has no video stream -> convert to mp3 and return kind='audio'
    - Else -> keep as video and return kind='video'
    """
    loop = asyncio.get_running_loop()
    has_video = await loop.run_in_executor(None, _has_video_stream_sync, path)
    if has_video:
        # Если это видео, но не mp4 — перекодируем в mp4
        if path.suffix.lower() != ".mp4":
            mp4_path = path.with_suffix(".mp4")
            await loop.run_in_executor(None, _reencode_video_to_mp4_sync, path, mp4_path)
            return mp4_path, "video"
        return path, "video"

    mp3_path = path.with_suffix(".mp3")
    await loop.run_in_executor(None, _convert_audio_to_mp3_sync, path, mp3_path)
    return mp3_path, "audio"


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
        prepared_path, kind = await prepare_media(original_path)

        if kind == "audio":
            final_path = prepared_path
        else:
            final_path = await compress_if_needed(prepared_path)

        if final_path is None:
            await status.edit_text(
                "Не удалось подготовить видео: файл больше 50 МБ даже после сжатия."
            )
            return

        video_file = FSInputFile(path=str(final_path))
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📚 Помощь", callback_data="help")]
            ]
        )
        if kind == "audio":
            await status.edit_text("Отправляю аудио...")
            if final_path.stat().st_size > TELEGRAM_MAX_FILE_SIZE:
                await status.edit_text("Аудио получилось больше 50 МБ, не могу отправить.")
                return
            await message.answer_audio(
                audio=video_file,
                caption="Готово! 🎵 Аудио в mp3.\nНажми, чтобы скачать/сохранить.",
                reply_markup=keyboard,
            )
        else:
            await status.edit_text("Отправляю видео...")
            await message.answer_video(
                video=video_file,
                caption="Готово! 🎬 Видео.\nМожно смотреть прямо в Telegram или скачать.",
                supports_streaming=True,
                reply_markup=keyboard,
            )

        try:
            await status.delete()
        except Exception:
            pass

    except Exception as e:
        logger.exception("Error while processing video: %s", e)
        err_text = str(e)
        if "Sign in to confirm you’re not a bot" in err_text or "confirm you're not a bot" in err_text:
            await status.edit_text(
                "YouTube запросил подтверждение (капча/логин) для этого видео.\n"
                "На Render такое иногда блокируется по IP дата‑центра — попробуй другую ссылку или другой хостинг/прокси."
            )
        else:
            await status.edit_text("Произошла ошибка при скачивании или обработке видео.")
    finally:
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)


app = FastAPI()


@app.api_route("/health", methods=["GET", "HEAD"])
async def health() -> PlainTextResponse:
    return PlainTextResponse("OK")


@app.api_route("/", methods=["GET", "HEAD"])
async def root() -> PlainTextResponse:
    return PlainTextResponse("Bot is running")


async def _start_bot() -> None:
    # If webhook was ever set ранее, убираем его (для polling).
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


@app.on_event("startup")
async def on_startup() -> None:
    asyncio.create_task(_start_bot())


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=False)

