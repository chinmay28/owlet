#!/usr/bin/env python
"""Poll the Owlet Smart Sock and publish readings to a HomeAPI server.

This module implements the long running process behind the
``owlet-homeapi`` systemd service. It logs into the Owlet cloud
service, polls every device at a configurable interval and stores the
latest reading in a `HomeAPI <https://github.com/chinmay28/HomeAPI>`_
instance running on the local area network.

Configuration is read from environment variables, which the systemd
unit sources from ``/etc/owlet-homeapi/owlet-homeapi.env``.
"""

import argparse
import logging
import os
import signal
import sys
import time

from .homeapiclient import DEFAULT_HTTP_TIMEOUT
from .homeapiclient import HomeAPIClient
from .homeapiconfig import ConfigurationError
from .homeapiconfig import get_float
from .homeapiconfig import get_list
from .homeapiconfig import get_optional
from .homeapiconfig import sanitize_key
from .homeapiconfig import setup_logging
from .owletapi import OwletAPI
from .owletexceptions import OwletException
from .owletexceptions import OwletTemporaryCommunicationException
from .owletexceptions import OwletPermanentCommunicationException

LOGGER = logging.getLogger('owlet_homeapi')

DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_REACTIVATE_INTERVAL = 10.0
DEFAULT_HOMEAPI_URL = 'http://localhost:9999'
DEFAULT_CATEGORY = 'owlet'
DEFAULT_KEY_PREFIX = 'owlet'
DEFAULT_LOGIN_BACKOFF = 30.0
MAX_LOGIN_BACKOFF = 300.0

# "latest" keeps one continuously updated entry per device, "history"
# writes one entry per reading and "both" does the two of them.
PUBLISH_MODES = ('latest', 'history', 'both')

# Attributes that are lifted out of the raw attribute dump into a
# stable, easy to consume "vitals" object.
VITALS = {
    'HEART_RATE': 'heart_rate',
    'OXYGEN_LEVEL': 'oxygen_level',
    'MOVEMENT': 'movement',
    'BATT_LEVEL': 'battery_level',
    'CHARGE_STATUS': 'charge_status',
    'SOCK_CONNECTION': 'sock_connection',
    'SOCK_OFF': 'sock_off',
    'BASE_STATION_ON': 'base_station_on',
}

# Alert attributes, published as a separate object.
ALERTS = {
    'CRIT_BATT_ALRT': 'critical_battery',
    'CRIT_OX_ALRT': 'critical_oxygen',
    'HIGH_HR_ALRT': 'high_heart_rate',
    'LOW_BATT_ALRT': 'low_battery',
    'LOW_HR_ALRT': 'low_heart_rate',
    'LOW_OX_ALRT': 'low_oxygen',
    'LOW_INTEG_READ': 'low_integrity_read',
    'SOCK_DISCON_ALRT': 'sock_disconnected',
}


