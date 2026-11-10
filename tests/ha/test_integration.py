from datetime import timedelta

import pytest

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
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"name": "Home Energy Twin"})
    assert r["step_id"] == "hardware_preset"
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


async def _setup(hass, title="Home Energy Twin", **extra):
    hass.config.time_zone = "Europe/London"
    hass.config.latitude, hass.config.longitude = 52.5, -1.5
    await hass.config.async_update(time_zone="Europe/London")
    hass.states.async_set("sensor.mini", "800", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.fc_now", "2.5", {"unit_of_measurement": "kW"})
    hass.states.async_set("sensor.price", "27.5", {"unit_of_measurement": "p/kWh"})
    hass.states.async_set("event.rates", "x", {"rates": _rates(None)})
    entry = MockConfigEntry(domain=DOMAIN, title=title, options={
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


async def test_multiple_scenarios(hass: HomeAssistant):
    a = await _setup(hass, strategy="rules")
    b = await _setup(hass, title="Big battery", strategy="rules", battery_kwh=20, pv_kwp=8)
    assert a.entry_id != b.entry_id and a.runtime_data is not b.runtime_data
    # independent devices / entities per scenario
    assert hass.states.get("sensor.home_energy_twin_battery_state_of_charge") is not None
    assert hass.states.get("sensor.big_battery_battery_state_of_charge") is not None
    # form entities exist only for the primary scenario
    assert hass.states.get("select.home_energy_twin_event_type") is not None
    assert hass.states.get("select.big_battery_event_type") is None
    # independent virtual batteries and hardware
    assert b.runtime_data.hardware().battery_kwh == 20 and a.runtime_data.hardware().battery_kwh == 13.5
    await a.runtime_data._async_annual()
    await b.runtime_data._async_annual()
    sa = hass.states.get("sensor.home_energy_twin_annual_saving")
    sb = hass.states.get("sensor.big_battery_annual_saving")
    assert sa.attributes["scenario"] == "Home Energy Twin" and sb.attributes["scenario"] == "Big battery"
    assert sb.attributes["battery_kwh"] == 20 and float(sb.state) != float(sa.state)
    # a grid event added once reaches BOTH scenarios' plans
    now = dt_util.utcnow()
    await hass.services.async_call(DOMAIN, "add_event", {
        "kind": "power_down", "start": (now + timedelta(hours=3)).isoformat(),
        "end": (now + timedelta(hours=4)).isoformat()}, blocking=True)
    await hass.async_block_till_done()
    assert "power_down" in a.runtime_data.plan.slots.event and "power_down" in b.runtime_data.plan.slots.event
    # the dashboard's Compare tab discovers both scenarios and renders a table
    import pathlib, yaml
    from homeassistant.helpers.template import Template
    dash = yaml.safe_load(pathlib.Path("dashboard/energy_twin_dashboard.yaml").read_text())
    cards = next(v for v in dash["views"] if v["path"] == "compare")["cards"]
    out = Template(cards[0]["content"], hass).async_render()
    assert "**Big battery**" in out and "**Home Energy Twin**" in out
    rows = [l for l in out.splitlines() if l.startswith("| **")]
    assert len(rows) == 2
    # targeted service
    await hass.services.async_call(DOMAIN, "reset_twin", {"scenario": "Big battery"}, blocking=True)


async def test_config_flow_copy_from_existing(hass: HomeAssistant):
    a = await _setup(hass)
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert r["step_id"] == "user"
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"name": "Scenario 2", "copy_from": a.entry_id})
    assert r["step_id"] == "hardware_preset"
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"hardware_preset": "sigenstor_5kw_16kwh"})
    assert r["step_id"] == "hardware"          # copying skips entity pages, goes straight to tweak the kit
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {
        "pv_kwp": 6, "inverter_kw": 5, "pv_annual_yield": 950, "battery_kwh": 16, "battery_max_kw": 5,
        "round_trip_eff": 0.9, "reserve_pct": 10, "export_limit_kw": 3.68, "cycle_cost_p": 0})
    assert r["type"] == "create_entry" and r["title"] == "Scenario 2"
    assert r["options"]["power_entity"] == "sensor.mini" and r["options"]["battery_kwh"] == 16


async def test_vehicles_drive_the_ev_plan(hass: HomeAssistant):
    hass.states.async_set("binary_sensor.tesla_plug", "on")
    hass.states.async_set("device_tracker.tesla", "home")
    hass.states.async_set("sensor.tesla_soc", "40", {"unit_of_measurement": "%"})
    hass.states.async_set("sensor.leaf_status", "Disconnected")
    hass.states.async_set("device_tracker.leaf", "not_home")
    vehicles = [
        {"name": "Tesla", "mode": "iog", "plug_entity": "binary_sensor.tesla_plug", "home_entity": "device_tracker.tesla",
         "soc_entity": "sensor.tesla_soc", "capacity_kwh": 60, "target_soc_pct": 80, "charger_kw": 7.0},
        {"name": "Leaf", "mode": "offpeak", "plug_entity": "sensor.leaf_status", "home_entity": "device_tracker.leaf",
         "capacity_kwh": 40, "charger_kw": 7.0, "session_kwh": 12},
    ]
    entry = await _setup(hass, vehicles=vehicles)
    c = entry.runtime_data
    info = {n["vehicle"]: n for n in c.ev_info}
    assert info["Tesla"]["plugged"] is True and info["Tesla"]["home"] is True and info["Tesla"]["probability"] == 1.0
    assert info["Tesla"]["needed_kwh"] == round(24 / 0.9, 1)
    assert info["Leaf"]["plugged"] is False and info["Leaf"]["home"] is False and info["Leaf"]["probability"] == 0.5
    st = hass.states.get("sensor.home_energy_twin_ev_charging_expected_tonight")
    assert float(st.state) > 20 and len(st.attributes["vehicles"]) == 2
    assert any(x >= 6.9 for x in c.plan.slots.exp_ev_kw)            # the plugged-in car charges at full rate
    # a car that is plugged in later changes the plan: unplug the Tesla -> its load becomes only a probability
    hass.states.async_set("binary_sensor.tesla_plug", "off")
    await c.async_recalculate()
    assert {n["vehicle"]: n for n in c.ev_info}["Tesla"]["probability"] == 0.5


