"""Wayland desktop capture and input through user-approved XDG portals."""
from __future__ import annotations

import base64
import io
import math
import os
import time
import uuid
import fcntl
import stat
from pathlib import Path

import dbus
import gi
from dbus.mainloop.glib import DBusGMainLoop

gi.require_version('Gst', '1.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gdk, GLib, Gst
from PIL import Image

DEST = 'org.freedesktop.portal.Desktop'
PATH = '/org/freedesktop/portal/desktop'
REMOTE = 'org.freedesktop.portal.RemoteDesktop'
CAST = 'org.freedesktop.portal.ScreenCast'
BUTTONS = {'left': 272, 'right': 273, 'middle': 274}


class CompletedInputError(RuntimeError):
    """The input and releases succeeded, but the observation wait failed."""
    input_completed = True


def state_directory():
    path = Path('/tmp') / f'linux-computer-use-{os.getuid()}'
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError('Desktop control state directory must be private and owned by this user')
    return path


def stop_input(reset=False):
    path = state_directory() / 'stop'
    if reset:
        path.unlink(missing_ok=True)
    else:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.close(fd)
    return {'stopped': not reset}


def screenshot_width(value):
    number(value, 320, 3840, 'max_width')
    if not isinstance(value, int):
        raise ValueError('max_width must be an integer')
    return value


def number(value, low, high, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{name} must be a number')
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'{name} must be between {low} and {high}')
    return value


def frame_to_image(sample):
    """Let GStreamer's PNG encoder handle pixel formats and padded rows."""
    buffer = sample.get_buffer()
    ok, mapped = buffer.map(Gst.MapFlags.READ)
    if not ok:
        raise RuntimeError('Cannot map captured frame')
    try:
        with Image.open(io.BytesIO(bytes(mapped.data))) as image:
            return image.convert('RGB')
    finally:
        buffer.unmap(mapped)


class Desktop:
    def __init__(self):
        DBusGMainLoop(set_as_default=True)
        Gst.init(None)
        self.bus = None
        self.remote = None
        self.session = None
        self.pipeline = None
        self.sink = None
        self.fd = None
        self.closed_match = None
        self.stream = None
        self.source = None
        self.devices = 0
        self.logical_size = None
        self.image_size = None
        self.pressed_keys = set()
        self.pressed_buttons = set()
        self.owner_fd = None
        self.stop_path = None
        self.action_deadline = None

    def _bus(self):
        if self.bus is None:
            try:
                self.bus = dbus.SessionBus()
            except dbus.DBusException as exc:
                raise RuntimeError('Cannot access the desktop session bus. Run this '
                                   'program as your desktop user, outside a sandbox '
                                   'that blocks /run/user/<uid>/bus.') from exc
        return self.bus

    def doctor(self):
        obj = self._bus().get_object(DEST, PATH)
        props = dbus.Interface(obj, 'org.freedesktop.DBus.Properties')
        return {
            'session_type': os.environ.get('XDG_SESSION_TYPE', 'unknown'),
            'desktop': os.environ.get('XDG_CURRENT_DESKTOP', 'unknown'),
            'remote_desktop': {str(k): int(v) for k, v in props.GetAll(REMOTE).items()},
            'screen_cast': {str(k): int(v) for k, v in props.GetAll(CAST).items()},
            'gstreamer': Gst.version_string(),
            'plugins': {name: bool(Gst.ElementFactory.find(name)) for name in
                        ('pipewiresrc', 'videoconvert', 'videorate', 'pngenc', 'appsink')},
            'connected': self.session is not None,
        }

    def _request(self, interface, method, *args, options=None, timeout=45):
        bus = self._bus()
        token = 'lc_' + uuid.uuid4().hex
        sender = bus.get_unique_name()[1:].replace('.', '_')
        request_path = f'{PATH}/request/{sender}/{token}'
        opts = dbus.Dictionary(options or {}, signature='sv')
        opts['handle_token'] = dbus.String(token)
        result = []
        loop = GLib.MainLoop()

        def response(code, values):
            result.append((int(code), values))
            loop.quit()

        match = bus.add_signal_receiver(response, signal_name='Response',
                                        dbus_interface='org.freedesktop.portal.Request',
                                        path=request_path, bus_name=DEST)
        expired = []

        def expire():
            expired.append(True)
            loop.quit()
            return False

        timer = GLib.timeout_add_seconds(timeout, expire)
        try:
            obj = bus.get_object(DEST, PATH)
            returned_path = str(getattr(dbus.Interface(obj, interface), method)(*args, opts))
            if returned_path != request_path:
                raise RuntimeError('Portal returned an unexpected request path')
            if not result:
                loop.run()
            if not result:
                try:
                    dbus.Interface(bus.get_object(DEST, request_path),
                                   'org.freedesktop.portal.Request').Close()
                except dbus.DBusException:
                    pass
                raise RuntimeError('Portal approval timed out; retry and accept the '
                                   'Ubuntu screen-sharing dialog within 45 seconds.')
            code, values = result[0]
            if code != 0:
                raise RuntimeError(f'Portal request cancelled or denied (response {code})')
            return values
        finally:
            match.remove()
            if not expired:
                GLib.source_remove(timer)

    def probe(self, source='window'):
        """Exercise session setup without presenting a sharing dialog or input."""
        try:
            self._create()
            self._select(source)
            return {'session_setup': 'passed', 'sharing_started': False,
                    'input_sent': False}
        finally:
            self.close()

    def _create(self):
        if self.session:
            raise RuntimeError('A desktop session is already connected')
        values = self._request(REMOTE, 'CreateSession', options={
            'session_handle_token': dbus.String('lc_' + uuid.uuid4().hex)})
        self.session = dbus.ObjectPath(values['session_handle'])
        self.remote = dbus.Interface(self._bus().get_object(DEST, PATH), REMOTE)
        self.closed_match = self.bus.add_signal_receiver(
            self._closed, signal_name='Closed',
            dbus_interface='org.freedesktop.portal.Session',
            path=str(self.session), bus_name=DEST)

    def _select(self, source='window'):
        if source not in ('window', 'monitor'):
            raise ValueError('source must be window or monitor')
        self._request(REMOTE, 'SelectDevices', self.session, options={
            'types': dbus.UInt32(3)})
        # Request only the selected kind. Never fall back to monitor capture.
        self._request(CAST, 'SelectSources', self.session, options={
            'types': dbus.UInt32(2 if source == 'window' else 1),
            'multiple': dbus.Boolean(False),
            'cursor_mode': dbus.UInt32(2)})

    def _accept_stream(self, values, source):
        streams = values.get('streams', [])
        if len(streams) != 1:
            raise RuntimeError(f'Select exactly one {source} in the sharing dialog')
        props = streams[0][1]
        expected = 2 if source == 'window' else 1
        if int(props.get('source_type', 0)) != expected:
            raise RuntimeError(f'Portal did not confirm {source}-only capture. '
                               'Sharing stopped; no fallback to desktop capture.')
        self.stream = int(streams[0][0])
        self.source = source
        self.devices = int(values.get('devices', 0))
        size = props.get('logical_size', props.get('size'))
        if size is not None:
            if len(size) != 2 or any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in size):
                raise RuntimeError('Portal returned invalid logical geometry')
            self.logical_size = tuple(int(v) for v in size)

    def _acquire_owner(self):
        directory = state_directory()
        self.stop_path = directory / 'stop'
        if self.stop_path.exists():
            raise RuntimeError('Input stopped. Inspect the desktop, then run reset-stop to resume.')
        fd = os.open(directory / 'owner.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise RuntimeError('Another linux-computer-use session owns desktop input')
        self.owner_fd = fd

    def connect(self, source='window'):
        if source not in ('window', 'monitor'):
            raise ValueError('source must be window or monitor')
        self._pump()
        if self.session:
            if self.source != source:
                raise RuntimeError('Disconnect before changing the capture source')
            return self.info()
        try:
            self._acquire_owner()
            self._create()
            self._select(source)
            values = self._request(REMOTE, 'Start', self.session, dbus.String(''))
            self._accept_stream(values, source)
            cast = dbus.Interface(self.bus.get_object(DEST, PATH), CAST)
            self.fd = cast.OpenPipeWireRemote(self.session,
                                             dbus.Dictionary({}, signature='sv')).take()
            self.pipeline = Gst.parse_launch(
                f'pipewiresrc fd={self.fd} path={self.stream} do-timestamp=true '
                '! videorate ! video/x-raw,framerate=5/1 '
                '! videoconvert ! video/x-raw,format=RGB ! pngenc compression-level=1 '
                '! appsink name=capture max-buffers=1 drop=true sync=false')
            self.sink = self.pipeline.get_by_name('capture')
            if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError('Cannot start PipeWire capture')
            self._frame()
            return self.info()
        except BaseException:
            self.close()
            raise

    def _pump(self):
        context = GLib.MainContext.default()
        while context.pending():
            context.iteration(False)

    def _require(self, device=0):
        self._pump()
        if not self.session:
            raise RuntimeError('Desktop is disconnected. Call linux_desktop_connect first.')
        if device and not self.devices & device:
            raise RuntimeError('The sharing dialog did not grant this input device')

    def info(self):
        self._pump()
        return {'connected': bool(self.session), 'stream': self.stream,
                'capture_source': self.source,
                'keyboard_allowed': bool(self.devices & 1),
                'pointer_allowed': bool(self.devices & 2),
                'logical_size': self.logical_size,
                'screenshot_size': self.image_size,
                'absolute_pointer_available': bool(self.logical_size and self.image_size),
                'focus_verified': False,
                'capture_fps': 5,
                'stop_file': str(self.stop_path) if self.stop_path else None,
                'input_target': 'currently focused application on the desktop'}

    def _frame(self):
        self._require()
        deadline = time.monotonic() + 8
        # Discard the queued frame so an observation follows the previous action.
        self.sink.emit('try-pull-sample', 0)
        while time.monotonic() < deadline:
            sample = self.sink.emit('try-pull-sample', 100 * Gst.MSECOND)
            if sample:
                return frame_to_image(sample)
            self._pump()
            self._require()
            msg = self.pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR)
            if msg:
                err, debug = msg.parse_error()
                raise RuntimeError(f'PipeWire capture failed: {err.message} ({debug})')
        raise RuntimeError('No screen frame received within 8 seconds')

    def screenshot(self, max_width=1280):
        screenshot_width(max_width)
        image = self._frame()
        image.thumbnail((max_width, 2160))
        self.image_size = image.size
        output = io.BytesIO()
        image.save(output, format='PNG')
        return output.getvalue(), self.info()

    def _notify(self, method, *args, device=0):
        self._require(device)
        getattr(self.remote, method)(self.session,
                                    dbus.Dictionary({}, signature='sv'), *args)

    def move(self, x=None, y=None, dx=None, dy=None, duration=0.15):
        self._require(2)
        absolute = x is not None or y is not None
        relative = dx is not None or dy is not None
        if absolute == relative:
            raise ValueError('Provide x and y OR dx and dy')
        if relative:
            return self.act(dx=0 if dx is None else dx, dy=0 if dy is None else dy,
                            duration=duration, settle=0)
        else:
            if not self.image_size or not self.logical_size:
                raise RuntimeError('Take a screenshot first; portal must report logical size')
            width, height = self.image_size
            number(x, 0, width - 1, 'x')
            number(y, 0, height - 1, 'y')
            lx, ly = self.logical_size
            self._guard_input()
            self._notify('NotifyPointerMotionAbsolute', dbus.UInt32(self.stream),
                         dbus.Double(x * lx / width), dbus.Double(y * ly / height), device=2)
        return self.info()

    def _sleep(self, duration):
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            self._pump()
            self._guard_input()
            time.sleep(min(0.02, max(0, deadline - time.monotonic())))

    def _guard_input(self):
        self._require()
        if self.stop_path and self.stop_path.exists():
            raise RuntimeError('Input interrupted by stop file')
        if self.action_deadline is not None and time.monotonic() >= self.action_deadline:
            raise RuntimeError('Action wall-clock deadline reached')

    def _keycodes(self, keys):
        if not isinstance(keys, list) or len(keys) > 8:
            raise ValueError('keys must be a list of at most 8 names')
        aliases = {'enter': 'Return', 'escape': 'Escape', 'esc': 'Escape',
                   'space': 'space', 'ctrl': 'Control_L', 'shift': 'Shift_L',
                   'alt': 'Alt_L', 'super': 'Super_L', 'tab': 'Tab',
                   'backspace': 'BackSpace', 'up': 'Up', 'down': 'Down',
                   'left': 'Left', 'right': 'Right'}
        codes = []
        for key in keys:
            if not isinstance(key, str):
                raise ValueError('Each key must be a string')
            code = Gdk.keyval_from_name(aliases.get(key.lower(), key))
            if code in (0, 0xffffff):
                raise ValueError(f'Unknown key name: {key}')
            if code not in codes:
                codes.append(code)
        return codes

    def validate_action(self, keys=None, buttons=None, duration=0.08,
                        dx=0, dy=0, x=None, y=None, settle=0.15):
        keys = [] if keys is None else keys
        buttons = [] if buttons is None else buttons
        self._keycodes(keys)
        if (not isinstance(buttons, list) or len(buttons) > 3
                or any(not isinstance(b, str) or b not in BUTTONS for b in buttons)
                or len(set(buttons)) != len(buttons)):
            raise ValueError('buttons must be unique left/right/middle names')
        number(duration, 0.01, 5, 'duration')
        number(settle, 0, 2, 'settle')
        number(dx, -4096, 4096, 'dx')
        number(dy, -4096, 4096, 'dy')
        absolute = x is not None or y is not None
        if absolute:
            if dx or dy:
                raise ValueError('Absolute menu coordinates cannot combine with relative motion')
            if not self.image_size or not self.logical_size:
                raise RuntimeError('Take a screenshot first; portal must report logical size')
            number(x, 0, self.image_size[0] - 1, 'x')
            number(y, 0, self.image_size[1] - 1, 'y')
        self._require()
        if keys:
            self._require(1)
        if buttons or dx or dy or absolute:
            self._require(2)
        return dict(keys=keys, buttons=buttons, duration=duration, dx=dx, dy=dy,
                    x=x, y=y, settle=settle)

    def _release_inputs(self):
        errors = []
        for release, codes in ((self._release_button, list(self.pressed_buttons)),
                               (self._release_key, list(self.pressed_keys))):
            for code in codes:
                try:
                    release(code)
                except Exception as exc:
                    errors.append(str(exc))
        if errors:
            # Closing the portal ends remote input even when individual releases fail.
            self.close()
            raise RuntimeError('Input release failed; sharing closed: ' + '; '.join(errors))

    def act(self, keys=None, buttons=None, duration=0.08, dx=0, dy=0,
            x=None, y=None, settle=0.15, deadline=None):
        action = self.validate_action(keys, buttons, duration, dx, dy, x, y, settle)
        if deadline is not None:
            number(deadline, 0, float('inf'), 'deadline')
        codes = self._keycodes(action['keys'])
        old_deadline = self.action_deadline
        self.action_deadline = deadline
        started = time.monotonic()
        try:
            self._guard_input()
            if x is not None:
                self.move(x=x, y=y)
                self._sleep(0.03)
            for code in codes:
                self._guard_input()
                self.pressed_keys.add(code)
                self._notify('NotifyKeyboardKeysym', dbus.Int32(code), dbus.UInt32(1), device=1)
            for button in action['buttons']:
                self._guard_input()
                code = BUTTONS[button]
                self.pressed_buttons.add(code)
                self._notify('NotifyPointerButton', dbus.Int32(code), dbus.UInt32(1), device=2)
            # Spread camera motion across frames rather than one potentially lost event.
            count = max(1, math.ceil(duration / 0.02)) if dx or dy else 1
            for _ in range(count):
                self._guard_input()
                if dx or dy:
                    self._notify('NotifyPointerMotion', dbus.Double(dx / count),
                                 dbus.Double(dy / count), device=2)
                self._sleep(duration / count)
            self._guard_input()
        finally:
            try:
                self._release_inputs()
            finally:
                self.action_deadline = old_deadline
        # Inputs are already released while the view settles or capture fails.
        old_deadline = self.action_deadline
        self.action_deadline = deadline
        try:
            self._sleep(settle)
        except Exception as exc:
            raise CompletedInputError(str(exc)) from exc
        finally:
            self.action_deadline = old_deadline
        return {**self.info(), 'input_completed': True,
                'elapsed_seconds': round(time.monotonic() - started, 4)}

    def click(self, button='left', duration=0.08, x=None, y=None):
        return self.act(buttons=[button], duration=duration, x=x, y=y, settle=0)

    def key(self, keys, duration=0.08):
        if not isinstance(keys, list) or not keys:
            raise ValueError('keys must contain at least one key')
        return self.act(keys=keys, duration=duration, settle=0)

    def scroll(self, steps, axis='vertical'):
        number(steps, -20, 20, 'steps')
        if not isinstance(steps, int):
            raise ValueError('steps must be an integer')
        if axis not in ('vertical', 'horizontal'):
            raise ValueError('axis must be vertical or horizontal')
        self._guard_input()
        self._notify('NotifyPointerAxisDiscrete', dbus.UInt32(axis == 'horizontal'),
                     dbus.Int32(steps), device=2)
        return self.info()

    def _release_key(self, code):
        if code in self.pressed_keys:
            if self.session and self.remote:
                self.remote.NotifyKeyboardKeysym(self.session, dbus.Dictionary({}, signature='sv'),
                                                 dbus.Int32(code), dbus.UInt32(0))
            self.pressed_keys.discard(code)

    def _release_button(self, code):
        if code in self.pressed_buttons:
            if self.session and self.remote:
                self.remote.NotifyPointerButton(self.session, dbus.Dictionary({}, signature='sv'),
                                                dbus.Int32(code), dbus.UInt32(0))
            self.pressed_buttons.discard(code)

    def _closed(self, *args):
        self.session = None
        self._cleanup()

    def _cleanup(self):
        pipeline, fd, match, owner = self.pipeline, self.fd, self.closed_match, self.owner_fd
        self.pipeline = self.sink = None
        self.fd = self.closed_match = self.owner_fd = None
        self.remote = self.stream = self.source = None
        self.devices = 0
        self.logical_size = self.image_size = None
        self.pressed_keys.clear()
        self.pressed_buttons.clear()
        self.action_deadline = None
        # Tear down every resource even if another teardown operation fails.
        for operation in (lambda: pipeline.set_state(Gst.State.NULL) if pipeline else None,
                          lambda: os.close(fd) if fd is not None else None,
                          lambda: match.remove() if match else None,
                          lambda: os.close(owner) if owner is not None else None):
            try:
                operation()
            except Exception:
                pass

    def close(self):
        for code in list(self.pressed_keys):
            try:
                self._release_key(code)
            except Exception:
                pass
        for code in list(self.pressed_buttons):
            try:
                self._release_button(code)
            except Exception:
                pass
        if self.session:
            try:
                dbus.Interface(self._bus().get_object(DEST, self.session),
                               'org.freedesktop.portal.Session').Close()
            except Exception:
                pass
        self.session = None
        self._cleanup()


def image_content(png):
    return {'type': 'image', 'mimeType': 'image/png',
            'data': base64.b64encode(png).decode('ascii')}
