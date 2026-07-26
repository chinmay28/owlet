#!/usr/bin/env python

import calendar
import json
import os
import time
from datetime import date

import pytest
import responses

# Bucketing happens in local time, so pin the timezone for the tests.
os.environ['TZ'] = 'UTC'
time.tzset()

from owlet_api.homeapiclient import HomeAPIClient          # noqa: E402
from owlet_api.homeapiclient import HomeAPIError           # noqa: E402
from owlet_api.homeapiclient import MAX_VALUE_CHARS        # noqa: E402
from owlet_api.homeapiclient import decode_value           # noqa: E402
from owlet_api.homeapi_summarizer import METRIC_BINS       # noqa: E402
from owlet_api.homeapi_summarizer import SummaryConfig     # noqa: E402
from owlet_api.homeapi_summarizer import Summarizer        # noqa: E402
from owlet_api.homeapi_summarizer import bin_label         # noqa: E402
from owlet_api.homeapi_summarizer import bin_spec          # noqa: E402
from owlet_api.homeapi_summarizer import fit_payload       # noqa: E402
from owlet_api.homeapi_summarizer import histogram         # noqa: E402
from owlet_api.homeapi_summarizer import is_alert_active   # noqa: E402
from owlet_api.homeapi_summarizer import iso_week_dates    # noqa: E402
from owlet_api.homeapi_summarizer import iso_week_key      # noqa: E402
from owlet_api.homeapi_summarizer import main              # noqa: E402
from owlet_api.homeapi_summarizer import merge_histograms  # noqa: E402
from owlet_api.homeapi_summarizer import merge_stats       # noqa: E402
from owlet_api.homeapi_summarizer import numeric_metrics   # noqa: E402
from owlet_api.homeapi_summarizer import parse_day         # noqa: E402
from owlet_api.homeapi_summarizer import percentile        # noqa: E402
from owlet_api.homeapi_summarizer import \
    percentile_from_histogram                              # noqa: E402
from owlet_api.homeapi_summarizer import stats_from_values  # noqa: E402
from owlet_api.homeapiconfig import ConfigurationError     # noqa: E402

MONDAY = date(2026, 7, 20)
TUESDAY = date(2026, 7, 21)
WEDNESDAY = date(2026, 7, 22)
DSN = 'AC000W00TEST'

BASE_ENV = {'HOMEAPI_URL': 'http://homeapi.local:9999'}


def epoch(day, hour=0, minute=0, second=0):
    return float(calendar.timegm(
        (day.year, day.month, day.day, hour, minute, second, 0, 0, 0)))


def reading(timestamp, heart_rate=130, oxygen=97, movement=1, battery=80,
            alerts=None):
    return {
        'dsn': DSN,
        'timestamp': timestamp,
        'vitals': {
            'heart_rate': heart_rate,
            'oxygen_level': oxygen,
            'movement': movement,
            'battery_level': battery,
        },
        'alerts': alerts if alerts is not None else {'low_oxygen': 0},
    }


class FakeHomeAPI():
    """In memory stand in for a HomeAPI category."""

    def __init__(self, store=None, category='owlet'):
        self.store = store if store is not None else {}
        self.category = category
        self.next_id = 1
        self.fail_page = None
        self.fail_delete = set()
        self.deleted = []

    def add(self, key, value):
        self.store[self.next_id] = {'id': self.next_id, 'key': key,
                                    'category': self.category, 'value': value}
        self.next_id = self.next_id + 1
        return self.next_id - 1

    def _find(self, key):
        for entry in self.store.values():
            if entry['key'] == key and entry['category'] == self.category:
                return entry
        return None

    # Client interface used by the summarizer.

    def iter_entries(self, per_page=200):
        if self.fail_page is not None:
            raise HomeAPIError('page %d failed' % self.fail_page)

        for entry in list(self.store.values()):
            if entry['category'] == self.category:
                # The list endpoint sends the value as a JSON string.
                yield {'id': entry['id'], 'key': entry['key'],
                       'category': entry['category'],
                       'value': json.dumps(entry['value'])}

    def get(self, key):
        entry = self._find(key)
        return entry['value'] if entry else None

    def create(self, key, value):
        self.add(key, value)
        return True

    def update(self, key, value):
        entry = self._find(key)
        if entry is None:
            return None
        entry['value'] = value
        return True

    def upsert(self, key, value):
        updated = self.update(key, value)
        return self.create(key, value) if updated is None else updated

    def delete(self, identifier):
        if identifier in self.fail_delete:
            return False
        self.store.pop(identifier, None)
        self.deleted.append(identifier)
        return True


