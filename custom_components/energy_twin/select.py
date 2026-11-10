"""Select entities: strategy and hardware preset."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_FIN_MODE, CONF_HW_PRESET, CONF_STRATEGY, CONF_TERM, DEFAULTS, STRATEGIES
from .entity import TwinEntity
from .sim.presets import HARDWARE_PRESETS, apply_preset, preset_choices


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    c = entry.runtime_data
    async_add_entities([StrategySelect(c), PresetSelect(c), EventKindSelect(c), FinanceModeSelect(c)])


class StrategySelect(TwinEntity, SelectEntity):
    _attr_translation_key = "strategy"

    def __init__(self, c) -> None:
        super().__init__(c, "strategy")
        self._by_label = {v: k for k, v in STRATEGIES.items()}
        self._attr_options = list(STRATEGIES.values())

    @property
    def current_option(self):
        return STRATEGIES.get(self.coordinator.cfg[CONF_STRATEGY])

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_options({CONF_STRATEGY: self._by_label[option]})


class PresetSelect(TwinEntity, SelectEntity):
    """Choosing a preset overwrites the hardware numbers (they stay editable afterwards)."""

    _attr_translation_key = "hardware_preset"

    def __init__(self, c) -> None:
        super().__init__(c, "hardware_preset")
        labels = preset_choices(HARDWARE_PRESETS)
        self._by_label = {v: k for k, v in labels.items()}
        self._attr_options = list(labels.values())

    @property
    def current_option(self):
        return preset_choices(HARDWARE_PRESETS).get(self.coordinator.cfg.get(CONF_HW_PRESET, "custom"))

    async def async_select_option(self, option: str) -> None:
        name = self._by_label[option]
        await self.coordinator.async_set_options({CONF_HW_PRESET: name, **apply_preset(HARDWARE_PRESETS, name)})


class EventKindSelect(TwinEntity, SelectEntity):
    """Form field for the dashboard's 'add an event' card."""

    _attr_translation_key = "event_kind"
    _LABELS = {"power_down": "Power Down (discharge + export)", "power_up": "Power Up (charge everything)"}

    def __init__(self, c) -> None:
        super().__init__(c, "event_kind")
        self._attr_entity_registry_enabled_default = c.is_primary
        self._by_label = {v: k for k, v in self._LABELS.items()}
        self._attr_options = list(self._LABELS.values())

    @property
    def current_option(self):
        return self._LABELS[self.coordinator.form["kind"]]

    async def async_select_option(self, option: str) -> None:
        self.coordinator.form["kind"] = self._by_label[option]
        self.async_write_ha_state()


class FinanceModeSelect(TwinEntity, SelectEntity):
    _attr_translation_key = "finance_mode"
    _LABELS = {"cash": "Pay in cash", "loan": "Loan (interest rate)", "fixed_monthly": "Fixed monthly payment"}

    def __init__(self, c) -> None:
        super().__init__(c, "finance_mode")
        self._by_label = {v: k for k, v in self._LABELS.items()}
        self._attr_options = list(self._LABELS.values())

    @property
    def current_option(self):
        cfg = self.coordinator.cfg
        return self._LABELS.get(cfg.get(CONF_FIN_MODE) or ("loan" if cfg[CONF_TERM] > 0 else "cash"))

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_options({CONF_FIN_MODE: self._by_label[option]})
