from __future__ import annotations

from uuid import UUID

from tfx_quant.domain.order import ClientOrderId


class DeterministicIdGenerator:
    def __init__(self, seed: int = 0) -> None:
        self._next = seed + 1

    def new_client_order_id(self) -> ClientOrderId:
        value = self._next
        self._next += 1
        return ClientOrderId(UUID(int=value))
