# Проверка

5 ноября 2026 года. Docker Desktop, Linux-контейнеры, Python 3.12.16, PostgreSQL 18.6.
Локальная Docker-среда: 16 CPU, около 8 ГБ памяти. Сквозные прогоны - четыре worker.

## Тесты и статические проверки

```text
docker compose --profile test run --build --rm tests
83 passed, 1 warning in 10.16s
TOTAL: 1071 statements, 215 missed, 80% coverage

ruff check app tests scripts migrations: All checks passed!
ruff format --check app tests scripts migrations: 34 files already formatted
alembic check: No new upgrade operations detected.
git diff --check: успешно
```

Pricing - 97%, money - 96%, rates/routing/provider - 100%, service - 93%, worker - 87%,
API - 92%. Общий процент включает эмулятор, исполняемый в отдельном контейнере во время
сквозных проверок и не измеряемый pytest-cov в процессе тестов.
Предупреждение Starlette относится к будущей смене транспорта TestClient;
зафиксированные версии проходят тесты.

Миграции проверены на тестовой БД и на существующей БД с историей переводов.
Схема соответствует моделям после upgrade.

## Основные HTTP-сценарии

`scripts/smoke.py` обращается к API, отдельному эмулятору и отдельным worker.

| Сценарий | Итог | Попыток |
|---|---|---:|
| ok | COMPLETED | 1 |
| decline | FAILED | 3 |
| error_5xx | FAILED | 3 |
| slow, ответ через 30 секунд | COMPLETED | 1 |

## Chaos

Расширенный эмулятор: потеря HTTP после принятия, 5xx, отстающий GET, временный неверный FAILED,
повторные/старые/неизвестные webhook, испорченный ответ, отсутствие callback, slow.

Финальный прогон: MOCK_MODE=chaos, MOCK_SEED=17, четыре worker,
`scripts/chaos_run.py --transfers 300 --timeout 240`.

```json
{"transfers":300,"statuses":{"COMPLETED":299,"FAILED":1},"double_payouts":0,"unexplained_holds":0}
```

Дополнительный прогон с seed=42: 297 COMPLETED, 3 FAILED; двойных выплат и необъяснимых
резервов также нет. Сверка проверяет число, сумму и валюту фактических выплат по reference,
conservation баланса и соответствие изменения spent/held итогам переводов.

## Нагрузка

`scripts/load_run.py --transfers 300 --concurrency 20`:

| Показатель | Результат |
|---|---:|
| Принято переводов в секунду | 34.66 |
| Весь прогон с ожиданием и сверкой | 21.47 сек |
| Quotes p50 / p95 / p99 | 94 / 2172 / 2266 мс |
| Transfers API p50 / p95 / p99 | 375 / 500 / 531 мс |
| HTTP-ошибки | 0 |
| Двойные выплаты | 0 |
| Необъяснимые резервы | 0 |

Все 300 переводов COMPLETED. Проверены фактические ledger провайдеров и расход баланса.
Машинные отчёты: [chaos](reports/chaos.json), [нагрузка](reports/load.json), [smoke](reports/smoke.json).
Соответствие проверок пунктам задания: [REQUIREMENTS.md](REQUIREMENTS.md).
