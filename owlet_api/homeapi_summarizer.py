#!/usr/bin/env python
"""Roll up per-reading Owlet data in HomeAPI into daily summaries.

The publisher writes one HomeAPI entry per reading, which at a two
second interval is roughly 43000 entries per device per day. This module
implements the ``owlet-homeapi-summarize`` command, run once a day by a
systemd timer, which

* reads every raw reading of the completed days,
* writes one summary entry per device and day (overall plus hourly
  statistics: sample count, range, average, median, percentiles and a
  histogram),
* refreshes the summary entry of the ISO week the day belongs to, built
  from the daily summaries,
* and only then deletes the raw entries it has summarized.

Configuration comes from the same environment file as the publisher.
"""

import argparse
import json
import logging
import os
import re
import statistics
import sys
import time
from datetime import date
from datetime import timedelta

from .homeapiclient import DEFAULT_DELETE_BATCH_SIZE
from .homeapiclient import HomeAPIClient
from .homeapiclient import HomeAPIError
from .homeapiclient import MAX_VALUE_CHARS
from .homeapiclient import decode_value
from .homeapiconfig import ConfigurationError
from .homeapiconfig import get_float
from .homeapiconfig import get_int
from .homeapiconfig import get_list
from .homeapiconfig import get_optional
from .homeapiconfig import sanitize_key
from .homeapiconfig import setup_logging

LOGGER = logging.getLogger('owlet_homeapi')

DEFAULT_HOMEAPI_URL = 'http://localhost:9999'
DEFAULT_CATEGORY = 'owlet'
DEFAULT_KEY_PREFIX = 'owlet'
DEFAULT_SUMMARY_CATEGORY = 'owlet_summary'
DEFAULT_SUMMARY_KEY_PREFIX = 'owlet_summary'
DEFAULT_HTTP_TIMEOUT = 30.0
DEFAULT_RETENTION_DAYS = 0

# Metrics that are summarized by default, in the order they are written.
DEFAULT_METRICS = ('heart_rate', 'oxygen_level', 'movement',
                   'battery_level')

# Histogram bins per metric as (lowest edge, highest edge, bin width).
# The bins are fixed so that histograms of different periods can be
# added up; values outside of the range land in the first or last bin,
# while the reported minimum and maximum are always the true ones.
METRIC_BINS = {
    'heart_rate': (0.0, 300.0, 5.0),
    'oxygen_level': (70.0, 100.0, 1.0),
    'movement': (0.0, 10.0, 1.0),
    'battery_level': (0.0, 100.0, 5.0),
    'charge_status': (0.0, 4.0, 1.0),
    'sock_connection': (0.0, 2.0, 1.0),
    'base_station_on': (0.0, 2.0, 1.0),
}

DEFAULT_BIN_SPEC = (0.0, 1000.0, 10.0)

# Percentiles reported next to the median.
PERCENTILES = (10, 90)

# Values that mean "no alert" in the Owlet attribute set.
INACTIVE = (None, 0, 0.0, False, '', '0', 'None', 'none', 'false')


class SummaryConfig():
    """Runtime configuration of the summarizer."""

    # A configuration object is a bag of values by nature.
    # pylint: disable=R0902,R0903
    def __init__(self, env=None):
        """Build the configuration from ``env`` (defaults to os.environ)."""
        if env is None:
            env = os.environ

        self.homeapi_url = env.get(
            'HOMEAPI_URL', DEFAULT_HOMEAPI_URL).strip().rstrip('/')
        self.category = env.get('HOMEAPI_CATEGORY', DEFAULT_CATEGORY).strip()
        self.key_prefix = env.get(
            'HOMEAPI_KEY_PREFIX', DEFAULT_KEY_PREFIX).strip()
        self.summary_category = env.get(
            'HOMEAPI_SUMMARY_CATEGORY', DEFAULT_SUMMARY_CATEGORY).strip()
        self.summary_key_prefix = env.get(
            'HOMEAPI_SUMMARY_KEY_PREFIX', DEFAULT_SUMMARY_KEY_PREFIX).strip()
        self.http_timeout = get_float(
            env, 'HOMEAPI_TIMEOUT', DEFAULT_HTTP_TIMEOUT)
        self.metrics = get_list(env, 'OWLET_SUMMARY_METRICS') or \
            list(DEFAULT_METRICS)
        self.retention_days = get_int(
            env, 'OWLET_RAW_RETENTION_DAYS', DEFAULT_RETENTION_DAYS)
        self.delete_batch_size = get_int(
            env, 'HOMEAPI_DELETE_BATCH_SIZE', DEFAULT_DELETE_BATCH_SIZE)
        self.device = get_optional(env, 'OWLET_DEVICE')
        self.log_level = env.get('OWLET_LOG_LEVEL', 'info').strip().upper()

    def validate(self):
        """Check the configuration, raise ConfigurationError if unusable."""
        if not self.homeapi_url.startswith(('http://', 'https://')):
            raise ConfigurationError(
                'HOMEAPI_URL must start with http:// or https://')

        if self.retention_days < 0:
            raise ConfigurationError(
                'OWLET_RAW_RETENTION_DAYS must not be negative')

        if self.delete_batch_size < 1:
            raise ConfigurationError(
                'HOMEAPI_DELETE_BATCH_SIZE must be at least 1')

        if self.summary_category == self.category and \
           self.summary_key_prefix == self.key_prefix:
            raise ConfigurationError(
                'HOMEAPI_SUMMARY_CATEGORY or HOMEAPI_SUMMARY_KEY_PREFIX '
                'must differ from the raw category and key prefix')


