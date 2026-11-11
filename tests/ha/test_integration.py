from datetime import timedelta, timezone

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
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "menu_kit"})
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "hardware"})
    assert r["step_id"] == "hardware"
    r = await hass.config_entries.options.async_configure(r["flow_id"], {
        "pv_kwp": 8, "inverter_kw": 8, "pv_annual_yield": 950, "battery_kwh": 16, "battery_max_kw": 8,
        "round_trip_eff": 0.9, "reserve_pct": 10, "export_limit_kw": 3.68, "cycle_cost_p": 0})
    assert r["type"] == "menu" and r["step_id"] == "menu_kit"                   # back in the section it came from
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "init"})
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
    assert info["Tesla"]["plugged"] is True and info["Tesla"]["home"] is True
    assert info["Tesla"]["planned_kwh"] == info["Tesla"]["needed_kwh"] > 20
    assert info["Tesla"]["needed_kwh"] == round(24 / 0.9, 1)
    assert info["Leaf"]["plugged"] is False and info["Leaf"]["home"] is False
    assert info["Leaf"]["planned_kwh"] == 0 and "not plugged in at home" in info["Leaf"]["why"]     # no guess about tonight
    st = hass.states.get("sensor.home_energy_twin_ev_charging_expected_tonight")
    assert float(st.state) > 20 and len(st.attributes["vehicles"]) == 2
    assert any(x >= 6.9 for x in c.plan.slots.exp_ev_kw)            # the plugged-in car charges at full rate
    # unplug the Tesla -> nothing is planned for it (no statistical "maybe")
    hass.states.async_set("binary_sensor.tesla_plug", "off")
    await c.async_recalculate()
    assert {n["vehicle"]: n for n in c.ev_info}["Tesla"]["planned_kwh"] == 0


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


async def test_session_energy_is_tracked_but_nothing_is_learned(hass: HomeAssistant):
    hass.states.async_set("sensor.car_power", "7000", {"unit_of_measurement": "W"})
    hass.states.async_set("binary_sensor.car_plug", "on")
    entry = await _setup(hass, vehicles=[{"name": "Car", "mode": "offpeak", "power_entity": "sensor.car_power",
                                          "plug_entity": "binary_sensor.car_plug", "session_kwh": 0, "charger_kw": 7}])
    c = entry.runtime_data
    assert not hasattr(c, "learn_live") and not hasattr(c, "learn_hist")          # no statistical car model at all
    t = dt_util.utcnow()
    for k in range(60):                                       # one hour at 7 kW = 7 kWh delivered this session
        c._track_vehicles(t + timedelta(minutes=k), 1 / 60)
    assert c.veh_rt["Car"]["delivered"] == pytest.approx(7.0, abs=0.2)
    hass.states.async_set("sensor.car_power", "0", {"unit_of_measurement": "W"})
    c._track_vehicles(t + timedelta(hours=5), 0.0)             # idle for > 3 h: the session is over, energy cleared
    assert c.veh_rt["Car"]["delivered"] == 0.0
    # for a car with a session size but no battery level, what has gone in this session is taken off what's left
    entry2 = await _setup(hass, title="Two", vehicles=[{"name": "Car2", "mode": "offpeak", "plug_entity": "binary_sensor.car_plug",
                                                        "session_kwh": 12, "charger_kw": 7}])
    c2 = entry2.runtime_data
    c2.veh_rt["Car2"] = {"delivered": 5.0, "last": None}
    c2.plan_dirty = True
    await c2.async_refresh()
    assert c2.ev_info[0]["needed_kwh"] == pytest.approx(7.0)


async def test_plugging_a_car_in_replans_immediately(hass: HomeAssistant):
    from pytest_homeassistant_custom_component.common import async_fire_time_changed
    hass.states.async_set("binary_sensor.car_plug", "off")
    hass.states.async_set("device_tracker.car", "home")
    hass.states.async_set("sensor.car_soc", "30", {"unit_of_measurement": "%"})
    entry = await _setup(hass, vehicles=[{"name": "Car", "mode": "offpeak", "plug_entity": "binary_sensor.car_plug",
                                          "home_entity": "device_tracker.car", "soc_entity": "sensor.car_soc",
                                          "capacity_kwh": 60, "target_soc_pct": 80}])
    c = entry.runtime_data
    assert c.ev_info[0]["planned_kwh"] == 0 and sum(c.plan.slots.exp_ev_kw) == 0                # unplugged: nothing planned
    hass.states.async_set("binary_sensor.car_plug", "on")                                       # plugged in at 30 %
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=15))
    await hass.async_block_till_done()
    assert c.ev_info[0]["planned_kwh"] == pytest.approx(50 / 100 * 60 / 0.9, abs=0.1) and sum(c.plan.slots.exp_ev_kw) > 0
    hass.states.async_set("sensor.car_soc", "80", {"unit_of_measurement": "%"})                 # it reaches its limit
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=40))
    await hass.async_block_till_done()
    assert c.ev_info[0]["planned_kwh"] == 0 and sum(c.plan.slots.exp_ev_kw) == 0               # left plugged in: nothing to charge

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


async def test_heat_pump_and_main_fuse(hass: HomeAssistant):
    hass.states.async_set("sensor.outdoor", "4.0", {"unit_of_measurement": "\u00b0C"})
    entry = await _setup(hass, hp_enabled=True, outdoor_temp_entity="sensor.outdoor", main_fuse_a=63, strategy="rules")
    c = entry.runtime_data
    hw = c.hardware()
    assert hw.import_limit_kw == pytest.approx(63 * 230 * 0.9 / 1000)             # 13.04 kW usable of a 63 A fuse
    sl = c.plan.slots
    assert sl.new_kw and max(sl.new_kw) > 1.0 and c.hp_plan is not None
    assert c.diag["heat_pump"]["temps_from"] in ("sensor-adjusted seasonal", "seasonal", "forecast")
    assert float(hass.states.get("sensor.home_energy_twin_heat_pump_power").state) >= 0
    tank = float(hass.states.get("sensor.home_energy_twin_hot_water_tank_temperature_est").state)
    assert 15 < tank < 56
    peak = float(hass.states.get("sensor.home_energy_twin_peak_grid_import_plan").state)
    unavoidable = max(h + e + n - p for h, e, n, p in zip(sl.exp_house_kw, sl.exp_ev_kw, sl.exp_new_kw,
                                                          [u * hw.pv_kwp for u in sl.exp_pv_unit]))
    assert peak <= max(hw.import_limit_kw, unavoidable) + 0.3
    assert hass.states.get("sensor.home_energy_twin_main_fuse_usage_plan_peak").state not in ("unknown", "unavailable")
    # yearly economics: gas avoided minus extra electricity, and the heat pump cost is in the outlay
    await c._async_annual()
    a = c.annual
    h = a["heat"]
    assert h["gas_kwh_displaced"] > 8000 and h["net_hp_saving"] == a["hp_saving"]
    assert a["finance"]["capex"] == 12000                                   # the one system cost (no grant set)
    st = hass.states.get("sensor.home_energy_twin_annual_heat_pump_saving")
    assert float(st.state) == a["hp_saving"] and st.attributes["scop_space"] > 2
    import pathlib, yaml
    from homeassistant.helpers.template import Template
    dash = yaml.safe_load(pathlib.Path("dashboard/energy_twin_dashboard.yaml").read_text())
    card = next(c_ for v in dash["views"] if v["path"] == "heatpump" for c_ in v["cards"]
                if c_.get("title", "").startswith("What the heat pump does"))
    out = Template(card["content"], hass).async_render()
    assert "Net saving" in out and "Gas no longer burned" in out and f"£{a['hp_saving']}" in out
    # live ledger: modelled heat pump electricity is charged to the twin, gas is credited
    t = dt_util.utcnow()
    idx = c._plan_index(t)
    hw0 = c.hardware()
    c.last_tick = t - timedelta(seconds=60)
    c._step(t, hw0, idx)
    day = c.acc["day"]
    assert day["gas_p"] > 0 and day["hp_kwh"] >= 0
    # the fuse is editable from the dashboard and takes effect immediately
    await hass.services.async_call("number", "set_value",
                                   {"entity_id": "number.home_energy_twin_main_fuse", "value": 40}, blocking=True)
    assert c.hardware().import_limit_kw == pytest.approx(40 * 230 * 0.9 / 1000)


