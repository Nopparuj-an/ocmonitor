"""Service for fetching exchange rates from frankfurter.dev with caching."""

from decimal import Decimal
from pathlib import Path
from typing import Optional

from .price_fetcher import (
    acquire_lock,
    load_cached_payload,
    release_lock,
    save_cached_payload_atomic,
)


def fetch_exchange_rates(url: str, timeout: int) -> Optional[dict]:
    """Fetch exchange rates from frankfurter.dev API.

    Args:
        url: The frankfurter.dev API URL
        timeout: Request timeout in seconds

    Returns:
        Parsed JSON dict or None if fetch fails
    """
    import json
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    try:
        req = Request(
            url,
            headers={
                "User-Agent": "ocmonitor/1.0",
                "Accept": "application/json",
            },
        )
        with urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
        # Return None on any fetch/parse error - caller handles fallback
        return None
    except Exception:
        # Catch-all for any other network/IO errors
        return None


def get_exchange_rate(currency_config) -> Optional[Decimal]:
    """Get exchange rate for configured currency.

    This is the main entry point for fetching exchange rates.
    It handles cache validation, network fetching, and stale fallback.

    Args:
        currency_config: CurrencyConfig instance with remote_rates settings

    Returns:
        Exchange rate as Decimal, or None if unavailable
    """
    from datetime import datetime, timedelta, timezone

    cache_path = Path(currency_config.remote_rates_cache_path)
    lock_path = cache_path.with_suffix(".lock")
    now = datetime.now(timezone.utc)

    # The lock file lives beside the cache, so its directory must exist first
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    # Normalize currency code to uppercase for lookup
    # (frankfurter.dev returns uppercase currency codes)
    currency_code = currency_config.code.upper()

    # Try to load existing cache
    envelope = load_cached_payload(cache_path)

    if envelope:
        try:
            expires_at = datetime.fromisoformat(envelope.get("expires_at", ""))

            # Cache is fresh, use it
            if now < expires_at:
                payload = envelope.get("payload", {})
                rates = payload.get("rates", {})
                if currency_code in rates:
                    return Decimal(str(rates[currency_code]))
        except (ValueError, TypeError):
            # Invalid expires_at, treat as expired
            pass

    # Cache is stale or missing, need to fetch
    lock_acquired = acquire_lock(lock_path)
    if not lock_acquired:
        # Could not acquire lock, use stale cache if available
        if envelope and currency_config.allow_stale_rates_on_error:
            payload = envelope.get("payload", {})
            rates = payload.get("rates", {})
            if currency_code in rates:
                return Decimal(str(rates[currency_code]))
        return None

    try:
        # Double-check cache after acquiring lock (another process may have updated)
        envelope = load_cached_payload(cache_path)
        if envelope:
            try:
                expires_at = datetime.fromisoformat(envelope.get("expires_at", ""))
                if now < expires_at:
                    payload = envelope.get("payload", {})
                    rates = payload.get("rates", {})
                    if currency_code in rates:
                        return Decimal(str(rates[currency_code]))
            except (ValueError, TypeError):
                pass

        # Fetch fresh data
        payload = fetch_exchange_rates(
            currency_config.remote_rates_url,
            currency_config.remote_rates_timeout_seconds,
        )

        if payload is not None:
            # Create new envelope
            new_envelope = {
                "schema_version": 1,
                "source_url": currency_config.remote_rates_url,
                "fetched_at": now.isoformat(),
                "expires_at": (
                    now + timedelta(hours=currency_config.remote_rates_cache_ttl_hours)
                ).isoformat(),
                "payload": payload,
            }

            # Save atomically
            save_cached_payload_atomic(cache_path, new_envelope)

            # Extract rate for requested currency
            rates = payload.get("rates", {})
            if currency_code in rates:
                return Decimal(str(rates[currency_code]))

        # Fetch failed, use stale cache if allowed
        if envelope and currency_config.allow_stale_rates_on_error:
            payload = envelope.get("payload", {})
            rates = payload.get("rates", {})
            if currency_code in rates:
                return Decimal(str(rates[currency_code]))

        return None

    finally:
        release_lock(lock_path)
