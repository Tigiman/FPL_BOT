"""FPL Deadline Reminder — запуск из GitHub Actions.

Скрипт делает одну проверку и завершается. Запускается по расписанию
из GitHub Actions (.github/workflows/remind.yml). Состояние (какие
напоминания уже отправлены) хранится в state.json, который workflow
коммитит обратно в репозиторий.
"""
import os
import json
import logging
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import httpx

# ─── CONFIG ──────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
DRY_RUN = os.environ.get("DRY_RUN") == "1"  # печатать вместо отправки
# Сколько максимум ждать внутри одного запуска (лимит job в GitHub — 6 ч).
MAX_WAIT_HOURS = float(os.environ.get("MAX_WAIT_HOURS", "5"))
# За сколько часов до напоминания начинать ждать/перезапускаться, не
# надеясь на cron. Больше максимального разрыва между запусками cron (~10 ч).
CHAIN_HORIZON_HOURS = float(os.environ.get("CHAIN_HORIZON_HOURS", "36"))

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


# ─── STATE ───────────────────────────────────────────────────────────────────
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception as e:
            log.warning(f"Не удалось прочитать state file: {e}")
    return {}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


# ─── FPL API ─────────────────────────────────────────────────────────────────
def fetch_next_gw(client: httpx.Client) -> tuple[int, str, datetime] | None:
    r = client.get(FPL_API)
    r.raise_for_status()
    now = datetime.now(timezone.utc)
    for gw in r.json()["events"]:
        deadline = datetime.fromisoformat(gw["deadline_time"].replace("Z", "+00:00"))
        if not gw["finished"] and deadline > now:
            return gw["id"], gw["name"], deadline
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


def send_message(client: httpx.Client, text: str):
    if DRY_RUN:
        log.info(f"[DRY_RUN] сообщение:\n{text}")
        return
    r = client.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text},
    )
    r.raise_for_status()


# ─── ОСНОВНАЯ ПРОВЕРКА ───────────────────────────────────────────────────────
def check(client: httpx.Client, state: dict) -> datetime | None:
    """Одна проверка: шлёт наступившее напоминание. Возвращает время
    следующего неотправленного напоминания (или None)."""
    now = datetime.now(timezone.utc)

    # Раз в месяц state.json меняется даже без туров (межсезонье), чтобы
    # workflow сделал коммит — иначе GitHub отключает cron в репозитории
    # после 60 дней без активности.
    state["keepalive"] = now.strftime("%Y-%m")

    result = fetch_next_gw(client)
    if result is None:
        log.info("Активных туров не найдено.")
        save_state(state)
        return None
    gw_id, gw_name, deadline = result

    sent = {k: v for k, v in state.get("sent", {}).items() if k.startswith(f"{gw_id}:")}

    # Окна, которые уже наступили и ещё не отправлены. Если бот
    # пропустил несколько окон (например, запуск был за 1ч до дедлайна) —
    # шлём только самое свежее, остальные помечаем, чтобы не спамить.
    due = [h for h in REMIND_HOURS
           if not sent.get(f"{gw_id}:{h}") and now >= deadline - timedelta(hours=h)]
    if due:
        latest = min(due)
        send_message(client, format_message(gw_name, deadline))
        for notice in EXTRA_NOTICES:
            send_message(client, notice)
        for h in due:
            sent[f"{gw_id}:{h}"] = True
        log.info(f"Отправлено: {gw_name} за {latest} ч. (+ {len(EXTRA_NOTICES)} доп. уведомления)")

    state["sent"] = sent
    save_state(state)

    nxt = [deadline - timedelta(hours=h) for h in REMIND_HOURS if not sent.get(f"{gw_id}:{h}")]
    if not nxt:
        log.info(f"{gw_name}: все напоминания уже отправлены")
        return None
    log.info(f"{gw_name}: следующее напоминание в {min(nxt):%Y-%m-%d %H:%M} UTC")
    return min(nxt)


def run():
    """GitHub запускает cron нерегулярно (на практике раз в 2–10 ч), поэтому
    полагаться только на него нельзя. Если напоминание близко, запуск ждёт
    его сам (до MAX_WAIT_HOURS), а затем перезапускает workflow
    (CHAIN=true в GITHUB_OUTPUT), пока перед дедлайном остаются напоминания."""
    if not DRY_RUN and not (BOT_TOKEN and CHAT_ID):
        raise SystemExit("Не заданы BOT_TOKEN / CHAT_ID")

    state = load_state()
    chain = False
    with httpx.Client(timeout=15) as client:
        nxt = check(client, state)
        if nxt and nxt - datetime.now(timezone.utc) <= timedelta(hours=CHAIN_HORIZON_HOURS):
            wait = min(nxt - datetime.now(timezone.utc), timedelta(hours=MAX_WAIT_HOURS))
            wait_s = max(0, wait.total_seconds()) + 30
            log.info(f"Жду {wait_s / 3600:.2f} ч.")
            time.sleep(wait_s)
            nxt = check(client, state)
            chain = bool(nxt and nxt - datetime.now(timezone.utc)
                         <= timedelta(hours=CHAIN_HORIZON_HOURS))

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"chain={'true' if chain else 'false'}\n")
    log.info(f"Перезапуск цепочки: {chain}")


if __name__ == "__main__":
    run()
