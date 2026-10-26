"""Local simulated connector worker. Rechecks current account grants every cycle."""
import argparse
import time
from sqlalchemy import select
from . import db, delivery, connectors, m4_schema as s
from .migrations import migrate
from .domain import require_role


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--actor-id', required=True)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    engine = db.make_engine(); migrate(engine)
    try:
        while True:
            with engine.connect() as c:
                actor = dict(c.execute(select(db.users).where(db.users.c.id == args.actor_id)).mappings().one())
                require_role(actor, 'admin')
                identities = list(c.execute(select(s.inbox.c.id).where(s.inbox.c.tenant_id == actor['tenant_id'],
                    s.inbox.c.status == 'received').order_by(s.inbox.c.received_at).limit(50)).scalars())
            for identity in identities: connectors.process(engine, actor, identity)
            result = delivery.tick(engine, actor)
            if args.once:
                print(result['status']); break
            time.sleep(1 if result['status'] == 'idle' else .1)
    finally: engine.dispose()


if __name__ == '__main__': main()
