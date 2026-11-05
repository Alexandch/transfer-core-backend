# Графики Prometheus

Открыть http://127.0.0.1:9090/query, ввести выражение и выбрать Graph. Для демо удобен диапазон 15 минут.

| Показатель | PromQL |
|---|---|
| Доступность API | `up{job="transfer-core"}` |
| Переводы по статусам | `transfer_core_transfers` |
| Успешные переводы | `transfer_core_transfers{status="COMPLETED"}` |
| Отказы | `transfer_core_transfers{status="FAILED"}` |
| Требуют проверки | `sum(transfer_core_transfer_requires_review) or vector(0)` |
| Отправки провайдерам | `sum by (provider_id) (transfer_core_attempt_sends_total)` |
| Резерв клиента в RUB | `transfer_core_balance_minor_units{client_id="c_101",currency="RUB",kind="held"} / 100` |
| Списано с клиента в RUB | `transfer_core_balance_minor_units{client_id="c_101",currency="RUB",kind="spent"} / 100` |
| Доступно клиенту в RUB | `transfer_core_balance_minor_units{client_id="c_101",currency="RUB",kind="available"} / 100` |

Показатель balance_minor_units выражен в минимальных единицах валюты: RUB делится на 100,
KWD - на 1000, JPY не делится. Остатки выводятся из одной записи счёта и удовлетворяют
available + held + spent = initial. Transfer_id есть в метриках отдельных переводов и попыток.

Prometheus сохраняет временные ряды. Удаление его тома удаляет графики; PostgreSQL остаётся
источником текущих финансовых остатков. Данные генераторов и отдельного audit-стенда не смешиваются.

[Документация графического интерфейса Prometheus](https://prometheus.io/docs/prometheus/latest/getting_started/).
