from __future__ import annotations

import re

# The local profile service validates `phone_number` against this pattern and answers 422 for
# anything else (notably 13-digit numbers, which this project's own submit API accepts). Kept
# in the domain layer so both the local client and the callback route share one definition.
LOCAL_PHONE_PATTERN = re.compile(r"^1[3-9]\d{9}$")


def is_local_supported_phone(value: str) -> bool:
    return bool(LOCAL_PHONE_PATTERN.match(value or ""))


def describe_phone_mismatch(value: str) -> str:
    """Explain why a phone will be rejected, for the worker's error_message."""
    if not value:
        return "phone_number is empty"
    if not value.isdigit():
        return f"phone_number {value!r} must be 11 digits (digits only, no '+' or other characters)"
    if len(value) != 11:
        return f"phone_number {value!r} must be 11 digits, got {len(value)}"
    return f"phone_number {value!r} must match ^1[3-9]\\d{{9}}$"
