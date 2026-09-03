"""IdGenerator port — replaceable ID source so tests can use deterministic IDs."""

from __future__ import annotations

from typing import Protocol

from tfx_quant.domain.order import ClientOrderId


class IdGenerator(Protocol):
    def new_client_order_id(self) -> ClientOrderId: ...
