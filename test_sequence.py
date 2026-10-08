"""Visual sequencing tests use synthetic PNGs and never send desktop input."""
import io
import unittest
from unittest.mock import patch

from PIL import Image

from sequence import execute


def frame(color='black', size=(100, 60)):
    image = Image.new('RGB', size, color)
    output = io.BytesIO()
    image.save(output, 'PNG')
    return output.getvalue()


class FakeDesktop:
    def __init__(self, frames=None, fail_at=None):
        self.frames = frames or [frame()]
        self.captures = 0
        self.actions = []
        self.validations = []
        self.fail_at = fail_at
        self.pressed = set()

    def screenshot(self, max_width=1280):
        png = self.frames[min(self.captures, len(self.frames) - 1)]
        self.captures += 1
        return png, {'logical_size': [100, 60], 'capture_source': 'window', 'stream': 1}

    def validate_action(self, **action):
        self.validations.append(action)
        if 'invalid' in action.get('keys', []):
            raise ValueError('Unknown key')
        return action

    def act(self, **action):
        self.actions.append(action)
        self.pressed.update(action.get('keys', []))
        try:
            if len(self.actions) - 1 == self.fail_at:
                raise RuntimeError('Synthetic input failure')
            return {'released': True}
        finally:
            self.pressed.clear()


