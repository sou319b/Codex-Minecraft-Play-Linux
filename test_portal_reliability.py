"""Portal lifecycle and action timing regressions; never contact a desktop."""
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from portal import Desktop


class CapabilityTests(unittest.TestCase):
    def test_absolute_pointer_requires_connection_permission_and_geometry(self):
        cases = [(True, 0, (640, 360), (1280, 720), False),
                 (True, 1, (640, 360), (1280, 720), False),
                 (True, 2, (640, 360), (1280, 720), True),
                 (True, 3, (640, 360), (1280, 720), True),
                 (False, 3, (640, 360), (1280, 720), False),
                 (True, 3, None, (1280, 720), False),
                 (True, 3, (640, 360), None, False)]
        for connected, devices, image_size, logical_size, expected in cases:
            with self.subTest(connected=connected, devices=devices,
                              image_size=image_size, logical_size=logical_size):
                desktop = Desktop()
                desktop._pump = lambda: None
                desktop.session = '/fake/session' if connected else None
                desktop.devices = devices
                desktop.image_size = image_size
                desktop.logical_size = logical_size
                self.assertEqual(desktop.info()['absolute_pointer_available'], expected)

    def test_screenshot_does_not_enable_ungranted_pointer_input(self):
        desktop = Desktop()
        desktop._pump = lambda: None
        desktop.session = '/fake/capture-only-session'
        desktop.devices = 0
        desktop.logical_size = (1280, 720)
        desktop._frame = lambda: Image.new('RGB', (1280, 720))
        desktop.remote = Mock()
        _, metadata = desktop.screenshot(max_width=640)
        self.assertEqual(metadata['screenshot_size'], (640, 360))
        self.assertFalse(metadata['absolute_pointer_available'])
        with self.assertRaisesRegex(RuntimeError, 'did not grant'):
            desktop.move(x=10, y=10)
        self.assertEqual(desktop.remote.mock_calls, [])


class ProbeTests(unittest.TestCase):
    def test_probe_rejects_existing_session_without_closing_it(self):
        desktop = Desktop()
        desktop.session = '/fake/existing-session'
        desktop.close = Mock()
        desktop._request = Mock()
        with self.assertRaisesRegex(RuntimeError, 'already connected'):
            desktop.probe()
        desktop.close.assert_not_called()
        desktop._request.assert_not_called()
        self.assertEqual(desktop.session, '/fake/existing-session')

    def test_invalid_probe_source_never_creates_a_portal_session(self):
        desktop = Desktop()
        desktop._create = Mock()
        desktop.close = Mock()
        with self.assertRaisesRegex(ValueError, 'source must be'):
            desktop.probe(source='invalid')
        desktop._create.assert_not_called()
        desktop.close.assert_not_called()

    def test_failed_probe_still_closes_the_session_it_created(self):
        desktop = Desktop()
        desktop._create = Mock()
        desktop._select = Mock(side_effect=RuntimeError('device selection failed'))
        desktop.close = Mock()
        with self.assertRaisesRegex(RuntimeError, 'device selection failed'):
            desktop.probe()
        desktop.close.assert_called_once_with()


class DisconnectTests(unittest.TestCase):
    def test_disconnect_keeps_modifiers_held_until_buttons_are_released(self):
        desktop = Desktop()
        desktop.session = '/fake/session'
        desktop._bus = Mock()
        desktop.remote = Mock()
        desktop.pressed_keys.add(65505)  # Shift_L
        desktop.pressed_buttons.add(272)
        remote = desktop.remote
        with patch('portal.dbus.Interface') as interface:
            desktop.close()
        self.assertEqual([call[0] for call in remote.mock_calls],
                         ['NotifyPointerButton', 'NotifyKeyboardKeysym'])
        interface.return_value.Close.assert_called_once_with()
        self.assertFalse(desktop.pressed_keys | desktop.pressed_buttons)
        self.assertIsNone(desktop.session)


class ActionTimingTests(unittest.TestCase):
    def run_motion(self, latency):
        desktop = Desktop()
        desktop.session = '/fake/session'
        desktop.devices = 3
        desktop._pump = lambda: None
        clock = [0.0]
        events = []
        sleeps = []

        class Remote:
            def __getattr__(self, name):
                def notify(*args):
                    events.append((name, args, clock[0]))
                    clock[0] += latency
                return notify

        def sleep(seconds):
            self.assertGreaterEqual(seconds, 0)
            sleeps.append(seconds)
            clock[0] += seconds

        desktop.remote = Remote()
        desktop._sleep = sleep
        with patch('portal.time.monotonic', side_effect=lambda: clock[0]):
            result = desktop.act(keys=['w'], buttons=['left'], dx=100, dy=-50,
                                 duration=.1, settle=0)
        self.assertFalse(desktop.pressed_keys | desktop.pressed_buttons)
        motions = [(args, at) for name, args, at in events
                   if name == 'NotifyPointerMotion']
        releases = [at for name, args, at in events
                    if name != 'NotifyPointerMotion' and int(args[-1]) == 0]
        return motions, releases, sleeps, result

    def test_portal_latency_is_included_in_motion_duration(self):
        motions, releases, _, result = self.run_motion(latency=.01)
        self.assertEqual(len(motions), 5)
        self.assertAlmostEqual(sum(float(args[-2]) for args, _ in motions), 100)
        self.assertAlmostEqual(sum(float(args[-1]) for args, _ in motions), -50)
        # Synchronous portal latency must not extend every 20 ms motion slice.
        self.assertAlmostEqual(releases[0] - motions[0][1], .1)
        self.assertAlmostEqual(result['elapsed_seconds'], .14)

    def test_slow_portal_does_not_add_sleep_after_missed_motion_slots(self):
        motions, releases, sleeps, _ = self.run_motion(latency=.035)
        self.assertEqual(len(motions), 5)
        self.assertAlmostEqual(releases[0] - motions[0][1], 5 * .035)
        self.assertEqual(sleeps, [0] * 6)


if __name__ == '__main__':
    unittest.main()
