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
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)

# ==========================================================
# 1. ЗАГРУЗКА ПЕРЕМЕННЫХ ОКРУЖЕНИЯ
# ==========================================================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CONTACT = os.getenv("ADMIN_CONTACT", "@BAZU193")

if not BOT_TOKEN:
    sys.exit("Ошибка: Токен бота не найден! Проверьте файл .env")

DB_NAME = "fitness_club.db"

# Каталог тарифов клуба
TARIFFS = {
    "trial": {
        "title": "Пробное занятие",
        "price": 500,
        "days": 1,
        "is_trial": True
    },
    "single": {
        "title": "Разовое занятие",
        "price": 1000,
        "days": 1,
        "is_trial": False
    },
    "month": {
        "title": "Абонемент на месяц (30 дней)",
        "price": 5000,
        "days": 30,
        "is_trial": False
    }
}

# ==========================================================
# 2. РАБОТА С БАЗОЙ ДАННЫХ (aiosqlite)
# ==========================================================

async def init_db():
    """Инициализация базы данных и создание таблицы при старте."""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                has_used_trial BOOLEAN DEFAULT 0,
                subscription_type TEXT DEFAULT NULL,
                subscription_end_date TIMESTAMP DEFAULT NULL
            )
        """)
        await db.commit()

async def get_or_create_user(telegram_id: int, username: Optional[str]) -> Dict[str, Any]:
    """Получение пользователя или добавление нового в базу."""
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
    """Получение актуальных данных о пользователе."""
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)) as cur:
            user = await cur.fetchone()
            return dict(user) if user else None

async def activate_subscription(telegram_id: int, tariff_key: str) -> datetime:
    """Активация и продление срока действия тарифа."""
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

    new_end_date = start_date + timedelta(days=tariff["days"])

    async with aiosqlite.connect(DB_NAME) as db:
        if tariff["is_trial"]:
            await db.execute("""
                UPDATE users 
                SET subscription_type = ?, subscription_end_date = ?, has_used_trial = 1 
                WHERE telegram_id = ?
            """, (tariff["title"], new_end_date.isoformat(), telegram_id))
        else:
            await db.execute("""
                UPDATE users 
                SET subscription_type = ?, subscription_end_date = ? 
                WHERE telegram_id = ?
            """, (tariff["title"], new_end_date.isoformat(), telegram_id))
        await db.commit()

    return new_end_date

# ==========================================================
# 3. КЛАВИАТУРЫ
# ==========================================================

def get_main_menu_kb() -> ReplyKeyboardMarkup:
    """Главная панель кнопок под строкой ввода."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💳 Купить занятие / абонемент")],
            [KeyboardButton(text="📋 Мой абонемент")],
            [KeyboardButton(text="📞 Связаться с нами")]
        ],
        resize_keyboard=True,
        is_persistent=True
    )

