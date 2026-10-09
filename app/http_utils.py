"""Polite HTTP helpers: retries, pauses, and the User-Agent that SEC.gov requires."""
import logging
import time

import requests

from app.config import get_env

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "biotech-research-tool (personal use)"


def is_sec_url(url):
    """True if the URL points at sec.gov or data.sec.gov."""
    return "sec.gov" in url


def build_headers(url, headers=None):
    """Add a User-Agent. SEC requests must carry the contact from SEC_USER_AGENT."""
    all_headers = {"User-Agent": DEFAULT_USER_AGENT}
    if is_sec_url(url):
        sec_agent = get_env("SEC_USER_AGENT")
        if sec_agent is None:
            log.warning("SEC_USER_AGENT is not set in .env; SEC may block requests")
        else:
            all_headers["User-Agent"] = sec_agent
    if headers:
        all_headers.update(headers)
    return all_headers


def get_response(url, params=None, headers=None, retries=3, pause_seconds=0.2):
    """GET a URL with retries. Returns the requests Response, or None after all retries fail."""
    # SEC allows at most 10 requests/second, so never pause less than 0.15s for it.
    if is_sec_url(url) and pause_seconds < 0.15:
        pause_seconds = 0.15
    all_headers = build_headers(url, headers)

    for attempt in range(1, retries + 1):
        try:
            response = requests.get(url, params=params, headers=all_headers, timeout=30)
            response.raise_for_status()
            time.sleep(pause_seconds)
            return response
        except requests.RequestException as error:
            log.warning("GET %s failed (attempt %d/%d): %s", url, attempt, retries, error)
            time.sleep(pause_seconds * attempt * 5)
    log.error("Giving up on %s", url)
    return None


def get_json(url, params=None, headers=None, retries=3, pause_seconds=0.2):
    """GET a URL and return parsed JSON, or None on failure."""
    response = get_response(url, params, headers, retries, pause_seconds)
    if response is None:
        return None
    try:
        return response.json()
    except ValueError as error:
        log.error("Bad JSON from %s: %s", url, error)
        return None


def get_text(url, params=None, headers=None, retries=3, pause_seconds=0.2):
    """GET a URL and return the body as text, or None on failure."""
    response = get_response(url, params, headers, retries, pause_seconds)
    if response is None:
        return None
    return response.text


if __name__ == "__main__":
    print("Usage: from app.http_utils import get_json, get_text")
    print("Example: get_json('https://www.sec.gov/files/company_tickers.json')")
