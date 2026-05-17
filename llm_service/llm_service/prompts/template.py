"""Jinja2-backed prompt templates (optional extra)."""

from __future__ import annotations

from llm_service.core.models import Message
from llm_service.core.types import Role


class PromptTemplate:
    def __init__(self, template_str: str) -> None:
        try:
            from jinja2 import BaseLoader, Environment
        except ImportError as e:  # pragma: no cover
            from llm_service.core.exceptions import ImportExtraError

            raise ImportExtraError("Install templates extra: pip install 'llm-service[templates]'") from e
        self._env = Environment(loader=BaseLoader(), autoescape=False)
        self._tmpl = self._env.from_string(template_str)

    def render(self, **kwargs: object) -> str:
        return self._tmpl.render(**kwargs)

    def to_messages(self, system: str | None = None, **kwargs: object) -> list[Message]:
        user_content = self.render(**kwargs)
        messages: list[Message] = []
        if system:
            messages.append(Message(role=Role.SYSTEM, content=system))
        messages.append(Message(role=Role.USER, content=user_content))
        return messages
