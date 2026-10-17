# Polymarket Underdog Bot (100x+)

Telegram-бот на aiogram 3.x. Он сам сканирует Polymarket и ищет исходы с ценой ≤ $0.01, то есть с коэффициентом от 100x. Берутся только рынки, которые разрешатся в окне от 15 минут до 24 часов. Бот следит, чтобы одновременно было открыто не больше 10 сделок по $1. Он рассылает сигналы со ссылками на Phantom и Polymarket, а при желании сам исполняет ордера через `py-clob-client`.

## Как это работает

1. Раз в `SCAN_INTERVAL_SEC` бот проверяет, есть ли свободные слоты. Если все 10 заняты, сканирование не запускается и сигналы не отправляются.
2. **Gamma API** (`/events`) отдаёт активные события, которые заканчиваются в нужном окне. Рынки с `closed`, `archived`, `acceptingOrders=false`, `enableOrderBook=false` отбрасываются, как и рынки, по которым уже был сигнал.
3. **CLOB API** (`POST /books`): бот загружает стаканы кандидатов и проверяет, что на стороне Ask по цене ≤ `MAX_PRICE` хватает объёма, чтобы целиком исполнить ордер на $1. По стакану считается средняя цена входа и итоговое количество акций.
4. Под сделку резервируется слот в SQLite. Проверка лимита и вставка выполняются атомарно под `asyncio.Lock`, а уникальный индекс не даёт открыть две позиции по одному токену.
5. Дальше бот либо отправляет сигнал, либо исполняет ордер в авто-режиме.
6. Раз в `RESOLUTION_CHECK_INTERVAL_SEC` чекер резолюций опрашивает CLOB `/markets/{condition_id}` (поле `winner`), а запасным источником служит Gamma `/markets?condition_ids=`. Разрешённые позиции переводятся в `RESOLVED_WIN` или `RESOLVED_LOSE`, слот освобождается, в Telegram уходит уведомление.

### Математика

`$1 / 0.01 = 100` акций. Если исход побеждает, каждая акция стоит $1, выплата $100, чистая прибыль $99. Если в стакане есть Ask дешевле (например, 0.005), бот покупает больше акций и показывает реальный коэффициент.

## Режимы

| Режим | Как включить | Что происходит |
|---|---|---|
| Сигналы (по умолчанию) | `TRADING_MODE=signals` | Приходит карточка с кнопками «⚡ Открыть в Phantom» и «🌐 Polymarket». Сигнал занимает слот: так лимит 10 соблюдается и для ручной торговли. Если вы не вошли в сделку, нажмите «🚫 Не вошёл» или отправьте `/cancel <id>`, и слот освободится. |
| Авто-торговля | `TRADING_MODE=auto` + `PRIVATE_KEY` | Бот покупает на $1: `FOK` — маркет-ордер с худшей ценой `MAX_PRICE`, `GTC` — лимитка по лучшему Ask. После исполнения присылает отчёт. Неисполненный FOK сразу освобождает слот. |

Ссылка Phantom имеет вид `https://phantom.app/ul/browse/<url-encoded polymarket url>?ref=...` и открывает страницу рынка во встроенном браузере Phantom. Там подключается EVM-кошелёк (Polygon). Публичного URL-параметра, который предвыбирал бы исход на polymarket.com, нет, поэтому ссылка ведёт на конкретный рынок, а нужный исход (YES/NO) указан в карточке.

## Команды

- `/start` — статус, режим и число открытых сделок X/10.
- `/positions` — активные сделки с таймером до закрытия и ссылками.
- `/stats` — всего сделок, выигрыши и проигрыши, win rate, PnL в $.
- `/cancel <id>` — освободить слот. Работает для сигнала без входа или неисполненного ордера; купленную позицию так закрыть нельзя.

Бот отвечает только чатам и пользователям из `TELEGRAM_CHAT_IDS`.

## Структура

```
main.py              запуск aiogram и фоновых задач (сканер и чекер резолюций)
scanner.py           Gamma и CLOB: фильтр по времени, цене и ликвидности стакана
position_manager.py  лимит слотов, резервирование, проверка резолюций и ордеров
clob_trader.py       py-clob-client (FOK/GTC), генератор ссылок Phantom и Polymarket
database.py          SQLite (aiosqlite): схема, запросы, статистика
telegram_ui.py       команды, карточки, рассылка с обработкой flood-limit
http_client.py       aiohttp: ретраи, экспоненциальный backoff, Retry-After, token-bucket rate limit
config.py            pydantic-settings (.env)
```