def bin_spec(metric):
    """Return the histogram bin specification of a metric."""
    return METRIC_BINS.get(metric, DEFAULT_BIN_SPEC)


def _number(value):
    """Return a float as int when it is integral, for compact output."""
    if float(value).is_integer():
        return int(value)

    return round(float(value), 3)


def bin_label(value, spec):
    """Return the histogram bin label a value belongs to."""
    low, high, width = spec
    bins = max(1, int(round((high - low) / width)))
    index = int((float(value) - low) // width)
    index = max(0, min(bins - 1, index))

    return str(_number(low + index * width))


def histogram(values, spec):
    """Return a sparse histogram of values, keyed by bin lower edge."""
    result = {}

    for value in values:
        label = bin_label(value, spec)
        result[label] = result.get(label, 0) + 1

    return result


def merge_histograms(histograms):
    """Return the sum of several histograms."""
    result = {}

    for part in histograms:
        for label, count in part.items():
            result[label] = result.get(label, 0) + count

    return result


def percentile(values, wanted):
    """Return a percentile of an unsorted list, by linear interpolation."""
    if not values:
        return None

    ordered = sorted(values)

    if len(ordered) == 1:
        return _number(ordered[0])

    position = (len(ordered) - 1) * wanted / 100.0
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower

    return _number(ordered[lower] * (1 - weight) + ordered[upper] * weight)


def percentile_from_histogram(hist, spec, wanted):
    """Estimate a percentile from a histogram, accurate to the bin width."""
    total = sum(hist.values())

    if not total:
        return None

    target = total * wanted / 100.0
    seen = 0

    for label in sorted(hist, key=float):
        count = hist[label]

        if seen + count >= target:
            within = (target - seen) / float(count)
            return _number(float(label) + within * spec[2])

        seen = seen + count

    return _number(max(float(label) for label in hist) + spec[2])


def stats_from_values(values, spec):
    """Return the statistics of a list of samples, None when empty."""
    if not values:
        return None

    stats = {
        'count': len(values),
        'min': _number(min(values)),
        'max': _number(max(values)),
        'range': _number(max(values) - min(values)),
        'sum': _number(math_sum(values)),
        'avg': _number(round(math_sum(values) / len(values), 3)),
        'median': _number(statistics.median(values)),
        'histogram': histogram(values, spec),
    }

    for wanted in PERCENTILES:
        stats['p%d' % wanted] = percentile(values, wanted)

    return stats


def merge_stats(parts, spec):
    """Merge statistics of several periods, None when there are none.

    Counts, sums and extremes stay exact, while median and percentiles
    are estimated from the merged histogram since the samples they were
    computed from are gone by then.
    """
    parts = [part for part in parts if part and part.get('count')]

    if not parts:
        return None

    count = sum(part['count'] for part in parts)
    total = math_sum(part.get('sum', 0) for part in parts)
    merged = merge_histograms(
        [part.get('histogram', {}) for part in parts])
    lowest = min(part['min'] for part in parts)
    highest = max(part['max'] for part in parts)

    stats = {
        'count': count,
        'min': _number(lowest),
        'max': _number(highest),
        'range': _number(highest - lowest),
        'sum': _number(total),
        'avg': _number(round(total / count, 3)),
        'median': percentile_from_histogram(merged, spec, 50),
        'histogram': merged,
    }

    for wanted in PERCENTILES:
        stats['p%d' % wanted] = percentile_from_histogram(
            merged, spec, wanted)

    return stats


def math_sum(values):
    """Return the sum of values as float."""
    return float(sum(values))


def is_alert_active(value):
    """Return True if an Owlet alert attribute is raised."""
    if isinstance(value, str):
        return value.strip().lower() not in ('', '0', 'none', 'false')

    return value not in INACTIVE


def iso_week_key(day):
    """Return the ISO week identifier of a date, e.g. "2026-W30"."""
    iso = day.isocalendar()
    return '%04d-W%02d' % (iso[0], iso[1])


def iso_week_dates(day):
    """Return the seven dates of the ISO week a date belongs to."""
    monday = day - timedelta(days=day.isocalendar()[2] - 1)
    return [monday + timedelta(days=offset) for offset in range(7)]


def parse_day(text):
    """Parse a YYYY-MM-DD string into a date, raise ValueError if bad."""
    parts = [int(part) for part in text.split('-')]

    if len(parts) != 3:
        raise ValueError('expected YYYY-MM-DD, got "%s"' % text)

    return date(*parts)


def _local(timestamp):
    """Return an ISO-8601 local timestamp for an epoch."""
    return time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(timestamp))


