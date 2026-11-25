"""Persistent storage for RF Presence Sense devices, rooms, baselines, and live tuning."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)


class RFPresenceStorage:
    """Manages persistent JSON storage per config entry via Home Assistant Store."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}_{entry_id}"
        )
        self._lock = asyncio.Lock()

    async def async_load(self) -> dict[str, Any]:
        """Load persisted state dictionary, returning empty dict if absent."""
        async with self._lock:
            try:
                data = await self._store.async_load()
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Failed to load RF Presence Sense storage (%s): %s",
                    self._entry_id,
                    err,
                )
                return {}
            if not isinstance(data, dict):
                return {}
            return data

    async def async_save(self, data: dict[str, Any]) -> None:
        """Persist state dictionary safely with serialization lock."""
        async with self._lock:
            try:
                await self._store.async_save(data)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Failed to save RF Presence Sense storage (%s): %s",
                    self._entry_id,
                    err,
                )

    async def async_remove(self) -> None:
        """Remove persisted storage file on entry deletion."""
        async with self._lock:
            try:
                await self._store.async_remove()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Failed to remove RF Presence Sense storage (%s): %s",
                    self._entry_id,
                    err,
                )
