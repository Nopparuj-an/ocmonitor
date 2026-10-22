"""Unit tests for the price_fetcher service."""

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from ocmonitor.services.price_fetcher import (
    acquire_lock,
    fetch_models_dev_json,
    get_remote_payload,
    load_cached_payload,
    map_models_dev_to_local,
    release_lock,
    save_cached_payload_atomic,
)


class TestFetchModelsDevJson:
    """Tests for fetch_models_dev_json function."""

    def test_fetch_models_dev_success_parses_json(self):
        """Test successful fetch returns parsed JSON."""
        mock_response = Mock()
        mock_response.read.return_value = b'{"providers": {"test": {"models": {}}}}'
        
        # urlopen is used as a context manager
        mock_cm = Mock()
        mock_cm.__enter__ = Mock(return_value=mock_response)
        mock_cm.__exit__ = Mock(return_value=False)
        
        with patch('ocmonitor.services.price_fetcher.urlopen', return_value=mock_cm):
            result = fetch_models_dev_json("https://models.dev/api.json", timeout=8)
        
        assert result == {"providers": {"test": {"models": {}}}}

    def test_fetch_models_dev_http_error_returns_none(self):
        """Test HTTP error returns None."""
        from urllib.error import HTTPError
        
        with patch(
            'ocmonitor.services.price_fetcher.urlopen',
            side_effect=HTTPError(None, 500, "Internal Server Error", None, None)
        ):
            result = fetch_models_dev_json("https://models.dev/api.json", timeout=8)
        
        assert result is None

    def test_fetch_models_dev_timeout_returns_none(self):
        """Test timeout returns None."""
        with patch(
            'ocmonitor.services.price_fetcher.urlopen',
            side_effect=TimeoutError()
        ):
            result = fetch_models_dev_json("https://models.dev/api.json", timeout=8)
        
        assert result is None

    def test_fetch_models_dev_json_decode_error_returns_none(self):
        """Test JSON decode error returns None."""
        mock_response = Mock()
        mock_response.read.return_value = b'invalid json'
        
        with patch('ocmonitor.services.price_fetcher.urlopen', return_value=mock_response):
            result = fetch_models_dev_json("https://models.dev/api.json", timeout=8)
        
        assert result is None


class TestLoadCachedPayload:
    """Tests for load_cached_payload function."""

    def test_cache_fresh_skips_network(self, tmp_path):
        """Test fresh cache is loaded from disk."""
        cache_path = tmp_path / "cache.json"
        envelope = {
            "schema_version": 1,
            "payload": {"test": "data"},
        }
        cache_path.write_text(json.dumps(envelope))
        
        result = load_cached_payload(cache_path)
        
        assert result == envelope

    def test_cache_missing_returns_none(self, tmp_path):
        """Test missing cache returns None."""
        cache_path = tmp_path / "nonexistent.json"
        
        result = load_cached_payload(cache_path)
        
        assert result is None

    def test_cache_invalid_json_returns_none(self, tmp_path):
        """Test invalid JSON cache returns None."""
        cache_path = tmp_path / "cache.json"
        cache_path.write_text("invalid json")
        
        result = load_cached_payload(cache_path)
        
        assert result is None

    def test_cache_no_payload_returns_none(self, tmp_path):
        """Test cache without payload field returns None."""
        cache_path = tmp_path / "cache.json"
        cache_path.write_text(json.dumps({"schema_version": 1}))
        
        result = load_cached_payload(cache_path)
        
        assert result is None


class TestSaveCachedPayloadAtomic:
    """Tests for save_cached_payload_atomic function."""

    def test_cache_write_is_atomic(self, tmp_path):
        """Test cache is written atomically."""
        cache_path = tmp_path / "cache.json"
        envelope = {
            "schema_version": 1,
            "payload": {"test": "data"},
        }
        
        result = save_cached_payload_atomic(cache_path, envelope)
        
        assert result is True
        assert cache_path.exists()
        assert not (tmp_path / "cache.tmp").exists()
        assert json.loads(cache_path.read_text()) == envelope

    def test_cache_write_creates_parent_dirs(self, tmp_path):
        """Test cache write creates parent directories."""
        cache_path = tmp_path / "subdir" / "nested" / "cache.json"
        envelope = {"schema_version": 1, "payload": {}}
        
        result = save_cached_payload_atomic(cache_path, envelope)
        
        assert result is True
        assert cache_path.exists()


class TestLockMechanism:
    """Tests for file locking mechanism."""

    def test_acquire_and_release_lock(self, tmp_path):
        """Test lock can be acquired and released."""
        lock_path = tmp_path / "cache.lock"
        
        acquired = acquire_lock(lock_path, timeout=1.0)
        
        assert acquired is True
        assert lock_path.exists()
        
        release_lock(lock_path)
        
        assert not lock_path.exists()

    def test_lock_file_prevents_concurrent_access(self, tmp_path):
        """Test lock file prevents concurrent access."""
        lock_path = tmp_path / "cache.lock"
        
        # Acquire lock in main thread
        acquired = acquire_lock(lock_path, timeout=0.5)
        assert acquired is True
        
        # Try to acquire in another thread - should fail
        results = []
        
        def try_acquire():
            results.append(acquire_lock(lock_path, timeout=0.1))
        
        thread = threading.Thread(target=try_acquire)
        thread.start()
        thread.join()
        
        assert results[0] is False
        
        release_lock(lock_path)


