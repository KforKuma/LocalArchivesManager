from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..config import CacheConfig


class MetadataCacheService:
    def __init__(self, root: Path, config: CacheConfig):
        self.root = root
        self.config = config
        self.hits = 0
        self.misses = 0
        self.corruptions = 0

    def get(
        self,
        provider: str,
        query_type: str,
        normalized_query: str,
    ) -> dict[str, Any] | None:
        if not self.config.enabled:
            self.misses += 1
            return None
        path = self._entry_path(provider, query_type, normalized_query)
        if not path.is_file():
            self.misses += 1
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            versions = payload["versions"]
            if versions != self._versions():
                self.misses += 1
                return None
            expires = datetime.fromisoformat(payload["expires_at"])
            if expires <= datetime.now(timezone.utc):
                self.misses += 1
                return None
            parsed = payload["parsed_result"]
            if not isinstance(parsed, dict):
                raise ValueError("parsed_result is not an object")
        except Exception:
            self.corruptions += 1
            self.misses += 1
            return None
        self.hits += 1
        return payload

    def put(
        self,
        provider: str,
        query_type: str,
        normalized_query: str,
        parsed_result: dict[str, Any],
        *,
        ttl_seconds: int,
        http_status: int | None = None,
        raw_response: bytes | None = None,
    ) -> Path | None:
        if not self.config.enabled:
            return None
        path = self._entry_path(provider, query_type, normalized_query)
        path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        raw_path = None
        if raw_response is not None:
            raw_path = path.with_suffix(".raw")
            self._atomic_bytes(raw_path, raw_response)
        payload = {
            "provider": provider,
            "query": {"type": query_type, "normalized": normalized_query},
            "fetched_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=max(1, ttl_seconds))).isoformat(),
            "http_status": http_status,
            "raw_response_path": raw_path.name if raw_path else None,
            "parsed_result": parsed_result,
            "versions": self._versions(),
            "cache_schema_version": self.config.cache_schema_version,
            "parser_version": self.config.parser_version,
            "provider_schema_version": self.config.provider_schema_version,
        }
        self._atomic_bytes(
            path,
            (json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n").encode("utf-8"),
        )
        return path

    def consume_daily_quota(self, provider: str, limit: int) -> bool:
        path = self.root / provider / "daily_counts.json"
        today = datetime.now(timezone.utc).date().isoformat()
        payload: dict[str, int] = {}
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    payload = {str(key): int(value) for key, value in raw.items()}
            except Exception:
                payload = {}
        count = payload.get(today, 0)
        if count >= limit:
            return False
        payload = {today: count + 1}
        path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_bytes(
            path, (json.dumps(payload, indent=2) + "\n").encode("utf-8")
        )
        return True

    def _entry_path(self, provider: str, query_type: str, query: str) -> Path:
        material = "|".join(
            (provider, query_type, query, self.config.provider_schema_version)
        )
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
        return self.root / provider / f"{digest}.json"

    def _versions(self) -> dict[str, str]:
        return {
            "cache_schema_version": self.config.cache_schema_version,
            "parser_version": self.config.parser_version,
            "provider_schema_version": self.config.provider_schema_version,
        }

    @staticmethod
    def _atomic_bytes(path: Path, content: bytes) -> None:
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_bytes(content)
        os.replace(temporary, path)
