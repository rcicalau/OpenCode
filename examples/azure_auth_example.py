"""Example project-local Azure auth bridge for Code Buddy.

Copy this file to the root of the project you want Code Buddy to work on:

    auth.py

Then replace the body of ``AzureAuthClient.get_token`` with your real Azure
authentication code. Code Buddy loads this class with the default config value
``auth_client = "auth:AzureAuthClient"``.
"""


class AzureAuthClient:
    """Return a bearer token for the OpenAI-compatible endpoint.

    Code Buddy calls ``get_token()`` before each model request, so this method
    may refresh tokens when needed. It can return either a plain string token
    or an object with a ``token`` attribute.
    """

    def get_token(self):
        """Return the current Azure access token.

        Replace this example with the auth code used in your workspace.
        """
        raise NotImplementedError("Replace with your Azure token acquisition code.")
