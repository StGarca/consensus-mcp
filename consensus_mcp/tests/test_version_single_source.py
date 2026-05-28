"""Version single-sourcing regression (2026-05-28).

Origin: `consensus_mcp/__init__.py.__version__` was frozen at `"2.0.0"` (the
initial-commit internal name) while `pyproject.toml` tracked the public `1.33.x`
line — three different version strings across the package (the editable dist
metadata was a stale fourth). The reconciliation makes `__init__.__version__`
the ONE source and has `pyproject` derive its version from it via setuptools'
`dynamic`/`attr`, so the two can no longer drift by construction.

These tests lock that wiring in place.
"""
import re
from pathlib import Path

try:
    import tomllib  # py3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

import consensus_mcp

_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def _pyproject() -> dict:
    assert _PYPROJECT.is_file(), f"pyproject.toml not found at {_PYPROJECT}"
    with _PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


def test_init_version_is_semver():
    """The single source must be a real version, not the stale '2.0.0' name."""
    assert re.match(r"^\d+\.\d+\.\d+", consensus_mcp.__version__), consensus_mcp.__version__


def test_pyproject_derives_version_from_init():
    """pyproject must NOT carry a static literal; it must derive the version from
    `consensus_mcp.__version__` so the two are structurally a single source."""
    data = _pyproject()
    project = data["project"]
    assert "version" not in project, (
        "pyproject [project] still has a static `version` literal — it must be "
        "`dynamic` so it derives from consensus_mcp.__version__"
    )
    assert "version" in project.get("dynamic", []), \
        "pyproject [project].dynamic must include 'version'"
    dyn = data["tool"]["setuptools"]["dynamic"]
    assert dyn.get("version") == {"attr": "consensus_mcp.__version__"}, dyn.get("version")
