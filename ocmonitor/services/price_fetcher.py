"""Service for fetching remote model pricing from models.dev with caching."""

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


# Lock for thread-safe cache operations
_cache_lock = threading.Lock()


def fetch_models_dev_json(url: str, timeout: int) -> Optional[dict]:
    """Fetch models.dev API JSON with timeout and error handling.
    
    Args:
        url: The models.dev API URL
        timeout: Request timeout in seconds
        
    Returns:
        Parsed JSON dict or None if fetch fails
    """
    try:
        req = Request(
            url,
            headers={
                'User-Agent': 'ocmonitor/1.0',
                'Accept': 'application/json',
            }
        )
        with urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode('utf-8'))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as e:
        # Return None on any fetch/parse error - caller handles fallback
        return None
    except Exception:
        # Catch-all for any other network/IO errors
        return None


def load_cached_payload(cache_path: Path) -> Optional[dict]:
    """Load cached payload from disk if it exists.
    
    Args:
        cache_path: Path to cache file
        
    Returns:
        Cache envelope dict or None if cache doesn't exist or is invalid
    """
    try:
        if not cache_path.exists():
            return None
        
        with open(cache_path, 'r') as f:
            envelope = json.load(f)
        
        # Validate envelope structure
        if not isinstance(envelope, dict) or 'payload' not in envelope:
            return None
        
        return envelope
    except (json.JSONDecodeError, IOError, OSError):
        return None


def save_cached_payload_atomic(cache_path: Path, envelope: dict) -> bool:
    """Save cache envelope atomically to avoid corruption.
    
    Args:
        cache_path: Path to cache file
        envelope: Cache envelope to save
        
    Returns:
        True if save succeeded, False otherwise
    """
    temp_path = cache_path.with_suffix('.tmp')
    try:
        # Ensure parent directory exists
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Write to temp file in same directory
        with open(temp_path, 'w') as f:
            json.dump(envelope, f, indent=2)
        
        # Atomic rename
        os.replace(temp_path, cache_path)
        return True
    except (IOError, OSError):
        # Clean up temp file if it exists
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass
        return False


def acquire_lock(lock_path: Path, timeout: float = 30.0) -> bool:
    """Acquire file lock with timeout for concurrent access safety.
    
    Args:
        lock_path: Path to lock file
        timeout: Maximum time to wait for lock in seconds
        
    Returns:
        True if lock acquired, False if timeout
    """
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            # Use O_EXCL to create file exclusively (fails if exists)
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return True
        except FileExistsError:
            # Lock held by another process, wait and retry
            time.sleep(0.1)
            continue
        except OSError:
            # Other OS error, wait and retry
            time.sleep(0.1)
            continue
    return False


def release_lock(lock_path: Path) -> None:
    """Release file lock.
    
    Args:
        lock_path: Path to lock file
    """
    try:
        if lock_path.exists():
            lock_path.unlink()
    except OSError:
        pass


def get_remote_payload(
    url: str,
    timeout: int,
    cache_path: Path,
    cache_ttl_hours: int,
    allow_stale_on_error: bool = True,
) -> Optional[dict]:
    """Get models.dev payload with caching and stale fallback.
    
    This is the main entry point for fetching remote pricing data.
    It handles cache validation, network fetching, and stale fallback.
    
    Args:
        url: The models.dev API URL
        timeout: Request timeout in seconds
        cache_path: Path to cache file
        cache_ttl_hours: Cache TTL in hours
        allow_stale_on_error: Whether to use stale cache on fetch error
        
    Returns:
        Raw models.dev payload dict or None if unavailable
    """
    lock_path = cache_path.with_suffix('.lock')
    now = datetime.now(timezone.utc)

    # The lock file lives beside the cache, so its directory must exist first
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    with _cache_lock:
        # Try to load existing cache
        envelope = load_cached_payload(cache_path)
        
        if envelope:
            try:
                expires_at = datetime.fromisoformat(envelope.get('expires_at', ''))
                
                # Cache is fresh, use it
                if now < expires_at:
                    return envelope.get('payload')
            except (ValueError, TypeError):
                # Invalid expires_at, treat as expired
                pass
        
        # Cache is stale or missing, need to fetch
        lock_acquired = acquire_lock(lock_path)
        if not lock_acquired:
            # Could not acquire lock, use stale cache if available
            if envelope and allow_stale_on_error:
                return envelope.get('payload')
            return None
        
        try:
            # Double-check cache after acquiring lock (another process may have updated)
            envelope = load_cached_payload(cache_path)
            if envelope:
                try:
                    expires_at = datetime.fromisoformat(envelope.get('expires_at', ''))
                    if now < expires_at:
                        return envelope.get('payload')
                except (ValueError, TypeError):
                    pass
            
            # Fetch fresh data
            payload = fetch_models_dev_json(url, timeout)
            
            if payload is not None:
                # Create new envelope
                new_envelope = {
                    'schema_version': 1,
                    'source_url': url,
                    'fetched_at': now.isoformat(),
                    'expires_at': (now + timedelta(hours=cache_ttl_hours)).isoformat(),
                    'payload': payload,
                }
                
                # Save atomically
                save_cached_payload_atomic(cache_path, new_envelope)
                return payload
            
            # Fetch failed, use stale cache if allowed
            if envelope and allow_stale_on_error:
                return envelope.get('payload')
            
            return None
            
        finally:
            release_lock(lock_path)


