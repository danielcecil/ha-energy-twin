"""Buttons: recalculate, run the default size sweep, reset the virtual battery."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import TwinEntity


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    c = entry.runtime_data
    async_add_entities([TwinButton(c, "recalculate", c.async_recalculate),
                        TwinButton(c, "run_sweep", lambda: c.async_run_sweep([0, 5, 10, 13.5, 16], [0, 4, 6, 8], None)),
                        TwinButton(c, "reset_twin", c.async_reset_twin),
                        TwinButton(c, "add_event", c.async_add_event_from_form),
                        TwinButton(c, "clear_events", c.async_clear_events)])


class TwinButton(TwinEntity, ButtonEntity):
    def __init__(self, c, key, action) -> None:
        super().__init__(c, key)
        self._attr_translation_key = key
        self._action = action

    async def async_press(self) -> None:
        await self._action()
