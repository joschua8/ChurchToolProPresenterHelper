"""ProPresenter-7-Formatter auf Basis von https://github.com/greyshirtguy/ProPresenter7-Proto.

Die *_pb2.py-Module importieren sich gegenseitig ohne Paketpräfix (z. B. `import cue_pb2`),
deshalb muss dieser Ordner im Suchpfad liegen.
"""

import sys
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
