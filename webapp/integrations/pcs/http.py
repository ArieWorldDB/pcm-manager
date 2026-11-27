"""Shared throttling and temporary cooldown for all PCS HTTP requests."""
from __future__ import annotations

import random
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

from curl_cffi import requests as curl_requests
from flask import has_request_context
from sqlalchemy import text

from webapp.extensions import db

# Fixed when this cooldown was requested. Keeping the deadline in code makes it
# consistent across web/worker processes and prevents restarts extending it.
PCS_FETCH_PAUSED_UNTIL = datetime(2026, 11, 27, 23, 29, 0, tzinfo=timezone.utc)
PCS_REQUEST_LOCK_KEY = 792211105
PCS_WEB_PRIORITY_LOCK_KEY = 792211106
PCS_REQUEST_DELAY_MIN_SECONDS = 2.0
PCS_REQUEST_DELAY_MAX_SECONDS = 5.0
PCS_WORKER_PRIORITY_RETRY_MIN_SECONDS = 0.05
PCS_WORKER_PRIORITY_RETRY_MAX_SECONDS = 0.15
PCS_ALLOWED_HOSTS = {'procyclingstats.com', 'www.procyclingstats.com'}
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class PcsFetchPaused(RuntimeError):
    """Raised instead of sending a PCS request during the cooldown."""


def pcs_fetch_pause_remaining(now=None):
    """Return the active pause deadline, or ``None`` after it expires."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return PCS_FETCH_PAUSED_UNTIL if current < PCS_FETCH_PAUSED_UNTIL else None


def ensure_pcs_fetch_allowed():
    pause_until = pcs_fetch_pause_remaining()
    if pause_until is not None:
        raise PcsFetchPaused(
            'PCS requests are paused until '
            f'{pause_until.isoformat(timespec="minutes")}.'
        )


def _validate_pcs_url(url):
    parsed = urlsplit(str(url or ''))
    if parsed.scheme != 'https' or parsed.hostname not in PCS_ALLOWED_HOSTS:
        raise ValueError('PCS requests must use an approved HTTPS ProCyclingStats URL.')


def _request_once(url, kwargs):
    """Serialize a request, favor web callers, and leave a shared randomized gap."""
    ensure_pcs_fetch_allowed()
    is_web_request = has_request_context()
    with db.engines['main'].connect() as connection:
        acquired_request_lock = False
        acquired_priority_lock = False
        try:
            if is_web_request:
                # Claim priority before waiting for the shared request slot.
                connection.execute(
                    text('SELECT pg_advisory_lock(:key)'),
                    {'key': PCS_WEB_PRIORITY_LOCK_KEY},
                )
                connection.commit()
                acquired_priority_lock = True
                connection.execute(
                    text('SELECT pg_advisory_lock(:key)'),
                    {'key': PCS_REQUEST_LOCK_KEY},
                )
                connection.commit()
                acquired_request_lock = True
            else:
                # Workers only take the request slot if no web caller owns or
                # is waiting for the priority lock. Release and retry otherwise.
                while not acquired_priority_lock:
                    connection.execute(
                        text('SELECT pg_advisory_lock(:key)'),
                        {'key': PCS_REQUEST_LOCK_KEY},
                    )
                    connection.commit()
                    acquired_request_lock = True
                    acquired_priority_lock = connection.execute(
                        text('SELECT pg_try_advisory_lock(:key)'),
                        {'key': PCS_WEB_PRIORITY_LOCK_KEY},
                    ).scalar_one()
                    connection.commit()
                    if not acquired_priority_lock:
                        connection.execute(
                            text('SELECT pg_advisory_unlock(:key)'),
                            {'key': PCS_REQUEST_LOCK_KEY},
                        )
                        connection.commit()
                        acquired_request_lock = False
                        time.sleep(random.uniform(
                            PCS_WORKER_PRIORITY_RETRY_MIN_SECONDS,
                            PCS_WORKER_PRIORITY_RETRY_MAX_SECONDS,
                        ))

            # A request may have waited for the previous holder while the
            # cooldown deadline elapsed, so check again before going outbound.
            ensure_pcs_fetch_allowed()
            response = curl_requests.get(url, allow_redirects=False, **kwargs)
            time.sleep(random.uniform(
                PCS_REQUEST_DELAY_MIN_SECONDS, PCS_REQUEST_DELAY_MAX_SECONDS,
            ))
            return response
        finally:
            if acquired_request_lock or acquired_priority_lock:
                try:
                    if acquired_request_lock:
                        connection.execute(
                            text('SELECT pg_advisory_unlock(:key)'),
                            {'key': PCS_REQUEST_LOCK_KEY},
                        )
                        connection.commit()
                    if acquired_priority_lock:
                        connection.execute(
                            text('SELECT pg_advisory_unlock(:key)'),
                            {'key': PCS_WEB_PRIORITY_LOCK_KEY},
                        )
                        connection.commit()
                except Exception:
                    connection.invalidate()
                    raise


def get(url, **kwargs):
    """GET a PCS page, spacing and validating every hop in a redirect chain."""
    kwargs.pop('allow_redirects', None)
    request_url = str(url)
    for _ in range(10):
        _validate_pcs_url(request_url)
        response = _request_once(request_url, kwargs)
        if response.status_code not in _REDIRECT_STATUSES:
            return response
        location = response.headers.get('Location')
        if not location:
            return response
        request_url = urljoin(request_url, location)
    raise RuntimeError('PCS exceeded the redirect limit.')