async def test_heat_pump_off_changes_nothing(hass: HomeAssistant):
    entry = await _setup(hass, strategy="rules")
    c = entry.runtime_data
    assert not c.plan.slots.new_kw and c.hp_plan is None and c.diag["heat_pump"] == "off"
    await c._async_annual()
    assert c.annual["hp_saving"] == 0 and c.annual["heat"] is None


async def test_heat_options_flow_steps(hass: HomeAssistant):
    entry = await _setup(hass)
    for section, step in (("menu_kit", "supply"), ("menu_heat", "heat"), ("menu_heat", "heat_costs"),
                          ("menu_heat", "sources_heat")):
        r = await hass.config_entries.options.async_init(entry.entry_id)
        r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": section})
        r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": step})
        assert r["type"] == "form" and r["step_id"] == step, step


async def _forecast_solar_device(hass: HomeAssistant, wp: int = 5000):
    """A Forecast.Solar config entry + device + its sensors, as the real integration registers them."""
    from homeassistant.helpers import device_registry as dr, entity_registry as er
    await hass.config.async_update(time_zone="Europe/London")
    fs = MockConfigEntry(domain="forecast_solar", title="Forecast.Solar", options={"modules_power": wp})
    fs.add_to_hass(hass)
    dev = dr.async_get(hass).async_get_or_create(config_entry_id=fs.entry_id, identifiers={("forecast_solar", fs.entry_id)},
                                                 name="Solar production forecast")
    reg, ids = er.async_get(hass), {}
    for key in ("energy_production_today_remaining", "energy_production_tomorrow", "power_highest_peak_time_tomorrow",
                "power_production_now", "energy_current_hour"):
        ids[key] = reg.async_get_or_create("sensor", "forecast_solar", f"{fs.entry_id}_{key}", config_entry=fs,
                                           device_id=dev.id, suggested_object_id=f"fs_{key}").entity_id
    tomorrow_local = dt_util.now().replace(hour=13, minute=45, second=0, microsecond=0) + timedelta(days=1)
    hass.states.async_set(ids["energy_production_today_remaining"], "6.0", {"unit_of_measurement": "kWh"})
    hass.states.async_set(ids["energy_production_tomorrow"], "24500", {"unit_of_measurement": "Wh"})   # Wh is converted
    hass.states.async_set(ids["power_highest_peak_time_tomorrow"], tomorrow_local.isoformat())
    hass.states.async_set(ids["power_production_now"], "1500", {"unit_of_measurement": "W"})
    hass.states.async_set(ids["energy_current_hour"], "1500", {"unit_of_measurement": "Wh"})
    return fs, dev, tomorrow_local


async def _check_forecast_plan(hass, entry, tomorrow_local):
    c = entry.runtime_data
    d = c.diag
    assert d["forecast_device"] == "Forecast.Solar" and d["forecast_array_kwp"] == 5.0      # array size read from the device
    assert d["forecast_entities_found"] == 5
    assert d["forecast_source"] == "forecast sensors (daily totals + peak time)"           # (no hourly feed in this test env)
    assert d["sensor_tomorrow_kwh"] == 24.5
    assert d["planner_solar_tomorrow_kwh"] == pytest.approx(24.5, rel=0.03)               # the plan agrees with the sensor
    assert d["forecast_check_tomorrow"] == "OK"
    sl = c.plan.slots
    tz = dt_util.get_default_time_zone()
    tom = [i for i, s in enumerate(sl.start) if s.astimezone(tz).date() == tomorrow_local.date()]
    pk = max(tom, key=lambda i: sl.exp_pv_unit[i])
    assert abs((sl.start[pk] - tomorrow_local.astimezone(timezone.utc)).total_seconds()) <= 1800   # peak where forecast says
    # live power from the device's own sensor: 1.5 kW on a 5 kWp array -> 1.8 kW on the 6 kWp twin
    assert d["live_pv_entity"] and float(hass.states.get("sensor.home_energy_twin_solar_power").state) == pytest.approx(1.8)


async def test_forecast_device_is_all_you_pick(hass: HomeAssistant):
    fs, dev, tomorrow_local = await _forecast_solar_device(hass)
    entry = await _setup(hass, strategy="rules", forecast_device=dev.id, forecast_ref_kwp=None, pv_now_entity=None)
    await _check_forecast_plan(hass, entry, tomorrow_local)
    # confidence scale: trust it less -> less solar in the plan
    c = entry.runtime_data
    before = sum(c.plan.slots.exp_pv_unit)
    await c.async_set_options({"forecast_scale_pct": 80.0}, annual=False)
    assert sum(c.plan.slots.exp_pv_unit) < before * 0.9


async def test_forecast_solar_is_auto_selected_when_there_is_only_one(hass: HomeAssistant):
    fs, dev, tomorrow_local = await _forecast_solar_device(hass)
    entry = await _setup(hass, strategy="rules", forecast_ref_kwp=None, pv_now_entity=None)      # nothing chosen at all
    await _check_forecast_plan(hass, entry, tomorrow_local)


