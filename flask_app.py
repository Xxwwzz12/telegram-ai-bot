from flask import Flask, request, jsonify
import requests
import json
import time
from collections import deque
import textwrap
import base64
import mimetypes
from datetime import datetime, timedelta
import pytz
from PIL import Image
from io import BytesIO
import logging
import re
import os
import traceback
import calendar
from dotenv import load_dotenv
import tempfile

# Загрузка переменных окружения из .env файла
load_dotenv()

# Определяем путь к файлу состояния в временной директории
STATE_FILE_PATH = os.path.join(tempfile.gettempdir(), 'usage_state.json')

# Универсальный парсер дат
def parse_datetime(dt_str):
    """Универсальный парсер дат с поддержкой Z-формата"""
    if isinstance(dt_str, datetime):
        return dt_str
        
    if dt_str.endswith('Z'):
        dt_str = dt_str[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(dt_str)
    except (TypeError, ValueError):
        return datetime.now(pytz.utc)

# Функция создания начального состояния
def create_initial_state():
    """Создает начальное состояние счетчиков"""
    now = datetime.now(pytz.utc)
    return {
        "deepseek_request_counter": {
            "count": 100,
            "date": now.date(),
            "last_reset": now
        },
        "claude_request_counter": {
            "count": 10,
            "date": now.date(),
            "last_reset": now
        },
        "hf_request_counter": {
            "image": {
                "count": 31,
                "date": now.date()
            }
        },
        "kandinsky_request_counter": {
            "image": {
                "count": 0,
                "date": now.date(),
                "monthly_limit_reset": "2025-07-01"
            }
        }
    }

# Функция загрузки состояния из файла
def load_usage_state():
    """Загружает состояние использования из файла"""
    try:
        # Если файл не существует, создаем начальное состояние
        if not os.path.exists(STATE_FILE_PATH):
            logger.warning(f"Файл состояния {STATE_FILE_PATH} не найден, создаем новый")
            initial_state = create_initial_state()
            save_usage_state(initial_state)
            return initial_state
            
        with open(STATE_FILE_PATH, 'r') as f:
            state = json.load(f)
        
        # Универсальное преобразование дат
        for counter in ["deepseek_request_counter", "claude_request_counter"]:
            if counter in state:
                state[counter]["last_reset"] = parse_datetime(state[counter]["last_reset"])
                state[counter]["date"] = parse_datetime(state[counter]["date"]).date()
        
        if "hf_request_counter" in state and "image" in state["hf_request_counter"]:
            state["hf_request_counter"]["image"]["date"] = parse_datetime(
                state["hf_request_counter"]["image"]["date"]
            ).date()
        
        if "kandinsky_request_counter" in state and "image" in state["kandinsky_request_counter"]:
            state["kandinsky_request_counter"]["image"]["date"] = parse_datetime(
                state["kandinsky_request_counter"]["image"]["date"]
            ).date()
            # Добавляем поле monthly_limit_reset, если его нет
            if "monthly_limit_reset" not in state["kandinsky_request_counter"]["image"]:
                state["kandinsky_request_counter"]["image"]["monthly_limit_reset"] = "2025-07-01"
        
        return state
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as e:
        # Создаем файл при ошибке загрузки
        logger.error(f"Ошибка загрузки состояния: {str(e)}")
        initial_state = create_initial_state()
        save_usage_state(initial_state)
        return initial_state

# Функция сохранения состояния в файл
def save_usage_state(state):
    save_state = {}
    
    # Сохраняем все счетчики
    for counter in ["deepseek_request_counter", "claude_request_counter"]:
        if counter in state:
            save_state[counter] = {
                "count": state[counter]["count"],
                "date": state[counter]["date"].isoformat(),
                "last_reset": state[counter]["last_reset"].isoformat()
            }
    
    # Сохраняем HF счетчик
    if "hf_request_counter" in state:
        save_state["hf_request_counter"] = {}
        for key in state["hf_request_counter"]:
            save_state["hf_request_counter"][key] = {
                "count": state["hf_request_counter"][key]["count"],
                "date": state["hf_request_counter"][key]["date"].isoformat()
            }
    
    # Сохраняем Kandinsky счетчик
    if "kandinsky_request_counter" in state:
        save_state["kandinsky_request_counter"] = {}
        for key in state["kandinsky_request_counter"]:
            save_state["kandinsky_request_counter"][key] = {
                "count": state["kandinsky_request_counter"][key]["count"],
                "date": state["kandinsky_request_counter"][key]["date"].isoformat(),
                "monthly_limit_reset": state["kandinsky_request_counter"][key].get("monthly_limit_reset", "2025-07-01")
            }
    
    try:
        with open(STATE_FILE_PATH, 'w') as f:
            json.dump(save_state, f, indent=2)
        logger.info(f"Состояние сохранено в {STATE_FILE_PATH}")
    except Exception as e:
        logger.error(f"Ошибка сохранения состояния: {str(e)}")

# Настройка логирования - только консольный вывод
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Удаляем все существующие обработчики
for handler in logger.handlers[:]:
    logger.removeHandler(handler)

# Добавляем потоковый обработчик
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(stream_handler)

app = Flask(__name__)

# === КОНФИГУРАЦИЯ ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "7937989706:AAGS7vojWYAXow1lkWwyE3KYZZJ9Ij2rI9o")

# DeepSeek R1 через OpenRouter (РАБОЧИЙ КЛЮЧ)
OPENROUTER_API_KEY = "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"
OPENROUTER_MODEL = "deepseek/deepseek-r1"

# Hugging Face для генерации изображений (резервный вариант) - ВРЕМЕННО ОТКЛЮЧЕН
HF_API_KEY = os.getenv("HF_API_KEY", "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")
HF_MODEL_NAME = "stabilityai/stable-diffusion-xl-base-1.0"

# Kandinsky 4.0 через Fusion Brain (исправленные URL) - ВРЕМЕННО ОТКЛЮЧЕН
KANDINSKY_API_URL = "https://api.fusionbrain.ai/api/v1/models"
KANDINSKY_GENERATE_URL = "https://api.fusionbrain.ai/api/v1/text2image/run"
KANDINSKY_STATUS_URL = "https://api.fusionbrain.ai/api/v1/text2image/status/"
KANDINSKY_API_KEY = os.getenv("KANDINSKY_API_KEY", "DE789CAE8D62AA810347DB1955B596D2")
KANDINSKY_SECRET_KEY = os.getenv("KANDINSKY_SECRET_KEY", "44F8BE6D6A22C546716031E8E9B3F890")

# Replicate.com для генерации изображений (основной вариант) - ВРЕМЕННО ОТКЛЮЧЕН
# REPLICATE_API_KEY = os.getenv("REPLICATE_API_KEY", "r8_IPeghCkTeZzdWNMAY07RSuYriMJ2DoR3V2LLU")

# Лимиты использования
MAX_DAILY_DEEPSEEK_REQUESTS = 1000  # Лимит текстовых запросов
MAX_DAILY_CLAUDE_REQUESTS = 50      # Лимит анализа изображений
HF_BUDGET_TOTAL = 0.10              # Стоимость одного изображения
HF_COST_PER_IMAGE = 0.08 / 31       # Стоимость одного изображения
MAX_MONTHLY_KANDINSKY_REQUESTS = 100  # Месячный лимит для Kandinsky

# Инициализация счётчиков
usage_state = load_usage_state()

# Инициализируем счетчики с защитой от отсутствия ключей
deepseek_request_counter = usage_state.get("deepseek_request_counter", {
    "count": 100,
    "date": datetime.now(pytz.utc).date(),
    "last_reset": datetime.now(pytz.utc)
})

claude_request_counter = usage_state.get("claude_request_counter", {
    "count": 10,
    "date": datetime.now(pytz.utc).date(),
    "last_reset": datetime.now(pytz.utc)
})

hf_request_counter = usage_state.get("hf_request_counter", {
    "image": {
        "count": 31,
        "date": datetime.now(pytz.utc).date()
    }
})

kandinsky_request_counter = usage_state.get("kandinsky_request_counter", {
    "image": {
        "count": 0,
        "date": datetime.now(pytz.utc).date(),
        "monthly_limit_reset": "2025-07-01"
    }
})

# Управление состояниями
user_states = {}
user_histories = {}
MAX_HISTORY = 20

# Системный промпт для DeepSeek R1
SYSTEM_PROMPT = {
    "role": "system",
    "content": "Ты - дружелюбный русскоязычный AI ассистент. Отвечай на языке пользователя. Будь полезным и развернутым."
}

# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ СЧЕТЧИКОВ ===
def check_reset_counter(counter):
    """Сбрасывает счетчик если наступил новый день"""
    now = datetime.now(pytz.utc)
    if now.date() != counter["date"]:
        counter["count"] = 0
        counter["date"] = now.date()
        counter["last_reset"] = now
        save_usage_state(usage_state)
        return True
    return False

def check_monthly_kandinsky_limit():
    """Проверяет и сбрасывает месячный лимит Kandinsky при необходимости"""
    now = datetime.now(pytz.utc)
    reset_date = datetime.fromisoformat(kandinsky_request_counter["image"]["monthly_limit_reset"]).date()
    
    if now.date() >= reset_date:
        # Рассчитываем дату сброса на следующий месяц
        next_month = now.month + 1 if now.month < 12 else 1
        next_year = now.year if now.month < 12 else now.year + 1
        last_day = calendar.monthrange(next_year, next_month)[1]
        new_reset_date = datetime(next_year, next_month, last_day).date()
        
        kandinsky_request_counter["image"]["count"] = 0
        kandinsky_request_counter["image"]["monthly_limit_reset"] = new_reset_date.isoformat()
        save_usage_state(usage_state)
        logger.info(f"Сброс месячного лимита Kandinsky. Новый сброс: {new_reset_date}")
        return True
    return False

def can_make_kandinsky_request():
    """Проверяет возможность выполнения запроса к Kandinsky"""
    check_monthly_kandinsky_limit()
    return kandinsky_request_counter["image"]["count"] < MAX_MONTHLY_KANDINSKY_REQUESTS

def increment_counter(counter):
    """Увеличивает счетчик и сохраняет состояние"""
    if "date" in counter:
        check_reset_counter(counter)
    counter["count"] += 1
    save_usage_state(state=usage_state)

def can_make_request(counter, max_requests):
    """Проверяет возможность выполнения запроса"""
    if "date" in counter:
        check_reset_counter(counter)
    return counter["count"] < max_requests

def get_progress_bar(used, total):
    """Возвращает текстовую строку прогресс-бара"""
    if total == 0:
        return "⬜️⬜️⬜️⬜️⬜️⬜️⬜️⬜️⬜️⬜️"  # Пустой бар если total=0
    
    percent = min(100, (used / total) * 100)  # Ограничиваем 100%
    filled = int(percent / 10)
    return "🟩" * filled + "⬜️" * (10 - filled)

# === ФУНКЦИИ ПРИВЕТСТВИЯ И ПОМОЩИ ===
def send_welcome(chat_id):
    text = "🤖 <b>Добро пожаловать в AI ассистента!</b>\n\n"
    text += "🔹 Работает на моделях DeepSeek R1\n"
    text += "🔹 Поддержка длинных диалогов (контекст 128K токенов)\n"
    text += "🔹 Генерация изображений временно отключена\n\n"
    text += "<b>Используйте кнопку меню (рядом с полем ввода) для доступа к командам!</b>\n\n"
    text += "<b>Основные команды:</b>\n"
    text += "/toggle - включить/выключить бота\n"
    text += "/clear - очистить историю диалога\n"
    text += "/usage - статистика запросов\n"
    text += "/help - справка по командам\n\n"
    text += "Просто напишите вопрос или загрузите изображение для анализа!"

    # Удаляем старую клавиатуру
    remove_keyboard = {"remove_keyboard": True}
    send_message(chat_id, text, reply_markup=remove_keyboard)

def send_help(chat_id):
    """Отправляет справку по командам"""
    text = "📋 <b>Доступные команды:</b>\n\n"
    text += "/start - Запустить бота\n"
    text += "/toggle - Вкл/выкл бота\n"
    text += "/clear - Очистить историю диалога\n"
    text += "/usage - Показать статистику использования\n"
    text += "/help - Показать эту справку\n\n"
    text += "ℹ️ Отправьте текст или изображение для взаимодействия с AI\n"
    text += "ℹ️ Генерация изображений временно отключена"
    
    send_message(chat_id, text)

# === ОБНОВЛЕННАЯ ФУНКЦИЯ СТАТИСТИКИ ===
def send_usage_info(chat_id):
    # Обновляем счетчики (проверяем сброс)
    for counter in [deepseek_request_counter, claude_request_counter]:
        check_reset_counter(counter)
    check_monthly_kandinsky_limit()
    
    # Рассчитываем статистику для DeepSeek
    deepseek_remaining = MAX_DAILY_DEEPSEEK_REQUESTS - deepseek_request_counter["count"]
    deepseek_percent = (deepseek_request_counter["count"] / MAX_DAILY_DEEPSEEK_REQUESTS) * 100
    deepseek_progress = get_progress_bar(deepseek_request_counter["count"], MAX_DAILY_DEEPSEEK_REQUESTS)
    
    # Рассчитываем статистику для Claude
    claude_remaining = MAX_DAILY_CLAUDE_REQUESTS - claude_request_counter["count"]
    claude_percent = (claude_request_counter["count"] / MAX_DAILY_CLAUDE_REQUESTS) * 100
    claude_progress = get_progress_bar(claude_request_counter["count"], MAX_DAILY_CLAUDE_REQUESTS)
    
    # Рассчитываем статистику для HF
    hf_spent = hf_request_counter["image"]["count"] * HF_COST_PER_IMAGE
    hf_remaining = HF_BUDGET_TOTAL - hf_spent
    hf_percent = (hf_spent / HF_BUDGET_TOTAL) * 100
    hf_progress = get_progress_bar(hf_spent, HF_BUDGET_TOTAL)
    
    # Статистика для Kandinsky
    kandinsky_count = kandinsky_request_counter["image"]["count"]
    kandinsky_remaining = MAX_MONTHLY_KANDINSKY_REQUESTS - kandinsky_count
    kandinsky_progress = get_progress_bar(kandinsky_count, MAX_MONTHLY_KANDINSKY_REQUESTS)
    reset_date = datetime.fromisoformat(kandinsky_request_counter["image"]["monthly_limit_reset"]).strftime("%d.%m.%Y")
    
    # Время сброса (берем от DeepSeek как основного счетчика)
    reset_time = deepseek_request_counter["last_reset"] + timedelta(days=1)
    moscow_tz = pytz.timezone('Europe/Moscow')
    reset_time_local = reset_time.astimezone(moscow_tz)

    text = (
        "📊 <b>Статистика использования</b>\n\n"
        "<b>DeepSeek (текстовые запросы):</b>\n"
        f"• Использовано: <b>{deepseek_request_counter['count']}/{MAX_DAILY_DEEPSEEK_REQUESTS}</b>\n"
        f"• Осталось: <b>{deepseek_remaining}</b>\n"
        f"{deepseek_progress}\n\n"
        
        "<b>Claude 3 (анализ изображений):</b>\n"
        f"• Использовано: <b>{claude_request_counter['count']}/{MAX_DAILY_CLAUDE_REQUESTS}</b>\n"
        f"• Осталось: <b>{claude_remaining}</b>\n"
        f"{claude_progress}\n\n"
        
        "<b>Kandinsky 4.0 (генерация изображений):</b>\n"
        f"• Использовано: <b>{kandinsky_count}/{MAX_MONTHLY_KANDINSKY_REQUESTS}</b>\n"
        f"• Осталось: <b>{kandinsky_remaining}</b>\n"
        f"• Сброс лимита: <b>{reset_date}</b>\n"
        f"{kandinsky_progress}\n\n"
        
        "<b>Hugging Face (резервная генерация):</b>\n"
        f"• Изображений сгенерировано: <b>{hf_request_counter['image']['count']}</b>\n"
        f"• Потрачено средств: <b>${hf_spent:.2f} из ${HF_BUDGET_TOTAL:.2f}</b>\n"
        f"• Осталось средств: <b>${hf_remaining:.2f}</b>\n"
        f"{hf_progress}\n\n"
        
        f"<i>Дневные счетчики сбросятся в {reset_time_local.strftime('%H:%M')} (МСК)</i>"
    )

    send_message(chat_id, text)

# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===
def send_typing_action(chat_id):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendChatAction"
    try:
        requests.post(url, json={'chat_id': chat_id, 'action': 'typing'}, timeout=2)
    except Exception as e:
        logger.error(f"Ошибка отправки действия: {str(e)}")

def send_message(chat_id, text, reply_markup=None):
    MAX_LENGTH = 4000
    if len(text) > MAX_LENGTH:
        chunks = split_message(text, MAX_LENGTH)
        for chunk in chunks:
            send_single_message(chat_id, chunk, reply_markup)
        return
    send_single_message(chat_id, text, reply_markup)

def send_single_message(chat_id, text, reply_markup=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'}
    if reply_markup:
        payload['reply_markup'] = json.dumps(reply_markup)

    try:
        # Повторные попытки отправки
        for attempt in range(3):
            try:
                response = requests.post(url, json=payload, timeout=15)
                if response.status_code == 200:
                    return response.json()
                else:
                    logger.warning(f"Попытка {attempt+1}: Ошибка {response.status_code}")
            except Exception as e:
                logger.warning(f"Попытка {attempt+1}: {str(e)}")
                time.sleep(1)

        logger.error("Все попытки отправки сообщения провалились")
        return None
    except Exception as e:
        logger.error(f"Критическая ошибка отправки: {str(e)}")
        return None

def split_message(text, max_length):
    chunks = []
    while text:
        if len(text) > max_length:
            split_pos = text.rfind('\n', 0, max_length)
            if split_pos == -1:
                split_pos = max_length
            chunks.append(text[:split_pos])
            text = text[split_pos:].lstrip()
        else:
            chunks.append(text)
            break
    return chunks

def is_russian(text):
    return bool(re.search(r'[А-Яа-яЁё]', text))

# Функции управления ботом
def toggle_bot_state(chat_id):
    current_state = user_states.get(chat_id, True)
    new_state = not current_state
    user_states[chat_id] = new_state
    if new_state:
        text = "🟢 <b>Бот включен!</b>\n\nГотов к работе!"
    else:
        text = "🔴 <b>Бот отключен</b>\n\nИспользуйте /toggle для включения"
    send_message(chat_id, text)

def clear_history(chat_id):
    if chat_id in user_histories:
        user_histories[chat_id] = deque(maxlen=MAX_HISTORY * 2)
        user_histories[chat_id].append(SYSTEM_PROMPT)
        send_message(chat_id, "🧹 История диалога очищена!")

# Обработка изображений
def compress_image(image_data, max_size=1024, quality=85):
    try:
        img = Image.open(BytesIO(image_data))
        if max(img.size) > max_size:
            img.thumbnail((max_size, max_size))
        if img.mode in ('RGBA', 'P'):
            img = img.convert('RGB')
        output = BytesIO()
        img.save(output, format='JPEG', quality=quality, optimize=True)
        return output.getvalue()
    except Exception as e:
        logger.error(f"Ошибка сжатия изображения: {str(e)}")
        return image_data

def send_photo(chat_id, image_data, caption=""):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    files = {'photo': ('image.jpg', image_data, 'image/jpeg')}
    data = {'chat_id': chat_id, 'caption': caption[:1024]}
    try:
        response = requests.post(url, files=files, data=data, timeout=30)
        logger.info(f"Изображение отправлено, статус: {response.status_code}")
    except Exception as e:
        logger.error(f"Ошибка отправки изображения: {str(e)}")

# Обработка запросов к DeepSeek R1 через OpenRouter
def process_deepseek_request(chat_id, messages, multimodal=False):
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "HTTP-Referer": f"https://Xxwwzz-telegram-ai-bot.hf.space",
        "X-Title": "Telegram AI Assistant"
    }

    # Для мультимодальных запросов используем другую модель
    model = OPENROUTER_MODEL
    if multimodal:
        model = "anthropic/claude-3-haiku"
        logger.info("Используем Claude 3 Haiku для анализа изображений")

    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": 1500,  # Уменьшено для экономии кредитов
        "temperature": 0.7
    }

    try:
        logger.info(f"Отправка запроса к {model} с {len(messages)} сообщениями")
        response = requests.post(url, json=payload, headers=headers, timeout=60)

        if response.status_code == 200:
            data = response.json()
            return data['choices'][0]['message']['content'].strip()
        else:
            # Детальное логирование ошибок API
            error_msg = f"Ошибка API ({response.status_code}): {response.text[:300]}"
            logger.error(error_msg)

            # Специальная обработка ошибки 402 (недостаточно средств)
            if response.status_code == 402:
                return "⚠️ Недостаточно кредитов на OpenRouter для обработки запроса. Пожалуйста, пополните счет на https://openrouter.ai/settings/credits"

            return None
    except requests.exceptions.Timeout:
        logger.error("Таймаут при запросе к API")
        return "⏳ Превышено время ожидания ответа"
    except Exception as e:
        logger.error(f"Исключение в API: {str(e)}")
        return None

