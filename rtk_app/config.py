"""
Настройки проекта: эндпоинты и модели DeepSeek + GigaChat.
"""
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --- DeepSeek ---
DS_API_URL = "https://api.deepseek.com/chat/completions"
DS_KEY_FILE = os.path.join(BASE_DIR, "apidpsk.txt")
# Модель DeepSeek, которая даёт голос. Имя модели в API — deepseek-flash.
DS_MODELS = [
    "deepseek-flash",
]

# --- GigaChat (облачный Сбер) ---
# Ключ: base64 строка "client_id:client_secret" (без переводов).
GC_KEY_FILE = os.path.join(BASE_DIR, "gigakey.txt")
GC_TOKEN_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
GC_API_BASE = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
GC_SCOPE = "GIGACHAT_API_PERS"
GC_MODEL = "GigaChat"          # самая простая/лёгкая
GC_TOKEN_TTL = 1500            # секунд жизни access token (меньше серверного срока)

# --- Общее ---
REQUEST_TIMEOUT = 90

WEB_HOST = "127.0.0.1"
WEB_PORT = 8000
WEB_ROOT = BASE_DIR

# --- Alias / метки ---
KEY_FILE = DS_KEY_FILE
API_URL = DS_API_URL
MODEL = "DeepSeek-flash | GigaChat"

# --- Параметры генерации по умолчанию ---
# Температура, если пользователь не указал свою для модели
DEFAULT_TEMPERATURE = 0.7
# Нижняя/верхняя границы температуры в интерфейсе выбора модели
TEMP_MIN = 0.0
TEMP_MAX = 1.0
TEMP_STEP = 0.05

# --- Максимальное число новых токенов (max_tokens), глобальная настройка ---
# Используется только если пользователь включил параметр в интерфейсе.
# Если настройка выключена — сервер не передаёт max_tokens и модель сама
# выбирает разумный предел вывода.
DEFAULT_MAX_TOKENS = 2048       # значение по умолчанию в поле ввода
MAX_TOKENS_MIN = 1              # минимальное допустимое значение
MAX_TOKENS_MAX = 100000         # верхняя граница (про запас; API может урезать)
MAX_TOKENS_STEP = 50            # шаг изменения в интерфейсе

# --- Сессии диалога (история сохраняется на диск) ---
SESSION_DIR = os.path.join(BASE_DIR, "session")
SESSION_FILE = os.path.join(SESSION_DIR, "session.json")

# --- Сжатие истории диалога ---
# Сколько последних сообщений хранить/передавать полностью (остальное — summary).
# ВАЖНО: keep = 0 означает «сжатие не применять» — ни подстановка summary,
# ни автосжатие не выполняются, в запрос уходит полная история.
COMPACT_KEEP = 10
# Сжатие истории выполняется АВТОМАТИЧЕСКИ и ИНКРЕМЕНТАЛЬНО (без кнопки):
# как только появляется ВЫТЕСНЕННОЕ сообщение (история стала больше keep),
# оно дописывается в summary. Т.е. для keep = 5 сжатие начинается с 6-го
# сообщения и продолжается по мере дальнейшего вытеснения (7-е, 8-е, …).
COMPACT_ENABLED = True
# Минимум сообщений в диалоге, при котором генерация summary имеет смысл.
COMPACT_MIN = 4
# Максимальная длина одного сообщения при подаче на сжатие (символов),
# чтобы запрос к модели не раздувался.
COMPACT_MSG_CAP = 2000
# Модель для генерации summary (используется GigaChat)
COMPACT_MODEL = "GigaChat"

# --- Цены DeepSeek, юань за 1М токенов (вход без кэша/выход, off-peak) ---
DS_PRICE_INPUT_PER_M = {"deepseek-flash": 1.5}
DS_PRICE_OUTPUT_PER_M = {"deepseek-flash": 4.5}


