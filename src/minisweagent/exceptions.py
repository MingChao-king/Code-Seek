class ModelProtocolError(RuntimeError):
    """Raised when a provider response cannot be normalized safely."""


class ContextWindowExceeded(RuntimeError):
    """Raised when the provider rejects the rendered request as too large."""


class ModelTimeout(RuntimeError):
    """Raised when a model request exceeds its transport timeout."""
