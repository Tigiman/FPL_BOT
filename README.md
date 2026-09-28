# FPL Deadline Reminder Bot

Шлёт в Telegram-группу напоминание о дедлайне FPL за 24 ч и за 2 ч (время в 4 часовых поясах) + доп. пинги про Challenge и Драфт.

## Как работает

Бот хостится бесплатно на **GitHub Actions** (`.github/workflows/remind.yml`):

- каждые ~15 минут запускается `python bot.py`, делает одну проверку FPL API и завершается;
- если окно напоминания наступило и ещё не отправлено — шлёт сообщение;
- если пропущено несколько окон сразу — шлёт только самое свежее;
- отправленное отмечается в `state.json`, который workflow коммитит обратно в репозиторий;
- раз в месяц `state.json` обновляется и в межсезонье, чтобы GitHub не отключил расписание за 60 дней неактивности.

GitHub может задерживать запуски по расписанию на 5–20 минут — для напоминаний за 24 ч / 2 ч это не критично.

## Настройка

1. **Settings → Secrets and variables → Actions → New repository secret**:
   - `BOT_TOKEN` — токен от @BotFather
   - `CHAT_ID` — id группы (отрицательное число)
2. **Actions** → workflow «FPL deadline reminder» → **Run workflow** — ручная проверка.

## Локальный прогон без отправки

```bash
pip install -r requirements.txt
DRY_RUN=1 python bot.py
```

## Что можно поменять (в `bot.py`)

- `TIMEZONES` — список поясов.
- `REMIND_HOURS` — за сколько часов напоминать (сейчас 24 и 2).
- `EXTRA_NOTICES` — доп. пинги.
- Текст сообщения — `format_message`.
- Частота проверок — `cron` в `.github/workflows/remind.yml`.
