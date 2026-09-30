from __future__ import annotations

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_DIRS = [
    REPO_ROOT / "dashboard",
    REPO_ROOT / "dashboard-plugin" / "tool-slimmer" / "dashboard",
]


def test_dashboard_manifests_reference_existing_assets() -> None:
    for dashboard_dir in DASHBOARD_DIRS:
        manifest_path = dashboard_dir / "manifest.json"
        assert manifest_path.exists(), f"{manifest_path} must exist"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["name"] == "tool-slimmer"
        assert manifest["entry"] == "dist/index.js"
        assert manifest["css"] == "dist/style.css"
        assert (dashboard_dir / manifest["entry"]).exists()
        assert (dashboard_dir / manifest["css"]).exists()
        assert (dashboard_dir / manifest["api"]).exists()


def test_dashboard_assets_are_non_empty() -> None:
    for dashboard_dir in DASHBOARD_DIRS:
        for relative in ("dist/index.js", "dist/style.css"):
            path = dashboard_dir / relative
            assert path.exists(), f"{path} must exist"
            assert path.stat().st_size > 0, f"{path} must not be empty"


def test_pyproject_includes_dashboard_assets() -> None:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for expected in (
        '"/dashboard"',
        '"/dashboard-plugin"',
        '"/dashboard/dist/index.js"',
        '"/dashboard/dist/style.css"',
        '"/dashboard-plugin/tool-slimmer/dashboard/dist/index.js"',
        '"/dashboard-plugin/tool-slimmer/dashboard/dist/style.css"',
    ):
        assert expected in text


def test_dashboard_copies_stay_in_sync() -> None:
    # The installer ships dashboard-plugin/; git installs load the repo root copy.
    root, plugin = DASHBOARD_DIRS
    for relative in ("manifest.json", "plugin_api.py", "dist/index.js", "dist/style.css"):
        assert (root / relative).read_bytes() == (plugin / relative).read_bytes(), f"{relative} differs between copies"
    for relative in ("plugin.yaml", "__init__.py"):
        root_file = REPO_ROOT / relative
        plugin_file = REPO_ROOT / "dashboard-plugin" / "tool-slimmer" / relative
        assert root_file.read_bytes() == plugin_file.read_bytes(), f"{relative} differs between copies"
