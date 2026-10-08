"""Independent regression review. Every input target is a fake portal."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from portal import Desktop
from server import Server


class Remote:
    def __init__(self, failure=None):
        self.events = []
        self.failure = failure

    def __getattr__(self, name):
        def notify(*args):
            self.events.append((name, args))
            if self.failure:
                self.failure(name, args)
        return notify


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.desktop = Desktop()
        self.desktop.session = '/fake/session'
        self.desktop.devices = 3
        self.desktop.image_size = (1280, 720)
        self.desktop.logical_size = (1920, 1080)
        self.desktop.stream = 42
        self.remote = self.desktop.remote = Remote()
        self.desktop._sleep = lambda seconds: None

    def test_shift_is_held_through_button_release(self):
        self.desktop.act(keys=['shift'], buttons=['left'], duration=.08, settle=0)
        self.assertEqual([(name, int(args[-1])) for name, args in self.remote.events],
                         [('NotifyKeyboardKeysym', 1), ('NotifyPointerButton', 1),
                          ('NotifyPointerButton', 0), ('NotifyKeyboardKeysym', 0)])
        self.assertFalse(self.desktop.pressed_keys | self.desktop.pressed_buttons)

    def test_partial_press_failure_releases_attempted_inputs(self):
        def failure(name, args):
            if name == 'NotifyPointerButton' and int(args[-1]) == 1:
                raise RuntimeError('failed button press')
        self.remote.failure = failure
        with self.assertRaisesRegex(RuntimeError, 'failed button press'):
            self.desktop.act(keys=['w', 'space'], buttons=['left'], settle=0)
        release_names = [name for name, args in self.remote.events if int(args[-1]) == 0]
        self.assertCountEqual(release_names, ['NotifyPointerButton', 'NotifyKeyboardKeysym',
                                             'NotifyKeyboardKeysym'])
        self.assertFalse(self.desktop.pressed_keys | self.desktop.pressed_buttons)

    def test_release_failure_does_not_skip_other_releases(self):
        def failure(name, args):
            if name == 'NotifyPointerButton' and int(args[-1]) == 0:
                raise RuntimeError('failed button release')
        self.remote.failure = failure
        closed = []
        def close():
            closed.append(True)
            self.desktop.session = None
            self.desktop._cleanup()
        self.desktop.close = close
        with self.assertRaisesRegex(RuntimeError, 'Input release failed'):
            self.desktop.act(keys=['w', 'space'], buttons=['left'], settle=0)
        self.assertEqual(len([args for name, args in self.remote.events
                              if name == 'NotifyKeyboardKeysym' and int(args[-1]) == 0]), 2)
        self.assertEqual(closed, [True])

    def test_invalid_combined_arguments_emit_no_events(self):
        cases = [dict(keys=['no_such_key']), dict(buttons=['left', 'left']),
                 dict(keys=['w'], settle=3), dict(keys=['w'], duration=True),
                 dict(keys=['w'], dy=float('nan')), dict(x=10),
                 dict(x=10, y=10, dx=1), dict(keys=['w'], deadline=float('inf'))]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.desktop.act(**kwargs)
        self.assertEqual(self.remote.events, [])
        with self.assertRaises(ValueError):
            Server(self.desktop).call('linux_desktop_act', {'keys': ['w'], 'max_width': 100})
        self.assertEqual(self.remote.events, [])

    def test_one_axis_motion_has_exact_total_and_spread(self):
        for kwargs, expected in (({'dx': 125}, (125, 0)), ({'dy': -75}, (0, -75))):
            self.remote.events.clear()
            self.desktop.move(**kwargs, duration=.1)
            motions = [args for name, args in self.remote.events if name == 'NotifyPointerMotion']
            self.assertEqual(len(motions), 5)
            self.assertAlmostEqual(sum(float(args[-2]) for args in motions), expected[0])
            self.assertAlmostEqual(sum(float(args[-1]) for args in motions), expected[1])

    def test_stop_file_interrupts_and_releases_held_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            self.desktop.stop_path = Path(directory) / 'stop'
            def stop_while_holding(seconds):
                self.desktop.stop_path.touch()
                self.desktop._guard_input()
            self.desktop._sleep = stop_while_holding
            with self.assertRaisesRegex(RuntimeError, 'stop file'):
                self.desktop.act(keys=['w'], buttons=['left'], settle=0)
            self.assertFalse(self.desktop.pressed_keys | self.desktop.pressed_buttons)
            self.assertEqual([int(args[-1]) for _, args in self.remote.events], [1, 1, 0, 0])

    def test_deadline_interrupts_and_releases_held_inputs(self):
        self.desktop._sleep = Desktop._sleep.__get__(self.desktop)
        with self.assertRaisesRegex(RuntimeError, 'deadline'):
            self.desktop.act(keys=['w'], duration=.1, settle=0,
                             deadline=time.monotonic() + .01)
        self.assertFalse(self.desktop.pressed_keys)
        self.assertEqual([int(args[-1]) for _, args in self.remote.events], [1, 0])
        self.assertIsNone(self.desktop.action_deadline)

    def test_capture_failure_occurs_after_all_inputs_released(self):
        captures = []
        def capture(**kwargs):
            captures.append(True)
            self.assertFalse(self.desktop.pressed_keys | self.desktop.pressed_buttons)
            raise RuntimeError('capture unavailable')
        self.desktop.screenshot = capture
        result = Server(self.desktop).call('linux_desktop_act', {'keys': ['w'], 'buttons': ['left']})
        self.assertTrue(result['isError'])
        self.assertEqual(len(result['content']), 1)
        self.assertEqual(result['content'][0]['type'], 'text')
        metadata = json.loads(result['content'][0]['text'])
        self.assertTrue(metadata['input_completed'])
        self.assertFalse(metadata['capture_fresh'])
        self.assertEqual(metadata['capture_error'], 'capture unavailable')
        self.assertEqual(captures, [True])
        self.assertEqual([int(args[-1]) for _, args in self.remote.events], [1, 1, 0, 0])
        self.assertFalse(self.desktop.pressed_keys | self.desktop.pressed_buttons)

    def test_act_response_geometry_describes_returned_image(self):
        self.desktop._frame = lambda: Image.new('RGB', (1920, 1080))
        result = Server(self.desktop).call('linux_desktop_act', {'max_width': 640})
        metadata = json.loads(result['content'][1]['text'])
        self.assertEqual(metadata['screenshot_size'], [640, 360])
        self.assertEqual(self.desktop.image_size, (640, 360))

    def test_settle_failure_preserves_completed_input_in_act_response(self):
        sleeps = []
        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 2:
                raise RuntimeError('Input interrupted by stop file')
        self.desktop._sleep = sleep
        self.desktop._frame = lambda: Image.new('RGB', (1920, 1080))
        result = Server(self.desktop).handle({
            'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': 'linux_desktop_act', 'arguments': {'keys': ['w']}}})['result']
        self.assertTrue(result['isError'])
        metadata = json.loads(result['content'][-1]['text'])
        self.assertTrue(metadata['input_completed'])
        self.assertTrue(metadata['capture_fresh'])
        self.assertIn('stop file', metadata['action_error'])
        self.assertFalse(self.desktop.pressed_keys | self.desktop.pressed_buttons)

    def test_settle_failure_preserves_completed_input_in_sequence(self):
        from sequence import execute
        sleeps = []
        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 2:
                raise RuntimeError('Input interrupted by stop file')
        self.desktop._sleep = sleep
        self.desktop._frame = lambda: Image.new('RGB', (1920, 1080))
        _, report = execute(self.desktop, [{'keys': ['w']}, {'keys': ['e']}])
        self.assertEqual(report['status'], 'stopped')
        self.assertEqual(report['input_completed_steps'], 1)
        self.assertTrue(report['steps'][0]['input_completed'])
        self.assertTrue(report['final_capture_fresh'])
        self.assertEqual(len(self.remote.events), 2)

    def test_sequence_rejects_coordinates_from_different_screenshot_size(self):
        from sequence import execute
        self.desktop.image_size = (640, 360)
        self.desktop._frame = lambda: Image.new('RGB', (1920, 1080))
        _, report = execute(self.desktop, [{'x': 200, 'y': 150, 'buttons': ['left']}],
                            stable_regions=[[0, 0, 10, 10]])
        self.assertEqual(report['status'], 'stopped')
        self.assertIn('last observed screenshot', report['error'])
        self.assertTrue(report['final_capture_fresh'])
        self.assertEqual(self.remote.events, [])

    def test_sequence_accepts_matching_observed_screenshot_size(self):
        from sequence import execute
        self.desktop.image_size = (640, 360)
        self.desktop._frame = lambda: Image.new('RGB', (1920, 1080))
        _, report = execute(self.desktop, [{'x': 200, 'y': 150, 'buttons': ['left']}],
                            stable_regions=[[0, 0, 10, 10]], max_width=640)
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(self.remote.events[0][0], 'NotifyPointerMotionAbsolute')

    def test_malformed_geometry_is_rejected(self):
        for size in ((0, 720), (-1, 720), (1280,), (1280, 720, 1),
                     (True, 720), ('1280', 720)):
            with self.subTest(size=size), self.assertRaises(RuntimeError):
                Desktop()._accept_stream({'streams': [(42, {'source_type': 2,
                                                           'logical_size': size})]}, 'window')


class OwnerReviewTests(unittest.TestCase):
    def test_cleanup_releases_file_descriptors_and_owner_despite_teardown_failures(self):
        operations = []
        class BrokenPipeline:
            def set_state(self, state):
                operations.append('pipeline')
                raise RuntimeError('pipeline teardown failed')
        class BrokenMatch:
            def remove(self):
                operations.append('match')
                raise RuntimeError('signal removal failed')
        with tempfile.TemporaryDirectory() as directory, patch('portal.state_directory',
                                                              return_value=Path(directory)):
            desktop, other = Desktop(), Desktop()
            read_fd, write_fd = os.pipe()
            try:
                desktop._acquire_owner()
                owner_fd = desktop.owner_fd
                desktop.fd = read_fd
                desktop.pipeline = BrokenPipeline()
                desktop.closed_match = BrokenMatch()
                desktop.pressed_keys.add(119)
                desktop.pressed_buttons.add(272)
                desktop._cleanup()
                self.assertEqual(operations, ['pipeline', 'match'])
                self.assertIsNone(desktop.fd)
                self.assertIsNone(desktop.pipeline)
                self.assertIsNone(desktop.closed_match)
                self.assertIsNone(desktop.owner_fd)
                self.assertFalse(desktop.pressed_keys | desktop.pressed_buttons)
                for fd in (read_fd, owner_fd):
                    with self.assertRaises(OSError):
                        os.fstat(fd)
                other._acquire_owner()
                self.assertIsNotNone(other.owner_fd)
            finally:
                desktop._cleanup()
                other._cleanup()
                os.close(write_fd)

    def test_single_owner_and_release_on_closed_signal(self):
        with tempfile.TemporaryDirectory() as directory, patch('portal.state_directory',
                                                              return_value=Path(directory)):
            first, second = Desktop(), Desktop()
            try:
                first._acquire_owner()
                with self.assertRaisesRegex(RuntimeError, 'Another'):
                    second._acquire_owner()
                first._closed()
                second._acquire_owner()
                self.assertIsNotNone(second.owner_fd)
            finally:
                first._cleanup()
                second._cleanup()

    def test_failed_connect_releases_owner(self):
        with tempfile.TemporaryDirectory() as directory, patch('portal.state_directory',
                                                              return_value=Path(directory)):
            desktop = Desktop()
            def fail_create():
                raise RuntimeError('create failed')
            desktop._create = fail_create
            with self.assertRaisesRegex(RuntimeError, 'create failed'):
                desktop.connect()
            self.assertIsNone(desktop.owner_fd)
            other = Desktop()
            try:
                other._acquire_owner()
            finally:
                other._cleanup()

    def test_stopped_state_prevents_owner_acquisition(self):
        with tempfile.TemporaryDirectory() as directory, patch('portal.state_directory',
                                                              return_value=Path(directory)):
            (Path(directory) / 'stop').touch()
            desktop = Desktop()
            with self.assertRaisesRegex(RuntimeError, 'Input stopped'):
                desktop._acquire_owner()
            self.assertIsNone(desktop.owner_fd)


if __name__ == '__main__':
    unittest.main()
