# Home Energy Twin

A Home Assistant custom integration that is a **digital twin of your home's energy use**, so you
can see what solar + a home battery would save you *before* buying anything.

It reads your real load (Octopus Home Mini), your real tariff (Octopus Energy integration,
Intelligent Octopus Go) and your solar forecast (Forecast.Solar / Solcast / Open-Meteo), then
runs a **virtual** battery and array alongside your house. It never controls hardware.

* **Live twin** - every minute a virtual battery (starts at 50 %; set it any time with the *Set battery level* slider
  or the `set_battery_level` service) is stepped against your actual load; a ledger
  shows today / month / total "what it would have saved".
* **Planner** - every 5 min it plans the next ~33 h: how much to charge in the cheap window, when to
  use solar, and **how much battery to export** when there's spare capacity (a dynamic-programming
  optimiser - deterministic and explainable, not a language model).
* **Power Up / Power Down** events change the plan (Power Up: charge everything; Power Down:
  pre-charge, then discharge + export).
* **Annual what-if** - 365 days replayed (your real history where you have it) with a split of
  savings between solar and battery, payback, cash vs finance cashflow, NPV.
* **Size sweep** - rank combinations of array size, battery size and inverter size by annual saving, with what each
  extra kWh of battery / kWp of array adds (judge it against your own quotes - there's no per-unit pricing).
* **Hard rule:** the battery never discharges while a car is charging.

## Install

1. Copy `custom_components/energy_twin` into your HA `config/custom_components/` (or add this repo
   to HACS as a custom repository).
2. Restart HA -> Settings -> Devices & services -> Add integration -> **Home Energy Twin**.
3. Choose a hardware preset (Powerwall 3, SigenStor, generic, battery-only, solar-only or Custom),
   a tariff preset, then pick your entities. **Every entity is optional** and everything stays
   editable later via *Configure*.
4. Dashboard: paste `dashboard/energy_twin_dashboard.yaml` into a new dashboard's raw editor. Charts
   need [apexcharts-card](https://github.com/RomRider/apexcharts-card) from HACS.

## Cars (EV charging)

Cars are modelled **individually**, not as an averaged nightly load. In *Configure -> Vehicles* add each car:

* **Pick the car's device** (e.g. each Tesla) - that is usually all you need. Its entities are found
  automatically: battery %, charger power, plugged-in / charging state, location, charge limit and energy added.
  Verified against Home Assistant's own Teslemetry, Tesla Fleet and Tessie integrations (they share entity keys);
  other makes fall back to device classes and names (battery, plug, power, device tracker).
* Anything you enter in the entity fields **overrides** the discovered one - and a car with no device at all
  can be set up entirely from entities (any make, or just a charger).
* **Charge target** comes from the car's own charge-limit entity when it has one (a Tesla's charge limit),
  otherwise the fixed % you set. Battery size and charger power are set per car (the car does not report them).

Each car is **IOG** (smart-charged: dispatch slots at any time of day plus the off-peak window) or **off-peak**
(only the off-peak window, from its start) - normally one of each. A car that is plugged in at home *will* charge;
one that isn't contributes only a learned probability for that weekday, so non-charging days no longer smear into
the plan. Learning happens only around real sessions (energy delivered, plug-in days), plus history from each car's
energy statistic. Integrations word their states differently, so there are optional "plugged-in states" /
"at-home states" overrides, and Diagnostics shows per car which entities were found and what was understood.

## Heat pump, induction hob and the main fuse

Switch on *Model a heat pump + induction hob* to see what replacing the gas combi and gas hob would do.

* **Gas inputs.** Pick your gas kWh sensor (history), a gas *unit rate* sensor and a *standing charge* sensor
  (e.g. the Octopus gas Current Rate / Current Standing Charge sensors) under *Heat pump -> Gas & temperature data*;
  the fixed numbers in *Gas replaced & costs* are only fallbacks when those sensors are missing or unavailable.
* **Gas history -> heat demand.** Daily gas is fitted against outdoor temperature (`gas = base + k x degree-days`).
  The `base` is hot water + cooking, `k` is heating. Boiler efficiencies (default 85 % heating, 72 % hot water -
  combis lose a lot on short draws) turn gas kWh into the useful heat you actually needed. With no history it
  uses a typical 11.5 MWh/yr home.
* **Heat -> electricity.** Divide by a COP that depends on outdoor and flow temperature (Carnot-fraction model),
  so cold days cost more and tank water (50-60 C) costs more than radiator water (45 C).
* **Tank.** 270 L by default; it charges from 04:00 in the cheap window, tops up during Power Up and smart-charge
  slots, and falls back to the grid at any price if it nearly runs dry. A weekly 60 C boost is included.
  If the heat pump cannot keep up (too small for a cold day) the shortfall is resistive heating at COP 1.
* **Induction.** Gas hob kWh x 0.5 -> electric kWh, shaped to meal times.
* **Money.** Net heat pump saving = gas avoided (+ standing charge if you disconnect gas) minus the extra
  electricity priced slot by slot. Solar/battery savings are then measured *with* the heat pump's load in the house
  (it makes them worth more). Put the heat pump, tank and hob in the setup's system cost, with any grant below it.
* **Main fuse.** Set the cut-out rating (default 100 A, single phase, 230 V, 90 % usable). The planner will not
  charge the battery past it, so a 7 kW car + heat pump + battery charge cannot stack beyond your supply.
  *Peak grid import* and *Main fuse usage* sensors show how close the plan gets.

**How accurate?** Hot water and cooking are fairly well pinned down by your gas history (roughly +/-15 %).
Annual space-heating electricity is more like +/-25-30 % because COP, flow temperature, emitters and sizing all
matter, and the model assumes steady all-day heating (it does not model your house's thermal mass, so it does
not pre-heat the building in the cheap window). Treat the numbers as a range and try COP-affecting settings
(flow temperature, Carnot fraction) to see the spread. Gas prices and the system cost are placeholders - check yours.

## Costs and finance

Each setup has **one system cost** (the whole installed price: solar, battery, inverter, heat pump, hob, install -
whatever that setup includes), less an optional **grant**. No per-kWh or per-kWp pricing to maintain: add a setup
per quote and compare them. Finance options:

* **Cash** - pay it all up front.
* **Loan** - APR over a term, with optional deposit and arrangement fee.
* **Fixed monthly payment** - for quotes like "£149 a month for 10 years". The twin works out the interest rate
  you are really paying and the total cost of finance (that example is ~13% APR, £7,880 on a £10,000 system).

Results: payback (with tariff inflation and panel/battery degradation), monthly net cashflow after payments, 10-year
net, NPV, and the whole annual bill (electricity + gas) today versus each setup.

## Compare several setups

Each config entry is a **scenario**: its own kit, strategy, costs, device and virtual battery, all
fed by the same real load, prices, forecast and Power Up/Down events. Add as many as you like
(Settings -> Devices & services -> Home Energy Twin -> *Add entry*), choosing *Copy settings from*
to reuse your entities. The dashboard's **Compare** tab finds every scenario automatically and ranks
them by NPV with the annual bill today-vs-setup, where the saving comes from (solar / battery / heat pump), a payback
race, a month-by-month table and a live "saved so far" ledger. Power Up/Down events are shared across scenarios;
Octopus/forecast/history lookups are cached and shared. The first setup's entities always keep the `home_energy_twin_*` IDs the dashboard uses, however you rename it
(auto-generated IDs are migrated on load; IDs you set yourself are left alone). Per-scenario tabs show one scenario -
`python scripts/gen_dashboard.py <entity_prefix> "<title>"` makes one for another.

## What to point it at

| Setting | Use |
|---|---|
| House power now | Home Mini power sensor (W or kW) |
| Car charger power | your charger sensors, so EV load never counts as house load |
| Expected solar now | your real-time expected-generation sensor (overrides the hourly forecast for "now") |
| Current import / export price | your current-tariff entities (GBP/kWh or p/kWh auto-detected) |
| Import / export rates | Octopus Energy integration rate entities (current + next day) - the `rates` attribute |
| Intelligent dispatching | Octopus `dispatching` binary sensor; planned slots are priced at the cheap rate |
| Power Up / Down entity | any binary sensor / event / calendar that is `on` or lists windows (or use the `add_event` service) |
| Solar forecast | just pick the **Forecast.Solar device** (or leave it blank - it is used automatically if it's the only one). Its sensors and array size are found for you; the twin scales that array to the one you're modelling |
| Home electricity consumption | your whole-home kWh **sensor** (e.g. Octopus *Current Total Consumption*). Its recorded history teaches the twin your pattern |

Without history it uses a typical-UK load shape; with >=14 days it learns weekday/weekend profiles;
with >=330 days it replays your actual year.

## Presets

Powerwall 3, SigenStor (5 kW + 8/16 kWh, 8 kW + 24 kWh), generic 5/10 kWh, battery-only (IOG
arbitrage), solar-only. **Figures are approximate starting points** - check the datasheet/quote
and edit. UK variants can differ from other markets.

## How a day works

1. **Cheap window / dispatch slots / Power Up** - charge from grid to the level tomorrow needs
   (or full if exporting later pays).
2. **Daytime** - solar runs the house, surplus fills the battery, then exports.
3. **Evening** - battery covers the house. If there's spare charge and
   `export price x round-trip > cheap rate + margin`, the surplus is exported - **only inside the late-evening export
   window** (default 18:00-23:30), before the next recharge. Never overnight while the grid is cheap: on a flat export
   price the optimiser would otherwise charge, sell, recharge and sell again, roughly doubling battery cycles for little
   gain. A paid Power Down event may override the window (switch that off to keep strictly to it).
4. **Power Down** - charge beforehand, then discharge flat out into house + grid.

## Services

`energy_twin.add_event`, `clear_events`, `run_sweep` (lists of `battery_kwh`, `pv_kwp`,
`inverter_kw`; `strategy: rules|optimised`), `recalculate`, `reset_twin`. `scenario` (name) targets one
scenario; events always apply to all.

## Honest limitations - please read

* **Not verified against a live Octopus install.** Rate / dispatch / event parsing is tolerant and
  written from the integration's documented attributes; if a count in the *Diagnostics* sensor is 0,
  tell me the entity's attributes and I'll adapt the parser.
* **Power Up / Power Down terms are placeholders** (Power Up import 0 p, Power Down export 30 p).
  Set the real rates in *Tariff*, or per event via `add_event`.
* **Solar forecast:** Forecast.Solar keys each hourly value by the *end* of its period (checked against the
  library source), and the twin maps it that way. It uses the integration's hourly forecast; if that isn't available
  it re-shapes a seasonal curve to the device's own sensors (energy remaining today, tomorrow's total, peak
  times). Diagnostics shows the source used and compares the plan with the device's sensors. The forecast has no
  uncertainty band, so *Forecast confidence %* (e.g. 85) lets you plan more cautiously. The annual what-if
  can't use a forecast (it only covers two days) and uses the seasonal curve.

## Develop

```bash
pip install numpy pytest pytest-homeassistant-custom-component
pytest tests/sim          # pure-Python engine (fast)
pytest tests/ha           # real Home Assistant: config flow, setup, entities, services
python scripts/gen_translations.py   # after changing option labels
```
