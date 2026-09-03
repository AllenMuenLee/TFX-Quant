"""Ports (interfaces) that infrastructure implementations must satisfy.

Defined as `typing.Protocol`s rather than ABCs so mocks and real adapters are
structurally checked without needing to inherit from anything.
"""
