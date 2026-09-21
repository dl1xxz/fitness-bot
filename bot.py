import os
import sys
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

import aiosqlite
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LinkPreviewOptions
)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CONTACT = os.getenv("ADMIN_CONTACT", "@juesmely")
ADMIN_ID = os.getenv("ADMIN_ID", "5014057300")

# Реквизиты студии
SBP_PHONE = "89186675213"
SBP_BANK = "Т-Банк"
SBP_RECIPIENT = "Виолетта К."

PAYMENT_LINK = "https://www.tinkoff.ru/rm/r_BoqThxuSKz.joZTwrhWWS/9Y3vz14923"
GROUP_LINK = "https://t.me/+Drh0esF9_ZgyNzQ5"

if not BOT_TOKEN:
    sys.exit("Ошибка: Токен бота не найден! Проверьте переменные окружения.")

DB_NAME = "fitness_club.db"

# Каталог тарифов с тестовым платежом
TARIFFS = {
    "test": {
        "title": "Тестовая оплата (проверка)",
        "price": 1,
        "days": 1,
        "is_trial": False
    },
    "trial": {
        "title": "Пробное занятие",
        "price": 600,
        "days": 1,
        "is_trial": True
    },
    "single": {
        "title": "Разовое занятие",
        "price": 800,
        "days": 1,
        "is_trial": False
    },
    "month": {
        "title": "Абонемент на месяц",
        "price": 3990,
        "days": 30,
        "is_trial": False
    }
}

class ClientRegistration(StatesGroup):
    waiting_for_personal_data = State()

