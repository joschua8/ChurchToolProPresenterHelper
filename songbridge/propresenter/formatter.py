#!/usr/bin/env python3
"""Wandelt eine Präsentation im „.json“-Zwischenformat in eine ProPresenter-7-Datei (.pro) um.

Zwischenformat = Ausgabe von `protoc --decode rv.data.Presentation` (Protobuf-Textformat),
siehe songbridge/resources/BeispielZielFormatierung.json. Echtes JSON (protobuf json_format) geht ebenfalls.

CLI:
    python -m songbridge.propresenter.formatter lied.json [ziel.pro]
    python -m songbridge.propresenter.formatter ordner_mit_json/ [zielordner/]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from google.protobuf import json_format, text_format

from songbridge.propresenter import proto  # noqa: F401  (pb2-Module in den Suchpfad)

import presentation_pb2  # noqa: E402

# protoc gibt Felder, die die .proto-Dateien nicht kennen, als "19: 1" aus.
# Die lassen sich nicht zurück-parsen; sie stammen aus neueren ProPresenter-Versionen
# (z. B. Graphics.Text.Attributes.ligature_style) und werden ausgelassen.
_UNKNOWN_FIELD_RE = re.compile(r"^[ \t]*\d+:[^\n]*\n?", re.MULTILINE)
_UNKNOWN_GROUP_RE = re.compile(r"^[ \t]*\d+[ \t]*\{")


def parse_presentation(text: str, warnings: list[str] | None = None) -> presentation_pb2.Presentation:
    """Liest das Zwischenformat (Protobuf-Text oder JSON) in eine Presentation-Nachricht."""
    pres = presentation_pb2.Presentation()
    stripped = text.lstrip("﻿").lstrip()
    if stripped.startswith("{"):
        json_format.Parse(stripped, pres, ignore_unknown_fields=True)
        return pres

    if any(_UNKNOWN_GROUP_RE.match(line) for line in stripped.splitlines()):
        raise ValueError("Unbekannte verschachtelte Felder im Zwischenformat – .proto-Dateien zu alt?")
    unknown = _UNKNOWN_FIELD_RE.findall(stripped)
    if unknown and warnings is not None:
        warnings.append(f"{len(unknown)} unbekannte(s) Feld(er) ausgelassen: "
                        + ", ".join(sorted({u.strip() for u in unknown})))
    text_format.Parse(_UNKNOWN_FIELD_RE.sub("", stripped), pres)
    return pres


def json_to_pro(text: str, warnings: list[str] | None = None) -> bytes:
    """Zwischenformat -> binäre .pro-Datei."""
    return parse_presentation(text, warnings).SerializeToString()


def presentation_to_json(pres: presentation_pb2.Presentation) -> str:
    """Presentation -> Zwischenformat (gleiches Format wie `protoc --decode`)."""
    return text_format.MessageToString(pres, as_utf8=False)


def _convert_file(src: Path, dst: Path) -> None:
    warnings: list[str] = []
    dst.write_bytes(json_to_pro(src.read_text(encoding="utf-8"), warnings))
    print(f"{src.name} -> {dst}")
    for w in warnings:
        print(f"  Hinweis: {w}")


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    src = Path(argv[0])
    if src.is_dir():
        out = Path(argv[1]) if len(argv) > 1 else src
        out.mkdir(parents=True, exist_ok=True)
        for f in sorted(src.glob("*.json")):
            _convert_file(f, out / f"{f.stem}.pro")
    else:
        _convert_file(src, Path(argv[1]) if len(argv) > 1 else src.with_suffix(".pro"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
