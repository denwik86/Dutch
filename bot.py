"""
Dutch A2 Exam Coach — Telegram Bot (FIXED)
Powered by Claude API (claude-sonnet-4-20250514)
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

TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN", "")
ANTHROPIC_KEY   = os.getenv("ANTHROPIC_KEY",  "")
CHAT_ID         = int(os.getenv("CHAT_ID", "0"))

DATA_FILE = Path("progress.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
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
    "current_quiz": None,
    "conversation": [],
    "weekly_mock_done": False,
    "vocab_learned": 0,
}

def load_progress() -> dict:
    if DATA_FILE.exists():
        with open(DATA_FILE) as f:
            data = json.load(f)
        for k, v in DEFAULT_PROGRESS.items():
            if k not in data:
                data[k] = v
        return data
    return DEFAULT_PROGRESS.copy()

def save_progress(p: dict):
    with open(DATA_FILE, "w") as f:
        json.dump(p, f, ensure_ascii=False, indent=2)

def get_weak_topics(p: dict) -> list:
    weak = []
    for topic, stats in p["knm_topics"].items():
        total = stats["correct"] + stats["wrong"]
        if total > 0:
            if stats["correct"] / total < 0.7:
                weak.append(topic)
        elif stats["done"] == 0:
            weak.append(topic)
    return weak[:3]

def days_to_exam(p: dict) -> int:
    exam = date.fromisoformat(p["exam_date"])
    return max(0, (exam - date.today()).days)

def get_current_phase(p: dict) -> int:
    days_left = days_to_exam(p)
    if days_left > 84:   return 1
    elif days_left > 56: return 2
    elif days_left > 14: return 3
    else:                return 4

def today_topic(p: dict) -> str:
    topics = list(p["knm_topics"].keys())
    weak = get_weak_topics(p)
    if weak:
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
        weak_topics=", ".join(weak) if weak else "нет!",
        topic_today=today_topic(p),
        phase=p["phase"],
        days_left=days_to_exam(p),
    )

async def ask_claude(p: dict, user_message: str) -> str:
    messages = p.get("conversation", [])[-20:]
    messages = messages + [{"role": "user", "content": user_message}]
    try:
        resp = claude.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=800,
            system=make_system(p),
            messages=messages,
        )
        answer = resp.content[0].text
        p["conversation"].append({"role": "user", "content": user_message})
        p["conversation"].append({"role": "assistant", "content": answer})
        p["conversation"] = p["conversation"][-40:]
        return answer
    except Exception as e:
        log.error(f"Claude API error: {e}")
        return "⚠️ Ошибка соединения с Claude. Попробуй снова."


# ─── Запланированные задачи ───────────────────────────────────────────────────

async def send_morning_task(app: Application):
    p = load_progress()
    p["day"] += 1
    p["phase"] = get_current_phase(p)

    last = date.fromisoformat(p.get("last_active", str(date.today())))
    delta = (date.today() - last).days
    if delta == 1:
        p["streak"] += 1
    elif delta > 1:
        p["streak"] = 0
    p["last_active"] = str(date.today())
    save_progress(p)

    instruction = (
        f"Сгенерируй утреннее задание для дня {p['day']}.\n"
        f"Формат:\n"
        f"1. Приветствие с прогрессом (1 строка)\n"
        f"2. Задание — 3 пункта (Lezen, Luisteren, KNM) с ссылками\n"
        f"3. Слово дня (нидерландское + перевод + пример)\n"
        f"4. Мотивирующая фраза\n\n"
        f"Ссылки:\n"
        f"- Lezen: https://nos.nl\n"
        f"- Luisteren: https://learndutch.org/courses/listening/\n"
        f"- KNM: https://inburgering.org/knm\n"
        f"Тема KNM сегодня: {today_topic(p)}"
    )
    text = await ask_claude(p, instruction)
    save_progress(p)
    try:
        await app.bot.send_message(
            chat_id=CHAT_ID, text=text,
            parse_mode="Markdown", disable_web_page_preview=True
        )
        log.info(f"Morning task sent, day {p['day']}")
    except Exception as e:
        log.error(f"Morning task send error: {e}")


async def send_evening_quiz(app: Application):
    p = load_progress()
    topic = today_topic(p)

    instruction = (
        f"Задай 1 вопрос КНМ по теме \"{topic}\" для Telegram.\n"
        f"Формат СТРОГО:\n"
        f"❓ [Вопрос на нидерландском]\n"
        f"A) [вариант]\n"
        f"B) [вариант]\n"
        f"C) [вариант]\n"
        f"D) [вариант]\n"
        f"ANSWER:[правильная буква]\n"
        f"EXPLANATION:[объяснение на русском, 1-2 предложения]"
    )
    raw = await ask_claude(p, instruction)
    save_progress(p)

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

    p["current_quiz"] = {
        "topic": topic,
        "answer": answer_letter.upper(),
        "explanation": explanation,
        "answered": False,
    }
    save_progress(p)

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("A", callback_data="quiz_A"),
        InlineKeyboardButton("B", callback_data="quiz_B"),
        InlineKeyboardButton("C", callback_data="quiz_C"),
        InlineKeyboardButton("D", callback_data="quiz_D"),
    ]])

    try:
        await app.bot.send_message(
            chat_id=CHAT_ID,
            text=f"📚 *Вечерний квиз — тема: {topic}*\n\n{clean_text}",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        log.info(f"Evening quiz sent, topic: {topic}")
    except Exception as e:
        log.error(f"Evening quiz send error: {e}")


async def send_weekly_report(app: Application):
    p = load_progress()
    text = await ask_claude(p,
        "Составь краткий недельный отчёт прогресса (макс 200 слов): "
        "итоги KNM по темам, топ-2 слабых места, рекомендация на следующую неделю, мотивация."
    )
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
    text = await ask_claude(p,
        f"Поприветствуй ученика с нулевым уровнем нидерландского! "
        f"До экзамена {days_to_exam(p)} дней. "
        f"Объясни что ты умеешь и дай список команд: "
        f"/task, /quiz, /mock, /word, /progress, /help."
    )
    save_progress(p)
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_task(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    p = load_progress()
    text = await ask_claude(p,
        f"Дай задание на сегодня (тема KNM: {today_topic(p)}). Включи конкретные ссылки."
    )
    save_progress(p)
    await update.message.reply_text(text, parse_mode="Markdown",
                                    disable_web_page_preview=True)


async def cmd_quiz(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await send_evening_quiz(ctx.application)


async def cmd_progress(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    p = load_progress()
    days = days_to_exam(p)
    weak = get_weak_topics(p)
    phase_names = {1: "Диагностика", 2: "Практика", 3: "Имитация", 4: "Финал"}

    knm_lines = []
    for topic, stats in p["knm_topics"].items():
        total = stats["correct"] + stats["wrong"]
        if total > 0:
            pct = int(stats["correct"] / total * 100)
            bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
            knm_lines.append(f"`{topic[:8]:8}` {bar} {pct}%")
        else:
            knm_lines.append(f"`{topic[:8]:8}` {'░'*10} —")

    text = (
        f"📈 *Прогресс — День {p['day']}*\n"
        f"🎯 До экзамена: *{days} дней*\n"
        f"🔥 Серия: {p['streak']} дней\n"
        f"⭐ XP: {p['total_xp']}\n"
        f"📍 Фаза: {phase_names.get(p['phase'], '?')}\n\n"
        f"*KNM по темам:*\n" + "\n".join(knm_lines) + "\n\n"
        f"⚠️ Слабые темы: {', '.join(weak) if weak else 'нет!'}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_mock(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    p = load_progress()
    text = await ask_claude(p,
        "Проведи мини mock-экзамен A2 (5 минут): "
        "1) Короткий текст (3-4 предложения на нидерландском) + 2 вопроса. "
        "2) Описание аудио-ситуации + 1 вопрос. "
        "3) 3 KNM вопроса по разным темам. "
        "Скажи пользователю написать ответы (1a, 1b, 2, 3a, 3b, 3c)."
    )
    save_progress(p)
    await update.message.reply_text("🎓 *MINI MOCK EXAM*\n\n" + text,
                                    parse_mode="Markdown")


async def cmd_word(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    p = load_progress()
    text = await ask_claude(p,
        "Дай одно полезное нидерландское слово уровня A2: "
        "слово — перевод — пример предложения — когда используется."
    )
    p["vocab_learned"] += 1
    save_progress(p)
    await update.message.reply_text("📖 *Слово дня*\n\n" + text,
                                    parse_mode="Markdown")


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
        "💬 Или просто пиши — отвечу как коуч!\n\n"
        "⏰ *Автоматически:*\n"
        "08:00 — дневное задание\n"
        "19:00 — вечерний KNM квиз\n"
        "Вс 20:00 — недельный отчёт"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def handle_quiz_answer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
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
        result_text = f"✅ *Правильно!* +10 XP 🎉"
    else:
        stats["wrong"] += 1
        result_text = f"❌ *Неверно.* Правильный ответ: *{correct}*"

    stats["done"] += 1
    quiz["answered"] = True
    save_progress(p)

    explanation = quiz.get("explanation", "")
    total = stats["correct"] + stats["wrong"]
    full_text = (
        f"{result_text}\n\n"
        f"📝 {explanation}\n\n"
        f"Тема {topic}: {stats['correct']}/{total} верных"
    )
    await query.edit_message_text(full_text, parse_mode="Markdown")

    if user_answer == correct:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Ещё вопрос ▶", callback_data="next_quiz")
        ]])
        await ctx.bot.send_message(chat_id=CHAT_ID,
                                   text="Отлично! Хочешь ещё?",
                                   reply_markup=keyboard)


async def handle_next_quiz(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await send_evening_quiz(ctx.application)


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    if update.message.chat_id != CHAT_ID:
        return
    p = load_progress()
    text = await ask_claude(p, update.message.text)
    save_progress(p)
    await update.message.reply_text(text, parse_mode="Markdown",
                                    disable_web_page_preview=True)


# ─── Запуск — планировщик стартует ВНУТРИ event loop через post_init ─────────

async def post_init(app: Application):
    """Запускаем планировщик после старта event loop."""
    scheduler = AsyncIOScheduler(timezone="Europe/Amsterdam")

    scheduler.add_job(
        lambda: asyncio.ensure_future(send_morning_task(app)),
        "cron", hour=8, minute=0, id="morning"
    )
    scheduler.add_job(
        lambda: asyncio.ensure_future(send_evening_quiz(app)),
        "cron", hour=19, minute=0, id="evening"
    )
    scheduler.add_job(
        lambda: asyncio.ensure_future(send_weekly_report(app)),
        "cron", day_of_week="sun", hour=20, minute=0, id="weekly"
    )

    scheduler.start()
    log.info("Scheduler started: 08:00 task / 19:00 quiz / Sun 20:00 report")


def main():
    log.info("Starting Dutch A2 Coach bot...")

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("task",     cmd_task))
    app.add_handler(CommandHandler("quiz",     cmd_quiz))
    app.add_handler(CommandHandler("progress", cmd_progress))
    app.add_handler(CommandHandler("mock",     cmd_mock))
    app.add_handler(CommandHandler("word",     cmd_word))
    app.add_handler(CommandHandler("help",     cmd_help))

    app.add_handler(CallbackQueryHandler(handle_quiz_answer, pattern="^quiz_[ABCD]$"))
    app.add_handler(CallbackQueryHandler(handle_next_quiz,   pattern="^next_quiz$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
