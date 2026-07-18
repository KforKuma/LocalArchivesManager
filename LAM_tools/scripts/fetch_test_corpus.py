from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlparse


MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def selected_cases(manifest: dict) -> list[dict]:
    cases = manifest.get("cases")
    if not isinstance(cases, list):
        raise ValueError("manifest.cases must be an array")
    return [item for item in cases if item.get("redistribution") == "download_only"]


def fetch_case(case: dict, output: Path, *, timeout: float) -> str:
    case_id = str(case.get("case_id") or "<unknown>")
    filename = str(case.get("filename") or "").strip()
    expected = str(case.get("sha256") or "").strip().casefold()
    url = str(case.get("download_url") or "").strip()
    if not filename or Path(filename).name != filename:
        raise ValueError(f"{case_id}: filename must be a plain basename")
    if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
        raise ValueError(f"{case_id}: download_only requires a valid SHA-256")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{case_id}: only direct HTTP(S) download URLs are allowed")
    target = output / filename
    if target.exists():
        actual = sha256(target)
        if actual != expected:
            raise ValueError(
                f"{case_id}: existing file hash mismatch; refusing to overwrite {target}"
            )
        return "already_present_verified"
    temporary = target.with_suffix(target.suffix + ".part")
    if temporary.exists():
        raise ValueError(f"{case_id}: partial file exists; remove it before retrying: {temporary}")
    request = urllib.request.Request(url, headers={"User-Agent": "LAM-test-corpus/0.6.1"})
    digest = hashlib.sha256()
    total = 0
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response, temporary.open("xb") as handle:
            final_url = response.geturl()
            if urlparse(final_url).scheme not in {"http", "https"}:
                raise ValueError(f"{case_id}: redirect left HTTP(S): {final_url}")
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise ValueError(f"{case_id}: download exceeds 100 MiB safety limit")
                digest.update(chunk)
                handle.write(chunk)
        actual = digest.hexdigest()
        if actual != expected:
            raise ValueError(f"{case_id}: SHA-256 mismatch; expected {expected}, got {actual}")
        os.replace(temporary, target)
        return "downloaded_verified"
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Fetch explicitly download-only LAM test corpus files")
    parser.add_argument("--manifest", type=Path, default=root / "examples" / "test_corpus" / "manifest.json")
    parser.add_argument("--output", type=Path, default=root / "examples" / "test_corpus" / "downloaded")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        cases = selected_cases(manifest)
    except Exception as exc:
        print(f"manifest error: {exc}", file=sys.stderr)
        return 2
    args.output.mkdir(parents=True, exist_ok=True)
    failures = 0
    if not cases:
        print("no redistribution=download_only cases are configured")
    for case in cases:
        try:
            status = fetch_case(case, args.output, timeout=args.timeout)
            print(f"{case.get('case_id')}: {status}")
        except Exception as exc:
            failures += 1
            print(f"{case.get('case_id', '<unknown>')}: failed: {exc}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
