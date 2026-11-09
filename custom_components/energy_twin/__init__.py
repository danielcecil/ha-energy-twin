"""Home Energy Twin - a digital twin of your home's energy use with modelled solar + battery."""
from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .const import (DOMAIN, SERVICE_ADD_EVENT, SERVICE_CLEAR_EVENTS, SERVICE_RECALC, SERVICE_RESET_TWIN,
                    SERVICE_SWEEP)
from .coordinator import TwinCoordinator
from .sim.models import GridEvent

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR, Platform.NUMBER, Platform.DATETIME, Platform.SELECT, Platform.SWITCH, Platform.BUTTON]
type TwinConfigEntry = ConfigEntry[TwinCoordinator]

SCENARIO_SCHEMA = vol.Schema({vol.Optional("scenario"): cv.string})
ADD_EVENT_SCHEMA = vol.Schema({
    vol.Required("kind"): vol.In(["power_up", "power_down"]),
    vol.Required("start"): cv.datetime,
    vol.Required("end"): cv.datetime,
    vol.Optional("import_rate_p"): vol.Coerce(float),
    vol.Optional("export_rate_p"): vol.Coerce(float),
    vol.Optional("bonus_p"): vol.Coerce(float),
    vol.Optional("scenario"): cv.string,
})
SWEEP_SCHEMA = vol.Schema({
    vol.Optional("battery_kwh", default=[0, 5, 10, 13.5, 16]): vol.All(cv.ensure_list, [vol.Coerce(float)]),
    vol.Optional("pv_kwp", default=[0, 4, 6, 8]): vol.All(cv.ensure_list, [vol.Coerce(float)]),
    vol.Optional("inverter_kw"): vol.All(cv.ensure_list, [vol.Coerce(float)]),
    vol.Optional("strategy", default="rules"): vol.In(["rules", "optimised"]),
    vol.Optional("scenario"): cv.string,
})


def _targets(hass: HomeAssistant, call: ServiceCall, default_all: bool) -> list[TwinCoordinator]:
    """`scenario` (name or entry id) picks one; otherwise all scenarios, or just the first if default_all=False."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    want = (call.data.get("scenario") or "").strip().lower()
    if want:
        return [e.runtime_data for e in entries if want in (e.title.lower(), e.entry_id.lower())]
    picked = entries if default_all else entries[:1]
    return [e.runtime_data for e in picked]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    async def add_event(call: ServiceCall) -> None:
        # grid events apply to every scenario; any coordinator can add (the list is shared)
        if targets := _targets(hass, call, True):
            d = call.data
            await targets[0].async_add_event(GridEvent(d["kind"], dt_util.as_utc(d["start"]), dt_util.as_utc(d["end"]),
                                                       d.get("import_rate_p"), d.get("export_rate_p"), d.get("bonus_p")))

    async def clear_events(call: ServiceCall) -> None:
        if targets := _targets(hass, call, True):
            await targets[0].async_clear_events()

    async def sweep(call: ServiceCall) -> None:
        for c in _targets(hass, call, False):
            await c.async_run_sweep(call.data["battery_kwh"], call.data["pv_kwp"], call.data.get("inverter_kw"),
                                    call.data["strategy"])

    async def recalc(call: ServiceCall) -> None:
        for c in _targets(hass, call, True):
            await c.async_recalculate()

    async def reset(call: ServiceCall) -> None:
        for c in _targets(hass, call, True):
            await c.async_reset_twin()

    hass.services.async_register(DOMAIN, SERVICE_ADD_EVENT, add_event, schema=ADD_EVENT_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_CLEAR_EVENTS, clear_events, schema=SCENARIO_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_SWEEP, sweep, schema=SWEEP_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_RECALC, recalc, schema=SCENARIO_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_RESET_TWIN, reset, schema=SCENARIO_SCHEMA)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: TwinConfigEntry) -> bool:
    coordinator = TwinCoordinator(hass, entry)
    await coordinator.async_setup()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_options_updated))
    return True


async def _options_updated(hass: HomeAssistant, entry: TwinConfigEntry) -> None:
    # our own number/select/switch entities write options too - those must not reload the entry
    if entry.runtime_data.consume_internal_update():
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: TwinConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
