import os
import json
import sqlite3
import aiohttp
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils import executor
from dotenv import load_dotenv
from aiogram.utils.callback_data import CallbackData

mod_search_cb = CallbackData("mod", "index")  # для кнопок выбора модов

user_state = {}  # для хранения состояния поиска (списка модов) по пользователям

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")
if not TOKEN or not ADMIN_USERNAME:
    raise ValueError("Не указан BOT_TOKEN или ADMIN_USERNAME в token.env")

bot = Bot(token=TOKEN)
dp = Dispatcher(bot)

WHITELIST = ["Keptchypk", "I_am_kil1ed"]

conn = sqlite3.connect("mods.db")
cursor = conn.cursor()
cursor.execute("""
    CREATE TABLE IF NOT EXISTS mods (
        id TEXT PRIMARY KEY,
        name TEXT,
        url TEXT
    )
""")
conn.commit()

user_states = {}
last_messages = {}

def is_whitelisted(username):
    return username in WHITELIST or username == ADMIN_USERNAME

def get_mods():
    cursor.execute("SELECT * FROM mods")
    return cursor.fetchall()

def mod_exists(mod_id):
    cursor.execute("SELECT 1 FROM mods WHERE id = ?", (mod_id,))
    return cursor.fetchone() is not None

@dp.message_handler(commands=["start", "help"])
async def start_handler(message: types.Message):
    if not is_whitelisted(message.from_user.username):
        return await message.reply("У вас нет доступа к этому боту.")
    
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("📦 Показать сохранённые моды", callback_data="show_saved_mods"))

    await message.reply(
        "Привет! Отправь название мода для Minecraft, и я найду его на Modrinth.\n"
        "Или нажми на кнопку ниже, чтобы посмотреть сохранённые моды.\n"
        "Команды: /start; /list",
        reply_markup=keyboard
    )

@dp.callback_query_handler(lambda c: c.data == "show_saved_mods")
async def show_saved_mods(callback_query: types.CallbackQuery):
    mods = get_mods()
    if not mods:
        await callback_query.message.edit_text("Список модов пуст.")
        return
    await callback_query.message.delete()
    await send_mods_page(callback_query.message.chat.id, mods, 0, callback_query.from_user.username)


@dp.message_handler(commands=["restart", "reload", "q"])
async def restart_handler(message: types.Message):
    if message.from_user.username != ADMIN_USERNAME:
        return await message.reply("У вас нет доступа к этой команде.")
    await message.reply("Бот перезапускается (требуется ручной рестарт скрипта).")
    await bot.close()
    raise SystemExit

@dp.message_handler(commands=["list"])
async def list_handler(message: types.Message):
    if not is_whitelisted(message.from_user.username):
        return await message.reply("У вас нет доступа к этому боту.")
    mods = get_mods()
    if not mods:
        return await message.reply("Список модов пуст.")
    await send_mods_page(message.chat.id, mods, 0, message.from_user.username)

async def send_mods_page(chat_id, mods, page, username):
    start = page * 10
    end = start + 10
    current_mods = mods[start:end]
    text = "\n".join([f"{i+1+start}. {mod[1]}" for i, mod in enumerate(current_mods)])
    markup = InlineKeyboardMarkup(row_width=1)
    for mod in current_mods:
        markup.add(InlineKeyboardButton(mod[1], callback_data=f"view:{mod[0]}"))

    nav_buttons = []
    if start > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️", callback_data=f"page:{page-1}"))
    if end < len(mods):
        nav_buttons.append(InlineKeyboardButton("➡️", callback_data=f"page:{page+1}"))
    if nav_buttons:
        markup.row(*nav_buttons)

    sent = await bot.send_message(chat_id, f"Сохранённые моды:\n{text}", reply_markup=markup)
    last_messages[chat_id] = sent.message_id

@dp.callback_query_handler(lambda c: c.data.startswith("page:"))
async def page_handler(callback_query: types.CallbackQuery):
    page = int(callback_query.data.split(":")[1])
    mods = get_mods()
    await bot.delete_message(callback_query.message.chat.id, callback_query.message.message_id)
    await send_mods_page(callback_query.message.chat.id, mods, page, callback_query.from_user.username)

@dp.message_handler()
async def handle_message(message: types.Message):
    if not is_whitelisted(message.from_user.username):
        return await message.reply("У вас нет доступа к этому боту.")
    query = message.text
    url = f"https://api.modrinth.com/v2/search?query={query}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                return await message.reply("Ошибка при обращении к Modrinth.")
            data = await resp.json()
    if not data["hits"]:
        return await message.reply("Не удалось найти подходящих модов на Modrinth.")

    user_state[message.from_user.id] = {
        "mods_found": [
            {
                "id": hit["project_id"],
                "name": hit["title"],
                "description": hit.get("description", "Описание отсутствует."),
                "slug": hit.get("slug", "")
            } for hit in data["hits"][:10]
        ],
        "step": "choosing_mod"
    }

    keyboard = InlineKeyboardMarkup(row_width=1)
    for i, mod in enumerate(user_state[message.from_user.id]["mods_found"]):
        keyboard.insert(InlineKeyboardButton(mod["name"], callback_data=mod_search_cb.new(index=i)))

    sent = await message.reply("Пожалуйста, выбери мод:", reply_markup=keyboard)
    last_messages[message.chat.id] = sent.message_id

