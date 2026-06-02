"""C1 characterization tests for ArbCore compute/execute boundary."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
STRATEGIES = ROOT / "strategies"
HELPERS = ROOT / "helpers"
FACTORS = ROOT / "factors"
CORE = ROOT / "core"
for p in (ROOT, STRATEGIES, HELPERS, FACTORS, CORE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


def test_arbcore_exposes_c1_boundary_methods():
    from strategies.ArbCore import ArbCore

    for name in ("sync_state", "compute_decision", "execute_decision", "persist_state"):
        assert hasattr(ArbCore, name), f"ArbCore missing {name}"
        assert callable(getattr(ArbCore, name))