# Обработка текстовых сообщений
def handle_text_message(chat_id, text):
    if not can_make_request(deepseek_request_counter, MAX_DAILY_DEEPSEEK_REQUESTS):
        send_message(chat_id, "⚠️ <b>Достигнут дневной лимит текстовых запросов!</b>")
        return

    send_typing_action(chat_id)
    increment_counter(deepseek_request_counter)

    # Добавляем сообщение в историю
    if chat_id not in user_histories:
        user_histories[chat_id] = deque(maxlen=MAX_HISTORY * 2)
        user_histories[chat_id].append(SYSTEM_PROMPT)
    
    user_histories[chat_id].append({"role": "user", "content": text})
    messages = list(user_histories[chat_id])

    # Отправляем запрос в DeepSeek R1
    response = process_deepseek_request(chat_id, messages)

    if response:
        # Добавляем ответ ассистента в историю
        user_histories[chat_id].append({"role": "assistant", "content": response})
        send_message(chat_id, response)
    else:
        send_message(chat_id, "⚠️ Не удалось получить ответ. Попробуйте позже.")

# Обработка изображений
def handle_image_message(chat_id, file_id, caption=""):
    if not can_make_request(claude_request_counter, MAX_DAILY_CLAUDE_REQUESTS):
        send_message(chat_id, "⚠️ <b>Достигнут дневной лимит анализа изображений!</b>")
        return

    send_typing_action(chat_id)
    increment_counter(claude_request_counter)

    # Загрузка изображения из Telegram
    file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}"
    response = requests.get(file_url, timeout=15).json()

    if not response.get('ok'):
        send_message(chat_id, "⚠️ Не удалось загрузить изображение.")
        return

    file_path = response['result']['file_path']
    image_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"

    try:
        image_response = requests.get(image_url, timeout=20)
        if image_response.status_code != 200:
            send_message(chat_id, f"⚠️ Ошибка загрузки изображения: {image_response.status_code}")
            return

        image_data = image_response.content
        compressed_image = compress_image(image_data)
        base64_image = base64.b64encode(compressed_image).decode('utf-8')

        # Определение MIME-типа
        try:
            img = Image.open(BytesIO(compressed_image))
            mime_type = img.format.lower() if img.format else "jpeg"
        except Exception:
            mime_type = "jpeg"
        mime_type = f"image/{mime_type}"

    except Exception as e:
        logger.error(f"Ошибка обработки изображения: {str(e)}")
        send_message(chat_id, "⚠️ Ошибка обработки изображения.")
        return

    # Формируем мультимодальный запрос
    content = []
    if caption:
        content.append({"type": "text", "text": caption})
    content.append({
        "type": "image_url",
        "image_url": {"url": f"data:{mime_type};base64,{base64_image}"}
    })

    # Инициализация истории, если нужно
    if chat_id not in user_histories:
        user_histories[chat_id] = deque(maxlen=MAX_HISTORY * 2)
        user_histories[chat_id].append(SYSTEM_PROMPT)
    
    user_histories[chat_id].append({"role": "user", "content": content})
    messages = list(user_histories[chat_id])

    # Для анализа изображений используем мультимодальный режим
    response = process_deepseek_request(chat_id, messages, multimodal=True)

    if response:
        user_histories[chat_id].append({"role": "assistant", "content": response})
        send_message(chat_id, response)
    else:
        send_message(chat_id, "⚠️ Не удалось проанализировать изображение.")

