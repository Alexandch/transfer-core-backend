from collections import deque
from datetime import datetime
from decimal import localcontext

from app.errors import DomainError
from app.money import D


class Rates:
    def __init__(self, rows: list[dict], at: datetime, max_age: int):
        self.rows = rows
        self.edges: dict[str, dict] = {}
        # Fresh explicit rates take priority over derived inverse rates.
        usable = []
        for row in rows:
            updated = datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00"))
            age = (at - updated).total_seconds()
            rate = D(row["rate"])
            if -60 <= age <= max_age and rate.is_finite() and rate > 0:
                usable.append(row)
        for row in sorted(usable, key=lambda x: x["updated_at"]):
            self.edges.setdefault(row["base"], {})[row["quote"]] = D(row["rate"])
        for row in usable:
            with localcontext() as ctx:
                ctx.prec = 60
                self.edges.setdefault(row["quote"], {}).setdefault(
                    row["base"], D(1) / D(row["rate"])
                )

    def get(self, source: str, target: str) -> D:
        if source == target:
            return D(1)
        queue = deque([(source, D(1))])
        seen = {source}
        with localcontext() as ctx:
            ctx.prec = 60
            while queue:
                currency, result = queue.popleft()
                for other, rate in sorted(self.edges.get(currency, {}).items()):
                    if other == target:
                        return result * rate
                    if other not in seen:
                        seen.add(other)
                        queue.append((other, result * rate))
        raise DomainError("RATE_UNAVAILABLE", f"No fresh rate for {source}/{target}")
