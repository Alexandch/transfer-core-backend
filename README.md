# Transfer Core

Бэкенд международных переводов: котировка, резерв, выплата, сверка и окончательный расчёт.
Python 3.12, FastAPI, SQLAlchemy 2, PostgreSQL 18, Alembic, HTTPX, Prometheus, pytest.

В проекте реализованы уровни 1-4. Соответствие пунктам задания:
[REQUIREMENTS.md](REQUIREMENTS.md). Результаты проверок: [VERIFICATION.md](VERIFICATION.md).
Данные, 10 примеров, эмулятор и scripts/chaos_run.py взяты из предоставленного starter без изменений.
Их контрольные суммы перечислены в starter-manifest.json и проверяются тестом.

## Запуск

Нужен Docker с Compose v2 и Linux-контейнерами. Python на хосте не требуется.

```sh
docker compose up
```

Команда собирает образы, запускает PostgreSQL, применяет миграции, добавляет начальные балансы,
поднимает API, worker, эмулятор и Prometheus. Повторный запуск сохраняет балансы и историю.

API: http://localhost:8080. Swagger: http://localhost:8080/docs.
Готовность: http://localhost:8080/health. Эмулятор: http://localhost:9000.
Prometheus с графиками: http://127.0.0.1:9090/query. Проверка сбора: http://127.0.0.1:9090/targets.
Порты PostgreSQL на хосте: 55438 для сервиса, 55439 для отдельной тестовой БД.
Том PostgreSQL 18 монтируется в `/var/lib/postgresql`.

В фоне: `docker compose up -d`. Логи: `docker compose logs -f api worker`.
Остановка: `docker compose down`. Команда `docker compose down -v` удаляет данные проекта.
Пароли и HMAC в Compose - локальные демонстрационные значения. Переменные перечислены в .env.example.

## Демо и графики

Первый запуск создаёт клиентов и начальные балансы из starter; переводов ещё нет.
Для демонстрации выполняется отдельная команда:

```sh
docker compose --profile demo run --rm demo
```

Создаются четыре перевода: ok, decline, error_5xx, slow. Проверяются оба режима котировки,
идемпотентность, выплаты по ledger, изменение баланса и успешный сбор Prometheus.
Операции остаются в PostgreSQL; при повторном запуске приложения они не исчезают.
Подробности и PromQL: [DEMO.md](DEMO.md), [monitoring/QUERIES.md](monitoring/QUERIES.md).
Метрики снимаются каждые 5 секунд, история хранится 7 дней в отдельном томе.

## Тесты

```sh
docker compose --profile test run --build --rm tests
```

Тесты используют отдельную PostgreSQL 18 в tmpfs. Рабочая БД не очищается.
Версии зависимостей зафиксированы в uv.lock.

Проверка официальных примеров через запущенный API:

```sh
uv run python scripts/check_examples.py --json output/official-examples.json
```

```sh
docker compose --profile test run --rm tests ruff check app tests scripts migrations
docker compose --profile test run --rm tests ruff format --check app tests scripts migrations
```

Локальная разработка с uv:

```sh
uv sync --frozen --python 3.12
docker compose --profile test up -d --wait db test-db
uv run alembic upgrade head
uv run python -m app.bootstrap
uv run pytest -q
```

TEST_DATABASE_URL переопределяет тестовую БД; имя должно оканчиваться на `_test`.
Её таблицы очищаются перед каждым интеграционным тестом.

## Пример PowerShell

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

10000.00 RUB дают комиссию 150.00 RUB, конвертацию 9850.00 RUB и выплату 4137.00 THB.
POST /transfers сразу возвращает 202. Результат, история и попытки доступны через GET.
Повтор с прежним телом и ключом возвращает прежний перевод, в том числе после истечения котировки.
Котировка используется один раз. Новый перевод требует новой котировки и нового ключа.
Если нет подходящего провайдера, перевод завершается FAILED с возвратом резерва;
причины исключения провайдеров доступны через /routing.

## API

| Метод | Путь | Результат |
|---|---|---|
| POST | /quotes | 201, сохранённая цена и expires_at |
| GET | /quotes/{quote_id} | Та же котировка, включая истёкшую |
| POST | /transfers | 202, обязательный Idempotency-Key |
| GET | /transfers/{transfer_id} | Статус, review_reason, история, попытки и наблюдения |
| GET | /clients/{client_id}/balance | available / held / spent / initial по валютам |
| POST | /webhooks/{provider_id} | HMAC, дедупликация и запуск сверки |
| GET | /transfers/{transfer_id}/routing | Оценки, маржа, исключения и config_version |
| GET | /metrics | Prometheus: состояния, ручная проверка и отправки с transfer_id |
| GET | /stats | JSON-число переводов по статусам |
| GET | /health | Проверка API и БД |

Денежные поля - строки с точностью валюты из currencies.json. Ошибка:
`{"error":{"code":"...","message":"...","details":{}}}`.

