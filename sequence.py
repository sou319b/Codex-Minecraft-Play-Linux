"""Small visual checkpoints around bounded desktop actions.

Pixel comparisons detect layout changes and lack of visible movement; they do
not recognize inventory items or prove that the selected application has focus.
The desktop adapter owns input cleanup, including when ``act`` raises.
"""
from __future__ import annotations

import io
import math
import time

from PIL import Image, ImageChops, ImageStat


ACTION_FIELDS = {'keys', 'buttons', 'duration', 'dx', 'dy', 'x', 'y', 'settle'}


def _number(value, low, high, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{name} must be a number')
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'{name} must be between {low} and {high}')
    return value


def _regions(value, name):
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        raise ValueError(f'{name} must contain 1 to 16 rectangles')
    result = []
    for region in value:
        if not isinstance(region, list) or len(region) != 4:
            raise ValueError(f'{name} rectangles must be [left, top, right, bottom]')
        if any(isinstance(v, bool) or not isinstance(v, int) for v in region):
            raise ValueError(f'{name} coordinates must be integers')
        left, top, right, bottom = region
        if left < 0 or top < 0 or right <= left or bottom <= top:
            raise ValueError(f'{name} rectangles must have positive size')
        result.append(tuple(region))
    return result


def _plan(steps, stable_regions, max_seconds, max_width, max_mean_error):
    _number(max_seconds, 1, 30, 'max_seconds')
    _number(max_width, 320, 3840, 'max_width')
    if not isinstance(max_width, int):
        raise ValueError('max_width must be an integer')
    _number(max_mean_error, 0, 30, 'max_mean_error')
    stable = None if stable_regions is None else _regions(stable_regions, 'stable_regions')
    if not isinstance(steps, list) or not 1 <= len(steps) <= 16:
        raise ValueError('steps must contain 1 to 16 actions')
    result = []
    planned = 0
    for index, step in enumerate(steps):
        if not isinstance(step, dict) or step.keys() - ACTION_FIELDS - {'progress'}:
            raise ValueError(f'step {index} has unknown fields or is not an object')
        action = {k: v for k, v in step.items() if k != 'progress'}
        duration = _number(action.get('duration', .08), .01, 5, 'duration')
        settle = _number(action.get('settle', .15), 0, 2, 'settle')
        action['duration'], action['settle'] = duration, settle
        for field, limit, values in (('keys', 8, None),
                                     ('buttons', 3, {'left', 'right', 'middle'})):
            if field not in action:
                continue
            sequence = action[field]
            if not isinstance(sequence, list) or not 1 <= len(sequence) <= limit:
                raise ValueError(f'{field} must contain 1 to {limit} names')
            if any(not isinstance(v, str) or not v for v in sequence):
                raise ValueError(f'{field} names must be nonempty strings')
            if values is not None and any(v not in values for v in sequence):
                raise ValueError('buttons must be left, right or middle')
        absolute = 'x' in action or 'y' in action
        relative = 'dx' in action or 'dy' in action
        if absolute:
            if not {'x', 'y'} <= action.keys() or relative:
                raise ValueError('Provide both x and y without dx or dy')
            _number(action['x'], 0, 3839, 'x')
            _number(action['y'], 0, 2159, 'y')
            if stable is None:
                raise ValueError('Coordinate actions require stable_regions')
        if relative:
            for field in ('dx', 'dy'):
                if field in action:
                    _number(action[field], -4096, 4096, field)
        if not (absolute or relative or action.get('keys') or action.get('buttons')):
            raise ValueError(f'step {index} has no input')
        progress = None
        if 'progress' in step:
            value = step['progress']
            if not isinstance(value, dict) or set(value) != {'regions', 'min_mean_error'}:
                raise ValueError('progress requires regions and min_mean_error only')
            threshold = _number(value['min_mean_error'], 0, 255, 'min_mean_error')
            if threshold == 0:
                raise ValueError('min_mean_error must be greater than zero')
            progress = {'regions': _regions(value['regions'], 'progress.regions'),
                        'min_mean_error': threshold}
        result.append((action, progress))
        planned += duration + settle + (.03 if absolute else 0)
    if planned > max_seconds:
        raise ValueError('Planned durations and settling exceed max_seconds')
    return result, stable


def _image(png):
    with Image.open(io.BytesIO(png)) as image:
        return image.convert('RGB')


def _check_regions(regions, size):
    if any(right > size[0] or bottom > size[1] for _, _, right, bottom in regions or []):
        raise ValueError('Region lies outside the screenshot dimensions')


def _errors(before, after, regions):
    return [sum(ImageStat.Stat(ImageChops.difference(before.crop(region),
                                                   after.crop(region))).mean) / 3
            for region in regions]


