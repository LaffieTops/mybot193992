import asyncio
import logging
import random
import os

from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils import executor
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.contrib.fsm_storage.memory import MemoryStorage

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError

from tinydb import TinyDB, Query

# Настройки
API_TOKEN = '7744788611:AAGe2lmUsrA0U2eD4w0LU24jx6KnR0R6mfQ'
db = TinyDB("db.json")
logging.basicConfig(level=logging.INFO)

bot = Bot(token=API_TOKEN, parse_mode="HTML")
dp = Dispatcher(bot, storage=MemoryStorage())

clients = {}
sending_tasks = {}

# FSM для задержек
class EditDelays(StatesGroup):
    waiting_for_delays = State()

# Главное меню
def main_menu():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("➕ Добавить аккаунт", callback_data="add_account"))
    for acc in db.table("accounts"):
        kb.add(InlineKeyboardButton(f"Аккаунт: {acc['phone']}", callback_data=f"menu_{acc['phone']}"))
    return kb

def account_menu(phone):
    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton("▶️ Старт", callback_data=f"start_{phone}"),
        InlineKeyboardButton("⏹ Стоп", callback_data=f"stop_{phone}")
    )
    kb.add(
        InlineKeyboardButton("✏️ Сообщение", callback_data=f"editmsg_{phone}"),
        InlineKeyboardButton("⏱ Задержки", callback_data=f"editdelays_{phone}")
    )
    kb.add(
        InlineKeyboardButton("📜 Чаты", callback_data=f"listchats_{phone}"),
        InlineKeyboardButton("🔄 Обновить чаты", callback_data=f"updatechats_{phone}")
    )
    kb.add(InlineKeyboardButton("❌ Удалить аккаунт", callback_data=f"delacc_{phone}"))
    kb.add(InlineKeyboardButton("⬅️ Назад", callback_data="back"))
    return kb

@dp.message_handler(commands=["start"])
async def start_handler(msg: types.Message):
    await msg.answer("Добро пожаловать в бота для рассылки!", reply_markup=main_menu())

@dp.callback_query_handler()
async def callback_handler(call: types.CallbackQuery):
    data = call.data
    if data == "add_account":
        await call.message.answer("Введи API ID:")
        await AddAccount.waiting_for_api_id.set()
    elif data == "back":
        await call.message.edit_text("Главное меню", reply_markup=main_menu())
    elif data.startswith("menu_"):
        phone = data.split("_", 1)[1]
        await call.message.edit_text(f"Меню аккаунта: {phone}", reply_markup=account_menu(phone))
    elif data.startswith("editmsg"):
        phone = data.split("_", 1)[1]
        await call.message.answer("Введи новое сообщение:")
        state = dp.current_state(user=call.from_user.id)
        await state.set_state("edit_message")
        await state.update_data(phone=phone)
    elif data.startswith("editdelays"):
        phone = data.split("_", 1)[1]
        await call.message.answer("Введи задержки (сек): <мин> <макс> <между циклами>")
        state = dp.current_state(user=call.from_user.id)
        await EditDelays.waiting_for_delays.set()
        await state.update_data(phone=phone)
    elif data.startswith("listchats"):
        phone = data.split("_", 1)[1]
        acc = get_account(phone)
        chats = acc.get("chats", [])
        text = "\n".join([f"- {c['title']}" for c in chats]) or "Нет чатов"
        await call.message.answer(f"Список чатов:\n{text}")
    elif data.startswith("updatechats"):
        phone = data.split("_", 1)[1]
        await call.message.answer("Обновляю чаты...")
        await update_chats(phone, call.message)
    elif data.startswith("delacc"):
        phone = data.split("_", 1)[1]
        db.table("accounts").remove(Query().phone == phone)
        await call.message.answer("Аккаунт удалён", reply_markup=main_menu())
    elif data.startswith("start"):
        phone = data.split("_", 1)[1]
        if phone not in sending_tasks:
            task = asyncio.create_task(start_sending(phone, call.message))
            sending_tasks[phone] = task
            await call.message.answer("Рассылка запущена.")
        else:
            await call.message.answer("Уже запущена.")
    elif data.startswith("stop"):
        phone = data.split("_", 1)[1]
        task = sending_tasks.pop(phone, None)
        if task:
            task.cancel()
            await call.message.answer("Рассылка остановлена.")

class AddAccount(StatesGroup):
    waiting_for_api_id = State()
    waiting_for_api_hash = State()
    waiting_for_phone = State()
    waiting_for_code = State()