@dp.callback_query_handler(mod_search_cb.filter())
async def mod_details(callback_query: types.CallbackQuery, callback_data: dict):
    user_id = callback_query.from_user.id
    if user_id not in user_state or user_state[user_id].get("step") != "choosing_mod":
        await callback_query.answer("Сессия устарела, начните поиск заново.", show_alert=True)
        return

    index = int(callback_data["index"])
    mods = user_state[user_id]["mods_found"]
    if index >= len(mods):
        await callback_query.answer("Неверный выбор.", show_alert=True)
        return

    mod = mods[index]
    mod_id = mod["id"]
    download_url = f"https://modrinth.com/mod/{mod.get('slug', '')}"

    if mod_exists(mod_id):
        buttons = [
            InlineKeyboardButton("🔗 Скачать", url=download_url),
            InlineKeyboardButton("🔙 Назад", callback_data="back_to_search")
        ]
    else:
        buttons = [
            InlineKeyboardButton("💾 Сохранить", callback_data=f"save:{mod_id}"),
            InlineKeyboardButton("🔗 Скачать", url=download_url),
            InlineKeyboardButton("🔙 Назад", callback_data="back_to_search")
        ]
    markup = InlineKeyboardMarkup(row_width=1)
    for btn in buttons:
        markup.add(btn)

    await callback_query.message.edit_text(
        f"**{mod['name']}**\n\n{mod['description']}",
        parse_mode="Markdown",
        reply_markup=markup
    )
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("save:"))
async def save_mod(callback_query: types.CallbackQuery):
    mod_id = callback_query.data.split(":")[1]
    async with aiohttp.ClientSession() as session:
        async with session.get(f"https://api.modrinth.com/v2/project/{mod_id}") as resp:
            if resp.status != 200:
                return await callback_query.message.edit_text("Ошибка при получении данных мода.")
            mod = await resp.json()
    download_url = f"https://modrinth.com/mod/{mod['slug']}"
    if mod_exists(mod_id):
        return await callback_query.message.edit_text("Этот мод уже сохранён.")

    cursor.execute("INSERT INTO mods (id, name, url) VALUES (?, ?, ?)", (mod_id, mod["title"], download_url))
    conn.commit()
    await callback_query.message.edit_text("Мод сохранён!")

@dp.callback_query_handler(lambda c: c.data.startswith("view:"))
async def view_saved_mod(callback_query: types.CallbackQuery):
    mod_id = callback_query.data.split(":")[1]
    cursor.execute("SELECT * FROM mods WHERE id = ?", (mod_id,))
    mod = cursor.fetchone()
    if not mod:
        return await callback_query.answer("Мод не найден.")
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(InlineKeyboardButton("🔗 Скачать", url=mod[2]))
    if callback_query.from_user.username == ADMIN_USERNAME:
        markup.add(InlineKeyboardButton("🗑 Удалить", callback_data=f"delete:{mod_id}"))
    markup.add(InlineKeyboardButton("🔙 Назад", callback_data="back_to_list"))
    await callback_query.message.edit_text(f"**{mod[1]}**", parse_mode="Markdown", reply_markup=markup)

@dp.callback_query_handler(lambda c: c.data == "back_to_list")
async def back_to_list(callback_query: types.CallbackQuery):
    mods = get_mods()
    await callback_query.message.delete()
    await send_mods_page(callback_query.message.chat.id, mods, 0, callback_query.from_user.username)

@dp.callback_query_handler(lambda c: c.data.startswith("delete:"))
async def delete_mod(callback_query: types.CallbackQuery):
    if callback_query.from_user.username != ADMIN_USERNAME:
        return await callback_query.answer("У вас нет доступа к этой кнопке.")
    mod_id = callback_query.data.split(":")[1]
    cursor.execute("DELETE FROM mods WHERE id = ?", (mod_id,))
    conn.commit()

    keyboard = InlineKeyboardMarkup().add(
        InlineKeyboardButton("🔙 Назад", callback_data="back_to_list")
    )

    await callback_query.message.edit_text("✅ Мод удалён.", reply_markup=keyboard)

@dp.callback_query_handler(lambda c: c.data == "back_to_search")
async def back_to_search(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    if user_id not in user_state or "mods_found" not in user_state[user_id]:
        await callback_query.answer("История поиска недоступна, начните заново.", show_alert=True)
        return

    mods_found = user_state[user_id]["mods_found"]
    kb = InlineKeyboardMarkup(row_width=1)
    for i, mod in enumerate(mods_found):
        kb.insert(InlineKeyboardButton(text=mod["name"], callback_data=mod_search_cb.new(index=i)))

    await callback_query.message.edit_text(
        "Пожалуйста, выберите мод из списка кнопок ниже:",
        reply_markup=kb
    )
    await callback_query.answer()

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True)