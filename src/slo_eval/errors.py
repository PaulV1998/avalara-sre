"""The error raised for unusable input files, and how offending values are shown in it."""

import json
from collections.abc import Sequence

_MAX_SHOWN = 40


class InputError(Exception):
    def __init__(self, summary: str, problems: Sequence[str] = ()) -> None:
        super().__init__(summary)
        self.summary = summary
        self.problems = list(problems)

    def __str__(self) -> str:
        if not self.problems:
            return self.summary
        return "\n".join([f"{self.summary}:", *(f"  - {problem}" for problem in self.problems)])


def describe(value: object) -> str:
    """Show a decoded TOML or JSON value in an error message, shortened when long."""
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif value is None:
        text = "null"
    elif isinstance(value, str):
        text = json.dumps(value, ensure_ascii=False)
    elif isinstance(value, dict):
        text = "{...}" if value else "{}"
    elif isinstance(value, list):
        text = "[...]" if value else "[]"
    else:
        text = str(value)
    return text if len(text) <= _MAX_SHOWN else text[: _MAX_SHOWN - 3] + "..."