class DayBucket():
    """Collects the raw samples of one device and one local day."""

    # pylint: disable=R0902
    def __init__(self):
        """Start an empty bucket."""
        self.entry_ids = []
        self.samples = 0
        self.first = None
        self.last = None
        self.hours = {}
        self.hour_samples = {}
        self.alerts = {}
        self.alert_names = set()

    def add(self, entry_id, timestamp, hour, metrics, alerts):
        """Add one raw reading to the bucket."""
        if entry_id is not None:
            self.entry_ids.append(entry_id)

        self.samples = self.samples + 1

        if self.first is None or timestamp < self.first:
            self.first = timestamp
        if self.last is None or timestamp > self.last:
            self.last = timestamp

        per_hour = self.hours.setdefault(hour, {})
        for name, value in metrics.items():
            per_hour.setdefault(name, []).append(value)

        self.hour_samples[hour] = self.hour_samples.get(hour, 0) + 1

        for name, value in alerts.items():
            # Every alert seen is reported, so that a quiet day still
            # shows a zero instead of a missing key.
            self.alert_names.add(name)

            if is_alert_active(value):
                self.alerts[name] = self.alerts.get(name, 0) + 1

    def values(self, metric):
        """Return every sample of a metric across the whole day."""
        collected = []

        for per_hour in self.hours.values():
            collected.extend(per_hour.get(metric, []))

        return collected


def numeric_metrics(reading, wanted):
    """Extract the numeric metrics of a reading, ignoring the rest."""
    vitals = reading.get('vitals') if isinstance(reading, dict) else None
    result = {}

    if not isinstance(vitals, dict):
        return result

    for name in wanted:
        if name not in vitals:
            continue

        try:
            result[name] = float(vitals[name])
        except (TypeError, ValueError):
            continue

    return result


