"""Home Energy Twin - a digital twin of your home's energy use with modelled solar + battery."""
from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import slugify
from homeassistant.util import dt as dt_util

from .const import (DOMAIN, SERVICE_ADD_EVENT, SERVICE_CLEAR_EVENTS, SERVICE_RECALC, SERVICE_RESET_TWIN,
                    SERVICE_SET_SOC, SERVICE_SWEEP)
from .coordinator import TwinCoordinator
from .sim.models import GridEvent

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR, Platform.NUMBER, Platform.DATETIME, Platform.SELECT, Platform.SWITCH, Platform.BUTTON]
type TwinConfigEntry = ConfigEntry[TwinCoordinator]

SCENARIO_SCHEMA = vol.Schema({vol.Optional("scenario"): cv.string})
SET_SOC_SCHEMA = vol.Schema({vol.Required("percent"): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
                             vol.Optional("scenario"): cv.string})
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

    async def set_soc(call: ServiceCall) -> None:
        for c in _targets(hass, call, False):
            await c.async_set_soc_pct(call.data["percent"])

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
    hass.services.async_register(DOMAIN, SERVICE_SET_SOC, set_soc, schema=SET_SOC_SCHEMA)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: TwinConfigEntry) -> bool:
    coordinator = TwinCoordinator(hass, entry)
    await coordinator.async_setup()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _normalise_entity_ids(hass, entry)
    _replan_when_cars_change(hass, entry, coordinator)
    _integrate_power(hass, entry, coordinator)
    entry.async_on_unload(entry.add_update_listener(_options_updated))
    return True


def _replan_when_cars_change(hass: HomeAssistant, entry: TwinConfigEntry, coordinator: TwinCoordinator) -> None:
    """A car being plugged in / unplugged / arriving / leaving, or its battery level or charge limit changing,
    re-plans straight away: the EV plan is built from the car's state now, not on a schedule."""
    ids = sorted({e for v in coordinator.vehicles
                  for e in (v.plug_entity, v.home_entity, v.soc_entity, v.target_soc_entity) if e})
    if not ids:
        return

    async def _changed(event) -> None:
        coordinator.plan_dirty = True
        await coordinator.async_request_refresh()

    entry.async_on_unload(async_track_state_change_event(hass, ids, _changed))


def _integrate_power(hass: HomeAssistant, entry: TwinConfigEntry, coordinator: TwinCoordinator) -> None:
    """Integrate the house power sensor on every reading, so spikes between our one-minute ticks are not missed."""
    from .const import CONF_POWER_ENTITY
    ent = coordinator.cfg.get(CONF_POWER_ENTITY)
    if not ent:
        return

    @callback
    def _reading(event) -> None:                      # synchronous: the reading is booked the instant it arrives
        coordinator.feed_power(ent, event.data.get("new_state"), event.time_fired)

    entry.async_on_unload(async_track_state_change_event(hass, [ent], _reading))


CANONICAL_NAME = "Home Energy Twin"


def _normalise_entity_ids(hass: HomeAssistant, entry: TwinConfigEntry) -> None:
    """Keep the first setup's entity IDs stable (home_energy_twin_*) whatever the setup is called.

    Home Assistant builds an entity ID from the device name when the entity is first created, so renaming a setup
    ("Home Energy Twin - PW3 13.5 kWh") makes every entity added afterwards come out with a different prefix and
    breaks anything that refers to them, including the shipped dashboard. IDs that are still the auto-generated
    ones are moved to the canonical form; an ID you set yourself, or one already taken, is never touched.
    """
    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries or entries[0].entry_id != entry.entry_id or entry.title == CANONICAL_NAME:
        return
    reg = er.async_get(hass)
    for ent in er.async_entries_for_config_entry(reg, entry.entry_id):
        name = ent.original_name
        if not name or not isinstance(name, str):
            continue
        auto = f"{ent.domain}.{slugify(f'{entry.title} {name}')}"
        canonical = f"{ent.domain}.{slugify(f'{CANONICAL_NAME} {name}')}"
        if ent.entity_id != auto or canonical == auto:
            continue                                                # customised by the user, or nothing to do
        if reg.async_get(canonical) is not None or hass.states.get(canonical) is not None:
            continue
        _LOGGER.info("Renaming %s to %s so the dashboard keeps working", ent.entity_id, canonical)
        reg.async_update_entity(ent.entity_id, new_entity_id=canonical)


async def _options_updated(hass: HomeAssistant, entry: TwinConfigEntry) -> None:
    # our own number/select/switch entities write options too - those must not reload the entry
    if entry.runtime_data.consume_internal_update():
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: TwinConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
