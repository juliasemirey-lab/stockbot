# Как собрать бесплатно

Главное: недельному дайджесту **не нужен сервер 24/7**. Нужен запуск раз в
неделю — 30 секунд работы. Это можно сделать бесплатно, и вариантов несколько.

| Вариант | Стоимость | Что умеет | Ограничение |
|---|---|---|---|
| **GitHub Actions** | 0 ₽ | Только рассылка по расписанию | Репозиторий должен быть **публичным** |
| **Oracle Cloud Always Free** | 0 ₽ | Полный бот: `/digest`, `/order` | Нужна карта для верификации |
| **Fly.io / Koyeb** | 0 ₽ | Полный бот | Засыпает без трафика, нужен healthcheck |
| **Raspberry Pi / старый ПК** | 0 ₽ | Полный бот | Должен быть включён |
| **VPS** | 200–400 ₽/мес | Полный бот | Не бесплатно, но без сюрпризов |

## Вариант 1 — GitHub Actions (самый простой, 0 ₽)

Подходит, если нужно **только еженедельное сообщение**. Команды `/digest` и
`/order` в этом режиме не работают: бот не живёт между запусками.

### Ловушка, на которой все спотыкаются

Плановые запуски (`cron`) в **приватном** репозитории на бесплатном плане
GitHub не срабатывают. Молча — просто ничего не происходит. Нужно одно из двух:

- сделать репозиторий **публичным** (код бота ничем не выдаёт ваш бизнес,
  токены хранятся отдельно в Secrets);
- либо платить за GitHub Pro (~$4/мес) — уже не бесплатно.

В публичном репозитории минуты Actions не ограничены.

### Шаги

1. Создать публичный репозиторий, залить код:

   ```bash
   git init && git add . && git commit -m "stockbot"
   git remote add origin https://github.com/ВАШ_ЛОГИН/stockbot.git
   git push -u origin main
   ```

2. Secrets: **Settings → Secrets and variables → Actions → New repository secret**

   | Имя | Значение |
   |---|---|
   | `MOYSKLAD_TOKEN` | токен из Настроек МойСклад |
   | `WB_TOKEN` | токен WB, категория «Контент» |
   | `TELEGRAM_BOT_TOKEN` | от @BotFather |
   | `TELEGRAM_CHAT_IDS` | ваш chat_id, можно несколько через запятую |

3. Проверить вручную: **Actions → weekly-digest → Run workflow**.
   Должно прийти сообщение. Если пришло — расписание сработает само.

4. Расписание уже в `.github/workflows/digest.yml`:

   ```yaml
   - cron: "0 7 * * 3"     # среда 07:00 UTC = 10:00 МСК
   ```

   GitHub cron работает в **UTC**, Москва — UTC+3 круглый год.

### Что важно знать про этот вариант

- Запуск может задержаться на 10–30 минут при высокой нагрузке у GitHub.
  Для недельного дайджеста это неважно.
- Если 60 дней в репозитории нет коммитов, GitHub отключает расписание и
  присылает письмо. Лечится кнопкой «Enable workflow».
- CSV сохраняется артефактом в разделе Actions — можно скачать за любой месяц.
- Файл `.github/workflows/digest.yml` должен быть именно в ветке `main`,
  иначе расписание не запустится.

## Вариант 2 — Oracle Cloud Always Free

Полноценный бот: и рассылка, и команды в любой момент.

```bash
# на инстансе: Ubuntu, ARM Ampere A1 (бесплатный тир)
sudo apt update && sudo apt install -y python3-venv git
git clone https://github.com/ВАШ_ЛОГИН/stockbot.git && cd stockbot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export MOYSKLAD_TOKEN='...'
export WB_TOKEN='...'
export TELEGRAM_BOT_TOKEN='...'
export TELEGRAM_CHAT_IDS='...'
python main.py run
```

Чтобы жил после выхода из SSH — systemd:

```ini
# /etc/systemd/system/stockbot.service
[Unit]
Description=stockbot
After=network-online.target

[Service]
WorkingDirectory=/home/ubuntu/stockbot
EnvironmentFile=/home/ubuntu/stockbot/.env
ExecStart=/home/ubuntu/stockbot/.venv/bin/python main.py run
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now stockbot
journalctl -u stockbot -f
```

Токены в `.env` (права `chmod 600`), не в коде и не в git.

Бесплатный тир Oracle даёт достаточно (2 CPU / 12 ГБ RAM на ARM после
изменения лимитов). Для бота это с большим запасом. Нужна банковская карта
для верификации — списаний не будет, но карту иногда блокируют при регистрации
из России. Альтернатива — Fly.io или Koyeb, там та же схема.

## Вариант 3 — свой компьютер

Если есть постоянно включённый ПК или Raspberry Pi:

```bash
python main.py run          # бот + расписание
```

Или только рассылка без бота, через системный cron:

```cron
0 10 * * 3 cd /home/user/stockbot && .venv/bin/python main.py once >> bot.log 2>&1
```

Это полностью бесплатно и не зависит от чужих сервисов. Минус — компьютер
должен быть включён в среду в 10:00.

## Что выбрать

- Нужна только рассылка по средам → **GitHub Actions**, 15 минут на настройку.
- Нужны команды `/digest` и `/order` в любой момент → **Oracle/Fly** или
  свой постоянно включённый ПК.

Можно и совместить: расписание на GitHub Actions, а `/order` запускать
локально, когда нужно сформировать закупку.

## Что бесплатно не получится

- **МойСклад API** — токен доступен на любом тарифе, но сама подписка
  МойСклад платная. Если она уже есть — API бесплатен.
- **WB Content API** — бесплатен, но нужен действующий кабинет продавца.
- **Telegram Bot API** — полностью бесплатен, лимиты щедрые.

## Расходы на этом варианте — ноль

Дайджест на 3000 SKU считается 0.14 секунды. Узкое место — запросы к API
МойСклад (пагинация по 1000 строк) и выгрузка карточек WB, но и они
укладываются в минуту. GitHub Actions даёт 2000 минут в месяц на приватные
репозитории и неограниченно на публичные — одного запуска в неделю хватит
с запасом на годы.
