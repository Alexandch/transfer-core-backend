# Архитектура

```mermaid
flowchart LR
    Client[HTTP-клиент] --> API[FastAPI :8080]
    API --> Pricing[Расчёт котировки / Decimal]
    API --> Service[Резерв и идемпотентность]
    Pricing --> Rates[Курсы /rates]
    Service --> DB[(PostgreSQL 18)]
    Worker[Worker / lease + SKIP LOCKED] --> DB
    Worker --> Provider[Провайдер / HTTP]
    Provider --> Webhook[Webhook / HMAC + dedup]
    Webhook --> DB
    API --> Metrics[Prometheus /metrics]
```

Расчёт, выбор провайдера, транзакции и HTTP-адаптер разделены по модулям.
Котировка хранит цену и FX-снимок. Создание перевода атомарно резервирует деньги и сохраняет
задание worker. Внешний HTTP выполняется после коммита попытки со стабильным ключом.

Worker подтверждает результат через GET провайдера. Webhook ускоряет сверку. Наблюдения
сохраняются в attempt_observations; неподтверждённый исход не освобождает резерв.
Терминальное завершение и изменение баланса происходят в одной транзакции.
