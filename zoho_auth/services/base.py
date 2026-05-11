"""Common base class for every service.

All services share two collaborators: a ``ZohoConfig`` (for endpoints and
credentials) and a ``Logger`` (for human-readable progress reporting). Putting
the storage of these in one place keeps service subclasses focused on their
own logic.
"""

from __future__ import annotations

from ..config import ZohoConfig
from ..logging import Logger


class Service:
    """Base class. Stores config and logger; do not instantiate directly."""

    def __init__(self, config: ZohoConfig, logger: Logger) -> None:
        self._config = config
        self._logger = logger

    @property
    def config(self) -> ZohoConfig:
        return self._config

    @property
    def logger(self) -> Logger:
        return self._logger
