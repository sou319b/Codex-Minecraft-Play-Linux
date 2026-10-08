"""Tests use synthetic video and a fake portal; never send desktop input."""
import io
import json
import select
import subprocess
import sys
import unittest
from pathlib import Path

from PIL import Image
from portal import Desktop, frame_to_image, number
from gi.repository import Gst
from server import Server, TOOLS


class FakeRemote:
    def __init__(self):
        self.events = []

    def __getattr__(self, name):
        def notify(*args):
            self.events.append((name, args))
        return notify


class InputTests(unittest.TestCase):
    def setUp(self):
        self.desktop = Desktop()
        self.desktop.session = '/fake/session'
        self.desktop.devices = 3
        self.remote = self.desktop.remote = FakeRemote()
        self.desktop.image_size = (1280, 720)
        self.desktop.logical_size = (1920, 1080)
        self.desktop.stream = 42
        self.desktop._sleep = lambda seconds: None

    def test_screenshot_coordinates_scaled_to_logical_monitor(self):
        self.desktop.move(x=640, y=360)
        name, args = self.remote.events[-1]
        self.assertEqual(name, 'NotifyPointerMotionAbsolute')
        self.assertEqual(tuple(args[2:]), (42, 960.0, 540.0))

    def test_release_every_key_after_interrupted_hold(self):
        def interrupted(seconds):
            raise KeyboardInterrupt
        self.desktop._sleep = interrupted
        with self.assertRaises(KeyboardInterrupt):
            self.desktop.key(['w', 'space'], duration=2)
        self.assertEqual([int(args[-1]) for _, args in self.remote.events], [1, 1, 0, 0])
        self.assertFalse(self.desktop.pressed_keys)

    def test_mouse_release_after_interrupted_hold(self):
        def interrupted(seconds):
            raise RuntimeError('lost stream')
        self.desktop._sleep = interrupted
        with self.assertRaises(RuntimeError):
            self.desktop.click(duration=2)
        self.assertEqual([int(args[-1]) for _, args in self.remote.events], [1, 0])
        self.assertFalse(self.desktop.pressed_buttons)

    def test_invalid_input_never_generates_events(self):
        calls = [lambda: self.desktop.key(['definitely_not_a_key']),
                 lambda: self.desktop.key(['w'], duration=100),
                 lambda: self.desktop.move(x=5000, y=0),
                 lambda: self.desktop.move(x=0, y=0, dx=1, dy=1),
                 lambda: self.desktop.click(button='unknown'),
                 lambda: self.desktop.scroll(1.5)]
        for call in calls:
            with self.assertRaises(ValueError):
                call()
        self.assertFalse(self.remote.events)

    def test_input_requires_granted_device(self):
        self.desktop.devices = 0
        with self.assertRaises(RuntimeError):
            self.desktop.key(['w'])
        with self.assertRaises(RuntimeError):
            self.desktop.move(dx=1, dy=1)
        self.assertFalse(self.remote.events)

    def test_disconnected_input_fails(self):
        self.desktop.session = None
        with self.assertRaises(RuntimeError):
            self.desktop.key(['w'])
        self.assertFalse(self.remote.events)

    def test_boolean_and_nonfinite_numbers_are_rejected(self):
        for value in (True, float('nan'), float('inf'), '1'):
            with self.assertRaises(ValueError):
                number(value, 0, 10, 'value')