def make_config(**overrides):
    env = dict(BASE_ENV)
    env.update(overrides)
    config = SummaryConfig(env)
    config.validate()
    return config


def build(store=None, **overrides):
    """Return a summarizer wired to two fake HomeAPI categories."""
    store = store if store is not None else {}
    raw = FakeHomeAPI(store, 'owlet')
    summaries = FakeHomeAPI(store, 'owlet_summary')
    summaries.next_id = 100000
    return Summarizer(make_config(**overrides), raw, summaries), raw, summaries


def fill_day(raw, day, samples=6, dsn=DSN, hours=(0, 6, 12, 18)):
    """Add readings spread over the given hours of a day."""
    added = []
    for hour in hours:
        for index in range(samples):
            timestamp = epoch(day, hour, index)
            added.append(raw.add(
                'owlet_%s_%d' % (dsn, int(timestamp)),
                reading(timestamp, heart_rate=120 + index,
                        oxygen=95 + index % 3)))
    return added


# --- statistics -------------------------------------------------------


def test_bin_label_clamps_to_the_bin_range():
    spec = METRIC_BINS['heart_rate']

    assert bin_label(0, spec) == '0'
    assert bin_label(132, spec) == '130'
    assert bin_label(135, spec) == '135'
    assert bin_label(-10, spec) == '0'
    assert bin_label(5000, spec) == '295'


def test_bin_spec_falls_back_for_unknown_metrics():
    assert bin_spec('heart_rate') == METRIC_BINS['heart_rate']
    assert bin_spec('something_else') == (0.0, 1000.0, 10.0)


def test_histogram_and_merge():
    spec = METRIC_BINS['heart_rate']

    first = histogram([131, 132, 137], spec)
    second = histogram([136], spec)

    assert first == {'130': 2, '135': 1}
    assert merge_histograms([first, second]) == {'130': 2, '135': 2}


def test_percentile():
    values = list(range(1, 101))

    assert percentile(values, 50) == 50.5
    assert percentile(values, 10) == 10.9
    assert percentile([], 50) is None
    assert percentile([7], 90) == 7


def test_percentile_from_histogram():
    spec = METRIC_BINS['heart_rate']
    hist = histogram([120, 121, 122, 123, 140, 141], spec)

    median = percentile_from_histogram(hist, spec, 50)

    assert 120 <= median <= 125
    assert percentile_from_histogram({}, spec, 50) is None


def test_stats_from_values():
    stats = stats_from_values([120, 130, 130, 140], METRIC_BINS['heart_rate'])

    assert stats['count'] == 4
    assert stats['min'] == 120
    assert stats['max'] == 140
    assert stats['range'] == 20
    assert stats['sum'] == 520
    assert stats['avg'] == 130
    assert stats['median'] == 130
    assert stats['p10'] == 123
    assert stats['p90'] == 137
    assert stats['histogram'] == {'120': 1, '130': 2, '140': 1}
    assert stats_from_values([], METRIC_BINS['heart_rate']) is None


def test_merge_stats_keeps_counts_exact():
    spec = METRIC_BINS['heart_rate']
    first = stats_from_values([120, 130], spec)
    second = stats_from_values([140, 150, 160], spec)

    merged = merge_stats([first, second], spec)

    assert merged['count'] == 5
    assert merged['sum'] == 700
    assert merged['min'] == 120
    assert merged['max'] == 160
    assert merged['range'] == 40
    assert merged['avg'] == 140
    # The median comes from the merged histogram, so it is accurate to
    # the bin width of 5 bpm.
    assert abs(merged['median'] - 140) <= 5
    assert merged['histogram'] == {'120': 1, '130': 1, '140': 1, '150': 1,
                                   '160': 1}
    assert merge_stats([None], spec) is None


def test_is_alert_active():
    assert is_alert_active(1) is True
    assert is_alert_active('1') is True
    assert is_alert_active(0) is False
    assert is_alert_active('0') is False
    assert is_alert_active(None) is False
    assert is_alert_active('None') is False
    assert is_alert_active('') is False


