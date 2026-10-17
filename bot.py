"""Telegram-бот: создание стикеров из фото/видео и собственных стикер-паков."""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from io import BytesIO
from pathlib import Path

try:  # .env поддержка (python-dotenv)
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    FSInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    ErrorEvent,
    InputSticker,
)
from aiogram.enums import StickerFormat, StickerType

from sticker_utils import convert_media, ANIM_DURATION_MAX

# Временное хранилище последнего стикера пользователя, чтобы добавить его в пак.
# В проде замените на Redis/БД. Ключ — user_id.
last_sticker: dict[int, Path] = {}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID = os.environ.get("ADMIN_ID", "")  # числовой id, опционально

WORK_DIR = Path(__file__).resolve().parent
DOWNLOADS = WORK_DIR / "downloads"
DOWNLOADS.mkdir(exist_ok=True)

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

# Username бота — заполняется в main() через get_me(). В aiogram у Bot нет .username.
BOT_USERNAME: str = ""
BOT_ID: int = 0


class CreatePackStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_title = State()


def _pack_name(user_id: int) -> str:
    """Имя будущего пака. Окончание _by_&lt;username> обязательно для Telegram."""
    return f"stickers_{user_id}_by_{BOT_USERNAME}"


# ----------------------------------------------------------------------------
# Глобальный перехват ошибок: бот никогда не должен молчать
# ----------------------------------------------------------------------------
@dp.errors()
async def global_error_handler(event: ErrorEvent):
    logger.exception("Глобальная ошибка при обработке апдейта", exc_info=event.exception)
    update = event.update
    if update.message:
        try:
            await update.message.answer("⚠️ Произошла ошибка. Попробуйте позже или напишите /start.")
        except Exception:
            pass


# ----------------------------------------------------------------------------
# Команды
# ----------------------------------------------------------------------------
@dp.message(CommandStart())
async def cmd_start(message: Message):
    text = (
        "🎨 <b>Привет! Я умею делать стикеры из твоих фото и видео.</b>\n\n"
        "Все просто:\n"
        "• Отправь мне <b>фото</b> — получу статичный стикер\n"
        "• Отправь <b>видео</b> (до 3 секунд) — сделаю анимированный WebM-стикер\n"
        "• После этого полученный стикер можно добавить в твой персональный пак\n\n"
        "Команды:\n"
        "/newpack — создать свой стикер-пак\n"
        "/mypack — показать ссылку на твой пак\n"
        "/packset — выбрать стикер для нового пака\n"
        "/add — добавить последний стикер в пак\n"
        "/help — помощь"
    )
    await message.answer(text)


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "Как пользоваться:\n\n"
        "1. Отправьте фотографию — конвертирую в стикер и покажу результат, предложу добавить в пак.\n"
        "2. Отправьте видео до 3 секунд — сделаю анимированный стикер.\n"
        "3. Команда /newpack создаёт ваш личный пак (спрошу название и обложку).\n\n"
        "После создания пака каждый готовый стикер можно сразу положить в него кнопкой «➕ в пак»."
    )


# ----------------------------------------------------------------------------
# Создание пака
# ----------------------------------------------------------------------------
@dp.message(Command("newpack"))
async def cmd_newpack(message: Message, state: FSMContext):
    await state.set_state(CreatePackStates.waiting_for_name)
    await message.answer(
        "Создаём ваш стикер-пак.\n\n"
        "Придумайте <b>название латиницей</b> (только a-z, 0-9, _), например: <code>mycat</code>\n"
        "Это технический id пака. Отправьте его."
    )


@dp.message(CreatePackStates.waiting_for_name, F.text)
async def pack_name(message: Message, state: FSMContext):
    name = message.text.strip()
    # Telegram требует минимум один стикер с эмодзи. Сохраним имя и попросим первый стикер.
    if not all(c.isalnum() or c == "_" for c in name) or len(name) > 64:
        await message.answer("⚠️ Только латинские буквы, цифры и _. Повторите.")
        return
    full_name = f"{name}_by_{BOT_USERNAME}"
    await state.update_data(pack_name=full_name)
    await state.set_state(CreatePackStates.waiting_for_title)
    await message.answer("Отлично! Теперь придумайте <b>название</b> для пака (как его видят люди), например «Мои котики».")


