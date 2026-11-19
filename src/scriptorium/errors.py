class ScriptoriumError(Exception):
    code = "scriptorium_error"
    exit_code = 1


class ConfigurationError(ScriptoriumError):
    code = "configuration_error"
    exit_code = 2


class StateError(ScriptoriumError):
    code = "invalid_state"
    exit_code = 1


class DuplicateRunError(StateError):
    code = "duplicate_run"


class InfrastructureError(ScriptoriumError):
    code = "infrastructure_error"
    exit_code = 3


class NotFoundError(ScriptoriumError):
    code = "not_found"
    exit_code = 2


class BuildFailedError(InfrastructureError):
    """LaTeX ran and failed, or produced no PDF; unlike a missing tool, the manuscript text is the cause."""

    def __init__(self, message: str, log: str = "") -> None:
        super().__init__(message)
        self.log = log