class Config():
    """Runtime configuration, read from the process environment."""

    # A configuration object is a bag of values by nature.
    # pylint: disable=R0902,R0903
    def __init__(self, env=None):
        """Build the configuration from ``env`` (defaults to os.environ)."""
        if env is None:
            env = os.environ

        self.email = env.get('OWLET_EMAIL', '').strip()
        self.password = env.get('OWLET_PASSWORD', '')
        self.device = get_optional(env, 'OWLET_DEVICE')
        self.poll_interval = get_float(
            env, 'OWLET_POLL_INTERVAL', DEFAULT_POLL_INTERVAL)
        self.reactivate_interval = get_float(
            env, 'OWLET_REACTIVATE_INTERVAL', DEFAULT_REACTIVATE_INTERVAL)
        self.attributes = get_list(env, 'OWLET_ATTRIBUTES')
        self.homeapi_url = env.get(
            'HOMEAPI_URL', DEFAULT_HOMEAPI_URL).strip().rstrip('/')
        self.category = env.get('HOMEAPI_CATEGORY', DEFAULT_CATEGORY).strip()
        self.key_prefix = env.get(
            'HOMEAPI_KEY_PREFIX', DEFAULT_KEY_PREFIX).strip()
        self.http_timeout = get_float(
            env, 'HOMEAPI_TIMEOUT', DEFAULT_HTTP_TIMEOUT)
        self.mode = env.get('OWLET_PUBLISH_MODE', 'history').strip().lower()
        self.log_level = env.get('OWLET_LOG_LEVEL', 'info').strip().upper()

    def validate(self):
        """Check the configuration, raise ConfigurationError if unusable."""
        if not self.email or not self.password:
            raise ConfigurationError(
                'OWLET_EMAIL and OWLET_PASSWORD must be set, see '
                '/etc/owlet-homeapi/owlet-homeapi.env')

        if not self.homeapi_url.startswith(('http://', 'https://')):
            raise ConfigurationError(
                'HOMEAPI_URL must start with http:// or https://')

        if self.mode not in PUBLISH_MODES:
            raise ConfigurationError(
                'OWLET_PUBLISH_MODE must be one of %s'
                % ', '.join(sorted(PUBLISH_MODES)))

        if self.poll_interval <= 0:
            raise ConfigurationError(
                'OWLET_POLL_INTERVAL must be greater than zero')


def _iso_utc(timestamp):
    """Return an ISO-8601 UTC timestamp with millisecond precision."""
    base = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(timestamp))
    return '%s.%03dZ' % (base, int(round(timestamp % 1, 3) * 1000))


def build_payload(device, attributes=None, timestamp=None):
    """Build the JSON payload published for a single Owlet device."""
    if timestamp is None:
        timestamp = time.time()

    properties = device.get_properties()

    if attributes:
        selected = [name for name in attributes if name in properties]
    else:
        selected = list(properties.keys())

    raw = {name: properties[name].value for name in selected}

    payload = {
        'dsn': device.dsn,
        'connection_status': device.connection_status,
        'timestamp': timestamp,
        'collected_at': _iso_utc(timestamp),
        'vitals': {
            label: properties[name].value
            for name, label in VITALS.items() if name in properties
        },
        'alerts': {
            label: properties[name].value
            for name, label in ALERTS.items() if name in properties
        },
        'attributes': raw,
    }

    if 'BABY_NAME' in properties:
        payload['baby_name'] = properties['BABY_NAME'].value

    return payload


