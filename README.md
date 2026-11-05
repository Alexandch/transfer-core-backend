# Transfer Core

Ядро сервиса международных переводов: котировка -> резерв -> выплата -> списание или возврат.
Python 3.12, FastAPI, SQLAlchemy 2, PostgreSQL 18, Alembic, HTTPX, pytest. Фронтенда нет.

**Происхождение данных.** Условие ссылается на отдельный `transfer-core-starter.zip`, но этот архив
не был предоставлен. Поэтому `data/`, `examples/`, эмулятор и `scripts/chaos_run.py` созданы для этого
решения по опубликованному контракту. Числа из иллюстрации PDF воспроизведены для RU-TH-BANK;
остальные тарифы и провайдеры демонстрационные. Прохождение персональных примеров и официального
chaos-тестера не заявляется.

## Запуск с нуля

Нужен Docker с Compose v2 и Linux-контейнерами. Python на хосте не требуется.

```sh
docker compose up
```

Compose собирает образы, поднимает PostgreSQL, применяет миграции, добавляет тестовых клиентов,
запускает эмулятор, API и worker. Сервис: http://localhost:8080,
Swagger: http://localhost:8080/docs, проверка готовности: http://localhost:8080/health.
Первый запуск требует доступа к реестрам Docker и PyPI.

В фоне: `docker compose up -d`. Логи: `docker compose logs -f api worker`.
Остановка с сохранением балансов: `docker compose down`.
`docker compose down -v` **удаляет данные этого проекта** и возвращает исходные балансы при следующем запуске.

Порты хоста: API 8080, эмулятор 9000, БД 55438. База внутри сети Compose использует 5432.
Для PostgreSQL 18 том подключён к `/var/lib/postgresql`.
Значения паролей и HMAC в Compose - публичные локальные заглушки, не реальные секреты.

## Тесты одной командой

```sh
docker compose --profile test run --build --rm tests
```

Используется отдельная PostgreSQL 18 в tmpfs; рабочая БД не затрагивается.
Проверяются оба режима расчёта, SOURCE/TARGET-комиссии, 0/2/3 знака валют,
округление, все обязательные коды ошибок, выбор и смена провайдера, сценарии
ok/decline/error_5xx/slow, конкурентные запросы и восстановление после сбоя.

Проверка стиля:

```sh
docker compose --profile test run --rm tests ruff check app tests scripts migrations
docker compose --profile test run --rm tests ruff format --check app tests scripts migrations
```

