"""Desktop layer: wxPython UI + the composition root.

The only layer allowed to depend on everything else (domain, application,
infrastructure, persistence) — it is the top of the dependency graph, not a
dependency of anything.
"""
