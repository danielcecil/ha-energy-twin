"""Constants and option defaults."""
from __future__ import annotations

DOMAIN = "energy_twin"
TZ_DEFAULT = "Europe/London"

# ---- data sources (all optional - the twin degrades gracefully without them) ----------------
CONF_POWER_ENTITY = "power_entity"                    # Home Mini / grid import power (W or kW)
CONF_EV_POWER_ENTITIES = "ev_power_entities"          # car charger power sensors
CONF_PV_NOW_ENTITY = "pv_now_entity"                  # real-time expected solar generation (W/kW)
CONF_IMPORT_NOW_ENTITY = "import_now_entity"          # current import price
CONF_EXPORT_NOW_ENTITY = "export_now_entity"          # current export price
CONF_IMPORT_RATES_ENTITIES = "import_rates_entities"  # Octopus rates entities (current/next day)
CONF_EXPORT_RATES_ENTITIES = "export_rates_entities"
CONF_DISPATCH_ENTITY = "dispatch_entity"              # Intelligent dispatching
CONF_POWER_UP_ENTITY = "power_up_entity"
CONF_POWER_DOWN_ENTITY = "power_down_entity"
CONF_FORECAST_ENTRY = "forecast_entry"                # Forecast.Solar / Solcast / Open-Meteo entry
CONF_FORECAST_REF_KWP = "forecast_ref_kwp"            # kWp that forecast integration is set up for
CONF_CONSUMPTION_STAT = "consumption_stat"            # whole-home import statistic id
CONF_EV_STATS = "ev_stats"                            # comma separated EV energy statistic ids
CONF_HISTORY_DAYS = "history_days"

# ---- presets -------------------------------------------------------------------------------
CONF_HW_PRESET = "hardware_preset"
CONF_TARIFF_PRESET = "tariff_preset"

# ---- tariff --------------------------------------------------------------------------------
CONF_CHEAP_START = "cheap_start"
CONF_CHEAP_END = "cheap_end"
CONF_CHEAP_P = "cheap_p"
CONF_STANDARD_P = "standard_p"
CONF_EXPORT_P = "export_p"
CONF_PU_IMPORT_P = "power_up_import_p"
CONF_PD_EXPORT_P = "power_down_export_p"
CONF_PD_BONUS_P = "power_down_bonus_p"

# ---- hardware ------------------------------------------------------------------------------
CONF_PV_KWP = "pv_kwp"
CONF_INVERTER_KW = "inverter_kw"
CONF_PV_YIELD = "pv_annual_yield"
CONF_BATT_KWH = "battery_kwh"
CONF_BATT_KW = "battery_max_kw"
CONF_RTE = "round_trip_eff"
CONF_RESERVE = "reserve_pct"
CONF_EXPORT_LIMIT = "export_limit_kw"
CONF_CYCLE_COST = "cycle_cost_p"

# ---- costs / finance -----------------------------------------------------------------------
CONF_PV_COST_FIXED = "pv_cost_fixed"
CONF_PV_COST_KWP = "pv_cost_per_kwp"
CONF_BATT_COST_FIXED = "battery_cost_fixed"
CONF_BATT_COST_KWH = "battery_cost_per_kwh"
CONF_OTHER_COST = "other_cost"
CONF_DEPOSIT = "deposit"
CONF_APR = "apr_pct"
CONF_TERM = "term_months"
CONF_ESCALATION = "tariff_escalation_pct"
CONF_PV_DEG = "pv_degradation_pct"
CONF_BATT_DEG = "battery_degradation_pct"
CONF_DISCOUNT = "discount_rate_pct"
CONF_HORIZON = "horizon_years"

# ---- strategy ------------------------------------------------------------------------------
CONF_STRATEGY = "strategy"
CONF_MARGIN = "margin_pct"
CONF_EXPORT_FROM = "export_from"
CONF_EXPORT_MARGIN = "export_margin_p"
CONF_ARBITRAGE = "arbitrage"
CONF_BATT_EXPORT = "allow_battery_export"

