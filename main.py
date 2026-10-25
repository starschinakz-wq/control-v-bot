import asyncio
import html
import json
import logging
import os
import random
import re
import sqlite3
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    FSInputFile,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
logging.basicConfig(level=logging.INFO)

ADMIN_USER_IDS = {
    int(admin_id.strip())
    for admin_id in os.environ.get("ADMIN_USER_IDS", "").split(",")
    if admin_id.strip().isdigit()
}

ADMIN_STATS_BUTTON = "📊 Статистика системы"
ADMIN_EXPORT_BUTTON = "📦 Скачать файлы данных"
ADMIN_REPLY_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=ADMIN_STATS_BUTTON)],
        [KeyboardButton(text=ADMIN_EXPORT_BUTTON)],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

# --- БАЗА ДАННЫХ (SQLite) ---
DB_NAME = "bot_database.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    try:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS test_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                gender TEXT,
                profile_group TEXT,
                experience TEXT,
                sector TEXT,
                avg_latency REAL,
                doubt_index REAL,
                latencies_str TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS enterprise_leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_name TEXT NOT NULL,
                contact_name TEXT NOT NULL,
                email TEXT NOT NULL,
                message TEXT NOT NULL DEFAULT '',
                consent INTEGER NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        columns = {row[1] for row in cursor.execute("PRAGMA table_info(test_results)")}
        if "doubt_index" not in columns:
            cursor.execute("ALTER TABLE test_results ADD COLUMN doubt_index REAL")
        if "gender" not in columns:
            cursor.execute("ALTER TABLE test_results ADD COLUMN gender TEXT")
        conn.commit()
    finally:
        conn.close()

init_db()

