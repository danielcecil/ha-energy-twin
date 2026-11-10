"""Adapters: turn Home Assistant entities / services into plain data for the simulation.

Every parser is deliberately tolerant - the Octopus Energy integration's attribute names have
changed between releases, and you may be pointing at template sensors of your own. Anything
unrecognised is ignored and shown in the diagnostics sensor, never raised.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from .sim.models import GridEvent
from .sim.tariff import floor_half_hour

_LOGGER = logging.getLogger(__name__)
UTC = timezone.utc
_LIST_KEYS_RATES = ("rates", "all_rates", "current_day_rates", "next_day_rates", "data")
_LIST_KEYS_DISPATCH = ("planned_dispatches", "completed_dispatches", "dispatches", "current_dispatches")
_LIST_KEYS_EVENTS = ("joined_events", "available_events", "upcoming_events", "events", "planned_events")


def _dt(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return dt_util.as_utc(v)
    if isinstance(v, str):
        p = dt_util.parse_datetime(v)
        return dt_util.as_utc(p) if p else None
    return None


def to_float(state: State | None) -> float | None:
    if state is None or state.state in ("unknown", "unavailable", "", None):
        return None
    try:
        return float(state.state)
    except (ValueError, TypeError):
        return None


def power_kw(state: State | None) -> float | None:
    """Read a power sensor in W or kW, return kW."""
    v = to_float(state)
    if v is None:
        return None
    unit = (state.attributes.get("unit_of_measurement") or "").lower()
    return v if unit == "kw" else v / 1000.0


def price_p(state: State | None) -> float | None:
    """Read a price entity in GBP/kWh or p/kWh, return pence. Heuristic: units first, then magnitude."""
    v = to_float(state)
    if v is None:
        return None
    unit = (state.attributes.get("unit_of_measurement") or "").lower()
    if "gbp" in unit or "£" in unit:
        return v * 100.0
    if unit.startswith("p") or "pence" in unit or "gbx" in unit:
        return v
    return v * 100.0 if abs(v) < 2.0 else v


def parse_rates(states: list[State | None]) -> dict[datetime, float]:
    """Octopus 'rates' attribute lists -> {30-min UTC start: pence/kWh}."""
    out: dict[datetime, float] = {}
    for st in states:
        if st is None:
            continue
        for key in _LIST_KEYS_RATES:
            items = st.attributes.get(key)
            if not isinstance(items, list):
                continue
            for it in items:
                if not isinstance(it, dict):
                    continue
                s, e = _dt(it.get("start")), _dt(it.get("end"))
                val = it.get("value_inc_vat", it.get("value", it.get("rate")))
                if s is None or e is None or val is None:
                    continue
                try:
                    p = float(val)
                except (TypeError, ValueError):
                    continue
                p = p * 100.0 if abs(p) < 2.0 else p            # GBP -> pence (Octopus integration uses GBP)
                t = floor_half_hour(s)
                while t < e:
                    out[t] = p
                    t += timedelta(minutes=30)
            break
    return out


def parse_dispatches(state: State | None, now: datetime) -> list[tuple[datetime, datetime]]:
    if state is None:
        return []
    out: list[tuple[datetime, datetime]] = []
    for key in _LIST_KEYS_DISPATCH:
        items = state.attributes.get(key)
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict):
                    s, e = _dt(it.get("start")), _dt(it.get("end"))
                    if s and e and e > now - timedelta(hours=1):
                        out.append((s, e))
    if not out and state.state == "on":                          # binary sensor with no detail
        out.append((floor_half_hour(now), floor_half_hour(now) + timedelta(minutes=30)))
    return out


def parse_events(state: State | None, kind: str, now: datetime) -> list[GridEvent]:
    """Pull event windows from an entity: attribute lists, calendar attributes, or an 'on' state."""
    if state is None:
        return []
    out: list[GridEvent] = []
    for key in _LIST_KEYS_EVENTS:
        items = state.attributes.get(key)
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict):
                    s = _dt(it.get("start") or it.get("start_time") or it.get("event_start"))
                    e = _dt(it.get("end") or it.get("end_time") or it.get("event_end"))
                    if s and e and e > now:
                        out.append(GridEvent(kind, s, e))
    s = _dt(state.attributes.get("start_time") or state.attributes.get("event_start"))
    e = _dt(state.attributes.get("end_time") or state.attributes.get("event_end"))
    if s and e and e > now:
        out.append(GridEvent(kind, s, e))
    if not out and state.state == "on":
        out.append(GridEvent(kind, floor_half_hour(now), floor_half_hour(now) + timedelta(hours=1)))
    return out


def wh_period_to_kw(items: list[tuple[datetime, float]]) -> dict[datetime, float]:
    """Forecast.Solar `wh_period` -> {30-min UTC slot start: kW}.

    Verified against the forecast_solar library: each value is keyed by the END of its period (its own
    `energy_current_hour` sums keys in (hour start, hour end]). So the energy at key 09:00 is the 08:00-09:00
    hour. Keys that fall inside an hour (e.g. a sunrise timestamp) belong to the hour they end in.
    """
    hours: dict[datetime, float] = {}
    for t, wh in items:
        hs = (t - timedelta(microseconds=1)).astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        hours[hs] = hours.get(hs, 0.0) + float(wh)
    out: dict[datetime, float] = {}
    for hs, wh in hours.items():
        out[hs] = out[hs + timedelta(minutes=30)] = wh / 1000.0          # Wh over one hour == average W
    return out


async def forecast_pv_kw(hass: HomeAssistant, entry_id: str | None) -> dict[datetime, float]:
    """Return {30-min UTC slot start: kW} for the configured forecast integration's array.

    Works with any integration exposing HA's energy platform (Forecast.Solar, Solcast, Open-Meteo Solar
    Forecast...). Timestamps are interpreted as Forecast.Solar's period-end keys - see wh_period_to_kw.
    """
    from homeassistant.loader import async_get_integration

    if not entry_id:
        return {}
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        return {}
    try:
        integration = await async_get_integration(hass, entry.domain)
        platform = await integration.async_get_platform("energy")
        data = await platform.async_get_solar_forecast(hass, entry_id)
    except Exception as err:  # noqa: BLE001 - any integration quirk must not break the twin
        _LOGGER.warning("Solar forecast from %s failed: %s", entry.domain, err)
        return {}
    items = [(t, wh) for ts, wh in ((data or {}).get("wh_hours") or {}).items()
             if (t := _dt(ts)) is not None and wh is not None]
    return wh_period_to_kw(items)


_FC_KEYS = ("energy_production_today", "energy_production_today_remaining", "energy_production_tomorrow",
            "power_highest_peak_time_today", "power_highest_peak_time_tomorrow", "power_production_now",
            "energy_current_hour", "energy_next_hour")


def forecast_entities(hass: HomeAssistant, entry_id: str | None) -> dict[str, str]:
    """Find the Forecast.Solar sensors that belong to a config entry, so users needn't pick each one."""
    from homeassistant.helpers import entity_registry as er

    if not entry_id:
        return {}
    reg = er.async_get(hass)
    out: dict[str, str] = {}
    for ent in er.async_entries_for_config_entry(reg, entry_id):
        if ent.domain != "sensor":
            continue
        for key in _FC_KEYS:
            if ent.translation_key == key or (ent.unique_id or "").endswith("_" + key):
                out.setdefault(key, ent.entity_id)
    return out


