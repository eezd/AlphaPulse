"""普通 JSON 接口的唯一响应契约；流和 OpenAPI 不经过此包装。"""

import logging
from http import HTTPStatus

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException

logger = logging.getLogger(__name__)


class ApiResponse[T](BaseModel):
    code: int
    data: T
    message: str


def success[T](data: T) -> ApiResponse[T]:
    return ApiResponse(code=0, data=data, message="")


class ValidationIssue(BaseModel):
    loc: list[str | int]
    type: str
    msg: str


class ValidationErrors(BaseModel):
    errors: list[ValidationIssue]


async def http_error(request: Request, error: HTTPException) -> JSONResponse:
    message = error.detail if isinstance(error.detail, str) else HTTPStatus(error.status_code).phrase
    return JSONResponse({"code": error.status_code, "data": None, "message": message},
                        status_code=error.status_code, headers=error.headers)


async def validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
    # input/ctx 可能包含密码等原始输入，不进入公开响应。
    errors = [{key: item[key] for key in ("loc", "type", "msg")} for item in error.errors()]
    return JSONResponse({"code": 422, "data": {"errors": errors}, "message": "请求参数无效"}, status_code=422)


async def internal_error(request: Request, error: Exception) -> JSONResponse:
    logger.error("接口处理失败：%s %s", request.method, request.url.path,
                 exc_info=(type(error), error, error.__traceback__))
    return JSONResponse({"code": 500, "data": None, "message": "服务器内部错误"}, status_code=500)
