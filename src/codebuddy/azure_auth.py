from __future__ import annotations

from .errors import ConfigError


class AzureAuthClient:
    """Project-local hook for Azure token acquisition.

    This default class deliberately does not contain environment-specific auth
    code. Replace ``get_token`` in your local clone, or point
    ``model.providers.azure_openai.auth_client`` at another import path.
    """

    def get_token(self):
        raise ConfigError(
            "AzureAuthClient is not configured. Edit "
            "C:\\Users\\RaduC\\Documents\\OpenCode\\src\\codebuddy\\azure_auth.py "
            "and implement AzureAuthClient.get_token(), or set "
            "model.providers.azure_openai.auth_client to your auth module."
        )
