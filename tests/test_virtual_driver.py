"""No installer, UAC prompt, driver change, network request or GPU is run here."""
import hashlib
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch
import xml.etree.ElementTree as ET

from goggles_bridge.app.virtual_display import elevation, virtual_driver, paths


class DriverTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows PowerShell')
    def test_failed_query_cannot_look_like_an_empty_inventory(self):
        from goggles_bridge.app.virtual_display.system import powershell_json
        with self.assertRaisesRegex(RuntimeError, 'system information query failed'):
            powershell_json("Write-Error 'inventory unavailable'; @() | ConvertTo-Json -Compress")
        self.assertIsNone(powershell_json('@() | ConvertTo-Json -Compress'))

    def test_config_has_eight_modes_without_replacing_selected_gpu(self):
        root=ET.fromstring(virtual_driver.CONFIG)
        rates={int(node.text) for node in root.findall('global/g_refresh_rate')}
        modes={(int(node.findtext('width')),int(node.findtext('height')),hz)
               for node in root.findall('resolutions/resolution') for hz in rates}
        self.assertEqual(modes,{(w,h,hz) for w,h in ((1280,720),(1920,1080))
                                for hz in (60,75,90,100)})
        configured=virtual_driver.CONFIG.replace('>default<','>NVIDIA Test GPU<')
        self.assertEqual(virtual_driver.configuration_details(configured),(True,'NVIDIA Test GPU'))
        self.assertFalse(virtual_driver.configuration_details(configured.replace('>100<','>120<'))[0])
        self.assertFalse(virtual_driver.configuration_details('<broken')[0])

    def test_virtual_driver_hash_failure_precedes_windows_signature_check(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder,'mttvdd.cat').write_bytes(b'tampered')
            with patch.object(virtual_driver,'powershell_json') as signature:
                with self.assertRaises(ValueError):
                    virtual_driver.verify_package(folder)
                signature.assert_not_called()
    def test_maintenance_rejects_arbitrary_or_extra_arguments(self):
        with patch.object(virtual_driver,'elevated_main',return_value=3010) as elevated:
            for args in ([],['anything'],['install-display-driver','arbitrary-path'],['--remove-display-driver']):
                self.assertEqual(virtual_driver.maintenance_main(args),64)
            elevated.assert_not_called()
            self.assertEqual(virtual_driver.maintenance_main(['install-display-driver']),3010)
            elevated.assert_called_once_with(remove_driver=False)

    def test_virtual_installer_uses_fixed_worker_operation(self):
        with patch.object(virtual_driver,'maintenance_command',return_value=('setup.exe',['/SILENT','/NORESTART'])), \
             patch('goggles_bridge.app.virtual_display.security.lock_program'), \
             patch.dict('sys.modules', {'_goggles_driver_build': SimpleNamespace(INSTALLER_SHA256='fixture')}), \
             patch.object(elevation.InstallerProcess,'_launch') as launch:
            virtual_driver.VirtualInstaller()
            launch.assert_called_once_with('setup.exe','/SILENT /NORESTART')

    def test_foreign_configuration_blocks_update_before_writing(self):
        with patch.object(virtual_driver,'owned_instance',return_value=r'ROOT\GOGGLESBRIDGE\0123456789abcdef0123456789abcdef'), \
             patch.object(virtual_driver,'_owns_configuration',return_value=False), \
             patch.object(virtual_driver,'winreg',MagicMock()), \
             patch.object(virtual_driver,'vdd_devices',return_value=[]), \
             patch.object(virtual_driver,'verify_package') as verify, \
             patch.object(virtual_driver.subprocess,'run') as run, \
             patch.object(virtual_driver,'os',SimpleNamespace(name='nt',environ={})):
            with self.assertRaises(RuntimeError):
                virtual_driver.install()
            verify.assert_not_called()
            run.assert_not_called()

    def test_second_driver_consumer_blocks_shared_configuration_update(self):
        instance=r'ROOT\GOGGLESBRIDGE\0123456789abcdef0123456789abcdef'
        with patch.object(virtual_driver,'owned_instance',return_value=instance), \
             patch.object(virtual_driver,'_owns_configuration',return_value=True), \
             patch.object(virtual_driver,'_instance_registered',return_value=True), \
             patch.object(virtual_driver,'vdd_devices',return_value=[{'PNPDeviceID':instance},
                                                                    {'PNPDeviceID':r'ROOT\DISPLAY\0002'}]), \
             patch.object(virtual_driver,'verify_package') as verify, \
             patch.object(virtual_driver,'os',SimpleNamespace(name='nt',environ={})):
            with self.assertRaises(RuntimeError):
                virtual_driver.install()
            verify.assert_not_called()
