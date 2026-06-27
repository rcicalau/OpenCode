from __future__ import annotations

import os
import re
from dataclasses import dataclass, field


SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?([A-Za-z0-9_\-./+=]{8,})"),
    re.compile(r"(?i)(bearer\s+)([A-Za-z0-9_\-./+=]{12,})"),
]


@dataclass(slots=True)
class Redactor:
    extra_values: list[str] = field(default_factory=list)

    def from_environment(self) -> "Redactor":
        values = []
        for key, value in os.environ.items():
            lowered = key.lower()
            if value and any(marker in lowered for marker in ("key", "token", "secret", "password")):
                if len(value) >= 8:
                    values.append(value)
        self.extra_values.extend(values)
        return self

    def redact(self, text: str) -> str:
        redacted = text
        for pattern in SECRET_PATTERNS:
            redacted = pattern.sub(lambda match: match.group(1) + "=<redacted>", redacted)
        for value in sorted(set(self.extra_values), key=len, reverse=True):
            if value:
                redacted = redacted.replace(value, "<redacted>")
        return redacted

