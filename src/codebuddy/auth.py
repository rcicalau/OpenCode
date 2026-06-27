from __future__ import annotations

import getpass
import json
import os
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from .errors import ConfigError


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


def auth_check(config: dict, provider_name: str, poster: AuthPoster | None = None, timeout_seconds: int = 30) -> AuthResult:
    provider = config.get("model", {}).get("providers", {}).get(provider_name)
    if not isinstance(provider, dict):
        raise ConfigError(f"unknown provider: {provider_name}")
    env_var = provider_api_key_env(config, provider_name)
    api_key = normalize_api_key(os.environ.get(env_var, ""))
    if not api_key:
        raise ConfigError(f"{env_var} is missing in this process")
    base_url = provider.get("base_url") or (os.environ.get(str(provider.get("base_url_env"))) if provider.get("base_url_env") else None)
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
        message = f"{provider_name}: live auth check passed using {env_var}"
    elif status == 401:
        message = f"{provider_name}: {env_var} is set, but the provider rejected it with 401 invalid API key"
    else:
        message = f"{provider_name}: {env_var} is set, but provider check returned HTTP {status}"
    return AuthResult(provider_name, env_var, 200 <= status < 300, message)


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
