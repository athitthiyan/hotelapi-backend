"""Tests for search cache Redis integration and in-memory fallback."""

from unittest.mock import MagicMock
import services.search_service as search_svc


class TestSearchCacheInMemoryFallback:
    """When Redis is unavailable, all cache operations use the in-memory dict."""

    def setup_method(self):
        search_svc._redis_state["client"] = None
        search_svc.clear_search_cache()

    def test_set_and_get_cached_search(self):
        search_svc.set_cached_search("key1", {"rooms": [1, 2, 3]})
        result = search_svc.get_cached_search("key1")
        assert result == {"rooms": [1, 2, 3]}

    def test_get_returns_none_for_missing_key(self):
        assert search_svc.get_cached_search("missing") is None

    def test_clear_removes_all_entries(self):
        search_svc.set_cached_search("a", {"x": 1})
        search_svc.set_cached_search("b", {"y": 2})
        search_svc.clear_search_cache()
        assert search_svc.get_cached_search("a") is None
        assert search_svc.get_cached_search("b") is None

    def test_expired_entry_returns_none(self):
        from datetime import timedelta
        search_svc.set_cached_search("expire_me", {"data": True})
        # Manually expire the entry
        with search_svc._cache_lock:
            ts, payload = search_svc._search_cache["expire_me"]
            search_svc._search_cache["expire_me"] = (
                search_svc.utc_now() - timedelta(seconds=1),
                payload,
            )
        assert search_svc.get_cached_search("expire_me") is None

    def test_make_search_cache_key_is_deterministic(self):
        k1 = search_svc.make_search_cache_key(city="NYC", price=100, sort="asc")
        k2 = search_svc.make_search_cache_key(sort="asc", price=100, city="NYC")
        assert k1 == k2


class TestSearchCacheRedisPath:
    """When a Redis client is present, operations delegate to Redis."""

    def setup_method(self):
        self.mock_redis = MagicMock()
        search_svc._redis_state["client"] = self.mock_redis

    def teardown_method(self):
        search_svc._redis_state["client"] = None

    def test_get_delegates_to_redis(self):
        import json
        self.mock_redis.get.return_value = json.dumps({"rooms": [1]})
        result = search_svc.get_cached_search("k")
        self.mock_redis.get.assert_called_once_with(f"{search_svc.SEARCH_CACHE_KEY_PREFIX}k")
        assert result == {"rooms": [1]}

    def test_get_returns_none_on_cache_miss(self):
        self.mock_redis.get.return_value = None
        assert search_svc.get_cached_search("miss") is None

    def test_set_delegates_to_redis(self):
        search_svc.set_cached_search("k", {"data": True})
        self.mock_redis.setex.assert_called_once()
        args = self.mock_redis.setex.call_args
        assert args[0][0] == f"{search_svc.SEARCH_CACHE_KEY_PREFIX}k"
        assert args[0][1] == search_svc.SEARCH_CACHE_TTL_SECONDS

    def test_clear_uses_scan_and_delete(self):
        self.mock_redis.scan.return_value = (0, ["search:a", "search:b"])
        search_svc.clear_search_cache()
        self.mock_redis.scan.assert_called()
        self.mock_redis.delete.assert_called_once_with("search:a", "search:b")

    def test_get_falls_back_to_memory_on_redis_error(self):
        self.mock_redis.get.side_effect = Exception("connection lost")
        # Should not raise, should fall through to in-memory (returns None since nothing stored)
        assert search_svc.get_cached_search("k") is None

    def test_set_falls_back_to_memory_on_redis_error(self):
        self.mock_redis.setex.side_effect = Exception("connection lost")
        search_svc.set_cached_search("fallback_key", {"x": 1})
        # Should have written to in-memory cache
        search_svc._redis_state["client"] = None  # switch to in-memory reads
        assert search_svc.get_cached_search("fallback_key") == {"x": 1}

    def test_clear_falls_back_to_memory_on_redis_error(self):
        self.mock_redis.scan.side_effect = Exception("connection lost")
        # Populate in-memory first
        search_svc._redis_state["client"] = None
        search_svc.set_cached_search("mem_key", {"y": 2})
        search_svc._redis_state["client"] = self.mock_redis
        # clear should fall back and clear in-memory
        search_svc.clear_search_cache()
        search_svc._redis_state["client"] = None
        assert search_svc.get_cached_search("mem_key") is None
