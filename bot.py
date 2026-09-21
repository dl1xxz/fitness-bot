import os
import sys
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

import aiosqlite
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
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

# ==========================================================
# 1. КОНФИГУРАЦИЯ
# ==========================================================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CONTACT = os.getenv("ADMIN_CONTACT", "@juesmely")

# Оба администратора жестко зафиксированы в коде
ADMIN_IDS: List[int] = [5014057300, 944829858]

# Дополнительно подтягиваем ID из переменных окружения, если они там указаны
env_admins = os.getenv("ADMIN_IDS", "")
for item in env_admins.split(","):
    clean_id = item.strip()
    if clean_id.isdigit() and int(clean_id) not in ADMIN_IDS:
        ADMIN_IDS.append(int(clean_id))

SBP_PHONE = "89186675213"
SBP_BANK = "Т-Банк"
SBP_RECIPIENT = "Виолетта К."

PAYMENT_LINK = "https://www.tinkoff.ru/rm/r_BoqThxuSKz.joZTwrhWWS/9Y3vz14923"
GROUP_LINK = "https://t.me/+Drh0esF9_ZgyNzQ5"

if not BOT_TOKEN:
    sys.exit("Ошибка: Токен бота не найден! Проверьте переменные окружения.")

# Защищенная папка для постоянного хранения базы данных
PERSISTENT_DIR = os.getenv("DATA_DIR", "/app/data" if os.path.exists("/app/data") else ".")
os.makedirs(PERSISTENT_DIR, exist_ok=True)
DB_NAME = os.path.join(PERSISTENT_DIR, "fitness_club.db")

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

# ==========================================================
# 2. СОСТОЯНИЯ (FSM)
# ==========================================================

class ClientRegistration(StatesGroup):
    waiting_for_personal_data = State()

class AdminStates(StatesGroup):
    waiting_for_broadcast = State()
    waiting_for_manual_id = State()
    waiting_for_manual_name = State()

# ==========================================================
# 3. БАЗА ДАННЫХ
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

async def get_db_stats() -> Dict[str, int]:
    now_iso = datetime.now().isoformat()
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            total_users = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE has_used_trial = 1") as cur:
            trials_used = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE subscription_end_date > ?", (now_iso,)) as cur:
            active_subs = (await cur.fetchone())[0]
    return {
        "total_users": total_users,
        "trials_used": trials_used,
        "active_subs": active_subs
    }

async def get_active_students() -> List[Dict[str, Any]]:
    now_iso = datetime.now().isoformat()
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE subscription_end_date > ? ORDER BY subscription_end_date ASC",
            (now_iso,)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

async def get_all_user_ids() -> List[int]:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT telegram_id FROM users") as cur:
            rows = await cur.fetchall()
            return [r[0] for r in rows]

# ==========================================================
# 4. КЛАВИАТУРЫ
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

def get_admin_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 Статистика", callback_data="adm_panel:stats"),
            InlineKeyboardButton(text="📋 Активные абонементы", callback_data="adm_panel:students")
        ],
        [
            InlineKeyboardButton(text="➕ Выдать абонемент вручную", callback_data="adm_panel:manual_sub")
        ],
        [
            InlineKeyboardButton(text="📢 Рассылка сообщений", callback_data="adm_panel:broadcast")
        ],
        [
            InlineKeyboardButton(text="❌ Закрыть меню", callback_data="adm_panel:close")
        ]
    ])

def get_manual_tariffs_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Пробное (1 день)", callback_data="adm_grant:trial")],
        [InlineKeyboardButton(text="Разовое (1 день)", callback_data="adm_grant:single")],
        [InlineKeyboardButton(text="Абонемент (30 дней)", callback_data="adm_grant:month")],
        [InlineKeyboardButton(text="« Отмена", callback_data="adm_panel:cancel")]
    ])

# ==========================================================
# 5. ХЭНДЛЕРЫ КЛИЕНТСКОЙ ЧАСТИ
# ==========================================================

