# Wegpunkt

Ein kleiner, selbst gehosteter Aktivitäten-Tracker im Stil von Strava, der mit dem
Fediverse spricht. Eine Instanz gehört genau einer Person.

- Vier Sportarten: **Gravel**, **Enduro MTB**, **Joggen** (per GPX-Datei) und **Calisthenics**
  (von Hand eingetragen: Übungen mit Sätzen, Wiederholungen oder Haltezeit, Zusatzgewicht)
- GPX-Dateien hochladen: Distanz, Zeit, Tempo bzw. Geschwindigkeit, Anstieg und Abfahrt werden berechnet
- Aktivitätsseite mit Karte (normale Karte oder Topo), interaktivem Höhenprofil, das mit der
  Karte verbunden ist, und Satz-Diagrammen bei Workouts
- Startseite mit Wochenübersicht und Trainingszeit der letzten 12 Wochen
- **Keine Registrierung**: Es gibt nur ein Passwort für dich. Nach 5 Fehlversuchen ist die
  Anmeldung 15 Minuten gesperrt.
- **Privatzone**: Rund um Start und Ziel wird die Strecke öffentlich nicht gezeigt (Standard 200 m)
- **Fediverse**: Man kann dir von Mastodon, GoToSocial usw. folgen (`@du@deine-domain`).
  Jede neue Aktivität erscheint dort als Beitrag mit Routenbild, Werten und Hashtag.
- Likes, Boosts und Antworten aus dem Fediverse erscheinen unter der Aktivität
- Löschen einer Aktivität löscht sie auch bei den Followern

## Lokal ausprobieren

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
WEGPUNKT_PASSWORD=test .venv/bin/python -m wegpunkt
```

Dann <http://localhost:8000> öffnen, mit dem Passwort anmelden und eine GPX-Datei
hochladen (z. B. `tests/data/run.gpx`).

Tests: `.venv/bin/python -m pytest`

Föderation funktioniert lokal nicht, weil andere Server deine Instanz erreichen müssen.

## Auf einem Server betreiben

Du brauchst eine eigene (Sub-)Domain mit HTTPS, z. B. `sport.example.de`.

1. Projekt auf den Server kopieren und wie oben installieren.
2. `.env.example` nach `.env` kopieren und ausfüllen. **Domain und Benutzername später
   nicht mehr ändern**, sonst finden dich deine Follower nicht mehr.
3. Starten, z. B. mit systemd:

   ```ini
   [Service]
   WorkingDirectory=/opt/wegpunkt
   EnvironmentFile=/opt/wegpunkt/.env
   ExecStart=/opt/wegpunkt/.venv/bin/python -m wegpunkt
   Restart=always
   ```

4. Einen Reverse-Proxy davorsetzen, z. B. Caddy (kümmert sich auch um das Zertifikat):

   ```
   sport.example.de {
       reverse_proxy 127.0.0.1:8000
   }
   ```

   Wichtig: Der Proxy muss den `Host`-Header unverändert durchreichen (Caddy und nginx mit
   `proxy_set_header Host $host;` tun das), sonst schlägt die Signaturprüfung fehl.

5. In Mastodon nach `@benutzername@sport.example.de` suchen und folgen.

Alle Daten liegen im Ordner `data/`: Datenbank, Original-GPX-Dateien, Routenbilder und der
private Schlüssel deines Accounts. **Diesen Ordner sichern.** Geht der Schlüssel verloren,
können andere Server deine Beiträge nicht mehr zuordnen.

## Aufbau

| Datei | Inhalt |
| --- | --- |
| `wegpunkt/app.py` | Webseiten und ActivityPub-Endpunkte (WebFinger, Actor, Inbox, Outbox, NodeInfo) |
| `wegpunkt/federation.py` | ActivityPub-Objekte bauen, fremde Accounts laden, zustellen, Inbox verarbeiten |
| `wegpunkt/httpsig.py` | HTTP-Signaturen erstellen und prüfen |
| `wegpunkt/tracks.py` | Sportarten, GPX auswerten, Privatzone, Routenbild |
| `wegpunkt/workouts.py` | Calisthenics-Workouts: Sätze einlesen, Summen |
| `wegpunkt/stats.py` | Wochenübersicht und Diagramm |
| `wegpunkt/db.py` | SQLite-Datenbank |
| `wegpunkt/config.py` | Einstellungen aus Umgebungsvariablen |

## Ideen für die nächsten Schritte

- FIT-Dateien (Garmin, Wahoo) unterstützen, Herzfrequenz und Trittfrequenz anzeigen
- Bearbeiten von Titel und Beschreibung (mit `Update` an die Follower)
- Sichtbarkeit pro Aktivität (öffentlich, nur Follower, privat)
- Routenbild mit Kartenhintergrund statt einfarbig
- Eigenen Accounts im Fediverse folgen und deren Aktivitäten in einem Feed sehen
- Strukturierte Felder (Distanz, Dauer, Sportart) im ActivityPub-Objekt, damit andere
  Fitness-Instanzen Aktivitäten maschinenlesbar übernehmen können
- Bestzeiten, Wochen- und Jahresstatistiken
- Zustellungen bei Fehlern später erneut versuchen (Warteschlange)

Leaflet liegt in `wegpunkt/static/leaflet/` (BSD-2-Clause), die Schriften Inter und Barlow
Condensed in `wegpunkt/static/fonts/` (SIL Open Font License). Die Lizenzen liegen jeweils daneben.
