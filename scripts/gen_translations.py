"""Regenerates strings.json + translations/en.json. Run: python scripts/gen_translations.py"""
import json
from pathlib import Path

F = {  # option key -> (label, help)
    "power_entity": ("House power now (Home Mini)", "Whole-home import power sensor, W or kW. Used to drive the live twin."),
    "ev_power_entities": ("Car charger power sensors", "Optional. Lets the twin separate EV load from house load, so the battery never 'feeds' a car."),
    "pv_now_entity": ("Expected solar generation now", "Real-time expected/forecast solar power sensor (W or kW), for the array size set below. Overrides the hourly forecast for the current slot."),
    "import_now_entity": ("Current import price", "Current tariff price entity (GBP/kWh or p/kWh, auto-detected)."),
    "export_now_entity": ("Current export price", "Current export tariff price entity (GBP/kWh or p/kWh)."),
    "import_rates_entities": ("Import rates entities", "Octopus Energy integration rate entities (current day + next day). Their 'rates' attribute is read."),
    "export_rates_entities": ("Export rates entities", "Octopus export rate entities, if you want real export prices (e.g. Agile Outgoing)."),
    "dispatch_entity": ("Intelligent dispatching entity", "Octopus Intelligent 'dispatching' binary sensor - planned smart-charge slots are priced at the cheap rate."),
    "power_up_entity": ("Power Up event entity", "Any binary sensor / event / calendar that is 'on' or lists windows during Power Up events."),
    "power_down_entity": ("Power Down event entity", "Any binary sensor / event / calendar that is 'on' or lists windows during Power Down events."),
    "forecast_entry": ("Solar forecast integration", "Forecast.Solar, Solcast or Open-Meteo Solar Forecast config entry."),
    "forecast_ref_kwp": ("Forecast array size (kWp)", "The array size that forecast integration is configured for. The twin scales it to your modelled array."),
    "consumption_stat": ("Home consumption statistic", "Long-term statistic id for whole-home import kWh (an energy sensor, or e.g. octopus_energy:electricity_<serial>_<mpan>_import_consumption - see Developer tools > Statistics)."),
    "ev_stats": ("EV energy statistics", "Comma-separated statistic ids for your chargers' kWh. If blank, off-peak demand above the night base load is assumed to be the cars."),
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
    "pv_cost_fixed": ("Solar fixed cost (£)", ""), "pv_cost_per_kwp": ("Solar cost per kWp (£)", ""),
    "battery_cost_fixed": ("Battery fixed cost (£)", ""), "battery_cost_per_kwh": ("Battery cost per kWh (£)", ""),
    "other_cost": ("Other install costs (£)", "Scaffolding, consumer unit, EV-safe isolation, etc."),
    "deposit": ("Deposit (£)", ""), "apr_pct": ("Finance APR (%)", ""), "term_months": ("Finance term (months)", "0 = pay in cash."),
    "tariff_escalation_pct": ("Tariff inflation (%/yr)", ""), "pv_degradation_pct": ("Solar degradation (%/yr)", ""),
    "battery_degradation_pct": ("Battery degradation (%/yr)", ""), "discount_rate_pct": ("Discount rate (%)", ""),
    "horizon_years": ("Analysis horizon (years)", ""),
    "strategy": ("Strategy", ""), "margin_pct": ("Safety margin on evening demand (%)", ""),
    "export_from": ("Earliest battery export time (rules mode)", ""), "export_margin_p": ("Required export margin (p/kWh)", "Export only if it beats re-buying at the cheap rate by this much."),
    "arbitrage": ("Allow cheap-grid-to-export arbitrage", "Charge from the cheap window specifically to export later."),
    "allow_battery_export": ("Allow exporting from the battery", ""),
    "night_base_load_kw": ("Night base load (kW)", "Used to separate cars from house when no EV statistics are given."),
    "default_daily_house_kwh": ("Typical house use (kWh/day)", "Used until enough history exists."), "default_daily_ev_kwh": ("Typical EV charging (kWh/day)", ""),
    "power_down_per_month": ("Assumed Power Downs per month", "For the annual what-if only."), "power_down_start_hour": ("Power Down start hour", ""),
    "power_down_hours": ("Power Down length (h)", ""), "power_up_per_month": ("Assumed Power Ups per month", ""),
    "power_up_start_hour": ("Power Up start hour", ""), "power_up_hours": ("Power Up length (h)", ""),
    "name": ("Scenario name", "e.g. '16 kWh + 6 kWp'. Each scenario is its own virtual battery, so you can compare setups side by side."),
    "copy_from": ("Copy settings from", "Optional: start from an existing scenario's entities and settings."),
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
    "sources_solar_history": ("Solar forecast & history", "", ["forecast_entry", "forecast_ref_kwp", "consumption_stat", "ev_stats", "history_days"]),
    "tariff": ("Tariff", "", ["cheap_start", "cheap_end", "cheap_p", "standard_p", "export_p", "power_up_import_p", "power_down_export_p", "power_down_bonus_p"]),
    "hardware": ("Solar & battery", "", ["pv_kwp", "inverter_kw", "pv_annual_yield", "battery_kwh", "battery_max_kw", "round_trip_eff", "reserve_pct", "export_limit_kw", "cycle_cost_p"]),
    "finance": ("Costs & finance", "", ["pv_cost_fixed", "pv_cost_per_kwp", "battery_cost_fixed", "battery_cost_per_kwh", "other_cost", "deposit", "apr_pct", "term_months", "tariff_escalation_pct", "pv_degradation_pct", "battery_degradation_pct", "discount_rate_pct", "horizon_years"]),
    "strategy": ("Strategy", "", ["strategy", "margin_pct", "export_from", "export_margin_p", "arbitrage", "allow_battery_export"]),
    "assumptions": ("Assumptions", "", ["night_base_load_kw", "default_daily_house_kwh", "default_daily_ev_kwh", "power_down_per_month", "power_down_start_hour", "power_down_hours", "power_up_per_month", "power_up_start_hour", "power_up_hours"]),
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

ENT = {
 "sensor": {"soc": "Battery state of charge", "stored_energy": "Battery stored energy", "battery_power": "Battery power",
   "grid_power": "Grid power", "solar_power": "Solar power", "house_load": "House load", "mode": "Twin mode",
   "import_price": "Import price", "export_price": "Export price", "saving_today": "Saving today",
   "saving_month": "Saving this month", "saving_total": "Saving total", "cost_today_actual": "Cost today (actual)",
   "cost_today_twin": "Cost today (twin)", "solar_today": "Solar generated today", "export_today": "Exported today",
   "plan_target_soc": "Planned overnight charge target", "plan_battery_export": "Planned battery export",
   "plan_saving": "Plan saving (next day)", "annual_saving": "Annual saving", "annual_pv_saving": "Annual saving from solar",
   "annual_battery_saving": "Annual saving from battery", "payback_years": "Payback", "capex": "Outlay",
   "monthly_net_cashflow": "Monthly net cashflow", "monthly_payment": "Monthly finance payment",
   "net_gain": "Net gain over horizon", "npv": "Net present value", "sweep_best": "Best sweep result",
   "next_event": "Next Power event", "diagnostics": "Diagnostics"},
 "number": {"pv_kwp": "Solar array size", "battery_kwh": "Battery capacity", "inverter_kw": "Inverter size",
   "battery_max_kw": "Battery max power", "reserve_pct": "Battery reserve", "export_limit_kw": "Export limit",
   "pv_annual_yield": "Annual yield per kWp", "export_p": "Export rate", "cheap_p": "Cheap import rate",
   "standard_p": "Standard import rate", "pv_cost_per_kwp": "Solar cost per kWp", "battery_cost_per_kwh": "Battery cost per kWh",
   "power_down_per_month": "Assumed Power Downs per month", "power_up_per_month": "Assumed Power Ups per month",
   "event_rate_p": "Event rate override"},
 "select": {"strategy": "Strategy", "hardware_preset": "Hardware preset", "event_kind": "Event type"},
 "datetime": {"event_start": "Event start", "event_end": "Event end"},
 "switch": {"arbitrage": "Grid-to-export arbitrage", "allow_battery_export": "Allow battery export"},
 "button": {"recalculate": "Recalculate annual what-if", "run_sweep": "Run size sweep", "reset_twin": "Reset virtual battery", "add_event": "Add event", "clear_events": "Clear manual events"},
}
doc = {
  "config": {"step": steps(["user", "hardware_preset", "hardware", "tariff_preset", "sources_live", "sources_octopus", "sources_solar_history"]),
             "abort": {"already_configured": "Home Energy Twin is already set up."}},
  "options": {"step": {**steps(), "init": {"title": "Home Energy Twin", "description": "Change any section, then choose Done.",
      "menu_options": {"sources_live": "Live data entities", "sources_octopus": "Octopus entities",
        "sources_solar_history": "Solar forecast & history", "hardware_preset": "Apply a hardware preset",
        "hardware": "Solar & battery", "tariff_preset": "Apply a tariff preset", "tariff": "Tariff",
        "finance": "Costs & finance", "strategy": "Strategy", "assumptions": "Assumptions", "done": "Save and finish"}}}},
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
