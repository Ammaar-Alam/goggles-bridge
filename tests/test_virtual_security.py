"""Real unprivileged file locks and pre-elevation rejection; never runs an installer."""
import hashlib
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from goggles_bridge.app.virtual_display import elevation, security, virtual_driver


@unittest.skipUnless(os.name == 'nt', 'Windows file sharing and ACLs')
class InstallerSecurityTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.program = self.folder/'installer.bin'
        self.program.write_bytes(b'known installer contents')
        self.digest = hashlib.sha256(self.program.read_bytes()).hexdigest()

    def test_installer_and_ancestors_cannot_be_replaced_while_locked(self):
        with security.lock_program(self.program, self.digest):
            with self.assertRaises(PermissionError):
                self.program.write_bytes(b'changed')
            with self.assertRaises(PermissionError):
                self.program.rename(self.folder/'replacement.bin')
            with self.assertRaises(PermissionError):
                self.folder.rename(self.folder.with_name(self.folder.name+'-moved'))
        self.program.write_bytes(b'released')

    def test_changed_installer_is_rejected_and_its_lock_released(self):
        self.program.write_bytes(b'changed')
        with self.assertRaisesRegex(RuntimeError, 'installer changed'):
            security.lock_program(self.program, self.digest)
        self.program.unlink()

    def test_user_writable_runtime_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'not protected|untrusted writes'):
            security.check_permissions(self.folder)

    def test_bad_hash_never_reaches_uac(self):
        with patch.object(virtual_driver, 'maintenance_command', return_value=(str(self.program), [])), \
             patch.dict(sys.modules, {'_goggles_driver_build': SimpleNamespace(INSTALLER_SHA256='0'*64)}), \
             patch.object(elevation.InstallerProcess, '_launch') as launch:
            with self.assertRaisesRegex(RuntimeError, 'installer changed'):
                virtual_driver.VirtualInstaller()
            launch.assert_not_called()

    def test_uac_cancellation_and_process_exit_release_locks(self):
        with patch.object(virtual_driver, 'maintenance_command', return_value=(str(self.program), [])), \
             patch.dict(sys.modules, {'_goggles_driver_build': SimpleNamespace(INSTALLER_SHA256=self.digest)}), \
             patch.object(elevation.InstallerProcess, '_launch', side_effect=OSError('cancelled')) as launch:
            with self.assertRaisesRegex(OSError, 'cancelled'):
                virtual_driver.VirtualInstaller()
            self.program.write_bytes(b'known installer contents')
            launch.side_effect = None
            installer = virtual_driver.VirtualInstaller()
            with patch.object(elevation.InstallerProcess, 'poll', side_effect=[None, 0]):
                self.assertIsNone(installer.poll())
                with self.assertRaises(PermissionError):
                    self.program.write_bytes(b'changed')
                self.assertEqual(installer.poll(), 0)
            self.program.unlink()

    def test_uninstaller_permissions_are_checked_before_uac(self):
        with patch.object(virtual_driver, 'maintenance_command', return_value=(str(self.program), [])), \
             patch.object(virtual_driver, 'install_folder', return_value=self.folder), \
             patch.object(elevation.InstallerProcess, '_launch') as launch:
            with self.assertRaisesRegex(RuntimeError, 'not protected|untrusted writes'):
                virtual_driver.VirtualInstaller(remove=True)
            launch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
