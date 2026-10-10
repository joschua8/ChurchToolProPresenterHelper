#!/usr/bin/env python3
"""Abgleich Liederdatenbank (songs/*.sng) <-> ProPresenter-Lieder-Bibliothek (*.pro).

  * Zuordnung über den Exportnamen (so heißt die .pro-Datei nach dem Export), sonst über den
    normalisierten Namen (Groß/klein, Satzzeichen, Unicode egal).
  * Inhaltlicher Vergleich über den Liedtext: aus der .pro werden alle Folientexte (RTF) gelesen
    und mit den Zeilen verglichen, die der Export aus der .sng erzeugen würde. Formatierung,
    Folienaufteilung und Reihenfolge spielen keine Rolle.

Status je Lied:  same | different | db_only | pp_only | error
"""

from __future__ import annotations

import re
import shutil
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from songbridge.propresenter import export as pro_export
from songbridge.propresenter import proto  # noqa: F401  (pb2-Module in den Suchpfad)
from songbridge.propresenter.playlist import nfc, norm_name
from songbridge.songs.sng import Song, read_sng

import presentation_pb2  # noqa: E402

# ------------------------------------------------------------------ RTF -> Text

_RTF_SKIP_DESTS = {"fonttbl", "colortbl", "stylesheet", "info", "pict", "expandedcolortbl", "listtable",
                   "listoverridetable", "header", "footer", "themedata", "colorschememapping"}
_RTF_TOKEN_RE = re.compile(r"\\([a-zA-Z]+)(-?\d+)? ?|\\'([0-9a-fA-F]{2})|\\(.)|([{}])|(\r?\n)|([^\\{}\r\n]+)", re.S)


def rtf_to_text(rtf: str) -> str:
    """Sehr einfacher RTF-Leser für ProPresenter-Texte (Cocoa-RTF)."""
    out: list[str] = []
    stack: list[tuple[bool, int]] = []
    skip, uc, pending_skip = False, 1, 0
    for m in _RTF_TOKEN_RE.finditer(rtf):
        word, arg, hexch, sym, brace, newline, text = m.groups()
        if brace == "{":
            stack.append((skip, uc))
            continue
        if brace == "}":
            skip, uc = stack.pop() if stack else (False, 1)
            continue
        if newline:
            continue
        if pending_skip and (text or hexch):
            if text:
                n = min(pending_skip, len(text))
                text, pending_skip = text[n:], pending_skip - n
                if not text:
                    continue
            else:
                pending_skip -= 1
                continue
        if word:
            if word in _RTF_SKIP_DESTS:
                skip = True
            elif word in ("par", "line"):
                if not skip:
                    out.append("\n")
            elif word == "tab":
                if not skip:
                    out.append("\t")
            elif word == "uc":
                uc = int(arg or 1)
            elif word == "u":
                if not skip:
                    code = int(arg or 0)
                    out.append(chr(code + 65536 if code < 0 else code))
                pending_skip = uc
            continue
        if sym is not None:
            if sym == "*":
                skip = True
            elif sym in "\\{}" and not skip:
                out.append(sym)
            elif sym in "\n\r" and not skip:  # „\<Zeilenumbruch>“ = Absatz (Cocoa)
                out.append("\n")
            elif sym == "~" and not skip:
                out.append("\u00a0")
            continue
        if hexch and not skip:
            out.append(bytes([int(hexch, 16)]).decode("cp1252", errors="replace"))
            continue
        if text and not skip:
            out.append(text)
    return "".join(out)


# ------------------------------------------------------------------ Texte vergleichen


def norm_line(line: str) -> str:
    s = unicodedata.normalize("NFC", line).replace("\u00a0", " ").casefold().replace("ß", "ss")
    return re.sub(r"[^\w]+", " ", s).strip()


def song_lines(song: Song) -> list[str]:
    """Zeilen, die der Export aus diesem Lied macht (inkl. Übersetzung), ohne Wiederholungen."""
    groups, _ = pro_export.song_groups(song, pro_export.ExportStyle(title_slide=False))
    return [t for g in groups for slide in g.slides for kind, t in slide if kind != "spacer" and t.strip()]


def pro_lines(path: Path) -> tuple[str, list[str]]:
    """-> (Präsentationsname, Textzeilen aller Folien außer der Titelfolie)."""
    pres = presentation_pb2.Presentation()
    pres.ParseFromString(path.read_bytes())
    title_cues = {cid.string for cg in pres.cue_groups if cg.group.name == pro_export.TITLE_NAME
                  for cid in cg.cue_identifiers}
    lines: list[str] = []
    for cue in pres.cues:
        if cue.uuid.string in title_cues:
            continue
        for action in cue.actions:
            if not action.HasField("slide"):
                continue
            for el in action.slide.presentation.base_slide.elements:
                rtf = el.element.text.rtf_data
                if not rtf:
                    continue
                text = rtf_to_text(rtf.decode("latin-1", errors="replace"))
                lines.extend(ln for ln in text.split("\n") if ln.strip())
    return pres.name, lines


def compare_lines(db: list[str], pp: list[str]) -> tuple[bool, list[str], list[str]]:
    """-> (gleich?, nur in Datenbank, nur in ProPresenter). Zeilenumbrüche innerhalb des Textes egal."""
    a = [n for n in (norm_line(x) for x in db) if n]
    b = [n for n in (norm_line(x) for x in pp) if n]
    if set(a) == set(b) or Counter(" ".join(a).split()) == Counter(" ".join(b).split()):
        return True, [], []
    sa, sb = set(a), set(b)
    only_db = list(dict.fromkeys(x for x, n in zip(db, (norm_line(x) for x in db)) if n and n not in sb))
    only_pp = list(dict.fromkeys(x for x, n in zip(pp, (norm_line(x) for x in pp)) if n and n not in sa))
    return False, only_db, only_pp