class Publisher():
    """Polls the Owlet cloud service and publishes to HomeAPI."""

    def __init__(self, config, api=None, client=None):
        """Initialize with a config and optional collaborators."""
        self.config = config
        self.api = api if api is not None else OwletAPI()
        self.client = client if client is not None else HomeAPIClient(
            config.homeapi_url, config.category, config.http_timeout)
        self.running = True
        self._last_reactivate = 0.0

    def stop(self, *_args):
        """Ask the publisher to leave its polling loop."""
        LOGGER.info('Shutdown requested, stopping after current cycle')
        self.running = False

    def login(self):
        """Log into the Owlet cloud service."""
        self.api.set_email(self.config.email)
        self.api.set_password(self.config.password)
        self.api.login()
        LOGGER.info('Logged into the Owlet cloud service as %s',
                    self.config.email)

    def devices(self):
        """Return the devices to publish, honouring the DSN filter."""
        devices = self.api.get_devices()

        if self.config.device is None:
            return devices

        return [device for device in devices
                if device.dsn == self.config.device]

    def latest_key(self, device):
        """Return the HomeAPI key of the always current entry."""
        return sanitize_key('%s_%s' % (self.config.key_prefix, device.dsn))

    def history_key(self, device, timestamp):
        """Return the HomeAPI key of one historic reading."""
        return sanitize_key('%s_%s_%d' % (self.config.key_prefix,
                                          device.dsn, int(timestamp)))

    def publish(self, device, timestamp=None):
        """Publish the reading of one device, True on success."""
        if timestamp is None:
            timestamp = time.time()

        payload = build_payload(device, self.config.attributes, timestamp)
        results = []

        if self.config.mode in ('latest', 'both'):
            results.append(
                self.client.upsert(self.latest_key(device), payload))

        if self.config.mode in ('history', 'both'):
            results.append(
                self.client.create(self.history_key(device, timestamp),
                                   payload))

        return all(results)

    def poll_once(self):
        """Run a single poll and publish cycle over all devices."""
        published = 0

        for device in self.devices():
            try:
                device.update()
            except OwletTemporaryCommunicationException as error:
                LOGGER.warning('Could not update %s: %s', device.dsn, error)
                continue

            if self._should_reactivate():
                try:
                    device.reactivate()
                except OwletException as error:
                    LOGGER.warning('Could not reactivate %s: %s',
                                   device.dsn, error)

            if self.publish(device):
                published = published + 1

        if self._should_reactivate():
            self._last_reactivate = time.time()

        return published

    def _should_reactivate(self):
        """Return True if the data stream should be re-armed this cycle."""
        return (time.time() - self._last_reactivate >=
                self.config.reactivate_interval)

    def run(self, max_cycles=None):
        """Poll and publish until stopped, return the number of cycles run."""
        self.login()

        if not self.client.health():
            LOGGER.warning(
                'HomeAPI at %s did not answer its health check, '
                'continuing anyway', self.config.homeapi_url)

        LOGGER.info('Publishing to %s every %.1fs (category "%s", mode "%s")',
                    self.config.homeapi_url, self.config.poll_interval,
                    self.config.category, self.config.mode)

        cycles = 0

        while self.running and (max_cycles is None or cycles < max_cycles):
            start = time.time()

            try:
                self.poll_once()
            except OwletTemporaryCommunicationException as error:
                LOGGER.warning('Poll cycle failed: %s', error)

            cycles = cycles + 1

            if not self.running or cycles == max_cycles:
                break

            wait = self.config.poll_interval - (time.time() - start)
            if wait > 0:
                time.sleep(wait)

        return cycles


def _interruptible_sleep(seconds, publisher):
    """Sleep in short slices so that a stop request is honoured quickly."""
    deadline = time.time() + seconds

    while publisher.running and time.time() < deadline:
        time.sleep(min(1.0, deadline - time.time()))


def parse_args(argv=None):
    """Parse the command line arguments of the service."""
    parser = argparse.ArgumentParser(
        description='Publish Owlet Smart Sock readings to a HomeAPI server')
    parser.add_argument('--once', dest='once', action='store_true',
                        help='Run a single poll cycle and exit')
    parser.add_argument('--check-config', dest='check_config',
                        action='store_true',
                        help='Validate the configuration and exit')
    return parser.parse_args(argv)


def main(argv=None):
    """Entry point of the owlet-homeapi service."""
    args = parse_args(argv)

    try:
        config = Config()
        config.validate()
    except ConfigurationError as error:
        setup_logging('INFO')
        LOGGER.error('%s', error)
        return 2

    setup_logging(config.log_level)

    if args.check_config:
        LOGGER.info('Configuration is valid, publishing to %s every %.1fs',
                    config.homeapi_url, config.poll_interval)
        return 0

    publisher = Publisher(config)
    signal.signal(signal.SIGTERM, publisher.stop)
    signal.signal(signal.SIGINT, publisher.stop)

    backoff = DEFAULT_LOGIN_BACKOFF

    while publisher.running:
        try:
            publisher.run(max_cycles=1 if args.once else None)
            return 0
        except OwletPermanentCommunicationException as error:
            LOGGER.error('Login failed, check OWLET_EMAIL and '
                         'OWLET_PASSWORD: %s', error)
            return 1
        except OwletException as error:
            if args.once:
                LOGGER.error('Owlet service unavailable: %s', error)
                return 1

            LOGGER.warning('Owlet service unavailable (%s), retrying in %ds',
                           error, backoff)
            _interruptible_sleep(backoff, publisher)
            backoff = min(backoff * 2, MAX_LOGIN_BACKOFF)

    return 0


def init():
    """Mandatory init function."""
    if __name__ == "__main__":
        sys.exit(main())


init()
