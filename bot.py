import os
import json
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import httpx
from telegram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ─── CONFIG ──────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
STATE_FILE = os.environ.get("STATE_FILE", "/tmp/fpl_reminder_state.json")
RECHECK_INTERVAL_HOURS = int(os.environ.get("RECHECK_INTERVAL_HOURS", "6"))

REMIND_HOURS = [24, 2]

TIMEZONES = [
    ("Минск", "Europe/Minsk"),
    ("Варшава/Вильнюс", "Europe/Warsaw"),
    ("Ереван", "Asia/Yerevan"),
    ("Израиль", "Asia/Jerusalem"),
]

# Доп. уведомления, которые уходят отдельными сообщениями сразу после
# каждого классического напоминания (и за 24ч, и за 2ч). Без времени дедлайна —
# просто пинг конкретных людей.
EXTRA_NOTICES = [
    "@smager14 @adkavalchuk @v_nltsk @SurgeonWes напоминание про Challenge!",
    "@adkavalchuk @v_nltsk @SurgeonWes напоминание про Драфт!",
]

FPL_API = "https://fantasy.premierleague.com/api/bootstrap-static/"
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler:
    return _scheduler


# ─── STATE (персистентность — фикс бага "тихой пропажи" напоминания) ────────
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception as e:
            log.warning(f"Не удалось прочитать state file: {e}")
    return {}


def save_state(state: dict):
    try:
        d = os.path.dirname(STATE_FILE)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        log.warning(f"Не удалось сохранить state file: {e}")


# ─── FPL API ─────────────────────────────────────────────────────────────────
async def fetch_events() -> list[dict]:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(FPL_API)
        r.raise_for_status()
        return r.json()["events"]


async def fetch_next_gw() -> tuple[int, str, datetime] | None:
    events = await fetch_events()
    now = datetime.now(timezone.utc)
    for gw in events:
        deadline = datetime.fromisoformat(gw["deadline_time"].replace("Z", "+00:00"))
        if not gw["finished"] and deadline > now:
            return gw["id"], gw["name"], deadline
    return None


async def fetch_gw_deadline(gw_id: int) -> tuple[str, datetime] | None:
    """Свежие данные по конкретному туру — нужно перед отправкой,
    на случай если дедлайн перенесли после того, как job был запланирован."""
    events = await fetch_events()
    for gw in events:
        if gw["id"] == gw_id:
            deadline = datetime.fromisoformat(gw["deadline_time"].replace("Z", "+00:00"))
            return gw["name"], deadline
    return None


# ─── СООБЩЕНИЯ ───────────────────────────────────────────────────────────────
def format_message(gw_name: str, deadline: datetime) -> str:
    remaining = deadline - datetime.now(timezone.utc)
    hours_left = max(0, round(remaining.total_seconds() / 3600))
    lines = [f"⏰ Дедлайн {gw_name} — через {hours_left} ч.", ""]
    for name, tz_name in TIMEZONES:
        local_time = deadline.astimezone(ZoneInfo(tz_name))
        lines.append(f"{name}: {local_time.strftime('%H:%M, %d.%m')}")
    lines.append("")
    lines.append("Проверьте составы.")
    return "\n".join(lines)


async def send_and_mark(state: dict, key: str, gw_name: str, deadline: datetime):
    if state.get(key):
        return  # уже отправлено — защита от гонки scheduler vs. sync_reminders

    text = format_message(gw_name, deadline)
    await bot.send_message(chat_id=CHAT_ID, text=text)

    for notice in EXTRA_NOTICES:
        await bot.send_message(chat_id=CHAT_ID, text=notice)

    state[key] = True
    save_state(state)
    log.info(f"Отправлено: {key} (+ {len(EXTRA_NOTICES)} доп. уведомления)")


# ─── ТОЧНЫЙ JOB (аналог старого scheduler.add_job, но с перепроверкой) ──────
async def fire_scheduled(state: dict, gw_id: int, hours: int):
    key = f"{gw_id}:{hours}"
    if state.get(key):
        return

    fresh = await fetch_gw_deadline(gw_id)
    if fresh is None:
        return
    gw_name, deadline = fresh
    fire_at = deadline - timedelta(hours=hours)
    now = datetime.now(timezone.utc)

    if now >= fire_at:
        await send_and_mark(state, key, gw_name, deadline)
    else:
        # дедлайн перенесли позже — перепланируем job, а не молчим
        get_scheduler().add_job(
            fire_scheduled, trigger="date", run_date=fire_at,
            args=[state, gw_id, hours], id=key, replace_existing=True,
        )
        log.info(f"{gw_name}: дедлайн перенесён, job {key} переставлен на {fire_at}")


# ─── СИНХРОНИЗАЦИЯ (старт + каждые RECHECK_INTERVAL_HOURS) ──────────────────
async def sync_reminders(state: dict):
    """Ставит job'ы на точное время; а если окно уже наступило, пока бот
    не работал (простой/редеплой) — досылает сразу, а не теряет напоминание."""
    result = await fetch_next_gw()
    if result is None:
        log.info("Активных туров не найдено.")
        return
    gw_id, gw_name, deadline = result

    for k in list(state.keys()):
        if not k.startswith(f"{gw_id}:"):
            del state[k]

    now = datetime.now(timezone.utc)

    for hours in REMIND_HOURS:
        key = f"{gw_id}:{hours}"
        if state.get(key):
            continue

        fire_at = deadline - timedelta(hours=hours)

        if now >= deadline:
            state[key] = True  # дедлайн целиком прошёл, слать поздно
        elif now >= fire_at:
            await send_and_mark(state, key, gw_name, deadline)
        else:
            get_scheduler().add_job(
                fire_scheduled, trigger="date", run_date=fire_at,
                args=[state, gw_id, hours], id=key, replace_existing=True,
            )
            log.info(f"Запланировано: {gw_name} за {hours} ч. → {fire_at.strftime('%Y-%m-%d %H:%M UTC')}")

    save_state(state)


async def main():
    global _scheduler
    log.info("FPL Deadline Bot запущен")

    state = load_state()

    _scheduler = AsyncIOScheduler(timezone="UTC")
    _scheduler.start()

    await sync_reminders(state)
    _scheduler.add_job(sync_reminders, "interval", hours=RECHECK_INTERVAL_HOURS, args=[state])

    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
