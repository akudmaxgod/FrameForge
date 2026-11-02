"""Safe errors: never include request headers or provider error bodies."""
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .stats import Stats


class AnalysisError(RuntimeError):
    stats: "Stats | None" = None


class ConfigurationError(AnalysisError):
    pass


class ProviderError(AnalysisError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class BalanceError(ProviderError):
    pass


class ResponseError(AnalysisError):
    pass
