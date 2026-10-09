# PyInstaller-Konfiguration: eine einzelne Programmdatei „SongBridge“ (bzw. .exe).
#   pip install pyinstaller && pyinstaller packaging/SongBridge.spec   ->  dist/SongBridge
# (aus dem Projektordner aufrufen)
from pathlib import Path

ROOT = Path(SPECPATH).parent
PKG = ROOT / "songbridge"
PROTO = PKG / "propresenter" / "proto"

pb2 = [p.stem for p in PROTO.glob("*_pb2.py")]

a = Analysis(
    [str(Path(SPECPATH) / "main.py")],
    pathex=[str(ROOT), str(PROTO)],  # die *_pb2-Module importieren sich ohne Paketpräfix
    datas=[
        (str(PKG / "web"), "songbridge/web"),
        (str(PKG / "resources"), "songbridge/resources"),
        (str(PROTO / "LICENSE-ProPresenter7-Proto"), "songbridge/propresenter/proto"),
    ],
    # songbridge/version.py schreibt der Build (fehlt beim Start aus dem Quellcode)
    hiddenimports=pb2 + ["songbridge.version"],
    excludes=["tkinter"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="SongBridge",
    console=True,  # Fenster zeigt Adresse/Datenordner; Schließen beendet das Programm
    upx=False,
)
