"""Request-cache reads must obey warehouse grants after a fresh login."""
import pytest
from app import accounts
from test_inventory import env, actor, counts
from test_m1_inventory import authorize, receipt


def login(client, username):
    client.delete('/api/v1/session')
    response = client.post('/api/v1/session', json={'username': username, 'password': 'test-password'})
    assert response.status_code == 200
    client.headers['X-CSRF-Token'] = response.json()['csrf']


def revoke_cn(engine):
    accounts.edit_user(engine, actor(engine), 'warehouse', {
        'name': '仓管小陈', 'role': 'warehouse', 'warehouse_ids': ['wh-us'],
        'active': True, 'expected_version': 0,
    }, 'revoke-warehouse-cn')


@pytest.mark.parametrize('operation', ['receipt', 'legacy_receipt', 'reserve', 'cancel'])
def test_replay_after_new_login_cannot_read_revoked_warehouse(env, operation):
    engine, client = env
    if operation in ('reserve', 'cancel'):
        grant = authorize(engine)['grant']
    login(client, 'warehouse')
    if operation in ('receipt', 'legacy_receipt'):
        body = receipt()
        path = '/api/v2/inventory/receipts'
        if operation == 'legacy_receipt':
            path = '/api/v1/receipts/confirm'
            body = {k: v for k, v in body.items() if k not in ('source_account', 'reference', 'occurred_at')}
    else:
        path = '/api/v2/commitments/grants/' + grant['id'] + '/reserve'
        body = dict(order_id='ORDER-REPLAY', line_id='1', action_id='RESERVE-REPLAY',
                    quantity=30, grant_version=1, expected_version=2)
        if operation == 'cancel':
            reserve = client.post(path, json=body, headers={'Idempotency-Key': 'setup-reserve-001'})
            assert reserve.status_code == 200
            reservation_id = reserve.json()['reservation']['id']
            path = '/api/v2/commitments/reservations/' + reservation_id + '/cancel'
            body = dict(action_id='CANCEL-REPLAY', quantity=10, reason='模拟取消',
                        grant_version=2, reservation_version=1, expected_version=3)
    headers = {'Idempotency-Key': 'original-successful-request'}
    first = client.post(path, json=body, headers=headers)
    assert first.status_code == 200, first.text
    replay = client.post(path, json=body, headers=headers)
    assert replay.status_code == 200 and replay.json()['replayed']
    revoke_cn(engine)
    login(client, 'warehouse')
    before = counts(engine)
    denied = client.post(path, json=body, headers=headers)
    assert denied.status_code == 404, denied.text
    assert denied.json()['error']['code'] == 'NOT_FOUND'
    assert 'balance' not in denied.json() and 'document_id' not in denied.json()
    assert counts(engine) == before