async def test_every_menu_item_is_labelled_and_reachable(hass: HomeAssistant):
    """Regression: new settings pages used to appear as blank menu rows."""
    from custom_components.energy_twin.menus import LABELS, SECTIONS, TOP
    entry = await _setup(hass)
    mgr = hass.config_entries.options
    r = await mgr.async_init(entry.entry_id)
    assert r["type"] == "menu" and isinstance(r["menu_options"], dict)
    assert list(r["menu_options"]) == TOP and all(v.strip() for v in r["menu_options"].values())
    for section, items in SECTIONS.items():
        for item in items:
            r = await mgr.async_init(entry.entry_id)
            r = await mgr.async_configure(r["flow_id"], {"next_step_id": section})
            assert r["type"] == "menu" and all(r["menu_options"][i] == LABELS[i] and LABELS[i].strip() for i in items)
            if item == "init":
                continue
            r = await mgr.async_configure(r["flow_id"], {"next_step_id": item})
            assert r["type"] in ("form", "menu"), (section, item)


def _tesla(hass, name, vin, *, cable=True, soc="40", limit="90", charging="charging", home="home", power="6.9"):
    """Register a car exactly as Teslemetry / Tesla Fleet / Tessie do (same entity keys, classes and units)."""
    from homeassistant.helpers import device_registry as dr, entity_registry as er
    ce = MockConfigEntry(domain="teslemetry", title=name)
    ce.add_to_hass(hass)
    dev = dr.async_get(hass).async_get_or_create(config_entry_id=ce.entry_id, identifiers={("teslemetry", vin)}, name=name)
    reg, slug = er.async_get(hass), name.lower().replace(" ", "_")

    def add(domain, key, state, attrs=None, **kw):
        e = reg.async_get_or_create(domain, "teslemetry", f"{vin}-{key}", config_entry=ce, device_id=dev.id,
                                    translation_key=key, suggested_object_id=f"{slug}_{key}", **kw)
        hass.states.async_set(e.entity_id, state, attrs or {})
        return e.entity_id

    ids = {
        "soc": add("sensor", "charge_state_battery_level", soc, {"unit_of_measurement": "%"}, original_device_class="battery"),
        "state": add("sensor", "charge_state_charging_state", charging, original_device_class="enum"),
        "power": add("sensor", "charge_state_charger_power", power, {"unit_of_measurement": "kW"}, original_device_class="power"),
        "limit": add("number", "charge_state_charge_limit_soc", limit, {"unit_of_measurement": "%"}),
        "home": add("device_tracker", "location", home),
        "energy": add("sensor", "charge_state_charge_energy_added", "5.0", {"unit_of_measurement": "kWh"}, original_device_class="energy"),
        "usable": None,
    }
    if cable:
        ids["cable"] = add("binary_sensor", "charge_state_conn_charge_cable", "on", original_device_class="connectivity")
    return dev, ids


async def test_two_teslas_from_their_devices(hass: HomeAssistant):
    a, ia = _tesla(hass, "Tesla Model Y", "VINA", cable=True, soc="40", limit="90", home="home")
    b, ib = _tesla(hass, "Tesla Model 3", "VINB", cable=False, soc="70", limit="80", charging="disconnected",
                   home="not_home", power="0")
    vehicles = [{"device_id": a.id, "mode": "iog", "capacity_kwh": 75, "charger_kw": 7.0},
                {"device_id": b.id, "mode": "offpeak", "capacity_kwh": 60, "charger_kw": 7.0, "session_kwh": 12}]
    entry = await _setup(hass, vehicles=vehicles)
    c = entry.runtime_data
    va, vb = c.vehicles
    assert (va.name, vb.name) == ("Tesla Model Y", "Tesla Model 3")                # names come from the devices
    assert va.soc_entity == ia["soc"] and va.power_entity == ia["power"] and va.home_entity == ia["home"]
    assert va.target_soc_entity == ia["limit"] and va.energy_stat == ia["energy"]
    assert va.plug_entity == ia["cable"]                                           # cable sensor preferred...
    assert vb.plug_entity == ib["state"]                                           # ...charging-state enum when there is none
    info = {n["vehicle"]: n for n in c.ev_info}
    ya, yb = info["Tesla Model Y"], info["Tesla Model 3"]
    assert ya["plugged"] is True and ya["home"] is True and ya["planned_kwh"] == ya["needed_kwh"] > 0
    assert ya["target_soc_pct"] == 90.0                                           # the car's own charge limit, not the fixed 80
    assert ya["needed_kwh"] == round((90 - 40) / 100 * 75 / 0.9, 1)
    assert yb["plugged"] is False and yb["home"] is False and yb["planned_kwh"] == 0
    assert set(ya["entities"]) >= {"power", "plug", "home", "battery", "charge limit", "energy"}


async def test_explicit_entities_override_a_vehicle_device(hass: HomeAssistant):
    a, ia = _tesla(hass, "Tesla Model Y", "VINA")
    hass.states.async_set("binary_sensor.my_plug", "off")
    entry = await _setup(hass, vehicles=[{"device_id": a.id, "mode": "iog", "plug_entity": "binary_sensor.my_plug",
                                          "name": "Family car"}])
    c = entry.runtime_data
    (v,) = c.vehicles
    assert v.name == "Family car" and v.plug_entity == "binary_sensor.my_plug" and v.soc_entity == ia["soc"]
    assert c.ev_info[0]["plugged"] is False                                       # the override wins over the cable sensor


async def test_other_makes_fall_back_to_device_classes(hass: HomeAssistant):
    from homeassistant.helpers import device_registry as dr, entity_registry as er
    ce = MockConfigEntry(domain="kia_uvo")
    ce.add_to_hass(hass)
    dev = dr.async_get(hass).async_get_or_create(config_entry_id=ce.entry_id, identifiers={("kia_uvo", "x")}, name="Kia EV6")
    reg = er.async_get(hass)
    soc = reg.async_get_or_create("sensor", "kia_uvo", "ev_battery_level", config_entry=ce, device_id=dev.id,
                                  original_device_class="battery", original_name="EV battery level").entity_id
    plug = reg.async_get_or_create("binary_sensor", "kia_uvo", "plug", config_entry=ce, device_id=dev.id,
                                   original_device_class="plug", original_name="EV battery plug").entity_id
    hass.states.async_set(soc, "55", {"unit_of_measurement": "%"})
    hass.states.async_set(plug, "on")
    entry = await _setup(hass, vehicles=[{"device_id": dev.id, "mode": "offpeak"}])
    (v,) = entry.runtime_data.vehicles
    assert v.name == "Kia EV6" and v.soc_entity == soc and v.plug_entity == plug and v.power_entity is None


