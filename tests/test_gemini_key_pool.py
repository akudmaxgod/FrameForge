import io
import json
import tempfile
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from imagegen.google_keys import GeminiKeyPool, GeminiPoolError, google_keys, _COOLDOWNS, _PREFERRED, _LAST_REQUEST
from imagegen.forgotten_cities.fidelity import SourceFidelityReviewer
from imagegen.service import Service


def response(value):
    result = MagicMock()
    result.__enter__.return_value = result
    result.read.return_value = json.dumps(value).encode()
    return result


def http_error(status, body=''):
    return urllib.error.HTTPError('https://generativelanguage.googleapis.com/test', status, 'rejected', {}, io.BytesIO(body.encode()))


class GeminiKeyPoolTests(unittest.TestCase):
    def setUp(self):
        for name in ('_COOLDOWNS', '_PREFERRED', '_LAST_REQUEST'):
            self.enterContext(patch('imagegen.google_keys.' + name, {}))

    def test_primary_is_first_and_keys_are_deduplicated_and_persist(self):
        self.assertEqual(google_keys('main', 'main\na,b; b c'), ['main', 'a', 'b', 'c'])
        with tempfile.TemporaryDirectory() as root:
            service = Service(root)
            service.save_settings({'google_api_key': 'main', 'google_extra_api_keys': 'second\nthird'})
            settings = Service(root).settings()
            self.assertEqual(google_keys(settings['google_api_key'], settings['google_extra_api_keys']), ['main', 'second', 'third'])

    def test_quota_switches_key_and_new_instances_keep_working_key(self):
        pool = GeminiKeyPool('main', 'second')
        with patch('urllib.request.urlopen', side_effect=[http_error(429), response({'ok': True}), response({'ok': True})]) as opened:
            self.assertTrue(pool.json('https://generativelanguage.googleapis.com/test', model='model')['ok'])
            self.assertTrue(GeminiKeyPool('main', 'second').json('https://generativelanguage.googleapis.com/test', model='model')['ok'])
        self.assertEqual([c.args[0].get_header('X-goog-api-key') for c in opened.call_args_list], ['main', 'second', 'second'])
        self.assertTrue(all('key=' not in c.args[0].full_url for c in opened.call_args_list))

    def test_invalid_or_restricted_key_uses_next_key(self):
        for error in (http_error(400, 'API_KEY_INVALID'), http_error(403)):
            with self.subTest(status=error.code), patch('imagegen.google_keys._COOLDOWNS', {}), patch('imagegen.google_keys._PREFERRED', {}):
                with patch('urllib.request.urlopen', side_effect=[error, response({'ok': True})]) as opened:
                    self.assertTrue(GeminiKeyPool('bad', 'good').json('https://generativelanguage.googleapis.com/test')['ok'])
                    self.assertEqual(opened.call_count, 2)

    def test_content_error_and_transport_failure_do_not_repeat(self):
        for error in (http_error(400, 'wrong prompt secret-main'), urllib.error.URLError('secret-main')):
            with self.subTest(error=type(error).__name__), patch('urllib.request.urlopen', side_effect=error) as opened:
                with self.assertRaises(GeminiPoolError) as failed:
                    GeminiKeyPool('secret-main', 'secret-extra').json('https://generativelanguage.googleapis.com/test')
                self.assertNotIn('secret', str(failed.exception))
                self.assertEqual(opened.call_count, 1)

    def test_exhausted_pool_stops_without_credentials_in_error(self):
        with patch('urllib.request.urlopen', side_effect=[http_error(429, 'secret-main'), http_error(429, 'secret-extra')]) as opened:
            with self.assertRaises(GeminiPoolError) as failed:
                GeminiKeyPool('secret-main', 'secret-extra').json('https://generativelanguage.googleapis.com/test')
            self.assertEqual(opened.call_count, 2)
            self.assertNotIn('secret', str(failed.exception))
            self.assertEqual(failed.exception.status, 429)

    def test_actual_source_reviewer_uses_pool_without_bypassing_verdict(self):
        payload = {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': '{"status":"fail"}'}]}}]}
        with tempfile.TemporaryDirectory() as root:
            reviewer = SourceFidelityReviewer(root, {'google_api_key': 'main', 'google_extra_api_keys': 'second'})
            with patch('urllib.request.urlopen', side_effect=[http_error(429), response(payload)]), patch('imagegen.google_keys.time.sleep'):
                self.assertEqual(reviewer._request('Review source', [b'image']), {'status': 'fail'})


if __name__ == '__main__':
    unittest.main()
