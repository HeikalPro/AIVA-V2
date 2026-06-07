from __future__ import annotations

from backend.auth.deps import UserContext

REQUESTER_CONTACT_MARKER = "\n\n--- Requester contact ---\n"


def requester_display_name(user: UserContext) -> str:
    name = " ".join(filter(None, [user.first_name, user.last_name])).strip()
    return name or user.email


def build_stored_description(description: str, user: UserContext, phone: str) -> str:
    return (
        f"{description.strip()}{REQUESTER_CONTACT_MARKER}"
        f"Name: {requester_display_name(user)}\n"
        f"Email: {user.email}\n"
        f"Phone: {phone.strip()}"
    )


def parse_stored_description(
    stored: str | None,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Return (kb_description, requester_name, requester_email, requester_phone)."""
    if not stored:
        return None, None, None, None
    if REQUESTER_CONTACT_MARKER not in stored:
        return stored, None, None, None

    kb, contact = stored.split(REQUESTER_CONTACT_MARKER, 1)
    name = email = phone = None
    for line in contact.strip().splitlines():
        if line.startswith("Name: "):
            name = line[6:].strip()
        elif line.startswith("Email: "):
            email = line[7:].strip()
        elif line.startswith("Phone: "):
            phone = line[7:].strip()
    return kb.strip() or None, name, email, phone
