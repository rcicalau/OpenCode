from __future__ import annotations

from . import ai_mart


class AzureAuthClient:
    """Return Azure access tokens through the AI Mark auth client."""

    def get_token(self) -> str:
        return ai_mart.auth_client.authenticate_broker().access_token