class Summarizer():
    """Summarizes raw HomeAPI readings and cleans them up."""

    def __init__(self, config, raw_client=None, summary_client=None):
        """Initialize with a config and optional collaborators."""
        self.config = config
        self.raw_client = raw_client if raw_client is not None else \
            HomeAPIClient(config.homeapi_url, config.category,
                          config.http_timeout,
                          delete_batch_size=config.delete_batch_size)
        self.summary_client = summary_client \
            if summary_client is not None else \
            HomeAPIClient(config.homeapi_url, config.summary_category,
                          config.http_timeout)
        self.raw_key = re.compile(
            r'^%s_(?P<dsn>[A-Za-z0-9.-]+)_(?P<epoch>\d{9,})$'
            % re.escape(config.key_prefix))

    def day_key(self, dsn, day):
        """Return the HomeAPI key of a daily summary."""
        return sanitize_key('%s_%s_%s' % (self.config.summary_key_prefix,
                                          dsn, day.isoformat()))

    def week_key(self, dsn, day):
        """Return the HomeAPI key of the weekly summary of a day."""
        return sanitize_key('%s_%s_%s' % (self.config.summary_key_prefix,
                                          dsn, iso_week_key(day)))

    def parse_entry(self, entry):
        """Return (dsn, timestamp) of a raw entry, or None if it is not one."""
        key = entry.get('key') or ''
        match = self.raw_key.match(key)

        if match is None:
            return None

        dsn = match.group('dsn')

        if self.config.device is not None and dsn != self.config.device:
            return None

        reading = decode_value(entry.get('value'))
        timestamp = float(match.group('epoch'))

        if isinstance(reading, dict):
            try:
                timestamp = float(reading['timestamp'])
            except (KeyError, TypeError, ValueError):
                pass

        return dsn, timestamp, reading

    def collect(self, target_day=None, today=None):
        """Group every raw reading of the completed days into buckets."""
        if today is None:
            today = date.today()

        buckets = {}
        skipped = 0

        for entry in self.raw_client.iter_entries():
            parsed = self.parse_entry(entry)

            if parsed is None:
                skipped = skipped + 1
                continue

            dsn, timestamp, reading = parsed
            local = time.localtime(timestamp)
            day = date(local.tm_year, local.tm_mon, local.tm_mday)

            # Today is still being written to, and an explicitly
            # requested day excludes every other one.
            if day >= today:
                continue
            if target_day is not None and day != target_day:
                continue

            bucket = buckets.get((dsn, day))
            if bucket is None:
                bucket = DayBucket()
                buckets[(dsn, day)] = bucket

            bucket.add(
                entry.get('id'),
                timestamp,
                '%02d' % local.tm_hour,
                numeric_metrics(reading, self.config.metrics),
                reading.get('alerts', {}) if isinstance(reading, dict) else {},
            )

        if skipped:
            LOGGER.debug('Ignored %d entries that are not raw readings',
                         skipped)

        return buckets

    def build_day_summary(self, dsn, day, bucket):
        """Build the summary payload of one device and one day."""
        hours = {}

        for hour in sorted(bucket.hours):
            metrics = {}

            for name in self.config.metrics:
                stats = stats_from_values(
                    bucket.hours[hour].get(name, []), bin_spec(name))
                if stats is not None:
                    metrics[name] = stats

            hours[hour] = {
                'samples': bucket.hour_samples.get(hour, 0),
                'metrics': metrics,
            }

        metrics = {}
        for name in self.config.metrics:
            stats = stats_from_values(bucket.values(name), bin_spec(name))
            if stats is not None:
                metrics[name] = stats

        payload = {
            'dsn': dsn,
            'period': 'day',
            'date': day.isoformat(),
            'week': iso_week_key(day),
            'generated_at': _local(time.time()),
            'samples': bucket.samples,
            'hours_with_data': len(hours),
            'first_sample_at': _local(bucket.first) if bucket.first else None,
            'last_sample_at': _local(bucket.last) if bucket.last else None,
            'median_from': 'samples',
            'metrics': metrics,
            'hours': hours,
            'alerts': _alert_summary(bucket.alerts, bucket.samples,
                                     bucket.alert_names),
        }

        return fit_payload(payload)

    def build_week_summary(self, dsn, day):
        """Build the weekly summary of a day from the daily summaries."""
        days = []
        daily = {}

        for other in iso_week_dates(day):
            summary = self.summary_client.get(self.day_key(dsn, other))

            if not isinstance(summary, dict) or not summary.get('metrics'):
                continue

            days.append(other.isoformat())
            daily[other.isoformat()] = summary

        if not days:
            return None

        metrics = {}
        for name in self.config.metrics:
            merged = merge_stats(
                [summary['metrics'].get(name) for summary in daily.values()],
                bin_spec(name))
            if merged is not None:
                metrics[name] = merged

        alerts = {}
        samples = 0
        for summary in daily.values():
            samples = samples + summary.get('samples', 0)
            for name, counts in (summary.get('alerts') or {}).items():
                alerts[name] = alerts.get(name, 0) + \
                    counts.get('active_samples', 0)

        payload = {
            'dsn': dsn,
            'period': 'week',
            'week': iso_week_key(day),
            'start_date': days[0],
            'end_date': days[-1],
            'days': days,
            'generated_at': _local(time.time()),
            'samples': samples,
            'median_from': 'histogram',
            'metrics': metrics,
            'daily': {
                other: _daily_digest(summary)
                for other, summary in daily.items()
            },
            'alerts': _alert_summary(alerts, samples),
        }

        return fit_payload(payload)

    def deletable(self, day, today):
        """Return True if a day's raw readings may be deleted now."""
        return (today - day).days > self.config.retention_days

    def delete_raw(self, bucket):
        """Delete the raw entries of a bucket, return (deleted, failed)."""
        return self.raw_client.delete_ids(bucket.entry_ids)

    # The run loop is a sequence of clearly named steps, splitting it up
    # further would not make it easier to follow.
    # pylint: disable=R0912,R0914
    def run(self, target_day=None, dry_run=False, keep_raw=False,
            today=None):
        """Summarize the completed days and drop the raw readings."""
        if today is None:
            today = date.today()

        buckets = self.collect(target_day, today)
        report = {
            'days_processed': 0,
            'days_failed': 0,
            'summaries_written': 0,
            'entries_summarized': 0,
            'entries_deleted': 0,
            'entries_kept': 0,
            'delete_failures': 0,
            'dry_run': bool(dry_run),
            'days': [],
        }

        if not buckets:
            LOGGER.info('No completed days with raw readings found')
            return report

        for dsn, day in sorted(buckets, key=lambda item: (item[1], item[0])):
            bucket = buckets[(dsn, day)]
            detail = {
                'dsn': dsn,
                'date': day.isoformat(),
                'week': iso_week_key(day),
                'samples': bucket.samples,
                'summary_written': False,
                'week_written': False,
                'deleted': 0,
            }
            report['days'].append(detail)
            report['entries_summarized'] += len(bucket.entry_ids)

            payload = self.build_day_summary(dsn, day, bucket)

            if dry_run:
                LOGGER.info(
                    'Would summarize %s %s: %d samples over %d hours, '
                    'would delete %d raw entries', dsn, day,
                    bucket.samples, detail_hours(payload),
                    len(bucket.entry_ids)
                    if self.deletable(day, today) and not keep_raw else 0)
                report['days_processed'] += 1
                continue

            if not self.write_day_summary(dsn, day, payload):
                LOGGER.warning(
                    'Keeping the raw readings of %s %s, its summary could '
                    'not be written', dsn, day)
                report['days_failed'] += 1
                report['entries_kept'] += len(bucket.entry_ids)
                continue

            detail['summary_written'] = True
            report['summaries_written'] += 1

            week = self.build_week_summary(dsn, day)

            if week is None or not self.summary_client.upsert(
                    self.week_key(dsn, day), week):
                LOGGER.warning(
                    'Keeping the raw readings of %s %s, the summary of week '
                    '%s could not be written', dsn, day, iso_week_key(day))
                report['days_failed'] += 1
                report['entries_kept'] += len(bucket.entry_ids)
                continue

            detail['week_written'] = True
            report['summaries_written'] += 1
            report['days_processed'] += 1

            if keep_raw or not self.deletable(day, today):
                report['entries_kept'] += len(bucket.entry_ids)
                LOGGER.info('Summarized %s %s: %d samples, keeping the raw '
                            'readings', dsn, day, bucket.samples)
                continue

            deleted, failed = self.delete_raw(bucket)
            detail['deleted'] = deleted
            report['entries_deleted'] += deleted
            report['delete_failures'] += failed

            LOGGER.info('Summarized %s %s: %d samples, deleted %d raw '
                        'entries%s', dsn, day, bucket.samples, deleted,
                        ', %d deletions failed' % failed if failed else '')

        return report

    def write_day_summary(self, dsn, day, payload):
        """Write a daily summary, keeping a more complete existing one."""
        key = self.day_key(dsn, day)
        existing = self.summary_client.get(key)

        if isinstance(existing, dict) and \
           existing.get('period') == 'day' and \
           existing.get('samples', 0) > payload['samples']:
            # A previous run summarized this day from more readings and
            # some of them could not be deleted afterwards. Keep the
            # better summary instead of overwriting it with less data.
            LOGGER.info(
                'Keeping the existing summary of %s %s, it covers %d '
                'samples instead of %d', dsn, day,
                existing['samples'], payload['samples'])
            return True

        return self.summary_client.upsert(key, payload)


