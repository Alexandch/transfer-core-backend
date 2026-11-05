from typing import Any


class DomainError(Exception):
    def __init__(
        self, code: str, message: str, status: int = 422, details: dict[str, Any] | None = None
    ):
        self.code, self.message, self.status = code, message, status
        self.details = details or {}
        super().__init__(message)

    def body(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}
