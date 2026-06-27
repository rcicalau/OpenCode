from __future__ import annotations

import getpass
import json
import os
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .errors import ConfigError
from .azure_openai_llm import DEFAULT_TOKEN_METHOD, load_auth_token, load_import_value


SecretPrompt = Callable[[str], str]
EnvWriter = Callable[[str, str], None]
AuthPoster = Callable[[str, dict[str, str], dict[str, Any], int], tuple[int, str]]


@dataclass(slots=True)
class AuthResult:
    provider: str
    env_var: str
    persisted: bool
    message: str


def normalize_api_key(value: str) -> str:
    normalized = value.strip().lstrip("\ufeff").strip()
    while len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in {"'", '"'}:
        normalized = normalized[1:-1].strip()
    return normalized


def provider_api_key_env(config: dict, provider_name: str) -> str:
    provider = config.get("model", {}).get("providers", {}).get(provider_name)
    if not isinstance(provider, dict):
        raise ConfigError(f"unknown provider: {provider_name}")
    env_var = provider.get("api_key_env")
    if not env_var:
        raise ConfigError(f"provider has no api_key_env: {provider_name}")
    return str(env_var)


def auth_status(config: dict, provider_name: str) -> AuthResult:
    provider = config.get("model", {}).get("providers", {}).get(provider_name)
    if not isinstance(provider, dict):
        raise ConfigError(f"unknown provider: {provider_name}")
    env_var = provider.get("api_key_env")
    if not env_var and provider.get("auth_client"):
        auth_client = str(provider["auth_client"])
        return AuthResult(
            provider=provider_name,
            env_var=auth_client,
            persisted=False,
            message=f"{provider_name}: auth client {auth_client} is configured",
        )
    env_var = provider_api_key_env(config, provider_name)
    is_set = bool(normalize_api_key(os.environ.get(env_var, "")))
    return AuthResult(
        provider=provider_name,
        env_var=env_var,
        persisted=False,
        message=f"{provider_name}: {env_var} is {'set' if is_set else 'missing'} in this process",
    )


def auth_set(
    config: dict,
    provider_name: str,
    prompt: SecretPrompt = getpass.getpass,
    writer: EnvWriter | None = None,
) -> AuthResult:
    env_var = provider_api_key_env(config, provider_name)
    value = normalize_api_key(prompt(f"{env_var}: "))
    if not value:
        raise ConfigError("empty API key was not saved")
    os.environ[env_var] = value
    (writer or persist_user_env_var)(env_var, value)
    return AuthResult(
        provider=provider_name,
        env_var=env_var,
        persisted=True,
        message=f"saved {env_var} as a Windows user environment variable; open a new terminal for future sessions",
    )


def auth_check(
    config: dict,
    provider_name: str,
    poster: AuthPoster | None = None,
    timeout_seconds: int = 30,
    project_root: Path | None = None,
) -> AuthResult:
    provider = config.get("model", {}).get("providers", {}).get(provider_name)
    if not isinstance(provider, dict):
        raise ConfigError(f"unknown provider: {provider_name}")
    env_var = provider.get("api_key_env")
    auth_client = provider.get("auth_client")
    if env_var:
        credential_label = str(env_var)
        api_key = normalize_api_key(os.environ.get(str(env_var), ""))
        if not api_key:
            raise ConfigError(f"{env_var} is missing in this process")
    elif auth_client:
        credential_label = str(auth_client)
        api_key = load_auth_token(
            auth_client_path=str(auth_client),
            token_method=str(provider.get("token_method", DEFAULT_TOKEN_METHOD)),
            project_root=project_root,
        ).value
    else:
        raise ConfigError(f"provider has no api_key_env or auth_client: {provider_name}")
    base_url = provider.get("base_url")
    if not base_url and provider.get("base_url_import"):
        base_url = load_import_value(str(provider["base_url_import"]), project_root)
    if not base_url and provider.get("base_url_env"):
        base_url = os.environ.get(str(provider["base_url_env"]))
    if not base_url:
        raise ConfigError(f"provider has no base_url: {provider_name}")
    endpoint_path = str(provider.get("endpoint_path", "/chat/completions"))
    model = str(provider.get("model") or config.get("model", {}).get("roles", {}).get("main", {}).get("model", "gpt-5.4"))
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with OK only."}],
        "max_tokens": 8,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    status, _body = (poster or _default_auth_post)(str(base_url).rstrip("/") + endpoint_path, headers, payload, timeout_seconds)
    if 200 <= status < 300:
        message = f"{provider_name}: live auth check passed using {credential_label}"
    elif status == 401:
        message = f"{provider_name}: {credential_label} is set, but the provider rejected it with 401 invalid API key"
    else:
        message = f"{provider_name}: {credential_label} is set, but provider check returned HTTP {status}"
    return AuthResult(provider_name, credential_label, 200 <= status < 300, message)


def _default_auth_post(url: str, headers: dict[str, str], payload: dict[str, Any], timeout_seconds: int) -> tuple[int, str]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        raise ConfigError(f"provider unavailable during auth check: {exc}") from exc


def persist_user_env_var(name: str, value: str) -> None:
    completed = subprocess.run(["setx", name, value], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if completed.returncode != 0:
        raise ConfigError(f"failed to persist {name}: {completed.stderr.strip() or completed.stdout.strip()}")
