#!/usr/bin/python3
"""Small local stdio MCP server. No network listener or API key required."""
from __future__ import annotations

import argparse
import json
import os
import select
import signal
import sys
from pathlib import Path

from portal import CompletedInputError, Desktop, image_content, screenshot_width, stop_input
from sequence import execute


def tool(name, description, properties=None, required=(), read_only=False):
    return {'name': 'linux_desktop_' + name, 'description': description,
            'inputSchema': {'type': 'object', 'properties': properties or {},
                            'required': list(required), 'additionalProperties': False},
            'annotations': {'readOnlyHint': read_only, 'openWorldHint': False}}


NUM = {'type': 'number'}
WIDTH = {'type': 'integer', 'minimum': 320, 'maximum': 3840}
ACTION = {
    'keys': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 8},
    'buttons': {'type': 'array', 'items': {'type': 'string', 'enum': ['left', 'right', 'middle']}, 'maxItems': 3},
    'duration': {'type': 'number', 'minimum': 0.01, 'maximum': 5},
    'settle': {'type': 'number', 'minimum': 0, 'maximum': 2},
    **{k: NUM for k in ('dx', 'dy', 'x', 'y')},
}
REGIONS = {'type': 'array', 'minItems': 1, 'maxItems': 16, 'items': {
    'type': 'array', 'minItems': 4, 'maxItems': 4, 'items': {'type': 'integer'}}}
TOOLS = [
    tool('connect', 'Request a desktop session. The human must accept Ubuntu\'s '
         'screen-sharing dialog within 45 seconds. Default source=window: select '
         'only the target window (e.g. Minecraft). No desktop fallback. source=monitor '
         'explicitly requests an entire monitor. Input remains desktop-wide and goes '
         'to the focused app, even with window capture.',
         {'source': {'type': 'string', 'enum': ['window', 'monitor'], 'default': 'window'}}),
    tool('info', 'Report session state and coordinate dimensions.', read_only=True),
    tool('screenshot', 'Capture the approved window or monitor as a PNG. Use pixel coordinates '
         'from this returned image for absolute move/click. Capturing also shows other '
         'visible apps on that monitor when using source=monitor. Window mode returns '
         'only the selected window.', {'max_width': {'type': 'integer',
                                                       'minimum': 320, 'maximum': 3840}},
         read_only=True),
    tool('move', 'Move the mouse. Use x,y in the most recent screenshot\'s pixel '
         'coordinates for menus, or dx,dy for relative motion (Minecraft camera). '
         'Omitted relative axes are zero; duration spreads motion (default 0.15s).',
         {**{k: NUM for k in ('x', 'y', 'dx', 'dy')},
          'duration': {'type': 'number', 'minimum': 0.01, 'maximum': 5}}),
    tool('act', 'Perform a short combined action, release all inputs, settle, and return '
         'a fresh screenshot. Supports Shift+click and walking+mining. Camera motion '
         'is spread over duration; align before mining. x,y use the latest screenshot '
         'and are for visibly open menus. Observe before acting; focus is NOT verified.',
         {**ACTION, 'max_width': WIDTH}),
    tool('sequence', 'Run 1-16 previously inspected steps with fresh screenshots and '
         'a wall-clock budget (default 15s, max 30s). Coordinate steps REQUIRE '
         'stable_regions of the currently visible menu, compared to the initial '
         'screenshot. max_mean_error defaults to 8. Optional per-step progress '
         'checks pixel change, not item identity. Stops on mismatch/failure; never '
         'replays steps. Inspect the returned image/report before continuing.',
         {'steps': {'type': 'array', 'minItems': 1, 'maxItems': 16, 'items': {
             'type': 'object', 'additionalProperties': False, 'properties': {
                 **ACTION, 'progress': {'type': 'object', 'additionalProperties': False,
                    'required': ['regions', 'min_mean_error'], 'properties': {
                        'regions': REGIONS, 'min_mean_error': {'type': 'number', 'minimum': 0.01, 'maximum': 255}}}}}},
          'stable_regions': REGIONS, 'max_seconds': {'type': 'number', 'minimum': 1, 'maximum': 30},
          'max_mean_error': {'type': 'number', 'minimum': 0, 'maximum': 30}, 'max_width': WIDTH}, ['steps']),
    tool('click', 'Press then release a mouse button. duration in seconds (max 5); '
         'long presses can mine blocks. Optional x,y use screenshot coordinates.',
         {'button': {'type': 'string', 'enum': ['left', 'right', 'middle']},
          'duration': {'type': 'number', 'minimum': 0.01, 'maximum': 5}, 'x': NUM, 'y': NUM}),
    tool('key', 'Press then release a key or chord, e.g. keys=["w","space"], '
         'duration=1. Key names: letters, digits, enter, escape, space, ctrl, shift, '
         'alt, tab, backspace, arrows, F1...F12. Input goes to the focused app.',
         {'keys': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 8},
          'duration': {'type': 'number', 'minimum': 0.01, 'maximum': 5}}, ['keys']),
    tool('scroll', 'Scroll up/left with negative steps, down/right with positive steps.',
         {'steps': {'type': 'integer', 'minimum': -20, 'maximum': 20},
          'axis': {'type': 'string', 'enum': ['vertical', 'horizontal']}}, ['steps']),
    tool('disconnect', 'Release inputs, close the session and stop screen sharing.'),
]