async def test_vehicle_form_accepts_a_device_alone(hass: HomeAssistant):
    a, _ = _tesla(hass, "Tesla Model Y", "VINA")
    entry = await _setup(hass)
    r = await hass.config_entries.options.async_init(entry.entry_id)
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "vehicles"})
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "vehicle_add"})
    base = {"mode": "iog", "capacity_kwh": 75, "target_soc_pct": 80, "charger_kw": 7.0, "session_kwh": 0}
    r = await hass.config_entries.options.async_configure(r["flow_id"], dict(base))               # neither name nor device
    assert r["type"] == "form" and r["errors"] == {"base": "name_required"}
    r = await hass.config_entries.options.async_configure(r["flow_id"], {**base, "device_id": a.id})
    assert r["type"] == "menu" and r["step_id"] == "vehicles"                                      # saved, name from the device
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "init"})
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "done"})
    await hass.async_block_till_done()
    saved = hass.config_entries.async_get_entry(entry.entry_id).options["vehicles"]
    assert saved[0]["name"] == "Tesla Model Y" and saved[0]["device_id"] == a.id


async def test_gas_rate_and_standing_charge_entities(hass: HomeAssistant):
    hass.states.async_set("sensor.gas_rate", "0.0612", {"unit_of_measurement": "GBP/kWh"})
    hass.states.async_set("sensor.gas_standing", "0.2915", {"unit_of_measurement": "GBP"})
    entry = await _setup(hass, hp_enabled=True, strategy="rules", gas_rate_entity="sensor.gas_rate",
                         gas_standing_entity="sensor.gas_standing", gas_unit_p=6.0, gas_standing_p_day=30.0)
    c = entry.runtime_data
    hp = c.heat_params()
    assert hp.gas_unit_p == pytest.approx(6.12) and hp.gas_standing_p_day == pytest.approx(29.15)   # sensors, in pence
    assert c.diag["heat_pump"]["gas_rate_from"] == "sensor" and c.diag["heat_pump"]["gas_rate_p"] == 6.12
    await c._async_annual()
    h = c.annual["heat"]
    assert h["gas_cost_avoided"] == pytest.approx(h["gas_kwh_displaced"] * 6.12 / 100, abs=0.05)
    assert h["gas_standing_avoided"] == pytest.approx(29.15 * 365 / 100, abs=0.05)
    # sensor goes unavailable -> the fixed numbers take over, nothing breaks
    hass.states.async_set("sensor.gas_rate", "unavailable")
    hass.states.async_set("sensor.gas_standing", "unknown")
    assert c.heat_params().gas_unit_p == 6.0 and c.heat_params().gas_standing_p_day == 30.0
    # a p/kWh sensor works too
    hass.states.async_set("sensor.gas_rate", "5.9", {"unit_of_measurement": "p/kWh"})
    assert c.heat_params().gas_unit_p == pytest.approx(5.9)


async def test_consumption_and_gas_fields_are_entity_pickers(hass: HomeAssistant):
    from homeassistant.helpers import selector as sel
    entry = await _setup(hass)
    mgr = hass.config_entries.options

    async def fields(section, step):
        r = await mgr.async_init(entry.entry_id)
        r = await mgr.async_configure(r["flow_id"], {"next_step_id": section})
        r = await mgr.async_configure(r["flow_id"], {"next_step_id": step})
        return {str(k): v for k, v in r["data_schema"].schema.items()}

    heat = await fields("menu_heat", "sources_heat")
    for key in ("gas_stat", "gas_rate_entity", "gas_standing_entity", "outdoor_temp_entity", "weather_entity"):
        assert isinstance(heat[key], sel.EntitySelector), key
    assert "outdoor_temp_stat" not in heat
    solar = await fields("menu_sources", "sources_solar_history")
    assert isinstance(solar["consumption_stat"], sel.EntitySelector)
    assert isinstance(solar["ev_stats"], sel.EntitySelector)


async def test_old_text_ev_stats_setting_still_loads(hass: HomeAssistant):
    entry = await _setup(hass, ev_stats="sensor.car_a_energy, sensor.car_b_energy", consumption_stat="sensor.house_kwh")
    await entry.runtime_data._async_load_history()          # a comma-separated string from an older version must not crash
    assert entry.runtime_data.profile is not None


def _tables(text: str) -> list[list[str]]:
    """Contiguous blocks of markdown table lines."""
    blocks, cur = [], []
    for line in text.splitlines():
        if line.startswith("|"):
            cur.append(line)
        elif cur:
            blocks.append(cur)
            cur = []
    return blocks + ([cur] if cur else [])


async def test_compare_tab_for_three_setups(hass: HomeAssistant):
    import pathlib, yaml
    from homeassistant.helpers.template import Template
    hass.states.async_set("sensor.gas_rate", "0.06", {"unit_of_measurement": "GBP/kWh"})
    a = await _setup(hass, title="Today's plan", strategy="rules")
    b = await _setup(hass, title="Big battery", strategy="rules", battery_kwh=20, pv_kwp=8)
    c = await _setup(hass, title="Heat pump + kit", strategy="rules", hp_enabled=True, gas_rate_entity="sensor.gas_rate")
    for e in (a, b, c):
        await e.runtime_data._async_annual()
    dash = yaml.safe_load(pathlib.Path("dashboard/energy_twin_dashboard.yaml").read_text())
    cards = next(v for v in dash["views"] if v["path"] == "compare")["cards"]
    out = {x.get("title") or "howto": Template(x["content"], hass).async_render() for x in cards}
    names = ("Today's plan", "Big battery", "Heat pump + kit")

    glance = out["At a glance"]
    for tag in ("Best long-term value", "Biggest saving a year", "Fastest payback", "Best monthly cashflow"):
        assert tag in glance, glance
    assert any(n in glance for n in names)

    bills = out["Your annual energy bill: today vs each setup"]
    assert "Today (do nothing)" in bills and all(f"**{n}**" in bills for n in names)
    ra, rb, rc = (e.runtime_data.annual for e in (a, b, c))
    # the 'do nothing' bill is one number; every setup's saving is bill_today - bill_with
    assert ra["bill_today"] == rb["bill_today"] == rc["bill_today"]
    for r in (ra, rb, rc):
        assert r["bill_today"] - r["bill_with"] == pytest.approx(r["saving_year1"], abs=0.05)
    assert rc["gas_with"] == 0 and ra["gas_with"] == ra["gas_today"]                  # only the heat pump setup drops gas
    assert "█" in bills

    race = out["Payback race: cumulative net position"]
    assert "✅" in race or "❌" in race and all(n in race for n in names)
    assert "🏆" in out["Month by month saving (🏆 = best that month)"]
    assert "Solar generated" in out["Energy flows per year"]
    assert "Heat pump" in out["Where the saving comes from"] and "—" in out["Where the saving comes from"]

    # every table is well-formed: header, separator, rows, same number of columns throughout, no stray blank lines
    for title, text in out.items():
        assert "Error" not in text and "None" not in text.replace("None yet", ""), (title, text[:300])
        for t in _tables(text):
            assert len(t) >= 3 and set(t[1].replace("|", "").replace(":", "").replace("-", "").strip()) <= {""}, (title, t[:2])
            cols = {l.count("|") for l in t}
            assert len(cols) == 1, (title, cols, t)


