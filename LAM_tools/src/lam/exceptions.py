class LamError(Exception):
    """Base class for expected LAM failures."""


class ConfigurationError(LamError):
    """The library root or configuration is invalid."""


class CatalogueError(LamError):
    """The catalogue cannot be read, validated, or safely written."""


class FileOperationError(LamError):
    """A managed file operation is unsafe or failed."""


class NetworkError(LamError):
    """A provider request failed after bounded retries."""


class ProviderError(LamError):
    """A provider response is invalid or cannot be parsed safely."""


class CacheError(LamError):
    """The metadata cache cannot be read or written safely."""