def test_iso_week_helpers():
    assert iso_week_key(MONDAY) == '2026-W30'
    assert iso_week_key(date(2026, 1, 1)) == '2026-W01'
    assert iso_week_dates(WEDNESDAY)[0] == MONDAY
    assert len(iso_week_dates(WEDNESDAY)) == 7
    assert parse_day('2026-07-20') == MONDAY

    with pytest.raises(ValueError):
        parse_day('20-07-2026x')


def test_numeric_metrics_ignores_unusable_values():
    metrics = numeric_metrics(
        {'vitals': {'heart_rate': '136', 'oxygen_level': None,
                    'movement': 1, 'other': 5}},
        ['heart_rate', 'oxygen_level', 'movement', 'missing'])

    assert metrics == {'heart_rate': 136.0, 'movement': 1.0}
    assert numeric_metrics({}, ['heart_rate']) == {}
    assert numeric_metrics('nonsense', ['heart_rate']) == {}


def test_decode_value_handles_strings_and_objects():
    assert decode_value('{"a": 1}') == {'a': 1}
    assert decode_value({'a': 1}) == {'a': 1}
    assert decode_value('not json') == 'not json'


# --- configuration ----------------------------------------------------


def test_summary_config_defaults():
    config = make_config()

    assert config.category == 'owlet'
    assert config.key_prefix == 'owlet'
    assert config.summary_category == 'owlet_summary'
    assert config.summary_key_prefix == 'owlet_summary'
    assert config.retention_days == 0
    assert config.metrics == ['heart_rate', 'oxygen_level', 'movement',
                              'battery_level']


def test_summary_config_rejects_bad_values():
    with pytest.raises(ConfigurationError):
        make_config(HOMEAPI_URL='homeapi.local')

    with pytest.raises(ConfigurationError):
        make_config(OWLET_RAW_RETENTION_DAYS='-1')

    with pytest.raises(ConfigurationError):
        make_config(OWLET_RAW_RETENTION_DAYS='soon')

    # Summaries must not be stored where the raw readings live.
    with pytest.raises(ConfigurationError):
        make_config(HOMEAPI_SUMMARY_CATEGORY='owlet',
                    HOMEAPI_SUMMARY_KEY_PREFIX='owlet')


# --- entry parsing ----------------------------------------------------


def test_parse_entry_only_matches_raw_readings():
    summarizer, _, _ = build()

    parsed = summarizer.parse_entry(
        {'id': 1, 'key': 'owlet_%s_1784505600' % DSN,
         'value': json.dumps(reading(1784505600.5))})

    assert parsed[0] == DSN
    assert parsed[1] == 1784505600.5

    # The always current entry, summaries and foreign keys are ignored.
    for key in ('owlet_%s' % DSN,
                'owlet_summary_%s_2026-07-20' % DSN,
                'owlet_summary_%s_2026-W30' % DSN,
                'watchlist_AAPL'):
        assert summarizer.parse_entry({'id': 2, 'key': key, 'value': '{}'}) \
            is None


def test_parse_entry_honours_the_device_filter():
    summarizer, _, _ = build(OWLET_DEVICE='AC000W00OTHER')

    assert summarizer.parse_entry(
        {'id': 1, 'key': 'owlet_%s_1784505600' % DSN, 'value': '{}'}) is None
    assert summarizer.parse_entry(
        {'id': 1, 'key': 'owlet_AC000W00OTHER_1784505600',
         'value': '{}'}) is not None


def test_parse_entry_falls_back_to_the_key_timestamp():
    summarizer, _, _ = build()

    parsed = summarizer.parse_entry(
        {'id': 1, 'key': 'owlet_%s_1784505600' % DSN,
         'value': json.dumps({'vitals': {'heart_rate': 130}})})

    assert parsed[1] == 1784505600.0


# --- collect ----------------------------------------------------------


def test_collect_skips_today_and_groups_by_day():
    summarizer, raw, _ = build()
    fill_day(raw, MONDAY, samples=2, hours=(0, 23))
    fill_day(raw, TUESDAY, samples=2, hours=(5,))
    fill_day(raw, WEDNESDAY, samples=2, hours=(5,))

    buckets = summarizer.collect(today=WEDNESDAY)

    assert sorted(buckets) == [(DSN, MONDAY), (DSN, TUESDAY)]
    assert buckets[(DSN, MONDAY)].samples == 4
    assert sorted(buckets[(DSN, MONDAY)].hours) == ['00', '23']


