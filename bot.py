import asyncio
import logging
import aiohttp
import imaplib
import os
import json
from google.oauth2 import service_account
from googleapiclient.discovery import build
from datetime import date, datetime, timedelta

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
import aiosqlite
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import speech_recognition as sr
from pydub import AudioSegment

# ==========================================
# НАСТРОЙКИ
# ==========================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "ТВОЙ_ТОКЕН")
GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS", "lexajukov.82@gmail.com")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "ТВОЙ_ПАРОЛЬ")

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

LAT = 56.3333
LON = 39.3833

# --- НАСТРОЙКА GOOGLE КАЛЕНДАРЯ ---
SCOPES = ['https://www.googleapis.com/auth/calendar.events']
creds = None
try:
    creds_dict = json.loads(os.getenv("GOOGLE_CREDENTIALS", "{}"))
    if creds_dict:
        creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        calendar_service = build('calendar', 'v3', credentials=creds)
    else:
        calendar_service = None
except Exception as e:
    logging.error(f"Ошибка инициализации календаря: {e}")
    calendar_service = None

def add_event_to_calendar(date_str, summary, description=""):
    if not calendar_service:
        logging.warning("Календарь не настроен, событие не добавлено.")
        return False
    try:
        # date_str ожидается в формате YYYY-MM-DD
        event = {
            'summary': summary,
            'description': description,
            'start': {
                'date': date_str,
                'timeZone': 'Europe/Moscow',
            },
            'end': {
                'date': date_str,
                'timeZone': 'Europe/Moscow',
            },
        }
        event_result = calendar_service.events().insert(calendarId='primary', body=event).execute()
        logging.info(f"Событие добавлено в календарь: {event_result.get('htmlLink')}")
        return True
    except Exception as e:
        logging.error(f"Ошибка добавления в календарь: {e}")
        return False
# ----------------------------------

