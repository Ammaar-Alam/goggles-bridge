"""Virtual-source routing, cancellation and policy tests; no display or USB mutation."""
from contextlib import contextmanager
from pathlib import Path
import json
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from goggles_bridge.engine import BridgeEngine, BridgeSettings, encoder_command
from goggles_bridge.app import backend
from goggles_bridge.app.virtual_display import capture
from goggles_bridge.app.virtual_display.system import Cancelled


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.settings = BridgeSettings(None, Path('ffmpeg.exe'), fps=100, bitrate_mbps=32,
                                       h264_profile='baseline', adaptive_quant=True,
                                       low_latency=False, usb_write_size=2048)
        self.options = dict(source='virtual', virtual_resolution='720p')
        self.stop = threading.Event()
        self.target = dict(hmonitor=9876)

    def test_only_monitor_selector_and_border_change(self):
        for frames in (0, 1):
            original = encoder_command(self.settings, frames=frames)
            adapted = capture.command_for_target(original, target=self.target)
            index = original.index('-i') + 1
            self.assertEqual(original[:index], adapted[:index])
            self.assertEqual(original[index+1:], adapted[index+1:])
            self.assertIn('hmonitor=9876', adapted[index])
            self.assertNotIn('monitor_idx=', adapted[index])
            self.assertIn('width=1920:height=1080', adapted[index])
            self.assertTrue(adapted[index].endswith(':display_border=0'))

    def test_rejects_unstable_target_before_launch(self):
        for target in ({}, dict(hmonitor=0), dict(hmonitor='9876')):
            with self.assertRaises(ValueError):
                capture.command_for_target(encoder_command(self.settings), target=target)

    def test_starting_log_records_the_1080p_transport(self):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder)/'stream.jsonl'
            stop = threading.Event()
            stop.set()
            transport = Mock()
            engine = BridgeEngine(self.settings, log,
                stop_event=stop, transport_factory=transport)
            engine.run()
            rows = [json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()]
            self.assertEqual(rows[0]['resolution'], '1920x1080')
            transport.assert_not_called()

    def test_desktop_size_changes_without_changing_preflight_or_transport_size(self):
        from contextlib import nullcontext
        for resolution, dimensions in (('720p', (1280,720)), ('1080p', (1920,1080))):
            commands = []
            def check(settings, run):
                run(encoder_command(settings, frames=1))
            def engine(settings, log, **kwargs):
                kwargs['process_factory'](encoder_command(settings))
                return Mock()
            with patch.object(capture, 'lease', return_value=nullcontext(self.target)) as lease, \
                 patch.object(capture, 'select_source', return_value=self.target) as select, \
                 patch.object(capture, 'run_cancellable', side_effect=lambda cmd,*a,**k:
                              (commands.append(cmd), subprocess.CompletedProcess(cmd, 0, '', ''))[1]), \
                 patch.object(capture.subprocess, 'Popen', side_effect=lambda cmd,**k: commands.append(cmd)):
                capture.run(self.settings, dict(virtual_resolution=resolution), Path('unused'),
                            self.stop, Mock(), engine_factory=engine, check=check)
            self.assertEqual(lease.call_args.args[:3], (*dimensions,100))
            self.assertEqual(select.call_count,2)
            for command in commands:
                graph = command[command.index('-i')+1]
                self.assertIn('width=1920:height=1080:resize_mode=scale_aspect',graph)
                self.assertIn('hmonitor=9876',graph)

    def test_new_monitor_wgc_readiness_retries_with_revalidated_ownership(self):
        from contextlib import nullcontext
        transient = subprocess.CompletedProcess([], 1, '',
            'Failed to setup graphics capture for monitor (0x80070057)')
        success = subprocess.CompletedProcess([], 0, '', '')
        def check(settings, run):
            self.assertEqual(run(encoder_command(settings, frames=1)).returncode, 0)
        with patch.object(capture, 'lease', return_value=nullcontext(self.target)), \
             patch.object(capture, 'select_source', side_effect=[self.target, dict(hmonitor=1234)]) as select, \
             patch.object(capture, 'run_cancellable', side_effect=[transient, success]) as run, \
             patch.object(self.stop, 'wait', return_value=False):
            capture.run(self.settings, self.options, Path('unused'), self.stop, Mock(),
                        engine_factory=Mock(), check=check)
        self.assertEqual(select.call_count, 2)
        self.assertIn('hmonitor=1234', run.call_args.args[0][run.call_args.args[0].index('-i')+1])

    def test_wgc_readiness_does_not_retry_unrelated_errors_or_ignore_stop(self):
        from contextlib import nullcontext
        for message, stopped, calls in (
            ('NVENC is unavailable', False, 1),
            ('Failed to setup graphics capture for monitor (0x80070057)', False, 4),
            ('Failed to setup graphics capture for monitor (0x80070057)', True, 1),
        ):
            with self.subTest(message=message, stopped=stopped), \
                 patch.object(capture, 'lease', return_value=nullcontext(self.target)), \
                 patch.object(capture, 'select_source', return_value=self.target), \
                 patch.object(capture, 'run_cancellable', return_value=subprocess.CompletedProcess([], 1, '', message)) as run, \
                 patch.object(self.stop, 'wait', return_value=stopped):
                def check(settings, run):
                    run(encoder_command(settings, frames=1))
                    return 'failed'
                if stopped:
                    capture.run(self.settings, self.options, Path('unused'), self.stop, Mock(), check=check)
                else:
                    with self.assertRaisesRegex(RuntimeError, 'failed'):
                        capture.run(self.settings, self.options, Path('unused'), self.stop, Mock(), check=check)
                self.assertEqual(run.call_count, calls)

    def run_stream(self, engine_error=None, check_error=None):
        events, released = [], []

        @contextmanager
        def lease(*args):
            try:
                yield self.target
            finally:
                released.append(True)

        engine = Mock()
        engine.return_value.run.side_effect = engine_error
        check = Mock(return_value=check_error)
        with patch.object(capture, 'lease', side_effect=lease):
            try:
                capture.run(self.settings, self.options, Path('ignored'), self.stop, events.append,
                            engine_factory=engine, check=check)
            finally:
                self.assertEqual(released, [True])
        return engine, check

    def test_disconnect_and_stream_error_release_the_display(self):
        with self.assertRaisesRegex(RuntimeError, 'USB disconnected'):
            self.run_stream(engine_error=RuntimeError('USB disconnected'))

    def test_preflight_failure_releases_the_display(self):
        with self.assertRaisesRegex(RuntimeError, 'capture unavailable'):
            self.run_stream(check_error='capture unavailable')

    def test_virtual_route_preserves_encoder_preferences_and_stops_on_disconnect(self):
        engine, check = self.run_stream()
        passed = engine.call_args.args[0]
        self.assertEqual(passed, self.settings)
        self.assertFalse(engine.call_args.kwargs['reconnect'])
        self.assertIs(engine.call_args.kwargs['stop_event'], self.stop)
        self.assertIs(check.call_args.args[0], passed)

    def test_cancellation_before_start_does_not_create_a_display(self):
        self.stop.set()
        with patch.object(capture, 'lease') as lease:
            capture.run(self.settings, self.options, Path('ignored'), self.stop, Mock())
        lease.assert_not_called()

    def test_cancelled_preflight_does_not_hide_cleanup_failure(self):
        @contextmanager
        def lease(*args):
            try:
                yield self.target
            finally:
                args[-1]('display_cleanup_error', message='detach failed')
        with patch.object(capture, 'lease', side_effect=lease):
            with self.assertRaisesRegex(RuntimeError, 'detach failed'):
                capture.run(self.settings, self.options, Path('ignored'), self.stop, Mock(),
                            check=Mock(side_effect=Cancelled()))


