"""Uniform error bodies: {"error": "<code>", "detail": "<text>"} (docs/API.md)."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from oyster.policy.catalogue import CatalogueError
from oyster.store import Conflict, NotFound

# Conflict codes that are not plain 409s
_CODE_STATUS = {
    "validation_failed": 422,
    "payload_too_large": 413,
    "not_found": 404,
    "rate_limited": 429,
}
_HTTP_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    422: "validation_failed",
    429: "rate_limited",
}


class ApiError(Exception):
    def __init__(self, status: int, code: str, detail: str = "", headers: dict | None = None):
        super().__init__(detail or code)
        self.status = status
        self.code = code
        self.detail = detail
        self.headers = headers


def error_response(status: int, code: str, detail: str = "", headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": code, "detail": detail[:500]}, status_code=status, headers=headers)


def status_for_code(code: str) -> int:
    return _CODE_STATUS.get(code, 409)


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError):
        return error_response(exc.status, exc.code, exc.detail, exc.headers)

    @app.exception_handler(NotFound)
    async def _not_found(_: Request, exc: NotFound):
        return error_response(404, "not_found", "unknown id")

    @app.exception_handler(Conflict)
    async def _conflict(_: Request, exc: Conflict):
        headers = {"Retry-After": "60"} if exc.code == "rate_limited" else None
        return error_response(status_for_code(exc.code), exc.code, exc.detail, headers)

    @app.exception_handler(CatalogueError)
    async def _catalogue(_: Request, exc: CatalogueError):
        return error_response(status_for_code(exc.code), exc.code, exc.detail)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        where = "/".join(str(p) for p in first.get("loc", ()))
        return error_response(422, "validation_failed", f"{where}: {first.get('msg', 'invalid request')}")

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException):
        code = _HTTP_CODES.get(exc.status_code, "error")
        detail = exc.detail if isinstance(exc.detail, str) else code
        return error_response(exc.status_code, code, detail, getattr(exc, "headers", None))
