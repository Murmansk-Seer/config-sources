# SPDX-License-Identifier: MIT
"""Refresh Unity gem config from the official Seer ConfigPackage."""

from __future__ import annotations

from dataclasses import dataclass
import io
import hashlib
import json
import os
import re
import struct
import time
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from solaris.parse.parsers.gems import GemsParser
from solaris.parse import import_parser_classes
from seerapi_models.somua import SOMUA_RESOURCES

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


def _request(url: str) -> Request:
    return Request(
        url,
        headers={
            "User-Agent": "IronsBot-config-sources/1.0",
            "Accept": "*/*",
        },
    )


def _download_bytes(url: str) -> bytes:
    last_error: Exception | None = None
    for attempt in range(1, HTTP_RETRY_ATTEMPTS + 1):
        try:
            with urlopen(_request(url), timeout=HTTP_TIMEOUT_SECONDS) as response:
                return response.read()
        except (URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt >= HTTP_RETRY_ATTEMPTS:
                break
            time.sleep(HTTP_RETRY_BACKOFF_SECONDS * attempt)

    if last_error is not None:
        raise last_error
    raise RuntimeError("HTTP request failed without an exception")


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


def _extract_text_asset(bundle_data: bytes, wanted: str) -> bytes:
    import UnityPy

    env = UnityPy.load(io.BytesIO(bundle_data))
    for obj in env.objects:
        if obj.type.name != "TextAsset":
            continue
        data = obj.read()
        name = str(data.m_Name)
        normalized_name = name if name.endswith(".bytes") else f"{name}.bytes"
        if normalized_name != wanted:
            continue
        script = data.m_Script
        return (
            script
            if isinstance(script, bytes)
            else script.encode("utf-8", "surrogateescape")
        )

    raise ValueError(f"ConfigPackage text asset missing: {wanted}")


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
    bundle_data = _download_bytes(bundle_url)
    if len(bundle_data) != bundle.file_size or hashlib.md5(bundle_data).hexdigest() != bundle.file_hash:
        raise ValueError("Official ConfigPackage bundle digest/size mismatch")
    outputs = parse_official_tables(bundle_data)
    UNITY_DIR.mkdir(parents=True, exist_ok=True)
    # Validate all tables before writing; failed builds never reach commit/push.
    for filename, parsed in outputs.items():
        (UNITY_DIR / filename).write_text(
            json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    VERSION_FILE.write_text(version, encoding="utf-8")
    (UNITY_DIR / ".parser-revision").write_text(revision + "\n", encoding="utf-8")
    (UNITY_DIR / ".somua-version").write_text(version + "\n", encoding="utf-8")
    print(
        f"Refreshed {OUTPUT_FILE.relative_to(ROOT)} from official ConfigPackage "
        f"{version}; tables={len(outputs)}"
    )


def parse_official_tables(bundle_data: bytes) -> dict:
    parsers = {cls.parsed_config_filename(): cls for cls in import_parser_classes()}
    if len(SOMUA_RESOURCES) != 27:
        raise ValueError("Incomplete Somua parser installation")
    result = {"gems.json": GemsParser().parse(_extract_text_asset(bundle_data, GEMS_BYTES_NAME))}
    for filename in SOMUA_RESOURCES:
        parser = parsers[filename]()
        result[filename] = parser.parse(_extract_text_asset(bundle_data, parser.source_config_filename()))
    return result


if __name__ == "__main__":
    main()
