"""Example AI Mark auth client module for Code Buddy.

Copy this file over the bundled AI Mark auth placeholder:

    C:\Users\RaduC\Documents\OpenCode\src\codebuddy\ai_mart.py

Then replace ``auth_client`` and ``base_url`` with your real AI Mark values.
``codebuddy.azure_auth.AzureAuthClient.get_token()`` calls:

    auth_client.authenticate_broker().access_token
"""


class AidMartAuthClient:
    """Return broker auth results for the OpenAI-compatible endpoint."""

    def authenticate_broker(self):
        """Return an object with an ``access_token`` attribute."""
        raise NotImplementedError("Replace with your AI Mark Azure auth code.")


auth_client = AidMartAuthClient()
base_url = "https://your-endpoint/openai/v1"