def get_tariffs_kb(has_used_trial: bool) -> InlineKeyboardMarkup:
    """Кнопки выбора тарифов."""
    buttons = []
    
    if not has_used_trial:
        buttons.append([
            InlineKeyboardButton(text="🔥 Пробное занятие — 500 ₽", callback_data="buy:trial")
        ])
    else:
        buttons.append([
            InlineKeyboardButton(text="🔒 Пробное занятие (Использовано)", callback_data="trial_locked")
        ])

    buttons.append([
        InlineKeyboardButton(text="🏋️ Разовое занятие — 1 000 ₽", callback_data="buy:single")
    ])
    buttons.append([
        InlineKeyboardButton(text="⭐ Абонемент на месяц — 5 000 ₽", callback_data="buy:month")
    ])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_payment_kb(tariff_key: str) -> InlineKeyboardMarkup:
    """Кнопка подтверждения демонстрационной оплаты."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Оплатить (Тест СБП)", callback_data=f"pay:{tariff_key}")],
        [InlineKeyboardButton(text="« Назад к тарифам", callback_data="back_to_tariffs")]
    ])

# ==========================================================
# 4. ОБРАБОТЧИКИ СОБЫТИЙ (ХЭНДЛЕРЫ)
# ==========================================================

dp = Dispatcher()

@dp.message(CommandStart())
async def handle_start(message: types.Message):
    await get_or_create_user(message.from_user.id, message.from_user.username)
    await message.answer(
        "Добро пожаловать в наш фитнес-клуб! Выберите нужное действие в меню ниже:",
        reply_markup=get_main_menu_kb()
    )

@dp.message(F.text == "💳 Купить занятие / абонемент")
async def handle_buy_menu(message: types.Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    await message.answer(
        "Выберите подходящий вариант занятий:\n\n"
        "• <b>Пробное занятие</b>: доступно только 1 раз для новых клиентов.\n"
        "• <b>Разовое занятие</b>: тренировка на 1 день.\n"
        "• <b>Абонемент на месяц</b>: 30 дней безлимитного посещения.",
        reply_markup=get_tariffs_kb(has_used_trial=bool(user["has_used_trial"]))
    )

@dp.callback_query(F.data == "trial_locked")
async def handle_trial_locked(callback: types.CallbackQuery):
    await callback.answer(
        "Вы уже использовали свое пробное занятие. Доступны разовые посещения и абонементы.",
        show_alert=True
    )

@dp.callback_query(F.data.startswith("buy:"))
async def handle_select_tariff(callback: types.CallbackQuery):
    tariff_key = callback.data.split(":")[1]
    tariff = TARIFFS.get(tariff_key)

    if tariff_key == "trial":
        user = await get_user(callback.from_user.id)
        if user and user["has_used_trial"]:
            await callback.answer(
                "Вы уже использовали свое пробное занятие. Доступны разовые посещения и абонементы.",
                show_alert=True
            )
            return

    await callback.message.edit_text(
        f"Вы выбрали: <b>{tariff['title']}</b>\n"
        f"Стоимость: <b>{tariff['price']} ₽</b>\n"
        f"Срок действия: <b>{tariff['days']} дн.</b>\n\n"
        "Для демонстрации нажмите кнопку тестовой оплаты ниже:",
        reply_markup=get_payment_kb(tariff_key)
    )
    await callback.answer()

@dp.callback_query(F.data == "back_to_tariffs")
async def handle_back(callback: types.CallbackQuery):
    user = await get_user(callback.from_user.id)
    has_trial = bool(user["has_used_trial"]) if user else False
    await callback.message.edit_text(
        "Выберите подходящий вариант занятий:",
        reply_markup=get_tariffs_kb(has_used_trial=has_trial)
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("pay:"))
async def handle_payment(callback: types.CallbackQuery):
    tariff_key = callback.data.split(":")[1]
    tariff = TARIFFS.get(tariff_key)

    if tariff["is_trial"]:
        user = await get_user(callback.from_user.id)
        if user and user["has_used_trial"]:
            await callback.answer("Ошибка: пробное занятие уже использовано!", show_alert=True)
            return

    end_date = await activate_subscription(callback.from_user.id, tariff_key)
    formatted_date = end_date.strftime("%d.%m.%Y в %H:%M")

    await callback.message.edit_text(
        f"🎉 <b>Оплата прошла успешно!</b>\n\n"
        f"Тариф: <b>{tariff['title']}</b>\n"
        f"Действует до: <b>{formatted_date}</b>\n\n"
        "Информация сохранена в разделе «📋 Мой абонемент»."
    )
    await callback.answer("Оплата подтверждена!")

@dp.message(F.text == "📋 Мой абонемент")
async def handle_my_sub(message: types.Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    sub_type = user.get("subscription_type")
    end_date_str = user.get("subscription_end_date")

    if not sub_type or not end_date_str:
        await message.answer(
            "У вас нет активного абонемента. Перейдите в раздел покупки, чтобы оформить его."
        )
        return

    try:
        end_date = datetime.fromisoformat(end_date_str)
    except ValueError:
        await message.answer("Ошибка формата даты. Свяжитесь с администратором.")
        return

    now = datetime.now()
    if end_date <= now:
        await message.answer(
            "Срок действия вашего абонемента истек. Перейдите в раздел покупки, чтобы продлить его."
        )
        return

    remaining = end_date - now
    days = remaining.days
    hours = remaining.seconds // 3600
    time_str = f"{days} дн. {hours} ч." if days > 0 else f"{hours} ч."

    await message.answer(
        f"📋 <b>Ваш абонемент:</b>\n\n"
        f"• Тариф: <b>{sub_type}</b>\n"
        f"• Действует до: <b>{end_date.strftime('%d.%m.%Y %H:%M')}</b>\n"
        f"• Осталось времени: <b>{time_str}</b>\n\n"
        "Ждем вас на тренировке! 💪"
    )

@dp.message(F.text == "📞 Связаться с нами")
async def handle_contacts(message: types.Message):
    await message.answer(
        "📞 <b>Контакты фитнес-клуба</b>\n\n"
        f"👤 <b>Администратор:</b> {ADMIN_CONTACT}\n"
        "📱 <b>Телефон:</b> +7 (999) 000-11-22\n"
        "📍 <b>Адрес:</b> ул. Спортивная, д. 10\n"
        "🕒 <b>Режим работы:</b> 07:00 — 23:00 без выходных"
    )

# ==========================================================
# 5. ЗАПУСК БОТА
# ==========================================================

async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    
    await bot.delete_webhook(drop_pending_updates=True)
    
    print("\n" + "="*40)
    print(">>> БОТ УСПЕШНО ЗАПУЩЕН НА ХОСТИНГЕ! <<<")
    print("Для остановки нажмите Ctrl + C")
    print("="*40 + "\n")
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\nБот остановлен.")
