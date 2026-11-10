"""Options-menu structure and labels. Pure data - shared by the flow and the translation generator.

Labels are passed to Home Assistant straight from the flow (menu_options as {step: label}), so the menu is
readable even if a translation file is stale or not loaded; translations/en.json carries the same text.
"""
from __future__ import annotations

TOP = ["menu_sources", "menu_kit", "menu_money", "vehicles", "menu_heat", "menu_planning", "done"]

SECTIONS: dict[str, list[str]] = {
    "menu_sources": ["sources_live", "sources_octopus", "sources_solar_history", "init"],
    "menu_kit": ["hardware_preset", "hardware", "supply", "init"],
    "menu_money": ["tariff_preset", "tariff", "finance", "init"],
    "menu_heat": ["heat", "heat_costs", "sources_heat", "init"],
    "menu_planning": ["strategy", "assumptions", "init"],
}
VEHICLE_MENU = ["vehicle_add", "vehicle_edit", "vehicle_remove", "init"]

LABELS: dict[str, str] = {
    # top level
    "menu_sources": "Data sources",
    "menu_kit": "Solar, battery & supply",
    "menu_money": "Tariff, costs & finance",
    "vehicles": "Vehicles (EV charging)",
    "menu_heat": "Heat pump & induction hob",
    "menu_planning": "Strategy & assumptions",
    "done": "Save and finish",
    "init": "Back",
    # data sources
    "sources_live": "Live data (power, prices)",
    "sources_octopus": "Octopus (rates, dispatches, events)",
    "sources_solar_history": "Solar forecast & consumption history",
    # solar, battery & supply
    "hardware_preset": "Choose a hardware preset",
    "hardware": "Solar & battery details",
    "supply": "Main fuse & supply",
    # money
    "tariff_preset": "Choose a tariff preset",
    "tariff": "Tariff rates",
    "finance": "Costs & finance",
    # heat pump
    "heat": "Heat pump & hot water tank",
    "heat_costs": "Gas replaced & costs",
    "sources_heat": "Gas & temperature data",
    # planning
    "strategy": "Strategy",
    "assumptions": "Assumptions",
    # vehicles
    "vehicle_add": "Add a vehicle",
    "vehicle_edit": "Edit a vehicle",
    "vehicle_remove": "Remove a vehicle",
}

SECTION_TEXT: dict[str, tuple[str, str]] = {
    "init": ("Home Energy Twin", "Choose a section. Choose **Save and finish** when you're done."),
    "menu_sources": ("Data sources", "Where the twin reads your real load, prices, solar forecast and history."),
    "menu_kit": ("Solar, battery & supply", "The kit being modelled and your main fuse."),
    "menu_money": ("Tariff, costs & finance", "Prices, what the kit costs, and how it's paid for."),
    "menu_heat": ("Heat pump & induction hob", "Model replacing the gas combi and gas hob."),
    "menu_planning": ("Strategy & assumptions", "How the battery is planned, and the assumptions behind the yearly model."),
}


def options(ids: list[str]) -> dict[str, str]:
    """{step_id: label} for async_show_menu."""
    return {i: LABELS[i] for i in ids}
