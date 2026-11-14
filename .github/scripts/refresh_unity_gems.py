# SPDX-License-Identifier: MIT
"""Refresh a complete Unity snapshot from the verified official ConfigPackage."""

from __future__ import annotations

from dataclasses import dataclass
import asyncio
import io
import hashlib
import json
import os
import re
import struct
import time
from pathlib import Path
from urllib.parse import urljoin

import anyio
import httpx

from solaris.parse.snapshot import (
    parse_unity_snapshot,
    publish_unity_snapshot,
    required_unity_sources,
)

PACKAGE_NAME = "ConfigPackage"
CONFIG_BUNDLE_NAME = "pgame_configs_bytes"
GEMS_BYTES_NAME = "gems.bytes"
HTTP_TIMEOUT_SECONDS = 180
HTTP_RETRY_ATTEMPTS = int(os.environ.get("CONFIG_PACKAGE_HTTP_RETRY_ATTEMPTS", "3"))
HTTP_RETRY_BACKOFF_SECONDS = float(
    os.environ.get("CONFIG_PACKAGE_HTTP_RETRY_BACKOFF_SECONDS", "2")
)

ROOT = Path(__file__).resolve().parents[2]
UNITY_DIR = ROOT / "unity"
OUTPUT_FILE = UNITY_DIR / "gems.json"
VERSION_FILE = UNITY_DIR / ".version"
CONFIG_PACKAGE_BASE_URL = os.environ.get(
    "CONFIG_PACKAGE_BASE_URL",
    "https://newseer.61.com/Assets/StandaloneWindows64/ConfigPackage/",
)


@dataclass(frozen=True, slots=True)
class BundleInfo:
    name: str
    file_hash: str
    file_size: int


class ManifestReader:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._offset = 0

    def _read(self, size: int) -> bytes:
        end = self._offset + size
        if end > len(self._data):
            raise ValueError("Unexpected end of ConfigPackage manifest")
        value = self._data[self._offset : end]
        self._offset = end
        return value

    def read_u32(self) -> int:
        return struct.unpack_from("<I", self._read(4))[0]

    def read_i32(self) -> int:
        return struct.unpack_from("<i", self._read(4))[0]

    def read_i64(self) -> int:
        return struct.unpack_from("<q", self._read(8))[0]

    def read_u16(self) -> int:
        return struct.unpack_from("<H", self._read(2))[0]

    def read_i8(self) -> int:
        return struct.unpack_from("<b", self._read(1))[0]

    def read_bool(self) -> bool:
        return self._read(1) != b"\x00"

    def read_text(self) -> str:
        length = self.read_u16()
        return self._read(length).decode("utf-8", "surrogateescape")


def _download_bytes(url: str, *, expected: BundleInfo | None = None) -> bytes:
    if type(HTTP_RETRY_ATTEMPTS) is not int or not 1 <= HTTP_RETRY_ATTEMPTS <= 3:
        raise ValueError("HTTP_RETRY_ATTEMPTS must be between 1 and 3")

    async def download():
        limit = HTTP_TIMEOUT_SECONDS if expected is not None else 60
        timeout = httpx.Timeout(180 if expected is not None else 30, connect=15)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            for attempt in range(1, HTTP_RETRY_ATTEMPTS + 1):
                try:
                    with anyio.fail_after(limit):
                        response = await client.get(url)
                        response.raise_for_status()
                        data = response.content
                        if expected is not None and (
                            len(data) != expected.file_size
                            or hashlib.md5(data).hexdigest() != expected.file_hash
                        ):
                            raise OSError(
                                "Official ConfigPackage bundle digest/size mismatch"
                            )
                        return data
                except httpx.HTTPStatusError as error:
                    if (
                        error.response.status_code not in (408, 429)
                        and error.response.status_code < 500
                    ):
                        raise
                    if attempt == HTTP_RETRY_ATTEMPTS:
                        raise
                except (httpx.TransportError, TimeoutError, OSError):
                    if attempt == HTTP_RETRY_ATTEMPTS:
                        raise
                await asyncio.sleep(HTTP_RETRY_BACKOFF_SECONDS * attempt)
        raise RuntimeError("HTTP request failed without an exception")

    return asyncio.run(download())


def _find_config_bundle(manifest_data: bytes) -> BundleInfo:
    reader = ManifestReader(manifest_data)
    reader.read_u32()
    reader.read_text()
    reader.read_bool()
    reader.read_bool()
    reader.read_bool()
    reader.read_i32()
    reader.read_text()
    reader.read_text()

    asset_count = reader.read_i32()
    for _ in range(asset_count):
        reader.read_text()
        reader.read_i32()
        depend_count = reader.read_u16()
        for _ in range(depend_count):
            reader.read_i32()

    bundle_count = reader.read_i32()
    bundles: list[BundleInfo] = []
    for _ in range(bundle_count):
        name = reader.read_text()
        reader.read_u32()
        file_hash = reader.read_text()
        reader.read_text()
        file_size = reader.read_i64()
        reader.read_bool()
        reader.read_i8()
        reference_count = reader.read_u16()
        for _ in range(reference_count):
            reader.read_i32()
        bundles.append(BundleInfo(name=name, file_hash=file_hash, file_size=file_size))

    for bundle in bundles:
        if bundle.name == CONFIG_BUNDLE_NAME:
            return bundle

    if len(bundles) == 1:
        return bundles[0]

    raise ValueError(f"ConfigPackage bundle not found: {CONFIG_BUNDLE_NAME}")


