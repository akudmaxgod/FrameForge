import base64
import unittest
from imagegen.providers import GeminiOmniFlashProvider, ExecutionError
from imagegen.domain import DomainError

class OmniProviderTests(unittest.TestCase):
    def test_interactions_payload_keeps_requested_six_seconds_and_quality(self):
        seen = {}
        def fake_request(payload):
            seen.update(payload)
            return {'outputs': [{'content': {'parts': [{'inlineData': {
                'mimeType': 'video/mp4', 'data': base64.b64encode(b'video-bytes').decode()
            }}]}}]}
        provider = GeminiOmniFlashProvider('test-key', request=fake_request)
        video, mime = provider.image_to_video('animate the supplied frame', b'png', resolution='720p', duration_seconds=6)
        self.assertEqual(b'video-bytes', video)
        self.assertEqual('video/mp4', mime)
        self.assertEqual('gemini-omni-1.1-flash', seen['model'])
        self.assertEqual('720p', seen['generationConfig']['video']['resolution'])
        self.assertEqual(6, seen['generationConfig']['video']['durationSeconds'])
        self.assertEqual(b'png', base64.b64decode(seen['input'][0]['parts'][1]['inlineData']['data']))

    def test_invalid_duration_is_rejected_before_network(self):
        called = False
        def fake_request(_):
            nonlocal called
            called = True
            return {}
        provider = GeminiOmniFlashProvider('test-key', request=fake_request)
        with self.assertRaises(DomainError):
            provider.image_to_video('p', b'i', duration_seconds=5)
        self.assertFalse(called)

    def test_empty_api_response_is_never_a_success(self):
        provider = GeminiOmniFlashProvider('test-key', request=lambda _: {'outputs': []})
        with self.assertRaises(ExecutionError):
            provider.image_to_video('p', b'i')

if __name__ == '__main__':
    unittest.main()