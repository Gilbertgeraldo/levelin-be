import logging
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("levelin")

DEFAULT_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_error",
    503: "service_unavailable",
}


DEFAULT_MESSAGES = {
    401: "Butuh autentikasi.",
    403: "Akses ditolak.",
    404: "Endpoint atau data tidak ditemukan.",
    405: "Metode HTTP tidak didukung untuk endpoint ini.",
    413: "Ukuran permintaan terlalu besar.",
    415: "Jenis konten tidak didukung.",
}


class ApiError(Exception):
    """Error yang dikirim ke klien dalam format {"error": {"code", "message"}}."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        details: Any = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        self.headers = headers


def not_found(what: str = "Data") -> ApiError:
    return ApiError(404, "not_found", f"{what} tidak ditemukan.")


def error_body(code: str, message: str, details: Any = None) -> dict:
    err: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        err["details"] = details
    return {"error": err}


async def _api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        error_body(exc.code, exc.message, exc.details),
        status_code=exc.status_code,
        headers=exc.headers,
    )


async def _http_error_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = DEFAULT_CODES.get(exc.status_code, "error")
    message = exc.detail if isinstance(exc.detail, str) else code
    if exc.status_code in DEFAULT_MESSAGES and message == HTTPStatus(exc.status_code).phrase:
        message = DEFAULT_MESSAGES[exc.status_code]
    return JSONResponse(
        error_body(code, message), status_code=exc.status_code, headers=getattr(exc, "headers", None)
    )


async def _validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    details = [
        {"loc": [str(p) for p in e.get("loc", [])], "message": e.get("msg", "")}
        for e in exc.errors()
    ]
    return JSONResponse(
        error_body("validation_error", "Data yang dikirim tidak valid.", details), status_code=400
    )


async def _unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error", exc_info=exc)
    return JSONResponse(
        error_body("internal_error", "Terjadi kesalahan di server."), status_code=500
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _api_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(Exception, _unhandled_error_handler)
