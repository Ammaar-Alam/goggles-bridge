"""Resources for the optional Windows virtual display driver."""
from pathlib import Path
import sys


def install_folder():
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Windows\CurrentVersion',
                        0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
        folder = winreg.QueryValueEx(key, 'ProgramFilesDir')[0]
    return Path(folder)/'Goggles Bridge Display'


def maintenance_command(remove=False):
    if remove:
        return str(install_folder()/'unins000.exe'), ['/SILENT', '/NORESTART']
    if not getattr(sys, 'frozen', False):
        raise RuntimeError('Use a packaged Goggles Bridge build to install the display driver.')
    installer = Path(sys.executable).parent/'GogglesBridgeDisplay-Setup.exe'
    if not installer.is_file():
        raise RuntimeError('Build or reinstall the app to obtain its virtual display installer.')
    return str(installer), ['/SILENT', '/NORESTART', '/RESTARTEXITCODE=3010']
