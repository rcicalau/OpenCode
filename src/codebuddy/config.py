from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised only on Python < 3.11
    import tomli as tomllib


DEFAULT_CONFIG: dict[str, Any] = {
    "model": {
        "timeout_seconds": 300,
        "timeout_grace_seconds": 30,
        "roles": {
            "main": {
                "provider": "azure_openai",
                "model": "openai/gpt-5.4",
                "temperature": 0.2,
            },
            "compactor": {
                "provider": "azure_openai",
                "model": "openai/gpt-5.4",
                "temperature": 0.0,
            },
        },
        "providers": {
            "azure_openai": {
                "base_url_import": "ai_mart:base_url",
                "auth_client": "azure_auth:AzureAuthClient",
                "token_method": "get_token",
                "model": "openai/gpt-5.4",
                "verify_ssl": False,
                "auth_refresh_retries": 1,
            },
            "openai": {
                "base_url": "https://api.openai.com/v1",
                "endpoint_path": "/chat/completions",
                "api_key_env": "OPENAI_API_KEY",
            },
            "perplexity": {
                "base_url": "https://api.perplexity.ai",
                "endpoint_path": "/chat/completions",
                "api_key_env": "PERPLEXITY_API_KEY",
                "model": "sonar-pro",
            },
        },
    },
    "ui": {
        "multiline": True,
        "external_editor": True,
        "markdown_rendering": True,
    },
    "workspace": {
        "root_boundary": True,
        "extra_read_roots": [],
        "extra_write_roots": [],
        "sensitive_paths": [".env", ".env.*", "*.pem", "*.key"],
    },
    "commands": {
        "shell": "powershell",
        "default_timeout_seconds": 120,
        "max_output_chars": 20000,
        "yolo": False,
        "yolo_skips_confirm": True,
        "hard_deny_requires_final_approval": True,
        "network_allowed": False,
        "package_installs_require_confirmation": True,
    },
    "validation": {
        "commands": [],
        "targeted_first": True,
    },
    "agent": {
        "max_tool_iterations": 200,
        "max_work_items_per_prompt": 200,
        "max_item_attempts": 3,
        "no_progress_repeat_limit": 8,
        "rate_limit_retries": 4,
        "rate_limit_backoff_seconds": 2,
    },
    "git": {
        "agent_branch_required": True,
        "branch_prefix": "codebuddy/",
        "auto_checkpoint_commits": True,
        "commit_only_after_validation": True,
        "protected_branches": ["main", "master", "develop"],
    },
    "storage": {
        "store_full_transcript": True,
        "redact_secrets": True,
        "compact_max_tokens": 4000,
    },
    "tools": {
        "read": True,
        "search": True,
        "explore": True,
        "edit": True,
        "shell": True,
        "git": True,
        "validate": True,
        "index": True,
        "compact": True,
    },
}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def user_config_path() -> Path:
    return Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".buddy" / "config.toml"


def project_config_path(project_root: Path) -> Path:
    return project_root / ".buddy" / "config.toml"


@dataclass(slots=True)
class ConfigLoadResult:
    config: dict[str, Any]
    sources: list[Path] = field(default_factory=list)


def load_config(project_root: Path, global_path: Path | None = None) -> ConfigLoadResult:
    config = copy.deepcopy(DEFAULT_CONFIG)
    sources: list[Path] = []
    for path in [global_path or user_config_path(), project_config_path(project_root)]:
        if path.exists():
            try:
                with path.open("rb") as handle:
                    data = tomllib.load(handle)
            except tomllib.TOMLDecodeError as exc:
                raise ConfigError(f"invalid TOML in {path}: {exc}") from exc
            config = deep_merge(config, data)
            sources.append(path)
    validate_config(config)
    return ConfigLoadResult(config=config, sources=sources)


def validate_config(config: dict[str, Any]) -> None:
    required_sections = ["model", "workspace", "commands", "validation", "agent", "git", "storage", "tools"]
    for section in required_sections:
        if section not in config or not isinstance(config[section], dict):
            raise ConfigError(f"missing or invalid config section: {section}")
    timeout = config["commands"].get("default_timeout_seconds")
    if not isinstance(timeout, int) or timeout <= 0:
        raise ConfigError("commands.default_timeout_seconds must be a positive integer")
    model_timeout = config["model"].get("timeout_seconds", 300)
    if not isinstance(model_timeout, (int, float)) or model_timeout <= 0:
        raise ConfigError("model.timeout_seconds must be a positive number")
    model_timeout_grace = config["model"].get("timeout_grace_seconds", 30)
    if not isinstance(model_timeout_grace, (int, float)) or model_timeout_grace < 0:
        raise ConfigError("model.timeout_grace_seconds must be a non-negative number")
    validations = config["validation"].get("commands")
    if not isinstance(validations, list) or not all(isinstance(item, str) for item in validations):
        raise ConfigError("validation.commands must be a list of strings")
    max_tool_iterations = config["agent"].get("max_tool_iterations", 0)
    if not isinstance(max_tool_iterations, int) or max_tool_iterations < 0:
        raise ConfigError("agent.max_tool_iterations must be a non-negative integer")
    max_work_items_per_prompt = config["agent"].get("max_work_items_per_prompt", 200)
    if not isinstance(max_work_items_per_prompt, int) or max_work_items_per_prompt <= 0:
        raise ConfigError("agent.max_work_items_per_prompt must be a positive integer")
    max_item_attempts = config["agent"].get("max_item_attempts", 3)
    if not isinstance(max_item_attempts, int) or max_item_attempts <= 0:
        raise ConfigError("agent.max_item_attempts must be a positive integer")
    no_progress_repeat_limit = config["agent"].get("no_progress_repeat_limit", 8)
    if not isinstance(no_progress_repeat_limit, int) or no_progress_repeat_limit <= 0:
        raise ConfigError("agent.no_progress_repeat_limit must be a positive integer")
    rate_limit_retries = config["agent"].get("rate_limit_retries", 4)
    if not isinstance(rate_limit_retries, int) or rate_limit_retries < 0:
        raise ConfigError("agent.rate_limit_retries must be a non-negative integer")
    rate_limit_backoff_seconds = config["agent"].get("rate_limit_backoff_seconds", 2)
    if not isinstance(rate_limit_backoff_seconds, (int, float)) or rate_limit_backoff_seconds < 0:
        raise ConfigError("agent.rate_limit_backoff_seconds must be a non-negative number")
    compact_max_tokens = config["storage"].get("compact_max_tokens", 4000)
    if not isinstance(compact_max_tokens, int) or compact_max_tokens <= 0:
        raise ConfigError("storage.compact_max_tokens must be a positive integer")


def redact_config(config: dict[str, Any]) -> dict[str, Any]:
    clone = copy.deepcopy(config)
    for provider in clone.get("model", {}).get("providers", {}).values():
        if isinstance(provider, dict):
            if "api_key" in provider:
                provider["api_key"] = "<redacted>"
    return clone
