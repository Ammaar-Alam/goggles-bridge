"""Display contracts tested with fake inventories; never change host displays."""
import ctypes
from contextlib import nullcontext
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from goggles_bridge.app.virtual_display import display_config, display_session, displays, virtual_driver

OWNED_ID = r'ROOT\GOGGLESBRIDGE\0123456789abcdef0123456789abcdef'
OWNED_IDENTITY = (OWNED_ID.casefold(), 256)


def screen(device='physical', virtual=False, **fields):
    row = dict(device=device,
               hardware_id='Root\\MttVDD' if virtual else 'PCI\\GPU',
               monitor_id='MONITOR\\TEST', primary=not virtual, active=True,
               hmonitor=123, width=1920 if virtual else 3440, height=1080,
               x=3440 if virtual else 0, y=0, hz=100)
    row.update(fields)
    return row


class OwnershipTests(unittest.TestCase):
    def test_reused_sequential_id_is_rejected_before_display_changes(self):
        with patch.object(virtual_driver, 'owner_value', return_value=r'ROOT\DISPLAY\0003'), \
             patch.object(virtual_driver, '_owns_configuration', return_value=True), \
             patch.object(virtual_driver, '_instance_registered', return_value=True), \
             patch.object(displays, 'inventory', return_value=[screen(), screen('foreign', True)]), \
             patch.object(display_config, 'identity', return_value=(r'root\display\0003', 256)) as identity, \
             patch.object(displays, 'api') as api:
            with self.assertRaises(displays.DisplayError):
                displays.enable_extended()
            identity.assert_not_called()
            api.assert_not_called()


class DisplayTests(unittest.TestCase):
    def setUp(self):
        for module, name, value in (
            (virtual_driver, 'owned_instance', OWNED_ID),
            (virtual_driver, '_owns_configuration', True),
            (virtual_driver, '_instance_registered', True),
            (display_config, 'identity', OWNED_IDENTITY),
        ):
            patcher = patch.object(module,name,return_value=value)
            setattr(self,name,patcher.start())
            self.addCleanup(patcher.stop)

    def test_only_visible_foreign_display_is_never_adopted(self):
        self.identity.return_value=(r'root\display\0002',256)
        with patch.object(displays,'inventory',return_value=[screen(),screen('foreign',True)]), \
             patch.object(displays,'api') as api:
            with self.assertRaises(displays.DisplayError):
                displays.enable_extended()
            api.assert_not_called()

    def test_owned_display_is_selected_among_same_driver_instances(self):
        self.identity.side_effect=lambda device: (OWNED_ID.casefold() if device=='owned' else r'root\display\0002',256)
        target=displays.virtual_display([screen('foreign',True),screen('owned',True)])
        self.assertEqual(target['device'],'owned')

    def test_missing_or_foreign_configuration_blocks_selection(self):
        self._owns_configuration.return_value=False
        with self.assertRaises(displays.DisplayError):
            displays.virtual_display([screen('virtual',True)])
        self.identity.assert_not_called()

    def test_all_eight_modes_preserve_primary_layout(self):
        for width, height in ((1280, 720), (1920, 1080)):
            for fps in (60, 75, 90, 100):
                with self.subTest(width=width, fps=fps):
                    physical = screen()
                    virtual = screen('virtual', True, width=width, height=height, hz=fps)
                    mode = displays.DEVMODE(dmSize=ctypes.sizeof(displays.DEVMODE),
                        dmPelsWidth=width, dmPelsHeight=height, dmDisplayFrequency=fps)
                    api = Mock()
                    api.ChangeDisplaySettingsExW.return_value = 0
                    with patch.object(displays, 'inventory', return_value=[physical, virtual]), \
                         patch.object(displays, 'mode', return_value=mode), \
                         patch.object(displays, 'api', return_value=api):
                        result = displays.enable_extended(width, height, fps)
                    self.assertEqual(result, dict(virtual,identity=OWNED_IDENTITY))
                    self.assertEqual((mode.x, mode.y), (3440, 0))
                    for call in api.ChangeDisplaySettingsExW.call_args_list:
                        self.assertEqual(call.args[0], 'virtual')
                        self.assertFalse(call.args[3] & 0x10)  # Never CDS_SET_PRIMARY.

    def test_primary_or_ambiguous_virtual_display_is_never_detached(self):
        for rows in ([screen('virtual', True, primary=True)],
                     [screen(), screen('v1', True), screen('v2', True)]):
            with patch.object(displays, 'inventory', return_value=rows), \
                 patch.object(displays, 'api') as api:
                with self.assertRaises(displays.DisplayError):
                    displays.disable_extended(expected=screen('virtual',True,identity=OWNED_IDENTITY))
                api.assert_not_called()

    def test_cleanup_does_not_detach_a_replacement_device(self):
        with patch.object(displays, 'inventory', return_value=[screen(), screen('new', True)]), \
             patch.object(displays, 'api') as api:
            with self.assertRaises(displays.DisplayError):
                displays.disable_extended(expected=screen('old', True,identity=(r'root\display\0002',256)))
            api.assert_not_called()

    def test_cleanup_handles_already_removed_monitor(self):
        with patch.object(displays, 'inventory', return_value=[screen()]), \
             patch.object(displays, 'api') as api:
            displays.disable_extended(expected=screen('virtual', True,identity=OWNED_IDENTITY))
            api.assert_not_called()

    def test_failed_attach_rolls_back_changed_physical_refresh(self):
        physical = screen()
        virtual = screen('virtual', True, active=False, hmonitor=None)
        changed = dict(physical, hz=60)
        mode = displays.DEVMODE(dmSize=ctypes.sizeof(displays.DEVMODE),
            dmPelsWidth=1920, dmPelsHeight=1080, dmDisplayFrequency=100)
        rollback = Mock()
        with patch.object(displays, 'inventory', side_effect=[[physical, virtual], [changed, virtual]]), \
             patch.object(displays, 'mode', return_value=mode), \
             patch.object(display_config, 'attach', return_value=rollback):
            with self.assertRaises(displays.DisplayError):
                displays.enable_extended(1920, 1080, 100)
        rollback.assert_called_once_with()



