"""Ready-made hardware / tariff starting points.

IMPORTANT: figures are approximate, from public marketing material, and intended as sensible
starting points. Always check the datasheet / your installer's quote and edit the values -
every field stays editable after a preset is applied. UK variants of products can differ
from the numbers quoted for other markets.
"""
from __future__ import annotations

CUSTOM = "custom"

# keys match the hardware option keys in const.py
HARDWARE_PRESETS: dict[str, dict] = {
    "custom": {},
    "tesla_powerwall_3": {
        "label": "Tesla Powerwall 3 (13.5 kWh, integrated hybrid inverter)",
        "battery_kwh": 13.5, "battery_max_kw": 10.0, "inverter_kw": 10.0,
        "round_trip_eff": 0.89, "reserve_pct": 10.0, "pv_kwp": 8.0,
        "note": "Inverter is built in; PV input up to ~20 kW DC. Check your market's continuous kW rating.",
    },
    "sigenstor_5kw_8kwh": {
        "label": "SigenStor 5 kW hybrid + 8 kWh",
        "battery_kwh": 8.0, "battery_max_kw": 5.0, "inverter_kw": 5.0,
        "round_trip_eff": 0.90, "reserve_pct": 10.0, "pv_kwp": 6.0,
        "note": "Single phase. Modular: each battery module adds capacity.",
    },
    "sigenstor_5kw_16kwh": {
        "label": "SigenStor 5 kW hybrid + 16 kWh (2 modules)",
        "battery_kwh": 16.0, "battery_max_kw": 5.0, "inverter_kw": 5.0,
        "round_trip_eff": 0.90, "reserve_pct": 10.0, "pv_kwp": 6.0,
    },
    "sigenstor_8kw_24kwh": {
        "label": "SigenStor 8 kW hybrid + 24 kWh (3 modules)",
        "battery_kwh": 24.0, "battery_max_kw": 8.0, "inverter_kw": 8.0,
        "round_trip_eff": 0.90, "reserve_pct": 10.0, "pv_kwp": 9.0,
        "note": "Above 3.68 kW per phase export needs a G99 application - raise 'export limit' only if approved.",
    },
    "generic_5kwh": {
        "label": "Generic AC/hybrid 5 kWh battery",
        "battery_kwh": 5.0, "battery_max_kw": 2.5, "inverter_kw": 3.6,
        "round_trip_eff": 0.90, "reserve_pct": 10.0, "pv_kwp": 4.0,
    },
    "generic_10kwh": {
        "label": "Generic hybrid 10 kWh battery",
        "battery_kwh": 10.0, "battery_max_kw": 5.0, "inverter_kw": 5.0,
        "round_trip_eff": 0.90, "reserve_pct": 10.0, "pv_kwp": 6.0,
    },
    "battery_only_10kwh": {
        "label": "Battery only, no solar (10 kWh, 5 kW) - IOG arbitrage",
        "battery_kwh": 10.0, "battery_max_kw": 5.0, "inverter_kw": 5.0,
        "round_trip_eff": 0.90, "reserve_pct": 10.0, "pv_kwp": 0.0,
    },
    "solar_only_6kwp": {
        "label": "Solar only, no battery (6 kWp)",
        "battery_kwh": 0.0, "battery_max_kw": 0.0, "inverter_kw": 5.0,
        "round_trip_eff": 0.90, "reserve_pct": 10.0, "pv_kwp": 6.0,
    },
}

TARIFF_PRESETS: dict[str, dict] = {
    "custom": {},
    "intelligent_go": {
        "label": "Intelligent Octopus Go (23:30-05:30 + smart-charge slots)",
        "cheap_start": "23:30:00", "cheap_end": "05:30:00",
        "cheap_p": 7.0, "standard_p": 27.0,
        "note": "Placeholder prices - the live rate entities override these when selected.",
    },
    "flat": {
        "label": "Flat rate (no cheap window)",
        "cheap_start": "00:00:00", "cheap_end": "00:00:00",
        "cheap_p": 25.0, "standard_p": 25.0,
    },
}

HARDWARE_KEYS = ("battery_kwh", "battery_max_kw", "inverter_kw", "round_trip_eff", "reserve_pct", "pv_kwp",
                 )


def apply_preset(table: dict[str, dict], name: str) -> dict:
    """Return only the option values a preset defines (no label/note)."""
    p = table.get(name) or {}
    return {k: v for k, v in p.items() if k not in ("label", "note")}


def preset_choices(table: dict[str, dict]) -> dict[str, str]:
    return {k: (v.get("label") or "Custom (enter everything yourself)") for k, v in table.items()}
