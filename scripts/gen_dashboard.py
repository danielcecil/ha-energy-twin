"""Make the dashboard for another scenario.

    python scripts/gen_dashboard.py big_battery "Big battery" > big_battery_dashboard.yaml

`big_battery` is the entity prefix (the scenario's device name, slugified: entities look like
sensor.big_battery_battery_state_of_charge). The Compare tab needs no changes.
"""
import sys
from pathlib import Path

if len(sys.argv) < 2:
    sys.exit(__doc__)
prefix = sys.argv[1]
title = sys.argv[2] if len(sys.argv) > 2 else prefix
src = (Path(__file__).resolve().parents[1] / "dashboard" / "energy_twin_dashboard.yaml").read_text()
out = src.replace("home_energy_twin_", f"{prefix}_").replace("title: Energy Twin\n", f"title: {title}\n", 1)
# the generic Compare tab uses integration_entities(), which is prefix-independent - leave it untouched
sys.stdout.write(out)