class TestGetRemotePayload:
    """Tests for get_remote_payload function."""

    def test_cache_fresh_skips_network(self, tmp_path):
        """Test fresh cache is used without network request."""
        cache_path = tmp_path / "cache.json"
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        envelope = {
            "schema_version": 1,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": future.isoformat(),
            "payload": {"test": "cached"},
        }
        save_cached_payload_atomic(cache_path, envelope)
        
        with patch('ocmonitor.services.price_fetcher.fetch_models_dev_json') as mock_fetch:
            result = get_remote_payload(
                url="https://models.dev/api.json",
                timeout=8,
                cache_path=cache_path,
                cache_ttl_hours=24,
            )
        
        mock_fetch.assert_not_called()
        assert result == {"test": "cached"}

    def test_cache_stale_refreshes(self, tmp_path):
        """Test stale cache triggers network refresh."""
        cache_path = tmp_path / "cache.json"
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        old_envelope = {
            "schema_version": 1,
            "fetched_at": (past - timedelta(hours=25)).isoformat(),
            "expires_at": past.isoformat(),
            "payload": {"test": "old"},
        }
        save_cached_payload_atomic(cache_path, old_envelope)
        
        new_payload = {"test": "new"}
        
        with patch(
            'ocmonitor.services.price_fetcher.fetch_models_dev_json',
            return_value=new_payload
        ) as mock_fetch:
            result = get_remote_payload(
                url="https://models.dev/api.json",
                timeout=8,
                cache_path=cache_path,
                cache_ttl_hours=24,
            )
        
        mock_fetch.assert_called_once()
        assert result == new_payload
        
        # Verify cache was updated
        updated_envelope = load_cached_payload(cache_path)
        assert updated_envelope["payload"] == new_payload

    def test_cache_stale_fetch_failure_uses_stale_when_allowed(self, tmp_path):
        """Test stale cache is used when fetch fails and allowed."""
        cache_path = tmp_path / "cache.json"
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        old_envelope = {
            "schema_version": 1,
            "fetched_at": (past - timedelta(hours=25)).isoformat(),
            "expires_at": past.isoformat(),
            "payload": {"test": "stale"},
        }
        save_cached_payload_atomic(cache_path, old_envelope)
        
        with patch(
            'ocmonitor.services.price_fetcher.fetch_models_dev_json',
            return_value=None
        ):
            result = get_remote_payload(
                url="https://models.dev/api.json",
                timeout=8,
                cache_path=cache_path,
                cache_ttl_hours=24,
                allow_stale_on_error=True,
            )
        
        assert result == {"test": "stale"}

    def test_cache_stale_fetch_failure_returns_none_when_not_allowed(self, tmp_path):
        """Test None returned when fetch fails and stale not allowed."""
        cache_path = tmp_path / "cache.json"
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        old_envelope = {
            "schema_version": 1,
            "expires_at": past.isoformat(),
            "payload": {"test": "stale"},
        }
        save_cached_payload_atomic(cache_path, old_envelope)
        
        with patch(
            'ocmonitor.services.price_fetcher.fetch_models_dev_json',
            return_value=None
        ):
            result = get_remote_payload(
                url="https://models.dev/api.json",
                timeout=8,
                cache_path=cache_path,
                cache_ttl_hours=24,
                allow_stale_on_error=False,
            )
        
        assert result is None

    def test_cache_missing_and_fetch_failure_returns_none(self, tmp_path):
        """Test None returned when no cache and fetch fails."""
        cache_path = tmp_path / "cache.json"
        
        with patch(
            'ocmonitor.services.price_fetcher.fetch_models_dev_json',
            return_value=None
        ):
            result = get_remote_payload(
                url="https://models.dev/api.json",
                timeout=8,
                cache_path=cache_path,
                cache_ttl_hours=24,
            )
        
        assert result is None


