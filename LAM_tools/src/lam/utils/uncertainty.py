from __future__ import annotations

import re
from dataclasses import dataclass

from .normalize import normalized_text


@dataclass(frozen=True, slots=True)
class UserConfirmation:
    field: str
    value: str
    raw: str


_CONFIRMATION = re.compile(r"^USER_CONFIRMED(?::\s*(.*))?$", re.I)


def parse_user_confirmations(value: object) -> tuple[UserConfirmation, ...]:
    """Parse both full and shorthand USER_CONFIRMED uncertainty lines."""
    confirmations: list[UserConfirmation] = []
    for raw_line in str(value or "").splitlines():
        line = raw_line.strip()
        match = _CONFIRMATION.match(line)
        if not match:
            continue
        payload = (match.group(1) or "").strip()
        field_match = re.search(r"(?:^|;)\s*field=([^;\r\n]+)", payload, re.I)
        value_match = re.search(r"(?:^|;)\s*value=([^;\r\n]*)", payload, re.I)
        field = field_match.group(1).strip() if field_match else "paper_identity"
        confirmed_value = value_match.group(1).strip() if value_match else ""
        confirmations.append(UserConfirmation(field, confirmed_value, raw_line.rstrip()))
    return tuple(confirmations)


def confirmation_for(value: object, field_name: str) -> UserConfirmation | None:
    key = normalized_text(field_name)
    return next(
        (
            item
            for item in parse_user_confirmations(value)
            if normalized_text(item.field) == key
        ),
        None,
    )


def has_user_confirmation(value: object, field_name: str) -> bool:
    return confirmation_for(value, field_name) is not None


def confirmed_value(value: object, field_name: str) -> str:
    confirmation = confirmation_for(value, field_name)
    return confirmation.value if confirmation else ""