async def test_system_cost_grant_and_finance_controls(hass: HomeAssistant):
    entry = await _setup(hass, strategy="rules")
    c = entry.runtime_data
    await c._async_annual()
    assert c.annual["finance"]["capex"] == 12000 and c.annual["finance"]["finance_mode"] == "cash"
    svc = hass.services.async_call
    await svc("number", "set_value", {"entity_id": "number.home_energy_twin_system_cost", "value": 15000}, blocking=True)
    await svc("number", "set_value", {"entity_id": "number.home_energy_twin_grant", "value": 3000}, blocking=True)
    await svc("select", "select_option", {"entity_id": "select.home_energy_twin_how_you_pay",
                                          "option": "Fixed monthly payment"}, blocking=True)
    await svc("number", "set_value", {"entity_id": "number.home_energy_twin_fixed_monthly_payment", "value": 120}, blocking=True)
    await svc("number", "set_value", {"entity_id": "number.home_energy_twin_finance_term", "value": 120}, blocking=True)
    await hass.async_block_till_done()
    await c._async_annual()
    f = c.annual["finance"]
    assert f["capex"] == 12000 and f["finance_mode"] == "fixed_monthly" and f["monthly_payment"] == 120
    assert f["total_paid_finance"] == 14400 and f["effective_apr_pct"] is not None and f["cost_of_finance"] > 0
    sens = hass.states.get("sensor.home_energy_twin_annual_saving").attributes
    assert sens["effective_apr_pct"] == f["effective_apr_pct"] and sens["finance_mode"] == "fixed_monthly"
    # a quoted loan: interest rate over a term
    await svc("select", "select_option", {"entity_id": "select.home_energy_twin_how_you_pay", "option": "Loan (interest rate)"}, blocking=True)
    await svc("number", "set_value", {"entity_id": "number.home_energy_twin_loan_interest_rate", "value": 6.9}, blocking=True)
    await hass.async_block_till_done()
    await c._async_annual()
    assert c.annual["finance"]["finance_mode"] == "loan" and c.annual["finance"]["effective_apr_pct"] == 6.9


async def test_export_window_is_configured_and_reported(hass: HomeAssistant):
    entry = await _setup(hass, strategy="optimised")
    c = entry.runtime_data
    st = c.strategy()
    assert (st.export_from_min, st.export_until_min, st.event_export_anywhere) == (18 * 60, 23 * 60 + 30, True)
    assert c.diag["battery_export_window"].startswith("18:00-23:30")
    r = await hass.config_entries.options.async_init(entry.entry_id)
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "menu_planning"})
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "strategy"})
    keys = {str(k) for k in r["data_schema"].schema}
    assert {"export_from", "export_until", "event_export_anywhere"} <= keys
    # the plan never exports from the battery in a cheap slot (here without any car load)
    sl, res = c.plan.slots, c.plan.result
    assert not [i for i in range(sl.n) if sl.cheap[i] and not sl.event[i] and res.export_kw[i] > 0.05 and res.battery_kw[i] < 0]


async def test_live_battery_never_discharges_into_a_car(hass: HomeAssistant):
    """CT clamps / firmware can't tell car from house, so battery discharge is blocked whenever a car draws power."""
    hass.states.async_set("sensor.car_power", "7000", {"unit_of_measurement": "W"})
    entry = await _setup(hass, strategy="rules", vehicles=[{"name": "Car", "mode": "offpeak", "power_entity": "sensor.car_power"}])
    c = entry.runtime_data
    hw, t = c.hardware(), dt_util.utcnow()
    idx = c._plan_index(t)
    c.soc = hw.emax * 0.8
    c.plan.result.battery_kw[idx] = -4.0                      # force a plan that wants to discharge
    c.plan.slots.cheap[idx] = False
    # 1) car power measured at 7 kW -> no discharge, SoC untouched
    c.last_tick = t - timedelta(seconds=60)
    before = c.soc
    c._step(t, hw, idx)
    assert c._live["battery"] >= 0 and c.soc >= before - 1e-9, c._live
    # 2) car draws nothing -> the same plan is allowed to discharge (proves the block above was the car)
    hass.states.async_set("sensor.car_power", "0", {"unit_of_measurement": "W"})
    c.last_tick = t - timedelta(seconds=60)
    c._step(t + timedelta(seconds=1), hw, idx)
    assert c._live["battery"] < 0
    # 3) car power NOT measured, but the plan expects a car charging -> still blocked
    entry2 = await _setup(hass, title="No sensor", strategy="rules")
    c2 = entry2.runtime_data
    hw2, i2 = c2.hardware(), c2._plan_index(t)
    c2.soc = hw2.emax * 0.8
    c2.plan.result.battery_kw[i2] = -4.0
    c2.plan.slots.cheap[i2] = False
    c2.plan.slots.ev_kw[i2] = 7.0
    c2.last_tick = t - timedelta(seconds=60)
    c2._step(t, hw2, i2)
    assert c2._live["battery"] >= 0


async def test_virtual_battery_level_survives_a_restart(hass: HomeAssistant):
    from custom_components.energy_twin.coordinator import TwinCoordinator
    entry = await _setup(hass, strategy="rules")
    c = entry.runtime_data
    hw = c.hardware()
    c.soc = 0.6 * hw.emax
    await c._store.async_save({"soc": c.soc, "last": dt_util.utcnow().isoformat(), "acc": c.acc})   # what a shutdown writes
    fresh = TwinCoordinator(hass, entry)                                    # a new process after the restart
    await fresh.async_setup()
    assert fresh.soc == pytest.approx(0.6 * hw.emax)                        # restored, not reset to the 10% reserve
    # brand-new scenario (nothing saved yet) starts at the reserve
    other = MockConfigEntry(domain=DOMAIN, title="New", options={"battery_kwh": 10.0})
    other.add_to_hass(hass)
    blank = TwinCoordinator(hass, other)
    await blank.async_setup()
    assert blank.soc == pytest.approx(0.5 * blank.hardware().emax)           # starts at 50 % by default
    # a smaller battery than the saved level is clamped rather than showing >100 %
    await entry.runtime_data._store.async_save({"soc": 12.0})
    small = MockConfigEntry(domain=DOMAIN, title="Small", options={"battery_kwh": 5.0})
    small.add_to_hass(hass)
    sc = TwinCoordinator(hass, small)
    sc._store = c._store                                                    # same saved data
    await sc.async_setup()
    assert sc.soc == pytest.approx(5.0)


