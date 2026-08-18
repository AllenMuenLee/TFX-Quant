"""Application layer: ports (interfaces), event coordination, safety gate, settings.

Depends only on `tfx_quant.domain`. Never imports `infrastructure`, `persistence`, or
`desktop` — enforced by import-linter.
"""
