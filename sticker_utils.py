"""Конвертация фото/видео в стикеры Telegram."""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageFilter

logger = logging.getLogger(__name__)

# Telegram-стикер единственного типа: 512x512 пикселей.
STICKER_SIZE = 512
MAX_FILE_SIZE = 256 * 1024  # жёсткий лимит Bot API для файлов стикера
TARGET_FILE_SIZE = 200 * 1024  # оставляем запас, чтобы не упереться в лимит

# Для анимированных (WebM) стикеров
ANIM_DURATION_MAX = 3.0  # секунд
ANIM_WEBM_MAX = 300 * 1024  # Telegram требует не более 256 КБ у нас будут ~200
ANIM_FPS = 30
TARGET_WEBM_SIZE = 230 * 1024


@dataclass
class Converted:
    path: Path
    kind: str  # 'sticker' | 'video'
    mime_type: str
    emoji: str = "😀"


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _scale_cover(img: Image.Image, size: int = STICKER_SIZE) -> Image.Image:
    """Масштабирует изображение, заполняя 512x512 без искажений (cover + crop)."""
    img = img.convert("RGBA")
    w, h = img.size
    scale = size / min(w, h)
    img = img.resize((round(w * scale), round(h * scale)), Image.LANCZOS)
    nw, nh = img.size
    left = (nw - size) // 2
    top = (nh - size) // 2
    return img.crop((left, top, left + size, top + size))


def _add_soft_edge(img: Image.Image) -> Image.Image:
    """Добавляет мягкий скруглённый угол/рамку, чтобы стикер был аккуратным."""
    scale = STICKER_SIZE
    mask = Image.new("L", (scale, scale), 0)
    from PIL import ImageDraw
    draw = ImageDraw.Draw(mask)
    radius = int(scale * 0.06)
    draw.rounded_rectangle([0, 0, scale - 1, scale - 1], radius=radius, fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(2))
    img.putalpha(mask)
    return img


def _compress_png(img: Image.Image, target: Path, budget: int = TARGET_FILE_SIZE) -> Path:
    """Сохраняет в PNG, уменьшая палитру, пока не влезем в лимит."""
    quantized = img.quantize(colors=256, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.FLOYDSTEINBERG)
    quantized.save(target, "PNG", optimize=True)
    if target.stat().st_size <= budget:
        return target
    # если не влезло — пробуем меньше цветов
    for colors in (192, 128, 96, 64, 32):
        quantized = img.quantize(colors=colors, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.FLOYDSTEINBERG)
        quantized.save(target, "PNG", optimize=True)
        if target.stat().st_size <= budget:
            break
    return target


def photo_to_sticker(input_path: Path, output_path: Path) -> Path:
    """Фото (или любой картинки) → PNG-стикер 512x512."""
    with Image.open(input_path) as src:
        img = _scale_cover(src)
        img = _add_soft_edge(img)
        _compress_png(img, output_path)
    if output_path.stat().st_size > MAX_FILE_SIZE:
        raise RuntimeError("Стикер получился легче не должно было, но превышен лимит 256 КБ")
    return output_path


def _run_ffmpeg(args: list[str]) -> None:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=120)
    except FileNotFoundError as e:
        raise RuntimeError(
            "ffmpeg не найден. Установите его (winget install ffmpeg) и перезапустите бота."
        ) from e
    if proc.returncode != 0:
        logger.error("ffmpeg failed: %s", proc.stderr[-800:])
        raise RuntimeError(f"Ошибка конвертации видео: {proc.stderr[-300:]}")


def video_to_webm(input_path: Path, output_path: Path) -> Path:
    """Видео → анимированный WebM-стикер (VP9, без звука, ~512x512)."""
    probe = _ffprobe_duration(input_path)
    if probe and probe > ANIM_DURATION_MAX:
        # используем только первые 3 секунды (ссылку на полезную) — обрежем
        duration_arg = ["-t", str(ANIM_DURATION_MAX)]
        logger.info("Видео длиннее 3 сек, обрезаем до первых 3 сек")
    else:
        duration_arg = []

    args = [
        "ffmpeg", "-y",
        "-i", str(input_path),
        *duration_arg,
        "-vf", "fps=" + str(ANIM_FPS) + ",scale=512:512:force_original_aspect_ratio=increase:flags=lanczos,crop=512:512,setsar=1",
        "-an",
        "-c:v", "libvpx-vp9",
        "-r", str(ANIM_FPS),
        "-b:v", "600k",
        "-crf", "42",
        "-row-mt", "1",
        str(output_path),
    ]
    _run_ffmpeg(args)

    # Telegram требует ≤256 КБ для webm; если превышено, сжимаем битрейтом
    if output_path.stat().st_size > MAX_FILE_SIZE:
        logger.info("webm > 256КБ (%d), пережимаем ниже", output_path.stat().st_size)
        for quality in (38, 36, 34, 30, 26):
            _run_ffmpeg([
                "ffmpeg", "-y",
                "-i", str(input_path),
                *duration_arg,
                "-vf", "fps=" + str(ANIM_FPS) + ",scale=512:512:force_original_aspect_ratio=increase:flags=lanczos,crop=512:512,setsar=1",
                "-an",
                "-c:v", "libvpx-vp9",
                "-b:v", "300k",
                "-crf", str(quality),
                "-row-mt", "1",
                str(output_path),
            ])
            if output_path.stat().st_size <= MAX_FILE_SIZE:
                break
    if output_path.stat().st_size > MAX_FILE_SIZE:
        raise RuntimeError("Не удалось ужать видео-стикер до 256 КБ")
    return output_path


def _ffprobe_duration(path: Path) -> float | None:
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        return float(proc.stdout.strip())
    except (FileNotFoundError, ValueError):
        return None


def convert_media(input_path: Path, mime_type: str, work_dir: Path) -> Converted:
    """Определяет тип входного файла и возвращает готовый стикер."""
    work_dir = _ensure_dir(work_dir)
    if mime_type.startswith("image/"):
        out = photo_to_sticker(input_path, work_dir / "sticker.png")
        return Converted(path=out, kind="sticker", mime_type="image/png")

    if mime_type.startswith("video/"):
        out = video_to_webm(input_path, work_dir / "sticker.webm")
        return Converted(path=out, kind="video", mime_type="video/webm")

    raise RuntimeError(f"Неподдерживаемый тип медиа: {mime_type}")