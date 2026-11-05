# Соответствие заданию

| Уровень | Требование | Реализация и проверка |
|---|---|---|
| 1 | POST/GET quotes, сохранение и TTL | api.py, Quote; test_quote_persists, test_transfer_errors, test_quote_lock_expiry.py |
| 1 | SOURCE/TARGET, процент + fixed + min, обе валюты комиссии | pricing.py, money.py; test_pricing.py |
| 1 | Точность валют, округление, лимиты, ошибки | money.py, rates.py; test_currency_precision, test_amount_errors, test_mandatory_quote_errors |
| 1 | Детерминизм и официальные примеры | test_deterministic, test_starter.py: 10 examples через расчёт и HTTP |
| 2 | Перевод по сохранённой цене | service.py; test_quote_price_is_frozen |
| 2 | Идемпотентность и конфликт тела | UNIQUE client/key + hash; test_idempotency_before_during_after_and_conflict |
| 2 | State machine и резерв | transition, settle; test_scenarios_and_balance_conservation |
| 2 | Фильтры, оценка, маржа, запасной провайдер | routing.py; test_routing.py, test_fallback_and_margin_explanation |
| 2 | ok/decline/error_5xx/slow до итога | test_scenarios_and_balance_conservation, scripts/smoke.py |
| 3 | Потеря ответа, повтор прежнего provider key | worker.py; test_ambiguous_outcome_never_double_pays, test_5xx_then_lagging_404_never_switches_provider |
| 3 | Параллельный расход баланса | FOR UPDATE клиента + CHECK счетов; test_parallel_balance_and_daily_limit |
| 3 | Повтор до/во время/после выполнения | test_idempotency_before_during_after_and_conflict, test_concurrent_same_key_creates_one_hold |
| 3 | Webhook: дубли, порядок, неизвестная выплата | HMAC + WebhookEvent; test_webhook_auth_duplicates_unknown_and_reordering |
| 3 | Падение после отправки, рестарт, истёкший lease | test_restart_after_send_before_result, test_old_lease_owner_cannot_submit_or_settle; scripts/check_recovery.py с SIGKILL |
| 3 | Фоновая сверка и требует проверки | Worker + review_reason; test_review_keeps_hold_and_can_recover, test_reliability.py |
| 3 | Суточный лимит и UTC-полночь | DailyUsage; test_parallel_balance_and_daily_limit, test_failure_releases_usage_from_creation_day |
| 3 | Официальный chaos и фактический ledger | исходные scripts/chaos_run.py и mock-provider/; результаты в VERIFICATION.md |
| 4 | Структурные логи и метрики с transfer_id | observability.py, /metrics; Prometheus :9090, monitoring/prometheus.yml; test_operations.py |
| 4 | Конфиг без редеплоя | Catalog, provider_config.py, config_version; test_provider_config_reload_without_app_restart |
| 4 | Миграции | migrations/versions; upgrade на пустой и существующей БД, alembic check |
| 4 | Нагрузочный сценарий | scripts/load_run.py: concurrency, throughput, p50/p95/p99, ledger и баланс |
| 4 | Почему выбран провайдер | GET /transfers/{id}/routing: оценки, маржа, исключения, версия конфигурации |

Набор данных, примеры, эмулятор и chaos-тестер взяты из starter и сверяются с starter-manifest.json.
Результаты запусков и параметры среды приведены в VERIFICATION.md.

Повторная сверка выполнена по всем пунктам разделов 4-8 PDF: обязательный HTTP-контракт,
денежные строки, оба режима цены, тарифы и лимиты, TTL, сохранение цены, провайдеры и маржа,
state machine, финансовые инварианты, ошибки, конкуренция, webhook, восстановление и сверка.
Незакрытых обязательных пунктов уровней 1-2 не обнаружено. Для уровня 3 выполнены кодовые
проверки всех перечисленных вопросов и официальный chaos. Все пять бонусов уровня 4 реализованы.
Состояние NEEDS_REVIEW разрешено условием и сохраняет резерв до подтверждения внешнего исхода;
границы гарантий внешнего провайдера перечислены в LIMITS.md.
