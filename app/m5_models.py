"""Strict API and worker contracts for procurement, supply and planning."""
from datetime import date
from typing import Annotated, Literal
from pydantic import Field, model_validator, field_validator, BeforeValidator
from .m4_models import Strict, Text
from .clock import parse_timestamp
from .m3_models import Details

Qty = Annotated[int, Field(strict=True, ge=0, le=1000000)]
Positive = Annotated[int, Field(strict=True, ge=1, le=1000000)]
Reason = Annotated[str, Field(min_length=2, max_length=1000)]
Timestamp = Annotated[str, BeforeValidator(lambda value: parse_timestamp(value).isoformat())]

class Line(Strict):
    sku_id: Text
    quantity: Positive

class Purchase(Strict):
    warehouse_id: Text
    code: Text
    supplier_id: Text
    replacement_return_id: Text | None = None
    note: Annotated[str, Field(max_length=500)] = ''
    ordered_at: Timestamp | None = None
    promised_at: Timestamp | None = None
    lines: Annotated[list[Line], Field(min_length=1, max_length=100)]
    @model_validator(mode='after')
    def unique(self):
        if len({x.sku_id for x in self.lines}) != len(self.lines): raise ValueError('商品行不可重复')
        return self

class Transfer(Strict):
    warehouse_id: Text
    destination_id: Text
    code: Text
    reason: Reason
    expected_at: Timestamp | None = None
    lines: Annotated[list[Line], Field(min_length=1, max_length=100)]
    @model_validator(mode='after')
    def unique(self):
        if len({x.sku_id for x in self.lines}) != len(self.lines): raise ValueError('商品行不可重复')
        return self

class Action(Strict):
    expected_version: Positive
    reference: Text
    reason: Reason

class Promise(Action):
    promised_at: Timestamp | None

class Batch(Action):
    supply_line_id: Text
    quantity: Positive
    carrier: Text | None = None
    transport_mode: Text | None = None
    tracking_number: Text | None = None
    planned_ship_at: Timestamp | None = None
    expected_at: Timestamp | None = None
    expected_balance_version: Qty | None = None

class BatchShip(Action):
    occurred_at: Timestamp | None = None
    expected_balance_version: Qty | None = None

class BatchReceive(Action):
    g: Qty = 0
    q: Qty = 0
    d: Qty = 0
    occurred_at: Timestamp | None = None
    expected_balance_version: Qty
    @model_validator(mode='after')
    def nonzero(self):
        if self.g+self.q+self.d == 0: raise ValueError('签收量必须大于零')
        return self

class BatchCancel(Action):
    expected_balance_version: Qty | None = None

class Claim(Action):
    quantity: Positive
    kind: Literal['shortage','refused','damaged']
    details: Details

class Review(Action):
    decision: Literal['approve','reject']

class Inspect(Strict):
    reference: Text
    quantity: Positive
    expected_balance_version: Qty
    details: Details

class Demand(Strict):
    warehouse_id: Text
    sku_id: Text
    day: str
    quantity: Qty
    quality: Literal['known','unknown','abnormal'] = 'known'
    source_reference: Text
    @field_validator('day')
    @classmethod
    def iso_day(cls, value):
        if date.fromisoformat(value).isoformat() != value: raise ValueError('日期须为 YYYY-MM-DD')
        return value

class Policy(Strict):
    warehouse_id: Text
    sku_id: Text
    supplier_id: Text
    window_days: Annotated[int, Field(strict=True, ge=1, le=90)]
    coverage_days: Annotated[int, Field(strict=True, ge=1, le=90)]
    lead_days: Annotated[int, Field(strict=True, ge=0, le=365)]
    safety: Qty = 0
    minimum: Positive = 1
    pack: Positive = 1
    max_daily: Positive = 1000000
    includes_open_orders: Annotated[bool, Field(strict=True)] = True
    active: Annotated[bool, Field(strict=True)] = True
    expected_version: Qty = 0

class Confirm(Strict):
    quantity: Qty
    reason: Reason
