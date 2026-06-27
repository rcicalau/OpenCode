from __future__ import annotations


class CodeBuddyError(Exception):
    """Base class for expected Code Buddy errors."""


class ConfigError(CodeBuddyError):
    pass


class PolicyError(CodeBuddyError):
    pass


class ConfirmationRequired(PolicyError):
    def __init__(self, message: str, risk: str = "confirm") -> None:
        super().__init__(message)
        self.risk = risk


class DeniedByPolicy(PolicyError):
    pass


class FileSafetyError(CodeBuddyError):
    pass


class EditConflict(FileSafetyError):
    pass


class UndoError(CodeBuddyError):
    pass


class SessionRootMismatch(CodeBuddyError):
    pass
