#!/usr/bin/env python
"""Shared configuration helpers for the HomeAPI services.

Both the publisher and the summarizer take their configuration from the
process environment, which the systemd units source from
``/etc/owlet-homeapi/owlet-homeapi.env``.
"""

import logging
import sys


class ConfigurationError(Exception):
    """Raised when the environment does not contain a usable config."""


def get_optional(env, name):
    """Return a stripped environment value or None if it is empty."""
    value = env.get(name, '').strip()
    return value if value else None


def get_float(env, name, default):
    """Return an environment value as float, falling back to default."""
    value = env.get(name, '').strip()

    if not value:
        return default

    try:
        return float(value)
    except ValueError as error:
        raise ConfigurationError(
            '%s must be a number, got "%s"' % (name, value)) from error


def get_int(env, name, default):
    """Return an environment value as int, falling back to default."""
    value = env.get(name, '').strip()

    if not value:
        return default

    try:
        return int(value)
    except ValueError as error:
        raise ConfigurationError(
            '%s must be a whole number, got "%s"' % (name, value)) from error


def get_list(env, name):
    """Return a comma separated environment value as list of strings."""
    value = env.get(name, '').strip()

    if not value:
        return []

    return [item.strip() for item in value.split(',') if item.strip()]


def sanitize_key(value):
    """Make ``value`` safe to use inside a HomeAPI entry key.

    HomeAPI resolves an entry by the last segment of the request path,
    so keys must not contain slashes or whitespace.
    """
    allowed = []
    for char in str(value):
        if char.isalnum() or char in ('-', '_', '.'):
            allowed.append(char)
        else:
            allowed.append('_')

    return ''.join(allowed)


def setup_logging(level):
    """Configure logging for journald consumption."""
    logging.basicConfig(
        stream=sys.stdout,
        format='%(levelname)s %(message)s',
        level=getattr(logging, level, logging.INFO),
    )