class TestMapModelsDevToLocal:
    """Tests for map_models_dev_to_local function."""

    def test_map_models_dev_fields_to_local_schema(self):
        """Test correct field mapping from remote to local format."""
        payload = {
            "providers": {
                "openai": {
                    "models": {
                        "gpt-4": {
                            "cost": {
                                "prompt": 3.00,
                                "completion": 15.00,
                                "input_cache_write": 3.75,
                                "input_cache_read": 0.30,
                            },
                            "limit": {
                                "context": 128000,
                            }
                        }
                    }
                }
            }
        }
        
        result = map_models_dev_to_local(payload)
        
        assert "gpt-4" in result
        assert "openai/gpt-4" in result
        
        for key in ["gpt-4", "openai/gpt-4"]:
            assert result[key]["input"] == 3.00
            assert result[key]["output"] == 15.00
            assert result[key]["cacheWrite"] == 3.75
            assert result[key]["cacheRead"] == 0.30
            assert result[key]["contextWindow"] == 128000
            assert result[key]["sessionQuota"] == 0.0

    def test_map_missing_cost_fields_defaults_to_zero(self):
        """Test missing cost fields default to zero."""
        payload = {
            "providers": {
                "test": {
                    "models": {
                        "minimal": {
                            "cost": {},
                            "limit": {}
                        }
                    }
                }
            }
        }
        
        result = map_models_dev_to_local(payload)
        
        assert result["minimal"]["input"] == 0.0
        assert result["minimal"]["output"] == 0.0
        assert result["minimal"]["cacheWrite"] == 0.0
        assert result["minimal"]["cacheRead"] == 0.0
        assert result["minimal"]["contextWindow"] == 0

    def test_map_creates_bare_and_provider_prefixed_keys(self):
        """Test both bare and provider-prefixed keys are created."""
        payload = {
            "providers": {
                "anthropic": {
                    "models": {
                        "claude-3-opus": {
                            "cost": {"prompt": 5.00, "completion": 25.00},
                            "limit": {"context": 200000}
                        }
                    }
                }
            }
        }
        
        result = map_models_dev_to_local(payload)
        
        # Both keys should exist
        assert "claude-3-opus" in result
        assert "anthropic/claude-3-opus" in result
        
        # Both should have same data
        assert result["claude-3-opus"] == result["anthropic/claude-3-opus"]

    def test_map_handles_empty_payload(self):
        """Test empty payload returns empty dict."""
        result = map_models_dev_to_local({})
        assert result == {}

    def test_map_handles_invalid_payload(self):
        """Test invalid payload returns empty dict."""
        result = map_models_dev_to_local("not a dict")
        assert result == {}

    def test_map_handles_missing_providers(self):
        """Test payload without providers returns empty dict."""
        result = map_models_dev_to_local({"other_key": "value"})
        assert result == {}

    def test_map_preserves_existing_keys_from_different_providers(self):
        """Test same model from different providers don't overwrite each other."""
        payload = {
            "providers": {
                "provider1": {
                    "models": {
                        "shared-model": {
                            "cost": {"prompt": 1.00},
                            "limit": {"context": 1000}
                        }
                    }
                },
                "provider2": {
                    "models": {
                        "shared-model": {
                            "cost": {"prompt": 2.00},
                            "limit": {"context": 2000}
                        }
                    }
                }
            }
        }
        
        result = map_models_dev_to_local(payload)
        
        # Bare key should keep first provider's data (provider1)
        assert result["shared-model"]["input"] == 1.00
        assert result["shared-model"]["contextWindow"] == 1000
        
        # Prefixed keys should have their respective provider's data
        assert result["provider1/shared-model"]["input"] == 1.00
        assert result["provider2/shared-model"]["input"] == 2.00

    def test_map_lowercases_keys(self):
        """Test model and provider IDs are lowercased."""
        payload = {
            "providers": {
                "OpenAI": {
                    "models": {
                        "GPT-4-Turbo": {
                            "cost": {"prompt": 10.00},
                            "limit": {"context": 10000}
                        }
                    }
                }
            }
        }
        
        result = map_models_dev_to_local(payload)
        
        assert "gpt-4-turbo" in result
        assert "openai/gpt-4-turbo" in result

    def test_map_real_models_dev_shape(self):
        """Test the real models.dev shape: providers at top level, cost.input/output/cache_*."""
        payload = {
            "anthropic": {
                "id": "anthropic",
                "models": {
                    "claude-sonnet-4-5": {
                        "id": "claude-sonnet-4-5",
                        "cost": {"input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75},
                        "limit": {"context": 200000, "output": 64000},
                    }
                },
            }
        }

        result = map_models_dev_to_local(payload)

        assert result["anthropic/claude-sonnet-4-5"] == {
            "input": 3.0,
            "output": 15.0,
            "cacheWrite": 3.75,
            "cacheRead": 0.3,
            "contextWindow": 200000,
            "sessionQuota": 0.0,
        }
        assert result["claude-sonnet-4-5"]["input"] == 3.0

    def test_map_bare_key_prefers_priced_first_party_provider(self):
        """Test bare key collisions prefer priced entries, then first-party providers."""
        payload = {
            "reseller": {"models": {"gpt-5": {"cost": {"input": 9, "output": 9}}}},
            "free-host": {"models": {"gpt-5": {"cost": {"input": 0, "output": 0}}}},
            "openai": {"models": {"gpt-5": {"cost": {"input": 1.25, "output": 10}}}},
        }

        result = map_models_dev_to_local(payload)

        assert result["gpt-5"]["input"] == 1.25
        assert result["reseller/gpt-5"]["input"] == 9.0
        assert result["free-host/gpt-5"]["input"] == 0.0