# ------------------------------------------------------------------ Abgleich


@dataclass
class DbSong:
    id: str           # Pfad relativ zum Liederordner
    path: Path
    name: str         # Exportname
    stem: str         # Dateiname der .pro (eindeutig)
    manual: bool
    song: Song | None = None
    error: str = ""


def db_songs(songs_dir: Path, index: dict) -> list[DbSong]:
    """Alle Lieder mit Exportnamen – Reihenfolge und Namen wie beim ZIP-Export."""
    out, taken = [], set()
    for path in sorted(songs_dir.rglob("*.sng"), key=lambda p: p.name.casefold()) if songs_dir.exists() else []:
        rel = path.relative_to(songs_dir).as_posix()
        try:
            song = read_sng(path)
        except Exception as exc:  # defekte Datei soll den Abgleich nicht blockieren
            out.append(DbSong(rel, path, path.stem, path.stem, False, None, str(exc)))
            continue
        name = pro_export.export_name(path, songs_dir, song, index)
        stem = pro_export.unique_stem(pro_export.file_stem(name), taken)
        out.append(DbSong(rel, path, name, stem, song.manual, song))
    return out


def pp_files(pp_dir: Path | None) -> list[Path]:
    if not pp_dir or not pp_dir.is_dir():
        return []
    return sorted((f for f in pp_dir.iterdir() if f.is_file() and f.suffix.lower() == ".pro"),
                  key=lambda f: nfc(f.name).casefold())


@dataclass
class DiffRow:
    status: str
    title: str
    db: DbSong | None = None
    pp: Path | None = None
    only_db: list[str] = field(default_factory=list)
    only_pp: list[str] = field(default_factory=list)
    note: str = ""

    def to_json(self) -> dict:
        return {
            "status": self.status,
            "title": self.title,
            "id": self.db.id if self.db else None,
            "exportName": self.db.name if self.db else None,
            "proName": nfc(self.db.stem) + ".pro" if self.db else None,
            "manual": bool(self.db and self.db.manual),
            "pp": {"file": nfc(self.pp.name), "modified": self.pp.stat().st_mtime} if self.pp else None,
            "dbModified": self.db.path.stat().st_mtime if self.db else None,
            "onlyDb": self.only_db[:6],
            "onlyPp": self.only_pp[:6],
            "note": self.note,
        }


def diff(songs_dir: Path, pp_dir: Path | None, index: dict) -> list[DiffRow]:
    db = db_songs(songs_dir, index)
    files = pp_files(pp_dir)
    by_stem = {nfc(f.stem).casefold(): f for f in files}
    by_norm: dict[str, list[Path]] = {}
    for f in files:
        by_norm.setdefault(norm_name(f.stem), []).append(f)
    used: set[Path] = set()

    # 1. Durchgang: exakte Dateinamen (haben Vorrang vor ähnlichen)
    pairs: dict[str, Path] = {}
    for s in db:
        f = by_stem.get(nfc(s.stem).casefold())
        if f and f not in used:
            pairs[s.id] = f
            used.add(f)
    # 2. Durchgang: normalisierte Namen
    for s in db:
        if s.id in pairs:
            continue
        f = next((f for f in by_norm.get(norm_name(s.name), []) if f not in used), None)
        if f:
            pairs[s.id] = f
            used.add(f)

    rows: list[DiffRow] = []
    for s in db:
        f = pairs.get(s.id)
        if s.song is None:
            rows.append(DiffRow("error", s.name, s, f, note=f"Lied nicht lesbar: {s.error}"))
            continue
        if f is None:
            rows.append(DiffRow("db_only", s.name, s))
            continue
        try:
            _, lines = pro_lines(f)
        except Exception as exc:
            rows.append(DiffRow("error", s.name, s, f, note=f".pro nicht lesbar: {exc}"))
            continue
        same, only_db, only_pp = compare_lines(song_lines(s.song), lines)
        note = "" if nfc(f.stem) == nfc(s.stem) else f"Dateiname in ProPresenter: {nfc(f.name)}"
        rows.append(DiffRow("same" if same else "different", s.name, s, f, only_db, only_pp, note))
    for f in files:
        if f not in used:
            rows.append(DiffRow("pp_only", nfc(f.stem), None, f))
    rows.sort(key=lambda r: nfc(r.title).casefold())
    return rows


def write_to_library(rows: list[DbSong], pp_dir: Path, style, index: dict, songs_dir: Path,
                     targets: dict[str, Path], backup_dir: Path) -> tuple[list[str], list[str], Path | None]:
    """Schreibt .pro-Dateien in die ProPresenter-Bibliothek.

    targets: Lied-ID -> Zieldatei (vorhandene Datei bei „abweichend“, sonst neue). Überschriebene
    Dateien werden vorher nach backup_dir/<Zeitstempel>/ kopiert. -> (geschrieben, Fehler, Sicherungsordner)
    """
    written, failed = [], []
    stamp_dir: Path | None = None
    for s in rows:
        target = targets[s.id]
        try:
            name = pro_export.export_name(s.path, songs_dir, s.song, index)
            _, data = pro_export.song_to_pro(s.song, style, key=s.id, name=name)
            if target.exists():
                if stamp_dir is None:
                    stamp_dir = backup_dir / time.strftime("%Y-%m-%d_%H%M%S")
                    stamp_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, stamp_dir / target.name)
            tmp = target.with_name(target.name + ".part")
            tmp.write_bytes(data)
            tmp.replace(target)
            written.append(nfc(target.name))
        except Exception as exc:
            failed.append(f"{s.name}: {exc}")
    return written, failed, stamp_dir
