import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('gems_refresh', Path(__file__).parents[1] / '.github/scripts/refresh_unity_gems.py')
refresh = importlib.util.module_from_spec(spec)
import sys
sys.modules[spec.name] = refresh
spec.loader.exec_module(refresh)


class Versions(unittest.TestCase):
    def test_stale_official_version_does_not_fetch_or_write_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            version = Path(directory) / '.version'
            version.write_text('20260930183512', encoding='utf-8')
            with patch.object(refresh, 'VERSION_FILE', version), patch.object(refresh, '_download_bytes', return_value=b'20260924175611') as download:
                refresh.main()
                self.assertEqual(download.call_count, 1)
                self.assertEqual(version.read_text(), '20260930183512')

    def test_invalid_version_blocks_refresh(self):
        with patch.object(refresh, '_download_bytes', return_value=b'<html>error</html>'):
            with self.assertRaisesRegex(ValueError, 'Invalid official'):
                refresh.main()


if __name__ == '__main__':
    unittest.main()
