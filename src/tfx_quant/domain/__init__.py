"""Domain layer: immutable value types and pure business rules.

No dependency on any other layer and no third-party packages — stdlib only
(dataclasses, decimal, datetime, zoneinfo, enum, uuid). Enforced by the
"Domain has no dependency on other layers" import-linter contract.
"""
