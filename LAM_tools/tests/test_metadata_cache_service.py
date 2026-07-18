from __future__ import annotations

import json

from lam.config import CacheConfig
from lam.services.metadata_cache_service import MetadataCacheService


def test_cache_hit_versions_and_raw_response(tmp_path):
    cache = MetadataCacheService(tmp_path, CacheConfig())
    path = cache.put(
        "pubmed",
        "pmid",
        "12345678",
        {"status": "found", "records": []},
        ttl_seconds=60,
        http_status=200,
        raw_response=b"<xml />",
    )
    assert path and path.with_suffix(".raw").read_bytes() == b"<xml />"
    hit = cache.get("pubmed", "pmid", "12345678")
    assert hit["parsed_result"]["status"] == "found"
    assert cache.hits == 1


def test_cache_corruption_is_a_miss(tmp_path):
    cache = MetadataCacheService(tmp_path, CacheConfig())
    path = cache.put(
        "arxiv", "id", "2401.12345", {"status": "found"}, ttl_seconds=60
    )
    path.write_text("not json", encoding="utf-8")
    assert cache.get("arxiv", "id", "2401.12345") is None
    assert cache.corruptions == 1


def test_daily_quota_stops_at_limit(tmp_path):
    cache = MetadataCacheService(tmp_path, CacheConfig())
    assert cache.consume_daily_quota("unpaywall", 2) is True
    assert cache.consume_daily_quota("unpaywall", 2) is True
    assert cache.consume_daily_quota("unpaywall", 2) is False


def test_no_cache_write_does_not_persist_quota_counter(tmp_path):
    cache = MetadataCacheService(tmp_path, CacheConfig())
    assert cache.consume_daily_quota("unpaywall", 2, persist=False) is True
    assert not (tmp_path / "unpaywall" / "daily_counts.json").exists()
