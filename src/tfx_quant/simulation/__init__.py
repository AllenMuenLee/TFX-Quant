"""Offline-only deterministic simulator.

Nothing in this package is imported by normal desktop composition.  It is reachable
only from the explicit ``--mock`` launcher branch and from automated tests.
"""

from tfx_quant.simulation.clock import VirtualClock
from tfx_quant.simulation.replay import ReplayEvent, ReplayHarness, ReplayMetadata

__all__ = ["ReplayEvent", "ReplayHarness", "ReplayMetadata", "VirtualClock"]