def _extract_text_assets(bundle_data: bytes, wanted: set[str]) -> dict[str, bytes]:
    import UnityPy

    result = {}
    env = UnityPy.load(io.BytesIO(bundle_data))
    for obj in env.objects:
        if obj.type.name != "TextAsset":
            continue
        data = obj.read()
        name = str(data.m_Name)
        normalized_name = name if name.endswith(".bytes") else f"{name}.bytes"
        if normalized_name not in wanted:
            continue
        script = data.m_Script
        if normalized_name in result:
            raise ValueError(f"Duplicate official TextAsset: {normalized_name}")
        result[normalized_name] = (
            script
            if isinstance(script, bytes)
            else script.encode("utf-8", "surrogateescape")
        )

    missing = wanted - result.keys()
    if missing:
        raise ValueError(f"ConfigPackage text assets missing: {sorted(missing)}")
    return result


def main() -> None:
    base_url = CONFIG_PACKAGE_BASE_URL.rstrip("/") + "/"
    version_url = urljoin(base_url, f"PackageManifest_{PACKAGE_NAME}.version")
    version = _download_bytes(f"{version_url}?t={int(time.time())}").decode().strip()
    if len(version) != 14 or not version.isdecimal():
        raise ValueError("Invalid official ConfigPackage version")
    if VERSION_FILE.exists():
        current = VERSION_FILE.read_text(encoding="utf-8").strip()
        if current.isdecimal() and int(version) < int(current):
            print(f"Ignoring stale official ConfigPackage {version}; keeping {current}")
            return

    revision = os.environ.get("SEERAPI_TOOLING_REVISION", "")
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError("A full SEERAPI_TOOLING_REVISION commit SHA is required")
    manifest_url = urljoin(base_url, f"PackageManifest_{PACKAGE_NAME}_{version}.bytes")
    manifest_data = _download_bytes(manifest_url)
    bundle = _find_config_bundle(manifest_data)
    bundle_url = urljoin(base_url, bundle.file_hash)
    bundle_data = _download_bytes(bundle_url, expected=bundle)
    if (
        len(bundle_data) != bundle.file_size
        or hashlib.md5(bundle_data).hexdigest() != bundle.file_hash
    ):
        raise ValueError("Official ConfigPackage bundle digest/size mismatch")
    outputs = parse_official_tables(bundle_data)
    extras = _download_derived_files(version)
    actual_version = (
        _download_bytes(f"{version_url}?t={int(time.time())}").decode().strip()
    )
    if actual_version != version:
        raise ValueError(
            f"Unity snapshot source changed: actual={actual_version!r}, expected={version!r}"
        )
    publish_unity_snapshot(
        outputs,
        UNITY_DIR,
        version=version,
        parser_revision=revision,
        extra_files=extras,
    )
    print(
        f"Refreshed complete Unity snapshot from official ConfigPackage "
        f"{version}; tables={len(outputs)}"
    )


def parse_official_tables(bundle_data: bytes) -> dict:
    assets = _extract_text_assets(bundle_data, required_unity_sources())
    return parse_unity_snapshot(assets)


def _download_derived_files(version: str) -> dict[str, bytes]:
    base = "https://raw.githubusercontent.com/Murmansk-Seer/seer-unity-assets/main/newseer/"
    contracts = _download_bytes(base + "derived/partner_contracts.json")
    document = json.loads(contracts)
    actual = document.get("source", {}).get("config_package_version")
    if document.get("schema_version") != 1 or actual != version:
        raise ValueError(
            f"partner_contracts.json source gate: actual={actual!r}, expected={version!r}, schema={document.get('schema_version')!r}"
        )
    cloth = _download_bytes(base + "assets/art/ui/assets/item/cloth/prev/config.json")
    json.loads(cloth)
    return {"partner_contracts.json": contracts, "clothPos.json": cloth}


if __name__ == "__main__":
    import sys

    if sys.argv[1:] == ["--version-only"]:
        url = urljoin(
            CONFIG_PACKAGE_BASE_URL.rstrip("/") + "/",
            f"PackageManifest_{PACKAGE_NAME}.version",
        )
        print(_download_bytes(f"{url}?t={int(time.time())}").decode().strip())
    else:
        main()
