import json
import logging

from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
from sqlalchemy import func, select

from app.clock import iso, now
from app.db import Attempt, Transfer


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        try:
            body = json.loads(record.getMessage())
            if not isinstance(body, dict):
                body = {"message": body}
        except (ValueError, TypeError):
            body = {"message": record.getMessage()}
        body.update(at=iso(now()), level=record.levelname, logger=record.name)
        if record.exc_info:
            body["exception"] = self.formatException(record.exc_info)
        return json.dumps(body, ensure_ascii=False)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def metrics_text(session) -> str:
    class DatabaseCollector:
        def collect(self):
            totals = GaugeMetricFamily(
                "transfer_core_transfers", "Transfers by status", labels=["status"]
            )
            for status, count in session.execute(
                select(Transfer.status, func.count()).group_by(Transfer.status)
            ):
                totals.add_metric([status], count)
            yield totals
            states = GaugeMetricFamily(
                "transfer_core_transfer_status",
                "Current transfer state",
                labels=["transfer_id", "status"],
            )
            reviews = GaugeMetricFamily(
                "transfer_core_transfer_requires_review", "Review required", labels=["transfer_id"]
            )
            for transfer in session.scalars(select(Transfer).order_by(Transfer.id)):
                states.add_metric([transfer.id, transfer.status], 1)
                reviews.add_metric(
                    [transfer.id],
                    int(transfer.status == "NEEDS_REVIEW" or transfer.review_reason is not None),
                )
            yield states
            yield reviews
            sends = CounterMetricFamily(
                "transfer_core_attempt_sends",
                "Provider submissions",
                labels=["transfer_id", "attempt_id", "provider_id"],
            )
            for attempt in session.scalars(select(Attempt).order_by(Attempt.id)):
                sends.add_metric(
                    [attempt.transfer_id, attempt.id, attempt.provider_id], attempt.sends
                )
            yield sends

    registry = CollectorRegistry()
    registry.register(DatabaseCollector())
    return generate_latest(registry).decode()
