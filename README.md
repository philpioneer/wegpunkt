# Wegpunkt

Ein kleiner, selbst gehosteter Aktivitäten-Tracker im Stil von Strava, der mit dem
Fediverse spricht. Eine Instanz gehört genau einer Person.

## Was es kann

**Sport**
- Vier Sportarten: **Gravel**, **Enduro MTB**, **Joggen** (per Datei) und **Calisthenics** (von Hand)
- **GPX- und FIT-Dateien** hochladen (Garmin, Wahoo, Strava-Export, auch `.gz`). Berechnet werden
  Distanz, Bewegungszeit, Tempo bzw. Geschwindigkeit, Anstieg, Abfahrt und, wenn vorhanden, der Puls.
- Aktivitätsseite mit Karte (normal oder Topo) und einem Verlaufsdiagramm für **Höhe, Tempo und Puls**,
  das mit der Karte verbunden ist
- Beim Joggen: **Kilometer-Splits** und **schnellste Abschnitte** (1 km, 5 km, 10 km, Halbmarathon, Marathon)
- Calisthenics: Übungen mit Sätzen, Wiederholungen oder Haltezeit und Zusatzgewicht. Jede Übung zeigt
  den Vergleich zum letzten Mal, den Verlauf und markiert Bestwerte. Ein früheres Workout dient als Vorlage.
- Startseite mit Wochenübersicht, Trainingszeit der letzten 12 Wochen, **Jahreskalender** und **Bestwerten**

**Fediverse**
- Man kann dir von Mastodon, GoToSocial usw. folgen (`@du@deine-domain`). Jede öffentliche Aktivität
  erscheint dort als Beitrag mit Routenbild, Werten und Hashtag.
- Likes, Boosts und Antworten erscheinen unter der Aktivität
- **Bearbeiten und Löschen** kommen bei den Followern an, ebenso Änderungen an Name, Text und Profilbild
- Zustellungen laufen über eine **Warteschlange**: Ist ein Server nicht erreichbar, wird es über gut
  drei Tage hinweg erneut versucht

**Privatsphäre und Sicherheit**
- **Keine Registrierung**: Es gibt nur ein Passwort für dich. Nach 5 Fehlversuchen ist die Anmeldung
  15 Minuten gesperrt.
- **Privatzone**: Rund um Start und Ziel wird die Strecke nicht gezeigt (Standard 200 m). Die
  Originaldatei kannst nur du herunterladen.
- **Private Aktivitäten**: nur für dich sichtbar, gehen nicht ins Fediverse
- Eingehende Nachrichten müssen signiert sein. Fremde Server können die Instanz nicht auf interne
  Adressen schicken. Fremdes HTML wird nur als Text angezeigt. Die Seiten setzen eine strenge
  Content-Security-Policy.
- Schriften, Icons und Kartensoftware liegen im Projekt. Von fremden Servern kommen nur die Kartenkacheln.

## Lokal ausprobieren

Du brauchst Python 3.11 oder neuer.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[dev]"
WEGPUNKT_PASSWORD=test .venv/bin/python -m wegpunkt
```

Dann <http://localhost:8000> öffnen, mit dem Passwort anmelden und eine GPX- oder FIT-Datei
hochladen (z. B. `tests/data/run.gpx`) oder ein Workout eintragen.

Tests: `.venv/bin/python -m pytest`

Föderation funktioniert lokal nicht, weil andere Server deine Instanz erreichen müssen. Zum
Ausprobieren reicht ein Tunnel, z. B. `cloudflared tunnel --url http://localhost:8000`. Die
angezeigte Domain trägst du dann als `WEGPUNKT_DOMAIN` ein.

## Auf einem Server betreiben

Du brauchst eine eigene (Sub-)Domain mit HTTPS, z. B. `sport.example.de`.

1. Projekt auf den Server kopieren und wie oben installieren.
2. `.env.example` nach `.env` kopieren und ausfüllen. Die Datei wird beim Start automatisch gelesen.
   **Domain und Benutzername später nicht mehr ändern**, sonst finden dich deine Follower nicht mehr.
3. Starten, z. B. mit systemd:

   ```ini
   [Service]
   WorkingDirectory=/opt/wegpunkt
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

Alle Daten liegen im Ordner `data/`: Datenbank, Originaldateien, Bilder und der private Schlüssel
deines Accounts. **Diesen Ordner sichern.** Geht der Schlüssel verloren, können andere Server deine
Beiträge nicht mehr zuordnen.

Nach einem Update werden ältere Datenbanken beim Start automatisch umgestellt und vorhandene
Aufzeichnungen neu ausgewertet.

## Aufbau

| Datei | Inhalt |
| --- | --- |
| `wegpunkt/app.py` | Baut die Anwendung zusammen: Einstellungen, Sicherheits-Header, Fehlerseiten |
| `wegpunkt/web.py` | Seiten: Übersicht, Aktivität, Eintragen, Bearbeiten, Profil |
| `wegpunkt/ap.py` | ActivityPub-Endpunkte: WebFinger, Actor, Outbox, Inbox, NodeInfo |
| `wegpunkt/federation.py` | ActivityPub-Objekte, fremde Accounts laden, Warteschlange, Inbox verarbeiten |
| `wegpunkt/httpsig.py` | HTTP-Signaturen erstellen und prüfen |
| `wegpunkt/tracks.py` | GPX und FIT einlesen und auswerten, Privatzone |
| `wegpunkt/workouts.py` | Calisthenics-Workouts: Sätze einlesen, Summen |
| `wegpunkt/stats.py` | Woche, Diagramm, Jahreskalender, Bestwerte, Fortschritt je Übung |
| `wegpunkt/render.py` | Routenbild, Vorschaupfade, Profilbild |
| `wegpunkt/sports.py`, `wegpunkt/fmt.py` | Sportarten und Zahlenformate |
| `wegpunkt/db.py` | SQLite-Datenbank samt Umstellung älterer Stände |
| `wegpunkt/config.py` | Einstellungen aus Umgebung oder `.env` |
| `wegpunkt/static/` | Stylesheet, Skripte, Schriften, Leaflet |

## Ideen für später

- Anderen Accounts im Fediverse folgen und deren Aktivitäten in einem Feed sehen
- Auf Antworten direkt von hier aus antworten
- Routenbild mit Kartenhintergrund statt einfarbig
- Strukturierte Felder (Distanz, Dauer, Sportart) im ActivityPub-Objekt, damit andere
  Fitness-Instanzen Aktivitäten maschinenlesbar übernehmen können
- Segmente und Vergleich wiederholter Strecken

Leaflet liegt in `wegpunkt/static/leaflet/` (BSD-2-Clause), die Schriften Inter und Barlow
Condensed in `wegpunkt/static/fonts/` (SIL Open Font License). Die Lizenzen liegen jeweils daneben.
