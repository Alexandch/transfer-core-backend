# Проверка реализации

Проверено 5 ноября 2026 года. Docker Desktop, Linux-контейнеры, PostgreSQL **18.6**
из образа `postgres:18-alpine`. Все проверки относятся к собственному демонстрационному набору:
официальный starter не предоставлен.

## Автоматические тесты

```text
docker compose --profile test run --build --rm tests
68 passed, 1 warning in 8.19s
TOTAL 866 statements, 189 missed, 78% coverage
```

Покрытие отдельных модулей: money 96%, pricing 97%, rates 100%, routing 100%,
provider 100%, service 97%, worker 86%, API 88%. Общие 78% включают отдельный эмулятор,
который проверяется сквозными прогонами, а не измеряется pytest-cov в процессе API.
Предупреждение исходит из Starlette TestClient: HTTPX объявлен устаревающим транспортом тестового
клиента. Текущие зафиксированные версии совместимы; это не ошибка тестов и не предупреждение API.

Среди проверок: 10 JSON-примеров, обе валюты комиссии, SOURCE/TARGET и минимальность обратного
расчёта, 0/2/3 знака валют, все обязательные ошибки, конкурентный расход баланса и лимита,
16 одновременных одинаковых ключей, смена провайдера, пропуск отключённого кандидата,
таймаут до/после принятия, 5xx после принятия, сбой до записи ответа, повторные и старые webhook,
сохранение резерва в NEEDS_REVIEW и последующее восстановление.

```text
ruff check app tests scripts migrations: All checks passed!
ruff format --check app tests scripts migrations: 28 files already formatted
alembic check: No new upgrade operations detected.
```

Миграции применены как к рабочей БД Compose, так и к пустой отдельной тестовой БД.

## Сквозная проверка четырёх сценариев

`docker compose up -d --build --wait`, затем `uv run python scripts/smoke.py`.
API действительно общается с отдельным контейнером эмулятора, worker работает отдельно.

| Сценарий | Итог | Попыток |
|---|---|---:|
| ok | COMPLETED | 1 |
| decline | FAILED | 3 |
| error_5xx | FAILED | 3 |
| slow, ответ через 30 секунд | COMPLETED | 1 |

## Собственный chaos-прогон

`MOCK_MODE=chaos`, `MOCK_SEED=42`, четыре worker, клиент c_load,
`scripts/chaos_run.py --transfers 300 --timeout 240`.

```json
{
  "transfers": 300,
  "statuses": {"COMPLETED": 295, "FAILED": 5},
  "double_payouts": 0,
  "unexplained_holds": 0,
  "emulator": "locally-authored-demo"
}
```

Проверены количество, сумма и валюта фактических выплат по reference; conservation балансов;
изменение spent соответствует успешным переводам; held не содержит необъяснённых резервов.
После smoke и chaos в БД 304 перевода: 297 COMPLETED, 7 FAILED.

Это подтверждает поведение на перечисленных сценариях собственного эмулятора. Оно не заменяет
проверку на неизвестном официальном chaos-режиме и не устраняет ограничения из LIMITS.md.