class CaptureTests(unittest.TestCase):
    def test_window_stream_is_accepted(self):
        desktop = Desktop()
        desktop._accept_stream({'streams': [(42, {'source_type': 2})], 'devices': 3}, 'window')
        self.assertEqual(desktop.source, 'window')
        self.assertEqual(desktop.stream, 42)
        # Missing logical geometry must not be guessed for absolute clicks.
        self.assertFalse(desktop.info()['absolute_pointer_available'])

    def test_monitor_or_unknown_stream_cannot_fall_back_from_window(self):
        for properties in ({'source_type': 1}, {}):
            desktop = Desktop()
            with self.assertRaises(RuntimeError):
                desktop._accept_stream({'streams': [(42, properties)]}, 'window')
            self.assertIsNone(desktop.stream)
            self.assertIsNone(desktop.source)

    def test_multiple_window_streams_are_rejected(self):
        desktop = Desktop()
        with self.assertRaises(RuntimeError):
            desktop._accept_stream({'streams': [(42, {'source_type': 2}),
                                                (43, {'source_type': 2})]}, 'window')

    def test_select_requests_only_window_type(self):
        desktop = Desktop()
        calls = []
        desktop._request = lambda *args, **kw: calls.append((args, kw))
        desktop._select('window')
        self.assertEqual(int(calls[1][1]['options']['types']), 2)
        self.assertFalse(calls[1][1]['options']['multiple'])

    def test_changing_source_requires_disconnect(self):
        desktop = Desktop()
        desktop.session = '/fake/session'
        desktop.source = 'monitor'
        with self.assertRaises(RuntimeError):
            desktop.connect(source='window')

    def test_invalid_source_does_not_contact_portal(self):
        desktop = Desktop()
        with self.assertRaises(ValueError):
            desktop.connect(source='anything')
        self.assertIsNone(desktop.bus)

    def test_real_gstreamer_image_conversion_with_odd_width(self):
        Gst.init(None)
        pipeline = Gst.parse_launch(
            'videotestsrc num-buffers=1 pattern=red '
            '! video/x-raw,format=RGB,width=321,height=181 '
            '! pngenc ! appsink name=sink sync=false')
        pipeline.set_state(Gst.State.PLAYING)
        try:
            sample = pipeline.get_by_name('sink').emit('try-pull-sample', 3 * Gst.SECOND)
            self.assertIsNotNone(sample)
            image = frame_to_image(sample)
            self.assertEqual(image.size, (321, 181))
            self.assertEqual(image.getpixel((320, 180)), (255, 0, 0))
            desktop = Desktop()
            desktop._frame = lambda: image.copy()
            png, meta = desktop.screenshot(max_width=320)
            with Image.open(io.BytesIO(png)) as result:
                self.assertEqual(result.size[0], 320)
            self.assertEqual(meta['screenshot_size'][0], 320)
        finally:
            pipeline.set_state(Gst.State.NULL)


class ProtocolTests(unittest.TestCase):
    def test_idle_stdio_dispatches_portal_closure_and_releases_owner(self):
        script = '''
import sys
from portal import Desktop, GLib
from server import Server
desktop = Desktop()
desktop._acquire_owner()
def closed():
    desktop._closed()
    print('owner_released=' + str(desktop.owner_fd is None), file=sys.stderr, flush=True)
    return False
GLib.timeout_add(50, closed)
Server(desktop).serve()
'''
        import tempfile
        # A separate process waits with stdin open. No actual portal is contacted.
        with tempfile.TemporaryDirectory() as directory:
            script = ('from pathlib import Path\nimport portal\n'
                      f'portal.state_directory = lambda: Path({directory!r})\n' + script)
            process = subprocess.Popen([sys.executable, '-c', script],
                                       cwd=Path(__file__).parent, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                ready, _, _ = select.select([process.stderr], [], [], 3)
                self.assertTrue(ready, 'Portal events stalled while stdin was idle')
                self.assertEqual(process.stderr.readline().strip(), 'owner_released=True')
            finally:
                stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertEqual(stdout, '')

    def test_stdio_handshake_list_and_errors_without_desktop_session(self):
        messages = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
             'params': {'protocolVersion': '2025-06-18', 'capabilities': {},
                        'clientInfo': {'name': 'test', 'version': '1'}}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
             'params': {'name': 'linux_desktop_info'}},
            {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call',
             'params': {'name': 'linux_desktop_move', 'arguments': {'bogus': 1}}},
            {'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call',
             'params': {'name': 'linux_desktop_key', 'arguments': {}}},
            {'jsonrpc': '2.0', 'id': 6, 'method': 'ping'},
        ]
        process = subprocess.run([sys.executable, str(Path(__file__).with_name('server.py')), 'serve'],
                                 input='\n'.join(json.dumps(m) for m in messages) + '\n{\n',
                                 capture_output=True, text=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        replies = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(len(replies), 7)
        self.assertEqual(replies[0]['result']['protocolVersion'], '2025-06-18')
        self.assertEqual({tool['name'] for tool in replies[1]['result']['tools']},
                         {tool['name'] for tool in TOOLS})
        self.assertIn('linux_desktop_act', {tool['name'] for tool in TOOLS})
        self.assertIn('linux_desktop_sequence', {tool['name'] for tool in TOOLS})
        self.assertFalse(json.loads(replies[2]['result']['content'][0]['text'])['connected'])
        self.assertTrue(replies[3]['result']['isError'])
        self.assertTrue(replies[4]['result']['isError'])
        self.assertEqual(replies[-1]['error']['code'], -32700)

    def test_screenshot_is_an_mcp_image(self):
        class FakeDesktop:
            def screenshot(self):
                return b'PNG_BYTES', {'screenshot_size': [1, 1]}
        result = Server(FakeDesktop()).call('linux_desktop_screenshot', {})
        self.assertEqual(result['content'][0]['type'], 'image')
        self.assertEqual(result['content'][0]['mimeType'], 'image/png')


if __name__ == '__main__':
    unittest.main()
