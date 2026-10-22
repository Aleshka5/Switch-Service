# SPDX-License-Identifier: Apache-2.0
"""API errors. ``code`` is stable; ``message`` is for humans."""

from __future__ import annotations

from typing import Any


class SwitchError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}

    def to_body(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = self.details
        return {"error": error}


class CatalogError(SwitchError):
    def __init__(self, message: str) -> None:
        super().__init__(400, "BAD_CATALOG", message)
