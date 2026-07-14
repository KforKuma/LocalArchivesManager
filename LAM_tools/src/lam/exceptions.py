class LamError(Exception):
    """Base class for expected LAM failures."""


class ConfigurationError(LamError):
    """The library root or configuration is invalid."""


class CatalogueError(LamError):
    """The catalogue cannot be read, validated, or safely written."""


class FileOperationError(LamError):
    """A managed file operation is unsafe or failed."""

