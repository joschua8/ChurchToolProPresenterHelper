"""Protobuf-Module des ProPresenter-7-Formats, erzeugt aus https://github.com/greyshirtguy/ProPresenter7-Proto
(MIT-Lizenz, siehe LICENSE-ProPresenter7-Proto).

Die *_pb2.py-Module importieren sich gegenseitig ohne Paketpräfix (z. B. `import cue_pb2`),
deshalb muss dieser Ordner im Suchpfad liegen. Überall nur so importieren:

    from songbridge.propresenter import proto  # noqa: F401
    import presentation_pb2

(nie als songbridge.propresenter.proto.presentation_pb2 – sonst lädt protobuf die Datei doppelt).
"""

import sys
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
