#!/usr/bin/env python

import json
import time

import pytest
import responses
from unittest.mock import Mock

from owlet_api.homeapi_publisher import ALERTS
from owlet_api.homeapi_publisher import _interruptible_sleep
from owlet_api.homeapi_publisher import Config
from owlet_api.homeapi_publisher import ConfigurationError
from owlet_api.homeapi_publisher import HomeAPIClient
from owlet_api.homeapi_publisher import Publisher
from owlet_api.homeapi_publisher import VITALS
from owlet_api.homeapi_publisher import build_payload
from owlet_api.homeapi_publisher import main
from owlet_api.homeapi_publisher import sanitize_key
from owlet_api.owletexceptions import OwletPermanentCommunicationException
from owlet_api.owletexceptions import OwletTemporaryCommunicationException

BASE_ENV = {
    'OWLET_EMAIL': 'test@example.org',
    'OWLET_PASSWORD': 'secret',
    'HOMEAPI_URL': 'http://homeapi.local:9999',
}


class FakeProperty():
    def __init__(self, name, value):
        self.name = name
        self.value = value


class FakeDevice():
    def __init__(self, dsn='AC000W00TEST', properties=None):
        self.dsn = dsn
        self.connection_status = 'Online'
        self.properties = properties if properties is not None else {
            'HEART_RATE': FakeProperty('HEART_RATE', 136),
            'OXYGEN_LEVEL': FakeProperty('OXYGEN_LEVEL', 96),
            'BATT_LEVEL': FakeProperty('BATT_LEVEL', 81),
            'BABY_NAME': FakeProperty('BABY_NAME', 'Little Baby'),
            'LOW_OX_ALRT': FakeProperty('LOW_OX_ALRT', 0),
            'APP_ACTIVE': FakeProperty('APP_ACTIVE', 1),
        }
        self.update = Mock()
        self.reactivate = Mock()

    def get_properties(self):
        return self.properties


def make_config(**overrides):
    env = dict(BASE_ENV)
    env.update(overrides)
    config = Config(env)
    config.validate()
    return config


def test_config_defaults():
    config = make_config()

    assert config.email == 'test@example.org'
    assert config.password == 'secret'
    assert config.poll_interval == 2.0
    assert config.reactivate_interval == 10.0
    assert config.category == 'owlet'
    assert config.key_prefix == 'owlet'
    assert config.mode == 'latest'
    assert config.device is None
    assert config.attributes == []
    assert config.homeapi_url == 'http://homeapi.local:9999'


def test_config_overrides():
    config = make_config(
        OWLET_POLL_INTERVAL='0.5',
        OWLET_DEVICE='AC000W00TEST',
        OWLET_ATTRIBUTES='HEART_RATE, OXYGEN_LEVEL ,',
        OWLET_PUBLISH_MODE='HISTORY',
        HOMEAPI_URL='http://192.168.1.5:9999/',
    )

    assert config.poll_interval == 0.5
    assert config.device == 'AC000W00TEST'
    assert config.attributes == ['HEART_RATE', 'OXYGEN_LEVEL']
    assert config.mode == 'history'
    assert config.homeapi_url == 'http://192.168.1.5:9999'


def test_config_requires_credentials():
    with pytest.raises(ConfigurationError):
        Config({'OWLET_PASSWORD': 'secret'}).validate()

    with pytest.raises(ConfigurationError):
        Config({'OWLET_EMAIL': 'test@example.org'}).validate()


def test_config_rejects_invalid_values():
    with pytest.raises(ConfigurationError):
        make_config(HOMEAPI_URL='homeapi.local:9999')

    with pytest.raises(ConfigurationError):
        make_config(OWLET_PUBLISH_MODE='append')

    with pytest.raises(ConfigurationError):
        make_config(OWLET_POLL_INTERVAL='0')

    with pytest.raises(ConfigurationError):
        make_config(OWLET_POLL_INTERVAL='fast')


def test_sanitize_key():
    assert sanitize_key('owlet_AC000W00') == 'owlet_AC000W00'
    assert sanitize_key('owlet/AC 00') == 'owlet_AC_00'


def test_build_payload():
    device = FakeDevice()

    payload = build_payload(device, timestamp=1546552539.5)

    assert payload['dsn'] == 'AC000W00TEST'
    assert payload['connection_status'] == 'Online'
    assert payload['timestamp'] == 1546552539.5
    assert payload['collected_at'] == '2019-01-03T21:55:39.500Z'
    assert payload['baby_name'] == 'Little Baby'
    assert payload['vitals'][VITALS['HEART_RATE']] == 136
    assert payload['vitals'][VITALS['OXYGEN_LEVEL']] == 96
    assert payload['alerts'][ALERTS['LOW_OX_ALRT']] == 0
    assert 'sock_off' not in payload['vitals']
    assert payload['attributes']['APP_ACTIVE'] == 1