def energy_kwh(state: State | None) -> float | None:
    """Read an energy sensor in Wh or kWh, return kWh."""
    v = to_float(state)
    if v is None:
        return None
    unit = (state.attributes.get("unit_of_measurement") or "").lower()
    return v / 1000.0 if unit == "wh" else v


def timestamp_state(state: State | None) -> datetime | None:
    return _dt(state.state) if state is not None and state.state not in ("unknown", "unavailable") else None


async def history_hourly(hass: HomeAssistant, stat_ids: list[str], days: int,
                         kind: str = "change") -> dict[str, list[tuple[datetime, float]]]:
    """Hourly long-term statistics per statistic id: 'change' (kWh) for energy, 'mean' for e.g. temperature."""
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import statistics_during_period

    if not stat_ids:
        return {}
    end = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)
    try:
        rows = await get_instance(hass).async_add_executor_job(
            statistics_during_period, hass, start, end, set(stat_ids), "hour", None, {kind}
        )
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Could not read statistics %s: %s", stat_ids, err)
        return {}
    out: dict[str, list[tuple[datetime, float]]] = {}
    for sid, lst in rows.items():
        series = []
        for r in lst:
            s = r["start"]
            s = datetime.fromtimestamp(s, UTC) if isinstance(s, (int, float)) else dt_util.as_utc(s)
            if r.get(kind) is not None:
                series.append((s, float(r[kind])))
        out[sid] = series
    return out


