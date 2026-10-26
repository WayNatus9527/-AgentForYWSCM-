"""Strict contracts used equally by HTTP, workers and direct service callers."""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from .clock import parse_timestamp

Text = Annotated[str, Field(min_length=1, max_length=120)]
Qty = Annotated[int, Field(strict=True, ge=1, le=1_000_000)]
Version = Annotated[int, Field(strict=True, ge=0)]
Time = str | None


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

    @field_validator('occurred_at', 'planned_at', 'snapshot_at', check_fields=False)
    @classmethod
    def timestamp(cls, value):
        return parse_timestamp(value).isoformat() if value is not None else None


class Connection(Strict):
    source_account: Text
    name: Text
    warehouse_ids: Annotated[list[Text], Field(min_length=1, max_length=30)]
    member_ids: Annotated[list[Text], Field(max_length=100)] = []
    supports_query: Annotated[bool, Field(strict=True)] = True
    supports_idempotency: Annotated[bool, Field(strict=True)] = True


class ConnectionUpdate(Strict):
    expected_version: Version
    member_ids: Annotated[list[Text], Field(max_length=100)]
    active: Annotated[bool, Field(strict=True)]


class OrderLine(Strict):
    source_line_id: Text
    sku_id: Text
    quantity: Qty


class OrderCreate(Strict):
    connection_id: Text
    source_order_id: Text
    warehouse_ids: Annotated[list[Text], Field(min_length=1, max_length=30)]
    lines: Annotated[list[OrderLine], Field(min_length=1, max_length=100)]
    planned_at: Time = None
    occurred_at: Time = None

    @model_validator(mode='after')
    def unique(self):
        if len(set(self.warehouse_ids)) != len(self.warehouse_ids) or len({x.source_line_id for x in self.lines}) != len(self.lines):
            raise ValueError('仓库顺序和来源订单行编号不可重复')
        return self


class Action(Strict):
    expected_version: Version
    action_id: Text
    reason: Annotated[str, Field(min_length=2, max_length=1000)]


class Allocate(Action):
    line_id: Text
    quantity: Qty
    warehouse_id: Text | None = None
    grant_id: Text | None = None


class Cancel(Action):
    line_id: Text
    quantity: Qty
    batch_id: Text | None = None


class Ship(Action):
    batch_id: Text
    quantity: Qty
    reference: Text
    occurred_at: Time = None


class TaskAction(Action):
    batch_id: Text
    kind: Literal['pick', 'review']


class Supply(Action):
    line_id: Text
    source_type: Literal['purchase_line', 'transfer_line', 'inspection']
    source_id: Text
    quantity: Qty


class Release(Action):
    link_id: Text
    quantity: Qty


class Envelope(Strict):
    run_id: Text
    tenant_id: Text
    source_event_id: Text
    business_action_id: Text
    entity_id: Text
    source_version: Annotated[int, Field(strict=True, ge=1)] | None
    kind: Literal['order.created', 'order.cancelled', 'order.completed', 'shipment.delivered', 'stock.delta']
    occurred_at: Time = None
    warehouse_id: Text | None = None
    sku_id: Text | None = None
    payload: dict


class IncomingCancel(Strict):
    source_line_id: Text
    quantity: Qty
    reason: Annotated[str, Field(min_length=2, max_length=1000)]


class StockDelta(Strict):
    warehouse_id: Text
    sku_id: Text
    sequence: Qty
    g: Annotated[int, Field(strict=True, ge=-1_000_000, le=1_000_000)] = 0
    q: Annotated[int, Field(strict=True, ge=-1_000_000, le=1_000_000)] = 0
    d: Annotated[int, Field(strict=True, ge=-1_000_000, le=1_000_000)] = 0


class Snapshot(Strict):
    snapshot_id: Text
    warehouse_id: Text
    sku_id: Text
    covered_watermark: Version | None
    snapshot_at: Time = None
    quantities: dict[Literal['g', 'q', 'd'], Annotated[int, Field(strict=True, ge=0, le=1_000_000)]]

    @model_validator(mode='after')
    def complete(self):
        if set(self.quantities) != {'g', 'q', 'd'}:
            raise ValueError('快照须明确良品、待验和残次三种数量')
        return self
