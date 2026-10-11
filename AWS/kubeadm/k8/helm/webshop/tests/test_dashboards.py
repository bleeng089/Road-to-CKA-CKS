"""The committed dashboard JSON is exactly what tools/build_dashboards.py builds."""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_builder():
    spec = importlib.util.spec_from_file_location("build_dashboards", ROOT / "tools" / "build_dashboards.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dashboards_are_in_sync_with_the_generator():
    builder = load_builder()
    expected = {
        "01-user-reliability.json": builder.user_reliability(),
        "02-service-health.json": builder.service_health(),
        "03-kubernetes-scaling.json": builder.kubernetes(),
        "04-database-queue.json": builder.data_and_queue(),
        "05-telemetry-health.json": builder.telemetry_health(),
        "06-order-investigation.json": builder.order_investigation(),
    }
    for name, board in expected.items():
        committed = json.loads((builder.OUT / name).read_text(encoding="utf-8"))
        assert committed == board, f"{name} is stale: run python tools/build_dashboards.py"


def test_dashboards_have_unique_uids_and_panel_ids():
    builder = load_builder()
    uids = set()
    for path in sorted(builder.OUT.glob("*.json")):
        board = json.loads(path.read_text(encoding="utf-8"))
        assert board["uid"] not in uids
        uids.add(board["uid"])
        ids = [p["id"] for p in board["panels"]]
        assert len(ids) == len(set(ids)), f"duplicate panel ids in {path.name}"