# First-party providers win bare model-ID collisions (lower = preferred)
_BARE_KEY_PROVIDER_PRIORITY = {
    'openai': 0,
    'anthropic': 0,
    'google': 0,
    'deepseek': 0,
    'xai': 0,
    'mistral': 0,
    'moonshotai': 0,
    'zai': 0,
    'xiaomi': 0,
    'alibaba': 0,
    'minimax': 0,
}
_DEFAULT_BARE_KEY_PRIORITY = 10


def _cost_value(cost: dict, *keys: str) -> float:
    """Return the first non-null numeric cost among keys, else 0.0."""
    for key in keys:
        value = cost.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return 0.0


def _bare_key_rank(provider_id: str, pricing: Dict[str, Any]) -> tuple:
    """Rank a candidate for a bare model key: priced entries first, then provider."""
    is_free = not pricing['input'] and not pricing['output']
    return (is_free, _BARE_KEY_PROVIDER_PRIORITY.get(provider_id, _DEFAULT_BARE_KEY_PRIORITY))


def map_models_dev_to_local(payload: dict) -> Dict[str, Dict[str, Any]]:
    """Convert models.dev response to local pricing dict format.

    The models.dev API is a top-level dict keyed by provider ID, each with a
    ``models`` dict. Maps remote fields to local pricing schema:
    - cost.input -> input
    - cost.output -> output
    - cost.cache_write -> cacheWrite
    - cost.cache_read -> cacheRead
    - limit.context -> contextWindow
    - sessionQuota is not provided, defaults to 0.0

    Generates both bare model IDs and provider-prefixed IDs for compatibility.
    When several providers offer the same bare model ID, a priced entry from a
    first-party provider is preferred.

    Args:
        payload: Raw models.dev API response

    Returns:
        Dict mapping model names to pricing data dicts
    """
    result = {}
    bare_key_ranks: Dict[str, tuple] = {}

    if not isinstance(payload, dict):
        return result

    # Accept a legacy {"providers": {...}} wrapper as well as the real shape
    providers = payload.get('providers')
    if not isinstance(providers, dict):
        providers = payload

    for provider_id, provider_data in providers.items():
        if not isinstance(provider_data, dict):
            continue
        
        models = provider_data.get('models', {})
        if not isinstance(models, dict):
            continue
        
        for model_id, model_data in models.items():
            if not isinstance(model_data, dict):
                continue
            
            # Extract cost and limit data
            cost = model_data.get('cost', {}) or {}
            limit = model_data.get('limit', {}) or {}
            
            # Build pricing entry
            pricing = {
                'input': _cost_value(cost, 'input', 'prompt'),
                'output': _cost_value(cost, 'output', 'completion'),
                'cacheWrite': _cost_value(cost, 'cache_write', 'input_cache_write'),
                'cacheRead': _cost_value(cost, 'cache_read', 'input_cache_read'),
                'contextWindow': limit.get('context', 0) or 0,
                'sessionQuota': 0.0,  # Not provided by models.dev
            }

            # Generate keys
            provider_id_lower = provider_id.lower()
            model_id_lower = model_id.lower()

            # Bare model ID (lowercase)
            bare_key = model_id_lower
            rank = _bare_key_rank(provider_id_lower, pricing)
            if bare_key not in bare_key_ranks or rank < bare_key_ranks[bare_key]:
                result[bare_key] = pricing.copy()
                bare_key_ranks[bare_key] = rank
            
            # Provider-prefixed model ID (lowercase)
            prefixed_key = f"{provider_id_lower}/{model_id_lower}"
            if prefixed_key not in result:
                result[prefixed_key] = pricing.copy()
    
    return result
