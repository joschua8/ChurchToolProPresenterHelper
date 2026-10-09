# Liederverwaltung – Programm starten

Die Liederverwaltung ist ein einzelnes Programm, eine Python-Installation braucht es nicht.
Nach dem Start öffnet sich der Browser mit der Oberfläche (http://127.0.0.1:5005).
Das Terminal-Fenster dabei offen lassen; wenn man es schließt, wird das Programm beendet.

## macOS

1. ZIP entpacken, die Datei `Liederverwaltung` z. B. in den Ordner „Programme“ legen.
2. Beim **ersten Start** mit **Rechtsklick → Öffnen** starten und „Öffnen“ bestätigen
   (das Programm ist nicht bei Apple signiert, deshalb fragt macOS nach).
   Falls macOS das Öffnen trotzdem verweigert: Systemeinstellungen → Datenschutz & Sicherheit →
   „Dennoch öffnen“. Oder im Terminal einmalig:
   `xattr -d com.apple.quarantine /Pfad/zu/Liederverwaltung`
3. Danach reicht ein Doppelklick.

Es gibt zwei Fassungen: **AppleSilicon** (Macs ab Ende 2020 mit M1/M2/M3/M4) und **Intel** (ältere Macs).
Unter Apple-Menü → „Über diesen Mac“ steht, welcher Chip verbaut ist.

## Windows

1. ZIP entpacken, `Liederverwaltung.exe` doppelklicken.
2. Falls „Der Computer wurde durch Windows geschützt“ erscheint: „Weitere Informationen“ →
   „Trotzdem ausführen“.

## Wo liegen meine Daten?

Im Ordner **Liederverwaltung** in deinem Benutzerordner
(macOS: `/Users/<Name>/Liederverwaltung`, Windows: `C:\Users\<Name>\Liederverwaltung`):

- `songs/` – die Liederdatenbank (.sng-Dateien)
- `app_settings.json`, `playlist_settings.json`, `export_settings.json` – Einstellungen
- `backup/` – Sicherungen überschriebener ProPresenter-Dateien

Zum Umziehen auf einen anderen Rechner einfach diesen Ordner mitkopieren.
Ein anderer Ort lässt sich über die Umgebungsvariable `LIEDERVERWALTUNG_DATA` einstellen.

Die ProPresenter-Bibliothek, den Ordner mit den Stille-Bildern und die ChurchTools-Anmeldung
stellst du in der Oberfläche unter **Einstellungen** ein.

## Lieder ohne ChurchTools hinzufügen

Unter **Lieder → Hochladen** Dateien aus CCLI SongSelect (`.usr` oder `.txt`) oder SongBeamer-Dateien
(`.sng`) hineinziehen. Gibt es ein Lied mit gleichem Titel schon, wird es nicht überschrieben.

## PowerPoint-Dateien im Ablaufplan

Präsentationen (.pptx/.ppt), die in ChurchTools am Termin hängen, werden für die Playlist in Bilder
umgewandelt. Dafür braucht es **LibreOffice** (kostenlos, libreoffice.org) oder – nur unter
Windows – ein installiertes **Microsoft PowerPoint**. PDF, Bilder, Videos und Audio gehen ohne.
Andere Dateien (z. B. .docx) werden gar nicht erst heruntergeladen; ihre Namen stehen in einer orangen
Kopfzeile ganz oben, damit nichts übersehen wird.