class SequenceTests(unittest.TestCase):
    def test_complete_plan_captures_after_each_action(self):
        desktop = FakeDesktop()
        png, report = execute(desktop, [{'keys': ['w']}, {'buttons': ['left']}])
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['completed_steps'], 2)
        self.assertEqual(report['input_completed_steps'], 2)
        self.assertEqual(desktop.captures, 3)
        self.assertEqual(len(desktop.validations), 2)
        self.assertTrue(all('deadline' in action for action in desktop.actions))
        self.assertEqual(png, frame())

    def test_layout_mismatch_prevents_coordinate_click(self):
        desktop = FakeDesktop([frame(), frame('white')])
        png, report = execute(desktop, [{'x': 20, 'y': 20, 'buttons': ['left']}],
                              stable_regions=[[0, 0, 100, 60]])
        self.assertEqual(report['status'], 'stopped')
        self.assertEqual(report['completed_steps'], 0)
        self.assertEqual(report['stopped_at'], 0)
        self.assertFalse(desktop.actions)
        self.assertEqual(png, frame('white'))

    def test_every_coordinate_click_rechecks_initial_layout(self):
        desktop = FakeDesktop([frame(), frame(), frame(), frame('white')])
        action = {'x': 20, 'y': 20, 'buttons': ['left']}
        _, report = execute(desktop, [action, action], stable_regions=[[0, 0, 100, 60]])
        self.assertEqual(report['completed_steps'], 1)
        self.assertEqual(report['stopped_at'], 1)
        self.assertEqual(len(desktop.actions), 1)

    def test_layout_threshold_checks_every_region(self):
        image = Image.new('RGB', (100, 60), 'black')
        image.paste('white', (0, 0, 1, 1))
        output = io.BytesIO()
        image.save(output, 'PNG')
        desktop = FakeDesktop([frame(), output.getvalue()])
        _, report = execute(desktop, [{'x': 20, 'y': 20}],
                            stable_regions=[[0, 0, 1, 1], [1, 1, 100, 60]])
        self.assertEqual(report['status'], 'stopped')
        self.assertFalse(desktop.actions)

    def test_partial_failure_reports_and_adapter_releases_inputs(self):
        desktop = FakeDesktop(fail_at=1)
        _, report = execute(desktop, [{'keys': ['w']}, {'keys': ['shift']}, {'keys': ['e']}])
        self.assertEqual(report['completed_steps'], 1)
        self.assertEqual(report['input_completed_steps'], 1)
        self.assertEqual(report['stopped_at'], 1)
        self.assertEqual(len(desktop.actions), 2)
        self.assertFalse(report['steps'][-1]['input_completed'])
        self.assertFalse(desktop.pressed)
        self.assertTrue(report['final_capture_fresh'])
        self.assertEqual(desktop.captures, 3)

    def test_failed_action_captures_changed_screen_once(self):
        desktop = FakeDesktop([frame(), frame('white')], fail_at=0)
        png, report = execute(desktop, [{'keys': ['w']}, {'keys': ['e']}])
        self.assertEqual(png, frame('white'))
        self.assertTrue(report['final_capture_fresh'])
        self.assertEqual(desktop.captures, 2)
        self.assertEqual(len(desktop.actions), 1)

    def test_failed_action_reports_failed_final_capture_without_retry(self):
        class FailingCapture(FakeDesktop):
            def screenshot(self, **kwargs):
                if self.captures == 1:
                    self.captures += 1
                    raise RuntimeError('Synthetic final capture failure')
                return super().screenshot(**kwargs)
        desktop = FailingCapture(fail_at=0)
        png, report = execute(desktop, [{'keys': ['w']}])
        self.assertEqual(png, frame())
        self.assertFalse(report['final_capture_fresh'])
        self.assertIn('input failure', report['error'])
        self.assertIn('capture failure', report['capture_error'])
        self.assertEqual(desktop.captures, 2)

    def test_failed_disconnected_action_does_not_capture(self):
        class DisconnectedDesktop(FakeDesktop):
            session = True

            def act(self, **kwargs):
                self.session = None
                return super().act(**kwargs)
        desktop = DisconnectedDesktop(fail_at=0)
        _, report = execute(desktop, [{'keys': ['w']}])
        self.assertFalse(report['final_capture_fresh'])
        self.assertEqual(desktop.captures, 1)

    def test_no_progress_stops_after_completed_input_without_replay(self):
        desktop = FakeDesktop()
        step = {'keys': ['w'], 'progress': {'regions': [[0, 0, 100, 60]], 'min_mean_error': 2}}
        _, report = execute(desktop, [step, {'keys': ['w']}])
        self.assertEqual(report['completed_steps'], 0)
        self.assertEqual(report['input_completed_steps'], 1)
        self.assertTrue(report['steps'][0]['input_completed'])
        self.assertFalse(report['steps'][0]['progress_success'])
        self.assertEqual(len(desktop.actions), 1)

    def test_progress_success_compares_immediate_before_and_after(self):
        desktop = FakeDesktop([frame(), frame('red'), frame('red')])
        step = {'keys': ['w'], 'progress': {'regions': [[0, 0, 100, 60]], 'min_mean_error': 2}}
        _, report = execute(desktop, [step])
        self.assertFalse(report['steps'][0]['progress_success'])
        desktop = FakeDesktop([frame(), frame('red'), frame('blue')])
        _, report = execute(desktop, [step])
        self.assertEqual(report['status'], 'completed')
        self.assertTrue(report['steps'][0]['progress_success'])

    def test_malformed_later_steps_never_send_earlier_input(self):
        bad_steps = [{}, {'duration': 100, 'keys': ['w']}, {'keys': 'w'}, {'keys': [True]},
                     {'buttons': ['bad']}, {'keys': ['w'], 'surprise': True},
                     {'keys': ['w'], 'settle': float('nan')}, {'keys': ['w'], 'duration': True},
                     {'dx': None}, {'x': 20}, {'x': 20, 'y': 20, 'dx': 1},
                     {'keys': ['w'], 'progress': {'regions': [[0, 0, 1, 1]], 'min_mean_error': 0}}]
        for bad in bad_steps:
            with self.subTest(bad=bad):
                desktop = FakeDesktop()
                with self.assertRaises(ValueError):
                    execute(desktop, [{'keys': ['w']}, bad])
                self.assertFalse(desktop.actions)

    def test_adapter_validates_all_key_names_before_first_input(self):
        desktop = FakeDesktop()
        with self.assertRaises(ValueError):
            execute(desktop, [{'keys': ['w']}, {'keys': ['invalid']}])
        self.assertFalse(desktop.actions)
        self.assertEqual(len(desktop.validations), 2)

    def test_coordinate_dimensions_are_validated_for_entire_plan(self):
        desktop = FakeDesktop()
        with self.assertRaises(ValueError):
            execute(desktop, [{'keys': ['w']}, {'x': 100, 'y': 20}],
                    stable_regions=[[0, 0, 100, 60]])
        self.assertFalse(desktop.actions)
        for region in ([[0, 0, 101, 60]], [[0, 0, 10.0, 10]], [[0, 0, 0, 1]]):
            with self.subTest(region=region), self.assertRaises(ValueError):
                execute(desktop, [{'keys': ['w']}], stable_regions=region)
        self.assertFalse(desktop.actions)

    def test_resize_after_input_stops_before_remaining_action(self):
        desktop = FakeDesktop([frame(), frame(size=(99, 60))])
        png, report = execute(desktop, [{'keys': ['w']}, {'keys': ['e']}])
        self.assertEqual(report['status'], 'stopped')
        self.assertEqual(report['input_completed_steps'], 1)
        self.assertEqual(len(desktop.actions), 1)
        self.assertEqual(png, frame(size=(99, 60)))

    def test_capture_mapping_change_stops_coordinate_action(self):
        class MappingDesktop(FakeDesktop):
            def screenshot(self, **kwargs):
                png, meta = super().screenshot(**kwargs)
                if self.captures > 1:
                    meta['logical_size'] = [200, 120]
                return png, meta
        desktop = MappingDesktop()
        _, report = execute(desktop, [{'x': 20, 'y': 20}], stable_regions=[[0, 0, 100, 60]])
        self.assertIn('mapping changed', report['error'])
        self.assertFalse(desktop.actions)

    def test_capture_failure_reports_last_observation_without_replay(self):
        class FailingCapture(FakeDesktop):
            def screenshot(self, **kwargs):
                if self.captures == 1:
                    raise RuntimeError('Synthetic capture failure')
                return super().screenshot(**kwargs)
        desktop = FailingCapture()
        png, report = execute(desktop, [{'keys': ['w']}, {'keys': ['e']}])
        self.assertEqual(png, frame())
        self.assertEqual(report['input_completed_steps'], 1)
        self.assertEqual(report['completed_steps'], 0)
        self.assertEqual(len(desktop.actions), 1)
        self.assertIn('capture failure', report['error'])
        self.assertFalse(report['final_capture_fresh'])

    def test_initial_capture_failure_returns_no_image_without_input(self):
        class FailingCapture(FakeDesktop):
            def screenshot(self, **kwargs):
                raise RuntimeError('Synthetic initial capture failure')
        desktop = FailingCapture()
        png, report = execute(desktop, [{'keys': ['w']}])
        self.assertIsNone(png)
        self.assertEqual(report['status'], 'stopped')
        self.assertFalse(desktop.actions)

    def test_budget_prediction_stops_before_action(self):
        desktop = FakeDesktop()
        with patch('sequence.time.monotonic', side_effect=[0, 0, .8, .8, .8, .8]):
            _, report = execute(desktop, [{'keys': ['w'], 'duration': .5, 'settle': .1}],
                                max_seconds=1)
        self.assertIn('budget', report['error'])
        self.assertFalse(desktop.actions)

    def test_static_budget_step_count_and_image_width(self):
        for steps, options in [([], {}), ([{'keys': ['w']}] * 17, {}),
                               ([{'keys': ['w'], 'duration': 2}], {'max_seconds': 1}),
                               ([{'keys': ['w']}], {'max_width': True}),
                               ([{'keys': ['w']}], {'max_width': 1280.5}),
                               ([{'keys': ['w']}], {'max_mean_error': 31}),
                               ([{'keys': ['w']}], {'max_mean_error': float('inf')})]:
            with self.subTest(options=options):
                desktop = FakeDesktop()
                with self.assertRaises(ValueError):
                    execute(desktop, steps, **options)
                self.assertFalse(desktop.actions)

    def test_coordinate_positioning_time_counts_against_planned_budget(self):
        desktop = FakeDesktop()
        with self.assertRaises(ValueError):
            execute(desktop, [{'x': 20, 'y': 20, 'duration': .98, 'settle': 0}],
                    stable_regions=[[0, 0, 100, 60]], max_seconds=1)
        self.assertFalse(desktop.actions)


if __name__ == '__main__':
    unittest.main()
