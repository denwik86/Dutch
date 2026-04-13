"""
Dutch A2 Exam Coach — Telegram Bot
Powered by Claude API (claude-sonnet-4-20250514)

Запуск: python bot.py
"""

import os
import json
import asyncio
import logging
from datetime import datetime, date
from pathlib import Path

import anthropic
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ─── Настройки ───────────────────────────────────────────────────────────────

TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN", "ВСТАВЬ_ТОКЕН_СЮДА")
ANTHROPIC_KEY   = os.getenv("ANTHROPIC_KEY",  "ВСТАВЬ_ANTHROPIC_KEY_СЮДА")
CHAT_ID         = int(os.getenv("CHAT_ID",    "ВСТАВЬ_СВОЙ_CHAT_ID"))  # числовой ID

DATA_FILE = Path("progress.json")
LOG_FILE  = Path("coach.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ─── База данных прогресса ────────────────────────────────────────────────────

DEFAULT_PROGRESS = {
    "start_date": str(date.today()),
    "exam_date": "2026-08-01",
    "day": 0,
    "phase": 1,
    "total_xp": 0,
    "streak": 0,
    "last_active": str(date.today()),
    "knm_topics": {
        "Wonen":         {"done": 0, "correct": 0, "wrong": 0},
        "Zorg":          {"done": 0, "correct": 0, "wrong": 0},
        "Werk":          {"done": 0, "correct": 0, "wrong": 0},
        "Onderwijs":     {"done": 0, "correct": 0, "wrong": 0},
        "Overheid":      {"done": 0, "correct": 0, "wrong": 0},
        "Vervoer":       {"done": 0, "correct": 0, "wrong": 0},
        "Geld":          {"done": 0, "correct": 0, "wrong": 0},
        "Cultuur":       {"done": 0, "correct": 0, "wrong": 0},
    },
    "lezen_sessions": 0,
    "luisteren_sessions": 0,
    "current_quiz": None,   # активный вопрос квиза
    "conversation": [],     # история разговора с Claude
    "weekly_mock_done": False,
    "vocab_learned": 0,
}

def load_progress() -> dict:
    if DATA_FILE.exists():
        with open(DATA_FILE) as f:
            data = json.load(f)
        # добавляем новые поля если апдейт
        for k, v in DEFAULT_PROGRESS.items():
            if k not in data:
                data[k] = v
        return data
    return DEFAULT_PROGRESS.copy()

def save_progress(p: dict):
    with open(DATA_FILE, "w") as f:
        json.dump(p, f, ensure_ascii=False, indent=2)

def get_weak_topics(p: dict) -> list[str]:
    weak = []
    for topic, stats in p["knm_topics"].items():
        total = stats["correct"] + stats["wrong"]
        if total > 0:
            acc = stats["correct"] / total
            if acc < 0.7:
                weak.append(topic)
        elif stats["done"] == 0:
            weak.append(topic)
    return weak[:3]

def days_to_exam(p: dict) -> int:
    exam = date.fromisoformat(p["exam_date"])
    return max(0, (exam - date.today()).days)

def get_current_phase(p: dict) -> int:
    days_left = days_to_exam(p)
    if days_left > 84:   return 1   # Диагностика и основы
    elif days_left > 56: return 2   # Активная практика
    elif days_left > 14: return 3   # Имитация экзамена
    else:                return 4   # Финальная шлифовка

def today_topic(p: dict) -> str:
    """Выбирает тему KNM на сегодня по ротации."""
    topics = list(p["knm_topics"].keys())
    weak = get_weak_topics(p)
    if weak:
        # возвращаемся к слабым темам чаще
        return weak[p["day"] % len(weak)]
    return topics[p["day"] % len(topics)]


# ─── Claude API ───────────────────────────────────────────────────────────────

claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

SYSTEM_PROMPT = """Ты — личный коуч по нидерландскому языку для Виктара.
Виктар готовится к экзамену инбургеринг A2 (Lezen, Luisteren, KNM).
Дата экзамена: август 2026. Начальный уровень: НОЛЬ.

ПРАВИЛА ОБЩЕНИЯ:
- Отвечай на русском языке (нидерландские слова/примеры — на нидерландском)
- Сообщения короткие, для Telegram (до 400 символов, только если нужно длиннее)
- Используй эмодзи умеренно для живости
- Будь дружелюбным, как настоящий коуч — поддерживай, но требуй

ТВОИ ФУНКЦИИ:
1. Каждое утро — дневное задание (15 мин чтение + 15 мин аудио + 15 мин KNM)
2. Вечером — KNM квиз (10 вопросов по теме дня)
3. Анализ ошибок и адаптация плана
4. Ответы на вопросы по голландскому языку и культуре
5. Мотивация при пропусках

СТРУКТУРА ЭКЗАМЕНА A2:
- Lezen (чтение): короткие тексты, объявления, письма — с вопросами
- Luisteren (аудирование): диалоги, объявления — один раз, с вопросами
- KNM: 40 вопросов с картинками за 45 мин (нужно 26/40 для сдачи)
  Темы KNM: Wonen, Zorg, Werk, Onderwijs, Overheid, Vervoer, Geld, Cultuur

УРОВЕНЬ A2 — ЧТО НУЖНО ЗНАТЬ:
- Словарный запас: ~1200 слов
- Простые предложения, настоящее и прошедшее время
- Понимание повседневных текстов и разговоров

Текущий прогресс: {progress_summary}
Слабые темы KNM: {weak_topics}
Тема дня: {topic_today}
Фаза обучения: {phase}
Дней до экзамена: {days_left}
"""

def make_system(p: dict) -> str:
    weak = get_weak_topics(p)
    knm_summary = ", ".join(
        f"{t}: {s['correct']}/{s['correct']+s['wrong']}"
        for t, s in p["knm_topics"].items()
        if s["correct"] + s["wrong"] > 0
    ) or "ещё не начали"

    return SYSTEM_PROMPT.format(
        progress_summary=f"День {p['day']}, Серия {p['streak']} дней, XP={p['total_xp']}, KNM: {knm_summary}",
        weak_topics=", ".join(weak) if weak else "нет (хорошая работа!)",
        topic_today=today_topic(p),
        phase=p["phase"],
        days_left=days_to_exam(p),
    )

async def ask_claude(p: dict, user_message: str, temp_instruction: str = "") -> str:
    """Основной вызов Claude с сохранением истории."""
    messages = p.get("conversation", [])[-20:]  # последние 20 сообщений

    if temp_instruction:
        messages = messages + [{"role": "user", "content": temp_instruction + "\n\n" + user_message}]
    else:
        messages = messages + [{"role": "user", "content": user_message}]

    try:
        resp = claude.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=800,
            system=make_system(p),
            messages=messages,
        )
        answer = resp.content[0].text

        # сохраняем историю (только последние 40)
        p["conversation"].append({"role": "user", "content": user_message})
        p["conversation"].append({"role": "assistant", "content": answer})
        p["conversation"] = p["conversation"][-40:]

        return answer
    except Exception as e:
        log.error(f"Claude API error: {e}")
        return "⚠️ Ошибка соединения с Claude. Попробуй снова через минуту."


