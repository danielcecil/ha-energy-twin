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
PLATFORMS = [Platform.SENSOR, Platform.NUMBER, Platform.SELECT, Platform.SWITCH, Platform.BUTTON]
type TwinConfigEntry = ConfigEntry[TwinCoordinator]

ADD_EVENT_SCHEMA = vol.Schema({
    vol.Required("kind"): vol.In(["power_up", "power_down"]),
    vol.Required("start"): cv.datetime,
    vol.Required("end"): cv.datetime,
    vol.Optional("import_rate_p"): vol.Coerce(float),
    vol.Optional("export_rate_p"): vol.Coerce(float),
    vol.Optional("bonus_p"): vol.Coerce(float),
})
SWEEP_SCHEMA = vol.Schema({
    vol.Optional("battery_kwh", default=[0, 5, 10, 13.5, 16]): vol.All(cv.ensure_list, [vol.Coerce(float)]),
    vol.Optional("pv_kwp", default=[0, 4, 6, 8]): vol.All(cv.ensure_list, [vol.Coerce(float)]),
    vol.Optional("inverter_kw"): vol.All(cv.ensure_list, [vol.Coerce(float)]),
    vol.Optional("strategy", default="rules"): vol.In(["rules", "optimised"]),
})


def _coordinator(hass: HomeAssistant) -> TwinCoordinator | None:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    return entries[0].runtime_data if entries else None


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    async def add_event(call: ServiceCall) -> None:
        if (c := _coordinator(hass)) is None:
            return
        d = call.data
        await c.async_add_event(GridEvent(d["kind"], dt_util.as_utc(d["start"]), dt_util.as_utc(d["end"]),
                                          d.get("import_rate_p"), d.get("export_rate_p"), d.get("bonus_p")))

    async def clear_events(call: ServiceCall) -> None:
        if c := _coordinator(hass):
            await c.async_clear_events()

    async def sweep(call: ServiceCall) -> None:
        if c := _coordinator(hass):
            await c.async_run_sweep(call.data["battery_kwh"], call.data["pv_kwp"], call.data.get("inverter_kw"),
                                    call.data["strategy"])

    async def recalc(call: ServiceCall) -> None:
        if c := _coordinator(hass):
            await c.async_recalculate()

    async def reset(call: ServiceCall) -> None:
        if c := _coordinator(hass):
            await c.async_reset_twin()

    hass.services.async_register(DOMAIN, SERVICE_ADD_EVENT, add_event, schema=ADD_EVENT_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_CLEAR_EVENTS, clear_events)
    hass.services.async_register(DOMAIN, SERVICE_SWEEP, sweep, schema=SWEEP_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_RECALC, recalc)
    hass.services.async_register(DOMAIN, SERVICE_RESET_TWIN, reset)
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
