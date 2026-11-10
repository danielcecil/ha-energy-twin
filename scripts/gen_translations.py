"""Regenerates strings.json + translations/en.json. Run: python scripts/gen_translations.py"""
import importlib.util
import json
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "menus", Path(__file__).resolve().parents[1] / "custom_components" / "energy_twin" / "menus.py")
menus = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(menus)   # pure data module: no Home Assistant import needed

F = {  # option key -> (label, help)
    "power_entity": ("Charging power sensor - override", "Whole-home import power sensor, W or kW. Used to drive the live twin."),
    "ev_power_entities": ("Car charger power sensors", "Optional. Lets the twin separate EV load from house load, so the battery never 'feeds' a car."),
    "pv_now_entity": ("Expected solar generation now", "Real-time expected/forecast solar power sensor (W or kW), for the array size set below. Overrides the hourly forecast for the current slot."),
    "import_now_entity": ("Current import price", "Current tariff price entity (GBP/kWh or p/kWh, auto-detected)."),
    "export_now_entity": ("Current export price", "Current export tariff price entity (GBP/kWh or p/kWh)."),
    "import_rates_entities": ("Import rates entities", "Octopus Energy integration rate entities (current day + next day). Their 'rates' attribute is read."),
    "export_rates_entities": ("Export rates entities", "Octopus export rate entities, if you want real export prices (e.g. Agile Outgoing)."),
    "dispatch_entity": ("Intelligent dispatching entity", "Octopus Intelligent 'dispatching' binary sensor - planned smart-charge slots are priced at the cheap rate."),
    "power_up_entity": ("Power Up event entity", "Any binary sensor / event / calendar that is 'on' or lists windows during Power Up events."),
    "power_down_entity": ("Power Down event entity", "Any binary sensor / event / calendar that is 'on' or lists windows during Power Down events."),
    "forecast_device": ("Solar forecast device", "Pick your Forecast.Solar device - that is all. Its sensors and array size are found automatically. Leave blank to auto-use it if it is the only one installed."),
    "forecast_ref_kwp": ("Forecast array size override (kWp)", "Optional. Normally read from the Forecast.Solar device. The twin scales that array to the one you are modelling."),
    "forecast_scale_pct": ("Forecast confidence (%)", "100 = trust the forecast. Lower (e.g. 85) makes the planner assume less sun than forecast, so it charges more overnight."),
    "consumption_stat": ("Home electricity consumption sensor (kWh)", "Your whole-home import kWh total - e.g. the Octopus electricity 'Current Total Consumption' sensor. Its long-term history teaches the twin your usage pattern."),
    "ev_stats": ("Other EV / charger energy sensors (kWh)", "Optional. Only needed for cars you did not add under Vehicles. If blank, off-peak demand above the night base load is assumed to be the cars."),
    "history_days": ("Days of history to learn from", ""),
    "cheap_start": ("Cheap window start", ""), "cheap_end": ("Cheap window end", ""),
    "cheap_p": ("Cheap import rate (p/kWh)", "Used when no rate entity covers a slot."),
    "standard_p": ("Standard import rate (p/kWh)", ""), "export_p": ("Export rate (p/kWh)", ""),
    "power_up_import_p": ("Power Up import rate (p/kWh)", "Placeholder - set to the actual terms of the event."),
    "power_down_export_p": ("Power Down export rate (p/kWh)", "Placeholder - set to the actual terms of the event."),
    "power_down_bonus_p": ("Power Down bonus (p/kWh discharged)", "Extra reward per kWh the battery discharges during an event."),
    "pv_kwp": ("Solar array (kWp)", "0 = no solar."), "inverter_kw": ("Inverter AC rating (kW)", "Clips solar and caps battery power (hybrid inverter assumption)."),
    "pv_annual_yield": ("Annual yield (kWh per kWp)", "From PVGIS or your installer for your roof. UK south-facing is ~900-1000."),
    "battery_kwh": ("Battery usable capacity (kWh)", "0 = no battery."), "battery_max_kw": ("Battery max charge/discharge (kW)", ""),
    "round_trip_eff": ("Round-trip efficiency (0-1)", ""), "reserve_pct": ("Reserve (%)", "Never discharged below this."),
    "export_limit_kw": ("Export limit (kW)", "3.68 kW is the G98 single-phase default."),
    "cycle_cost_p": ("Wear cost (p per kWh discharged)", "Optional. Makes the planner avoid low-value cycling."),
    "system_cost": ("System cost (£)", "The whole installed price for this setup in one number: solar, battery, inverter, heat pump, induction hob, install - whatever this scenario includes."),
    "grant": ("Grant (£)", "Deducted from the system cost, e.g. the Boiler Upgrade Scheme. Check current eligibility and amount."),
    "finance_mode": ("How you pay", "Cash, a loan (interest rate over a term), or a fixed monthly payment as quoted (e.g. £149 a month for 120 months)."),
    "finance_fee": ("Arrangement fee (£)", "Paid up front when financing."),
    "finance_monthly": ("Fixed monthly payment (£/month)", "Only used for 'Fixed monthly payment'. The twin works out the interest rate you are really paying."),
    "deposit": ("Deposit (£)", "Paid up front when financing."), "apr_pct": ("Loan interest rate - APR (%)", "Only used for 'Loan'. 0 = interest free."), "term_months": ("Finance term (months)", "0 = pay in cash."),
    "tariff_escalation_pct": ("Tariff inflation (%/yr)", ""), "pv_degradation_pct": ("Solar degradation (%/yr)", ""),
    "battery_degradation_pct": ("Battery degradation (%/yr)", ""), "discount_rate_pct": ("Discount rate (%)", ""),
    "horizon_years": ("Analysis horizon (years)", ""),
    "strategy": ("Strategy", ""), "margin_pct": ("Safety margin on evening demand (%)", ""),
    "export_from": ("Battery export window opens", "Battery power is only sent to the GRID inside this late-evening window - never overnight while the grid is cheap, which would just cycle the battery again. It still covers the house any time, and surplus solar still exports."),
    "export_until": ("Battery export window closes", "Normally when the cheap window starts (23:30)."),
    "event_export_anywhere": ("Power Down events can export outside that window", "Octopus Power Down events are paid, so they may override the window. Turn off to keep strictly to it."), "export_margin_p": ("Required export margin (p/kWh)", "Export only if it beats re-buying at the cheap rate by this much."),
    "arbitrage": ("Allow cheap-grid-to-export arbitrage", "Charge from the cheap window specifically to export later."),
    "allow_battery_export": ("Allow exporting from the battery", ""),
    "night_base_load_kw": ("Night base load (kW)", "Used to separate cars from house when no EV statistics are given."),
    "default_daily_house_kwh": ("Typical house use (kWh/day)", "Used until enough history exists."), "default_daily_ev_kwh": ("Typical EV charging (kWh/day)", ""),
    "power_down_per_month": ("Assumed Power Downs per month", "For the annual what-if only."), "power_down_start_hour": ("Power Down start hour", ""),
    "power_down_hours": ("Power Down length (h)", ""), "power_up_per_month": ("Assumed Power Ups per month", ""),
    "power_up_start_hour": ("Power Up start hour", ""), "power_up_hours": ("Power Up length (h)", ""),
    "name": ("Scenario name", "e.g. '16 kWh + 6 kWp'. Each scenario is its own virtual battery, so you can compare setups side by side."),
    "copy_from": ("Copy settings from", "Optional: start from an existing scenario's entities and settings."),
    "gas_stat": ("Gas consumption sensor (kWh)", "Your gas meter's kWh total - e.g. the Octopus gas 'Current Total Consumption' or 'Accumulative Consumption' sensor (kWh, not m³). Its long-term history is used to split hot water from heating."),
    "gas_rate_entity": ("Gas unit rate sensor", "Your current gas price per kWh (e.g. the Octopus gas 'Current Rate' sensor, GBP/kWh or p/kWh). Used instead of the fixed rate when set."),
    "gas_standing_entity": ("Gas standing charge sensor", "Optional. e.g. the Octopus gas 'Current Standing Charge' sensor (per day). Counted as saved if you disconnect gas."),
    "outdoor_temp_entity": ("Outdoor temperature sensor", "Its history is used to fit gas use against weather, and its current value nudges the forecast."),
    "weather_entity": ("Weather entity (hourly forecast)", "Optional. Gives the planner the real forecast instead of a seasonal curve."),
    "tank_temp_entity": ("Hot water tank temperature", "Optional. If you have a real tank sensor it overrides the model."),
    "main_fuse_a": ("Main cut-out fuse (A)", "Typically 100 A for a single-phase UK home; older homes can be 60-80 A."),
    "supply_phases": ("Supply", ""), "supply_voltage": ("Voltage (V)", ""),
    "fuse_usable_pct": ("Usable share of the fuse (%)", "The planner keeps grid import below this share of the fuse rating."),
    "hp_enabled": ("Model a heat pump + induction hob", "Replaces the gas combi (heating and hot water) and gas hob."),
    "tank_litres": ("Hot water tank (litres)", ""), "tank_setpoint_c": ("Tank set point (°C)", ""),
    "tank_loss_kwh_day": ("Tank standing loss (kWh/day)", "0 = estimate from the tank size."),
    "legionella_c": ("Weekly legionella boost (°C)", "Set to the set point to switch off."),
    "dhw_start": ("Hot water charge starts at", "Main tank charge, inside the cheap window. It also tops up during Power Up and smart-charge slots."),
    "hp_elec_max_kw": ("Heat pump electrical power (kW)", ""), "hp_capacity_kw": ("Heat pump output at 7°C (kW)", "Undersized on cold days = resistive backup, shown as a higher bill."),
    "flow_temp_c": ("Space-heating flow temperature (°C)", "Lower is better: ~35 underfloor, 45 good radiators, 55+ old radiators."),
    "carnot_frac": ("Efficiency vs ideal (Carnot fraction)", "0.35 poor, 0.42 typical, 0.5 excellent."),
    "base_temp_c": ("Heating base temperature (°C)", "Outdoor temperature below which heating is needed."),
    "boiler_eff_heating": ("Boiler efficiency - heating", ""), "boiler_eff_dhw": ("Boiler efficiency - hot water", "Lower than heating: combis lose a lot on short draws."),
    "cooking_gas_kwh_day": ("Gas hob use (kWh/day)", ""), "induction_ratio": ("Induction electric per gas kWh", "~0.5: induction is roughly twice as efficient."),
    "fallback_annual_gas_kwh": ("Typical annual gas (kWh)", "Used until enough gas + temperature history exists."),
    "gas_unit_p": ("Gas unit rate - fallback (p/kWh)", "Used only if no gas rate sensor is set (or it is unavailable)."), "gas_standing_p_day": ("Gas standing charge - fallback (p/day)", "Used only if no standing charge sensor is set."),
    "gas_disconnect": ("Disconnect gas afterwards", "Counts the avoided standing charge as a saving."),
    "hardware_preset": ("Hardware", "Pick a starting point; every value stays editable afterwards."),
    "tariff_preset": ("Tariff", "Pick a starting point; live rate entities override these."),
}
SEC = {
    "user": ("New scenario", "Add one scenario per setup you want to compare.", ["name", "copy_from"]),
    "tariff_preset": ("Choose your tariff", "", ["tariff_preset"]),
    "hardware_preset": ("Hardware preset", "Overwrites the hardware numbers with the preset's values.", ["hardware_preset"]),
    "sources_live": ("Live data", "All optional. The twin works from the tariff and a typical profile without them.",
                     ["power_entity", "ev_power_entities", "pv_now_entity", "import_now_entity", "export_now_entity"]),
    "sources_octopus": ("Octopus Energy", "Entities from the Octopus Energy integration, or your own.",
                        ["import_rates_entities", "export_rates_entities", "dispatch_entity", "power_up_entity", "power_down_entity"]),
    "sources_solar_history": ("Solar forecast & history", "", ["forecast_device", "forecast_ref_kwp", "forecast_scale_pct", "consumption_stat", "ev_stats", "history_days"]),
    "sources_heat": ("Heat pump data", "Gas history and outdoor temperature let the twin work out how much of your gas is heating and how much is hot water.",
                     ["gas_stat", "gas_rate_entity", "gas_standing_entity", "outdoor_temp_entity", "weather_entity", "tank_temp_entity"]),
    "supply": ("Supply & main fuse", "Limits how much the planner draws from the grid (cars + heat pump + battery charging).",
               ["main_fuse_a", "supply_phases", "supply_voltage", "fuse_usable_pct"]),
    "heat": ("Heat pump & tank", "", ["hp_enabled", "tank_litres", "tank_setpoint_c", "tank_loss_kwh_day", "legionella_c", "dhw_start", "hp_elec_max_kw", "hp_capacity_kw", "flow_temp_c", "carnot_frac", "base_temp_c"]),
    "heat_costs": ("Gas replaced & costs", "", ["boiler_eff_heating", "boiler_eff_dhw", "cooking_gas_kwh_day", "induction_ratio", "fallback_annual_gas_kwh", "gas_unit_p", "gas_standing_p_day", "gas_disconnect"]),
    "tariff": ("Tariff", "", ["cheap_start", "cheap_end", "cheap_p", "standard_p", "export_p", "power_up_import_p", "power_down_export_p", "power_down_bonus_p"]),
    "hardware": ("Solar & battery", "", ["pv_kwp", "inverter_kw", "pv_annual_yield", "battery_kwh", "battery_max_kw", "round_trip_eff", "reserve_pct", "export_limit_kw", "cycle_cost_p"]),
    "finance": ("Costs & finance", "", ["system_cost", "grant", "finance_mode", "deposit", "finance_fee", "apr_pct", "term_months", "finance_monthly", "tariff_escalation_pct", "pv_degradation_pct", "battery_degradation_pct", "discount_rate_pct", "horizon_years"]),
    "strategy": ("Strategy", "", ["strategy", "margin_pct", "export_from", "export_until", "event_export_anywhere", "export_margin_p", "arbitrage", "allow_battery_export"]),
    "assumptions": ("Assumptions", "", ["night_base_load_kw", "default_daily_house_kwh", "default_daily_ev_kwh", "power_down_per_month", "power_down_start_hour", "power_down_hours", "power_up_per_month", "power_up_start_hour", "power_up_hours"]),
}