def save_result_to_db(
    user_id,
    username,
    gender,
    profile_group,
    experience,
    sector,
    avg_latency,
    doubt_index,
    latencies,
):
    conn = sqlite3.connect(DB_NAME)
    try:
        cursor = conn.cursor()
        latencies_str = ", ".join(map(str, latencies))
        cursor.execute('''
            INSERT INTO test_results 
            (user_id, username, gender, profile_group, experience, sector, avg_latency, doubt_index, latencies_str)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            user_id,
            username,
            gender,
            profile_group,
            experience,
            sector,
            avg_latency,
            doubt_index,
            latencies_str,
        ))
        conn.commit()
    finally:
        conn.close()

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_LEAD_BODY_BYTES = 16_384

def validate_enterprise_lead(payload):
    if not isinstance(payload, dict):
        raise ValueError("Пожалуйста, отправьте действительную форму.")

    def required_text(field, label, max_length):
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} is required.")
        value = value.strip()
        if len(value) > max_length:
            raise ValueError(f"{label} is too long.")
        return value

    company_name = required_text("company_name", "Company name", 160)
    contact_name = required_text("contact_name", "Contact name", 120)
    email = required_text("email", "Work email", 254).lower()
    if not EMAIL_PATTERN.fullmatch(email):
        raise ValueError("Введите действительный адрес электронной почты.")

    message = payload.get("message", "")
    if not isinstance(message, str):
        raise ValueError("Сообщение должно быть текстом.")
    message = message.strip()
    if len(message) > 2000:
        raise ValueError("Сообщение слишком длинное.")

    if payload.get("consent") is not True:
        raise ValueError("Пожалуйста, согласитесь на то, чтобы с вами связались по поводу вашего запроса.")

    return company_name, contact_name, email, message

def save_enterprise_lead(company_name, contact_name, email, message):
    conn = sqlite3.connect(DB_NAME)
    try:
        conn.execute(
            """
            INSERT INTO enterprise_leads (company_name, contact_name, email, message, consent)
            VALUES (?, ?, ?, ?, 1)
        """,
            (company_name, contact_name, email, message),
        )
        conn.commit()
    finally:
        conn.close()

class LandingRequestHandler(BaseHTTPRequestHandler):
    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if urlsplit(self.path).path not in ("/", "/index.html"):
            self.send_error(404)
            return
        try:
            page = Path(__file__).resolve().with_name("index.html").read_bytes()
        except OSError:
            logging.exception("Не удалось прочитать целевую страницу Control-V.")
            self.send_error(500, "Страница недоступна")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.end_headers()
        self.wfile.write(page)

    def do_POST(self):
        if urlsplit(self.path).path != "/api/leads":
            self.send_json(404, {"ok": False, "message": "Не найдено."})
            return
        if self.headers.get_content_type() != "application/json":
            self.send_json(415, {"ok": False, "message": "JSON is required."})
            return
        try:
            body_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(400, {"ok": False, "message": "Неверный запрос."})
            return
        if body_length <= 0 or body_length > MAX_LEAD_BODY_BYTES:
            self.send_json(413, {"ok": False, "message": "Запрос слишком большой."})
            return
        try:
            payload = json.loads(self.rfile.read(body_length))
            lead = validate_enterprise_lead(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_json(400, {"ok": False, "message": "Неверные данные формы."})
            return
        except ValueError as error:
            self.send_json(400, {"ok": False, "message": str(error)})
            return

        if isinstance(payload.get("website"), str) and payload["website"].strip():
            self.send_json(201, {"ok": True, "message": "Спасибо. Ваш запрос получен."})
            return

        try:
            save_enterprise_lead(*lead)
        except sqlite3.Error:
            logging.exception("Не удалось сохранить потенциального клиента.")
            self.send_json(500, {"ok": False, "message": "Не удалось сохранить ваш запрос. Пожалуйста, попробуйте еще раз."})
            return

        self.send_json(201, {"ok": True, "message": "Спасибо. Ваш запрос получен."})

    def log_message(self, format_string, *args):
        logging.info("Landing server: %s", format_string % args)

SECTOR_DETAILS_TEMPLATES = {
    "Сектор 1 (Высокая дисциплина)": {
        "desc": "{role_title} неукоснительно соблюдает установленные регламенты, инструкции и нормативные требования. Высокий уровень исполнительности.",
        "strengths": "Предсказуемость действий, соблюдение техники безопасности, точность.",
        "risks": "Возможен недостаток гибкости в нестандартных или экстренных ситуациях, требующих отступления от шаблона.",
        "rec": "{rec_word} к допуску на объекты с повышенным риском и строгим регламентным контролем.",
    },
    "Сектор 4 (Компромиссный профиль)": {
        "desc": "{role_title} {inclined_word} искать компромиссы между регламентом и текущей производственной необходимостью.",
        "strengths": "Адаптивность, ориентированность на результат в условиях жестких дедлайнов.",
        "risks": "Склонность к обходу второстепенных процедур ради ускорения процессов.",
        "rec": "{rec_word} к прохождению дополнительных инструкций по критическим точкам безопасности.",
    },
    "Сектор 7 (Зона повышенного внимания)": {
        "desc": "{role_title} систематически отдает приоритет неформальным договоренностям или действует в обход установленных процедур.",
        "strengths": "Высокая самостоятельность и инициативность.",
        "risks": "Высокий риск создания нештатных ситуаций, сокрытия мелких инцидентов и нарушения субординации.",
        "rec": "{rec_word} к самостоятельной работе только после детального собеседования со службой безопасности.",
    },
}

def get_sector_details(sector, gender):
    is_female = gender == "Женский"
    terms = {
        "role_title": "Сотрудница" if is_female else "Сотрудник",
        "rec_word": "Рекомендована" if is_female else "Рекомендован",
        "inclined_word": "склонна" if is_female else "склонен",
    }
    template = SECTOR_DETAILS_TEMPLATES.get(
        sector,
        {
            "desc": "Стандартный профиль поведенческих реакций.",
            "strengths": "Соответствие базовым требованиям.",
            "risks": "Требуется плановое наблюдение.",
            "rec": "{rec_word} к стандартному порядку допуска.",
        },
    )
    return {key: value.format(**terms) for key, value in template.items()}

SECTOR_RISK = {
    "Сектор 1 (Высокая дисциплина)": {"label": "НИЗКИЙ", "color": "#2E7D32"},
    "Сектор 4 (Компромиссный профиль)": {"label": "УМЕРЕННЫЙ", "color": "#B26A00"},
    "Сектор 7 (Зона повышенного внимания)": {"label": "ПОВЫШЕННЫЙ", "color": "#C62828"},
}

def generate_pdf_report(
    filename,
    username,
    gender,
    group,
    exp,
    sector,
    avg_lat,
    doubt_idx,
    latencies,
):
    regular_font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    bold_font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    if not os.path.isfile(regular_font_path) or not os.path.isfile(bold_font_path):
        raise FileNotFoundError("Для отображения русского текста в PDF-файлах требуются шрифты DejaVu Sans.")
    if "DejaVuSans" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("DejaVuSans", regular_font_path))
    if "DejaVuSans-Bold" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("DejaVuSans-Bold", bold_font_path))

    doc = SimpleDocTemplate(
        filename,
        pagesize=letter,
        rightMargin=36,
        leftMargin=36,
        topMargin=36,
        bottomMargin=36,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ControlVTitle",
        parent=styles["Heading1"],
        fontName="DejaVuSans-Bold",
        fontSize=18,
        leading=22,
        textColor=colors.HexColor("#1A2B4C"),
        alignment=1,
    )
    subtitle_style = ParagraphStyle(
        "ControlVSubtitle",
        parent=styles["Normal"],
        fontName="DejaVuSans",
        fontSize=10,
        leading=14,
        textColor=colors.HexColor("#555555"),
        alignment=1,
    )
    body_style = ParagraphStyle(
        "ControlVBody",
        parent=styles["Normal"],
        fontName="DejaVuSans",
        fontSize=11,
        leading=16,
        textColor=colors.HexColor("#222222"),
    )
    bold_style = ParagraphStyle(
        "ControlVBold", parent=body_style, fontName="DejaVuSans-Bold"
    )

    def paragraph(value, style):
        return Paragraph(html.escape(str(value)), style)

    details = get_sector_details(sector, gender)
    risk = SECTOR_RISK.get(sector, {"label": "ТРЕБУЕТ ОЦЕНКИ", "color": "#B26A00"})
    risk_color = colors.HexColor(risk["color"])
    doubt_is_low = doubt_idx <= 1.5
    doubt_desc = "Уверенная позиция" if doubt_is_low else "Высокий уровень колебаний / поиск ответа"
    doubt_text_color = "#2E7D32" if doubt_is_low else "#8A4B00"
    doubt_background = colors.HexColor("#E8F5E9") if doubt_is_low else colors.HexColor("#FFF3E0")
    
    doubt_value = Paragraph(
        f'<font color="{doubt_text_color}"><b>{html.escape(str(doubt_idx))} ({html.escape(doubt_desc)})</b></font>',
        body_style,
    )
    risk_value_style = ParagraphStyle(
        "ControlVRiskValue",
        parent=body_style,
        fontName="DejaVuSans-Bold",
        textColor=colors.white,
        alignment=1,
    )

    story = [
        paragraph("СЛУЖЕБНАЯ КАРТА СИТУАЦИОННОЙ АДАПТИВНОСТИ «КОНТРОЛЬ-В»", title_style),
        paragraph("Конфиденциальный отчет для ответственного специалиста", subtitle_style),
        Spacer(1, 15),
    ]

    table_data = [
        [paragraph("Параметр оценки", bold_style), paragraph("Значение / показатель", bold_style)],
        [paragraph("Участник", body_style), paragraph(username, body_style)],
        [paragraph("Пол", body_style), paragraph(gender or "Не указан", body_style)],
        [paragraph("Возрастная категория", body_style), paragraph(group, body_style)],
        [paragraph("Стаж в отрасли", body_style), paragraph(exp, body_style)],
        [
            paragraph("Итоговый сектор", body_style),
            Paragraph(f'<font color="{risk["color"]}"><b>{html.escape(str(sector))}</b></font>', body_style),
        ],
        [paragraph("Среднее время ответа", body_style), paragraph(f"{avg_lat} сек.", body_style)],
        [paragraph("Индекс сомнений", body_style), doubt_value],
        [paragraph("Индикатор риска", body_style), paragraph(risk["label"], risk_value_style)],
    ]

    report_table = Table(table_data, colWidths=[180, 320])
    report_table.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EAECEE")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#1A2B4C")),
            ("ALIGN", (0, 0), (-1, -1), "LEFT"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#BDC3C7")),
            ("BACKGROUND", (1, 7), (1, 7), doubt_background),
            ("BACKGROUND", (1, 8), (1, 8), risk_color),
        ])
    )

    story.extend([
        report_table,
        Spacer(1, 10),
        paragraph(
            "Цвета сектора: зеленый — низкий риск; янтарный — умеренный; красный — повышенное внимание. Индекс сомнений выделен по порогу 1,5.",
            subtitle_style,
        ),
        Spacer(1, 10),
        paragraph("Экспертная интерпретация профиля", title_style),
        Paragraph(f'<font name="DejaVuSans-Bold">Характеристика:</font> {html.escape(details["desc"])}', body_style),
        Spacer(1, 4),
        Paragraph(f'<font name="DejaVuSans-Bold">Сильные стороны:</font> {html.escape(details["strengths"])}', body_style),
        Spacer(1, 4),
        Paragraph(f'<font name="DejaVuSans-Bold">Зоны риска:</font> {html.escape(details["risks"])}', body_style),
        Spacer(1, 4),
        Paragraph(f'<font name="DejaVuSans-Bold">Рекомендация:</font> {html.escape(details["rec"])}', body_style),
        Spacer(1, 10),
        paragraph("Латентность по шагам (сек):", bold_style),
        paragraph(", ".join(f"{l}с" for l in latencies), body_style),
        Spacer(1, 15),
        paragraph(
            "Эта автоматическая интерпретация является вспомогательным средством и не должна быть единственным основанием для кадровых решений.",
            subtitle_style,
        ),
        Spacer(1, 8),
        paragraph("Документ создан автоматически системой «Контроль-В».", subtitle_style),
    ])

    doc.build(story)

# --- БАНК КЕЙСОВ: 20 СИТУАЦИЙ В 4 КАТЕГОРИЯХ ---
CASE_CATEGORIES = (
    "Безопасность",
    "Подчинение",
    "Этика",
    "Управление ресурсами",
)

CATEGORY_LABELS = {
    "Безопасность": "Безопасность",
    "Подчинение": "Подчинение",
    "Этика": "Этика",
    "Управление ресурсами": "Управление ресурсами",
}

QUESTION_POOL = [
    {
        "id": "SAF-01",
        "category": "Безопасность",
        "text": "Вы обнаружили отклонение от стандартного регламента на объекте. Ваши действия?",
        "options": [
            "1. Сообщу ответственному по установленному порядку",
            "2. Устраню самостоятельно, не привлекая внимания",
            "3. Отложу вопрос до плановой проверки"
        ]
    },
    {
        "id": "SAF-02",
        "category": "Безопасность",
        "text": "Обнаружен конфликт между двумя инструкциями на участке. Как поступить?",
        "options": [
            "1. Приостановлю операцию и запрошу официальное разъяснение",
            "2. Выберу удобный порядок и продолжу без согласования",
            "3. Сообщу руководителю смены и зафиксирую противоречие"
        ]
    },
    {
        "id": "SAF-03",
        "category": "Безопасность",
        "text": "На критическом узле повреждена пломба, хотя оборудование работает штатно. Ваши действия?",
        "options": [
            "1. Немедленно сообщу службе контроля и безопасности",
            "2. Самостоятельно заменю пломбу и не буду оформлять инцидент",
            "3. Ограничу доступ к узлу и дождусь ответственного специалиста"
        ]
    },
    {
        "id": "SAF-04",
        "category": "Безопасность",
        "text": "Коробки загромождают эвакуационный путь, но проход временно не используется. Ваши действия?",
        "options": [
            "1. Организую освобождение прохода и сообщу ответственному",
            "2. Переставлю коробку в другое место без фиксации",
            "3. Огражу участок и запрошу уполномоченного сотрудника"
        ]
    },
    {
        "id": "SAF-05",
        "category": "Безопасность",
        "text": "Перед началом работ вы заметили повреждения защитных перчаток. Ваши действия?",
        "options": [
            "1. Получу исправные средства защиты до начала работы",
            "2. Использую поврежденные перчатки, так как задача займет мало времени",
            "3. Приостановлю работу и сообщу о необходимости замены"
        ]
    },
    {
        "id": "SUB-01",
        "category": "Подчинение",
        "text": "Руководитель предлагает обойти один из этапов проверки для ускорения. Ваши действия?",
        "options": [
            "1. Соблюду инструкцию и сообщу о рисках руководителю",
            "2. Выполню просьбу, чтобы не срывать сроки",
            "3. Запрошу письменное подтверждение и уточню порядок"
        ]
    },
    {
        "id": "SUB-02",
        "category": "Подчинение",
        "text": "Два руководителя дают вам противоречащие друг другу срочные задания. Ваши действия?",
        "options": [
            "1. Сообщу обоим о конфликте и запрошу согласованный приоритет",
            "2. Выберу одно задание сам и не стану уведомлять второго",
            "3. Приостановлю задачи до официального согласования"
        ]
    },
    {
        "id": "SUB-03",
        "category": "Подчинение",
        "text": "Сотрудник из другого подразделения просит помочь ему в ущерб текущей задаче. Ваши действия?",
        "options": [
            "1. Согласую смену приоритетов со своим руководителем",
            "2. Помогу коллеге, не предупредив своего руководителя",
            "3. Направлю коллегу к профильному руководителю"
        ]
    },
    {
        "id": "SUB-04",
        "category": "Подчинение",
        "text": "Руководитель устно просит изменить последовательность работ в обход плана. Ваши действия?",
        "options": [
            "1. Запрошу официальное согласование изменений",
            "2. Изменю порядок на словах, чтобы продемонстрировать исполнительность",
            "3. Уточню у ответственного за планирование"
        ]
    },
    {
        "id": "SUB-05",
        "category": "Подчинение",
        "text": "В отсутствие руководителя коллега предлагает перераспределить обязанности. Ваши действия?",
        "options": [
            "1. Буду действовать строго по регламенту и свяжусь с дежурным",
            "2. Приму предложение коллеги без проверки полномочий",
            "3. Сохраню текущие задачи до возвращения руководства"
        ]
    },
    {
        "id": "ETH-01",
        "category": "Этика",
        "text": "Коллега допустил небольшое нарушение и просит не сообщать об этом проверяющим. Ваши действия?",
        "options": [
            "1. Уведомлю ответственное лицо согласно регламенту",
            "2. Скрою нарушение, чтобы защитить коллегу",
            "3. Предложу коллеге самому сообщить о случившемся"
        ]
    },
    {
        "id": "ETH-02",
        "category": "Этика",
        "text": "При смене смены обнаружилась неисправность, которую сменщик просит скрыть. Ваши действия?",
        "options": [
            "1. Зафиксирую неисправность в журнале приёма-передачи",
            "2. Договорюсь с коллегой скрыть факт до проверки",
            "3. Сообщу руководителю и согласую передачу смены"
        ]
    },
    {
        "id": "ETH-03",
        "category": "Этика",
        "text": "Поставщик предлагает небольшой лимитный подарок за ускоренное оформление. Ваши действия?",
        "options": [
            "1. Откажусь и сообщу о попытке по официальному каналу",
            "2. Приму подарок, если качество материалов нормальное",
            "3. Передам информацию в отдел комплаенса"
        ]
    },
    {
        "id": "ETH-04",
        "category": "Этика",
        "text": "Вас просят проставить задним числом отметку о выполнении регламента. Ваши действия?",
        "options": [
            "1. Откажусь и зафиксирую реальное время выполнения",
            "2. Проставлю дату ради общего отчёта подразделения",
            "3. Запрошу официальное распоряжение руководства"
        ]
    },
    {
        "id": "ETH-05",
        "category": "Этика",
        "text": "На рабочем месте оставлены чужие документы с конфиденциальными данными. Ваши действия?",
        "options": [
            "1. Передам их владельцу или ответственному лицу",
            "2. Посмотрю содержимое из любопытства",
            "3. Сообщу в службу безопасности и оставлю на месте"
        ]
    },
    {
        "id": "RES-01",
        "category": "Управление ресурсами",
        "text": "Поступила задача с неполными исходными данными и жестким сроком. Ваши действия?",
        "options": [
            "1. Запрошу недостающие сведения до начала работы",
            "2. Начну выполнять на своё усмотрение без фиксации допущений",
            "3. Выполню безопасную часть и запрошу уточнение"
        ]
    },
    {
        "id": "RES-02",
        "category": "Управление ресурсами",
        "text": "Соседний отдел просит временно передать оборудование без оформления акта. Ваши действия?",
        "options": [
            "1. Передам только после оформления учета и согласования",
            "2. Выдам под устное обещание вернуть",
            "3. Запрошу резолюцию ответственного за имущество"
        ]
    },
    {
        "id": "RES-03",
        "category": "Управление ресурсами",
        "text": "Осталась одна запасная деталь на два аварийных участка. Ваши действия?",
        "options": [
            "1. Сравню риски и согласую приоритет с руководством",
            "2. Отдам деталь первому обратившемуся знакомому",
            "3. Запрошу решение материально ответственного лица"
        ]
    },
    {
        "id": "RES-04",
        "category": "Управление ресурсами",
        "text": "Запас расходных материалов ограничен. Ваши действия?",
        "options": [
            "1. Учту остаток и своевременно оформлю заявку на пополнение",
            "2. Возьму с запасом «на всякий случай»",
            "3. Согласую нормы расхода с отделом снабжения"
        ]
    },
    {
        "id": "RES-05",
        "category": "Управление ресурсами",
        "text": "Плановое обслуживание совпало с важной производственной задачей. Ваши действия?",
        "options": [
            "1. Согласую скорректированный график с руководителями",
            "2. Самовольно отменю обслуживание",
            "3. Запрошу приоритеты у руководства"
        ]
    },
]

def select_test_cases():
    """Выбирает ровно 10 сценариев: по 2 из каждой категории + 2 случайных из остатка."""
    categories = {}
    for case in QUESTION_POOL:
        cat = case["category"]
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(case)

    selected = []
    for cat, items in categories.items():
        sampled = random.sample(items, min(2, len(items)))
        selected.extend(sampled)
        for item in sampled:
            items.remove(item)

    remaining = [item for items in categories.values() for item in items]
    needed = 10 - len(selected)
    if needed > 0 and remaining:
        selected.extend(random.sample(remaining, min(needed, len(remaining))))

    random.shuffle(selected)
    return selected[:10]

def format_case_prompt(index, case):
    category_label = CATEGORY_LABELS.get(case["category"], case["category"])
    return f"{index}/10. [{category_label}] {case['text']}"

def case_choices_with_scores(case):
    choices = []
    for option in case["options"]:
        match = re.match(r"^\s*(\d+)\.\s*(.*)$", option)
        if not match:
            raise ValueError(f"Неверный формат параметра в случае {case.get('id')}: {option!r}")
        choices.append({
            "label": match.group(2),
            "score": int(match.group(1)),
        })
    return choices

# --- СОСТОЯНИЯ ТЕСТА (FSM) ---
class AssessmentFSM(StatesGroup):
    s0_gender = State()
    s1_profile = State()
    s2_experience = State()
    answering_cases = State()

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

def is_admin(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id in ADMIN_USER_IDS)

def get_database_stats():
    conn = sqlite3.connect(DB_NAME)
    try:
        cursor = conn.cursor()
        total_results = cursor.execute("SELECT COUNT(*) FROM test_results").fetchone()[0]
        unique_users = cursor.execute("SELECT COUNT(DISTINCT user_id) FROM test_results").fetchone()[0]
        average_latency = cursor.execute("SELECT AVG(avg_latency) FROM test_results").fetchone()[0]
        average_doubt_index = cursor.execute("SELECT AVG(doubt_index) FROM test_results").fetchone()[0]
        sector_counts = cursor.execute("SELECT sector, COUNT(*) FROM test_results GROUP BY sector").fetchall()
        latest_result = cursor.execute("SELECT MAX(timestamp) FROM test_results").fetchone()[0]
        return total_results, unique_users, average_latency, average_doubt_index, sector_counts, latest_result
    finally:
        conn.close()

@dp.message(Command("admin"))
@dp.message(F.text == ADMIN_STATS_BUTTON)
async def admin_stats(message: Message):
    if not is_admin(message):
        await message.answer("Доступ к этой службе ограничен.")
        return
    total, users, avg_latency, avg_doubt_index, sectors, latest = get_database_stats()
    sector_summary = "\n".join(f"• {sec}: {cnt}" for sec, cnt in sectors) or "• Пока нет сохранённых результатов"
    avg_latency_text = f"{avg_latency:.2f} сек." if avg_latency is not None else "—"
    avg_doubt_text = f"{avg_doubt_index:.2f}" if avg_doubt_index is not None else "—"
    latest_text = latest or "—"
    await message.answer(
        f"Статистика SQLite:\n"
        f"• Всего завершённых тестов: {total}\n"
        f"• Уникальных участников: {users}\n"
        f"• Среднее время ответа: {avg_latency_text}\n"
        f"• Средний индекс: {avg_doubt_text}\n"
        f"• Последний результат: {latest_text}\n\n"
        f"Результаты по секторам:\n{sector_summary}",
        reply_markup=ADMIN_REPLY_KEYBOARD,
    )

@dp.message(Command("export"))
@dp.message(F.text == ADMIN_EXPORT_BUTTON)
async def export_database(message: Message):
    if not is_admin(message):
        await message.answer("Доступ к этой службе ограничен.")
        return
    if not os.path.isfile(DB_NAME):
        await message.answer("Файл базы данных не найден.", reply_markup=ADMIN_REPLY_KEYBOARD)
        return
    await message.answer_document(
        document=FSInputFile(DB_NAME, filename=DB_NAME),
        caption="Экспорт базы данных SQLite.",
        reply_markup=ADMIN_REPLY_KEYBOARD,
    )

def make_keyboard(options, is_admin_user=False):
    shuffled_options = list(options)
    random.shuffle(shuffled_options)
    buttons = [[KeyboardButton(text=opt)] for opt in shuffled_options]
    if is_admin_user:
        buttons.extend([
            [KeyboardButton(text=ADMIN_STATS_BUTTON)],
            [KeyboardButton(text=ADMIN_EXPORT_BUTTON)],
        ])
    return ReplyKeyboardMarkup(
        keyboard=buttons,
        resize_keyboard=True,
        one_time_keyboard=not is_admin_user,
        is_persistent=is_admin_user,
    )

@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    import time
    await state.update_data(start_time=time.time(), latencies=[], last_step_time=time.time())
    kb = make_keyboard(["👨 Мужской", "👩 Женский"], is_admin_user=is_admin(message))
    await message.answer("Выберите пол для корректного оформления отчета:", reply_markup=kb)
    await state.set_state(AssessmentFSM.s0_gender)

@dp.message(AssessmentFSM.s0_gender)
async def process_gender(message: Message, state: FSMContext):
    gender_options = {"👨 Мужской": "Мужской", "👩 Женский": "Женский"}
    gender = gender_options.get(message.text)
    if not gender:
        kb = make_keyboard(["👨 Мужской", "👩 Женский"], is_admin_user=is_admin(message))
        await message.answer("Пожалуйста, выберите один из вариантов:", reply_markup=kb)
        return
    import time
    data = await state.get_data()
    now = time.time()
    latencies = data.get("latencies", [])
    latencies.append(round(now - data.get("last_step_time", now), 2))
    await state.update_data(gender=gender, latencies=latencies, last_step_time=now)
    
    kb = make_keyboard(["Группа А (До 30 лет)", "Группа Б (31-45 лет)", "Группа В (46+ лет)"], is_admin_user=is_admin(message))
    await message.answer("Добро пожаловать в симулятор ситуационной адаптации «Контроль-В».\n\nВыберите вашу возрастную категорию:", reply_markup=kb)
    await state.set_state(AssessmentFSM.s1_profile)

@dp.message(AssessmentFSM.s1_profile)
async def process_profile(message: Message, state: FSMContext):
    import time
    data = await state.get_data()
    now = time.time()
    latencies = data.get('latencies', [])
    latencies.append(round(now - data.get('last_step_time', now), 2))
    await state.update_data(profile_group=message.text, last_step_time=now, latencies=latencies)
    
    kb = make_keyboard(["Менее 3 лет", "3-8 лет", "Более 8 лет"], is_admin_user=is_admin(message))
    await message.answer("Укажите ваш общий стаж работы в отрасли:", reply_markup=kb)
    await state.set_state(AssessmentFSM.s2_experience)

@dp.message(AssessmentFSM.s2_experience)
async def process_experience(message: Message, state: FSMContext):
    import time
    data = await state.get_data()
    now = time.time()
    latencies = data.get('latencies', [])
    latencies.append(round(now - data['last_step_time'], 2))
    
    selected_cases = select_test_cases()
    await state.update_data(
        experience=message.text,
        last_step_time=now,
        latencies=latencies,
        cases=selected_cases,
        case_index=0,
        user_answers=[]
    )
    
    first_case = selected_cases[0]
    choices = case_choices_with_scores(first_case)
    choice_scores = {choice["label"]: choice["score"] for choice in choices}
    await state.update_data(current_case_choice_scores=choice_scores)
    
    kb = make_keyboard([choice["label"] for choice in choices], is_admin_user=is_admin(message))
    await message.answer(format_case_prompt(1, first_case), reply_markup=kb)
    await state.set_state(AssessmentFSM.answering_cases)

@dp.message(AssessmentFSM.answering_cases)
async def process_cases(message: Message, state: FSMContext):
    data = await state.get_data()
    choice_scores = data.get("current_case_choice_scores", {})
    selected_score = choice_scores.get(message.text)
    if selected_score is None:
        await message.answer(
            "Пожалуйста, выберите один из предложенных вариантов:",
            reply_markup=make_keyboard(list(choice_scores), is_admin_user=is_admin(message)),
        )
        return
        
    import time
    now = time.time()
    latencies = data.get('latencies', [])
    latencies.append(round(now - data['last_step_time'], 2))
    answers = data.get('user_answers', [])
    answers.append(selected_score)
    
    cases = data.get('cases', [])
    case_index = data.get('case_index', 0) + 1
    
    if case_index < len(cases):
        await state.update_data(
            case_index=case_index,
            user_answers=answers,
            last_step_time=now,
            latencies=latencies
        )
        next_case = cases[case_index]
        choices = case_choices_with_scores(next_case)
        next_choice_scores = {choice["label"]: choice["score"] for choice in choices}
        await state.update_data(current_case_choice_scores=next_choice_scores)
        
        kb = make_keyboard([choice["label"] for choice in choices], is_admin_user=is_admin(message))
        await message.answer(format_case_prompt(case_index + 1, next_case), reply_markup=kb)
    else:
        sector = "Сектор 1 (Высокая дисциплина)"
        compromise_count = sum(1 for score in answers if score == 2)
        if compromise_count >= 3:
            sector = "Сектор 7 (Зона повышенного внимания)"
        elif compromise_count >= 1:
            sector = "Сектор 4 (Компромиссный профиль)"
            
        avg_latency = round(sum(latencies) / len(latencies), 2) if latencies else 0
        base_latencies = latencies[:3]
        case_latencies = latencies[3:]
        avg_base = sum(base_latencies) / len(base_latencies) if base_latencies else 1.0
        avg_case = sum(case_latencies) / len(case_latencies) if case_latencies else 1.0
        doubt_index = round(avg_case / avg_base if avg_base > 0 else 1.0, 2)
        
        user_disp_name = message.from_user.full_name or message.from_user.username or "Участник"
        user_username = message.from_user.username or "Не указано"
        user_gender = data.get("gender")
        
        save_result_to_db(
            user_id=message.from_user.id,
            username=user_username,
            gender=user_gender,
            profile_group=data.get('profile_group'),
            experience=data.get('experience'),
            sector=sector,
            avg_latency=avg_latency,
            doubt_index=doubt_index,
            latencies=latencies
        )
        
        admin_summary = (
            "НОВЫЙ РЕЗУЛЬТАТ ТЕСТИРОВАНИЯ\n\n"
            f"Участник: {user_disp_name} (@{user_username})\n"
            f"Пол: {user_gender or 'Не указан'}\n"
            f"Категория: {data.get('profile_group')}\n"
            f"Стаж: {data.get('experience')}\n"
            f"Итоговый сектор: {sector}\n"
            f"Среднее время ответа: {avg_latency} сек.\n"
            f"Индекс сомнений: {doubt_index}\n"
            f"Пройдено кейсов: {len(cases)}\n\n"
            "Экспертный PDF-отчёт приложен ниже."
        )
        user_confirmation = "Тестирование завершено. Результаты сохранены и переданы ответственному специалисту."
        
        pdf_filename = None
        try:
            with tempfile.NamedTemporaryFile(prefix="control_v_report_", suffix=".pdf", delete=False) as report_file:
                pdf_filename = report_file.name
            generate_pdf_report(
                pdf_filename,
                user_disp_name,
                user_gender,
                data.get('profile_group'),
                data.get('experience'),
                sector,
                avg_latency,
                doubt_index,
                latencies,
            )
        except Exception:
            logging.exception("Не удалось сгенерировать PDF-отчет")
            pdf_filename = None
            
        await message.answer(
            user_confirmation,
            reply_markup=ADMIN_REPLY_KEYBOARD if is_admin(message) else ReplyKeyboardRemove(),
        )
        
        for admin_id in sorted(ADMIN_USER_IDS):
            try:
                summary = admin_summary
                if not pdf_filename:
                    summary += "\n\nPDF сгенерировать не удалось."
                await bot.send_message(admin_id, summary, reply_markup=ADMIN_REPLY_KEYBOARD)
                if pdf_filename and os.path.isfile(pdf_filename):
                    await bot.send_document(
                        admin_id,
                        FSInputFile(pdf_filename, filename="Control-V-report.pdf"),
                        caption=f"Карта оценок: {user_disp_name}",
                    )
            except Exception:
                logging.exception("Не удалось переслать отчет администратору %s", admin_id)
                
        if pdf_filename and os.path.exists(pdf_filename):
            try:
                os.remove(pdf_filename)
            except OSError:
                pass
                
        await state.clear()

async def main():
    port = int(os.environ.get("PORT", "5000"))
    web_server = ThreadingHTTPServer(("0.0.0.0", port), LandingRequestHandler)
    web_thread = Thread(target=web_server.serve_forever, name="control-v-landing", daemon=True)
    web_thread.start()
    logging.info("Страница Control-V прослушивает порт %s", port)
    
    try:
        await dp.start_polling(bot)
    finally:
        await asyncio.to_thread(web_server.shutdown)
        web_server.server_close()
        web_thread.join(timeout=2)

if __name__ == "__main__":
    asyncio.run(main())