dp = Dispatcher(storage=MemoryStorage())

@dp.message(CommandStart())
async def handle_start(message: types.Message, state: FSMContext):
    await state.clear()
    await get_or_create_user(message.from_user.id, message.from_user.username)
    
    welcome_text = (
        "✨ <b>Добро пожаловать в студию танца!</b> 🫶🏻\n\n"
        "Раскрываем пластику, ритм и уверенность в себе через яркую современную хореографию 💘\n\n"
        "💃 <b>Направления:</b> Jazz Funk / Girly Hip-Hop\n"
        "👥 <b>Возраст:</b> Набор в группу для девочек от 13 лет\n"
        "📍 <b>Локация:</b> ул. Станиславского, 85к1\n\n"
        "Ждём вас на паркете! Выберите нужное действие в меню ниже ⬇️"
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
        f"{PAYMENT_LINK}\n\n"
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

    username_str = f"@{message.from_user.username}" if message.from_user.username else "не указан"
    admin_text = (
        "🔔 <b>Новая оплата на проверку!</b>\n\n"
        f"💃 <b>Тариф:</b> {tariff_title} ({tariff_price} ₽)\n"
        f"👤 <b>Клиентка:</b> {student_data}\n"
        f"📱 <b>Telegram:</b> {username_str} (ID: <code>{message.from_user.id}</code>)\n"
        f"📅 <b>Время заявки:</b> {datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n"
        "Проверьте поступление средств и подтвердите оплату:"
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=admin_text,
                reply_markup=get_admin_confirm_kb(message.from_user.id, tariff_key)
            )
        except Exception as e:
            logging.error(f"Не удалось отправить уведомление админу {admin_id}: {e}")

@dp.callback_query(F.data.startswith("adm_confirm:"))
async def handle_admin_confirm(callback: types.CallbackQuery, bot: Bot):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("У вас нет прав администратора!", show_alert=True)
        return

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
                "Ждем вас на занятиях по адресу: ул. Станиславского, 85к1🫶🏻"
            )
        )
    except Exception as e:
        logging.error(f"Не удалось отправить сообщение клиенту {user_id}: {e}")

    admin_name = callback.from_user.first_name or "Администратор"
    await callback.message.edit_text(
        f"{callback.message.text}\n\n"
        f"✅ <b>ОПЛАТА ПОДТВЕРЖДЕНА</b> (админ: {admin_name})"
    )
    await callback.answer("Оплата подтверждена!")

@dp.callback_query(F.data.startswith("adm_decline:"))
async def handle_admin_decline(callback: types.CallbackQuery, bot: Bot):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("У вас нет прав администратора!", show_alert=True)
        return

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
        logging.error(f"Не удалось отправить сообщение клиенту {user_id}: {e}")

    admin_name = callback.from_user.first_name or "Администратор"
    await callback.message.edit_text(
        f"{callback.message.text}\n\n"
        f"❌ <b>ОПЛАТА ОТКЛОНЕНА</b> (админ: {admin_name})"
    )
    await callback.answer("Заявка отклонена")

@dp.message(F.text == "📋 Мой абонемент")
async def handle_my_sub(message: types.Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    sub_type = user.get("subscription_type")
    start_date_str = user.get("subscription_start_date")
    end_date_str = user.get("subscription_end_date")
    student_name = user.get("student_info") or "Данные не указаны"

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
        f"👤 <b>Ученица:</b> {student_name}\n"
        f"💃 <b>Направление / Тариф:</b> {sub_type}\n"
        f"📅 <b>Был оплачен:</b> {formatted_start}\n"
        f"⏳ <b>Действует до:</b> {formatted_end} включительно."
    )

@dp.message(F.text == "📞 Связаться с нами")
async def handle_contacts(message: types.Message):
    await message.answer(
        f"По всем вопросам и для записи пишите: {ADMIN_CONTACT}"
    )