def test_collect_can_target_a_single_day():
    summarizer, raw, _ = build()
    fill_day(raw, MONDAY, samples=2, hours=(1,))
    fill_day(raw, TUESDAY, samples=2, hours=(1,))

    buckets = summarizer.collect(target_day=MONDAY, today=WEDNESDAY)

    assert list(buckets) == [(DSN, MONDAY)]


# --- summaries --------------------------------------------------------


def test_day_summary_shape():
    summarizer, raw, _ = build()
    fill_day(raw, MONDAY, samples=4, hours=(0, 12))

    bucket = summarizer.collect(today=TUESDAY)[(DSN, MONDAY)]
    payload = summarizer.build_day_summary(DSN, MONDAY, bucket)

    assert payload['dsn'] == DSN
    assert payload['period'] == 'day'
    assert payload['date'] == '2026-07-20'
    assert payload['week'] == '2026-W30'
    assert payload['samples'] == 8
    assert payload['hours_with_data'] == 2
    assert payload['median_from'] == 'samples'
    assert sorted(payload['hours']) == ['00', '12']
    assert payload['hours']['00']['samples'] == 4
    assert payload['metrics']['heart_rate']['count'] == 8
    assert payload['metrics']['heart_rate']['min'] == 120
    assert payload['metrics']['heart_rate']['max'] == 123
    assert payload['metrics']['heart_rate']['range'] == 3
    assert payload['metrics']['heart_rate']['histogram'] == {'120': 8}
    assert payload['hours']['12']['metrics']['oxygen_level']['count'] == 4
    assert payload['alerts']['low_oxygen']['active_samples'] == 0


def test_day_summary_counts_alerts():
    summarizer, raw, _ = build()
    for index in range(4):
        timestamp = epoch(MONDAY, 3, index)
        raw.add('owlet_%s_%d' % (DSN, int(timestamp)),
                reading(timestamp,
                        alerts={'low_oxygen': 1 if index < 3 else 0,
                                'low_battery': 0}))

    bucket = summarizer.collect(today=TUESDAY)[(DSN, MONDAY)]
    payload = summarizer.build_day_summary(DSN, MONDAY, bucket)

    assert payload['alerts']['low_oxygen'] == {'active_samples': 3,
                                               'share': 0.75}
    assert payload['alerts']['low_battery']['active_samples'] == 0


def test_week_summary_merges_the_daily_summaries():
    summarizer, raw, summaries = build()
    fill_day(raw, MONDAY, samples=3, hours=(1,))
    fill_day(raw, TUESDAY, samples=3, hours=(1,))

    summarizer.run(today=WEDNESDAY)
    week = summaries.get('owlet_summary_%s_2026-W30' % DSN)

    assert week['period'] == 'week'
    assert week['week'] == '2026-W30'
    assert week['days'] == ['2026-07-20', '2026-07-21']
    assert week['start_date'] == '2026-07-20'
    assert week['end_date'] == '2026-07-21'
    assert week['samples'] == 6
    assert week['median_from'] == 'histogram'
    assert week['metrics']['heart_rate']['count'] == 6
    assert week['metrics']['heart_rate']['min'] == 120
    assert week['metrics']['heart_rate']['max'] == 122
    assert sorted(week['daily']) == ['2026-07-20', '2026-07-21']
    assert week['daily']['2026-07-20']['samples'] == 3
    assert week['daily']['2026-07-20']['metrics']['heart_rate']['avg'] == 121
    assert 'histogram' not in \
        week['daily']['2026-07-20']['metrics']['heart_rate']


def test_week_summary_is_none_without_daily_summaries():
    summarizer, _, _ = build()

    assert summarizer.build_week_summary(DSN, MONDAY) is None


# --- run --------------------------------------------------------------


