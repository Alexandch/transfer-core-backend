# Проверка

5 ноября 2026 года. Приложение: Python 3.12.16 и PostgreSQL 18.6.
Официальный mock-provider и chaos_run.py запущены из присланного starter без изменений.
Docker-среда: 16 CPU, около 8 ГБ памяти. Chaos и нагрузка - четыре audit-worker;
аварийный рестарт - один audit-worker; демонстрационный стенд - один worker.

## Исходный набор

Архив: transfer-core-starter (2).zip.
SHA-256: `0b912b15e7a9fc4e2c322c2ab5ab656b31a8fab30eb2cc5b1da97b5815170568`.
25 исходных файлов data/, examples/, mock-provider/ и scripts/chaos_run.py проверяются
по starter-manifest.json. Compose адаптирован к PostgreSQL 18 и реализации приложения.

## Тесты

```text
docker compose --profile test run --build --rm tests
112 passed, 1 warning in 23.23s
TOTAL: 939 statements, 68 missed, 93% coverage

ruff check app tests scripts migrations: All checks passed!
ruff format --check app tests scripts migrations: 37 files already formatted
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
`scripts/chaos_run.py --transfers 300 --seed 42 --api http://127.0.0.1:18080 --mock http://127.0.0.1:19000 --json output/official-chaos.json`.
Изолированный compose.audit.yaml, девять доступных направлений, исходные клиенты и тарифы starter.

| Показатель | Результат |
|---|---:|
| Создано переводов | 300 |
| Успешных | 216 |
| Неуспешных | 83 |
| Требуют проверки | 1 |
| Двойных запросов с одним ключом | 44 |
| Длительность прогона | 101 сек |
| Нарушений во всех категориях | 0 |

RESULT: OK. Проверены double_payouts, stuck_reserves, balance_invariant, failed_but_paid,
success_without_payout, amount_mismatch, unaccounted_payouts, idempotency, stuck_transfers и api_errors.

## Нагрузка на официальном эмуляторе

300 переводов, concurrency=20, X-Scenario=ok, клиент c_003, адреса 127.0.0.1.
Суточный лимит c_003 задан в USD, списания выполняются в RUB.

| Показатель | Результат |
|---|---:|
| Принято переводов в секунду | 37.43 |
| Весь прогон с ожиданием и сверкой | 16.81 сек |
| Quotes p50 / p95 / p99 | 94.0 / 235.0 / 266.0 мс |
| Transfers API p50 / p95 / p99 | 406.0 / 515.0 / 562.0 мс |
| HTTP-ошибки | 0 |
| Двойные выплаты | 0 |
| Необъяснимые резервы | 0 |

Все 300 переводов COMPLETED; ledger и расход баланса сверены.
Отчёты: [examples](reports/examples.json), [chaos](reports/chaos.json),
[нагрузка](reports/load.json), [smoke](reports/smoke.json).
Соответствие заданию: [REQUIREMENTS.md](REQUIREMENTS.md).


## Повторная сверка задания и TTL

Все пункты разделов 4-8 PDF повторно сопоставлены с кодом и тестами в REQUIREMENTS.md.
Исправлен TTL после ожидания клиентской блокировки: реальный конкурентный тест PostgreSQL
подтверждает QUOTE_EXPIRED без резерва, когда котировка истекает во время ожидания.
Для чистой базы проверены нулевые метрики всех шести статусов и исходные денежные остатки.

## Аварийный рестарт процесса

`scripts/check_recovery.py --json output/recovery.json` выполнялся в отдельном audit-проекте.
Provider принял выплату, а запись Attempt ещё имела SUBMITTING и пустой payout_id.
Worker остановлен SIGKILL в этом окне и запущен заново. Результат: COMPLETED,
одна попытка, одна отправка, одна фактическая выплата, списано 10000.00 RUB, held=0.00.
Основной демонстрационный стенд не перезапускался. Отчёт: [recovery](reports/recovery.json).

## Prometheus и демонстрация

Установлен Prometheus 3.16.0 в основном Compose, интервал сбора 5 секунд,
срок хранения 7 дней. promtool check config: SUCCESS. UI /query: HTTP 200,
target transfer-core: UP без ошибок, query_range возвращает все шесть рядов статусов.
Временной ряд резерва c_101 достигал 4000000 minor units и вернулся в ноль.
Финальное списание в TSDB: 4000000 minor units.
HTTP/API-проверка интерфейса и временных рядов выполнена; браузерная автоматизация в среде недоступна.
Отчёт: [monitoring](reports/monitoring.json).

Проверены оба запуска демо: локальный Python и точная команда
`docker compose --profile demo run --rm demo`. Каждый создаёт четыре перевода через API:
два COMPLETED, два FAILED, резерв возвращается в ноль, spent растёт на 20000.00 RUB.
Идемпотентность, конфликт ключа, ledger и попадание окончательного остатка в Prometheus подтверждены.
После двух прогонов в основном стенде восемь переводов, c_101.spent=40000.00 RUB, held=0.00.
Отчёт последнего запуска: [demo](reports/demo.json). Инструкция: [DEMO.md](DEMO.md).
