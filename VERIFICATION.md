# Проверка

5 ноября 2026 года. Приложение: Python 3.12.16 и PostgreSQL 18.6.
Официальный mock-provider и chaos_run.py запущены из присланного starter без изменений.
Docker-среда: 16 CPU, около 8 ГБ памяти; сквозные прогоны - четыре worker.

## Исходный набор

Архив: transfer-core-starter (2).zip.
SHA-256: `0b912b15e7a9fc4e2c322c2ab5ab656b31a8fab30eb2cc5b1da97b5815170568`.
25 исходных файлов data/, examples/, mock-provider/ и scripts/chaos_run.py проверяются
по starter-manifest.json. Compose адаптирован к PostgreSQL 18 и реализации приложения.

## Тесты

```text
docker compose --profile test run --build --rm tests
110 passed, 1 warning in 16.38s
TOTAL: 932 statements, 68 missed, 93% coverage

ruff check app tests scripts migrations: All checks passed!
ruff format --check app tests scripts migrations: 34 files already formatted
alembic check: No new upgrade operations detected.
git diff --check: успешно
```

Все 10 официальных примеров проверены через расчёт и HTTP. Отдельный сквозной запуск
scripts/check_examples.py на работающем API: 10 passed, 0 failed.
Дополнительно проверены исходные регрессионные случаи, баланс, идемпотентность, сбои,
лимит в USD при списании RUB, счета RUB/KWD с общим лимитом и направление без провайдеров.
Предупреждение Starlette относится к будущей смене транспорта TestClient;
зафиксированные версии совместимы. Официальный код исключён из форматирования и линтинга,
чтобы сохранять его исходные байты.

## Официальные сценарии

| Сценарий | Итог | Попыток |
|---|---|---:|
| ok | COMPLETED | 1 |
| decline | FAILED | 3 |
| error_5xx | FAILED | 3 |
| slow, ответ через 30 секунд | COMPLETED | 1 |

## Официальный chaos

MOCK_MODE=chaos, MOCK_SEED=42.
`scripts/chaos_run.py --transfers 300 --seed 42 --api http://127.0.0.1:8080 --mock http://127.0.0.1:9000 --json output/official-chaos.json`.
Девять доступных направлений, исходные клиенты и тарифы starter.

| Показатель | Результат |
|---|---:|
| Создано переводов | 300 |
| Успешных | 227 |
| Неуспешных | 72 |
| Требуют проверки | 1 |
| Двойных запросов с одним ключом | 44 |
| Длительность прогона | 98 сек |
| Нарушений во всех категориях | 0 |

RESULT: OK. Проверены double_payouts, stuck_reserves, balance_invariant, failed_but_paid,
success_without_payout, amount_mismatch, unaccounted_payouts, idempotency, stuck_transfers и api_errors.

## Нагрузка на официальном эмуляторе

300 переводов, concurrency=20, X-Scenario=ok, клиент c_003, адреса 127.0.0.1.
Суточный лимит c_003 задан в USD, списания выполняются в RUB.

| Показатель | Результат |
|---|---:|
| Принято переводов в секунду | 30.82 |
| Весь прогон с ожиданием и сверкой | 20.59 сек |
| Quotes p50 / p95 / p99 | 125.0 / 265.0 / 312.0 мс |
| Transfers API p50 / p95 / p99 | 485.0 / 625.0 / 687.0 мс |
| HTTP-ошибки | 0 |
| Двойные выплаты | 0 |
| Необъяснимые резервы | 0 |

Все 300 переводов COMPLETED; ledger и расход баланса сверены.
Отчёты: [examples](reports/examples.json), [chaos](reports/chaos.json),
[нагрузка](reports/load.json), [smoke](reports/smoke.json).
Соответствие заданию: [REQUIREMENTS.md](REQUIREMENTS.md).