def detail_hours(payload):
    """Return the number of hours a summary payload covers."""
    return len(payload.get('hours') or {})


def _daily_digest(summary):
    """Return the compact per day view stored in a weekly summary."""
    digest = {'samples': summary.get('samples', 0), 'metrics': {}}

    for name, stats in (summary.get('metrics') or {}).items():
        digest['metrics'][name] = {
            key: stats[key]
            for key in ('count', 'min', 'max', 'range', 'avg', 'median')
            if key in stats
        }

    return digest


def _alert_summary(counts, samples, names=None):
    """Return alert counts together with their share of all samples."""
    summary = {}
    wanted = set(counts) | set(names or ())

    for name in sorted(wanted):
        count = counts.get(name, 0)
        summary[name] = {
            'active_samples': count,
            'share': round(count / float(samples), 5) if samples else 0,
        }

    return summary


def fit_payload(payload, limit=MAX_VALUE_CHARS):
    """Shrink a summary until HomeAPI accepts it as an entry value.

    HomeAPI rejects values above 100000 characters, so the least
    important details are dropped first rather than losing the summary.
    """
    if len(json.dumps(payload)) <= limit:
        return payload

    for stats in _hourly_stats(payload):
        stats.pop('histogram', None)

    if len(json.dumps(payload)) <= limit:
        LOGGER.warning('Dropped the hourly histograms of the %s summary of '
                       '%s to stay below %d characters',
                       payload.get('date') or payload.get('week'),
                       payload.get('dsn'), limit)
        return payload

    for stats in _hourly_stats(payload):
        for wanted in PERCENTILES:
            stats.pop('p%d' % wanted, None)
        stats.pop('sum', None)

    if len(json.dumps(payload)) <= limit:
        LOGGER.warning('Dropped the hourly percentiles of the %s summary of '
                       '%s to stay below %d characters',
                       payload.get('date') or payload.get('week'),
                       payload.get('dsn'), limit)
        return payload

    payload.pop('hours', None)
    payload.pop('daily', None)
    LOGGER.warning('Dropped the hourly detail of the %s summary of %s to '
                   'stay below %d characters',
                   payload.get('date') or payload.get('week'),
                   payload.get('dsn'), limit)

    return payload