class OwnershipTests(unittest.TestCase):
    def setUp(self):
        from contextlib import nullcontext
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.instance = r'ROOT\GOGGLESBRIDGE\0123456789abcdef0123456789abcdef'
        self.parents = (r'SOFTWARE\G2Bridge', r'SOFTWARE\MikeTheTech')
        self.records = {virtual_driver.OWNER: {'DeviceInstanceId': self.instance},
                        virtual_driver.REGISTRY: {'VDDPATH': str(self.folder)},
                        **{parent: {} for parent in self.parents}}
        registry = MagicMock()
        def open_key(_root, name, *args):
            if name not in self.records:
                raise FileNotFoundError(name)
            return nullcontext(name)
        def query(key, name):
            try:
                return self.records[key][name], 1
            except KeyError:
                raise FileNotFoundError(name)
        registry.OpenKey.side_effect = open_key
        registry.QueryValueEx.side_effect = query
        registry.QueryInfoKey.side_effect = lambda key: (
            sum(name.startswith(key+'\\') and '\\' not in name[len(key)+1:] for name in self.records),
            len(self.records[key]), 0)
        registry.DeleteKey.side_effect = lambda _root, name: self.records.pop(name)
        self.enterContext(patch.object(virtual_driver, 'winreg', registry))
        self.enterContext(patch.object(virtual_driver, 'install_folder', return_value=self.folder))
        self.enterContext(patch.object(virtual_driver, 'os', SimpleNamespace(name='nt')))
        self.enterContext(patch.object(virtual_driver, '_owns_configuration', return_value=True))
        self.enterContext(patch.object(virtual_driver, '_instance_ready', return_value=True))
        self.devices = self.enterContext(patch.object(virtual_driver, 'vdd_devices', return_value=[]))
        self.remove = self.enterContext(patch.object(virtual_driver, '_pnputil', return_value=0))
        self.inventory = self.enterContext(patch('goggles_bridge.app.virtual_display.displays.inventory', return_value=[]))

    def test_sequential_id_cannot_be_reclaimed_or_removed(self):
        old = r'ROOT\DISPLAY\0003'
        self.records[virtual_driver.OWNER]['DeviceInstanceId'] = old
        self.records['SYSTEM\\CurrentControlSet\\Enum\\'+old] = {'HardwareID': ['Root\\MttVDD']}
        self.devices.return_value = [{'PNPDeviceID': old}]
        self.assertIsNone(virtual_driver.owned_instance())
        self.assertFalse(virtual_driver._instance_registered(old))
        state = virtual_driver.status()
        self.assertTrue(state['previous'])
        self.assertFalse(state['config_current'])
        with patch.object(virtual_driver, 'verify_package') as verify:
            with self.assertRaisesRegex(RuntimeError, 'original app'):
                virtual_driver.install()
            verify.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, 'Another virtual display'):
            virtual_driver.remove()
        self.remove.assert_not_called()
        self.assertIn(virtual_driver.OWNER, self.records)

    def test_previous_fork_is_only_a_hint_not_a_migration_authority(self):
        self.records.pop(virtual_driver.OWNER)
        self.records[virtual_driver.PREVIOUS_OWNER] = {'DeviceInstanceId': r'ROOT\DISPLAY\0003'}
        self.records[r'SYSTEM\CurrentControlSet\Enum\ROOT\DISPLAY\0003'] = {'HardwareID': ['Root\\MttVDD']}
        self.assertTrue(virtual_driver.status()['previous'])
        with self.assertRaisesRegex(RuntimeError, 'original app'):
            virtual_driver.install()
        self.remove.assert_not_called()
        self.assertIn(virtual_driver.PREVIOUS_OWNER, self.records)

    def test_random_record_requires_matching_hardware(self):
        key = 'SYSTEM\\CurrentControlSet\\Enum\\'+self.instance
        self.records[key] = {'HardwareID': ['Root\\MttVDD']}
        self.assertEqual(virtual_driver.owned_instance(), self.instance)
        self.assertTrue(virtual_driver._instance_registered(self.instance))
        self.records[key]['HardwareID'] = ['OtherDevice']
        self.assertFalse(virtual_driver._instance_registered(self.instance))

    def test_old_record_without_any_remaining_devices_can_be_cleaned(self):
        self.records[virtual_driver.OWNER]['DeviceInstanceId'] = r'ROOT\DISPLAY\0003'
        self.assertEqual(virtual_driver.remove(), 0)
        self.remove.assert_not_called()
        self.assertNotIn(virtual_driver.OWNER, self.records)
        self.assertNotIn(virtual_driver.REGISTRY, self.records)
        for parent in self.parents:
            self.assertNotIn(parent, self.records)

    def test_absent_old_device_can_be_repaired_with_a_new_random_id(self):
        self.records[virtual_driver.OWNER]['DeviceInstanceId'] = r'ROOT\DISPLAY\0003'
        (self.folder/'unins000.exe').touch()
        state = virtual_driver.status()
        self.assertFalse(state['previous'])
        self.assertTrue(state['owned'])
        self.assertFalse(state['config_current'])
        def create(_inf, instance):
            self.assertIsNone(instance)
            self.records[virtual_driver.OWNER]['DeviceInstanceId'] = self.instance
            return False
        with patch.object(virtual_driver, 'verify_package', return_value=self.folder), \
             patch.object(virtual_driver, '_stage_package'), \
             patch.object(virtual_driver, '_install_device', side_effect=create):
            self.assertEqual(virtual_driver.install(), 0)
        self.assertEqual(virtual_driver.owned_instance(), self.instance)
        self.remove.assert_not_called()

    def test_absent_old_device_does_not_allow_repair_over_another_consumer(self):
        self.records[virtual_driver.OWNER]['DeviceInstanceId'] = r'ROOT\DISPLAY\0003'
        self.devices.return_value = [{'PNPDeviceID': r'ROOT\DISPLAY\0004'}]
        with patch.object(virtual_driver, 'verify_package') as verify:
            with self.assertRaisesRegex(RuntimeError, 'Another virtual display'):
                virtual_driver.install()
            verify.assert_not_called()
        self.remove.assert_not_called()

    def test_parent_keys_with_sibling_products_or_values_are_preserved(self):
        self.records[self.parents[0]+'\\OtherProduct'] = {'setting': 'keep'}
        self.records[self.parents[1]]['setting'] = 'keep'
        self.assertEqual(virtual_driver.remove(), 0)
        self.assertNotIn(virtual_driver.OWNER, self.records)
        for parent in self.parents:
            self.assertIn(parent, self.records)
        self.assertEqual(self.records[self.parents[0]+'\\OtherProduct'], {'setting': 'keep'})
        self.assertEqual(self.records[self.parents[1]], {'setting': 'keep'})

    def test_owned_package_removal_prunes_only_empty_umdf_keys(self):
        service = virtual_driver.UMDF_SERVICE
        for nonempty in (False, True):
            with self.subTest(nonempty=nonempty):
                self.records[virtual_driver.OWNER] = {'PublishedInf': 'oem42.inf'}
                self.records[virtual_driver.REGISTRY] = {'VDDPATH': str(self.folder)}
                self.records[service] = {}
                self.records[service+'\\Parameters'] = {'other': 'keep'} if nonempty else {}
                self.records[service+'\\State'] = {}
                with patch.object(virtual_driver, 'system_directory', return_value=self.folder/'System32'):
                    self.assertEqual(virtual_driver.remove(), 0)
                self.assertNotIn(service+'\\State', self.records)
                self.assertEqual(service in self.records, nonempty)
                if nonempty:
                    self.assertEqual(self.records[service+'\\Parameters'], {'other': 'keep'})


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        from contextlib import ExitStack
        self.stack = self.enterContext(ExitStack())
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.instance = r'ROOT\GOGGLESBRIDGE\0123456789abcdef0123456789abcdef'
        for name,value in (
            ('install_folder',self.folder), ('verify_package',self.folder),
            ('owned_instance',self.instance), ('owner_value',None),
            ('_owns_configuration',True), ('_instance_registered',True), ('_instance_ready',True),
            ('vdd_devices',[{'PNPDeviceID':self.instance}]), ('_pnputil',0),
        ):
            setattr(self,name,self.stack.enter_context(patch.object(virtual_driver,name,return_value=value)))
        self.registry=self.stack.enter_context(patch.object(virtual_driver,'winreg',MagicMock()))
        self.stack.enter_context(patch.object(virtual_driver, 'os', SimpleNamespace(name='nt')))
        self.config=self.folder/'vdd_settings.xml'

    def test_inventory_failure_blocks_install_and_removal_before_mutation(self):
        self.config.write_bytes(b'original settings')
        self.vdd_devices.side_effect = RuntimeError('inventory unavailable')
        for operation in (virtual_driver.install, virtual_driver.remove):
            with self.subTest(operation=operation.__name__):
                with self.assertRaisesRegex(RuntimeError, 'inventory unavailable'):
                    operation()
                self.assertEqual(self.config.read_bytes(), b'original settings')
                self.verify_package.assert_not_called()
                self._pnputil.assert_not_called()
                self.registry.CreateKeyEx.assert_not_called()
                self.registry.DeleteKey.assert_not_called()

    def test_missing_invalid_xml_and_invalid_utf8_are_repairable(self):
        for original in (None,b'<broken',b'\xff\xfe'):
            with self.subTest(original=original):
                self.config.unlink(missing_ok=True)
                if original is not None:
                    self.config.write_bytes(original)
                self.assertEqual(virtual_driver.install(),0)
                self.assertTrue(virtual_driver.configuration_details(self.config.read_text(encoding='utf-8'))[0])
                self._pnputil.assert_called_with('/restart-device',self.instance)

    def test_failed_restart_restores_exact_bytes_or_absence(self):
        self._pnputil.side_effect=RuntimeError('restart failed')
        for original in (None,b'\xff\xfe',virtual_driver.CONFIG.replace('>100<','>120<').encode()):
            with self.subTest(original=original):
                self.config.unlink(missing_ok=True)
                if original is not None:
                    self.config.write_bytes(original)
                with self.assertRaisesRegex(RuntimeError,'restart failed'):
                    virtual_driver.install()
                self.assertEqual(self.config.read_bytes() if self.config.exists() else None,original)
                self.assertFalse(self.config.with_suffix('.tmp').exists())

    def test_repair_preserves_gpu_preference(self):
        self.config.write_text(virtual_driver.CONFIG.replace('>default<','>NVIDIA Test GPU<').replace('>100<','>120<'))
        virtual_driver.install()
        self.assertEqual(virtual_driver.configuration_details(self.config.read_text()),(True,'NVIDIA Test GPU'))

    def test_failed_device_rollback_keeps_configuration_for_cleanup(self):
        self.owned_instance.return_value = None
        with patch.object(virtual_driver, '_stage_package'), \
             patch.object(virtual_driver, '_install_device', side_effect=virtual_driver.DeviceRollbackError('rollback failed')):
            self.vdd_devices.return_value = []
            with self.assertRaisesRegex(virtual_driver.DeviceRollbackError, 'rollback failed'):
                virtual_driver.install()
        self.assertTrue(virtual_driver.configuration_details(self.config.read_text())[0])
        self.registry.DeleteKey.assert_not_called()

    def test_registered_but_unbound_device_is_repaired_in_place(self):
        self.config.write_text(virtual_driver.CONFIG)
        self._instance_ready.return_value = False
        self.assertFalse(virtual_driver.status()['config_current'])
        with patch.object(virtual_driver, '_stage_package') as stage, \
             patch.object(virtual_driver, '_install_device', return_value=False) as bind:
            self.assertEqual(virtual_driver.install(), 0)
        stage.assert_called_once_with(self.folder/'MttVDD.inf')
        bind.assert_called_once_with(self.folder/'MttVDD.inf', self.instance)
        self._pnputil.assert_not_called()

    def test_invalid_utf8_status_requests_repair(self):
        self.config.write_bytes(b'\xff')
        with patch.object(virtual_driver,'os',SimpleNamespace(name='nt')):
            self.assertFalse(virtual_driver.status()['config_current'])

    def test_shared_consumer_blocks_remove_before_any_mutation(self):
        self.vdd_devices.return_value=[{'PNPDeviceID':self.instance},{'PNPDeviceID':r'ROOT\DISPLAY\0002'}]
        with self.assertRaisesRegex(RuntimeError,'Another virtual display'):
            virtual_driver.remove()
        self._pnputil.assert_not_called()
        self.registry.DeleteKey.assert_not_called()

    def test_removed_instance_can_still_be_uninstalled(self):
        self._instance_registered.return_value=False
        self.vdd_devices.return_value=[]
        self.assertEqual(virtual_driver.remove(),0)
        self._pnputil.assert_not_called()
        self.registry.DeleteKey.assert_any_call(self.registry.HKEY_LOCAL_MACHINE,virtual_driver.OWNER)
        self.registry.DeleteKey.assert_any_call(self.registry.HKEY_LOCAL_MACHINE,virtual_driver.REGISTRY)

    def test_reboot_and_failure_retain_cleanup_records(self):
        for outcome in (3010,RuntimeError('remove failed')):
            self._pnputil.side_effect=outcome if isinstance(outcome,Exception) else None
            self._pnputil.return_value=outcome
            with self.assertRaises(RuntimeError):
                virtual_driver.remove()
            self.registry.DeleteKey.assert_not_called()

    def test_component_only_failure_does_not_touch_foreign_driver(self):
        self.owned_instance.return_value=None
        self._owns_configuration.return_value=False
        virtual_driver.remove()
        self.vdd_devices.assert_not_called()
        self._pnputil.assert_not_called()
        self.registry.DeleteKey.assert_not_called()

    def test_owned_package_is_removed_without_force_or_uninstall(self):
        self.vdd_devices.side_effect=[[{'PNPDeviceID':self.instance}],[]]
        self.owner_value.return_value='oem42.inf'
        (self.folder/'INF').mkdir()
        payload=b'pinned driver inf'
        (self.folder/'INF/oem42.inf').write_bytes(payload)
        with patch.object(virtual_driver,'system_directory',return_value=self.folder/'System32'), \
             patch.dict(virtual_driver.HASHES,{'MttVDD.inf':hashlib.sha256(payload).hexdigest()}):
            virtual_driver.remove()
        self.assertEqual([call.args for call in self._pnputil.call_args_list],
                         [('/remove-device',self.instance,'/subtree'),('/delete-driver','oem42.inf')])

    @unittest.skipUnless(__import__('os').name == 'nt', 'Windows system directory')
    def test_system_tool_paths_ignore_inherited_environment(self):
        from goggles_bridge.app.virtual_display.system import system_directory
        with patch.dict(__import__('os').environ,{'SystemRoot':str(self.folder/'untrusted')}):
            self.assertNotIn('untrusted',str(system_directory()))


