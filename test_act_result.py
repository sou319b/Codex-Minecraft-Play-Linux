"""Action failure feedback tests; all desktop input uses a fake portal."""
import json
import unittest
from unittest.mock import patch

from PIL import Image
from portal import Desktop
from server import Server


class ActResultTests(unittest.TestCase):
    def setUp(self):
        self.desktop = Desktop()
        self.desktop.session = '/fake/session'
        self.desktop.devices = 3
        self.events = []
        self.captures = []
        events = self.events

        class Remote:
            def NotifyKeyboardKeysym(self, *args):
                events.append(int(args[-1]))

        self.desktop.remote = Remote()
        self.desktop._sleep = lambda seconds: None

        def frame():
            self.assertFalse(self.desktop.pressed_keys | self.desktop.pressed_buttons)
            self.captures.append(True)
            return Image.new('RGB', (640, 360))
        self.desktop._frame = frame

    def call(self, **args):
        result = Server(self.desktop).handle({
            'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': 'linux_desktop_act', 'arguments': args}})['result']
        return result

    def interrupt(self, seconds):
        raise RuntimeError('Interrupted during hold')

    def test_partial_hold_is_observed_once_after_release(self):
        self.desktop._sleep = self.interrupt
        result = self.call(keys=['w'])
        metadata = json.loads(result['content'][-1]['text'])
        self.assertTrue(result['isError'])
        self.assertEqual(result['content'][0]['type'], 'image')
        self.assertFalse(metadata['input_completed'])
        self.assertTrue(metadata['input_may_have_occurred'])
        self.assertTrue(metadata['capture_fresh'])
        self.assertIn('Interrupted during hold', metadata['action_error'])
        self.assertIn('Do not replay', metadata['warning'])
        self.assertEqual(self.events, [1, 0])
        self.assertEqual(self.captures, [True])

    def test_partial_hold_and_failed_capture_preserve_both_errors(self):
        self.desktop._sleep = self.interrupt

        def fail_capture():
            self.captures.append(True)
            raise RuntimeError('Capture unavailable')
        self.desktop._frame = fail_capture
        result = self.call(keys=['w'])
        metadata = json.loads(result['content'][-1]['text'])
        self.assertTrue(result['isError'])
        self.assertFalse(metadata['capture_fresh'])
        self.assertEqual(metadata['capture_error'], 'Capture unavailable')
        self.assertEqual(metadata['action_error'], 'Interrupted during hold')
        self.assertEqual(self.events, [1, 0])
        self.assertEqual(self.captures, [True])

    def test_disconnect_during_hold_does_not_retry_capture(self):
        def disconnect(seconds):
            self.desktop._closed()
            raise RuntimeError('Sharing ended')
        self.desktop._sleep = disconnect
        result = self.call(keys=['w'])
        metadata = json.loads(result['content'][-1]['text'])
        self.assertEqual(metadata['capture_skipped'], 'disconnected')
        self.assertFalse(metadata['capture_fresh'])
        self.assertEqual(self.captures, [])

    def test_invalid_action_never_sends_input_or_captures(self):
        result = self.call(keys=['not_a_key'])
        self.assertTrue(result['isError'])
        self.assertIn('Unknown key', result['content'][0]['text'])
        self.assertEqual(self.events, [])
        self.assertEqual(self.captures, [])

    def test_action_and_capture_latency_are_separate(self):
        clock = [10.0]

        def sleep(seconds):
            clock[0] += seconds
        self.desktop._sleep = sleep
        original_frame = self.desktop._frame

        def slow_frame():
            clock[0] += .4
            return original_frame()
        self.desktop._frame = slow_frame
        with patch('server.time.monotonic', side_effect=lambda: clock[0]):
            result = self.call(keys=['w'], duration=.1, settle=.15)
        metadata = json.loads(result['content'][-1]['text'])
        self.assertFalse(result['isError'])
        self.assertEqual(metadata['timings'], {'action_seconds': .25,
                                               'capture_seconds': .4,
                                               'total_seconds': .65})


if __name__ == '__main__':
    unittest.main()