async def test_battery_level_starts_at_50_and_can_be_set(hass: HomeAssistant):
    entry = await _setup(hass, strategy="rules")
    c = entry.runtime_data
    hw = c.hardware()
    sensor = lambda: float(hass.states.get("sensor.home_energy_twin_battery_state_of_charge").state)  # noqa: E731
    assert c.soc == pytest.approx(0.5 * hw.emax, abs=0.05) and 49 < sensor() < 51          # fresh start: 50 %
    num = "number.home_energy_twin_set_battery_level"
    assert float(hass.states.get(num).state) == pytest.approx(50, abs=1)
    # slider
    await hass.services.async_call("number", "set_value", {"entity_id": num, "value": 80}, blocking=True)
    assert c.soc == pytest.approx(0.8 * hw.emax, abs=0.05) and 79 < sensor() < 81
    # service (for automations)
    await hass.services.async_call(DOMAIN, "set_battery_level", {"percent": 30}, blocking=True)
    assert c.soc == pytest.approx(0.3 * hw.emax, abs=0.05)
    # can't go below your reserve (10 %) or above full
    await hass.services.async_call(DOMAIN, "set_battery_level", {"percent": 0}, blocking=True)
    assert c.soc == pytest.approx(hw.emin, abs=0.05)
    await hass.services.async_call("number", "set_value", {"entity_id": num, "value": 100}, blocking=True)
    assert c.soc == pytest.approx(hw.emax, abs=0.05)
    # the plan is rebuilt from the new level
    assert c.plan.soc0 == pytest.approx(hw.emax, abs=0.05)
    # reset returns to the configured starting level
    await c.async_set_options({"start_soc_pct": 60.0}, annual=False)
    await hass.services.async_call("button", "press", {"entity_id": "button.home_energy_twin_reset_virtual_battery"}, blocking=True)
    assert c.soc == pytest.approx(0.6 * hw.emax, abs=0.05)
    with pytest.raises(Exception):
        await hass.services.async_call(DOMAIN, "set_battery_level", {"percent": 150}, blocking=True)   # out of range is rejected


async def test_renamed_setup_keeps_the_dashboard_entity_ids(hass: HomeAssistant):
    """Regression: a setup called 'Home Energy Twin - PW3 13.5 kWh' gave new entities a longer prefix and the
    dashboard showed 'Entity not found' for everything created after the rename."""
    import pathlib, re
    from homeassistant.helpers import entity_registry as er
    entry = await _setup(hass, title="Home Energy Twin - PW3 13.5 kWh", hp_enabled=True)
    await hass.async_block_till_done()
    txt = pathlib.Path("dashboard/energy_twin_dashboard.yaml").read_text()
    wanted = set(re.findall(r"\b((?:sensor|number|select|switch|button|datetime)\.home_energy_twin_[a-z_]+)", txt))
    missing = sorted(e for e in wanted if hass.states.get(e) is None)
    assert missing == [], missing
    assert not [s.entity_id for s in hass.states.async_all() if "pw3_13_5" in s.entity_id]     # no long-prefixed leftovers
    # a second setup is NOT renamed (only the first one backs the shipped dashboard)
    b = await _setup(hass, title="Big battery", strategy="rules")
    assert hass.states.get("sensor.big_battery_battery_state_of_charge") is not None
    # an ID the user chose themselves is left alone
    reg = er.async_get(hass)
    e = reg.async_get("sensor.home_energy_twin_grid_power")
    reg.async_update_entity(e.entity_id, new_entity_id="sensor.my_grid_power")
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert reg.async_get("sensor.my_grid_power") is not None and reg.async_get("sensor.home_energy_twin_grid_power") is None


async def test_grid_import_export_and_balance_card(hass: HomeAssistant):
    import pathlib, yaml
    from homeassistant.helpers.template import Template
    entry = await _setup(hass, strategy="optimised", pv_now_entity=None, forecast_ref_kwp=5.0)
    c = entry.runtime_data
    hw = c.hardware()
    t = dt_util.utcnow()
    idx = c._plan_index(t)
    sl, res = c.plan.slots, c.plan.result
    hass.states.async_set("sensor.mini", "400", {"unit_of_measurement": "W"})
    sl.cheap[idx], sl.tod_min[idx] = False, 6 * 60
    card = next(x for v in yaml.safe_load(pathlib.Path("dashboard/energy_twin_dashboard.yaml").read_text())["views"]
                if v["path"] == "live" for x in v["cards"] if x.get("title") == "Right now - how the numbers add up")

    async def case(soc, pv_unit, r_plan):
        sl.pv_unit[idx] = pv_unit
        c.soc, res.battery_kw[idx] = soc, r_plan
        c.last_tick = t - timedelta(seconds=60)
        hass.states.async_set("sensor.mini", "400", {"unit_of_measurement": "W"})
        c.async_set_updated_data(c._snapshot(t, hw, c._step(t, hw, idx)))
        await hass.async_block_till_done()
        g = lambda k: float(hass.states.get(f"sensor.home_energy_twin_{k}").state)  # noqa: E731
        return {k: g(k) for k in ("home_load_meter", "solar_power", "battery_power", "grid_import", "grid_export", "grid_power")}

    # 1) dawn, battery at the reserve (can't help): you import exactly what you use
    a = await case(hw.emin, 0.0, 0.0)
    assert a["grid_import"] == pytest.approx(a["home_load_meter"]) and a["grid_export"] == 0
    # 2) dawn, battery 50% and no sun: the virtual battery runs the house, so import is 0 - by design
    b = await case(0.5 * hw.emax, 0.0, 0.0)
    assert b["grid_import"] == 0 and b["battery_power"] == pytest.approx(-b["home_load_meter"])
    # 3) sunny, battery full: surplus solar is exported, import is 0
    d = await case(hw.emax, 1.0, 0.0)
    assert d["grid_import"] == 0 and d["grid_export"] > 0.5
    # the identity behind the card: net grid = load - solar + battery, and import/export are its two halves
    for x in (a, b, d):
        net = x["home_load_meter"] - x["solar_power"] + x["battery_power"]
        assert x["grid_power"] == pytest.approx(net, abs=0.01)
        assert x["grid_import"] == pytest.approx(max(net, 0), abs=0.01)
        assert x["grid_export"] == pytest.approx(min(max(-net, 0), hw.export_limit_kw), abs=0.01)   # export is capped (G98)
    out = Template(card["content"], hass).async_render()
    assert "Grid with the kit" in out and "Importing" in out and "Exporting" in out
    assert hass.states.get("sensor.home_energy_twin_imported_today") is not None


