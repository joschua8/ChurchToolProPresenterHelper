# Liedbrücke

**Die Brücke zwischen ChurchTools und ProPresenter 7.**
Liedbrücke holt die Lieder (SongBeamer-Dateien) aus ChurchTools, wandelt sie in ProPresenter-Präsentationen um
und macht aus dem Ablaufplan eines Gottesdienstes mit einem Klick eine fertige ProPresenter-Playlist –
inklusive Predigtfolien, Vaterunser und Stille-Bild.

Liedbrücke läuft lokal auf dem Rechner, auf dem ProPresenter läuft (macOS oder Windows), und wird im Browser bedient.
Es ist ein einzelnes Programm, eine Python-Installation ist nicht nötig.

> Liedbrücke ist ein privates Gemeindeprojekt und steht in keiner Verbindung zu ChurchTools (ECGroup) oder
> Renewed Vision (ProPresenter).

---

## Funktionen

| Bereich | Was es tut |
|---|---|
| **Ablaufplan → Playlist** | Termin in ChurchTools wählen → Ablaufplan ansehen → `.proPlaylist` herunterladen. Überschriften und Ablaufpunkte werden farbige Kopfzeilen, Lieder verweisen auf die vorhandenen Präsentationen in deiner ProPresenter-Bibliothek. |
| **Automatische Einträge** | Unter „Vaterunser“ kommt die Vaterunser-Präsentation (per CCLI-Nummer gefunden), unter jedem Punkt mit „Stille“ ein zufälliges Bild aus deinem Stille-Ordner. |
| **Dateien am Termin** | PowerPoint, PDF, Bilder, Videos und Audio aus dem Reiter „Dateien“ des Termins werden geladen und unter dem passenden Ablaufpunkt eingefügt (z. B. „Predigt …pptx“ unter „Predigt“). PowerPoint/PDF werden dafür in Folienbilder umgewandelt. |
| **Liedzuordnung** | ChurchTools-Liedname ↔ Präsentation: exakt, ohne Satzzeichen/CCLI-Nummer, zweisprachige Titel, ähnliche Namen („bitte prüfen“). Jede Zuordnung lässt sich in der Oberfläche ändern. |
| **Lieder-Import** | Alle SongBeamer-Dateien (`.sng`) aus ChurchTools in eine lokale Liederdatenbank laden; bei jedem weiteren Import nur neue und geänderte. |
| **Lieder hochladen** | Lieder ohne `.sng` als CCLI-SongSelect-Datei (`.usr`/`.txt`) oder `.sng` per Drag & Drop hinzufügen. |
| **Export nach ProPresenter** | `.sng` → `.pro`, mit einstellbarer Formatierung (Schrift, Größe, Farben, Zeilen pro Folie, Textbox per Maus verschieben), Folienvorschau und Tastenkürzeln je Liedteil (Strophe 1 = A, Refrain = C, Bridge = B …). |
| **Abgleich** | Vergleicht die Liederdatenbank mit deiner ProPresenter-Lieder-Bibliothek: was fehlt, was hat anderen Text – und aktualisiert auf Wunsch (mit Sicherung). |
| **Selbst-Update** | Beim Start sucht das Programm auf GitHub nach einer neuen Version und aktualisiert sich selbst. |

## Installation

1. Unter [**Releases**](../../releases/latest) die passende ZIP laden:
   - `Liedbruecke-Windows.zip`
   - `Liedbruecke-macOS-AppleSilicon.zip` (Macs mit M1/M2/M3/M4)
   - `Liedbruecke-macOS-Intel.zip` (ältere Macs)
2. Entpacken und die Programmdatei in einen Ordner legen, in den du schreiben darfst (Desktop, Programme …) –
   sonst kann sie sich nicht selbst aktualisieren.
3. Starten:
   - **Windows:** `Liedbruecke.exe` doppelklicken. Bei „Der Computer wurde durch Windows geschützt“:
     „Weitere Informationen“ → „Trotzdem ausführen“.
   - **macOS:** beim ersten Mal **Rechtsklick → Öffnen** (das Programm ist nicht bei Apple signiert).
4. Der Browser öffnet sich mit der Oberfläche (`http://127.0.0.1:5005`). Das Terminal-Fenster offen lassen –
   wenn man es schließt, wird das Programm beendet.

