"""Client-side revision capture and delayed-response handling, without networking."""
from copy import deepcopy
from unittest.mock import patch
import io
import json
from urllib.error import HTTPError

import pytest

from factory_hmi.client import GatewayClient, GatewayError


class Reply:
    def __init__(self, value): self.value = value
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self): return json.dumps(self.value).encode()


def snap(serial=1, revision=0, epoch='gateway-1'):
    return {'phase': 'OFF', 'coordination': {'epoch': epoch, 'revision': revision, 'snapshot_sequence': serial}}


def test_plc_client_sends_the_captured_page_context_without_refresh_or_retry():
    client = GatewayClient('http://127.0.0.1:1', client_id='window-b')
    seen = snap()['coordination'] | {'request_id': 'click-1'}
    client._remember_plc_snapshot(snap(3, 2))
    with patch('factory_hmi.client.request.urlopen', return_value=Reply({'ok': True, 'result': snap(4, 3)})) as send:
        client.plc_set_channel(1, False, 'b', context=seen)
    assert send.call_count == 1
    body = json.loads(send.call_args.args[0].data)
    assert body['expected_revision'] == 0  # Never silently replace what the page saw.
    assert body['request_id'] == 'click-1'
    assert send.call_args.args[0].get_header('X-rp1-client-id') == 'window-b'


def test_conflict_caches_fresh_snapshot_and_does_not_retry():
    client = GatewayClient('http://127.0.0.1:1')
    shared = snap(4, 3)
    detail = {'code': 'stale_revision', 'message': '其他窗口已改变状态', 'snapshot': shared}
    error = HTTPError('http://127.0.0.1:1', 409, '', {}, io.BytesIO(json.dumps({'detail': detail}).encode()))
    with patch('factory_hmi.client.request.urlopen', side_effect=error) as send:
        with pytest.raises(GatewayError) as rejected:
            client.plc_stop('b', context=snap()['coordination'])
    assert send.call_count == 1
    assert client._plc_snapshot == shared
    assert '其他窗口已改变状态' in str(rejected.value)
    assert 'snapshot_sequence' not in str(rejected.value)


def test_client_ignores_out_of_order_snapshots_and_retired_gateway_epochs():
    client = GatewayClient('http://127.0.0.1:1')
    client._remember_plc_snapshot(snap(10, 4))
    client._remember_plc_snapshot(snap(9, 3))
    assert client._plc_snapshot == snap(10, 4)
    client._remember_plc_snapshot(snap(1, 0, 'gateway-2'))
    client._remember_plc_snapshot(snap(11, 5))
    assert client._plc_snapshot == snap(1, 0, 'gateway-2')


def test_all_stop_without_any_observed_snapshot_sends_once_without_a_get():
    client = GatewayClient('http://127.0.0.1:1')
    with patch('factory_hmi.client.request.urlopen', return_value=Reply({'ok': True, 'result': snap()})) as send:
        client.plc_all_stop('b')
    assert send.call_count == 1
    assert send.call_args.args[0].method == 'POST'
    body = json.loads(send.call_args.args[0].data)
    assert body['request_id']
    assert body['expected_revision'] is None