@dp.message_handler(state=AddAccount.waiting_for_api_id)
async def get_api_id(msg: types.Message, state: FSMContext):
    await state.update_data(api_id=int(msg.text))
    await msg.answer("Теперь введи API Hash:")
    await AddAccount.next()

@dp.message_handler(state=AddAccount.waiting_for_api_hash)
async def get_api_hash(msg: types.Message, state: FSMContext):
    await state.update_data(api_hash=msg.text)
    await msg.answer("Теперь введи номер:")
    await AddAccount.next()

@dp.message_handler(state=AddAccount.waiting_for_phone)
async def get_phone(msg: types.Message, state: FSMContext):
    await state.update_data(phone=msg.text)
    data = await state.get_data()
    client = TelegramClient(StringSession(), data['api_id'], data['api_hash'])
    await client.connect()
    try:
        await client.send_code_request(data['phone'])
        clients[msg.from_user.id] = client
        await msg.answer("Введи код:")
        await AddAccount.next()
    except Exception as e:
        await msg.answer(f"Ошибка: {e}")
        await client.disconnect()
        await state.finish()

@dp.message_handler(state=AddAccount.waiting_for_code)
async def get_code(msg: types.Message, state: FSMContext):
    data = await state.get_data()
    client = clients.get(msg.from_user.id)
    try:
        await client.sign_in(data['phone'], msg.text)
        session = client.session.save()
        db.table("accounts").insert({
            "phone": data['phone'],
            "api_id": data['api_id'],
            "api_hash": data['api_hash'],
            "session": session,
            "message": "✉️ Тестовое сообщение",
            "delays": {"min": 40, "max": 110, "cycle": 3900},
            "chats": []
        })
        await msg.answer("Аккаунт добавлен!", reply_markup=main_menu())
        await update_chats(data['phone'], msg)
    except SessionPasswordNeededError:
        await msg.answer("Нужен пароль 2FA — не поддерживается.")
    except Exception as e:
        await msg.answer(f"Ошибка: {e}")
    finally:
        await client.disconnect()
        await state.finish()

def get_account(phone):
    return db.table("accounts").get(Query().phone == phone)

async def update_chats(phone, msg):
    acc = get_account(phone)
    client = TelegramClient(StringSession(acc['session']), acc['api_id'], acc['api_hash'])
    await client.connect()
    chats = []
    async for dialog in client.iter_dialogs():
        if dialog.is_group or dialog.is_channel:
            chats.append({"id": dialog.id, "title": dialog.name})
    db.table("accounts").update({"chats": chats}, Query().phone == phone)
    await client.disconnect()
    await msg.answer(f"Чаты обновлены: {len(chats)}")

@dp.message_handler(state="edit_message")
async def edit_msg(msg: types.Message, state: FSMContext):
    data = await state.get_data()
    db.table("accounts").update({"message": msg.text}, Query().phone == data["phone"])
    await msg.answer("Сообщение обновлено.")
    await state.finish()

@dp.message_handler(state=EditDelays.waiting_for_delays)
async def edit_delays(msg: types.Message, state: FSMContext):
    try:
        min_d, max_d, cycle_d = map(int, msg.text.strip().split())
        if min_d > max_d:
            await msg.answer("Мин. задержка не может быть больше макс.")
            return
        data = await state.get_data()
        db.table("accounts").update({
            "delays": {"min": min_d, "max": max_d, "cycle": cycle_d}
        }, Query().phone == data["phone"])
        await msg.answer("Задержки обновлены.")
    except:
        await msg.answer("Формат: 2 5 60")
    finally:
        await state.finish()

async def start_sending(phone, msg):
    acc = get_account(phone)
    client = TelegramClient(StringSession(acc['session']), acc['api_id'], acc['api_hash'])
    await client.connect()
    try:
        while True:
            acc = get_account(phone)
            success, failed = 0, 0
            for chat in acc["chats"]:
                try:
                    await client.send_message(chat['id'], acc["message"])
                    success += 1
                    await msg.answer(f"✅ {chat['title']}")
                except Exception as e:
                    failed += 1
                    await msg.answer(f"❌ {chat['title']} — {e}")
                d = random.randint(acc["delays"]["min"], acc["delays"]["max"])
                await asyncio.sleep(d)
            await msg.answer(f"Цикл завершён. Успешно: {success} | Ошибки: {failed}")
            await asyncio.sleep(acc["delays"]["cycle"])
    except asyncio.CancelledError:
        pass
    finally:
        await client.disconnect()

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True)