class Server:
    def __init__(self, desktop=None):
        self.desktop = desktop if desktop is not None else Desktop()

    def call(self, name, args):
        schema = next((t['inputSchema'] for t in TOOLS if t['name'] == name), None)
        if schema is None:
            raise ValueError(f'Unknown tool: {name}')
        if not isinstance(args, dict):
            raise ValueError('arguments must be an object')
        extra = args.keys() - schema['properties'].keys()
        missing = set(schema['required']) - args.keys()
        if extra or missing:
            raise ValueError(f'Unexpected arguments: {sorted(extra)}; missing: {sorted(missing)}')
        action = name.removeprefix('linux_desktop_')
        if action == 'screenshot':
            png, meta = self.desktop.screenshot(**args)
            return {'content': [image_content(png),
                                {'type': 'text', 'text': json.dumps(meta)}]}
        if action == 'act':
            kwargs = dict(args)
            width = screenshot_width(kwargs.pop('max_width', 1280))
            action_error = None
            try:
                value = self.desktop.act(**kwargs)
            except CompletedInputError as exc:
                action_error = str(exc)
                value = {'input_completed': True, 'action_error': action_error,
                         'warning': 'Input completed; inspect the image before continuing. Do not replay automatically.'}
            try:
                png, meta = self.desktop.screenshot(max_width=width)
            except Exception as exc:
                return {'isError': True, 'content': [{'type': 'text', 'text': json.dumps({
                    **value, 'capture_fresh': False, 'capture_error': str(exc),
                    'warning': 'Input completed; inspect a fresh screenshot before continuing. Do not replay automatically.'})}]}
            return {'isError': action_error is not None,
                    'content': [image_content(png), {'type': 'text',
                    'text': json.dumps({**value, **meta, 'capture_fresh': True})}]}
        if action == 'sequence':
            png, report = execute(self.desktop, **args)
            content = [image_content(png)] if png is not None else []
            return {'content': [*content, {'type': 'text', 'text': json.dumps(report)}],
                    'isError': report['status'] == 'stopped'}
        if action == 'disconnect':
            self.desktop.close()
            value = self.desktop.info()
        else:
            value = getattr(self.desktop, action)(**args)
        return {'content': [{'type': 'text', 'text': json.dumps(value)}]}

    def handle(self, msg):
        if not isinstance(msg, dict) or msg.get('jsonrpc') != '2.0':
            return {'jsonrpc': '2.0', 'id': None,
                    'error': {'code': -32600, 'message': 'Invalid Request'}}
        if 'id' not in msg:
            return None
        ident = msg['id']
        try:
            method = msg.get('method')
            params = msg.get('params', {})
            if not isinstance(params, dict):
                raise ValueError('params must be an object')
            if method == 'initialize':
                requested = params.get('protocolVersion')
                supported = ('2024-11-05', '2025-03-26', '2025-06-18', '2025-11-25')
                result = {'protocolVersion': requested if requested in supported else '2025-06-18',
                          'capabilities': {'tools': {}},
                          'serverInfo': {'name': 'linux-computer-use', 'version': '0.3.1'},
                          'instructions': 'Use only for the human-authorized desktop task. '
                          'Connect defaults to a single window; the human selects it in '
                          'Ubuntu sharing approval. Never substitute monitor capture '
                          'unless requested. Screenshot, then '
                          'act in short steps, then screenshot again. Inputs affect the '
                          'focused app; there is no app isolation. Coordinates refer to '
                          'the latest screenshot. Disconnect when finished. This is an '
                          'independent Linux tool, not OpenAI Computer Use.'}
            elif method == 'ping':
                result = {}
            elif method == 'tools/list':
                result = {'tools': TOOLS}
            elif method == 'tools/call':
                try:
                    result = self.call(params.get('name'), params.get('arguments', {}))
                except Exception as exc:
                    result = {'content': [{'type': 'text', 'text': str(exc)}], 'isError': True}
            else:
                return {'jsonrpc': '2.0', 'id': ident,
                        'error': {'code': -32601, 'message': 'Method not found'}}
            return {'jsonrpc': '2.0', 'id': ident, 'result': result}
        except (TypeError, ValueError) as exc:
            return {'jsonrpc': '2.0', 'id': ident,
                    'error': {'code': -32602, 'message': str(exc)}}

    def _input_lines(self):
        """Keep portal signals flowing even while the client sends no messages."""
        fd = sys.stdin.fileno()
        pending = b''
        while True:
            self.desktop._pump()
            ready, _, _ = select.select([fd], [], [], 0.1)
            if not ready:
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                if pending:
                    yield pending
                return
            pending += chunk
            while b'\n' in pending:
                line, pending = pending.split(b'\n', 1)
                yield line

    def serve(self):
        try:
            for line in self._input_lines():
                try:
                    msg = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    response = {'jsonrpc': '2.0', 'id': None,
                                'error': {'code': -32700, 'message': 'Parse error'}}
                else:
                    response = self.handle(msg)
                if response is not None:
                    print(json.dumps(response, ensure_ascii=False), flush=True)
        finally:
            self.desktop.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['serve', 'doctor', 'probe', 'capture', 'stop', 'reset-stop'])
    parser.add_argument('--output', type=Path, default=Path('desktop-preview.png'))
    parser.add_argument('--source', choices=['window', 'monitor'], default='window')
    args = parser.parse_args()
    if args.command in ('stop', 'reset-stop'):
        print(json.dumps(stop_input(reset=args.command == 'reset-stop')))
        return 0
    desktop = Desktop()

    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        if args.command == 'serve':
            Server(desktop).serve()
        elif args.command == 'doctor':
            print(json.dumps(desktop.doctor(), indent=2))
        elif args.command == 'probe':
            print(json.dumps(desktop.probe(source=args.source), indent=2))
        else:
            target = 'Minecraftなどの対象ウィンドウ' if args.source == 'window' else 'モニター'
            print(f'Ubuntuの画面共有ダイアログで{target}を選択して許可してください。'
                  '45秒でタイムアウトします。入力は送信しません。', file=sys.stderr, flush=True)
            desktop.connect(source=args.source)
            png, metadata = desktop.screenshot()
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_bytes(png)
            print(json.dumps({'output': str(args.output.resolve()), **metadata}, indent=2))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        desktop.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