# ---- assumptions ---------------------------------------------------------------------------
CONF_NIGHT_BASE = "night_base_load_kw"
CONF_DAILY_HOUSE = "default_daily_house_kwh"
CONF_DAILY_EV = "default_daily_ev_kwh"
CONF_PD_PER_MONTH = "power_down_per_month"
CONF_PD_START_HOUR = "power_down_start_hour"
CONF_PD_HOURS = "power_down_hours"
CONF_PU_PER_MONTH = "power_up_per_month"
CONF_PU_START_HOUR = "power_up_start_hour"
CONF_PU_HOURS = "power_up_hours"

STRATEGIES = {"optimised": "Optimised (look-ahead planner)", "rules": "Simple rules (solar-first, evening discharge)"}

DEFAULTS: dict = {
    CONF_HW_PRESET: "custom", CONF_TARIFF_PRESET: "intelligent_go",
    CONF_FORECAST_REF_KWP: 1.0, CONF_HISTORY_DAYS: 365,
    CONF_CHEAP_START: "23:30:00", CONF_CHEAP_END: "05:30:00",
    CONF_CHEAP_P: 7.0, CONF_STANDARD_P: 27.0, CONF_EXPORT_P: 15.0,
    CONF_PU_IMPORT_P: 0.0, CONF_PD_EXPORT_P: 30.0, CONF_PD_BONUS_P: 0.0,
    CONF_PV_KWP: 6.0, CONF_INVERTER_KW: 5.0, CONF_PV_YIELD: 950.0,
    CONF_BATT_KWH: 10.0, CONF_BATT_KW: 5.0, CONF_RTE: 0.90, CONF_RESERVE: 10.0,
    CONF_EXPORT_LIMIT: 3.68, CONF_CYCLE_COST: 0.0,
    CONF_PV_COST_FIXED: 1500.0, CONF_PV_COST_KWP: 900.0,
    CONF_BATT_COST_FIXED: 1500.0, CONF_BATT_COST_KWH: 350.0, CONF_OTHER_COST: 0.0,
    CONF_DEPOSIT: 0.0, CONF_APR: 0.0, CONF_TERM: 0,
    CONF_ESCALATION: 3.0, CONF_PV_DEG: 0.5, CONF_BATT_DEG: 2.0, CONF_DISCOUNT: 3.0, CONF_HORIZON: 20,
    CONF_STRATEGY: "optimised", CONF_MARGIN: 15.0, CONF_EXPORT_FROM: "16:00:00",
    CONF_EXPORT_MARGIN: 1.0, CONF_ARBITRAGE: True, CONF_BATT_EXPORT: True,
    CONF_NIGHT_BASE: 0.3, CONF_DAILY_HOUSE: 9.0, CONF_DAILY_EV: 15.0,
    CONF_PD_PER_MONTH: 0, CONF_PD_START_HOUR: 17, CONF_PD_HOURS: 1,
    CONF_PU_PER_MONTH: 0, CONF_PU_START_HOUR: 13, CONF_PU_HOURS: 2,
}

# sources, in the order the config form shows them
ENTITY_KEYS = (CONF_POWER_ENTITY, CONF_EV_POWER_ENTITIES, CONF_PV_NOW_ENTITY, CONF_IMPORT_NOW_ENTITY,
               CONF_EXPORT_NOW_ENTITY, CONF_IMPORT_RATES_ENTITIES, CONF_EXPORT_RATES_ENTITIES,
               CONF_DISPATCH_ENTITY, CONF_POWER_UP_ENTITY, CONF_POWER_DOWN_ENTITY)

TICK_SECONDS = 60
REPLAN_SECONDS = 300
STORE_VERSION = 1
SERVICE_ADD_EVENT = "add_event"
SERVICE_CLEAR_EVENTS = "clear_events"
SERVICE_SWEEP = "run_sweep"
SERVICE_RECALC = "recalculate"
SERVICE_RESET_TWIN = "reset_twin"
