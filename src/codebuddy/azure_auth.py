from __future__ import annotations

from .aid_mart import auth_client


class AzureAuthClient:
    """Return Azure access tokens through the AI Mark auth client."""

    def get_token(self) -> str:
        return auth_client.authenticate_broker().access_token
