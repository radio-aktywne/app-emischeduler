class ServiceError(Exception):
    """Base class for service errors."""


class NotFoundError(ServiceError):
    """Raised when a resource is not found."""
