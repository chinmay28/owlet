#!/usr/bin/env python
"""Client for the HomeAPI entry store.

`HomeAPI <https://github.com/chinmay28/HomeAPI>`_ is a small self hosted
key/value store. This module wraps the handful of endpoints needed to
publish Owlet readings, to summarize them and to clean them up again.
"""

import json
import logging

import requests
from requests.exceptions import RequestException

LOGGER = logging.getLogger('owlet_homeapi')

DEFAULT_HTTP_TIMEOUT = 10.0

# HomeAPI rejects entries whose value exceeds this many characters.
MAX_VALUE_CHARS = 100000

# HomeAPI clamps per_page to this maximum.
MAX_PER_PAGE = 200

# Ids sent per bulk delete request. HomeAPI turns them into one SQL
# placeholder each, and SQLite before 3.32 allows 999 variables per
# statement, so keep a comfortable margin below that.
DEFAULT_DELETE_BATCH_SIZE = 500
MAX_DELETE_BATCH_SIZE = 5000


class HomeAPIError(Exception):
    """Raised when HomeAPI cannot be read reliably."""


class HomeAPIClient():
    """Minimal client for the HomeAPI entries endpoint."""

    def __init__(self, base_url, category, timeout=DEFAULT_HTTP_TIMEOUT,
                 session=None, delete_batch_size=DEFAULT_DELETE_BATCH_SIZE):
        """Initialize the client for the given HomeAPI base url."""
        self.base_url = base_url.rstrip('/')
        self.category = category
        self.timeout = timeout
        self.session = session if session is not None else requests.Session()
        self.delete_batch_size = max(
            1, min(delete_batch_size, MAX_DELETE_BATCH_SIZE))
        # None until the first bulk delete tells us whether the server
        # supports the endpoint.
        self.bulk_delete = None

    def health(self):
        """Return True if the HomeAPI server answers its health check."""
        try:
            result = self.session.get(
                self.base_url + '/api/health', timeout=self.timeout)
        except RequestException as error:
            LOGGER.warning('HomeAPI health check failed: %s', error)
            return False

        return result.status_code == 200

    def create(self, key, value):
        """Create an entry, return True on success."""
        try:
            result = self.session.post(
                self.base_url + '/api/entries',
                json={
                    'category': self.category,
                    'key': key,
                    'value': value,
                },
                timeout=self.timeout,
            )
        except RequestException as error:
            LOGGER.warning('HomeAPI create failed for %s: %s', key, error)
            return False

        if result.status_code == 201:
            return True

        LOGGER.warning('HomeAPI create for %s returned %s: %s',
                       key, result.status_code, _body(result))
        return False

    def update(self, key, value):
        """Update an entry, return True on success, None if not found."""
        try:
            result = self.session.put(
                self.base_url + '/api/entries/' + key,
                json={'value': value},
                timeout=self.timeout,
            )
        except RequestException as error:
            LOGGER.warning('HomeAPI update failed for %s: %s', key, error)
            return False

        if result.status_code == 200:
            return True

        if result.status_code == 404:
            return None

        LOGGER.warning('HomeAPI update for %s returned %s: %s',
                       key, result.status_code, _body(result))
        return False

    def upsert(self, key, value):
        """Update an entry, creating it first time round."""
        updated = self.update(key, value)

        if updated is not None:
            return updated

        LOGGER.info('Creating HomeAPI entry %s/%s', self.category, key)
        return self.create(key, value)

    def get(self, key):
        """Return the value of an entry, or None if it does not exist."""
        try:
            result = self.session.get(
                self.base_url + '/api/entries/' + key, timeout=self.timeout)
        except RequestException as error:
            LOGGER.warning('HomeAPI get failed for %s: %s', key, error)
            return None

        if result.status_code == 404:
            return None

        if result.status_code != 200:
            LOGGER.warning('HomeAPI get for %s returned %s: %s',
                           key, result.status_code, _body(result))
            return None

        try:
            return decode_value(result.json().get('value'))
        except ValueError:
            LOGGER.warning('HomeAPI get for %s sent invalid json', key)
            return None

    def list_page(self, page, per_page=MAX_PER_PAGE):
        """Return one page of entries of this category, None on failure."""
        try:
            result = self.session.get(
                self.base_url + '/api/entries',
                params={
                    'category': self.category,
                    'page': page,
                    'per_page': min(per_page, MAX_PER_PAGE),
                },
                timeout=self.timeout,
            )
        except RequestException as error:
            LOGGER.warning('HomeAPI list page %s failed: %s', page, error)
            return None

        if result.status_code != 200:
            LOGGER.warning('HomeAPI list page %s returned %s: %s',
                           page, result.status_code, _body(result))
            return None

        try:
            return result.json()
        except ValueError:
            LOGGER.warning('HomeAPI list page %s sent invalid json', page)
            return None

    def iter_entries(self, per_page=MAX_PER_PAGE):
        """Yield every entry of this category, page by page.

        Raises HomeAPIError if any page cannot be read, so that callers
        never mistake a partial listing for the full data set.
        """
        page = 1
        seen = set()

        while True:
            payload = self.list_page(page, per_page)

            if payload is None:
                raise HomeAPIError(
                    'Could not read page %d of category "%s"'
                    % (page, self.category))

            entries = payload.get('entries') or []

            for entry in entries:
                identifier = entry.get('id')

                # New readings arriving during the scan shift entries
                # towards later pages, so the same entry can show up
                # twice. Ignore anything already seen.
                if identifier in seen:
                    continue

                seen.add(identifier)
                yield entry

            total_pages = payload.get('total_pages') or 0

            if not entries or page >= total_pages:
                break

            page = page + 1

    def delete(self, identifier):
        """Delete an entry by id or key, return True when it is gone."""
        try:
            result = self.session.delete(
                '%s/api/entries/%s' % (self.base_url, identifier),
                timeout=self.timeout)
        except RequestException as error:
            LOGGER.warning('HomeAPI delete failed for %s: %s',
                           identifier, error)
            return False

        if result.status_code in (200, 204, 404):
            return True

        LOGGER.warning('HomeAPI delete for %s returned %s: %s',
                       identifier, result.status_code, _body(result))
        return False

    def delete_ids(self, identifiers):
        """Delete entries of this category by id, in batches.

        Uses the bulk delete endpoint, so a day of readings costs a
        couple of requests instead of one per entry. Returns
        (deleted, failed); ids that no longer exist are neither deleted
        nor failed, they simply do not match any more.
        """
        remaining = list(identifiers)
        deleted = 0
        failed = 0

        while remaining:
            batch = remaining[:self.delete_batch_size]
            remaining = remaining[self.delete_batch_size:]

            if self.bulk_delete is False:
                gone, lost = self._delete_one_by_one(batch)
            else:
                gone, lost = self._bulk_delete_batch(batch)

            deleted = deleted + gone
            failed = failed + lost

        return deleted, failed

    def _bulk_delete_batch(self, batch):
        """Delete one batch of ids with the bulk delete endpoint."""
        try:
            result = self.session.delete(
                self.base_url + '/api/entries',
                # The category narrows the match, so a wrong id can
                # never take an entry outside of this category with it.
                json={'ids': batch, 'category': self.category},
                timeout=self.timeout,
            )
        except RequestException as error:
            LOGGER.warning('HomeAPI bulk delete of %d entries failed: %s',
                           len(batch), error)
            return 0, len(batch)

        # An older HomeAPI without the bulk delete endpoint answers with
        # "method not allowed". Fall back for the rest of this run.
        if result.status_code in (404, 405, 501):
            LOGGER.info('HomeAPI does not support bulk delete (status %s), '
                        'falling back to one request per entry',
                        result.status_code)
            self.bulk_delete = False
            return self._delete_one_by_one(batch)

        if result.status_code != 200:
            LOGGER.warning('HomeAPI bulk delete of %d entries returned '
                           '%s: %s', len(batch), result.status_code,
                           _body(result))
            return 0, len(batch)

        self.bulk_delete = True

        try:
            payload = result.json()
        except ValueError:
            LOGGER.warning('HomeAPI bulk delete sent invalid json')
            return 0, len(batch)

        deleted = payload.get('deleted')

        if not isinstance(deleted, int):
            LOGGER.warning('HomeAPI bulk delete did not report a count: %s',
                           _body(result))
            return 0, len(batch)

        if deleted < len(batch):
            LOGGER.debug('Bulk delete removed %d of %d entries, the rest '
                         'was already gone', deleted, len(batch))

        return deleted, 0

    def _delete_one_by_one(self, batch):
        """Delete a batch of ids with one request per entry."""
        deleted = 0
        failed = 0

        for identifier in batch:
            if self.delete(identifier):
                deleted = deleted + 1
            else:
                failed = failed + 1

        return deleted, failed


def decode_value(value):
    """Return an entry value as a Python object.

    The list endpoint sends the stored value as a JSON string while the
    single entry endpoint sends it as an object, so both are accepted.
    """
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value

    return value


def _body(result):
    """Return a short, printable version of a response body."""
    try:
        return json.dumps(result.json())[:200]
    except ValueError:
        return result.text[:200]