## Настройка

1. Создайте бота у [@BotFather](https://t.me/BotFather) и получите `BOT_TOKEN`.
2. Узнайте свой user id, например у [@userinfobot](https://t.me/userinfobot), и запишите его в `TELEGRAM_CHAT_IDS`. Чтобы сигналы шли в группу, добавьте туда id группы (он отрицательный).
3. Выполните `cp .env.example .env` и заполните файл.

### Авто-торговля (опционально)

- `PRIVATE_KEY` — приватный ключ кошелька в сети Polygon. Заведите отдельный кошелёк и держите на нём только небольшую сумму.
- `SIGNATURE_TYPE`:
  - `0` — EOA: USDC.e лежит прямо на адресе ключа. Один раз нужно выдать approve для USDC.e и CTF контрактам Exchange; проще всего один раз купить что-нибудь через UI Polymarket с этим кошельком.
  - `1` — аккаунт Polymarket через email/Magic: ключ экспортируется в настройках Polymarket. Нужен `FUNDER_ADDRESS` — адрес профиля.
  - `2` — браузерный кошелёк (Phantom/MetaMask EVM), подключённый к Polymarket. Ключ экспортируется из Phantom (аккаунт Ethereum/Polygon). `FUNDER_ADDRESS` — адрес профиля Polymarket, то есть proxy-кошелёк, на котором лежат депозиты.
- `ORDER_TYPE=FOK` рекомендуется: ордер исполняется сразу на $1 целиком или не исполняется вовсе.

> ⚠️ Покупка исходов по 1% — это лотерея: подавляющее большинство таких ставок проигрывает. Используйте только деньги, которые готовы потерять. Перед авто-режимом проверьте, разрешён ли Polymarket в вашей юрисдикции.

## Запуск локально

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # заполнить
python main.py
```

## Деплой на VPS (Docker)

```bash
# Ubuntu 22.04/24.04
curl -fsSL https://get.docker.com | sh
git clone <repo-url> polymarket-bot && cd polymarket-bot
cp .env.example .env && nano .env
mkdir -p data && sudo chown 1000:1000 data   # контейнер работает от uid 1000
docker compose up -d --build
docker compose logs -f bot
```

Обновление: `git pull && docker compose up -d --build`. База `data/bot.db` хранится на хосте и переживает перезапуски.

## Деплой без Docker (systemd)

```bash
sudo useradd -r -m -d /opt/polymarket-bot polybot
sudo -u polybot git clone <repo-url> /opt/polymarket-bot/app
cd /opt/polymarket-bot/app
sudo -u polybot python3 -m venv .venv
sudo -u polybot .venv/bin/pip install -r requirements.txt
sudo -u polybot cp .env.example .env && sudo -u polybot nano .env
```

Создайте `/etc/systemd/system/polymarket-bot.service`:

```ini
[Unit]
Description=Polymarket Underdog Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
User=polybot
WorkingDirectory=/opt/polymarket-bot/app
ExecStart=/opt/polymarket-bot/app/.venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now polymarket-bot
journalctl -u polymarket-bot -f
```

## Бот-хостинги (Pterodactyl, Bothost и т.п.)

Загрузите файлы проекта, укажите стартовый файл `main.py` и Python 3.11 или новее, установите зависимости из `requirements.txt`. Переменные из `.env.example` задайте в панели хостинга или в файле `.env`. Каталог `data/` должен быть доступен для записи.

## Надёжность

- Gamma и CLOB ограничиваются token bucket'ами (`GAMMA_MAX_RPS`, `CLOB_MAX_RPS`). После ответа 429 лимитер этого хоста ставится на паузу с учётом `Retry-After`.
- Ответы 408/425/429/5xx, обрывы соединения и таймауты повторяются с экспоненциальным backoff и jitter (`HTTP_MAX_RETRIES`).
- Если пакетный `POST /books` не сработал, стаканы запрашиваются по одному через `GET /book`.
- Отправка ордера **никогда** не повторяется автоматически, чтобы исключить двойную покупку. При ошибке приходит уведомление.
- На `TelegramRetryAfter` бот ждёт и повторяет отправку. Ошибка в одной позиции или одном цикле не останавливает фоновые задачи.
- SQLite работает в режиме WAL, бот корректно завершается по SIGTERM/SIGINT.
