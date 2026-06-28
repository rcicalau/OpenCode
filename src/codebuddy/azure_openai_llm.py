from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .llm import LLMResponse, Message
from .tool_calls import parse_native_tool_calls


DEFAULT_AUTH_CLIENT = "azure_auth:AzureAuthClient"
DEFAULT_TOKEN_METHOD = "get_token"


class AzureAuthOpenAIClient:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        auth_client: str = DEFAULT_AUTH_CLIENT,
        token_method: str = DEFAULT_TOKEN_METHOD,
        project_root: Path | None = None,
        verify_ssl: bool = False,
        timeout_seconds: int = 75,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.auth_client_path = auth_client
        self.token_method = token_method
        self.project_root = project_root
        self.verify_ssl = verify_ssl
        self.timeout_seconds = timeout_seconds
        self._auth_client = None

    @classmethod
    def from_provider_config(
        cls,
        provider: dict[str, Any],
        model: str,
        *,
        project_root: Path | None = None,
    ) -> "AzureAuthOpenAIClient":
        base_url = provider.get("base_url")
        base_url_import = provider.get("base_url_import")
        if not base_url and base_url_import:
            base_url = load_import_value(str(base_url_import), project_root)
        if not base_url:
            raise ConfigError(f"missing provider base_url; configure {base_url_import or 'base_url'}")
        timeout_seconds = provider.get("timeout_seconds", 75)
        if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            raise ConfigError("provider timeout_seconds must be a positive number")
        return cls(
            base_url=str(base_url),
            model=str(provider.get("model", model)),
            auth_client=str(provider.get("auth_client", DEFAULT_AUTH_CLIENT)),
            token_method=str(provider.get("token_method", DEFAULT_TOKEN_METHOD)),
            project_root=project_root,
            verify_ssl=bool(provider.get("verify_ssl", False)),
            timeout_seconds=int(timeout_seconds),
        )

    def complete(self, messages: list[Message], tools: list[dict[str, Any]] | None = None) -> LLMResponse:
        http_client = None
        client = None
        try:
            from openai import OpenAI
            import httpx

            http_client = httpx.Client(verify=self.verify_ssl, timeout=self.timeout_seconds)
            client = OpenAI(base_url=self.base_url, api_key=self._token(), http_client=http_client)
            response = client.chat.completions.create(
                model=self.model,
                messages=[{"role": msg.role, "content": msg.content} for msg in messages],
                tools=tools or None,
            )
        finally:
            if client is not None and hasattr(client, "close"):
                client.close()
            elif http_client is not None and hasattr(http_client, "close"):
                http_client.close()

        message = response.choices[0].message
        message_dict = _model_dump(message)
        raw = _model_dump(response)
        content = message_dict.get("content")
        return LLMResponse(
            content="" if content is None else str(content),
            raw=raw,
            tool_calls=parse_native_tool_calls(message_dict, tolerate_malformed=True),
        )

    def _token(self) -> str:
        token = load_auth_token(
            auth_client_path=self.auth_client_path,
            token_method=self.token_method,
            project_root=self.project_root,
            existing_client=self._auth_client,
        )
        self._auth_client = token.client
        return token.value


class AuthToken:
    def __init__(self, value: str, client: Any) -> None:
        self.value = value
        self.client = client


def load_auth_token(
    *,
    auth_client_path: str,
    token_method: str = DEFAULT_TOKEN_METHOD,
    project_root: Path | None = None,
    existing_client: Any = None,
) -> AuthToken:
    auth_client = existing_client or _load_auth_client(auth_client_path, project_root)
    method = getattr(auth_client, token_method, None)
    if not callable(method):
        raise ConfigError(f"auth client {auth_client_path} has no callable {token_method} method")
    raw_token = method()
    token_value = getattr(raw_token, "access_token", getattr(raw_token, "token", raw_token))
    if not token_value:
        raise ConfigError(f"auth client {auth_client_path}.{token_method} returned an empty token")
    return AuthToken(str(token_value), auth_client)


def load_import_value(import_path: str, project_root: Path | None = None) -> Any:
    module_name, value_name = _split_import_path(import_path)
    module_file = (project_root / f"{module_name}.py") if project_root and "." not in module_name else None
    if module_file and module_file.exists():
        module = _load_module_from_file(module_name, module_file)
    else:
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            raise ConfigError(f"could not import {module_name!r} for {import_path}: {exc}") from exc
    if not hasattr(module, value_name):
        raise ConfigError(f"import value not found: {import_path}")
    return getattr(module, value_name)


def _load_auth_client(auth_client_path: str, project_root: Path | None) -> Any:
    module_name, class_name = _split_import_path(auth_client_path)
    auth_file = (project_root / f"{module_name}.py") if project_root and "." not in module_name else None
    if auth_file and auth_file.exists():
        module = _load_module_from_file(module_name, auth_file)
    else:
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            raise ConfigError(f"could not import auth client module {module_name!r}: {exc}") from exc
    auth_class = getattr(module, class_name, None)
    if auth_class is None:
        raise ConfigError(f"auth client class not found: {auth_client_path}")
    return auth_class()


def _split_import_path(path: str) -> tuple[str, str]:
    if ":" in path:
        module_name, class_name = path.split(":", 1)
    else:
        module_name, _, class_name = path.rpartition(".")
    if not module_name or not class_name:
        raise ConfigError("auth_client must look like 'module:ClassName'")
    return module_name, class_name


def _load_module_from_file(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(f"_codebuddy_project_{module_name}_{abs(hash(path))}", path)
    if spec is None or spec.loader is None:
        raise ConfigError(f"could not load auth client module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _model_dump(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        return dumped if isinstance(dumped, dict) else {}
    if isinstance(value, dict):
        return value
    return {
        "content": getattr(value, "content", None),
        "tool_calls": getattr(value, "tool_calls", None),
    }
