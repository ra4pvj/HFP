# PyInstaller spec for a macOS .app bundle:  pyinstaller --noconfirm hfpager_mac.spec
from PyInstaller.utils.hooks import collect_all
datas, binaries, hiddenimports = collect_all('sounddevice')
a = Analysis(['hfpager_app.py'], pathex=['.'], binaries=binaries, datas=datas,
             hiddenimports=hiddenimports + ['hfpager_modem', 'hfpager_rx'])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='HFPagerApp', console=False)
coll = COLLECT(exe, a.binaries, a.datas, name='HFPagerApp')
app = BUNDLE(coll, name='HFPagerApp.app', bundle_identifier='local.hfpager.app',
             info_plist={'NSMicrophoneUsageDescription':
                         'Приложение принимает сигналы пейджера со звукового входа.',
                         'NSHighResolutionCapable': True})
