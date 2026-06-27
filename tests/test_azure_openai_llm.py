from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codebuddy.auth import auth_check
from codebuddy.azure_openai_llm import AzureAuthOpenAIClient, load_auth_token
from codebuddy.errors import ConfigError
from codebuddy.llm import Message


class AzureOpenAILlmTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.created_clients = []
        self.created_http_clients = []
        self.old_modules = {name: sys.modules.get(name) for name in ("auth", "openai", "httpx")}

    def tearDown(self) -> None:
        for name, module in self.old_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        self.tmp.cleanup()

    def install_fake_openai_sdk(self) -> None:
        created_clients = self.created_clients
        created_http_clients = self.created_http_clients

        class FakeHttpClient:
            def __init__(self, *, verify=True, timeout=None):
                self.verify = verify
                self.timeout = timeout
                self.closed = False
                created_http_clients.append(self)

            def close(self):
                self.closed = True

        class FakeMessage:
            content = "done"

            def model_dump(self):
                return {
                    "content": "done",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "read_text", "arguments": "{\"path\":\"README.md\"}"},
                        }
                    ],
                }

        class FakeResponse:
            choices = [types.SimpleNamespace(message=FakeMessage())]

            def model_dump(self):
                return {"choices": [{"message": {"content": "done"}}]}

        class FakeOpenAI:
            def __init__(self, *, base_url, api_key, http_client):
                self.base_url = base_url
                self.api_key = api_key
                self.http_client = http_client
                self.closed = False
                self.requests = []
                created_clients.append(self)
                self.chat = types.SimpleNamespace(
                    completions=types.SimpleNamespace(create=self.create)
                )

            def create(self, **kwargs):
                self.requests.append(kwargs)
                return FakeResponse()

            def close(self):
                self.closed = True
                self.http_client.close()

        openai_module = types.ModuleType("openai")
        openai_module.OpenAI = FakeOpenAI
        httpx_module = types.ModuleType("httpx")
        httpx_module.Client = FakeHttpClient
        sys.modules["openai"] = openai_module
        sys.modules["httpx"] = httpx_module

    def test_adapter_uses_project_auth_client_token_and_preserves_tool_calls(self) -> None:
        self.install_fake_openai_sdk()
        (self.root / "auth.py").write_text(
            "class AzureAuthClient:\n"
            "    calls = 0\n"
            "    def get_token(self):\n"
            "        type(self).calls += 1\n"
            "        return 'azure-token'\n",
            encoding="utf-8",
        )
        client = AzureAuthOpenAIClient(
            base_url="https://aimark.example/openai/v1",
            model="openai/gpt-5.4",
            auth_client="auth:AzureAuthClient",
            project_root=self.root,
            verify_ssl=False,
            timeout_seconds=12,
        )

        response = client.complete([Message("user", "hello")], tools=[{"type": "function"}])

        self.assertEqual(response.content, "done")
        self.assertEqual(response.tool_calls[0].name, "read_text")
        self.assertEqual(response.tool_calls[0].arguments["path"], "README.md")
        self.assertEqual(self.created_clients[0].base_url, "https://aimark.example/openai/v1")
        self.assertEqual(self.created_clients[0].api_key, "azure-token")
        self.assertFalse(self.created_http_clients[0].verify)
        self.assertEqual(self.created_http_clients[0].timeout, 12)
        self.assertEqual(self.created_clients[0].requests[0]["model"], "openai/gpt-5.4")
        self.assertEqual(self.created_clients[0].requests[0]["messages"], [{"role": "user", "content": "hello"}])
        self.assertEqual(self.created_clients[0].requests[0]["tools"], [{"type": "function"}])
        self.assertTrue(self.created_clients[0].closed)

    def test_auth_check_loads_project_auth_client(self) -> None:
        (self.root / "auth.py").write_text(
            "class AzureAuthClient:\n"
            "    def get_token(self):\n"
            "        return 'project-token'\n",
            encoding="utf-8",
        )
        captured = {}

        result = auth_check(
            {
                "model": {
                    "roles": {"main": {"model": "openai/gpt-5.4"}},
                    "providers": {
                        "azure_openai": {
                            "base_url": "https://aimark.example/openai/v1",
                            "auth_client": "auth:AzureAuthClient",
                            "token_method": "get_token",
                            "model": "openai/gpt-5.4",
                        }
                    },
                }
            },
            "azure_openai",
            poster=lambda url, headers, payload, timeout: captured.update(
                {"url": url, "headers": headers, "payload": payload, "timeout": timeout}
            )
            or (200, "{}"),
            project_root=self.root,
        )

        self.assertTrue(result.persisted)
        self.assertEqual(captured["headers"]["Authorization"], "Bearer project-token")
        self.assertEqual(captured["url"], "https://aimark.example/openai/v1/chat/completions")
        self.assertEqual(captured["payload"]["model"], "openai/gpt-5.4")

    def test_bundled_default_auth_client_fails_with_setup_guidance(self) -> None:
        with self.assertRaises(ConfigError) as context:
            load_auth_token(auth_client_path="codebuddy.azure_auth:AzureAuthClient")

        self.assertIn("AzureAuthClient is not configured", str(context.exception))
        self.assertIn("src\\codebuddy\\azure_auth.py", str(context.exception))


if __name__ == "__main__":
    unittest.main()