class DisplayLeaseTests(unittest.TestCase):
    def setUp(self):
        self.target = screen('virtual', True)
        for module, name, value in (
            (display_session.virtual_driver, 'status', dict(owned=True, config_current=True)),
            (displays, 'enable_extended', self.target),
            (displays, 'disable_extended', None),
        ):
            patcher = patch.object(module, name, return_value=value)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        mutex = patch.object(display_session, '_process_lease', side_effect=nullcontext)
        mutex.start()
        self.addCleanup(mutex.stop)

    def test_normal_and_failed_stream_both_release_the_leased_identity(self):
        for fail in (False, True):
            self.disable_extended.reset_mock()
            try:
                with display_session.lease(fps=100) as target:
                    self.assertEqual(target,self.target)
                    if fail:
                        raise ValueError('stream failed')
            except ValueError:
                self.assertTrue(fail)
            self.disable_extended.assert_called_once_with(expected=self.target)

    def test_cleanup_callback_failure_preserves_original_error(self):
        self.disable_extended.side_effect=RuntimeError('detach failed')
        with self.assertRaisesRegex(ValueError,'stream failed'):
            with display_session.lease(emit=Mock(side_effect=RuntimeError('callback failed'))):
                raise ValueError('stream failed')
        self.disable_extended.assert_called_once_with(expected=self.target)

    def test_enable_failure_does_not_detach_an_unleased_display(self):
        self.enable_extended.side_effect = RuntimeError('rolled back inside enable')
        with self.assertRaises(RuntimeError):
            with display_session.lease():
                self.fail('unreachable')
        self.disable_extended.assert_not_called()

    def test_cleanup_error_preserves_original_stream_error(self):
        self.disable_extended.side_effect = RuntimeError('display was replaced')
        with self.assertRaisesRegex(ValueError, 'stream failed'):
            with display_session.lease():
                raise ValueError('stream failed')
        # Failed teardown must release the guard so a subsequent repair can run.
        self.disable_extended.side_effect = None
        with display_session.lease():
            pass

    def test_existing_third_party_driver_is_not_leased(self):
        self.status.return_value = dict(owned=False, config_current=True)
        with self.assertRaises(displays.DisplayError):
            with display_session.lease():
                self.fail('unreachable')
        self.enable_extended.assert_not_called()