При локальной разработке с [uv](https://docs.astral.sh/uv/):

```sh
uv sync --frozen --python 3.12
docker compose --profile test up -d --wait db test-db
uv run alembic upgrade head
uv run python -m app.bootstrap
uv run pytest -q
```

Тестовый порт - 55439. `TEST_DATABASE_URL` переопределяет адрес тестовой БД; её имя должно
оканчиваться на `_test`: перед каждым интеграционным тестом таблицы в ней очищаются.
Точная версия зависимостей зафиксирована в `uv.lock`.

## Пример запроса

PowerShell:

```powershell
$quote = Invoke-RestMethod http://localhost:8080/quotes -Method Post `
  -ContentType 'application/json' -InFile examples/quote-request.json
$body = @{
  quote_id = $quote.quote_id
  client_id = 'c_001'
  recipient = @{ name = 'Somchai Jaidee'; account = '1234567890'; bank_code = 'BKKB' }
} | ConvertTo-Json
$transfer = Invoke-RestMethod http://localhost:8080/transfers -Method Post `
  -ContentType 'application/json' -Headers @{'Idempotency-Key' = 'demo-1'; 'X-Scenario' = 'ok'} `
  -Body $body
Invoke-RestMethod "http://localhost:8080/transfers/$($transfer.transfer_id)"
Invoke-RestMethod http://localhost:8080/clients/c_001/balance
```

Для запроса на 10000.00 RUB: комиссия 150.00 RUB, конвертация 9850.00 RUB,
выплата 4137.00 THB. `POST /transfers` сразу возвращает **202**, обычно со статусом `RESERVED`.
Итог виден через GET. Новый ключ нужен для нового запроса; повтор с прежним телом и ключом
возвращает прежний перевод даже после истечения котировки. Каждая котировка расходуется один раз.

`X-Scenario`: `ok`, `decline`, `error_5xx`, `slow`. Этот заголовок разрешён в демонстрационном
Compose (`ALLOW_SCENARIOS=true`); вне демонстрации его следует отключить.
Сценарий применяется ко всем провайдерам попытки: поэтому полный decline/error_5xx заканчивается
`FAILED`, а отдельный отказ с успешным запасным провайдером проверяется интеграционным тестом.

## API

| Метод | Путь | Результат |
|---|---|---|
| POST | `/quotes` | 201, сохранённая котировка |
| GET | `/quotes/{quote_id}` | 200, та же цена и TTL |
| POST | `/transfers` | 202, обязательный `Idempotency-Key` |
| GET | `/transfers/{transfer_id}` | Цена, статус, история, попытки |
| GET | `/clients/{client_id}/balance` | available / held / spent / initial по валютам |
| POST | `/webhooks/{provider_id}` | HMAC, дедупликация, запуск сверки |
| GET | `/transfers/{transfer_id}/routing` | Кандидаты, оценки, причины исключения |
| GET | `/health` | Проверка API и БД |
| GET | `/metrics` | Число переводов по статусам в JSON |

Деньги в API - строки с точностью из `currencies.json`, включая входные суммы.
Клиентские ошибки возвращаются в формате
`{"error":{"code":"...","message":"...","details":{}}}`.

| HTTP | Коды |
|---|---|
| 422 | VALIDATION_ERROR, UNSUPPORTED_DIRECTION, AMOUNT_BELOW_MIN, AMOUNT_ABOVE_MAX, RATE_UNAVAILABLE при отсутствии свежего пути FX |
| 404 | QUOTE_NOT_FOUND, CLIENT_NOT_FOUND, TRANSFER_NOT_FOUND, PROVIDER_NOT_FOUND |
| 409 | QUOTE_EXPIRED, QUOTE_ALREADY_USED, IDEMPOTENCY_CONFLICT, INSUFFICIENT_FUNDS, DAILY_LIMIT_EXCEEDED, NO_ELIGIBLE_PROVIDER, WEBHOOK_CONFLICT |
| 401 | INVALID_SIGNATURE |
| 413 | VALIDATION_ERROR для слишком большого webhook |
| 503 | RATE_UNAVAILABLE при недоступности источника курсов |

GET истёкшей котировки возвращает сохранённую цену; использовать её в новом переводе нельзя.
Подпись webhook: hex HMAC-SHA256 исходных байтов тела, ключ `PROVIDER_WEBHOOK_SECRET`.
Одинаковый event_id с изменённым телом возвращает WEBHOOK_CONFLICT.

## Самопроверка с эмулятором

Для включения chaos в PowerShell:

```powershell
$env:MOCK_MODE = 'chaos'
$env:MOCK_SEED = '42'
docker compose up -d --force-recreate mock-provider
docker compose up -d --scale worker=4
uv run python scripts/chaos_run.py --transfers 300
```

Без Python на хосте, после включения chaos:

```sh
docker compose run --rm api python scripts/chaos_run.py --transfers 300 --api http://api:8080 --provider http://mock-provider:9000
```

Скрипт отправляет запросы параллельно, повторяет идемпотентные POST, ожидает итоговые статусы,
сравнивает reference, сумму и валюту с реальными записями выплат эмулятора и проверяет балансы.
Используется отдельный демонстрационный клиент `c_load`. Не запускайте другой генератор переводов
для того же клиента одновременно: скрипт сравнивает изменение его баланса за весь прогон.
`MOCK_SEED` определяет поведение для reference, но UUID переводов случайны, поэтому новый прогон
не обязан воспроизвести те же сценарии в том же порядке.

Эмулятор хранит выплаты в схеме `mock` PostgreSQL и переживает перезапуск. Он умеет отказ,
5xx до/после принятия, ответ через 30 секунд, испорченный ответ, отсутствие callback,
повторный и запоздавший webhook. `/rates` выдаёт значения снимка `data/rates.json` со свежим
временем публикации: это тестовый источник, а не реальные рыночные данные.

## Структура

```text
app/money.py, pricing.py, rates.py  расчёт и FX без HTTP/БД
app/routing.py                    фильтрация, маржа, ранжирование
app/service.py, db.py              транзакции, резерв, баланс, состояния
app/provider.py                   HTTP-адаптер провайдера
app/worker.py                     очередь в PostgreSQL, lease, сверка
app/api.py                        HTTP-контракт
app/mock_provider.py              собственный эмулятор
migrations/                      версионируемая схема Alembic
tests/                           модульные и PostgreSQL-тесты
data/, examples/                 демонстрационный набор
```

Обоснования: [DECISIONS.md](DECISIONS.md). Точные правила: [ASSUMPTIONS.md](ASSUMPTIONS.md).
Границы решения: [LIMITS.md](LIMITS.md). Разбор для защиты: [DEFENSE.md](DEFENSE.md).
Отчёт фактической проверки: [VERIFICATION.md](VERIFICATION.md).
