#!/usr/bin/env python3
"""CCLI-SongSelect-Downloads -> Song (für die Liederdatenbank als .sng).

Unterstützt:
  * .usr  – „SongShow Plus“/USR-Format: INI-artig, Abschnitte in `Fields=`/`Words=` durch „/t“ getrennt,
            Zeilen durch „/n“; CCLI-Nummer im Kopf `[S A1234567]`.
  * .txt  – Textdatei von SongSelect: Titel, Abschnitte mit Überschrift („Verse 1“, „Chorus“ …),
            am Ende ein Block ab „CCLI Song #“ / „CCLI-Liednummer“ mit Autoren und Copyright.
  * .sng  – SongBeamer-Datei, wird unverändert übernommen.

Die Abschnitte werden als Freitext an sng.parse_freetext übergeben – dieselbe Erkennung der
Abschnittsnamen (Verse/Strophe, Chorus/Refrain …) wie beim Rest des Programms.
"""

from __future__ import annotations

import re

from songbridge.songs.sng import Song, parse_freetext, parse_sng

EXTENSIONS = (".usr", ".txt", ".sng")

# Fußzeile der .txt-Datei („CCLI Song # 7113062“, deutsch „CCLI-Liednummer 7113062“)
_TXT_CCLI_RE = re.compile(r"^\s*CCLI[- ]?(?:Song\s*#|Liednummer|Song Number|Nr\.?)\s*:?\s*(\d+)", re.I)
_TXT_FOOTER_RE = re.compile(r"^\s*(CCLI[- ]?(?:Song|Lied|Lizenz|License)|For use solely|Nur zur Verwendung|Verwendung nur)", re.I)


class SongSelectError(ValueError):
    pass


def _clean(text: str) -> str:
    return text.replace("﻿", "").replace("\r\n", "\n").replace("\r", "\n")


def _song(title: str, author: str, ccli: str, copyright_: str, blocks: list[tuple[str, list[str]]]) -> tuple[Song, list[str]]:
    freetext = "\n\n".join("\n".join(([name] if name else []) + lines) for name, lines in blocks if lines or name)
    sections, order, warnings = parse_freetext(freetext)
    if not sections:
        raise SongSelectError("Kein Liedtext gefunden.")
    title = " ".join(title.split())
    if not title:
        raise SongSelectError("Kein Titel gefunden.")
    return Song(title=title, author=author.strip(), ccli=ccli.strip(), copyright=copyright_.strip(),
                sections=sections, order=order), warnings


def parse_usr(text: str) -> tuple[Song, list[str]]:
    fields: dict[str, str] = {}
    ccli = ""
    for line in _clean(text).split("\n"):
        m = re.match(r"^\s*\[S\s+A?(\d+)\]", line)
        if m:
            ccli = m.group(1)
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            fields.setdefault(key.strip().lower(), value)
    names = [n.strip() for n in fields.get("fields", "").split("/t")]
    words = fields.get("words", "").split("/t")
    blocks = []
    for i, body in enumerate(words):
        lines = [l.strip() for l in body.split("/n") if l.strip()]
        if lines:
            blocks.append((names[i] if i < len(names) else "", lines))
    author = " | ".join(a.strip() for a in fields.get("author", "").split("|") if a.strip())
    return _song(fields.get("title", ""), author, ccli, fields.get("copyright", "").replace("/n", " "), blocks)


def parse_txt(text: str) -> tuple[Song, list[str]]:
    lines = _clean(text).split("\n")
    # Fußzeile abtrennen
    footer_at = next((i for i, l in enumerate(lines) if _TXT_FOOTER_RE.match(l)), len(lines))
    body, footer = lines[:footer_at], lines[footer_at:]
    ccli = next((m.group(1) for l in footer if (m := _TXT_CCLI_RE.match(l))), "")
    # Nach der CCLI-Zeile: Autoren, dann „© …“
    rest = [l.strip() for l in footer if l.strip() and not _TXT_FOOTER_RE.match(l)]
    copyright_ = next((l.lstrip("©").strip() for l in rest if l.startswith("©")), "")
    author = next((l for l in rest if not l.startswith("©")), "")
    # Titel = erste nichtleere Zeile
    while body and not body[0].strip():
        body.pop(0)
    if not body:
        raise SongSelectError("Leere Datei.")
    title = body.pop(0).strip()
    blocks, cur = [], []
    for l in body + [""]:
        if l.strip():
            cur.append(l.strip())
        elif cur:
            blocks.append(("", cur))
            cur = []
    return _song(title, author, ccli, copyright_, blocks)


def parse_upload(filename: str, data: bytes) -> tuple[Song | None, str, list[str]]:
    """-> (Song, "", Hinweise) für .usr/.txt; (None, sng-Text, []) für .sng (unverändert speichern)."""
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    if ext not in EXTENSIONS:
        raise SongSelectError(f"Dateityp {ext or '(ohne Endung)'} wird nicht unterstützt – nur {', '.join(EXTENSIONS)}.")
    for enc in ("utf-8-sig", "cp1252"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if ext == ".sng":
        song = parse_sng(text)
        if not song.title or not song.sections:
            raise SongSelectError("Keine gültige SongBeamer-Datei.")
        return None, text, []
    song, warnings = parse_usr(text) if ext == ".usr" else parse_txt(text)
    return song, "", warnings
