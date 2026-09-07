class GenerationAlreadyRunning(Exception):
    """Raised when a conversation already has an active inference attempt."""


class AuthenticationFailed(Exception):
    """Raised when credentials cannot establish an authenticated principal."""


class ProviderAuthenticationError(Exception):
    """Raised when a federated identity provider cannot authenticate a user."""
