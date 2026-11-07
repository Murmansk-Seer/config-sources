import hashlib
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
    def test_full_inventory_is_required_and_disabled_is_empty(self):
        assets = {"gems.bytes": b"unused"}
        from solaris.parse import import_parser_classes
        for parser in import_parser_classes():
            if parser.parsed_config_filename() in refresh.SOMUA_RESOURCES:
                assets[parser.source_config_filename()] = b"\x00"
        with patch.object(refresh, "_extract_text_asset", side_effect=lambda _, name: assets[name]), patch.object(refresh.GemsParser, "parse", return_value={"gems": {}}):
            result = refresh.parse_official_tables(b"bundle")
            self.assertEqual(len(result), 28)
            for name in refresh.SOMUA_RESOURCES:
                self.assertEqual(result[name], {"data": []})
            del assets["SomuaModelConfig.bytes"]
            with self.assertRaises(KeyError):
                refresh.parse_official_tables(b"bundle")

    def test_failed_parse_does_not_write_any_source(self):
        bundle = b"bundle"
        with tempfile.TemporaryDirectory() as directory:
            unity = Path(directory)
            version = unity / ".version"
            version.write_text("20261101000000")
            info = refresh.BundleInfo("pgame_configs_bytes", hashlib.md5(bundle).hexdigest(), len(bundle))
            with patch.dict(os.environ, {"SEERAPI_TOOLING_REVISION": "a" * 40}), patch.object(refresh, "UNITY_DIR", unity), patch.object(refresh, "VERSION_FILE", version), patch.object(refresh, "_download_bytes", side_effect=[b"20261105230633", b"manifest", bundle]), patch.object(refresh, "_find_config_bundle", return_value=info), patch.object(refresh, "parse_official_tables", side_effect=ValueError("truncated")):
                with self.assertRaisesRegex(ValueError, "truncated"):
                    refresh.main()
            self.assertEqual(version.read_text(), "20261101000000")
            self.assertEqual(list(unity.iterdir()), [version])

    def test_parser_revision_participates_in_skip_and_both_packages_are_pinned(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/sync-upstream.yml").read_text("utf-8")
        self.assertRegex(workflow, r'SEERAPI_TOOLING_REVISION: [0-9a-f]{40}')
        self.assertIn('"$parser_version" = "$SEERAPI_TOOLING_REVISION"', workflow)
        self.assertIn('"$somua_version" = "$local_version"', workflow)
        self.assertIn('@${SEERAPI_TOOLING_REVISION}#subdirectory=packages/seerapi-models', workflow)
        self.assertIn('@${SEERAPI_TOOLING_REVISION}#subdirectory=packages/solaris', workflow)