VF = {  # vehicle form fields
    "device_id": ("Vehicle device", "Pick the car's device (e.g. your Tesla). Its battery, charger power, plugged-in, location, charge-limit and energy entities are found automatically. The fields below only override or add to that - or fill them all in for a car with no device."),
    "target_soc_entity": ("Charge limit entity (%) - override", "Optional. e.g. a Tesla's charge-limit number. When present it is used as the charge target instead of the fixed value below."),
    "name": ("Vehicle name", "Optional if you picked a device (its name is used). Used to label the car on the dashboard."),
    "mode": ("How it charges", "Only one car per account is usually on Intelligent Octopus Go. The other charges on the off-peak schedule."),
    "power_entity": ("Charging power sensor", "Optional. W or kW from the car or charger integration. Lets the twin see real charging and learn session sizes."),
    "plug_entity": ("Plugged-in entity - override", "Optional. A binary sensor, or a charging-status sensor - states like on/off, connected/disconnected, charging, stopped are understood."),
    "home_entity": ("At-home entity - override", "Optional. A device tracker / person / binary sensor. Any zone other than 'home' counts as away."),
    "soc_entity": ("Battery level entity (%) - override", "Optional. With it the twin knows exactly how much the car still needs."),
    "capacity_kwh": ("Battery size (kWh)", ""), "target_soc_pct": ("Charge target (%)", ""),
    "charger_kw": ("Charger power (kW)", ""),
    "session_kwh": ("Typical energy per charge (kWh)", "0 = learn it from observed sessions. Used when the car has no battery-level entity."),
    "energy_stat": ("Charging energy sensor (kWh) - override", "Optional. Found automatically for a Tesla. Its history lets the twin learn this car's pattern straight away."),
    "plug_states": ("Plugged-in states (override)", "Only if your integration uses unusual wording. Comma-separated, e.g. 'ready,charging'. Blank = auto-detect."),
    "home_states": ("At-home states (override)", "Comma-separated, e.g. 'home,garage'. Blank = auto-detect."),
}