def test_run_summarizes_then_deletes_the_raw_entries():
    summarizer, raw, summaries = build()
    raw_ids = fill_day(raw, MONDAY, samples=5, hours=(0, 12))
    # The always current entry must survive the clean up.
    latest_id = raw.add('owlet_%s' % DSN, reading(epoch(TUESDAY, 1)))

    report = summarizer.run(today=TUESDAY)

    assert report['days_processed'] == 1
    assert report['days_failed'] == 0
    assert report['summaries_written'] == 2
    assert report['entries_deleted'] == len(raw_ids)
    assert report['entries_kept'] == 0
    assert sorted(raw.deleted) == sorted(raw_ids)
    assert summaries.get('owlet_summary_%s_2026-07-20' % DSN) is not None
    assert summaries.get('owlet_summary_%s_2026-W30' % DSN) is not None
    assert raw.store[latest_id]['key'] == 'owlet_%s' % DSN


def test_run_keeps_raw_entries_within_the_retention_window():
    summarizer, raw, summaries = build(OWLET_RAW_RETENTION_DAYS='2')
    fill_day(raw, MONDAY, samples=2, hours=(1,))

    report = summarizer.run(today=TUESDAY)

    assert report['summaries_written'] == 2
    assert report['entries_deleted'] == 0
    assert report['entries_kept'] == 2
    assert raw.deleted == []
    assert summaries.get('owlet_summary_%s_2026-07-20' % DSN) is not None


def test_run_deletes_once_the_retention_window_has_passed():
    summarizer, raw, _ = build(OWLET_RAW_RETENTION_DAYS='1')
    fill_day(raw, MONDAY, samples=2, hours=(1,))

    report = summarizer.run(today=WEDNESDAY)

    assert report['entries_deleted'] == 2


def test_run_keeps_raw_entries_with_keep_raw():
    summarizer, raw, summaries = build()
    fill_day(raw, MONDAY, samples=2, hours=(1,))

    report = summarizer.run(today=TUESDAY, keep_raw=True)

    assert report['entries_deleted'] == 0
    assert report['entries_kept'] == 2
    assert summaries.get('owlet_summary_%s_2026-07-20' % DSN) is not None


def test_run_dry_run_changes_nothing():
    summarizer, raw, summaries = build()
    fill_day(raw, MONDAY, samples=2, hours=(1,))

    report = summarizer.run(today=TUESDAY, dry_run=True)

    assert report['dry_run'] is True
    assert report['days_processed'] == 1
    assert report['summaries_written'] == 0
    assert report['entries_deleted'] == 0
    assert raw.deleted == []
    assert summaries.get('owlet_summary_%s_2026-07-20' % DSN) is None


def test_run_keeps_raw_entries_when_the_summary_cannot_be_written():
    summarizer, raw, summaries = build()
    fill_day(raw, MONDAY, samples=2, hours=(1,))
    summaries.create = lambda key, value: False
    summaries.update = lambda key, value: None

    report = summarizer.run(today=TUESDAY)

    assert report['days_failed'] == 1
    assert report['entries_deleted'] == 0
    assert report['entries_kept'] == 2
    assert raw.deleted == []


def test_run_reports_failed_deletions():
    summarizer, raw, _ = build()
    raw_ids = fill_day(raw, MONDAY, samples=3, hours=(1,))
    raw.fail_delete = {raw_ids[0]}

    report = summarizer.run(today=TUESDAY)

    assert report['entries_deleted'] == 2
    assert report['delete_failures'] == 1


def test_run_keeps_a_more_complete_existing_summary():
    summarizer, raw, summaries = build()
    raw_ids = fill_day(raw, MONDAY, samples=4, hours=(1,))

    summarizer.run(today=TUESDAY)
    first = summaries.get('owlet_summary_%s_2026-07-20' % DSN)
    assert first['samples'] == 4

    # A single raw entry survived the previous clean up, so the next run
    # sees it again. The better summary must not be overwritten, and the
    # leftover has to be removed.
    raw.store[raw_ids[0]] = {'id': raw_ids[0], 'category': 'owlet',
                             'key': 'owlet_%s_%d' % (DSN,
                                                     int(epoch(MONDAY, 1))),
                             'value': reading(epoch(MONDAY, 1))}
    report = summarizer.run(today=TUESDAY)

    second = summaries.get('owlet_summary_%s_2026-07-20' % DSN)
    assert second['samples'] == 4
    assert report['entries_deleted'] == 1
    assert raw_ids[0] not in raw.store


def test_run_aborts_without_deleting_when_a_page_cannot_be_read():
    summarizer, raw, summaries = build()
    fill_day(raw, MONDAY, samples=2, hours=(1,))
    raw.fail_page = 2

    with pytest.raises(HomeAPIError):
        summarizer.run(today=TUESDAY)

    assert raw.deleted == []
    assert summaries.get('owlet_summary_%s_2026-07-20' % DSN) is None


