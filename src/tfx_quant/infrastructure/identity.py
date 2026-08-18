"""Real IdGenerator implementation."""

from __future__ import annotations

from uuid import uuid4

from tfx_quant.domain.order import ClientOrderId


class UuidIdGenerator:
    """Implements `application.ports.identity.IdGenerator` using random UUID4s."""

    def new_client_order_id(self) -> ClientOrderId:
        return ClientOrderId(uuid4())