_CACHE = "energy_twin_cache"


async def cached(hass: HomeAssistant, key: tuple, ttl: float, factory):
    """Share an expensive lookup (forecast, statistics) between scenarios for `ttl` seconds."""
    store = hass.data.setdefault(_CACHE, {})
    ent = store.setdefault(key, {"t": None, "v": None, "lock": asyncio.Lock()})
    async with ent["lock"]:
        now = time.monotonic()
        if ent["t"] is None or now - ent["t"] > ttl:
            ent["v"] = await factory()
            ent["t"] = now
        return ent["v"]


async def forecast_temps(hass: HomeAssistant, weather_entity: str | None) -> dict[datetime, float]:
    """Hourly outdoor temperature (C) from a weather entity's forecast, keyed by 30-min UTC slot start."""
    if not weather_entity:
        return {}
    try:
        resp = await hass.services.async_call("weather", "get_forecasts", {"entity_id": weather_entity, "type": "hourly"},
                                              blocking=True, return_response=True)
    except Exception as err:  # noqa: BLE001 - not every weather entity supports hourly forecasts
        _LOGGER.debug("No hourly forecast from %s: %s", weather_entity, err)
        return {}
    state = hass.states.get(weather_entity)
    fahrenheit = bool(state and "f" in str(state.attributes.get("temperature_unit", "")).lower())
    out: dict[datetime, float] = {}
    for it in ((resp or {}).get(weather_entity) or {}).get("forecast") or []:
        t, temp = _dt(it.get("datetime")), it.get("temperature")
        if t is None or temp is None:
            continue
        c = (float(temp) - 32.0) * 5.0 / 9.0 if fahrenheit else float(temp)
        out[floor_half_hour(t)] = c
        out[floor_half_hour(t) + timedelta(minutes=30)] = c
    return out


_FORECAST_DOMAINS = ("forecast_solar", "solcast_solar", "open_meteo_solar_forecast")


def resolve_forecast_entry(hass: HomeAssistant, cfg: dict) -> str | None:
    """Which solar-forecast config entry to use: the chosen device, a legacy entry id, or the only one installed."""
    from homeassistant.helpers import device_registry as dr

    dev_id = cfg.get("forecast_device")
    if dev_id:
        dev = dr.async_get(hass).async_get(dev_id)
        if dev:
            for eid in dev.config_entries:
                e = hass.config_entries.async_get_entry(eid)
                if e and e.domain in _FORECAST_DOMAINS:
                    return eid
    if cfg.get("forecast_entry"):
        return cfg["forecast_entry"]
    for dom in _FORECAST_DOMAINS:                                  # nothing chosen: use it if there is exactly one
        entries = hass.config_entries.async_entries(dom)
        if len(entries) == 1:
            return entries[0].entry_id
    return None


def forecast_array_kwp(hass: HomeAssistant, entry_id: str | None) -> float | None:
    """The array size the forecast integration was set up with (Forecast.Solar stores it in Wp)."""
    entry = hass.config_entries.async_get_entry(entry_id) if entry_id else None
    if entry is None or entry.domain != "forecast_solar":
        return None
    wp = entry.options.get("modules_power")
    return float(wp) / 1000.0 if wp else None
