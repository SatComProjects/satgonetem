"""Fakes for external boundaries used by the test suite."""

from __future__ import annotations

from typing import Any


class FakeCommandExecutor:
    """Records commands and returns a configured result.

    This stands in for a Node/command port. It deliberately does not emulate
    shell parsing; tests assert the argv/command that orchestration produced.
    """

    def __init__(self, output: str = "", error: Exception | None = None) -> None:
        self.output = output
        self.error = error
        self.calls: list[tuple[Any, bool]] = []

    def execute(self, command: Any, detach: bool = False) -> str:
        self.calls.append((command, detach))
        if self.error is not None:
            raise self.error
        return "" if detach else self.output

    def __call__(self, command: Any, detach: bool = False) -> str:
        return self.execute(command, detach)


class FakeContainer:
    """Minimal Docker container double for Node command execution."""

    def __init__(
        self,
        *,
        pid: int = 1234,
        exit_code: int = 0,
        output: bytes = b"",
        exec_error: Exception | None = None,
    ) -> None:
        self.attrs = {"State": {"Pid": pid}}
        self.exit_code = exit_code
        self.output = output
        self.exec_error = exec_error
        self.calls: list[dict[str, Any]] = []

    def exec_run(self, **kwargs: Any) -> tuple[int, bytes]:
        self.calls.append(kwargs)
        if self.exec_error is not None:
            raise self.exec_error
        return self.exit_code, self.output


class FakeGrpcClient:
    """Records gRPC calls and returns configured responses by method name."""

    def __init__(self, **responses: Any) -> None:
        self.responses = responses
        self.calls: list[tuple[str, Any]] = []

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)

        def _call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, (args, kwargs)))
            response = self.responses.get(name)
            return response(*args, **kwargs) if callable(response) else response

        return _call
