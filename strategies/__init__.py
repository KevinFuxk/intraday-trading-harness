"""Strategy registry. The production deployment registered two private strategies here;
the public repository ships only the demonstration strategy."""
from .ma_cross import MACross

REGISTRY = {MACross.name: MACross}


def load(name: str, **params):
    if name not in REGISTRY:
        raise SystemExit(f"✗ unknown strategy '{name}' — available: {', '.join(sorted(REGISTRY))}")
    return REGISTRY[name](**params)