def test_build_payload_filters_attributes():
    device = FakeDevice()

    payload = build_payload(
        device, attributes=['HEART_RATE', 'DOES_NOT_EXIST'], timestamp=1.0)

    assert payload['attributes'] == {'HEART_RATE': 136}
    # Vitals are always published, independent of the attribute filter.
    assert payload['vitals'][VITALS['OXYGEN_LEVEL']] == 96


@responses.activate
def test_client_create():
    responses.add(responses.POST, 'http://homeapi.local:9999/api/entries',
                  json={'id': 1}, status=201)

    client = HomeAPIClient('http://homeapi.local:9999/', 'owlet')

    assert client.create('owlet_test', {'a': 1}) is True

    body = json.loads(responses.calls[0].request.body.decode('utf-8'))
    assert body == {'category': 'owlet', 'key': 'owlet_test',
                    'value': {'a': 1}}


@responses.activate
def test_client_create_conflict():
    responses.add(responses.POST, 'http://homeapi.local:9999/api/entries',
                  json={'error': 'exists'}, status=409)

    client = HomeAPIClient('http://homeapi.local:9999', 'owlet')

    assert client.create('owlet_test', {'a': 1}) is False


@responses.activate
def test_client_update():
    responses.add(responses.PUT,
                  'http://homeapi.local:9999/api/entries/owlet_test',
                  json={'id': 1}, status=200)

    client = HomeAPIClient('http://homeapi.local:9999', 'owlet')

    assert client.update('owlet_test', {'a': 1}) is True


@responses.activate
def test_client_update_missing_returns_none():
    responses.add(responses.PUT,
                  'http://homeapi.local:9999/api/entries/owlet_test',
                  json={'error': 'not found'}, status=404)

    client = HomeAPIClient('http://homeapi.local:9999', 'owlet')

    assert client.update('owlet_test', {'a': 1}) is None


@responses.activate
def test_client_upsert_creates_once_then_updates():
    responses.add(responses.PUT,
                  'http://homeapi.local:9999/api/entries/owlet_test',
                  json={'error': 'not found'}, status=404)
    responses.add(responses.POST, 'http://homeapi.local:9999/api/entries',
                  json={'id': 1}, status=201)

    client = HomeAPIClient('http://homeapi.local:9999', 'owlet')

    assert client.upsert('owlet_test', {'a': 1}) is True
    assert [call.request.method for call in responses.calls] == \
        ['PUT', 'POST']

    responses.reset()
    responses.add(responses.PUT,
                  'http://homeapi.local:9999/api/entries/owlet_test',
                  json={'id': 1}, status=200)

    assert client.upsert('owlet_test', {'a': 2}) is True
    assert [call.request.method for call in responses.calls] == ['PUT']


@responses.activate
def test_client_survives_connection_errors():
    client = HomeAPIClient('http://homeapi.local:9999', 'owlet')

    assert client.health() is False
    assert client.create('owlet_test', {'a': 1}) is False
    assert client.update('owlet_test', {'a': 1}) is False


@responses.activate
def test_client_health():
    responses.add(responses.GET, 'http://homeapi.local:9999/api/health',
                  json={'status': 'ok'}, status=200)

    client = HomeAPIClient('http://homeapi.local:9999', 'owlet')

    assert client.health() is True


def test_publisher_entry_keys():
    device = FakeDevice()

    publisher = Publisher(make_config(), api=Mock(), client=Mock())
    assert publisher.entry_key(device, 1546552539.5) == 'owlet_AC000W00TEST'

    publisher = Publisher(make_config(OWLET_PUBLISH_MODE='history',
                                      HOMEAPI_KEY_PREFIX='sock'),
                          api=Mock(), client=Mock())
    assert publisher.entry_key(device, 1546552539.5) == \
        'sock_AC000W00TEST_1546552539'


def test_publisher_publishes_latest_via_upsert():
    device = FakeDevice()
    client = Mock()
    client.upsert.return_value = True

    publisher = Publisher(make_config(), api=Mock(), client=client)

    assert publisher.publish(device, timestamp=1.0) is True
    client.upsert.assert_called_once()
    assert client.create.called is False

    key, payload = client.upsert.call_args[0]
    assert key == 'owlet_AC000W00TEST'
    assert payload['dsn'] == 'AC000W00TEST'