@unittest.skipUnless(os.name == 'nt', 'Windows device APIs')
class DeviceBindingTests(unittest.TestCase):
    def setUp(self):
        self.setup, self.driver = Mock(), Mock()
        self.setup.SetupDiCreateDeviceInfoList.return_value = 123
        self.driver.DiInstallDevice.return_value = True
        self.instance = r'ROOT\GOGGLESBRIDGE\0123456789abcdef0123456789abcdef'

        self.enterContext(patch.object(virtual_driver.uuid, 'uuid4',
            return_value=SimpleNamespace(hex=self.instance.rsplit('\\', 1)[1])))
        self.enterContext(patch.object(virtual_driver.C, 'WinDLL', side_effect=lambda name, **kw:
                                      self.setup if name == 'setupapi' else self.driver))
        self.owner = self.enterContext(patch.object(virtual_driver, '_save_owner'))

    def test_owner_is_recorded_before_registration(self):
        order = []
        self.owner.side_effect = lambda *args: order.append('owner')
        self.setup.SetupDiCallClassInstaller.side_effect = lambda operation, *args: order.append(operation) or True
        virtual_driver._install_device(Path('pinned.inf'))
        self.assertEqual(order, ['owner', 0x19])
        self.owner.assert_called_once_with('DeviceInstanceId', self.instance)
        self.assertEqual(self.setup.SetupDiCreateDeviceInfoW.call_args.args[1], self.instance)
        self.assertEqual(self.setup.SetupDiCreateDeviceInfoW.call_args.args[5], 0)
        self.driver.DiInstallDevice.assert_called_once()

    def test_recording_failure_never_registers_a_device(self):
        self.owner.side_effect = OSError('registry write failed')
        with self.assertRaisesRegex(OSError, 'registry write failed'):
            virtual_driver._install_device(Path('pinned.inf'))
        self.setup.SetupDiCallClassInstaller.assert_not_called()
        self.driver.DiInstallDevice.assert_not_called()

    def test_existing_random_id_is_not_adopted(self):
        self.setup.SetupDiCreateDeviceInfoW.return_value = False
        with self.assertRaises(OSError):
            virtual_driver._install_device(Path('pinned.inf'))
        self.owner.assert_not_called()
        self.setup.SetupDiCallClassInstaller.assert_not_called()
        self.setup.SetupDiOpenDeviceInfoW.assert_not_called()

    def test_sequential_id_cannot_be_bound_as_an_existing_device(self):
        with self.assertRaisesRegex(RuntimeError, 'original app'):
            virtual_driver._install_device(Path('pinned.inf'), r'ROOT\DISPLAY\0003')
        self.setup.SetupDiOpenDeviceInfoW.assert_not_called()
        self.driver.DiInstallDevice.assert_not_called()

    def test_failed_repair_does_not_create_or_remove_the_existing_device(self):
        self.driver.DiInstallDevice.return_value = False
        with self.assertRaises(OSError):
            virtual_driver._install_device(Path('pinned.inf'), self.instance)
        self.setup.SetupDiOpenDeviceInfoW.assert_called_once()
        self.assertEqual(self.setup.SetupDiOpenDeviceInfoW.call_args.args[1], self.instance)
        self.setup.SetupDiCreateDeviceInfoW.assert_not_called()
        self.setup.SetupDiCallClassInstaller.assert_not_called()
        self.owner.assert_not_called()

    def test_failed_rollback_retains_recorded_ownership(self):
        self.driver.DiInstallDevice.return_value = False
        self.setup.SetupDiCallClassInstaller.side_effect = lambda operation, *args: operation != 5
        with self.assertRaises(virtual_driver.DeviceRollbackError):
            virtual_driver._install_device(Path('pinned.inf'))
        self.owner.assert_called_once_with('DeviceInstanceId', self.instance)

    def test_readiness_requires_a_started_device_without_problems(self):
        self.driver.CM_Locate_DevNodeW.return_value = 0
        for status, ready in ((0, False), (0x8, True), (0x408, False)):
            with self.subTest(status=status):
                def query(flags, *_args):
                    flags._obj.value = status
                    return 0
                self.driver.CM_Get_DevNode_Status.side_effect = query
                self.assertEqual(virtual_driver._instance_ready(self.instance), ready)


