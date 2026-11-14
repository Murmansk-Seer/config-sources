import hashlib
import json
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "somua_refresh", Path(__file__).parents[1] / ".github/scripts/refresh_unity_gems.py"
)
refresh = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = refresh
spec.loader.exec_module(refresh)


class SomuaExportTests(unittest.TestCase):
    def test_stale_derived_contract_blocks_before_more_downloads_or_promotion(self):
        raw = json.dumps(
            {
                "schema_version": 1,
                "source": {"config_package_version": "20261110171953"},
            }
        ).encode()
        with (
            patch.object(refresh, "_download_bytes", return_value=raw) as download,
            patch.object(refresh, "publish_unity_snapshot") as publish,
        ):
            with self.assertRaisesRegex(ValueError, "actual=.*expected="):
                refresh._download_derived_files("20261113190033")
            self.assertEqual(download.call_count, 1)
            publish.assert_not_called()

    def test_full_inventory_is_required_and_disabled_is_empty(self):
        required = refresh.required_unity_sources()
        self.assertEqual(len(required), 294)
        assets = {name: b"raw" for name in required}
        expected = {"complete": True}
        with (
            patch.object(
                refresh, "_extract_text_assets", return_value=assets
            ) as extract,
            patch.object(
                refresh, "parse_unity_snapshot", return_value=expected
            ) as parse,
        ):
            self.assertEqual(refresh.parse_official_tables(b"bundle"), expected)
            extract.assert_called_once_with(b"bundle", required)
            parse.assert_called_once_with(assets)

    def test_failed_parse_does_not_write_any_source(self):
        bundle = b"bundle"
        with tempfile.TemporaryDirectory() as directory:
            unity = Path(directory)
            version = unity / ".version"
            version.write_text("20261101000000")
            info = refresh.BundleInfo(
                "pgame_configs_bytes", hashlib.md5(bundle).hexdigest(), len(bundle)
            )
            with (
                patch.dict(os.environ, {"SEERAPI_TOOLING_REVISION": "a" * 40}),
                patch.object(refresh, "UNITY_DIR", unity),
                patch.object(refresh, "VERSION_FILE", version),
                patch.object(
                    refresh,
                    "_download_bytes",
                    side_effect=[b"20261105230633", b"manifest", bundle],
                ),
                patch.object(refresh, "_find_config_bundle", return_value=info),
                patch.object(
                    refresh,
                    "parse_official_tables",
                    side_effect=ValueError("truncated"),
                ),
            ):
                with self.assertRaisesRegex(ValueError, "truncated"):
                    refresh.main()
            self.assertEqual(version.read_text(), "20261101000000")
            self.assertEqual(list(unity.iterdir()), [version])

    def test_parser_revision_participates_in_skip_and_both_packages_are_pinned(self):
        workflow = (
            Path(__file__).parents[1] / ".github/workflows/sync-upstream.yml"
        ).read_text("utf-8")
        self.assertRegex(workflow, r"SEERAPI_TOOLING_REVISION: [0-9a-f]{40}")
        self.assertIn('"$parser_version" = "$SEERAPI_TOOLING_REVISION"', workflow)
        self.assertIn('"$somua_version" = "$local_version"', workflow)
        self.assertIn(
            "@${SEERAPI_TOOLING_REVISION}#subdirectory=packages/seerapi-models",
            workflow,
        )
        self.assertIn(
            "@${SEERAPI_TOOLING_REVISION}#subdirectory=packages/solaris", workflow
        )