def test_run_without_data():
    summarizer, _, _ = build()

    report = summarizer.run(today=TUESDAY)

    assert report['days_processed'] == 0
    assert report['days'] == []


def test_run_handles_several_devices():
    summarizer, raw, summaries = build()
    fill_day(raw, MONDAY, samples=2, hours=(1,), dsn=DSN)
    fill_day(raw, MONDAY, samples=2, hours=(1,), dsn='AC000W00OTHER')

    report = summarizer.run(today=TUESDAY)

    assert report['days_processed'] == 2
    assert summaries.get('owlet_summary_%s_2026-07-20' % DSN) is not None
    assert summaries.get(
        'owlet_summary_AC000W00OTHER_2026-07-20') is not None


# --- payload size -----------------------------------------------------


def test_full_day_summary_fits_into_a_homeapi_entry():
    summarizer, raw, _ = build()

    # A full day at a two second interval, 43200 readings.
    bucket = None
    for hour in range(24):
        for index in range(1800):
            timestamp = epoch(MONDAY, hour) + index * 2
            local = time.localtime(timestamp)
            if bucket is None:
                from owlet_api.homeapi_summarizer import DayBucket
                bucket = DayBucket()
            bucket.add(index, timestamp, '%02d' % local.tm_hour,
                       {'heart_rate': 120 + index % 40,
                        'oxygen_level': 94 + index % 6,
                        'movement': index % 2,
                        'battery_level': 60 + index % 40},
                       {'low_oxygen': index % 100 == 0})

    payload = summarizer.build_day_summary(DSN, MONDAY, bucket)
    encoded = json.dumps(payload)

    assert payload['samples'] == 43200
    assert len(payload['hours']) == 24
    assert len(encoded) < MAX_VALUE_CHARS
    assert 'histogram' in payload['hours']['00']['metrics']['heart_rate']


def test_fit_payload_drops_details_until_it_fits():
    payload = {
        'dsn': DSN,
        'date': '2026-07-20',
        'metrics': {'heart_rate': {'count': 1}},
        'hours': {
            '%02d' % hour: {
                'samples': 1800,
                'metrics': {
                    'heart_rate': {
                        'count': 1800, 'p10': 1, 'p90': 2, 'sum': 3,
                        'histogram': {str(value): 1 for value in range(400)},
                    },
                },
            }
            for hour in range(24)
        },
    }

    fitted = fit_payload(payload, limit=4000)
    hourly = fitted['hours']['00']['metrics']['heart_rate']

    # Dropping the hourly histograms is enough here, the percentiles of
    # the remaining hours are kept.
    assert 'histogram' not in hourly
    assert 'p10' in hourly
    assert len(json.dumps(fitted)) <= 4000


def test_fit_payload_drops_hourly_percentiles_next():
    payload = {
        'dsn': DSN,
        'date': '2026-07-20',
        'metrics': {'heart_rate': {'count': 1}},
        'hours': {
            '%02d' % hour: {
                'samples': 1800,
                'metrics': {
                    'heart_rate': {
                        'count': 1800, 'p10': 1, 'p90': 2, 'sum': 3,
                        'histogram': {str(value): 1 for value in range(400)},
                    },
                },
            }
            for hour in range(24)
        },
    }

    # Too tight for the histograms and the percentiles, but the hourly
    # sample counts and averages still fit.
    fitted = fit_payload(payload, limit=2000)
    hourly = fitted['hours']['00']['metrics']['heart_rate']

    assert 'histogram' not in hourly
    assert 'p10' not in hourly
    assert 'sum' not in hourly
    assert hourly['count'] == 1800
    assert len(json.dumps(fitted)) <= 2000


def test_fit_payload_gives_up_the_hours_when_needed():
    payload = {
        'dsn': DSN,
        'week': '2026-W30',
        'hours': {'00': {'samples': 1, 'metrics': {}}},
        'daily': {'2026-07-20': {'samples': 1}},
    }

    fitted = fit_payload(payload, limit=10)

    assert 'hours' not in fitted
    assert 'daily' not in fitted


# --- HTTP client ------------------------------------------------------