class LauncherTests(unittest.TestCase):
    def test_development_launch_requires_the_native_installer(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(paths,'resources',return_value=Path(folder)), \
             patch.object(paths.sys,'frozen',False,create=True):
            with self.assertRaisesRegex(RuntimeError,'Build or reinstall'):
                paths.maintenance_command()
            (Path(folder)/'dist').mkdir()
            installer=Path(folder)/'dist/GogglesBridgeDisplay-Setup.exe'
            installer.write_bytes(b'fixture')
            program,args=paths.maintenance_command()
            self.assertEqual(program,str(installer))
            self.assertIn('/NORESTART',args)

    def test_removal_uses_only_the_machine_component_uninstaller(self):
        with patch.object(paths,'install_folder',return_value=Path('protected')):
            program,args=paths.maintenance_command(True)
            self.assertEqual(program,str(Path('protected/unins000.exe')))
            self.assertEqual(args,['/SILENT','/NORESTART'])

    @unittest.skipUnless(__import__('os').name == 'nt', 'Windows ABI')
    def test_exact_device_install_structure_sizes(self):
        import ctypes
        self.assertEqual(ctypes.sizeof(virtual_driver.INSTALL_PARAMS),584)
        self.assertEqual(ctypes.sizeof(virtual_driver.DRIVER_INFO),1568)


if __name__ == '__main__':
    unittest.main()