# ==========================================================
# 6. АДМИН-ПАНЕЛЬ (/admin)
# ==========================================================

@dp.message(Command("admin"))
async def handle_admin_command(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer(
            f"⛔ <b>Доступ запрещен.</b>\nВаш ID: <code>{message.from_user.id}</code>\n"
            "Этот аккаунт не найден в списке администраторов."
        )
        return

    await state.clear()
    await message.answer(
        "👑 <b>Панель управления студией:</b>\n\n"
        "Выберите действие из меню ниже:",
        reply_markup=get_admin_main_kb()
    )

@dp.callback_query(F.data == "adm_panel:close")
async def handle_admin_close(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        return
    await state.clear()
    await callback.message.delete()
    await callback.answer()

@dp.callback_query(F.data == "adm_panel:cancel")
async def handle_admin_cancel(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        return
    await state.clear()
    await callback.message.edit_text(
        "👑 <b>Панель управления студией:</b>",
        reply_markup=get_admin_main_kb()
    )
    await callback.answer("Действие отменено")

@dp.callback_query(F.data == "adm_panel:stats")
async def handle_admin_stats(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return

    stats = await get_db_stats()
    text = (
        "📊 <b>Текущая статистика студии:</b>\n\n"
        f"👥 Всего пользователей в базе: <b>{stats['total_users']}</b>\n"
        f"🎟 Использовано пробных занятий: <b>{stats['trials_used']}</b>\n"
        f"⭐ Активных абонементов сейчас: <b>{stats['active_subs']}</b>"
    )
    await callback.message.edit_text(text, reply_markup=get_admin_main_kb())
    await callback.answer()

@dp.callback_query(F.data == "adm_panel:students")
async def handle_admin_students(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return

    students = await get_active_students()
    if not students:
        await callback.message.edit_text(
            "📋 В данный момент нет действующих активных абонементов.",
            reply_markup=get_admin_main_kb()
        )
        await callback.answer()
        return

    text_lines = ["📋 <b>Список активных учениц:</b>\n"]
    for idx, s in enumerate(students, 1):
        name = s.get("student_info") or "Имя не указано"
        uname = f"(@{s['username']})" if s.get("username") else f"(ID: {s['telegram_id']})"
        end_d = datetime.fromisoformat(s["subscription_end_date"]).strftime("%d.%m.%Y")
        stype = s.get("subscription_type") or "Абонемент"
        text_lines.append(f"{idx}. <b>{name}</b> {uname}\n   • {stype} — до {end_d}")

    full_text = "\n".join(text_lines)
    if len(full_text) > 4000:
        full_text = full_text[:4000] + "\n... (список обрезан)"

    await callback.message.edit_text(full_text, reply_markup=get_admin_main_kb())
    await callback.answer()

@dp.callback_query(F.data == "adm_panel:manual_sub")
async def handle_manual_sub_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        return

    await state.set_state(AdminStates.waiting_for_manual_id)
    await callback.message.edit_text(
        "➕ <b>Ручная выдача абонемента:</b>\n\n"
        "Отправьте ответным сообщением <b>Telegram ID</b> ученицы (только цифры):\n"
        "<i>(Ученица может узнать свой ID через @userinfobot)</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Отмена", callback_data="adm_panel:cancel")]
        ])
    )
    await callback.answer()

@dp.message(AdminStates.waiting_for_manual_id)
async def handle_manual_id_input(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return

    text = message.text.strip()
    if not text.isdigit():
        await message.answer("⚠️ ID должен состоять только из цифр. Попробуйте еще раз или нажмите /admin для отмены:")
        return

    user_id = int(text)
    await state.update_data(target_user_id=user_id)
    await state.set_state(AdminStates.waiting_for_manual_name)
    await message.answer(
        f"Отлично. Теперь введите <b>ФИО и дату рождения</b> ученицы для ID <code>{user_id}</code>:"
    )

@dp.message(AdminStates.waiting_for_manual_name)
async def handle_manual_name_input(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return

    student_name = message.text.strip()
    await state.update_data(target_student_name=student_name)
    await message.answer(
        f"Ученица: <b>{student_name}</b>\n\n"
        "Выберите тариф для ручной активации:",
        reply_markup=get_manual_tariffs_kb()
    )

@dp.callback_query(F.data.startswith("adm_grant:"))
async def handle_manual_grant(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
    if callback.from_user.id not in ADMIN_IDS:
        return

    tariff_key = callback.data.split(":")[1]
    tariff = TARIFFS.get(tariff_key)

    state_data = await state.get_data()
    user_id = state_data.get("target_user_id")
    student_name = state_data.get("target_student_name")
    await state.clear()

    await get_or_create_user(user_id, username=None)
    await save_student_info(user_id, student_name)
    _, end_date = await activate_subscription(user_id, tariff_key)
    formatted_end = end_date.strftime("%d.%m.%Y")

    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                f"🎉 <b>Вам активирован абонемент администратором студии!</b>\n\n"
                f"Тариф: <b>{tariff['title']}</b>\n"
                f"Действует до: <b>{formatted_end}</b> включительно.\n\n"
                f"🔗 <b>Ссылка на закрытую группу:</b> {GROUP_LINK} 💘\n\n"
                "Ждем вас на занятиях по адресу: ул. Станиславского, 85к1🫶🏻"
            )
        )
    except Exception as e:
        logging.warning(f"Не удалось отправить уведомление пользователю {user_id}: {e}")

    await callback.message.edit_text(
        f"✅ <b>Абонемент успешно выдан!</b>\n\n"
        f"• Ученица: <b>{student_name}</b> (ID: <code>{user_id}</code>)\n"
        f"• Тариф: {tariff['title']}\n"
        f"• Срок: до {formatted_end}",
        reply_markup=get_admin_main_kb()
    )
    await callback.answer()

@dp.callback_query(F.data == "adm_panel:broadcast")
async def handle_broadcast_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        return

    await state.set_state(AdminStates.waiting_for_broadcast)
    await callback.message.edit_text(
        "📢 <b>Рассылка сообщений:</b>\n\n"
        "Отправьте следующее сообщение, которое хотите разослать всем пользователям бота (поддерживаются текст, фото и форматирование):\n\n"
        "<i>Для отмены нажмите кнопку ниже:</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Отмена", callback_data="adm_panel:cancel")]
        ])
    )
    await callback.answer()