@responses.activate
def test_client_iter_entries_pages_and_dedupes():
    responses.add(
        responses.GET, 'http://homeapi.local:9999/api/entries',
        json={'entries': [{'id': 1, 'key': 'a'}, {'id': 2, 'key': 'b'}],
              'total': 3, 'page': 1, 'per_page': 2, 'total_pages': 2},
        status=200)
    responses.add(
        responses.GET, 'http://homeapi.local:9999/api/entries',
        json={'entries': [{'id': 2, 'key': 'b'}, {'id': 3, 'key': 'c'}],
              'total': 3, 'page': 2, 'per_page': 2, 'total_pages': 2},
        status=200)

    client = HomeAPIClient('http://homeapi.local:9999', 'owlet')
    entries = list(client.iter_entries(per_page=2))

    assert [entry['id'] for entry in entries] == [1, 2, 3]
    assert 'category=owlet' in responses.calls[0].request.url


@responses.activate
def test_client_iter_entries_raises_on_a_failed_page():
    responses.add(responses.GET, 'http://homeapi.local:9999/api/entries',
                  json={'error': 'boom'}, status=500)

    client = HomeAPIClient('http://homeapi.local:9999', 'owlet')

    with pytest.raises(HomeAPIError):
        list(client.iter_entries())


@responses.activate
def test_client_get_and_delete():
    responses.add(responses.GET,
                  'http://homeapi.local:9999/api/entries/owlet_test',
                  json={'id': 1, 'key': 'owlet_test',
                        'value': {'samples': 5}}, status=200)
    responses.add(responses.DELETE,
                  'http://homeapi.local:9999/api/entries/7',
                  json={'deleted': True}, status=200)
    responses.add(responses.DELETE,
                  'http://homeapi.local:9999/api/entries/8',
                  json={'error': 'boom'}, status=500)

    client = HomeAPIClient('http://homeapi.local:9999', 'owlet')

    assert client.get('owlet_test') == {'samples': 5}
    assert client.delete(7) is True
    assert client.delete(8) is False


@responses.activate
def test_client_get_missing_entry():
    responses.add(responses.GET,
                  'http://homeapi.local:9999/api/entries/nope',
                  json={'error': 'not found'}, status=404)

    client = HomeAPIClient('http://homeapi.local:9999', 'owlet')

    assert client.get('nope') is None


# --- command line -----------------------------------------------------


def test_main_check_config(monkeypatch):
    monkeypatch.setenv('HOMEAPI_URL', 'http://homeapi.local:9999')

    assert main(['--check-config']) == 0


def test_main_rejects_a_bad_date(monkeypatch):
    monkeypatch.setenv('HOMEAPI_URL', 'http://homeapi.local:9999')

    assert main(['--date', 'yesterday']) == 2


def test_main_rejects_a_bad_url(monkeypatch):
    monkeypatch.setenv('HOMEAPI_URL', 'homeapi.local:9999')

    assert main(['--check-config']) == 2


def test_main_reports_a_failed_scan(monkeypatch, capsys):
    monkeypatch.setenv('HOMEAPI_URL', 'http://homeapi.local:9999')

    def explode(self, **kwargs):
        raise HomeAPIError('page 1 failed')

    monkeypatch.setattr(Summarizer, 'run', explode)

    assert main([]) == 1


def test_main_prints_the_report_as_json(monkeypatch, capsys):
    monkeypatch.setenv('HOMEAPI_URL', 'http://homeapi.local:9999')

    monkeypatch.setattr(
        Summarizer, 'run',
        lambda self, **kwargs: {'days_processed': 1, 'summaries_written': 2,
                                'entries_deleted': 3, 'entries_kept': 0,
                                'days_failed': 0, 'delete_failures': 0})

    assert main(['--json']) == 0
    assert json.loads(capsys.readouterr().out)['entries_deleted'] == 3


def test_main_fails_when_a_day_could_not_be_summarized(monkeypatch):
    monkeypatch.setenv('HOMEAPI_URL', 'http://homeapi.local:9999')

    monkeypatch.setattr(
        Summarizer, 'run',
        lambda self, **kwargs: {'days_processed': 0, 'summaries_written': 0,
                                'entries_deleted': 0, 'entries_kept': 5,
                                'days_failed': 1, 'delete_failures': 0})

    assert main([]) == 1