async def test_vehicle_options_flow(hass: HomeAssistant):
    entry = await _setup(hass)
    r = await hass.config_entries.options.async_init(entry.entry_id)
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "vehicles"})
    assert r["type"] == "menu" and "vehicle_edit" not in r["menu_options"]
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "vehicle_add"})
    assert r["step_id"] == "vehicle_add"
    r = await hass.config_entries.options.async_configure(r["flow_id"], {
        "name": "Kia", "mode": "iog", "capacity_kwh": 64, "target_soc_pct": 90, "charger_kw": 7.4, "session_kwh": 0,
        "plug_entity": "sensor.kia_plug", "plug_states": "ready, charging"})
    assert r["type"] == "menu" and "vehicle_edit" in r["menu_options"]
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "init"})
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "done"})
    await hass.async_block_till_done()
    v = hass.config_entries.async_get_entry(entry.entry_id).options["vehicles"]
    assert v[0]["name"] == "Kia" and v[0]["plug_states"] == "ready, charging"


async def test_sessions_are_learned_only_when_a_car_actually_charges(hass: HomeAssistant):
    hass.states.async_set("sensor.car_power", "7000", {"unit_of_measurement": "W"})
    hass.states.async_set("binary_sensor.car_plug", "on")
    entry = await _setup(hass, vehicles=[{"name": "Car", "mode": "offpeak", "power_entity": "sensor.car_power",
                                          "plug_entity": "binary_sensor.car_plug", "session_kwh": 0, "charger_kw": 7}])
    c = entry.runtime_data
    t = dt_util.utcnow()
    for k in range(60):                                       # one hour at 7 kW = 7 kWh
        c._track_vehicles(t + timedelta(minutes=k), 1 / 60)
    assert c.veh_rt["Car"]["delivered"] == pytest.approx(7.0, abs=0.2)
    assert not c.learn_live["Car"].sessions                   # still in progress
    hass.states.async_set("sensor.car_power", "0", {"unit_of_measurement": "W"})
    c._track_vehicles(t + timedelta(hours=5), 0.0)             # idle for > 3 h: session over
    assert c.learn_live["Car"].sessions == [pytest.approx(7.0, abs=0.2)]
    # a day with the car never plugged in is recorded too (it lowers that weekday's probability)
    hass.states.async_set("binary_sensor.car_plug", "off")
    c._track_vehicles(t + timedelta(days=1, hours=5), 0.0)
    assert sum(c.learn_live["Car"].wd_seen) >= 1


async def test_dashboard_vehicle_card_renders(hass: HomeAssistant):
    import pathlib, yaml
    from homeassistant.helpers.template import Template
    hass.states.async_set("sensor.leaf_status", "Connected: waiting for car demand")
    entry = await _setup(hass, vehicles=[{"name": "Leaf", "mode": "offpeak", "plug_entity": "sensor.leaf_status",
                                          "session_kwh": 12}])
    dash = yaml.safe_load(pathlib.Path("dashboard/energy_twin_dashboard.yaml").read_text())
    card = next(c for v in dash["views"] if v["path"] == "diagnostics" for c in v["cards"]
                if c.get("title") == "Vehicles (what the twin understood)")
    out = Template(card["content"], hass).async_render()
    assert "**Leaf**" in out and "True (Connected: waiting for car demand)" in out


async def test_live_twin_soc_moves_at_real_speed(hass: HomeAssistant):
    """Regression: a one-minute tick used to move the battery by a whole half-hour of power."""
    now = dt_util.utcnow()
    entry = await _setup(hass, battery_max_kw=10.0, inverter_kw=10.0, power_up_import_p=0.0)
    c = entry.runtime_data
    await hass.services.async_call(DOMAIN, "add_event", {
        "kind": "power_up", "start": (now - timedelta(hours=1)).isoformat(),
        "end": (now + timedelta(hours=3)).isoformat()}, blocking=True)     # forces max-rate grid charging
    await hass.async_block_till_done()
    hw = c.hardware()
    idx = c._plan_index(dt_util.utcnow())
    assert idx is not None and c.plan.slots.event[idx] == "power_up"
    c.soc = hw.emin
    t = dt_util.utcnow()
    c.last_tick = t - timedelta(seconds=60)
    c._step(t, hw, idx)
    gained = c.soc - hw.emin
    assert 0 < gained <= hw.charge_kw * (1 / 60) + 1e-6, gained            # <= ~0.16 kWh, was ~4.7 kWh
    for k in range(1, 15):                                                  # 15 min in total
        c._step(t + timedelta(minutes=k), hw, idx)
    assert c.soc < hw.emax * 0.5                                            # nowhere near full after 15 min