| HTTP | Коды |
|---|---|
| 422 | VALIDATION_ERROR, UNSUPPORTED_DIRECTION, AMOUNT_BELOW_MIN, AMOUNT_ABOVE_MAX, RATE_UNAVAILABLE |
| 404 | QUOTE_NOT_FOUND, CLIENT_NOT_FOUND, TRANSFER_NOT_FOUND, PROVIDER_NOT_FOUND |
| 409 | QUOTE_EXPIRED, QUOTE_ALREADY_USED, IDEMPOTENCY_CONFLICT, INSUFFICIENT_FUNDS, DAILY_LIMIT_EXCEEDED, WEBHOOK_CONFLICT |
| 401 | INVALID_SIGNATURE |
| 413 | VALIDATION_ERROR для слишком большого webhook |
| 503 | RATE_UNAVAILABLE при недоступности курсов, CONFIG_INVALID при неверном конфиге провайдеров |

Подпись webhook: hex HMAC-SHA256 исходных байтов тела, ключ PROVIDER_WEBHOOK_SECRET.
Одинаковый event_id с другим телом возвращает WEBHOOK_CONFLICT. Неизвестные reference сохраняются.

## Сценарии и chaos

X-Scenario: ok, decline, error_5xx, slow. В Compose разрешён ALLOW_SCENARIOS=true.
Полный decline/error_5xx применяется ко всем провайдерам и завершается FAILED.
PROVIDER_PROTOCOL=starter включает обработку подтверждённого в исходниках отказа первого POST
с кодом INTERNAL_ERROR. Ошибки повторного POST, 504, обрыв связи и отстающий GET остаются
неоднозначными: повторяется прежний provider key, резерв сохраняется.
Slow принимает выплату, но отвечает через 30 секунд. Worker использует таймаут 3 секунды.

```sh
uv run python scripts/smoke.py
```

Chaos в PowerShell:

```powershell
$env:MOCK_MODE = 'chaos'
$env:MOCK_SEED = '42'
docker compose up -d --no-deps --force-recreate mock-provider
docker compose up -d --no-deps --scale worker=4 worker
New-Item -ItemType Directory -Force output | Out-Null
uv run python scripts/chaos_run.py --transfers 300 --seed 42 --api http://127.0.0.1:8080 --mock http://127.0.0.1:9000 --json output/official-chaos.json
```

Без Python на хосте:

```sh
docker compose run --rm api python scripts/chaos_run.py --transfers 300 --api http://api:8080 --mock http://mock-provider:9000
```

Официальный chaos включает 5xx до/после принятия, обрыв связи, зависший ответ, задержку видимости
в GET, устаревший PROCESSING, потерянные, поддельные, повторные и противоречивые webhook.
Скрипт проверяет выплаты по recipient/account, суммы, валюты, идемпотентность и балансы клиентов.
Параметры: --settle-timeout (420 секунд по умолчанию), --ledger-grace (30 секунд), --json.
MOCK_SEED определяет поведение провайдера, --seed - распределение запросов генератора.

Эмулятор хранит выплаты и ключи в памяти: перезапуск контейнера сбрасывает его ledger.
Смена режима и новые прогоны выполняются после завершения текущих операций.
БД приложения сохраняется в отдельном томе db-starter-release-data.

Для проверки chaos без изменения демонстрационного стенда предусмотрен отдельный Compose-проект:

```sh
docker compose -f compose.audit.yaml up -d --build --scale worker=4 --wait
uv run python scripts/chaos_run.py --transfers 300 --seed 42 --api http://127.0.0.1:18080 --mock http://127.0.0.1:19000
uv run python scripts/check_recovery.py --json output/recovery.json
```

Вторая команда запускает исходный официальный checker. Третья останавливает только audit-worker
через SIGKILL после принятия выплаты провайдером и до сохранения ответа, запускает worker снова
и проверяет единственную выплату и списание. Основной стенд на 8080/9000 остаётся доступен.
Audit-БД отдельная и временная; `docker compose -f compose.audit.yaml down` удаляет этот тестовый стенд.

## Нагрузка

```sh
uv run python scripts/load_run.py --transfers 300 --concurrency 20 --output output/load-result.json
```

Сценарий измеряет throughput создания переводов, p50/p95/p99 отдельно для quotes и transfers,
ожидает завершения и проверяет выплаты по ledger и расход баланса. Использует X-Scenario=ok.
Нагрузочный прогон использует официального клиента c_101; --client задаёт другого клиента.
Нагрузочный и chaos-прогоны выполняются последовательно, чтобы расход баланса был однозначен.

## Конфигурация и модули

providers.json перечитывается без перезапуска. Перед каждой новой попыткой заново проверяются
доступность, лимиты, комиссия и маржа. Уже опробованные провайдеры исключаются.
Оценки каждой попытки сохраняются в selections плана маршрута; цена котировки остаётся прежней.
Конфигурация валидируется; её SHA-256 сохраняется в плане маршрута как config_version.
При ошибке конфигурации резерв сохраняется, перевод получает NEEDS_REVIEW с причиной.

money.py/pricing.py/rates.py - расчёт; routing.py - выбор; service.py/db.py - транзакции;
provider.py - HTTP; worker.py - исполнение и сверка; mock-provider/ - официальный эмулятор.
Изолированные искусственные данные регрессионных тестов находятся в tests/fixtures/demo/.

[Архитектура](ARCHITECTURE.md), [решения](DECISIONS.md), [допущения](ASSUMPTIONS.md),
[ограничения](LIMITS.md), [требования](REQUIREMENTS.md).
