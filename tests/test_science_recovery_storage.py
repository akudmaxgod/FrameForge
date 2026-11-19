from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from imagegen.script_analysis.analyzer import Analyzer
from imagegen.script_analysis.cache import DiskCache
from imagegen.script_analysis.errors import AnalysisError


class RecoveryStorageTests(unittest.TestCase):
    def test_legacy_cache_remains_readable_with_write_only_to_profile_data(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            legacy=DiskCache(root/'app/_internal/.script-analysis/cache')
            legacy.save('old',{'marker':'old'})
            cache=DiskCache.for_data(root/'profile')
            cache.legacy=legacy.directory
            self.assertEqual(cache.load('old')['marker'],'old')
            cache.ensure_writable()
            cache.save('new',{'marker':'new'})
            self.assertEqual(cache.load('new')['marker'],'new')
            self.assertFalse((legacy.directory/'new.json').exists())

    def test_unwritable_profile_stops_before_paid_request(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            blocked=root/'unavailable'
            blocked.write_text('file',encoding='utf-8')
            cache=DiskCache(blocked/'cache')
            with patch('imagegen.script_analysis.analyzer.JsonClient',side_effect=AssertionError('Paid client forbidden')):
                with self.assertRaisesRegex(AnalysisError,'недоступна для записи'):
                    Analyzer(cache=cache).analyze('Тестовый сценарий.')

    def test_resume_missing_base_cache_never_contacts_deepseek(self):
        with TemporaryDirectory() as directory:
            with patch('imagegen.script_analysis.analyzer.JsonClient',side_effect=AssertionError('Paid client forbidden')):
                with self.assertRaisesRegex(AnalysisError,'Не найден базовый разбор'):
                    Analyzer(cache=DiskCache(Path(directory))).analyze('Тестовый сценарий.',cache_only=True)

    def test_base_passes_store_failure_details_in_the_profile(self):
        with TemporaryDirectory() as directory, patch('imagegen.script_analysis.analyzer.JsonClient') as client:
            cache = DiskCache.for_data(Path(directory) / 'profile')
            client.return_value.ask_json.side_effect = AnalysisError('Тестовый сбой модели')
            with self.assertRaisesRegex(AnalysisError, 'Тестовый сбой модели'):
                Analyzer(cache=cache).analyze('Тестовый сценарий.')
            client.assert_called_once_with(api_key=None, failed_dir=cache.directory.parent / 'failures')