def test_publisher_publishes_history_via_create():
    device = FakeDevice()
    client = Mock()
    client.create.return_value = True

    publisher = Publisher(make_config(OWLET_PUBLISH_MODE='history'),
                          api=Mock(), client=client)

    assert publisher.publish(device, timestamp=1.0) is True
    client.create.assert_called_once()
    assert client.upsert.called is False


def test_publisher_filters_devices():
    wanted = FakeDevice('AC000W00WANTED')
    other = FakeDevice('AC000W00OTHER')

    api = Mock()
    api.get_devices.return_value = [wanted, other]

    publisher = Publisher(make_config(OWLET_DEVICE='AC000W00WANTED'),
                          api=api, client=Mock())

    assert publisher.devices() == [wanted]


def test_publisher_poll_once():
    device = FakeDevice()
    api = Mock()
    api.get_devices.return_value = [device]
    client = Mock()
    client.upsert.return_value = True

    publisher = Publisher(make_config(), api=api, client=client)

    assert publisher.poll_once() == 1
    device.update.assert_called_once()
    device.reactivate.assert_called_once()
    client.upsert.assert_called_once()

    # The stream is only re-armed once per reactivate interval.
    assert publisher.poll_once() == 1
    device.reactivate.assert_called_once()


def test_publisher_poll_once_skips_unreachable_device():
    device = FakeDevice()
    device.update.side_effect = OwletTemporaryCommunicationException('down')
    api = Mock()
    api.get_devices.return_value = [device]
    client = Mock()

    publisher = Publisher(make_config(), api=api, client=client)

    assert publisher.poll_once() == 0
    assert client.upsert.called is False


def test_publisher_poll_once_survives_failed_reactivate():
    device = FakeDevice()
    device.reactivate.side_effect = \
        OwletTemporaryCommunicationException('down')
    api = Mock()
    api.get_devices.return_value = [device]
    client = Mock()
    client.upsert.return_value = True

    publisher = Publisher(make_config(), api=api, client=client)

    assert publisher.poll_once() == 1


def test_publisher_run_stops_after_max_cycles():
    device = FakeDevice()
    api = Mock()
    api.get_devices.return_value = [device]
    client = Mock()
    client.health.return_value = True
    client.upsert.return_value = True

    publisher = Publisher(make_config(), api=api, client=client)

    assert publisher.run(max_cycles=2) == 2
    api.login.assert_called_once()
    assert client.upsert.call_count == 2


def test_publisher_run_stops_on_signal():
    device = FakeDevice()
    api = Mock()
    api.get_devices.return_value = [device]
    client = Mock()
    client.health.return_value = False
    client.upsert.return_value = True

    publisher = Publisher(make_config(OWLET_POLL_INTERVAL='0.01'),
                          api=api, client=client)
    device.update.side_effect = lambda: publisher.stop()

    assert publisher.run() == 1


def test_publisher_run_keeps_going_on_temporary_errors():
    api = Mock()
    api.get_devices.side_effect = \
        OwletTemporaryCommunicationException('cloud down')
    client = Mock()
    client.health.return_value = True

    publisher = Publisher(make_config(OWLET_POLL_INTERVAL='0.01'),
                          api=api, client=client)

    assert publisher.run(max_cycles=2) == 2


def test_main_without_configuration(monkeypatch):
    monkeypatch.delenv('OWLET_EMAIL', raising=False)
    monkeypatch.delenv('OWLET_PASSWORD', raising=False)

    assert main([]) == 2


def test_main_check_config(monkeypatch):
    for name, value in BASE_ENV.items():
        monkeypatch.setenv(name, value)

    assert main(['--check-config']) == 0


def test_main_reports_bad_credentials(monkeypatch):
    for name, value in BASE_ENV.items():
        monkeypatch.setenv(name, value)

    def explode(self, max_cycles=None):
        raise OwletPermanentCommunicationException('bad password')

    monkeypatch.setattr(Publisher, 'run', explode)

    assert main(['--once']) == 1


def test_main_once(monkeypatch):
    for name, value in BASE_ENV.items():
        monkeypatch.setenv(name, value)

    calls = []

    def record(self, max_cycles=None):
        calls.append(max_cycles)
        return 1

    monkeypatch.setattr(Publisher, 'run', record)

    assert main(['--once']) == 0
    assert calls == [1]


def test_interruptible_sleep_returns_when_stopped():
    publisher = Publisher(make_config(), api=Mock(), client=Mock())
    publisher.stop()

    start = time.time()
    _interruptible_sleep(30, publisher)

    assert time.time() - start < 1
