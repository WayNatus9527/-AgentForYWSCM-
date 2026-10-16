import hashlib
import json
import uuid
from datetime import datetime, timezone
from sqlalchemy import select, insert, update
from . import db

FIELDS = ('g','q','d','r','t','h','b','offline','online','pending','withdrawing')
def uid(): return str(uuid.uuid4())
def now(): return datetime.now(timezone.utc).isoformat()

class BusinessError(Exception):
    def __init__(self, code, message, status=409):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)

def require_role(actor, *roles):
    if actor['role'] not in roles:
        raise BusinessError('FORBIDDEN', '当前岗位没有此操作权限', 403)

def get_warehouse(conn, actor, warehouse_id, writable=False):
    row = conn.execute(select(db.warehouses).where(db.warehouses.c.id == warehouse_id,
                       db.warehouses.c.tenant_id == actor['tenant_id'])).mappings().first()
    if not row or (actor['role'] != 'admin' and warehouse_id not in actor['warehouse_ids']):
        raise BusinessError('NOT_FOUND', '仓库不存在或未授权', 404)
    if writable and row['authority'] != 'local':
        raise BusinessError('READ_ONLY_WAREHOUSE', '该仓为人工参考数据，尚未接入收发回执，禁止自动改账')
    return row

def get_sku(conn, actor, sku_id):
    row = conn.execute(select(db.skus).where(db.skus.c.id == sku_id, db.skus.c.tenant_id == actor['tenant_id'])).mappings().first()
    if not row: raise BusinessError('NOT_FOUND', '商品不存在', 404)
    return row

def available(row):
    return row['g']-row['r']-row['t']-row['h']-row['b']

def unallocated(row):
    return max(0, available(row))-row['offline']-row['online']-row['pending']-row['withdrawing']

def stock_action(engine, actor, kind, payload, key):
    # Compatibility entry point; posting now lives in the inventory module.
    from .inventory import legacy_stock_action
    return legacy_stock_action(engine, actor, kind, payload, key)
