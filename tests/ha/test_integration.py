from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_twin.const import DOMAIN


def _rates(now, cheap_p=0.07, std_p=0.27):
    """Octopus-style rates attribute: 48 half-hours, GBP/kWh."""
    start = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    out = []
    for i in range(96):
        s = start + timedelta(minutes=30 * i)
        loc = dt_util.as_local(s)
        mins = loc.hour * 60 + loc.minute
        cheap = mins >= 23 * 60 + 30 or mins < 5 * 60 + 30
        out.append({"start": s.isoformat(), "end": (s + timedelta(minutes=30)).isoformat(),
                    "value_inc_vat": cheap_p if cheap else std_p})
    return out


async def test_config_flow_with_preset(hass: HomeAssistant):
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert r["type"] == "form" and r["step_id"] == "user"
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"hardware_preset": "tesla_powerwall_3"})
    assert r["step_id"] == "tariff_preset"
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"tariff_preset": "intelligent_go"})
    assert r["step_id"] == "sources_live"
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"power_entity": "sensor.mini"})
    assert r["step_id"] == "sources_octopus"
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
    assert r["step_id"] == "sources_solar_history"
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
    assert r["type"] == "create_entry"
    assert r["options"]["battery_kwh"] == 13.5 and r["options"]["power_entity"] == "sensor.mini"


async def _setup(hass, **extra):
    hass.config.time_zone = "Europe/London"
    hass.config.latitude, hass.config.longitude = 52.5, -1.5
    await hass.config.async_update(time_zone="Europe/London")
    hass.states.async_set("sensor.mini", "800", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.fc_now", "2.5", {"unit_of_measurement": "kW"})
    hass.states.async_set("sensor.price", "27.5", {"unit_of_measurement": "p/kWh"})
    hass.states.async_set("event.rates", "x", {"rates": _rates(None)})
    entry = MockConfigEntry(domain=DOMAIN, options={
        "power_entity": "sensor.mini", "pv_now_entity": "sensor.fc_now", "import_now_entity": "sensor.price",
        "import_rates_entities": ["event.rates"], "forecast_ref_kwp": 5.0,
        "battery_kwh": 13.5, "pv_kwp": 6.0, **extra})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_setup_entities_and_plan(hass: HomeAssistant):
    entry = await _setup(hass)
    c = entry.runtime_data
    assert c.plan is not None and c.plan.slots.n > 40
    assert c.diag["import_rate_slots"] >= 90 and c.diag["live_pv_entity"] and c.diag["live_price_entity"]
    soc = hass.states.get("sensor.home_energy_twin_battery_state_of_charge")
    assert soc is not None and float(soc.state) >= 0
    plan = hass.states.get("sensor.home_energy_twin_planned_overnight_charge_target")
    assert plan.state not in ("unknown", "unavailable")
    series = plan.attributes["series"]
    assert len(series["t"]) == len(series["battery_kw"]) == c.plan.slots.n
    # live PV: 2.5 kW from a 5 kWp reference array, scaled to 6 kWp -> 3.0 kW
    assert float(hass.states.get("sensor.home_energy_twin_solar_power").state) == 3.0
    assert hass.states.get("sensor.home_energy_twin_twin_mode").state
    d = hass.states.get("sensor.home_energy_twin_diagnostics")
    assert d is not None and d.attributes["diagnostics"]["import_rate_slots"] >= 90
    # every sensor the dashboard references must exist
    import re, pathlib
    txt = pathlib.Path("dashboard/energy_twin_dashboard.yaml").read_text()
    for eid in set(re.findall(r"\b((?:sensor|number|select|switch|button|datetime)\.home_energy_twin_[a-z_]+)", txt)):
        assert hass.states.get(eid) is not None, eid


async def test_controls_do_not_reload_and_preset_applies(hass: HomeAssistant):
    entry = await _setup(hass)
    c = entry.runtime_data
    await hass.services.async_call("number", "set_value",
                                   {"entity_id": "number.home_energy_twin_battery_capacity", "value": 20}, blocking=True)
    await hass.async_block_till_done()
    assert entry.options["battery_kwh"] == 20 and entry.runtime_data is c        # same coordinator => no reload
    await hass.services.async_call("select", "select_option", {
        "entity_id": "select.home_energy_twin_hardware_preset",
        "option": "Tesla Powerwall 3 (13.5 kWh, integrated hybrid inverter)"}, blocking=True)
    await hass.async_block_till_done()
    assert entry.options["battery_kwh"] == 13.5 and entry.runtime_data is c


async def test_events_service_changes_plan(hass: HomeAssistant):
    entry = await _setup(hass, power_down_export_p=60.0)
    c = entry.runtime_data
    now = dt_util.utcnow()
    await hass.services.async_call(DOMAIN, "add_event", {
        "kind": "power_down", "start": (now + timedelta(hours=3)).isoformat(),
        "end": (now + timedelta(hours=5)).isoformat()}, blocking=True)
    await hass.async_block_till_done()
    assert "power_down" in c.plan.slots.event
    assert hass.states.get("sensor.home_energy_twin_next_power_event").state.startswith("power_down")
    await hass.services.async_call(DOMAIN, "clear_events", {}, blocking=True)
    await hass.async_block_till_done()
    assert "power_down" not in c.plan.slots.event


async def test_annual_and_sweep(hass: HomeAssistant):
    entry = await _setup(hass, strategy="rules")
    c = entry.runtime_data
    await c._async_annual()
    a = c.annual
    assert a and a["saving_year1"] > 0 and a["finance"]["capex"] > 0 and len(a["monthly"]) >= 12
    await c.async_run_sweep([0, 10], [0, 6], None, "rules")
    assert len(c.sweep_rows) == 3 and not c.sweep_info["running"]
    best = hass.states.get("sensor.home_energy_twin_best_sweep_result")
    assert best.state not in ("unknown", "unavailable")


async def test_options_flow_menu(hass: HomeAssistant):
    entry = await _setup(hass)
    r = await hass.config_entries.options.async_init(entry.entry_id)
    assert r["type"] == "menu"
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "hardware"})
    assert r["step_id"] == "hardware"
    r = await hass.config_entries.options.async_configure(r["flow_id"], {
        "pv_kwp": 8, "inverter_kw": 8, "pv_annual_yield": 950, "battery_kwh": 16, "battery_max_kw": 8,
        "round_trip_eff": 0.9, "reserve_pct": 10, "export_limit_kw": 3.68, "cycle_cost_p": 0})
    assert r["type"] == "menu"
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "done"})
    assert r["type"] == "create_entry"
    await hass.async_block_till_done()
    assert hass.config_entries.async_get_entry(entry.entry_id).options["battery_kwh"] == 16