def steps(keys=None):
    out = {}
    for sid, (title, desc, ks) in SEC.items():
        if keys and sid not in keys:
            continue
        out[sid] = {"title": title, **({"description": desc} if desc else {}),
                    "data": {k: F[k][0] for k in ks},
                    "data_description": {k: F[k][1] for k in ks if F[k][1]}}
    return out

def vsteps():
    return {
      "vehicles": {"title": "Vehicles", "description": "Model each car separately so charging is predicted from whether it is plugged in, at home or dispatched - not averaged over every night.\n\nCurrent vehicles:\n{vehicles}",
                   "menu_options": menus.options(menus.VEHICLE_MENU)},
      "vehicle_add": {"title": "Vehicle", "description": "Pick the car's **device** and give it a mode - that is usually all you need. Charge target comes from the car's own charge limit when it has one.", "data": {k: v[0] for k, v in VF.items()}, "data_description": {k: v[1] for k, v in VF.items() if v[1]}},
      "vehicle_edit": {"title": "Edit which vehicle?", "data": {"name": "Vehicle"}},
      "vehicle_remove": {"title": "Remove which vehicle?", "data": {"name": "Vehicle"}},
    }

ENT = {
 "sensor": {"soc": "Battery state of charge", "stored_energy": "Battery stored energy", "battery_power": "Battery power",
   "grid_power": "Grid power", "solar_power": "Solar power", "house_load": "House load", "mode": "Twin mode",
   "import_price": "Import price", "export_price": "Export price", "saving_today": "Saving today",
   "saving_month": "Saving this month", "saving_total": "Saving total", "cost_today_actual": "Cost today (actual)",
   "cost_today_twin": "Cost today (twin)", "solar_today": "Solar generated today", "export_today": "Exported today",
   "ev_tonight": "EV charging expected tonight", "hp_power": "Heat pump power", "hp_tank_temp": "Hot water tank temperature (est.)", "peak_import": "Peak grid import (plan)", "fuse_usage": "Main fuse usage (plan peak)", "annual_hp_saving": "Annual heat pump saving", "plan_target_soc": "Planned overnight charge target", "plan_battery_export": "Planned battery export",
   "plan_saving": "Plan saving (next day)", "annual_saving": "Annual saving", "annual_pv_saving": "Annual saving from solar",
   "annual_battery_saving": "Annual saving from battery", "payback_years": "Payback", "capex": "Outlay",
   "monthly_net_cashflow": "Monthly net cashflow", "monthly_payment": "Monthly finance payment",
   "net_gain": "Net gain over horizon", "npv": "Net present value", "sweep_best": "Best sweep result",
   "next_event": "Next Power event", "diagnostics": "Diagnostics"},
 "number": {"pv_kwp": "Solar array size", "battery_kwh": "Battery capacity", "inverter_kw": "Inverter size",
   "battery_max_kw": "Battery max power", "reserve_pct": "Battery reserve", "export_limit_kw": "Export limit",
   "pv_annual_yield": "Annual yield per kWp", "export_p": "Export rate", "cheap_p": "Cheap import rate",
   "standard_p": "Standard import rate", "tank_litres": "Hot water tank size", "main_fuse_a": "Main fuse", "gas_unit_p": "Gas unit rate", "hp_capacity_kw": "Heat pump capacity", "system_cost": "System cost", "grant": "Grant", "finance_deposit": "Finance deposit", "finance_apr": "Loan interest rate", "finance_term_months": "Finance term", "finance_monthly": "Fixed monthly payment",
   "power_down_per_month": "Assumed Power Downs per month", "power_up_per_month": "Assumed Power Ups per month",
   "event_rate_p": "Event rate override"},
 "select": {"strategy": "Strategy", "hardware_preset": "Hardware preset", "event_kind": "Event type", "finance_mode": "How you pay"},
 "datetime": {"event_start": "Event start", "event_end": "Event end"},
 "switch": {"arbitrage": "Grid-to-export arbitrage", "allow_battery_export": "Allow battery export", "hp_enabled": "Model heat pump + induction", "gas_disconnect": "Disconnect gas"},
 "button": {"recalculate": "Recalculate annual what-if", "run_sweep": "Run size sweep", "reset_twin": "Reset virtual battery", "add_event": "Add event", "clear_events": "Clear manual events"},
}
doc = {
  "config": {"step": steps(["user", "hardware_preset", "hardware", "tariff_preset", "sources_live", "sources_octopus", "sources_solar_history"]),
             "abort": {"already_configured": "Home Energy Twin is already set up."}},
  "options": {"error": {"name_required": "Pick a vehicle device or enter a name."}, "step": {**steps(), **vsteps(), **{k: {"title": t, "description": d, "menu_options": menus.options(menus.TOP if k == "init" else menus.SECTIONS[k])}
             for k, (t, d) in menus.SECTION_TEXT.items()}}},
  "entity": {p: {k: {"name": v} for k, v in d.items()} for p, d in ENT.items()},
  "services": {
    "add_event": {"name": "Add Power Up / Power Down event", "description": "Tell the twin about an Octopus event window.",
      "fields": {"kind": {"name": "Kind", "description": "power_up or power_down"}, "start": {"name": "Start", "description": ""},
                 "end": {"name": "End", "description": ""}, "import_rate_p": {"name": "Import rate (p/kWh)", "description": "Overrides the default."},
                 "export_rate_p": {"name": "Export rate (p/kWh)", "description": "Overrides the default."}, "scenario": {"name": "Scenario", "description": "Ignored: events are shared by all scenarios"}, "bonus_p": {"name": "Bonus (p/kWh)", "description": "Overrides the default."}}},
    "clear_events": {"name": "Clear manual events", "description": "Remove events added with add_event.", "fields": {"scenario": {"name": "Scenario", "description": "Ignored: events are shared by all scenarios"}}},
    "run_sweep": {"name": "Run size sweep", "description": "Evaluate combinations of battery size, array size and inverter size over a year.",
      "fields": {"battery_kwh": {"name": "Battery sizes (kWh)", "description": "List, e.g. [0, 5, 10]"}, "pv_kwp": {"name": "Array sizes (kWp)", "description": "List"},
                 "inverter_kw": {"name": "Inverter sizes (kW)", "description": "Optional list"}, "scenario": {"name": "Scenario", "description": "Scenario name; blank = default"}, "strategy": {"name": "Strategy", "description": "rules is much faster"}}},
    "recalculate": {"name": "Recalculate", "description": "Re-run the plan and annual what-if.", "fields": {"scenario": {"name": "Scenario", "description": "Blank = all scenarios"}}},
    "reset_twin": {"name": "Reset twin", "description": "Reset the virtual battery and savings ledger.", "fields": {"scenario": {"name": "Scenario", "description": "Blank = all scenarios"}}}},
}
root = Path(__file__).resolve().parents[1] / "custom_components" / "energy_twin"
(root / "translations").mkdir(exist_ok=True)
for p in (root / "strings.json", root / "translations" / "en.json"):
    p.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
print("written")