async def test_import_price_does_not_flick_to_placeholders(hass: HomeAssistant):
    """Regression: a blank Octopus 'rates' update made the plan fall back to the placeholder 27p/7p for a cycle."""
    entry = await _setup(hass, strategy="rules", import_now_entity=None)
    c = entry.runtime_data
    real = _rates(None, cheap_p=0.0669, std_p=0.36294)                        # your real IOG rates: 6.69p / 36.294p
    hass.states.async_set("event.rates", "x", {"rates": real})
    c.plan_dirty = True
    await c.async_refresh()
    assert c.learned_rates["standard_p"] == pytest.approx(36.294) and c.learned_rates["cheap_p"] == pytest.approx(6.69)
    day = [i for i, s in enumerate(c.plan.slots.start) if not c.plan.slots.cheap[i]]
    assert day and all(c.plan.slots.import_p[i] == pytest.approx(36.294) for i in day)
    prices = []
    for attrs in ({}, {"rates": []}, {"rates": real}, {}, {"rates": real}):      # the entity flickers between plans
        hass.states.async_set("event.rates", "x", attrs)
        c.plan_dirty = True
        await c.async_refresh()
        prices.append(float(hass.states.get("sensor.home_energy_twin_import_price").state))
    assert len(set(prices)) == 1, prices                                         # one price throughout - no 27p / 7p blips
    assert all(c.plan.slots.import_p[i] == pytest.approx(36.294) for i in day)
    # slots the lists never covered use YOUR rates, not the placeholders
    assert max(c.plan.slots.import_p) == pytest.approx(36.294) and min(c.plan.slots.import_p) == pytest.approx(6.69)
    # copy the learned rates into the settings (the annual what-if uses those numbers)
    await hass.services.async_call("button", "press", {"entity_id": "button.home_energy_twin_use_my_live_tariff_rates"}, blocking=True)
    assert entry.options["standard_p"] == pytest.approx(36.294) and entry.options["cheap_p"] == pytest.approx(6.69)


async def test_price_is_the_live_sensor_reading_even_with_no_rate_lists(hass: HomeAssistant):
    """Regression (import_rate_slots: 0): the sensor flicked to the 27p/7p placeholders whenever the clock moved on
    to a slot the plan knew nothing about. The live price sensor now sets the price, and real rates are learned."""
    from homeassistant.util import dt as dt_util2
    hass.states.async_set("sensor.price_now", "36.294", {"unit_of_measurement": "p/kWh"})
    entry = await _setup(hass, strategy="rules", import_rates_entities=[], export_rates_entities=[],
                         import_now_entity="sensor.price_now", cheap_p=7.0, standard_p=27.0)
    c = entry.runtime_data
    hw = c.hardware()
    t = dt_util.utcnow()
    idx = c._plan_index(t)
    sl = c.plan.slots
    assert c.diag["import_rate_slots"] == 0 and "import_rates_hint" in c.diag
    # step the clock through several later slots (as it does between 5-minute plan rebuilds): price never moves
    seen = set()
    for k in range(0, 5):
        i = min(idx + k, sl.n - 1)
        if sl.event[i] or sl.cheap[i]:
            continue
        c.last_tick = t - timedelta(seconds=60)
        c._step(t, hw, i)
        seen.add(round(c._live["import_p"], 3))
    assert seen == {36.294}, seen
    # observations feed the learned rates: two standard-rate and two cheap-rate slots are enough for a flat tariff
    tariff = c.tariff()
    c._rate_cache["imp"].clear()
    base = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    for h in (8, 9, 10, 11):                                                     # daytime, outside the 23:30-05:30 window
        c._rate_cache["imp"][base.replace(hour=h)] = 36.294
    for h in (1, 2, 3):                                                          # inside the window
        c._rate_cache["imp"][base.replace(hour=h)] = 6.69
    c._learn_rates(tariff)
    assert c.learned_rates["standard_p"] == pytest.approx(36.294) and c.learned_rates["cheap_p"] == pytest.approx(6.69)


async def test_load_between_ticks_is_integrated_not_sampled(hass: HomeAssistant):
    entry = await _setup(hass, strategy="rules", pv_now_entity=None)
    c = entry.runtime_data
    hw = c.hardware()
    idx = c._plan_index(dt_util.utcnow())
    t0 = dt_util.utcnow()
    c.feed_power("sensor.mini", hass.states.get("sensor.mini"), t0)
    c._step(t0, hw, idx)                                             # start a clean tick window
    c._integ["sensor.mini"].feed(t0 + timedelta(seconds=0), 0.4)
    for sec, kw in ((20, 6.0), (30, 0.4), (50, 0.4)):                # a 10 s kettle spike between ticks
        c._integ["sensor.mini"].feed(t0 + timedelta(seconds=sec), kw)
    hass.states.async_set("sensor.mini", "400", {"unit_of_measurement": "W"})
    c._integ["sensor.mini"].kw = 0.4
    c.last_tick = t0
    c._step(t0 + timedelta(seconds=60), hw, idx)
    exact = (0.4 * 20 + 6.0 * 10 + 0.4 * 30) / 60
    assert c._live["house"] == pytest.approx(exact, abs=0.02) and c._live["house"] > 1.0   # not the 0.4 kW sample
    assert c.diag.get("power_integration") == "every reading as it arrives"


async def test_usage_follows_the_octopus_meter(hass: HomeAssistant):
    hass.states.async_set("sensor.meter", "5000.0", {"unit_of_measurement": "kWh"})
    entry = await _setup(hass, strategy="rules", pv_now_entity=None, consumption_stat="sensor.meter")
    c = entry.runtime_data
    hw = c.hardware()
    idx = c._plan_index(dt_util.utcnow())
    t = dt_util.utcnow()
    hass.states.async_set("sensor.mini", "600", {"unit_of_measurement": "W"})         # a steady 0.6 kW
    c.feed_power("sensor.mini", hass.states.get("sensor.mini"), t)
    c._roll_buckets(t)
    c.acc["day"] = {**c.acc["day"], "load_kwh": 0.0, "actual_p": 0.0, "twin_p": 0.0, "meter_kwh": 0.0, "trueup_kwh": 0.0}
    c.last_tick = t
    c._meter_prev = None
    c._step(t + timedelta(seconds=1), hw, idx)                                       # first reading: remembered only
    price = c._live["import_p"]
    for k in range(1, 7):                                                            # 6 minutes: we integrate 0.06 kWh
        c._step(t + timedelta(minutes=k), hw, idx)
    ledger_before = c.acc["day"]["load_kwh"]
    assert ledger_before == pytest.approx(0.6 * 6 / 60, abs=0.01)
    # Octopus reports 0.10 kWh in that time (the real meter saw peaks our sensor didn't): the ledger follows it
    hass.states.async_set("sensor.meter", "5000.1", {"unit_of_measurement": "kWh"})
    c._step(t + timedelta(minutes=7), hw, idx)
    d = c.acc["day"]
    assert d["meter_kwh"] == pytest.approx(0.1, abs=1e-6)
    assert d["load_kwh"] == pytest.approx(0.1 + 0.6 / 60, abs=0.012)                  # = meter energy + the minute since
    assert d["trueup_kwh"] == pytest.approx(0.1 - 0.07, abs=0.006)               # meter 0.10 vs the 0.07 we had booked (7 min)
    # the corrected energy is priced into both the real bill and the twin's bill (savings are unaffected)
    assert d["actual_p"] > 0 and d["twin_p"] > 0
    usage = hass.states.get("sensor.home_energy_twin_home_usage_today")
    c.async_set_updated_data(c._snapshot(t, hw, c._live))
    await hass.async_block_till_done()
    usage = hass.states.get("sensor.home_energy_twin_home_usage_today")
    assert float(usage.state) == pytest.approx(d["load_kwh"], abs=0.001)
    assert usage.attributes["octopus_meter_kwh_today"] == pytest.approx(0.1, abs=1e-3)


