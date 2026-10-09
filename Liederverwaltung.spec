# PyInstaller-Konfiguration: eine einzelne Programmdatei „Liederverwaltung“ (bzw. .exe).
#   pip install pyinstaller && pyinstaller Liederverwaltung.spec   ->  dist/Liederverwaltung
from pathlib import Path

pb2 = [p.stem for p in Path("propresenterFormatter").glob("*_pb2.py")]

a = Analysis(
    ["app.py"],
    pathex=["propresenterFormatter"],  # die *_pb2-Module importieren sich ohne Paketpräfix
    datas=[
        ("web", "web"),
        ("examples", "examples"),
        ("export_settings.json", "."),
    ],
    hiddenimports=pb2 + ["propresenterFormatter.formatter", "version"],  # version.py schreibt der Build
    excludes=["tkinter"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="Liederverwaltung",
    console=True,  # Fenster zeigt Adresse/Datenordner; Schließen beendet das Programm
    upx=False,
)