@dp.message(AdminStates.waiting_for_broadcast)
async def handle_broadcast_send(message: types.Message, state: FSMContext, bot: Bot):
    if message.from_user.id not in ADMIN_IDS:
        return

    await state.clear()
    user_ids = await get_all_user_ids()
    status_msg = await message.answer(f"⏳ Запуск рассылки на <b>{len(user_ids)}</b> пользователей...")

    success = 0
    failed = 0

    for uid in user_ids:
        try:
            await message.copy_to(chat_id=uid)
            success += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1

    await status_msg.edit_text(
        f"✅ <b>Рассылка завершена!</b>\n\n"
        f"• Успешно доставлено: <b>{success}</b>\n"
        f"• Не удалось отправить: <b>{failed}</b> (заблокировали бота или удалили аккаунт)",
        reply_markup=get_admin_main_kb()
    )

# ==========================================================
# 7. ТОЧКА ВХОДА
# ==========================================================

async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    
    try:
        await bot.set_my_description(
            description="Добро пожаловать в нашу студию танца!🫶🏻"
        )
    except Exception as e:
        logging.warning(f"Не удалось обновить описание бота: {e}")

    await bot.delete_webhook(drop_pending_updates=True)
    print(f">>> БОТ ЗАПУЩЕН! СПИСОК АДМИНИСТРАТОРОВ: {ADMIN_IDS} <<<")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\nБот остановлен.")
