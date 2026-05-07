"""
HTTP utilities with retry, timeout, and silent-error support.
Shared by all collectors.
"""
import time
import logging
import requests
from typing import Optional, Dict, Any

from config import CONFIG

logger = logging.getLogger(__name__)


def http_get(url: str, params: Optional[Dict] = None,
             headers: Optional[Dict] = None,
             silent_codes: tuple = (),
             timeout: Optional[int] = None) -> Optional[Any]:
    """
    GET with retry on transient errors.

    silent_codes: tuple of HTTP status codes to silently treat as "no data"
                  (e.g. 400, 404 for endpoints that may not have data for
                  some symbols).
    """
    timeout = timeout or CONFIG.REQUEST_TIMEOUT
    for attempt in range(CONFIG.REQUEST_RETRIES + 1):
        try:
            r = requests.get(url, params=params, headers=headers,
                             timeout=timeout)
            if r.status_code == 200:
                try:
                    return r.json()
                except ValueError:
                    return r.text
            if r.status_code == 429:
                wait = 2 ** attempt
                logger.warning(f"Rate limited on {url}, sleeping {wait}s")
                time.sleep(wait)
                continue
            if r.status_code in silent_codes:
                return None
            logger.warning(f"HTTP {r.status_code} from {url}: {r.text[:200]}")
            return None
        except requests.exceptions.RequestException as e:
            logger.warning(f"Request failed ({url}, attempt {attempt}): {e}")
            time.sleep(1)
    return None