def execute(desktop, steps, stable_regions=None, max_seconds=15, max_width=1280,
            max_mean_error=8):
    """Return latest PNG and an explicit partial-completion report; never replay.

    Rectangles use exclusive right/bottom edges in the returned screenshot.
    Each stable rectangle must stay within ``max_mean_error`` of the initial
    frame. Progress succeeds when at least one chosen region changes by its
    minimum mean RGB error. ``validate_action`` must reject unsupported keys
    and unavailable devices without sending input; ``act`` must release every
    key/button it presses in its own finally block.

    Static validation errors raise ValueError before any input. Operational
    errors stop the sequence and return the latest available image (possibly
    None when initial capture fails). Capture time counts against the budget,
    though a capture already in flight can finish after the deadline. Failed
    actions get one final observation if still connected; failed captures are
    never retried. ``final_capture_fresh`` records whether the returned image
    was captured after the latest attempted input.
    """
    plan, stable = _plan(steps, stable_regions, max_seconds, max_width, max_mean_error)
    coordinate_plan = any('x' in action for action, _ in plan)
    observed_size = getattr(desktop, 'image_size', None)
    started = time.monotonic()
    report = {'status': 'stopped', 'completed_steps': 0, 'input_completed_steps': 0,
              'stopped_at': None, 'error': None, 'steps': [],
              'final_capture_fresh': False}
    png = None
    index = None
    baseline = baseline_meta = None
    action_failed = False

    def capture():
        nonlocal png
        value, meta = desktop.screenshot(max_width=max_width)
        current = _image(value)
        png = value
        report['final_capture_fresh'] = True
        if baseline is not None:
            if current.size != baseline.size:
                raise RuntimeError('Screenshot dimensions changed; sequence stopped')
            for field in ('stream', 'capture_source', 'logical_size'):
                if meta.get(field) != baseline_meta.get(field):
                    raise RuntimeError(f'Capture mapping changed ({field}); sequence stopped')
        return current, meta

    def budget(action=None):
        remaining = max_seconds - (time.monotonic() - started)
        needed = (0 if action is None else action['duration'] + action['settle'] +
                  (.03 if 'x' in action else 0))
        if remaining <= 0 or needed > remaining:
            raise RuntimeError('Sequence wall-time budget exhausted')

    try:
        baseline, baseline_meta = capture()
        if coordinate_plan and hasattr(desktop, 'image_size'):
            if observed_size is None or tuple(observed_size) != baseline.size:
                raise RuntimeError('Coordinate mapping differs from the last observed screenshot; '
                                   'inspect this image and use the same max_width before continuing')
        # Validate dimensions and every adapter action before the first input.
        _check_regions(stable, baseline.size)
        for action, progress in plan:
            if 'x' in action and (action['x'] >= baseline.width or action['y'] >= baseline.height):
                raise ValueError('Coordinate lies outside the screenshot dimensions')
            if progress:
                _check_regions(progress['regions'], baseline.size)
            desktop.validate_action(**action)
        budget()
        current = baseline
        for index, (action, progress) in enumerate(plan):
            entry = {'index': index, 'input_completed': False, 'progress_success': None}
            report['steps'].append(entry)
            budget()
            # Refresh immediately before absolute input so earlier actions cannot
            # silently invalidate positions measured in the initial screenshot.
            if 'x' in action or progress:
                current, _ = capture()
                budget()
            if 'x' in action:
                entry['stable_region_errors'] = _errors(baseline, current, stable)
                if max(entry['stable_region_errors']) > max_mean_error:
                    raise RuntimeError('Stable regions changed; coordinate action cancelled')
            before = current
            budget(action)
            report['final_capture_fresh'] = False
            try:
                entry['action_metadata'] = desktop.act(**action, deadline=started + max_seconds)
            except Exception as exc:
                action_failed = True
                if getattr(exc, 'input_completed', False):
                    entry['input_completed'] = True
                    report['input_completed_steps'] += 1
                raise
            entry['input_completed'] = True
            report['input_completed_steps'] += 1
            current, _ = capture()
            if progress:
                entry['progress_region_errors'] = _errors(before, current, progress['regions'])
                entry['progress_success'] = max(entry['progress_region_errors']) >= progress['min_mean_error']
                if not entry['progress_success']:
                    raise RuntimeError('Insufficient visible progress; sequence stopped')
            report['completed_steps'] += 1
            budget()
        report['status'] = 'completed'
    except ValueError as exc:
        # Plan errors are actionable corrections, not partial execution reports.
        # validate_action also runs here before the first input.
        if not report['steps']:
            raise
        report['stopped_at'] = index
        report['error'] = str(exc)
    except Exception as exc:
        report['stopped_at'] = index
        report['error'] = str(exc)
    finally:
        # An interrupted action can have changed the screen despite not returning
        # metadata. Observe once so the caller can assess partial input. A failed
        # screenshot itself is never retried, and disconnected sessions are left
        # alone. Fake/offline adapters need only provide screenshot().
        if action_failed and getattr(desktop, 'session', True):
            try:
                capture()
            except Exception as exc:
                report['capture_error'] = str(exc)
        report['elapsed_seconds'] = round(time.monotonic() - started, 4)
        report['budget_exceeded'] = report['elapsed_seconds'] > max_seconds
    return png, report