# ==========================================================
# БАЗА ДАННЫХ
# ==========================================================

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                has_used_trial BOOLEAN DEFAULT 0,
                subscription_type TEXT DEFAULT NULL,
                subscription_start_date TIMESTAMP DEFAULT NULL,
                subscription_end_date TIMESTAMP DEFAULT NULL,
                student_info TEXT DEFAULT NULL
            )
        """)
        for column in [
            ("subscription_start_date", "TIMESTAMP"),
            ("student_info", "TEXT")
        ]:
            try:
                await db.execute(f"ALTER TABLE users ADD COLUMN {column[0]} {column[1]}")
            except Exception:
                pass
        await db.commit()

async def get_or_create_user(telegram_id: int, username: Optional[str]) -> Dict[str, Any]:
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)) as cur:
            user = await cur.fetchone()
        
        if not user:
            await db.execute(
                "INSERT INTO users (telegram_id, username, has_used_trial) VALUES (?, ?, 0)",
                (telegram_id, username)
            )
            await db.commit()
            async with db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)) as cur:
                user = await cur.fetchone()
        return dict(user)

async def get_user(telegram_id: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)) as cur:
            user = await cur.fetchone()
            return dict(user) if user else None

async def activate_subscription(telegram_id: int, tariff_key: str):
    tariff = TARIFFS[tariff_key]
    user = await get_user(telegram_id)
    
    now = datetime.now()
    start_date = now
    
    if user and user["subscription_end_date"]:
        try:
            current_end = datetime.fromisoformat(user["subscription_end_date"])
            if current_end > now:
                start_date = current_end
        except ValueError:
            start_date = now

    end_date = start_date + timedelta(days=tariff["days"])

    async with aiosqlite.connect(DB_NAME) as db:
        if tariff["is_trial"]:
            await db.execute("""
                UPDATE users 
                SET subscription_type = ?, 
                    subscription_start_date = ?, 
                    subscription_end_date = ?, 
                    has_used_trial = 1 
                WHERE telegram_id = ?
            """, (tariff["title"], now.isoformat(), end_date.isoformat(), telegram_id))
        else:
            await db.execute("""
                UPDATE users 
                SET subscription_type = ?, 
                    subscription_start_date = ?, 
                    subscription_end_date = ? 
                WHERE telegram_id = ?
            """, (tariff["title"], now.isoformat(), end_date.isoformat(), telegram_id))
        await db.commit()

    return now, end_date

async def save_student_info(telegram_id: int, info: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET student_info = ? WHERE telegram_id = ?", (info, telegram_id))
        await db.commit()

# ==========================================================
# ИНТЕРФЕЙС И КЛАВИАТУРЫ
# ==========================================================

def get_main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💳 Оплатить занятие")],
            [KeyboardButton(text="📋 Мой абонемент")],
            [KeyboardButton(text="📞 Связаться с нами")]
        ],
        resize_keyboard=True,
        is_persistent=True
    )

def get_tariffs_kb(has_used_trial: bool) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="🧪 Тест (для проверки) — 1 ₽", callback_data="buy:test")]
    ]
    
    if not has_used_trial:
        buttons.append([
            InlineKeyboardButton(text="✨ Пробное занятие — 600 ₽", callback_data="buy:trial")
        ])
    else:
        buttons.append([
            InlineKeyboardButton(text="🔒 Пробное занятие (Уже использовано)", callback_data="trial_locked")
        ])

    buttons.append([
        InlineKeyboardButton(text="💃 Разовое занятие — 800 ₽", callback_data="buy:single")
    ])
    buttons.append([
        InlineKeyboardButton(text="⭐ Абонемент на месяц — 3 990 ₽", callback_data="buy:month")
    ])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_sbp_payment_kb(tariff_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Оплатить по ссылке Т-Банка", url=PAYMENT_LINK)],
        [InlineKeyboardButton(text="✅ Я оплатила", callback_data=f"paid:{tariff_key}")],
        [InlineKeyboardButton(text="« Назад к тарифам", callback_data="back_to_tariffs")]
    ])

def get_admin_confirm_kb(user_id: int, tariff_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"adm_confirm:{user_id}:{tariff_key}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_decline:{user_id}")
        ]
    ])

# ==========================================================
# ОБРАБОТЧИКИ
# ==========================================================

dp = Dispatcher(storage=MemoryStorage())

@dp.message(CommandStart())
async def handle_start(message: types.Message, state: FSMContext):
    await state.clear()
    await get_or_create_user(message.from_user.id, message.from_user.username)
    welcome_text = (
        "Добро пожаловать в нашу студию танца!🫶🏻\n"
        "Преподаем в направлениях jazz funk/girly hip-hop💘\n"
        "Набираем девочек в группу возраст от 13 лет, расписание среда/суббота 18:00-19:00❣️\n"
        "Мы находимся на Станиславского 85к1, всех ждём🫶🏻"
    )
    await message.answer(welcome_text, reply_markup=get_main_menu_kb())

@dp.message(F.text == "💳 Оплатить занятие")
async def handle_buy_menu(message: types.Message, state: FSMContext):
    await state.clear()
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    await message.answer(
        "Выберите подходящий вариант для записи:",
        reply_markup=get_tariffs_kb(has_used_trial=bool(user["has_used_trial"]))
    )

@dp.callback_query(F.data == "trial_locked")
async def handle_trial_locked(callback: types.CallbackQuery):
    await callback.answer(
        "Пробное занятие доступно только один раз для новых учениц!",
        show_alert=True
    )

@dp.callback_query(F.data.startswith("buy:"))
async def handle_select_tariff(callback: types.CallbackQuery):
    tariff_key = callback.data.split(":")[1]
    tariff = TARIFFS.get(tariff_key)

    if tariff_key == "trial":
        user = await get_user(callback.from_user.id)
        if user and user["has_used_trial"]:
            await callback.answer("Пробное занятие доступно только один раз!", show_alert=True)
            return

    text = (
        f"Вы выбрали: <b>{tariff['title']}</b>\n"
        f"Сумма к оплате: <b>{tariff['price']} ₽</b>\n\n"
        f"📱 <b>Способ 1: Перевод по СБП (напрямую в банк):</b>\n"
        f"• Номер телефона: <code>{SBP_PHONE}</code> <i>(нажмите для копирования)</i>\n"
        f"• Банк: <b>{SBP_BANK}</b>\n"
        f"• Получатель: <b>{SBP_RECIPIENT}</b>\n\n"
        f"⚠️ <b>ВАЖНО:</b> в комментарии к переводу обязательно напишите ваши <b>ФИО и дату рождения</b>!\n\n"
        f"───────────────\n"
        f"🔗 <b>Способ 2: Оплата по ссылке:</b>\n"
        f"{PAYMENT_LINK}\n"
        f"<i>(Если по ссылке белый экран — откройте её через браузер телефона или переведите по номеру выше)</i>\n\n"
        f"После оплаты нажмите кнопку <b>«✅ Я оплатила»</b> ниже:"
    )

    await callback.message.edit_text(
        text,
        reply_markup=get_sbp_payment_kb(tariff_key),
        link_preview_options=LinkPreviewOptions(is_disabled=True)
    )
    await callback.answer()

@dp.callback_query(F.data == "back_to_tariffs")
async def handle_back(callback: types.CallbackQuery):
    user = await get_user(callback.from_user.id)
    has_trial = bool(user["has_used_trial"]) if user else False
    await callback.message.edit_text(
        "Выберите подходящий вариант для записи:",
        reply_markup=get_tariffs_kb(has_used_trial=has_trial)
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("paid:"))
async def handle_paid_clicked(callback: types.CallbackQuery, state: FSMContext):
    tariff_key = callback.data.split(":")[1]
    tariff = TARIFFS.get(tariff_key)

    if tariff["is_trial"]:
        user = await get_user(callback.from_user.id)
        if user and user["has_used_trial"]:
            await callback.answer("Пробное занятие уже было использовано!", show_alert=True)
            return

    await state.update_data(
        tariff_key=tariff_key,
        tariff_title=tariff["title"],
        tariff_price=tariff["price"]
    )
    await state.set_state(ClientRegistration.waiting_for_personal_data)

    await callback.message.edit_text(
        "📝 <b>Напишите ответным сообщением:</b>\n\n"
        "Вашу <b>Фамилию, Имя и дату рождения</b>\n"
        "<i>(например: Иванова Анна, 15.03.2009)</i>"
    )
    await callback.answer()

@dp.message(ClientRegistration.waiting_for_personal_data)
async def handle_receive_student_data(message: types.Message, state: FSMContext, bot: Bot):
    student_data = message.text.strip()
    await save_student_info(message.from_user.id, student_data)
    
    state_data = await state.get_data()
    tariff_key = state_data.get("tariff_key")
    tariff_title = state_data.get("tariff_title", "Занятие")
    tariff_price = state_data.get("tariff_price", "0")
    
    await state.clear()

    await message.answer(
        "⏳ <b>Ваша заявка отправлена администратору на проверку оплаты.</b>\n\n"
        "Как только платёж подтвердится, вам придёт ссылка на группу и доступ к абонементу🫶🏻",
        reply_markup=get_main_menu_kb()
    )

    if ADMIN_ID:
        username_str = f"@{message.from_user.username}" if message.from_user.username else "не указан"
        admin_text = (
            "🔔 <b>Новая оплата на проверку!</b>\n\n"
            f"💃 <b>Тариф:</b> {tariff_title} ({tariff_price} ₽)\n"
            f"👤 <b>Клиентка:</b> {student_data}\n"
            f"📱 <b>Telegram:</b> {username_str} (ID: <code>{message.from_user.id}</code>)\n"
            f"📅 <b>Время заявки:</b> {datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n"
            "Проверьте поступление средств в Т-Банке (по ФИО в комментарии) и нажмите кнопку:"
        )
        try:
            await bot.send_message(
                chat_id=int(ADMIN_ID),
                text=admin_text,
                reply_markup=get_admin_confirm_kb(message.from_user.id, tariff_key)
            )
        except Exception as e:
            logging.error(f"Не удалось отправить уведомление админу: {e}")

# ==========================================================
# ПОДТВЕРЖДЕНИЕ / ОТКЛОНЕНИЕ
# ==========================================================

@dp.callback_query(F.data.startswith("adm_confirm:"))
async def handle_admin_confirm(callback: types.CallbackQuery, bot: Bot):
    parts = callback.data.split(":")
    user_id = int(parts[1])
    tariff_key = parts[2]
    tariff = TARIFFS.get(tariff_key)

    _, end_date = await activate_subscription(user_id, tariff_key)
    formatted_end = end_date.strftime("%d.%m.%Y")

    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                f"🎉 <b>Оплата подтверждена!</b>\n\n"
                f"Тариф: <b>{tariff['title']}</b>\n"
                f"Действует до: <b>{formatted_end}</b> включительно.\n\n"
                f"🔗 <b>Ссылка на закрытую группу:</b> {GROUP_LINK} 💘\n\n"
                "Ждем вас на занятиях по адресу: ул. Станиславского 85к1🫶🏻"
            )
        )
    except Exception as e:
        logging.error(f"Не удалось отправить сообщение клиенту: {e}")

    await callback.message.edit_text(
        f"{callback.message.text}\n\n"
        f"✅ <b>ОПЛАТА ПОДТВЕРЖДЕНА АДМИНИСТРАТОРОМ</b>"
    )
    await callback.answer("Оплата подтверждена, ссылка отправлена!")

@dp.callback_query(F.data.startswith("adm_decline:"))
async def handle_admin_decline(callback: types.CallbackQuery, bot: Bot):
    user_id = int(callback.data.split(":")[1])

    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                "❌ <b>Оплата не была найдена.</b>\n\n"
                f"Если перевод был отправлен, пожалуйста, свяжитесь с нами: {ADMIN_CONTACT}"
            )
        )
    except Exception as e:
        logging.error(f"Не удалось отправить сообщение клиенту: {e}")

    await callback.message.edit_text(
        f"{callback.message.text}\n\n"
        f"❌ <b>ОПЛАТА ОТКЛОНЕНА</b>"
    )
    await callback.answer("Заявка отклонена")

# ==========================================================
# МЕНЮ
# ==========================================================

@dp.message(F.text == "📋 Мой абонемент")
async def handle_my_sub(message: types.Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    sub_type = user.get("subscription_type")
    start_date_str = user.get("subscription_start_date")
    end_date_str = user.get("subscription_end_date")

    if not sub_type or not end_date_str:
        await message.answer("У вас нет активного абонемента.")
        return

    try:
        end_date = datetime.fromisoformat(end_date_str)
    except ValueError:
        await message.answer("У вас нет активного абонемента.")
        return

    now = datetime.now()
    if end_date <= now:
        await message.answer("У вас нет активного абонемента.")
        return

    formatted_start = "Не указана"
    if start_date_str:
        try:
            formatted_start = datetime.fromisoformat(start_date_str).strftime("%d.%m.%Y")
        except ValueError:
            pass

    formatted_end = end_date.strftime("%d.%m.%Y")

    await message.answer(
        f"📋 <b>Информация о вашем абонементе:</b>\n\n"
        f"• Направление: <b>{sub_type}</b>\n"
        f"• Был оплачен: <b>{formatted_start}</b>\n"
        f"• Действует до: <b>{formatted_end}</b> включительно."
    )

@dp.message(F.text == "📞 Связаться с нами")
async def handle_contacts(message: types.Message):
    await message.answer(
        f"По всем вопросам и для записи пишите: {ADMIN_CONTACT}"
    )

# ==========================================================
# ТОЧКА ВХОДА
# ==========================================================

async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    
    await bot.delete_webhook(drop_pending_updates=True)
    print(">>> ОБНОВЛЕННЫЙ ТАНЦЕВАЛЬНЫЙ БОТ УСПЕШНО ЗАПУЩЕН <<<")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\nБот остановлен.")