class BackendTests(unittest.TestCase):
    def test_driver_failure_shows_only_a_fresh_valid_diagnostic(self):
        import os
        from goggles_bridge.app.virtual_display import virtual_driver, paths
        for content, modified, expected in (
            ('{"success":false,"error":"Close the previous app first"}', 101, 'Close the previous app first'),
            ('{"success":false,"error":"old error"}', 99, 'Virtual display setup failed'),
            ('broken json', 101, 'Virtual display setup failed'),
            ('null', 101, 'Virtual display setup failed'),
        ):
            with self.subTest(content=content, modified=modified), tempfile.TemporaryDirectory() as temp, \
                 patch.object(backend, 'WINDOWS', True), patch.object(backend.time, 'time', return_value=100), \
                 patch.object(paths, 'install_folder', return_value=Path(temp)), \
                 patch.object(virtual_driver, 'VirtualInstaller', return_value=Mock(poll=Mock(return_value=1))):
                report = Path(temp)/'display-install.json'
                report.write_text(content, encoding='utf-8')
                os.utime(report, (modified, modified))
                app = backend.Desktop(settings_path=Path(temp)/'settings.json')
                app.install_virtual_display()
                for _ in range(500):
                    if not app._installing_display:
                        break
                    threading.Event().wait(.01)
                self.assertIn(expected, app._state['problem']['message'])
                self.assertFalse(app._installing_display)

    def test_source_round_trip_preserves_all_physical_preferences(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(backend, 'WINDOWS', True):
            app = backend.Desktop(settings_path=Path(temp)/'settings.json')
            preferred = dict(capture='ddagrab', h264_profile='baseline', adaptive_quant=True,
                             low_latency=False, keyframe_seconds=1, usb_write_size=2048)
            app._settings(preferred)
            app._settings(dict(source='virtual', virtual_resolution='720p'))
            app._save()
            restored = backend.Desktop(settings_path=Path(temp)/'settings.json')
            restored._settings(dict(source='display'))
            for key, value in preferred.items():
                self.assertEqual(restored._options[key], value)

    def test_settings_cannot_change_under_an_active_lease(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(backend, 'WINDOWS', True):
            app = backend.Desktop(settings_path=Path(temp)/'settings.json')
            with patch.object(app, '_running', return_value=True):
                self.assertIn('error', app.set_options(dict(source='virtual')))
            self.assertEqual(app._options['source'], 'display')

    def test_nonwindows_settings_do_not_add_virtual_source(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(backend, 'WINDOWS', False):
            app = backend.Desktop(settings_path=Path(temp)/'settings.json')
            app._settings(dict(source='virtual'))
            self.assertNotIn('source', app._options)

    def test_updates_and_display_maintenance_exclude_each_other(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(backend, 'WINDOWS', True):
            app = backend.Desktop(settings_path=Path(temp)/'settings.json')
            app._installing_display = True
            with patch.object(backend.updates, 'download') as download:
                self.assertIn('error', app.install_update())
                download.assert_not_called()
            app._installing_display = False
            app._updating = True
            with patch.object(backend.threading, 'Thread') as thread:
                self.assertIn('error', app.install_virtual_display())
                thread.assert_not_called()

    def test_stream_failure_does_not_replace_cleanup_error_in_final_state(self):
        @contextmanager
        def lease(*args):
            try:
                yield dict(hmonitor=9876)
            finally:
                args[-1]('display_cleanup_error', message='detach failed')
        with tempfile.TemporaryDirectory() as temp, patch.object(backend, 'WINDOWS', True):
            engine = Mock()
            engine.return_value.run.side_effect = RuntimeError('USB disconnected')
            app = backend.Desktop(settings_path=Path(temp)/'settings.json', engine_factory=engine,
                                  check=Mock(return_value=None))
            settings = app._settings(dict(source='virtual'))
            with patch.object(capture, 'lease', side_effect=lease):
                app._run(settings, Path(temp)/'ignored')
            self.assertEqual(app._state['phase'], 'error')
            self.assertIn('USB disconnected', app._state['problem']['message'])
            self.assertIn('detach failed', app._state['problem']['message'])


if __name__ == '__main__':
    unittest.main()
