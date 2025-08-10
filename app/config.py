# app/config.py
import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

DB_USER = os.getenv("DB_USER")
DB_PASS = os.getenv("DB_PASS")
DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT")
DB_NAME = os.getenv("DB_NAME")

MOTIVATION_THRESHOLD = 15

DATABASE_URL = f"postgresql+asyncpg://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(admin_id.strip()) for admin_id in ADMIN_IDS_STR.split(',') if admin_id.strip() and admin_id.strip().isdigit()]

CHANNEL_ID = 
CHANNEL_URL = "" 

if not BOT_TOKEN:
    raise ValueError("Необходимо указать BOT_TOKEN в .env файле")
if not all([DB_USER, DB_PASS, DB_HOST, DB_PORT, DB_NAME]):
    raise ValueError("Не все переменные для подключения к БД указаны в .env файле")