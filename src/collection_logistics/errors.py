"""标本事件快处服务向 API 和 CLI 暴露的稳定错误。"""

from __future__ import annotations

from typing import Any, Mapping


class CollectionDispatchError(RuntimeError):
    code = "traffic_error"
    status = 400

    def __init__(self, message: str, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = None if details is None else dict(details)


class NotFound(CollectionDispatchError):
    code = "not_found"
    status = 404


class Conflict(CollectionDispatchError):
    code = "conflict"
    status = 409


class Forbidden(CollectionDispatchError):
    code = "forbidden"
    status = 403


class InvalidState(CollectionDispatchError):
    code = "invalid_state"
    status = 409


class ValidationFailed(CollectionDispatchError):
    code = "validation_failed"
    status = 422


class ResourceConstraint(CollectionDispatchError):
    """库存批次不满足任务的具体资源约束。"""

    code = "resource_constraint"
    status = 409
