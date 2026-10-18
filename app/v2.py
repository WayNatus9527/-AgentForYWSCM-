"""M1 API contracts. Business scope always comes from the authenticated actor."""
from typing import Annotated
from fastapi import Request, Header, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select, func
from . import inventory, commitments, events, accounts
from .config import runtime_descriptor
from .clock import parse_timestamp
from .domain import get_warehouse, get_sku, require_role

Text = Annotated[str, Field(min_length=1, max_length=120)]
Qty = Annotated[int, Field(strict=True, ge=0, le=1_000_000)]
Positive = Annotated[int, Field(strict=True, ge=1, le=1_000_000)]
Key = Annotated[str, Header(alias='Idempotency-Key', min_length=8, max_length=160)]


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class Receipt(Strict):
    warehouse_id: Text
    sku_id: Text
    expected_version: Qty
    source: Text
    source_account: Text
    reference: Text
    g: Qty
    q: Qty = 0
    d: Qty = 0
    note: Annotated[str, Field(max_length=500)] = ''
    occurred_at: str | None = None

    @field_validator('occurred_at')
    @classmethod
    def occurrence(cls, value):
        if value is None:
            return None
        return parse_timestamp(value).isoformat()

    @model_validator(mode='after')
    def total(self):
        if self.g + self.q + self.d == 0:
            raise ValueError('实收数量合计须大于零')
        return self


class Sale(Strict):
    warehouse_id: Text
    sku_id: Text
    expected_version: Qty
    source: Text
    source_account: Text
    reference: Text
    quantity: Positive
    note: Annotated[str, Field(max_length=500)] = ''
    occurred_at: str | None = None

    @field_validator('occurred_at')
    @classmethod
    def occurrence(cls, value):
        if value is None:
            return None
        return parse_timestamp(value).isoformat()


class Grant(Strict):
    warehouse_id: Text
    sku_id: Text
    source_account: Text
    reference: Text
    action_id: Text
    quantity: Positive
    expected_version: Qty
    restore_on_cancel: Annotated[bool, Field(strict=True)]


class Reserve(Strict):
    order_id: Text
    line_id: Text
    action_id: Text
    quantity: Positive
    grant_version: Qty
    expected_version: Qty


class Withdraw(Strict):
    action_id: Text
    quantity: Positive
    reason: Text
    grant_version: Qty
    expected_version: Qty


class Cancel(Withdraw):
    reservation_version: Qty


def install(app, engine, actor_for):
    @app.get('/api/v2/runtime')
    def runtime():
        return {**runtime_descriptor(engine), 'version': '0.7.0', 'stage': 'M3',
                'simulated_data_only': True, 'external_connectors': 'not_connected'}

    @app.post('/api/v2/inventory/receipts')
    def receipt(body: Receipt, request: Request, idempotency_key: Key):
        actor, _ = actor_for(request, True)
        return inventory.receive(engine, actor, body.model_dump(), idempotency_key)

    @app.post('/api/v2/inventory/sales')
    def sale(body: Sale, request: Request, idempotency_key: Key):
        actor, _ = actor_for(request, True)
        return inventory.sell(engine, actor, body.model_dump(), idempotency_key)

    @app.get('/api/v2/events')
    def event_list(request: Request, warehouse_id: str | None = None, sku_id: str | None = None,
                   object_id: str | None = None, limit: int = Query(50, ge=1, le=100),
                   offset: int = Query(0, ge=0)):
        actor, _ = actor_for(request)
        require_role(actor, 'admin', 'warehouse')
        table = events.business_events
        with engine.connect() as c:
            accounts.assert_current(c, actor)
            conditions = [table.c.tenant_id == actor['tenant_id']]
            if actor['role'] != 'admin':
                conditions.append(table.c.warehouse_id.in_(actor['warehouse_ids']))
            if warehouse_id:
                get_warehouse(c, actor, warehouse_id)
                conditions.append(table.c.warehouse_id == warehouse_id)
            if sku_id:
                get_sku(c, actor, sku_id)
                conditions.append(table.c.sku_id == sku_id)
            if object_id:
                conditions.append(table.c.object_id == object_id)
            total = c.scalar(select(func.count()).select_from(table).where(*conditions))
            items = [dict(r) for r in c.execute(select(table).where(*conditions).order_by(
                table.c.registered_at.desc(), table.c.id).offset(offset).limit(limit)).mappings()]
        return dict(items=items, total=total, offset=offset, limit=limit,
                    note='实际发生时间缺失时保持未知；事件登记不表示已具备全流程瓶颈诊断。')

    @app.get('/api/v2/commitments/grants')
    def grants(request: Request, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
        actor, _ = actor_for(request)
        return commitments.list_records(engine, actor, 'grants', offset, limit)

    @app.get('/api/v2/commitments/reservations')
    def reservations(request: Request, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
        actor, _ = actor_for(request)
        return commitments.list_records(engine, actor, 'reservations', offset, limit)

    @app.post('/api/v2/commitments/grants')
    def grant(body: Grant, request: Request, idempotency_key: Key):
        actor, _ = actor_for(request, True)
        return commitments.authorize(engine, actor, body.model_dump(), idempotency_key)

    @app.post('/api/v2/commitments/grants/{grant_id}/reserve')
    def reserve(grant_id: str, body: Reserve, request: Request, idempotency_key: Key):
        actor, _ = actor_for(request, True)
        return commitments.reserve(engine, actor, grant_id, body.model_dump(), idempotency_key)

    @app.post('/api/v2/commitments/grants/{grant_id}/withdraw')
    def withdraw(grant_id: str, body: Withdraw, request: Request, idempotency_key: Key):
        actor, _ = actor_for(request, True)
        return commitments.withdraw(engine, actor, grant_id, body.model_dump(), idempotency_key)

    @app.post('/api/v2/commitments/grants/{grant_id}/confirm-withdrawal')
    def confirm_withdrawal(grant_id: str, body: Withdraw, request: Request, idempotency_key: Key):
        actor, _ = actor_for(request, True)
        return commitments.withdraw(engine, actor, grant_id, body.model_dump(), idempotency_key, confirm=True)

    @app.post('/api/v2/commitments/reservations/{reservation_id}/cancel')
    def cancel(reservation_id: str, body: Cancel, request: Request, idempotency_key: Key):
        actor, _ = actor_for(request, True)
        return commitments.cancel(engine, actor, reservation_id, body.model_dump(), idempotency_key)
