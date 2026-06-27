from __future__ import annotations

from .errors import ConfigError


class _UnconfiguredAiMartAuthClient:
    def authenticate_broker(self):
        raise ConfigError(
            "codebuddy.ai_mart.auth_client is not configured. Replace "
            "C:\\Users\\RaduC\\Documents\\OpenCode\\src\\codebuddy\\ai_mart.py "
            "with your AI Mark auth client module, or assign auth_client to an object "
            "with authenticate_broker().access_token."
        )


auth_client = _UnconfiguredAiMartAuthClient()
base_url = ""