async def test_event_form_entities(hass: HomeAssistant):
    entry = await _setup(hass)
    c = entry.runtime_data
    start = (dt_util.utcnow() + timedelta(hours=4)).replace(microsecond=0)
    await hass.services.async_call("datetime", "set_value", {
        "entity_id": "datetime.home_energy_twin_event_start", "datetime": start.isoformat()}, blocking=True)
    await hass.services.async_call("datetime", "set_value", {
        "entity_id": "datetime.home_energy_twin_event_end", "datetime": (start + timedelta(hours=2)).isoformat()}, blocking=True)
    await hass.services.async_call("select", "select_option", {
        "entity_id": "select.home_energy_twin_event_type", "option": "Power Up (charge everything)"}, blocking=True)
    await hass.services.async_call("number", "set_value", {
        "entity_id": "number.home_energy_twin_event_rate_override", "value": 2.5}, blocking=True)
    await hass.services.async_call("button", "press", {"entity_id": "button.home_energy_twin_add_event"}, blocking=True)
    await hass.async_block_till_done()
    assert "power_up" in c.plan.slots.event
    assert c.manual_events[-1].import_p == 2.5 and c.manual_events[-1].export_p is None
    await hass.services.async_call("button", "press", {"entity_id": "button.home_energy_twin_clear_manual_events"}, blocking=True)
    await hass.async_block_till_done()
    assert not c.manual_events