@dp.message(CreatePackStates.waiting_for_title, F.text)
async def pack_title(message: Message, state: FSMContext):
    title = message.text.strip()[:64]
    data = await state.get_data()
    full_name = data["pack_name"]

    try:
        png_bytes = await _make_empty_sticker()
        uploaded = await bot.upload_sticker_file(
            user_id=message.from_user.id,
            sticker=png_bytes,  # BytesIO
            sticker_format=StickerFormat.STATIC,
        )
        await bot.create_new_sticker_set(
            user_id=message.from_user.id,
            name=full_name,
            title=title,
            sticker_type=StickerType.REGULAR,
            stickers=[
                InputSticker(
                    sticker=uploaded.file_id,
                    emoji_list=["😀"],
                    format=StickerFormat.STATIC,
                )
            ],
        )
    except Exception as e:
        logger.exception("Не удалось создать пак")
        await message.answer(f"❌ Не удалось создать пак: {e}\nПопробуйте другое имя (id).")
        return

    await state.clear()
    await message.answer(
        f"✅ Пак <b>{title}</b> создан!\n"
        f"Ссылка: https://t.me/addstickers/{full_name}\n\n"
        "Теперь присылайте фото/видео — каждый результат можно добавить в пак кнопкой «➕ в пак»."
    )


async def _make_empty_sticker() -> BytesIO:
    """Создаём временный прозрачный PNG как 'обложку'-заглушку для нового пака."""
    from PIL import Image
    img = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    buf = BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return buf


@dp.message(Command("mypack"))
async def cmd_mypack(message: Message):
    full_name = f"stickers_{message.from_user.id}_by_{BOT_USERNAME}"
    # Проверим, существует ли пак
    try:
        await bot.get_sticker_set(full_name)
    except Exception:
        await message.answer("У вас пока нет стикер-пака. Создайте через /newpack.")
        return
    await message.answer(f"Ваш пак: https://t.me/addstickers/{full_name}")


# ----------------------------------------------------------------------------
# Обработка медиа
# ----------------------------------------------------------------------------
@dp.message(F.photo)
async def on_photo(message: Message):
    photo = message.photo[-1]  # самое большое разрешение
    async with tempfile.TemporaryDirectory(dir=str(DOWNLOADS)) as tmp:
        tmp_path = Path(tmp)
        source = tmp_path / "input.jpg"
        try:
            await bot.download(photo, destination=source)
            conv = convert_media(source, "image/jpeg", tmp_path)
        except Exception as e:
            logger.exception("Ошибка обработки фото")
            await message.answer(f"❌ Не получилось обработать фото: {e}")
            return
        await _present_sticker(message, conv, is_video=False)


@dp.message(F.video | F.animation)
async def on_video(message: Message):
    if message.video:
        media = message.video
    else:
        media = message.animation

    async with tempfile.TemporaryDirectory(dir=str(DOWNLOADS)) as tmp:
        tmp_path = Path(tmp)
        source = tmp_path / "input.mp4"
        await bot.download(media, destination=source)
        try:
            conv = convert_media(source, "video/mp4", tmp_path)
        except Exception as e:
            await message.answer(f"❌ Не получилось обработать видео: {e}")
            return
        await _present_sticker(message, conv, is_video=True)


async def _present_sticker(message: Message, conv, is_video: bool):
    """Показывает готовый стикер и кнопку добавления в пак."""
    # Сохраняем стикер во временную папку для последующего добавления в пак
    persistent_dir = DOWNLOADS / "stickers" / str(message.from_user.id)
    persistent_dir.mkdir(parents=True, exist_ok=True)
    saved_path = persistent_dir / conv.path.name
    conv.path.replace(saved_path)  # перемещаем из временной папки

    last_sticker[message.from_user.id] = saved_path

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить в мой пак", callback_data="add_to_pack"),
         InlineKeyboardButton(text="💾 Отправить как файл", callback_data="send_as_file")],
    ])
    st_file = FSInputFile(saved_path)
    try:
        await message.answer_sticker(st_file, reply_markup=kb)
    except Exception as e:
        logger.exception("Не смог показать встроенный стикер")
        await message.answer(f"⚠️ Стикер готов, но не смог показать встроенный: {e}\nОтправляю файлом.")
        await message.answer_document(st_file)
        return