# ─── Запланированные задачи ───────────────────────────────────────────────────

async def send_morning_task(app: Application):
    """08:00 — дневное задание."""
    p = load_progress()
    p["day"] += 1
    p["phase"] = get_current_phase(p)

    # проверяем серию
    last = date.fromisoformat(p.get("last_active", str(date.today())))
    delta = (date.today() - last).days
    if delta == 1:
        p["streak"] += 1
    elif delta > 1:
        p["streak"] = 0
    p["last_active"] = str(date.today())
    p["weekly_mock_done"] = False if p["day"] % 7 == 1 else p["weekly_mock_done"]

    save_progress(p)

    instruction = f"""Сгенерируй утреннее задание для дня {p['day']}.
Формат:
1. Приветствие с прогрессом (1 строка)
2. Задание на сегодня — 3 пункта (Lezen, Luisteren, KNM) с конкретными ссылками на ресурсы
3. Слово дня (1 нидерландское слово с переводом и примером)
4. Мотивирующая фраза (1 строка)

Ссылки для заданий:
- Lezen: https://nos.nl (читать новость), https://www.inburgeren.nl (практика)
- Luisteren: https://www.nporadio1.nl или https://learndutch.org/courses/listening/
- KNM: https://inburgering.org/knm или https://www.inburgeringexam.nl/academy/knm/
Тема KNM сегодня: {today_topic(p)}"""

    text = await ask_claude(p, instruction)
    save_progress(p)

    try:
        await app.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="Markdown",
                                   disable_web_page_preview=True)
        log.info(f"Morning task sent, day {p['day']}")
    except Exception as e:
        log.error(f"Failed to send morning task: {e}")