class DisplayConfigTests(unittest.TestCase):
    def test_exact_paths_and_timings_survive_attach_and_rollback(self):
        primary, virtual, unrelated = display_config.PATH(flags=1), display_config.PATH(), display_config.PATH()
        primary.sourceInfo.id = 2
        primary.sourceInfo.modeInfoIdx = 0
        primary.targetInfo.modeInfoIdx = 1
        virtual.targetInfo.targetAvailable = unrelated.targetInfo.targetAvailable = True
        virtual.sourceInfo.id, unrelated.sourceInfo.id = 7, 9
        modes = [display_config.MODE(infoType=1), display_config.MODE(infoType=2)]
        modes[0].sourceMode.width = 3440
        modes[1].raw[0] = 123456789
        with patch.object(display_config, 'query', return_value=([primary,virtual,unrelated],modes)), \
             patch.object(display_config, 'source_name', side_effect=lambda p: str(p.sourceInfo.id)), \
             patch.object(display_config, 'path_identity',return_value=('owned',256)), \
             patch.object(display_config, 'identity',return_value=('owned',256)), \
             patch.object(display_config, 'configure') as configure:
            rollback = display_config.attach('7',1920,1080,100,3440,0,('owned',256))
            proposed, new_modes, flags = configure.call_args.args
            self.assertEqual(len(proposed),2)
            self.assertEqual(bytes(proposed[0]),bytes(primary))
            self.assertEqual([bytes(m) for m in new_modes[:2]],[bytes(m) for m in modes])
            self.assertEqual((proposed[1].targetInfo.refreshRate.Numerator,
                              proposed[1].targetInfo.refreshRate.Denominator),(100,1))
            self.assertFalse(flags & 0x400)
            rollback()
            self.assertEqual(configure.call_args.args[0],[primary])

    def test_replaced_path_cannot_be_attached(self):
        physical,virtual=display_config.PATH(flags=1),display_config.PATH()
        virtual.targetInfo.targetAvailable=True
        with patch.object(display_config,'query',return_value=([physical,virtual],[])), \
             patch.object(display_config,'source_name',side_effect=lambda p: 'virtual' if p is virtual else 'physical'), \
             patch.object(display_config,'path_identity',return_value=('foreign',256)), \
             patch.object(display_config,'configure') as configure:
            with self.assertRaisesRegex(RuntimeError,'replaced'):
                display_config.attach('virtual',1920,1080,60,1920,0,('owned',256))
            configure.assert_not_called()

    @unittest.skipUnless(os.name == 'nt', 'Windows ABI')
    def test_windows_structure_sizes(self):
        self.assertEqual(ctypes.sizeof(display_config.PATH),72)
        self.assertEqual(ctypes.sizeof(display_config.MODE),64)


class DisplayMutexTests(unittest.TestCase):
    def test_cross_process_contention_never_releases_other_owners_mutex(self):
        kernel = Mock()
        kernel.CreateMutexW.return_value = 456
        kernel.WaitForSingleObject.return_value = 258
        with patch.object(display_session, '_lease_api', return_value=kernel):
            with self.assertRaises(displays.DisplayError):
                with display_session._process_lease():
                    self.fail('unreachable')
        kernel.WaitForSingleObject.assert_called_once_with(456,0)
        kernel.ReleaseMutex.assert_not_called()
        kernel.CloseHandle.assert_called_once_with(456)

    def test_owned_or_abandoned_mutex_releases_after_stream_error(self):
        for result in (0,0x80):
            with self.subTest(wait_result=result):
                kernel = Mock()
                kernel.CreateMutexW.return_value = 456
                kernel.WaitForSingleObject.return_value = result
                with patch.object(display_session, '_lease_api', return_value=kernel):
                    with self.assertRaisesRegex(ValueError,'stream failure'):
                        with display_session._process_lease():
                            raise ValueError('stream failure')
                kernel.ReleaseMutex.assert_called_once_with(456)
                kernel.CloseHandle.assert_called_once_with(456)


if __name__ == '__main__':
    unittest.main()
