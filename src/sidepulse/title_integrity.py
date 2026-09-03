from __future__ import annotations

import re


_REQUEST_SECTION_PATTERN = re.compile(
    r"^\s*#{1,6}\s*My request(?:\s+for\s+[^:\n]+)?\s*:\s*(?:\r?\n|$)",
    re.IGNORECASE | re.MULTILINE,
)
_PROTOCOL_WRAPPER_PATTERN = re.compile(
    r"\A\s*<(?P<root>task-notification|cross-session-message|"
    r"codex_delegation|realtime_delegation|task)\b[^>]*>"
    r"(?P<body>.*)</(?P=root)>\s*\Z",
    re.IGNORECASE | re.DOTALL,
)
_DELEGATION_INPUT_PATTERN = re.compile(
    r"<input\b[^>]*>(?P<input>.*?)</input>",
    re.IGNORECASE | re.DOTALL,
)
_FORBIDDEN_TAG_PATTERN = re.compile(
    r"</?\s*(?P<tag>task-notification|cross-session-message|codex_delegation|"
    r"realtime_delegation|in-app-browser-context|task|task-id|tool-use-id|"
    r"output-file|function_calls|tool_calls)\b[^>]*>",
    re.IGNORECASE,
)
_FORBIDDEN_TOKEN_PATTERN = re.compile(
    r"\b(?:task-notification|cross-session-message|codex_delegation|"
    r"realtime_delegation|in-app-browser-context|task-id|tool-use-id|"
    r"output-file|function_calls|tool_calls)\b",
    re.IGNORECASE,
)
_TEMP_OUTPUT_PATTERN = re.compile(
    r"(?:/private)?/tmp/[^\s<>'\"]*/tasks/[^\s<>'\"]+\.output\b|"
    r"/var/folders/[^\s<>'\"]+/T/[^\s<>'\"]+\.output\b",
    re.IGNORECASE,
)
_TOOL_METADATA_PATTERN = re.compile(
    r"\b(?:toolu_|call_)[a-z0-9_-]+\b|"
    r"\b(?:functions|tools)\.[a-z_][a-z0-9_]*\b|"
    r"\bscript running with cell id\b",
    re.IGNORECASE,
)
_PROTOCOL_ROOT_PATTERN = re.compile(
    r"\A\s*(?:[^:\n]{1,40}:\s*)?<\s*(?:task-notification|"
    r"cross-session-message|codex_delegation|realtime_delegation|"
    r"in-app-browser-context|task-id|tool-use-id|output-file|"
    r"function_calls|tool_calls)\b",
    re.IGNORECASE,
)


def normalize_user_request(value: object) -> str | None:
    """Return genuine request text, excluding whole internal envelopes."""
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    markers = tuple(_REQUEST_SECTION_PATTERN.finditer(text))
    if markers:
        text = text[markers[-1].end():].strip()
    if not text:
        return None

    wrapper = _PROTOCOL_WRAPPER_PATTERN.fullmatch(text)
    if wrapper is None:
        return text

    root = wrapper.group("root").casefold()
    if root in {"task-notification", "cross-session-message"}:
        return None
    if root in {"codex_delegation", "realtime_delegation", "task"}:
        delegated = _DELEGATION_INPUT_PATTERN.search(wrapper.group("body"))
        if delegated is not None:
            return delegated.group("input").strip() or None
        if root != "task":
            return None
    return wrapper.group("body").strip() or None


def is_readable_session_title(value: object) -> bool:
    """Reject transport/protocol material at every title acceptance boundary."""
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text or not any(character.isalpha() for character in text):
        return False
    return not (
        _FORBIDDEN_TAG_PATTERN.search(text)
        or _FORBIDDEN_TOKEN_PATTERN.search(text)
        or _TEMP_OUTPUT_PATTERN.search(text)
        or _TOOL_METADATA_PATTERN.search(text)
    )


def humanize_title_text(value: object) -> str | None:
    """Make genuine prose title-safe without salvaging protocol payloads."""
    text = normalize_user_request(value)
    if not text or _PROTOCOL_ROOT_PATTERN.match(text):
        return None

    replacements = {
        "task-notification": "task notification",
        "cross-session-message": "cross-session message",
        "codex_delegation": "Codex delegation",
        "realtime_delegation": "realtime delegation",
        "in-app-browser-context": "browser context",
        "task": "task",
        "task-id": "task ID",
        "tool-use-id": "tool-use ID",
        "output-file": "output file",
        "function_calls": "function calls",
        "tool_calls": "tool calls",
    }
    text = _TEMP_OUTPUT_PATTERN.sub("temporary output", text)
    text = _TOOL_METADATA_PATTERN.sub("tool command", text)
    text = _FORBIDDEN_TAG_PATTERN.sub(
        lambda match: (
            " "
            if match.group(0).lstrip().startswith("</")
            else f" {replacements[match.group('tag').casefold()]} "
        ),
        text,
    )
    text = _FORBIDDEN_TOKEN_PATTERN.sub(
        lambda match: replacements[match.group(0).casefold()], text
    )
    text = re.sub(r"\s+", " ", text).strip(" -:\n\t")
    return text if is_readable_session_title(text) else None