async def send_evening_quiz(app: Application):
    """19:00 — KNM квиз."""
    p = load_progress()
    topic = today_topic(p)

    instruction = f"""Задай 1 вопрос КНМ по теме "{topic}" в формате для Telegram.
Формат СТРОГО такой:
❓ [Вопрос на нидерландском]
A) [вариант]
B) [вариант]  
C) [вариант]
D) [вариант]
ANSWER:[правильная буква]
EXPLANATION:[объяснение на русском, 1-2 предложения]

Вопрос должен быть реалистичным, как на настоящем экзамене A2."""

    raw = await ask_claude(p, instruction)
    save_progress(p)

    # парсим ответ и правильный вариант
    answer_letter = "A"
    explanation = ""
    clean_text = raw

    if "ANSWER:" in raw:
        parts = raw.split("ANSWER:")
        clean_text = parts[0].strip()
        rest = parts[1].strip() if len(parts) > 1 else ""
        if "EXPLANATION:" in rest:
            exp_parts = rest.split("EXPLANATION:")
            answer_letter = exp_parts[0].strip()
            explanation = exp_parts[1].strip() if len(exp_parts) > 1 else ""
        else:
            answer_letter = rest.split("\n")[0].strip()

    # сохраняем активный квиз
    p["current_quiz"] = {
        "topic": topic,
        "answer": answer_letter.upper(),
        "explanation": explanation,
        "answered": False,
    }
    save_progress(p)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("A", callback_data="quiz_A"),
            InlineKeyboardButton("B", callback_data="quiz_B"),
            InlineKeyboardButton("C", callback_data="quiz_C"),
            InlineKeyboardButton("D", callback_data="quiz_D"),
        ]
    ])

    header = f"📚 *Вечерний квиз — тема: {topic}*\n\n"
    try:
        await app.bot.send_message(
            chat_id=CHAT_ID,
            text=header + clean_text,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        log.info(f"Evening quiz sent, topic: {topic}")
    except Exception as e:
        log.error(f"Failed to send quiz: {e}")


async def send_weekly_report(app: Application):
    """Воскресенье 20:00 — недельный отчёт."""
    p = load_progress()
    instruction = """Составь недельный отчёт прогресса.
Включи:
1. Итоги недели (статистика KNM по темам)
2. Топ-2 слабых места — что повторить
3. Рекомендация на следующую неделю
4. Мотивационный итог

Формат: короткий, 200 слов макс."""

    text = await ask_claude(p, instruction)
    save_progress(p)

    try:
        await app.bot.send_message(
            chat_id=CHAT_ID,
            text="📊 *НЕДЕЛЬНЫЙ ОТЧЁТ*\n\n" + text,
            parse_mode="Markdown",
        )
    except Exception as e:
        log.error(f"Weekly report error: {e}")


# ─── Обработчики Telegram ─────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    p = load_progress()
    days = days_to_exam(p)
    text = await ask_claude(p,
        f"Поприветствуй нового ученика! Он начинает с нулевого уровня нидерландского. "
        f"До экзамена {days} дней. Объясни что ты умеешь и как работать с тобой. "
        f"Дай команды: /task (задание), /quiz (квиз), /progress (прогресс), /help (помощь). "
        f"Будь воодушевляющим!"
    )
    save_progress(p)
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_task(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Ручной запрос задания."""
    p = load_progress()
    instruction = f"Дай задание на сегодня (тема KNM: {today_topic(p)}). Включи конкретные ссылки."
    text = await ask_claude(p, instruction)
    save_progress(p)
    await update.message.reply_text(text, parse_mode="Markdown", disable_web_page_preview=True)


async def cmd_quiz(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Ручной запрос квиза."""
    await send_evening_quiz(ctx.application)


async def cmd_progress(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    p = load_progress()
    days = days_to_exam(p)
    weak = get_weak_topics(p)
    phase_names = {1: "Диагностика и основы", 2: "Активная практика",
                   3: "Имитация экзамена", 4: "Финальная шлифовка"}

    knm_lines = []
    for topic, stats in p["knm_topics"].items():
        total = stats["correct"] + stats["wrong"]
        if total > 0:
            pct = int(stats["correct"] / total * 100)
            bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
            knm_lines.append(f"{topic[:8]:8} {bar} {pct}%")
        else:
            knm_lines.append(f"{topic[:8]:8} {'░'*10} не начато")

    text = (
        f"📈 *Прогресс — День {p['day']}*\n"
        f"🎯 До экзамена: **{days} дней**\n"
        f"🔥 Серия: {p['streak']} дней\n"
        f"⭐ XP: {p['total_xp']}\n"
        f"📍 Фаза: {phase_names.get(p['phase'], '?')}\n\n"
        f"*KNM по темам:*\n```\n" + "\n".join(knm_lines) + "\n```\n"
        f"\n⚠️ Слабые темы: {', '.join(weak) if weak else 'нет!'}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_mock(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Запрос mock-экзамена."""
    p = load_progress()
    instruction = """Проведи короткий mock-экзамен A2 (мини-версия, 5 минут).
Дай:
1. Один короткий текст для чтения (3-4 предложения на нидерландском) + 2 вопроса
2. Описание аудио-ситуации + 1 вопрос (без реального аудио)  
3. 3 KNM вопроса по разным темам

Скажи пользователю написать ответы (1a, 1b, 2, 3a, 3b, 3c)."""
    text = await ask_claude(p, instruction)
    save_progress(p)
    await update.message.reply_text("🎓 *MINI MOCK EXAM*\n\n" + text, parse_mode="Markdown")


async def cmd_word(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Слово дня."""
    p = load_progress()
    text = await ask_claude(p,
        "Дай одно полезное нидерландское слово уровня A2. "
        "Формат: слово — перевод — пример предложения — контекст когда используется."
    )
    p["vocab_learned"] += 1
    save_progress(p)
    await update.message.reply_text("📖 *Слово дня*\n\n" + text, parse_mode="Markdown")


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "🇳🇱 *Dutch A2 Coach — Команды*\n\n"
        "/start — начало работы\n"
        "/task — задание на сегодня\n"
        "/quiz — KNM квиз\n"
        "/mock — мини mock-экзамен\n"
        "/word — слово дня\n"
        "/progress — твой прогресс\n"
        "/help — эта справка\n\n"
        "💬 Или просто пиши мне — я отвечу как коуч!\n\n"
        "⏰ *Автоматически:*\n"
        "08:00 — дневное задание\n"
        "19:00 — вечерний KNM квиз\n"
        "Вс 20:00 — недельный отчёт"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def handle_quiz_answer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Обработка ответа на квиз (кнопки A/B/C/D)."""
    query = update.callback_query
    await query.answer()

    p = load_progress()
    quiz = p.get("current_quiz")

    if not quiz or quiz.get("answered"):
        await query.edit_message_text("❌ Нет активного вопроса. Используй /quiz")
        return

    user_answer = query.data.replace("quiz_", "").upper()
    correct = quiz["answer"].upper()
    topic = quiz["topic"]
    stats = p["knm_topics"][topic]

    if user_answer == correct:
        stats["correct"] += 1
        p["total_xp"] += 10
        result_icon = "✅"
        result_text = f"*Правильно!* +10 XP 🎉"
    else:
        stats["wrong"] += 1
        result_icon = "❌"
        result_text = f"*Неверно.* Правильный ответ: **{correct}**"

    stats["done"] += 1
    quiz["answered"] = True
    save_progress(p)

    explanation = quiz.get("explanation", "")
    full_text = (
        f"{result_icon} {result_text}\n\n"
        f"📝 {explanation}\n\n"
        f"Тема {topic}: {stats['correct']}/{stats['correct']+stats['wrong']} верных"
    )

    await query.edit_message_text(full_text, parse_mode="Markdown")

    # если правильно — предлагаем следующий вопрос
    if user_answer == correct:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Ещё вопрос ▶", callback_data="next_quiz")
        ]])
        await ctx.bot.send_message(chat_id=CHAT_ID,
                                   text="Отлично! Хочешь ещё вопрос?",
                                   reply_markup=keyboard)


async def handle_next_quiz(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Кнопка 'Ещё вопрос'."""
    query = update.callback_query
    await query.answer()
    await send_evening_quiz(ctx.application)


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Свободный чат с коучем."""
    if not update.message or not update.message.text:
        return

    # только от своего chat_id
    if update.message.chat_id != CHAT_ID:
        return

    p = load_progress()
    text = await ask_claude(p, update.message.text)
    save_progress(p)

    await update.message.reply_text(text, parse_mode="Markdown",
                                    disable_web_page_preview=True)


# ─── Запуск ───────────────────────────────────────────────────────────────────

def main():
    log.info("Starting Dutch A2 Coach bot...")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("task",     cmd_task))
    app.add_handler(CommandHandler("quiz",     cmd_quiz))
    app.add_handler(CommandHandler("progress", cmd_progress))
    app.add_handler(CommandHandler("mock",     cmd_mock))
    app.add_handler(CommandHandler("word",     cmd_word))
    app.add_handler(CommandHandler("help",     cmd_help))

    # Кнопки квиза
    app.add_handler(CallbackQueryHandler(handle_quiz_answer, pattern="^quiz_[ABCD]$"))
    app.add_handler(CallbackQueryHandler(handle_next_quiz,   pattern="^next_quiz$"))

    # Свободный чат
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Планировщик
    scheduler = AsyncIOScheduler(timezone="Europe/Amsterdam")

    scheduler.add_job(
        lambda: asyncio.ensure_future(send_morning_task(app)),
        "cron", hour=8, minute=0,
        id="morning_task"
    )
    scheduler.add_job(
        lambda: asyncio.ensure_future(send_evening_quiz(app)),
        "cron", hour=19, minute=0,
        id="evening_quiz"
    )
    scheduler.add_job(
        lambda: asyncio.ensure_future(send_weekly_report(app)),
        "cron", day_of_week="sun", hour=20, minute=0,
        id="weekly_report"
    )

    scheduler.start()
    log.info("Scheduler started. Morning: 08:00, Evening quiz: 19:00, Weekly: Sun 20:00")

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
