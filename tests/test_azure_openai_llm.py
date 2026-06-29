from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codebuddy.auth import auth_check
import codebuddy.azure_openai_llm as azure_module
from codebuddy.azure_openai_llm import AzureAuthOpenAIClient, load_auth_token, load_import_value
from codebuddy.errors import ConfigError
from codebuddy.llm import Message


class AzureOpenAILlmTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.created_clients = []
        self.created_http_clients = []
        self.old_module_file = azure_module.__file__
        self.old_modules = {
            name: sys.modules.get(name)
            for name in ("auth", "ai_mart", "azure_auth", "broker", "broker.ai_mart", "openai", "httpx")
        }

    def tearDown(self) -> None:
        azure_module.__file__ = self.old_module_file
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

    def test_adapter_refreshes_auth_client_and_retries_once_after_unauthorized(self) -> None:
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

        class FakeUnauthorized(Exception):
            status_code = 401

        class FakeMessage:
            content = "done after refresh"

            def model_dump(self):
                return {"content": "done after refresh", "tool_calls": []}

        class FakeResponse:
            choices = [types.SimpleNamespace(message=FakeMessage())]

            def model_dump(self):
                return {"choices": [{"message": {"content": "done after refresh"}}]}

        class FakeOpenAI:
            def __init__(self, *, base_url, api_key, http_client):
                self.base_url = base_url
                self.api_key = api_key
                self.http_client = http_client
                self.closed = False
                created_clients.append(self)
                self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self.create))

            def create(self, **_kwargs):
                if self.api_key == "expired-token":
                    raise FakeUnauthorized("401 token expired")
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
        counter_path = self.root / "token-count.txt"
        (self.root / "auth.py").write_text(
            "from pathlib import Path\n"
            f"counter_path = Path({str(counter_path)!r})\n\n"
            "class AzureAuthClient:\n"
            "    def get_token(self):\n"
            "        count = int(counter_path.read_text()) if counter_path.exists() else 0\n"
            "        counter_path.write_text(str(count + 1))\n"
            "        return 'expired-token' if count == 0 else 'fresh-token'\n",
            encoding="utf-8",
        )
        client = AzureAuthOpenAIClient(
            base_url="https://aimark.example/openai/v1",
            model="openai/gpt-5.4",
            auth_client="auth:AzureAuthClient",
            project_root=self.root,
        )

        response = client.complete([Message("user", "hello")])

        self.assertEqual(response.content, "done after refresh")
        self.assertEqual([item.api_key for item in self.created_clients], ["expired-token", "fresh-token"])
        self.assertTrue(all(item.closed for item in self.created_clients))
        self.assertTrue(all(item.closed for item in self.created_http_clients))

    def test_adapter_stops_after_configured_auth_refresh_retries(self) -> None:
        created_clients = self.created_clients

        class FakeHttpClient:
            def __init__(self, *, verify=True, timeout=None):
                self.closed = False

            def close(self):
                self.closed = True

        class FakeUnauthorized(Exception):
            status_code = 401

        class FakeOpenAI:
            def __init__(self, *, base_url, api_key, http_client):
                self.api_key = api_key
                self.http_client = http_client
                self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self.create))
                created_clients.append(self)

            def create(self, **_kwargs):
                raise FakeUnauthorized("401 token expired")

            def close(self):
                self.http_client.close()

        openai_module = types.ModuleType("openai")
        openai_module.OpenAI = FakeOpenAI
        httpx_module = types.ModuleType("httpx")
        httpx_module.Client = FakeHttpClient
        sys.modules["openai"] = openai_module
        sys.modules["httpx"] = httpx_module
        (self.root / "auth.py").write_text(
            "class AzureAuthClient:\n"
            "    def get_token(self):\n"
            "        return 'still-expired-token'\n",
            encoding="utf-8",
        )
        client = AzureAuthOpenAIClient(
            base_url="https://aimark.example/openai/v1",
            model="openai/gpt-5.4",
            auth_client="auth:AzureAuthClient",
            project_root=self.root,
            auth_refresh_retries=1,
        )

        with self.assertRaises(FakeUnauthorized):
            client.complete([Message("user", "hello")])

        self.assertEqual(len(self.created_clients), 2)

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

    def test_auth_check_loads_base_url_from_project_import(self) -> None:
        (self.root / "ai_mart.py").write_text('base_url = "https://aimark.example/openai/v1"\n', encoding="utf-8")
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
                            "base_url_import": "ai_mart:base_url",
                            "auth_client": "auth:AzureAuthClient",
                            "token_method": "get_token",
                            "model": "openai/gpt-5.4",
                        }
                    },
                }
            },
            "azure_openai",
            poster=lambda url, headers, payload, timeout: captured.update({"url": url})
            or (200, "{}"),
            project_root=self.root,
        )

        self.assertTrue(result.persisted)
        self.assertEqual(captured["url"], "https://aimark.example/openai/v1/chat/completions")

    def test_provider_config_loads_base_url_from_project_import(self) -> None:
        (self.root / "ai_mart.py").write_text('base_url = "https://aimark.example/openai/v1"\n', encoding="utf-8")

        client = AzureAuthOpenAIClient.from_provider_config(
            {
                "base_url_import": "ai_mart:base_url",
                "auth_client": "azure_auth:AzureAuthClient",
                "token_method": "get_token",
                "model": "openai/gpt-5.4",
                "auth_refresh_retries": 2,
            },
            "openai/gpt-5.4",
            project_root=self.root,
        )

        self.assertEqual(client.base_url, "https://aimark.example/openai/v1")
        self.assertEqual(client.timeout_seconds, 300)
        self.assertEqual(client.auth_refresh_retries, 2)

    def test_provider_config_rejects_invalid_auth_refresh_retry_count(self) -> None:
        with self.assertRaises(ConfigError) as context:
            AzureAuthOpenAIClient.from_provider_config(
                {
                    "base_url": "https://aimark.example/openai/v1",
                    "auth_client": "azure_auth:AzureAuthClient",
                    "token_method": "get_token",
                    "auth_refresh_retries": -1,
                },
                "openai/gpt-5.4",
                project_root=self.root,
            )

        self.assertIn("auth_refresh_retries", str(context.exception))

    def test_provider_config_loads_base_url_from_project_src_import(self) -> None:
        src = self.root / "src"
        src.mkdir()
        (src / "ai_mart.py").write_text('base_url = "https://aimark-src.example/openai/v1"\n', encoding="utf-8")

        value = load_import_value("ai_mart:base_url", project_root=self.root)

        self.assertEqual(value, "https://aimark-src.example/openai/v1")

    def test_provider_config_loads_base_url_from_codebuddy_src_fallback(self) -> None:
        install_src = self.root / "install" / "src"
        package_dir = install_src / "codebuddy"
        project = self.root / "project"
        package_dir.mkdir(parents=True)
        project.mkdir()
        azure_module.__file__ = str(package_dir / "azure_openai_llm.py")
        (install_src / "ai_mart.py").write_text('base_url = "https://install-src.example/openai/v1"\n', encoding="utf-8")

        value = load_import_value("ai_mart:base_url", project_root=project)

        self.assertEqual(value, "https://install-src.example/openai/v1")

    def test_provider_config_loads_base_url_from_codebuddy_package_fallback(self) -> None:
        install_src = self.root / "install" / "src"
        package_dir = install_src / "codebuddy"
        project = self.root / "project"
        package_dir.mkdir(parents=True)
        project.mkdir()
        azure_module.__file__ = str(package_dir / "azure_openai_llm.py")
        (package_dir / "ai_mart.py").write_text('base_url = "https://package-src.example/openai/v1"\n', encoding="utf-8")

        value = load_import_value("ai_mart:base_url", project_root=project)

        self.assertEqual(value, "https://package-src.example/openai/v1")

    def test_project_src_package_imports_are_available(self) -> None:
        package = self.root / "src" / "broker"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "ai_mart.py").write_text('base_url = "https://pkg.example/openai/v1"\n', encoding="utf-8")

        value = load_import_value("broker.ai_mart:base_url", project_root=self.root)

        self.assertEqual(value, "https://pkg.example/openai/v1")

    def test_project_src_auth_client_can_import_src_sibling_module(self) -> None:
        src = self.root / "src"
        src.mkdir()
        (src / "ai_mart.py").write_text(
            "class AuthClient:\n"
            "    def authenticate_broker(self):\n"
            "        return type('Token', (), {'access_token': 'src-token'})()\n"
            "auth_client = AuthClient()\n",
            encoding="utf-8",
        )
        (src / "azure_auth.py").write_text(
            "from ai_mart import auth_client\n\n"
            "class AzureAuthClient:\n"
            "    def get_token(self):\n"
            "        return auth_client.authenticate_broker()\n",
            encoding="utf-8",
        )

        token = load_auth_token(auth_client_path="azure_auth:AzureAuthClient", project_root=self.root)

        self.assertEqual(token.value, "src-token")

    def test_external_default_auth_client_uses_access_token_contract(self) -> None:
        (self.root / "azure_auth.py").write_text(
            "class AzureAuthClient:\n"
            "    def get_token(self):\n"
            "        return type('Token', (), {'access_token': 'ai-mart-token'})()\n",
            encoding="utf-8",
        )

        token = load_auth_token(auth_client_path="azure_auth:AzureAuthClient", project_root=self.root)

        self.assertEqual(token.value, "ai-mart-token")

    def test_external_default_auth_client_fails_when_missing(self) -> None:
        with self.assertRaises(ConfigError) as context:
            load_auth_token(auth_client_path="azure_auth:AzureAuthClient", project_root=self.root)

        self.assertIn("could not import auth client module", str(context.exception))


if __name__ == "__main__":
    unittest.main()