# === ВРЕМЕННО ОТКЛЮЧЕННЫЕ ФУНКЦИИ ГЕНЕРАЦИИ ИЗОБРАЖЕНИЙ ===
# Генерация изображений временно отключена из-за проблем с доступом к внешним API
# Ключи сохранены для будущего использования
def generate_image(chat_id, prompt):
    """Функция генерации изображений временно отключена"""
    send_message(chat_id, "⚠️ <b>Генерация изображений временно отключена!</b>\n\n"
                "Мы работаем над восстановлением этой функции. "
                "Пожалуйста, попробуйте позже или используйте другие возможности бота.\n\n"
                "Для получения помощи используйте /help")

# Вебхук и обработка сообщений
@app.route('/set_webhook', methods=['GET'])
def set_webhook():
    # Проверка доступности API Telegram
    try:
        test_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe"
        response = requests.get(test_url, timeout=10)
        if response.status_code != 200:
            logger.error(f"Telegram API не отвечает: {response.status_code}")
            return jsonify({"status": "api_unavailable"}), 500
    except Exception as e:
        logger.error(f"Ошибка подключения к Telegram API: {str(e)}")
        return jsonify({"status": "connection_error"}), 500

    # Сначала удалим старый вебхук
    delete_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook"
    try:
        response = requests.get(delete_url, params={"drop_pending_updates": True}, timeout=10)
        logger.info(f"Старый вебхук удалён: {response.json()}")
    except Exception as e:
        logger.error(f"Ошибка удаления вебхука: {str(e)}")

    # Теперь установим новый
    SPACE_ID = os.getenv('SPACE_ID', 'Xxwwzz/telegram-ai-bot')
    SPACE_HOST = SPACE_ID.replace('/', '-') + '.hf.space'
    webhook_url = f"https://{SPACE_HOST}/webhook"
    
    try:
        response = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook",
            params={
                "url": webhook_url,
                "drop_pending_updates": True,
                "max_connections": 100,
                "allowed_updates": json.dumps(["message"])
            },
            timeout=15
        )
        logger.info(f"Новый вебхук установлен: {response.json()}")
        return jsonify(response.json()), 200
    except Exception as e:
        logger.error(f"Ошибка установки вебхука: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/check_webhook')
def check_webhook():
    """Проверяет состояние вебхука"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getWebhookInfo"
        response = requests.get(url, timeout=10).json()
        return jsonify(response)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def setup_webhook():
    """Устанавливает вебхук для Telegram бота"""
    # Явно указываем правильный URL
    webhook_url = "https://Xxwwzz-telegram-ai-bot.hf.space/webhook"
    logger.info(f"Устанавливаем вебхук на URL: {webhook_url}")

    # Удаляем старый вебхук
    delete_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook"
    try:
        response = requests.get(delete_url, params={"drop_pending_updates": True}, timeout=10)
        logger.info(f"Старый вебхук удалён: {response.json()}")
    except Exception as e:
        logger.error(f"Ошибка удаления вебхука: {str(e)}")

    # Устанавливаем новый вебхук
    set_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook"
    payload = {
        "url": webhook_url,
        "max_connections": 100,
        "drop_pending_updates": True
    }

    try:
        response = requests.post(set_url, json=payload, timeout=15)
        logger.info(f"Вебхук установлен: {response.json()}")
        return response.json()
    except Exception as e:
        logger.error(f"Ошибка установки вебхука: {str(e)}")
        return {"status": "error", "message": str(e)}

@app.route('/webhook', methods=['POST'])
def webhook():
    # Проверка доступности Telegram API
    try:
        requests.get(f"https://api.telegram.org", timeout=5)
    except Exception as e:
        logger.critical(f"Telegram API недоступен: {str(e)}")
        return jsonify({"status": "error", "message": "Telegram API недоступен"}), 500

    try:
        update = request.json

        # Логируем входящее обновление
        logger.debug(f"Получено обновление: {json.dumps(update, indent=2)}")

        if 'message' in update:
            message = update['message']
            chat_id = message['chat']['id']

            # Инициализация состояния
            if chat_id not in user_states:
                user_states[chat_id] = True
                
            if chat_id not in user_histories:
                user_histories[chat_id] = deque(maxlen=MAX_HISTORY * 2)
                user_histories[chat_id].append(SYSTEM_PROMPT)

            # Обработка команд
            if 'text' in message:
                text = message['text']
                if text == '/start':
                    send_welcome(chat_id)
                elif text == '/toggle':
                    toggle_bot_state(chat_id)
                elif text == '/clear':
                    clear_history(chat_id)
                elif text == '/usage':
                    send_usage_info(chat_id)
                elif text == '/help':
                    send_help(chat_id)
                elif text.startswith('/generate'):
                    generate_image(chat_id, text.replace('/generate', '', 1).strip())
                else:
                    if user_states.get(chat_id, True):
                        handle_text_message(chat_id, text)
                    else:
                        send_message(chat_id, "❌ Бот отключен. Включите: /toggle")

            # Обработка изображений
            elif 'photo' in message:
                if user_states.get(chat_id, True):
                    photo = message['photo'][-1]  # Берем последнее (самое качественное) фото
                    file_id = photo['file_id']
                    caption = message.get('caption', '')
                    handle_image_message(chat_id, file_id, caption)
                else:
                    send_message(chat_id, "❌ Бот отключен. Включите: /toggle")

            # Обработка документов (изображений)
            elif 'document' in message:
                if user_states.get(chat_id, True):
                    document = message['document']
                    mime_type = document.get('mime_type', '')
                    if mime_type.startswith('image/'):
                        file_id = document['file_id']
                        caption = message.get('caption', '')
                        handle_image_message(chat_id, file_id, caption)
                    else:
                        send_message(chat_id, "❌ Пожалуйста, отправьте изображение")
                else:
                    send_message(chat_id, "❌ Бот отключен. Включите: /toggle")

        return jsonify({"status": "ok"}), 200

    except Exception as e:
        logger.error(f"Webhook error: {str(e)}\n{traceback.format_exc()}")
        # Принудительно перезагружаем состояние при критической ошибке
        global usage_state, deepseek_request_counter, claude_request_counter, hf_request_counter, kandinsky_request_counter
        usage_state = load_usage_state()
        deepseek_request_counter = usage_state["deepseek_request_counter"]
        claude_request_counter = usage_state["claude_request_counter"]
        hf_request_counter = usage_state["hf_request_counter"]
        kandinsky_request_counter = usage_state["kandinsky_request_counter"]
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/')
def home():
    return "✅ Бот активен! Отправьте /start в Telegram."

@app.route('/fix_menu')
def fix_menu():
    """Принудительное обновление меню для отладки"""
    try:
        set_bot_commands()
        set_menu_button()

        # Дополнительная диагностика
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getChatMenuButton"
        response = requests.get(url, timeout=5)
        logger.info(f"Текущее состояние кнопки меню: {response.json()}")

        return "Меню обновлено! Проверьте логи для деталей."
    except Exception as e:
        logger.error(f"Ошибка в fix_menu: {str(e)}")
        return f"Ошибка: {str(e)}"

@app.route('/reset_state')
def reset_state():
    """Сброс файла состояния"""
    try:
        if os.path.exists(STATE_FILE_PATH):
            os.remove(STATE_FILE_PATH)
            logger.info(f"Файл состояния {STATE_FILE_PATH} удалён")
    except Exception as e:
        logger.error(f"Ошибка удаления файла состояния: {str(e)}")
    
    global usage_state, deepseek_request_counter, claude_request_counter, hf_request_counter, kandinsky_request_counter
    usage_state = load_usage_state()
    deepseek_request_counter = usage_state["deepseek_request_counter"]
    claude_request_counter = usage_state["claude_request_counter"]
    hf_request_counter = usage_state["hf_request_counter"]
    kandinsky_request_counter = usage_state["kandinsky_request_counter"]
    
    return f"Состояние сброшено! Новый файл состояния: {STATE_FILE_PATH}"

@app.route('/debug_state')
def debug_state():
    state = load_usage_state()
    return jsonify(state)

# === УСТАНОВКА МЕНЮ КОМАНД ===
def set_bot_commands():
    """Устанавливает меню команд для бота"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setMyCommands"
    commands = [
        {"command": "start", "description": "Запустить бота"},
        {"command": "toggle", "description": "Вкл/выкл бота"},
        {"command": "clear", "description": "Очистить историю"},
        {"command": "usage", "description": "Статистика запросов"},
        {"command": "help", "description": "Справка по командам"}
    ]
    payload = {"commands": commands}

    try:
        response = requests.post(url, json=payload, timeout=15)
        if response.status_code == 200:
            logger.info("Меню команд успешно установлено")
        else:
            logger.error(f"Ошибка установки меню: {response.text}")
    except Exception as e:
        logger.error(f"Ошибка при установке меню команд: {str(e)}")

# === УСТАНОВКА КНОПКИ МЕНЮ ===
def set_menu_button():
    """Устанавливает кнопку меню в интерфейсе Telegram"""
    # Сначала сбросим все предыдущие настройки
    reset_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setChatMenuButton"
    reset_payload = {"menu_button": {"type": "default"}}

    try:
        response = requests.post(reset_url, json=reset_payload, timeout=15)
        if response.status_code == 200:
            logger.info("Кнопка меню успешно сброшена")
        else:
            logger.error(f"Ошибка сброса кнопки меню: {response.text}")
    except Exception as e:
        logger.error(f"Ошибка при сбросе кнопки меню: {str(e)}")

    # Теперь установим новую кнопку
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setChatMenuButton"
    payload = {
        "menu_button": {
            "type": "commands"
        }
    }

    try:
        response = requests.post(url, json=payload, timeout=15)
        if response.status_code == 200:
            logger.info("Кнопка меню успешно установлена")
        else:
            logger.error(f"Ошибка установки кнопки меню: {response.text}")
    except Exception as e:
        logger.error(f"Ошибка при установке кнопки меню: {str(e)}")

    # Принудительно обновляем команды
    set_bot_commands()

if __name__ == '__main__':
    # Включаем отладочное логирование
    logger.setLevel(logging.DEBUG)

    # Логируем путь к файлу состояния
    logger.info(f"Используем файл состояния: {STATE_FILE_PATH}")

    # Устанавливаем команды и кнопку меню
    set_bot_commands()
    set_menu_button()

    # Устанавливаем вебхук
    webhook_result = setup_webhook()
    logger.info(f"Результат установки вебхука: {webhook_result}")

    # Дополнительная диагностика
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMyCommands"
        response = requests.get(url, timeout=10)
        logger.debug(f"Текущие команды: {response.json()}")
    except Exception as e:
        logger.error(f"Ошибка проверки команд: {str(e)}")

    app.run(debug=True)