async def test_meter_edge_cases_are_ignored_safely(hass: HomeAssistant):
    hass.states.async_set("sensor.meter", "100000", {"unit_of_measurement": "Wh"})      # Wh is converted: 100 kWh
    entry = await _setup(hass, strategy="rules", pv_now_entity=None, consumption_stat="sensor.meter")
    c = entry.runtime_data
    hw = c.hardware()
    idx = c._plan_index(dt_util.utcnow())
    t = dt_util.utcnow()
    c.feed_power("sensor.mini", hass.states.get("sensor.mini"), t)
    c.last_tick = t
    c._step(t + timedelta(seconds=1), hw, idx)
    for k in range(1, 4):
        c._step(t + timedelta(minutes=k), hw, idx)
    base = dict(c.acc["day"])
    hass.states.async_set("sensor.meter", "160000", {"unit_of_measurement": "Wh"})       # +60 kWh in minutes: implausible
    c._step(t + timedelta(minutes=5), hw, idx)
    assert c.acc["day"]["meter_kwh"] == base["meter_kwh"]                                 # ignored, nothing booked
    hass.states.async_set("sensor.meter", "0.2", {"unit_of_measurement": "kWh"})          # a daily reset to ~0
    c._step(t + timedelta(minutes=6), hw, idx)
    assert c.acc["day"]["meter_kwh"] == pytest.approx(base["meter_kwh"] + 0.2, abs=1e-6)  # a drop to ~0 is a daily reset: 0.2 kWh since
    hass.states.async_set("sensor.meter", "0.25", {"unit_of_measurement": "kWh"})
    c._step(t + timedelta(minutes=7), hw, idx)
    assert c.acc["day"]["meter_kwh"] == pytest.approx(base["meter_kwh"] + 0.25, abs=1e-6)  # normal increments resume
    # no consumption sensor set: nothing happens
    other = await _setup(hass, title="NoMeter", strategy="rules")
    assert other.runtime_data.diag.get("meter") == "no consumption sensor set"


async def test_power_sensor_that_reads_consistently_high_is_calibrated(hass: HomeAssistant):
    hass.states.async_set("sensor.meter", "10.0", {"unit_of_measurement": "kWh"})
    entry = await _setup(hass, strategy="rules", pv_now_entity=None, consumption_stat="sensor.meter")
    c = entry.runtime_data
    hw = c.hardware()
    idx = c._plan_index(dt_util.utcnow())
    t = dt_util.utcnow()
    hass.states.async_set("sensor.mini", "1000", {"unit_of_measurement": "W"})            # sensor says 1.0 kW...
    c.feed_power("sensor.mini", hass.states.get("sensor.mini"), t)
    c.last_tick = t
    c._meter_prev = None
    c._step(t + timedelta(seconds=1), hw, idx)
    now, meter = t, 10.0
    for block in range(8):                                                                 # ...the real meter says 0.8 kW
        for k in range(1, 31):
            c._step(now + timedelta(minutes=k), hw, idx)
        now += timedelta(minutes=30)
        meter += 0.4                                                                       # 0.8 kW x 0.5 h
        hass.states.async_set("sensor.meter", f"{meter:.3f}", {"unit_of_measurement": "kWh"})
        c._step(now + timedelta(seconds=30), hw, idx)
    assert c._k_n >= 5 and c.meter_k == pytest.approx(0.8, abs=0.06)
    c._step(now + timedelta(minutes=2), hw, idx)
    assert c._live["total"] == pytest.approx(0.8, abs=0.07)                                # the load now matches the meter
    assert c._snapshot(now, hw, c._live)["meter"]["calibration_factor"] == pytest.approx(c.meter_k, abs=0.001)


async def test_numbers_stack_up_with_modelled_heat_pump_load(hass: HomeAssistant):
    """Regression: 0.40 load / 0.32 solar / -0.31 battery looked wrong because 0.23 kW of modelled heat pump load wasn't shown."""
    import pathlib, yaml
    from homeassistant.helpers.template import Template
    entry = await _setup(hass, strategy="optimised", hp_enabled=True, pv_now_entity=None, forecast_ref_kwp=5.0)
    c = entry.runtime_data
    hw = c.hardware()
    t = dt_util.utcnow()
    idx = c._plan_index(t)
    sl, res = c.plan.slots, c.plan.result
    sl.cheap[idx], sl.tod_min[idx] = False, 12 * 60
    sl.pv_unit[idx], sl.new_kw[idx] = 0.32 / 6.0, 0.23
    c.soc, res.battery_kw[idx] = 0.5 * hw.emax, 0.0
    hass.states.async_set("sensor.mini", "400", {"unit_of_measurement": "W"})
    c.feed_power("sensor.mini", hass.states.get("sensor.mini"), t)
    c.last_tick = t - timedelta(seconds=60)
    c.async_set_updated_data(c._snapshot(t, hw, c._step(t, hw, idx)))
    await hass.async_block_till_done()
    g = lambda k: float(hass.states.get(f"sensor.home_energy_twin_{k}").state)  # noqa: E731
    load, mod, sol, bat, net = g("home_load_meter"), g("modelled_extra_load"), g("solar_power"), g("battery_power"), g("grid_power")
    assert mod == pytest.approx(0.23, abs=0.01) and bat < -0.2                  # the battery is covering modelled load
    assert net == pytest.approx(load + mod - sol + bat, abs=0.01)               # ...and now every term is on screen
    card = next(x for v in yaml.safe_load(pathlib.Path("dashboard/energy_twin_dashboard.yaml").read_text())["views"]
                if v["path"] == "live" for x in v["cards"] if x.get("title") == "Right now - how the numbers add up")
    out = Template(card["content"], hass).async_render()
    assert "Heat pump & hob (modelled" in out and "Grid with the kit" in out