# --- БАЗА ДАННЫХ ---
async def init_db():
    async with aiosqlite.connect("assistant.db") as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, task_text TEXT, task_date TEXT, task_type TEXT)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, note_text TEXT, created_at TEXT)""")
        await db.commit()

# --- СОСТОЯНИЯ ---
class AddTask(StatesGroup): waiting_for_text = State()
class MoveTask(StatesGroup): waiting_for_id = State(); waiting_for_date = State()
class AddWorkoutPlan(StatesGroup): waiting_for_plan = State()
class AddNote(StatesGroup): waiting_for_text = State()
class VoiceChoice(StatesGroup): waiting_for_choice = State()

voice_text_cache = {}

# --- КЛАВИАТУРЫ ---
def get_main_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="📝 Добавить в план", callback_data="add_task")
    builder.button(text="💪 Добавить тренировку", callback_data="add_workout")
    builder.button(text="📓 Новая заметка", callback_data="add_note")
    builder.button(text="📅 План на сегодня", callback_data="show_today")
    builder.button(text="📚 Мои заметки", callback_data="show_notes")
    builder.button(text="🔄 Перенести задачу", callback_data="move_task")
    builder.button(text="📋 Загрузить план тренировок", callback_data="add_workout_plan")
    builder.button(text="📧 Проверить почту", callback_data="check_email")
    return builder.as_markup()

def get_voice_choice_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ В задачи", callback_data="voice_to_task")
    builder.button(text="📓 В заметки", callback_data="voice_to_note")
    builder.button(text="❌ Отмена", callback_data="voice_cancel")
    return builder.as_markup()

# --- ПРОВЕРКА ПОЧТЫ ---
async def get_unread_emails_count():
    def _check_imap():
        try:
            mail = imaplib.IMAP4_SSL('imap.gmail.com')
            mail.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            mail.select('inbox')
            status, messages = mail.search(None, 'UNSEEN')
            if status == 'OK':
                return len(messages[0].split())
            return 0
        except Exception as e:
            logging.error(f"Ошибка почты: {e}")
            return -1
        finally:
            try: mail.close(); mail.logout()
            except: pass
    return await asyncio.to_thread(_check_imap)

# --- ГОЛОСОВЫЕ СООБЩЕНИЯ ---
@dp.message(F.voice)
async def handle_voice_message(message: types.Message):
    await message.answer("🎤 Слушаю и распознаю... Подожди пару секунд ⏳")
    file = await bot.get_file(message.voice.file_id)
    ogg_path = f"voice_{message.from_user.id}.ogg"
    wav_path = f"voice_{message.from_user.id}.wav"
    await bot.download_file(file.file_path, ogg_path)
    try:
        audio = AudioSegment.from_file(ogg_path, format="ogg")
        audio.export(wav_path, format="wav")
        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_path) as source:
            audio_data = recognizer.record(source)
        text = recognizer.recognize_google(audio_data, language="ru-RU")
        voice_text_cache[message.from_user.id] = text
        await message.answer(f"🗣 Я услышал: **\"{text}\"**\n\nКуда это сохранить?", reply_markup=get_voice_choice_keyboard())
    except sr.UnknownValueError:
        await message.answer("🤔 Не удалось распознать речь.")
    except sr.RequestError:
        await message.answer("⚠️ Ошибка сервиса распознавания.")
    except Exception as e:
        logging.error(f"Ошибка голоса: {e}")
        await message.answer("❌ Ошибка обработки аудио.")
    finally:
        if os.path.exists(ogg_path): os.remove(ogg_path)
        if os.path.exists(wav_path): os.remove(wav_path)

@dp.callback_query(F.data == "voice_to_task")
async def voice_to_task(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    text = voice_text_cache.get(user_id, "Текст")
    today = date.today().isoformat()
    async with aiosqlite.connect("assistant.db") as db:
        await db.execute("INSERT INTO tasks (user_id, task_text, task_date, task_type) VALUES (?, ?, ?, ?)",
                         (user_id, text, today, "plan"))
        await db.commit()
    del voice_text_cache[user_id]
    await callback.message.edit_text(f"✅ Добавлено в задачи: **{text}**", reply_markup=get_main_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "voice_to_note")
async def voice_to_note(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    text = voice_text_cache.get(user_id, "Текст")
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    async with aiosqlite.connect("assistant.db") as db:
        await db.execute("INSERT INTO notes (user_id, note_text, created_at) VALUES (?, ?, ?)",
                         (user_id, text, now))
        await db.commit()
    del voice_text_cache[user_id]
    await callback.message.edit_text(f"📓 Сохранено в заметки: **{text}**", reply_markup=get_main_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "voice_cancel")
async def voice_cancel(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if user_id in voice_text_cache:
        del voice_text_cache[user_id]
    await callback.message.edit_text("❌ Отменено.", reply_markup=get_main_keyboard())
    await callback.answer()

# --- СТАРТ ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Привет! Я твой личный ассистент. 🤖\n\nВыбери действие или отправь голосовое сообщение!", reply_markup=get_main_keyboard())

# --- ПРОВЕРКА ПОЧТЫ (кнопка) ---
@dp.callback_query(F.data == "check_email")
async def process_check_email(callback: types.CallbackQuery):
    await callback.answer("Проверяю почту... ⏳")
    unread_count = await get_unread_emails_count()
    if unread_count >= 0:
        await callback.message.answer(f"📧 **Статус почты:**\nУ тебя **{unread_count}** непрочитанных писем.")
    else:
        await callback.message.answer("📧 **Статус почты:**\nНе удалось подключиться.")
    await callback.answer()

# --- ЗАДАЧИ И ТРЕНИРОВКИ ---
@dp.callback_query(F.data == "add_task")
async def process_add_task(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Напиши задачу для плана на сегодня:")
    await state.set_state(AddTask.waiting_for_text)
    await state.update_data(task_type="plan")
    await callback.answer()

@dp.callback_query(F.data == "add_workout")
async def process_add_workout(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Какую тренировку добавим? (например: Бег 5 км или Зал - день ног)")
    await state.set_state(AddTask.waiting_for_text)
    await state.update_data(task_type="workout")
    await callback.answer()

@dp.message(AddTask.waiting_for_text)
async def save_task(message: types.Message, state: FSMContext):
    data = await state.get_data()
    task_type = data.get("task_type", "plan")
    today = date.today().isoformat()
    async with aiosqlite.connect("assistant.db") as db:
        await db.execute("INSERT INTO tasks (user_id, task_text, task_date, task_type) VALUES (?, ?, ?, ?)",
                         (message.from_user.id, message.text, today, task_type))
        await db.commit()
    
    emoji = "💪" if task_type == "workout" else "📝"
    
    # Добавляем в Google Календарь
    add_event_to_calendar(
        date_str=today,
        summary=f"{emoji} {message.text[:50]}...",
        description=message.text
    )
    
    await state.clear()
    await message.answer(f"{emoji} Записал!", reply_markup=get_main_keyboard())
# --- ЗАМЕТКИ ---
@dp.callback_query(F.data == "add_note")
async def process_add_note(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Напиши свою заметку:")
    await state.set_state(AddNote.waiting_for_text)
    await callback.answer()

@dp.message(AddNote.waiting_for_text)
async def save_note(message: types.Message, state: FSMContext):
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    async with aiosqlite.connect("assistant.db") as db:
        await db.execute("INSERT INTO notes (user_id, note_text, created_at) VALUES (?, ?, ?)",
                         (message.from_user.id, message.text, now))
        await db.commit()
    await state.clear()
    await message.answer("📓 Заметка сохранена!", reply_markup=get_main_keyboard())

@dp.callback_query(F.data == "show_notes")
async def show_notes(callback: types.CallbackQuery):
    async with aiosqlite.connect("assistant.db") as db:
        async with db.execute("SELECT note_text, created_at FROM notes WHERE user_id = ? ORDER BY id DESC LIMIT 10", (callback.from_user.id,)) as cursor:
            notes = await cursor.fetchall()
    text = "📚 **Твои последние заметки:**\n\n" + "\n".join([f"• 🕒 {created}\n  {note}\n" for note, created in notes]) if notes else "Пока нет ни одной заметки."
    await callback.message.edit_text(text, reply_markup=get_main_keyboard())
    await callback.answer()

# --- ПЛАН НА СЕГОДНЯ ---
@dp.callback_query(F.data == "show_today")
async def show_today(callback: types.CallbackQuery):
    today = date.today().isoformat()
    async with aiosqlite.connect("assistant.db") as db:
        async with db.execute("SELECT id, task_text, task_type FROM tasks WHERE user_id = ? AND task_date = ?", (callback.from_user.id, today)) as cursor:
            tasks = await cursor.fetchall()
    text = f"📅 **Твой план на {today}:**\n\n" + "\n".join([f"• {'💪' if t[2]=='workout' else '📝'} {t[1]} (ID: {t[0]})" for t in tasks]) if tasks else "На сегодня план пуст."
    try:
        await callback.message.edit_text(text, reply_markup=get_main_keyboard())
    except Exception:
        pass  # Игнорируем ошибку, если текст не изменился
    await callback.answer()

# --- ПЕРЕНОС ЗАДАЧИ ---
@dp.callback_query(F.data == "move_task")
async def process_move_task(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Напиши ID задачи, которую хочешь перенести (ID виден в «План на сегодня»):")
    await state.set_state(MoveTask.waiting_for_id)
    await callback.answer()

@dp.message(MoveTask.waiting_for_id)
async def move_task_get_id(message: types.Message, state: FSMContext):
    try:
        task_id = int(message.text)
        await state.update_data(task_id=task_id)
        await message.answer("На какую дату перенести? (формат: ГГГГ-ММ-ДД, например 2026-10-20)")
        await state.set_state(MoveTask.waiting_for_date)
    except ValueError:
        await message.answer("Нужно ввести число (ID). Попробуй ещё раз:")

@dp.message(MoveTask.waiting_for_date)
async def move_task_complete(message: types.Message, state: FSMContext):
    data = await state.get_data()
    task_id = data.get("task_id")
    new_date = message.text.strip()
    try:
        datetime.strptime(new_date, "%Y-%m-%d")
    except ValueError:
        await message.answer("Неверный формат даты. Используй ГГГГ-ММ-ДД")
        return
    async with aiosqlite.connect("assistant.db") as db:
        await db.execute("UPDATE tasks SET task_date = ? WHERE id = ? AND user_id = ?",
                         (new_date, task_id, message.from_user.id))
        await db.commit()
    await state.clear()
    await message.answer(f"✅ Задача перенесена на {new_date}", reply_markup=get_main_keyboard())

# --- ЗАГРУЗКА ПЛАНА ТРЕНИРОВОК ---
@dp.callback_query(F.data == "add_workout_plan")
async def process_add_workout_plan(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "Пришли план тренировок текстом, по одному дню в строке:\n\n"
        "Понедельник: Бег 5 км\n"
        "Среда: Зал - день ног\n"
        "Пятница: Йога\n\n"
        "Бот сам раскидает их по датам на ближайшую неделю."
    )
    await state.set_state(AddWorkoutPlan.waiting_for_plan)
    await callback.answer()

@dp.message(AddWorkoutPlan.waiting_for_plan)
async def save_workout_plan(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    days_map = {
        "понедельник": 0, "пн": 0, 
        "вторник": 1, "вт": 1, 
        "среда": 2, "ср": 2, 
        "четверг": 3, "чт": 3, 
        "пятница": 4, "пт": 4, 
        "суббота": 5, "сб": 5, 
        "воскресенье": 6, "вс": 6
    }
    
    today = date.today()
    current_weekday = today.weekday()
    added_count = 0
    
    # Разбиваем текст на строки
    lines = message.text.strip().split('\n')
    
    async with aiosqlite.connect("assistant.db") as db:
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            found_day = None
            workout_text = line
            
            # Ищем день недели в любом месте строки (это надежнее, чем startswith)
            for day_name in days_map.keys():
                if day_name in line.lower():
                    found_day = day_name
                    # Находим, где именно начинается день недели
                    idx = line.lower().find(day_name)
                    # Берем весь текст ПОСЛЕ названия дня
                    remainder = line[idx + len(day_name):].strip()
                    # Убираем двоеточия, тире и пробелы в начале
                    workout_text = remainder.lstrip(': \t-–—')
                    break
            
            # Если день найден и текст тренировки не пустой — сохраняем
            if found_day and workout_text:
                target_weekday = days_map[found_day]
                days_ahead = (target_weekday - current_weekday) % 7
                if days_ahead == 0:
                    days_ahead = 7  # Если сегодня этот день, ставим на следующую неделю
                
                target_date = today + timedelta(days=days_ahead)
                
                await db.execute(
                    "INSERT INTO tasks (user_id, task_text, task_date, task_type) VALUES (?, ?, ?, ?)",
                    (user_id, workout_text, target_date.isoformat(), "workout")
                )
                added_count += 1
                
                # Добавляем в Google Календарь
                add_event_to_calendar(
                    date_str=target_date.isoformat(),
                    summary=f"🏃‍️ Тренировка: {workout_text[:50]}...",
                    description=workout_text
                )
    await db.commit()             
    await state.clear()
    await message.answer(f"✅ Успешно добавлено тренировок: {added_count} 🎉\nТеперь каждая тренировка — это отдельная задача!", reply_markup=get_main_keyboard())

# --- СТРАХОВКА ОТ НЕИЗВЕСТНЫХ КНОПОК ---
@dp.callback_query()
async def unknown_callback(callback: types.CallbackQuery):
    await callback.answer("Эта кнопка пока не настроена 🙂")

# --- ПОГОДА ---
async def get_weather_report(is_morning=True):
    url = f"https://api.open-meteo.com/v1/forecast?latitude={LAT}&longitude={LON}&current_weather=true&daily=temperature_2m_max,temperature_2m_min&timezone=Europe/Moscow"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                data = await response.json()
        if is_morning:
            return f"🌤 **Погода в Кольчугино:**\nСейчас: {data['current_weather']['temperature']}°C\nДнем: до {data['daily']['temperature_2m_max'][0]}°C\n"
        else:
            return f"🌤 **Погода на завтра:**\nОт {data['daily']['temperature_2m_min'][1]}°C до {data['daily']['temperature_2m_max'][1]}°C\n"
    except:
        return "🌤 **Погода:** Не удалось загрузить.\n"

# --- УТРЕННИЙ ОТЧЁТ ---
async def morning_report():
    today = date.today().isoformat()
    weather = await get_weather_report(True)
    emails = await get_unread_emails_count()
    email_text = f"📧 **Почта:** {emails} непрочитанных\n" if emails >= 0 else ""
    async with aiosqlite.connect("assistant.db") as db:
        async with db.execute("SELECT DISTINCT user_id FROM tasks WHERE task_date = ?", (today,)) as cursor:
            users = await cursor.fetchall()
    for (user_id,) in users:
        async with aiosqlite.connect("assistant.db") as db:
            async with db.execute("SELECT task_text, task_type FROM tasks WHERE user_id = ? AND task_date = ?", (user_id, today)) as cursor:
                tasks = await cursor.fetchall()
        report = f"☀️ **Доброе утро!**\n\n{weather}{email_text}\n📅 **План на {today}:**\n" + "\n".join([f"• {'💪' if t[1]=='workout' else '📝'} {t[0]}" for t in tasks])
        try: await bot.send_message(user_id, report + "\n\nОтличного дня! 🚀")
        except: pass

# --- ВЕЧЕРНИЙ ОТЧЁТ ---
async def evening_report():
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    weather = await get_weather_report(False)
    emails = await get_unread_emails_count()
    email_text = f"📧 **Почта:** {emails} непрочитанных\n" if emails >= 0 else ""
    async with aiosqlite.connect("assistant.db") as db:
        async with db.execute("SELECT DISTINCT user_id FROM tasks WHERE task_date = ?", (tomorrow,)) as cursor:
            users = await cursor.fetchall()
    for (user_id,) in users:
        async with aiosqlite.connect("assistant.db") as db:
            async with db.execute("SELECT task_text, task_type FROM tasks WHERE user_id = ? AND task_date = ?", (user_id, tomorrow)) as cursor:
                tasks = await cursor.fetchall()
        report = f"🌙 **Добрый вечер!**\n\n{weather}{email_text}\n📅 **План на завтра ({tomorrow}):**\n" + "\n".join([f"• {'💪' if t[1]=='workout' else '📝'} {t[0]}" for t in tasks])
        try: await bot.send_message(user_id, report + "\n\nХорошего отдыха! 😴")
        except: pass

# --- ЗАПУСК ---
async def main():
    await init_db()
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(morning_report, 'cron', hour=8, minute=0)
    scheduler.add_job(evening_report, 'cron', hour=21, minute=0)
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен.")