# ----------------------------------------------------------------------------
# Callback-кнопки
# ----------------------------------------------------------------------------
@dp.callback_query(F.data == "send_as_file")
async def cb_send_file(call: CallbackQuery):
    path = last_sticker.get(call.from_user.id)
    if not path or not path.exists():
        await call.answer("Сначала отправьте фото/видео ещё раз", show_alert=True)
        return
    await call.answer("Отправляю файл!", show_alert=False)
    await call.message.answer_document(FSInputFile(path))


async def _add_sticker_to_pack(message: Message, user_id: int) -> str:
    """Добавляет последний стикер пользователя в его пак. Возвращает текст результата."""
    path = last_sticker.get(user_id)
    if not path or not path.exists():
        return "Сначала отправьте фото или видео — нечего добавлять."

    if path.suffix not in (".png", ".webm"):
        return "Неизвестный формат стикера."

    full_name = f"stickers_{user_id}_by_{BOT_USERNAME}"
    try:
        await bot.get_sticker_set(full_name)
    except Exception:
        # Пака нет — предлагаем создать
        return (
            "У вас ещё нет стикер-пака. Создайте его командой /newpack, "
            "а затем нажмите «Добавить в пак» снова."
        )

    try:
        # Определяем формат по расширению файла
        if path.suffix == ".webm":
            fmt = StickerFormat.VIDEO
        else:
            fmt = StickerFormat.STATIC
        sticker_input = FSInputFile(path)
        uploaded = await bot.upload_sticker_file(
            user_id=user_id, sticker=sticker_input, sticker_format=fmt
        )
        await bot.add_sticker_to_set(
            user_id=user_id,
            name=full_name,
            sticker=InputSticker(
                sticker=uploaded.file_id,
                emoji_list=["😀"],
                format=fmt,
            ),
        )
        return (
            f"✅ Стикер добавлен в ваш пак.\n"
            f"Ссылка: https://t.me/addstickers/{full_name}"
        )
    except Exception as e:
        logger.exception("Не удалось добавить стикер в пак")
        return f"❌ Не удалось добавить стикер: {e}"


@dp.callback_query(F.data == "add_to_pack")
async def cb_add_to_pack(call: CallbackQuery):
    result = await _add_sticker_to_pack(call.message, call.from_user.id)
    await call.answer()
    await call.message.answer(result)


@dp.message(Command("add"))
async def cmd_add(message: Message):
    result = await _add_sticker_to_pack(message, message.from_user.id)
    await message.answer(result)


# ----------------------------------------------------------------------------
# Запасной ответ на любой непонятный текст (последний в очереди).
# Должен быть объявлен ПОСЛЕ всех команд и FSM-хендлеров, иначе перехватит их.
# ----------------------------------------------------------------------------
@dp.message(F.text, ~F.text.startswith("/"))
async def fallback_text(message: Message, state: FSMContext):
    # Во время создания пака не перебиваем диалог — обработают FSM-хендлеры
    current = await state.get_state()
    if current in (CreatePackStates.waiting_for_name.state, CreatePackStates.waiting_for_title.state):
        return
    await message.answer(
        "Не понял. Используйте /start для помощи, /newpack чтобы создать стикер-пак, "
        "или просто пришлите фото/видео."
    )


# ----------------------------------------------------------------------------
# Запуск
# ----------------------------------------------------------------------------
async def main():
    global BOT_USERNAME, BOT_ID
    if not TOKEN:
        raise SystemExit("Задайте BOT_TOKEN в переменных окружения (см. .env.example)")
    me = await bot.get_me()
    BOT_USERNAME = me.username or (f"bot{me.id}")
    BOT_ID = me.id
    logger.info("Бот запущен: @%s (id=%s)", BOT_USERNAME, BOT_ID)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())