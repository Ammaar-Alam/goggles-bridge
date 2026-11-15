"""Resources for the optional Windows virtual display driver."""
from pathlib import Path
import sys


def resources():
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[3]))


def install_folder():
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Windows\CurrentVersion',
                        0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
        folder = winreg.QueryValueEx(key, 'ProgramFilesDir')[0]
    return Path(folder)/'Goggles Bridge Display'


def maintenance_command(remove=False):
    if remove:
        return str(install_folder()/'unins000.exe'), ['/SILENT', '/NORESTART']
    folder = Path(sys.executable).parent if getattr(sys, 'frozen', False) else resources()/'dist'
    installer = folder/'GogglesBridgeDisplay-Setup.exe'
    if not installer.is_file():
        raise RuntimeError('Build or reinstall the app to obtain its virtual display installer.')
    return str(installer), ['/SILENT', '/NORESTART', '/RESTARTEXITCODE=3010']
