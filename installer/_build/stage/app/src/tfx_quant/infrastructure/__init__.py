"""Infrastructure layer: real and mock implementations of application ports.

May depend on `domain` and `application`. Must never depend on `persistence` or
`desktop` — enforced by import-linter. Only `infrastructure.yuanta` may import vendor
COM/OCX types.
"""