Für PowerPoint-Dateien im Ablaufplan braucht es [LibreOffice](https://www.libreoffice.org) (kostenlos) oder –
nur unter Windows – ein installiertes Microsoft PowerPoint. PDF, Bilder, Videos und Audio gehen ohne.

## Bedienung

### 1. Einrichten (einmalig, „Einstellungen“)

- **ChurchTools-Anmeldung:** Adresse deiner ChurchTools-Instanz, Benutzername, Passwort (ggf. Zwei-Faktor-Code).
  Das Passwort wird nicht gespeichert. Optional: mit Login-Token anmelden und den Token speichern.
- **ProPresenter:** Den Arbeitsordner findet das Programm in der Regel selbst. Sonst den Ordner der
  Lieder-Bibliothek eintragen, z. B.
  - macOS: `~/Library/Application Support/RenewedVision/ProPresenter/…/Libraries/Songs`
  - Windows: `%APPDATA%\RenewedVision\ProPresenter\LocalWorkspaces\ProPresenter\Libraries\Songs`
- **Stille-Bilder:** Ordner mit `.jpg`/`.png`-Bildern. Je Ablaufpunkt „Stille“ wird eins zufällig ausgewählt.
- **Bibliotheken-Reihenfolge:** Gibt es ein Lied in mehreren Bibliotheken, gewinnt die obere.

### 2. Playlist für den Gottesdienst („ChurchTools → Ablaufpläne“)

1. Termin auswählen. Der Ablaufplan erscheint mit der Zuordnung jedes Liedes:
   grün = gefunden, gelb = ähnlicher Name (bitte prüfen), rot = fehlt.
2. Bei Bedarf Zuordnungen über „ändern“ korrigieren und Punkte per Häkchen ab- oder anwählen.
3. Rechts **„.proPlaylist herunterladen“** und die Datei in ProPresenter öffnen (Doppelklick oder in die
   Playlist-Liste ziehen).

> **Windows:** ProPresenter zeigt die Lieder einer Playlist dort nur an, wenn sie mitgeliefert werden.
> Deshalb ist unter Windows „Lieder in die Playlist-Datei packen“ eingeschaltet; ProPresenter fragt beim Öffnen,
> ob vorhandene Präsentationen ersetzt werden sollen. Unter macOS bleibt es aus (sonst entstehen Duplikate).

### 3. Lieder pflegen

- **ChurchTools → Lieder-Import:** lädt alle `.sng` (Zwei-Faktor-Code ggf. vorher eintragen).
- **Lieder → Hochladen:** SongSelect- oder `.sng`-Dateien hineinziehen.
- **ProPresenter → Export & Formatierung:** Lieder auswählen, Formatierung einstellen, als ZIP mit `.pro`-Dateien laden
  und in ProPresenter importieren.
- **ProPresenter → Abgleich:** zeigt Unterschiede zwischen Datenbank und ProPresenter-Bibliothek.

### Daten

Lieder und Einstellungen liegen im Ordner `Liedbruecke` in deinem Benutzerordner (bzw. `Liederverwaltung` aus
früheren Versionen). Zum Umziehen einfach den Ordner mitkopieren. Anderer Ort: Umgebungsvariable `LIEDBRUECKE_DATA`.

### Updates

Beim Start und danach alle 6 Stunden wird nach einer neuen Version gesucht. Beim Start wird sie automatisch
installiert, im laufenden Betrieb erscheint oben ein Hinweis „Jetzt aktualisieren“.
Abschalten unter **Einstellungen → Programm-Updates**.

## Entwicklung

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py            # http://127.0.0.1:5005
```

| Datei | Aufgabe |
|---|---|
| `app.py` | Flask-Server (nur `127.0.0.1`) und REST-API |
| `web/index.html` | komplette Oberfläche, kein Build-Schritt |
| `download_songs.py` | ChurchTools-Client und Lieder-Import (auch als CLI) |
| `playlist.py` | Ablaufplan → `.proPlaylist`, Liedzuordnung, Medien-Präsentationen |
| `sng.py`, `songselect.py` | SongBeamer- und SongSelect-Dateien lesen/schreiben |
| `pro_export.py`, `propresenterFormatter/` | `.sng` → ProPresenter-7-`.pro` |
| `pp_sync.py` | Abgleich Liederdatenbank ↔ ProPresenter-Bibliothek |
| `updater.py` | Selbst-Update über GitHub-Releases |

Jeder Push auf `master` baut über GitHub Actions die Programme für macOS und Windows (PyInstaller) und
veröffentlicht sie als Release – von dort holen sich die installierten Programme ihre Updates.

## Credits

- **[greyshirtguy/ProPresenter7-Proto](https://github.com/greyshirtguy/ProPresenter7-Proto)** – die
  reverse-engineerten Protobuf-Definitionen des ProPresenter-7-Dateiformats. Ohne sie wäre die Umwandlung
  von SongBeamer-Dateien in ProPresenter-Präsentationen (und das Schreiben von Playlists) nicht möglich.
  Die daraus erzeugten `*_pb2.py` in `propresenterFormatter/` stehen unter der MIT-Lizenz von greyshirtguy
  (siehe [`propresenterFormatter/LICENSE-ProPresenter7-Proto`](propresenterFormatter/LICENSE-ProPresenter7-Proto)).
  Vielen Dank!
- [ChurchTools](https://church.tools) für die offene REST-API.
- ProPresenter ist eine Marke von Renewed Vision, ChurchTools eine Marke der ECGroup GmbH.
