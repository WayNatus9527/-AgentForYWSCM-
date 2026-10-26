"""Authenticated M4 endpoints; every mutation shares the normal CSRF boundary."""
from typing import Annotated, Literal
from fastapi import Request, Query
from pydantic import Field
from . import m4_models as m, m4_schema as s, fulfillment, connectors, delivery, reconciliation
from .phase2 import Key
from .domain import require_role

Offset = Annotated[int, Query(ge=0)]
Limit = Annotated[int, Query(ge=1, le=100)]


class Retry(m.Strict):
    reason: Annotated[str, Field(min_length=2, max_length=1000)]


def install(app, engine, actor_for):
    @app.get('/api/v2/orders')
    def orders(request: Request, offset: Offset = 0, limit: Limit = 50):
        actor, _ = actor_for(request); return fulfillment.listing(engine, actor, offset, limit)

    @app.get('/api/v2/orders/{identity}')
    def order(identity: str, request: Request):
        actor, _ = actor_for(request)
        with engine.connect() as c: return fulfillment.view(c, actor, identity)

    @app.post('/api/v2/orders')
    def create_order(body: m.OrderCreate, request: Request, idempotency_key: Key):
        actor, _ = actor_for(request, True); return fulfillment.create(engine, actor, body.model_dump(), idempotency_key)

    # Register concrete models, so OpenAPI, API and worker validation agree.
    def route(action, model):
        def execute(identity: str, body, request: Request, idempotency_key: Key):
            actor, _ = actor_for(request, True)
            return fulfillment.act(engine, actor, identity, action, body.model_dump(), idempotency_key)
        execute.__annotations__['body'] = model
        execute.__name__ = 'order_' + action
        app.post('/api/v2/orders/{identity}/' + action)(execute)
    for action, model in fulfillment.CONTRACTS.items(): route(action, model)

    @app.get('/api/v2/connections')
    def connections(request: Request, offset: Offset = 0, limit: Limit = 50):
        actor, _ = actor_for(request); return connectors.listing(engine, actor, offset=offset, limit=limit)

    @app.post('/api/v2/connections')
    def create_connection(body: m.Connection, request: Request, idempotency_key: Key):
        actor, _ = actor_for(request, True); return connectors.create(engine, actor, body.model_dump(), idempotency_key)

    @app.post('/api/v2/connections/{identity}')
    def edit_connection(identity: str, body: m.ConnectionUpdate, request: Request, idempotency_key: Key):
        actor, _ = actor_for(request, True); return connectors.edit(engine, actor, identity, body.model_dump(), idempotency_key)

    @app.post('/api/v2/connections/{identity}/events')
    def receive(identity: str, body: m.Envelope, request: Request):
        actor, _ = actor_for(request, True); return connectors.receive(engine, actor, identity, body.model_dump())

    @app.post('/api/v2/connections/{identity}/source-facts')
    def source_fact(identity: str, body: m.Envelope, request: Request):
        actor, _ = actor_for(request, True); return connectors.source_fixture(engine, actor, identity, body.model_dump())

    @app.get('/api/v2/connector-records/{kind}')
    def records(kind: Literal['inbox', 'outbox', 'snapshots', 'reconciliations'], request: Request, offset: Offset = 0, limit: Limit = 50):
        actor, _ = actor_for(request); return connectors.listing(engine, actor, getattr(s, kind), offset, limit)

    @app.post('/api/v2/inbox/{identity}/{action}')
    def process(identity: str, action: Literal['process', 'verify'], request: Request):
        actor, _ = actor_for(request, True); return connectors.process(engine, actor, identity, verify=action == 'verify')

    @app.post('/api/v2/outbox/dispatch')
    def dispatch(request: Request):
        actor, _ = actor_for(request, True); return delivery.tick(engine, actor)

    @app.post('/api/v2/outbox/{identity}/retry')
    def retry(identity: str, body: Retry, request: Request, idempotency_key: Key):
        actor, _ = actor_for(request, True); return delivery.retry(engine, actor, identity, body.model_dump(), idempotency_key)

    @app.post('/api/v2/connections/{identity}/snapshots')
    def snapshot(identity: str, body: m.Snapshot, request: Request, idempotency_key: Key):
        actor, _ = actor_for(request, True); return reconciliation.snapshot(engine, actor, identity, body.model_dump(), idempotency_key)

    @app.post('/api/v2/snapshots/{identity}/compare')
    def compare(identity: str, request: Request, idempotency_key: Key):
        actor, _ = actor_for(request, True); return reconciliation.compare(engine, actor, identity, idempotency_key)
