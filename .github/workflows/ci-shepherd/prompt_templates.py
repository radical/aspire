"""Reviewable launch instructions with strict, nonrecursive substitutions."""

from pathlib import Path
import re

from reasoning import render


DIRECTORY = Path(__file__).parent / "prompts"


def load(name, **values):
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z-]*", name):
        raise ValueError("invalid prompt template name")
    # One final LF belongs to the text file, not the historically rendered
    # prompt. Internal newlines and trailing spaces remain significant.
    template = (DIRECTORY / f"{name}.md").read_text(encoding="utf-8").removesuffix("\n")
    # A backslash-newline wraps reviewable prose without changing its rendered
    # whitespace. Ordinary newlines still delimit correlation and JSON sections.
    template = template.replace("\\\n", "")
    return render(template, **values)