def _hourly_stats(payload):
    """Yield the statistics of every hour of a summary payload."""
    for hour in (payload.get('hours') or {}).values():
        yield from (hour.get('metrics') or {}).values()


def parse_args(argv=None):
    """Parse the command line arguments of the summarizer."""
    parser = argparse.ArgumentParser(
        description='Summarize Owlet readings stored in HomeAPI and delete '
                    'the per reading entries afterwards')
    parser.add_argument('--date', dest='day',
                        help='Only summarize this day (YYYY-MM-DD)')
    parser.add_argument('--dry-run', dest='dry_run', action='store_true',
                        help='Report what would happen, change nothing')
    parser.add_argument('--keep-raw', dest='keep_raw', action='store_true',
                        help='Write the summaries but keep the raw entries')
    parser.add_argument('--check-config', dest='check_config',
                        action='store_true',
                        help='Validate the configuration and exit')
    parser.add_argument('--json', dest='as_json', action='store_true',
                        help='Print the report as JSON')
    return parser.parse_args(argv)


def main(argv=None):
    """Entry point of the owlet-homeapi-summarize command."""
    args = parse_args(argv)

    try:
        config = SummaryConfig()
        config.validate()
        target_day = parse_day(args.day) if args.day else None
    except (ConfigurationError, ValueError) as error:
        setup_logging('INFO')
        LOGGER.error('%s', error)
        return 2

    setup_logging(config.log_level)

    if args.check_config:
        LOGGER.info('Configuration is valid, summarizing category "%s" of '
                    '%s into "%s"', config.category, config.homeapi_url,
                    config.summary_category)
        return 0

    summarizer = Summarizer(config)

    try:
        report = summarizer.run(target_day=target_day,
                                dry_run=args.dry_run,
                                keep_raw=args.keep_raw)
    except HomeAPIError as error:
        LOGGER.error('Aborted without changing anything: %s', error)
        return 1

    if args.as_json:
        print(json.dumps(report, indent=2))

    LOGGER.info('Done, %d day(s) summarized, %d summary entries written, '
                '%d raw entries deleted, %d kept',
                report['days_processed'], report['summaries_written'],
                report['entries_deleted'], report['entries_kept'])

    if report['days_failed'] or report['delete_failures']:
        return 1

    return 0


def init():
    """Mandatory init function."""
    if __name__ == "__main__":
        sys.exit(main())


init()
