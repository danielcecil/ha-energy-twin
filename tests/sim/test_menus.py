import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "custom_components" / "energy_twin"
_spec = importlib.util.spec_from_file_location("menus", ROOT / "menus.py")
menus = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(menus)


def test_every_menu_item_has_a_label():
    used = set(menus.TOP) | {i for ids in menus.SECTIONS.values() for i in ids} | set(menus.VEHICLE_MENU)
    assert used <= set(menus.LABELS), used - set(menus.LABELS)
    assert all(v.strip() for v in menus.LABELS.values())
    assert set(menus.SECTIONS) <= set(menus.LABELS) and set(menus.SECTIONS) | {"init"} <= set(menus.SECTION_TEXT)


def test_translations_match_the_in_code_labels():
    en = json.loads((ROOT / "translations" / "en.json").read_text())["options"]["step"]
    assert en["init"]["menu_options"] == menus.options(menus.TOP)
    for sec, ids in menus.SECTIONS.items():
        assert en[sec]["menu_options"] == menus.options(ids), sec
    assert en["vehicles"]["menu_options"] == menus.options(menus.VEHICLE_MENU)
    # every settings page a menu can open has a title
    pages = {i for ids in menus.SECTIONS.values() for i in ids} - {"init"}
    assert all(en.get(p, {}).get("title") or p in menus.SECTIONS for p in pages), [p for p in pages if p not in en]
