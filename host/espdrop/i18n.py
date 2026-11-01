# EspDrop - the app in German or English.
#
# The texts in the code are German; _() gives them in the language chosen
# (Allgemein -> Sprache: Automatisch, Deutsch, English). "Automatisch" is the
# system's language: German for a German system, English for any other.
# Texts with values use str.format placeholders, so the English sentence may
# put them elsewhere: _("Gesendet an {name}").format(name=...).
#
# The English texts are in EN below; test_i18n.py checks that every _("...")
# in the app has one, and that no German text is shown without _().
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import locale
import os

LANGUAGES = [("auto", "Automatisch"), ("de", "Deutsch"), ("en", "English")]
LANG = "de"                                    # in use now: "de" or "en"


def system_language():
    """'de' on a German system, else 'en' (ESPDROP_LANG overrides)."""
    forced = os.environ.get("ESPDROP_LANG", "").lower()
    if forced in ("de", "en"):
        return forced
    candidates = [os.environ.get(k, "") for k in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE")]
    try:
        from PySide6.QtCore import QLocale
        candidates.insert(0, QLocale.system().name())         # Windows: the UI language
    except ImportError:
        pass
    try:
        candidates.append(locale.getlocale()[0] or "")
    except ValueError:
        pass
    for c in candidates:
        if c:
            return "de" if c.lower().startswith("de") else "en"
    return "en"


def set_language(choice):
    """choice: 'auto', 'de' or 'en'; returns the language now in use."""
    global LANG
    LANG = choice if choice in ("de", "en") else system_language()
    return LANG


def N_(text):
    """Marks a text for translation where it is defined (tables); _() at use."""
    return text


def _(text):
    """text (German, as in the code) in the language in use."""
    if LANG == "en":
        return EN.get(text, text)
    return text


EN = {
    # ---- the window -------------------------------------------------------
    "AirDrop": "AirDrop",
    "Mehr": "More",
    "Protokoll anzeigen": "Show log",
    "Protokoll": "Log",
    "ESP-Firmware …": "ESP firmware …",
    "Serieller Port …": "Serial port …",
    "Serieller Port des ESP:": "The ESP's serial port:",
    "automatisch": "automatic",
    "Beenden": "Quit",
    "Suche ESP …": "Looking for the ESP …",
    "ESP verbunden": "ESP connected",
    "ESP {mac}": "ESP {mac}",
    "Kein ESP": "No ESP",
    "ESP antwortet nicht": "ESP does not answer",
    "Firmware installieren …": "Install firmware …",
    "Firmware wird installiert …": "Installing firmware …",
    "Beendet": "Stopped",
    "Empfangen von": "Receiving from",
    "Aus": "Off",
    "10 Minuten": "10 minutes",
    "Für alle": "Everyone",
    "Niemand kann dir gerade etwas senden.": "Nobody can send you anything right now.",
    "Für alle sichtbar – noch {left}": "Visible to everyone – {left} left",
    "abgelehnt": "declined",
    "Für alle sichtbar, bis du es ausschaltest.": "Visible to everyone until you turn it off.",
    "Name": "Name",
    "So sehen dich iPhone, iPad und Mac.": "How iPhone, iPad and Mac see you.",
    "Empfangene Dateien": "Received files",
    "Ändern …": "Change …",
    "Öffnen": "Open",
    "Ordner für empfangene Dateien": "Folder for received files",
    "In die Zwischenablage kopieren": "Copy to the clipboard",
    "Empfangenes zusätzlich zum Einfügen bereithalten: Bilder, Dateien und Links.":
        "Also keep what arrives ready to paste: pictures, files and links.",
    "Geräte in der Nähe": "Nearby devices",
    "Suchen": "Search",
    "Suche läuft …": "Searching …",
    "Suche nach Geräten in der Nähe …": "Looking for nearby devices …",
    "„Suchen“ findet AirDrop-Geräte in der Nähe. Am iPhone oder iPad: AirDrop „Für alle“, "
    "entsperrt.":
        "“Search” finds AirDrop devices nearby. On the iPhone or iPad: AirDrop “Everyone”, "
        "unlocked.",
    "Dateien auf ein Gerät ziehen oder es anklicken.": "Drop files on a device or click it.",
    "Übertragungen": "Transfers",
    "Allgemein": "General",
    "Erscheinungsbild": "Appearance",
    "Automatisch": "Automatic",
    "Hell": "Light",
    "Dunkel": "Dark",
    "Sprache": "Language",
    "Gilt nach einem Neustart der App.": "Takes effect when the app restarts.",
    "Jetzt neu starten": "Restart now",
    "Beim Anmelden im Hintergrund starten": "Start in the background at login",
    "Unsichtbar im Infobereich und für alle sichtbar; nur eingehende Dateien melden sich.":
        "Hidden in the notification area and visible to everyone; only incoming files "
        "show up.",
    "Im Rechtsklickmenü des Explorers anbieten": "Offer in Explorer's right-click menu",
    "„Senden an → EspDrop (AirDrop)“ und „Mit EspDrop senden“":
        "“Send to → EspDrop (AirDrop)” and “Send with EspDrop”",
    "Im Dateimanager anbieten": "Offer in the file manager",
    "„Öffnen mit → AirDrop (EspDrop)“": "“Open with → AirDrop (EspDrop)”",
    "ESP-Firmware": "ESP firmware",
    "Installieren …": "Install …",
    "Aktualisieren …": "Update …",
    "EspDrop öffnen": "Open EspDrop",
    "Dateien senden …": "Send files …",
    "Für alle sichtbar (10 Minuten)": "Visible to everyone (10 minutes)",
    "Nicht mehr sichtbar": "No longer visible",
    "Das ging nicht: {error}": "That did not work: {error}",
    "Läuft im Hintergrund weiter (Symbol im Infobereich).":
        "Keeps running in the background (icon in the notification area).",
    "Die Firmware wird gerade installiert; danach beenden.":
        "The firmware is being installed; quit afterwards.",
    "Gesendet an {name}": "Sent to {name}",
    "Nicht gesendet an {name}": "Not sent to {name}",
    "{name} möchte {what} teilen": "{name} wants to share {what}",
    "Link von {name}": "Link from {name}",
    " – in der Zwischenablage": " – on the clipboard",
    " – klicken zum Öffnen": " – click to open",
    "Wird empfangen …": "Receiving …",
    "Empfangen · {size}": "Received · {size}",
    "Empfangen: {names}": "Received: {names}",
    "Unvollständig": "Incomplete",
    "Ordner lassen sich noch nicht senden, nur Dateien.":
        "Folders cannot be sent yet, only files.",
    "Senden geht nicht: {error}": "Cannot send: {error}",
    "An {name}": "To {name}",
    "das Gerät": "the device",
    # transfer states
    "Warten …": "Waiting …",
    "Warten auf Annahme …": "Waiting for accept …",
    "Senden …": "Sending …",
    "Gesendet": "Sent",
    "Abgelehnt": "Declined",
    "Fehlgeschlagen": "Failed",
    "Abgebrochen": "Cancelled",
    # what is shared
    "einen Link": "a link",
    "{n} Links": "{n} links",
    "ein Foto": "a photo",
    "{n} Fotos": "{n} photos",
    "ein Video": "a video",
    "{n} Videos": "{n} videos",
    "eine Datei": "a file",
    "{n} Dateien": "{n} files",
    "etwas": "something",
    # the radio
    "Funk immer an": "Radio always on",
    "Funk aus (spart Strom)": "Radio off (saves power)",
    "Funk schläft (empfangsbereit)": "Radio asleep (ready to receive)",
    "Funk an": "Radio on",
    "Funk an ({why})": "Radio on ({why})",
    "für die App": "for the app",
    "zum Senden": "for sending",
    "Übertragung": "transfer",
    "Gerät in der Nähe": "device nearby",
    # ---- the accept card ----------------------------------------------------
    "„{name}“ möchte {what} teilen.": "“{name}” wants to share {what}.",
    "{names} und {n} weitere": "{names} and {n} more",
    "Ablehnen": "Decline",
    "Annehmen": "Accept",
    "Ohne Antwort abgelehnt in {secs} s": "Declined without an answer in {secs} s",
    # ---- the AirDrop sheet ----------------------------------------------------
    "Hinzufügen …": "Add …",
    "Abbrechen": "Cancel",
    "Keine Datei gewählt": "No file chosen",
    "„Hinzufügen …“ wählt Dateien (Ordner gehen noch nicht).":
        "“Add …” chooses files (folders do not work yet).",
    "Per AirDrop senden": "Send with AirDrop",
    "Verbinde mit dem ESP …": "Connecting to the ESP …",
    "Suche nach Geräten in der Nähe …\nAm iPhone oder iPad: AirDrop „Für alle“, entsperrt.":
        "Looking for nearby devices …\nOn the iPhone or iPad: AirDrop “Everyone”, unlocked.",
    "Zum Senden auf ein Gerät klicken.": "Click a device to send.",
    "Warten, bis {name} annimmt …": "Waiting for {name} to accept …",
    "Senden an {name} …": "Sending to {name} …",
    "Fertig": "Done",
    "{name} hat abgelehnt.": "{name} declined.",
    "Abgebrochen.": "Cancelled.",
    "Fehlgeschlagen: {error}": "Failed: {error}",
    " Zum erneuten Versuch auf ein Gerät klicken.": " Click a device to try again.",
    "nicht in der Nähe": "not nearby",
    " (Name noch unbekannt)": " (name not known yet)",
    "Noch keine Übertragungen.": "No transfers yet.",
    "Senden … {percent} %": "Sending … {percent} %",
    "Im Ordner zeigen": "Show in folder",
    # ---- the firmware dialog ---------------------------------------------------
    "Die App bringt die Firmware für den ESP32-C5 mit und spielt sie über das USB-Kabel "
    "auf; Tasten am ESP braucht es dafür nicht.":
        "The app comes with the firmware for the ESP32-C5 and puts it on over the USB "
        "cable; no buttons on the ESP are needed.",
    "Auf dem ESP": "On the ESP",
    "Andere Datei …": "Other file …",
    "Zum Installieren": "To install",
    "Anschluss": "Port",
    "Schließen": "Close",
    "Installieren": "Install",
    "Keine Firmware dabei: eine Datei wählen (espdrop-esp32c5.bin aus einem Release).":
        "No firmware included: choose a file (espdrop-esp32c5.bin from a release).",
    "EspDrop {version} für den {chip}, gebaut {built}":
        "EspDrop {version} for the {chip}, built {built}",
    "Firmware wählen": "Choose firmware",
    "ESP-Firmware (*.bin flasher_args.json);;Alle Dateien (*)":
        "ESP firmware (*.bin flasher_args.json);;All files (*)",
    "Kein ESP gefunden: per USB anstecken.": "No ESP found: plug it in over USB.",
    "Mehrere gefunden: den ESP wählen.": "Several found: choose the ESP.",
    "Gerade läuft eine Übertragung; danach installieren.":
        "A transfer is running; install afterwards.",
    "Schreibe die Firmware …": "Writing the firmware …",
    "Schreibe die Firmware … {percent} %": "Writing the firmware … {percent} %",
    "Prüfe, ob alles angekommen ist …": "Checking that everything arrived …",
    "Starte den ESP neu …": "Restarting the ESP …",
    "Flashen fehlgeschlagen: {error}": "Flashing failed: {error}",
    "Erneut versuchen": "Try again",
    "Fertig: EspDrop {version} ist auf dem ESP. Er startet neu und verbindet sich gleich "
    "wieder.":
        "Done: EspDrop {version} is on the ESP. It restarts and reconnects in a moment.",
    "Erneut installieren": "Install again",
    "Wird gerade installiert …": "Being installed …",
    "EspDrop, eine ältere Firmware ohne Versionsangabe":
        "EspDrop, an older firmware without a version",
    "EspDrop {version} auf dem {chip}": "EspDrop {version} on the {chip}",
    "EspDrop {version}": "EspDrop {version}",
    "Ein ESP steckt, antwortet aber nicht: keine oder eine andere Firmware":
        "An ESP is plugged in but does not answer: no firmware, or another one",
    "Kein ESP verbunden": "No ESP connected",
    " · neu in der App: {version}": " · new in the app: {version}",
    # flash.py
    "{path} lässt sich nicht lesen: {error}": "{path} cannot be read: {error}",
    "Das ist nur der App-Teil einer Firmware. Gebraucht wird das vollständige Image "
    "({name}) oder der Build-Ordner.":
        "This is only the app part of a firmware. Needed is the complete image ({name}) "
        "or the build folder.",
    "Keine ESP-Firmware: es fehlt die App an 0x10000.":
        "Not ESP firmware: there is no app at 0x10000.",
    "Firmware für einen anderen Chip (Kennung {chip}).":
        "Firmware for another chip (id {chip}).",
    "Kein vollständiges Image: der Bootloader für den {chip} fehlt an {offset}.":
        "Not a complete image: the bootloader for the {chip} is missing at {offset}.",
    "Kein vollständiges Image: die Partitionstabelle fehlt an 0x8000.":
        "Not a complete image: the partition table is missing at 0x8000.",
    "{folder} ist kein ESP-IDF-Build-Ordner ({error}).":
        "{folder} is not an ESP-IDF build folder ({error}).",
    "Dieser Build ist die Test-Konsole, nicht die Firmware für die App: mit "
    "sdkconfig.bridge.defaults bauen (firmware/README.md).":
        "This build is the test console, not the firmware for the app: build with "
        "sdkconfig.bridge.defaults (firmware/README.md).",
    "{name} fehlt im Build ({error}).": "{name} is missing in the build ({error}).",
    "Die App im Build ({name}) ist kein ESP-Image.": "The app in the build ({name}) is not an ESP image.",
    "{name} ist für einen anderen Chip als die App.": "{name} is for another chip than the app.",
    "esptool fehlt: tools/opendrop_install.py installiert es (oder pip install esptool).":
        "esptool is missing: tools/opendrop_install.py installs it (or pip install esptool).",
    "Am Anschluss {port} steckt ein {found}; diese Firmware ist für den {chip}.":
        "Port {port} has an {found}; this firmware is for the {chip}.",
    "Der Anschluss {port} ist belegt oder gesperrt: hat ihn ein anderes Programm offen "
    "(awdl_bridge.py, ein serieller Monitor)?":
        "Port {port} is busy or locked: does another program have it open (awdl_bridge.py, "
        "a serial monitor)?",
    " Unter Linux muss der Benutzer in der Gruppe 'dialout' sein.":
        " On Linux the user must be in the group 'dialout'.",
    "Der ESP ließ sich nicht in den Bootloader schalten. Abstecken, die BOOT-Taste "
    "gedrückt halten, wieder anstecken, loslassen und erneut versuchen.":
        "The ESP could not be switched into its bootloader. Unplug it, hold the BOOT "
        "button, plug it in again, let go and try again.",
    # ---- installing (install.py) ---------------------------------------------
    "Eine laufende EspDrop-App ließ sich nicht beenden. Bitte beenden und erneut starten.":
        "A running EspDrop app could not be closed. Please quit it and start again.",
    "EspDrop ließ sich nicht installieren ({error}); es läuft jetzt von hier.":
        "EspDrop could not be installed ({error}); it runs from here now.",
    "EspDrop ist installiert und im Startmenü.": "EspDrop is installed and in the Start menu.",
    "EspDrop ist installiert und im Anwendungsmenü.":
        "EspDrop is installed and in the applications menu.",
    "Deinstallieren …": "Uninstall …",
    "EspDrop von diesem PC entfernen? Empfangene Dateien bleiben erhalten.":
        "Remove EspDrop from this PC? Received files are kept.",
    "Entfernen": "Remove",
    "EspDrop ist entfernt.": "EspDrop has been removed.",
    "Einiges ließ sich nicht entfernen: {errors}": "Some things could not be removed: {errors}",
    # ---- desktop entries (integration.py) --------------------------------------
    "Mit EspDrop senden": "Send with EspDrop",
    "Per AirDrop senden (EspDrop)": "Send with AirDrop (EspDrop)",
    "AirDrop über den ESP32 – empfangsbereit im Hintergrund":
        "AirDrop over the ESP32 – ready to receive in the background",
    "Dateien per AirDrop an ein iPhone/iPad senden": "Send files to an iPhone/iPad with AirDrop",
    "AirDrop mit iPhone, iPad und Mac über den ESP32-C5":
        "AirDrop with iPhone, iPad and Mac over the ESP32-C5",
}
