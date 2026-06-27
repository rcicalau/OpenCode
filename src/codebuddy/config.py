from __future__ import annotations

import copy
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError


DEFAULT_CONFIG: dict[str, Any] = {
    "model": {
        "timeout_seconds": 75,
        "roles": {
            "main": {
                "provider": "openai",
                "model": "gpt-5.4",
                "temperature": 0.2,
            },
            "compactor": {
                "provider": "openai",
                "model": "gpt-5.4",
                "temperature": 0.0,
            },
        },
        "providers": {
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
    },
    "tools": {
        "read": True,
        "search": True,
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
    return Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".pyagent" / "config.toml"


def project_config_path(project_root: Path) -> Path:
    return project_root / ".pyagent" / "config.toml"


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
    required_sections = ["model", "workspace", "commands", "validation", "git", "storage", "tools"]
    for section in required_sections:
        if section not in config or not isinstance(config[section], dict):
            raise ConfigError(f"missing or invalid config section: {section}")
    timeout = config["commands"].get("default_timeout_seconds")
    if not isinstance(timeout, int) or timeout <= 0:
        raise ConfigError("commands.default_timeout_seconds must be a positive integer")
    model_timeout = config["model"].get("timeout_seconds", 75)
    if not isinstance(model_timeout, (int, float)) or model_timeout <= 0:
        raise ConfigError("model.timeout_seconds must be a positive number")
    validations = config["validation"].get("commands")
    if not isinstance(validations, list) or not all(isinstance(item, str) for item in validations):
        raise ConfigError("validation.commands must be a list of strings")


def redact_config(config: dict[str, Any]) -> dict[str, Any]:
    clone = copy.deepcopy(config)
    for provider in clone.get("model", {}).get("providers", {}).values():
        if isinstance(provider, dict):
            if "api_key" in provider:
                provider["api_key"] = "<redacted>"
    return clone
