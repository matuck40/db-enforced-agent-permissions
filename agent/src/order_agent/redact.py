"""Strip secrets from text before it leaves the process (logs, errors, traces)."""

import re

REDACTED = "[REDACTED]"

# OpenAI (sk-, sk-proj-), Anthropic (sk-ant-) and Langfuse (pk-lf-, sk-lf-) keys.
# The 16-character minimum keeps prose such as "sk-" or "task-list" untouched.
_KEY = re.compile(r"(?<![A-Za-z0-9_-])(?:sk|pk)-[A-Za-z0-9_-]{16,}")
# Only the password of scheme://user:password@host is replaced.
_DB_PASSWORD = re.compile(r"(postgres(?:ql)?://[^:/@\s]+:)[^@\s]+(@)")


def redact(text: str) -> str:
    text = _DB_PASSWORD.sub(rf"\1{REDACTED}\2", text)
    return _KEY.sub(REDACTED, text)
