# Arriba auf Uberspace installieren

Diese Anleitung gilt für **Uberspace 7**. Sie richtet Arriba als private Instanz ein: Nur du
kommst nach der Anmeldung hinein, und neue Aktivitäten werden automatisch auf deinem
bestehenden Mastodon-Account gepostet.

In den Befehlen steht `isabell` für deinen Uberspace-Benutzernamen und `stardust` für deinen
Server. Ersetze beides.

## 1. Anmelden

```bash
ssh isabell@stardust.uberspace.de
```

## 2. Arriba holen und installieren

```bash
git clone https://github.com/philpioneer/wegpunkt.git ~/arriba
cd ~/arriba
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e .
```

Am Ende sollte `Successfully installed ...` stehen.

## 3. Eine eigene Adresse anlegen

Damit Arriba andere Seiten auf deinem Uberspace nicht verdrängt, bekommt es eine eigene
Subdomain:

```bash
uberspace web domain add arriba.isabell.uber.space
```

Du kannst stattdessen eine eigene Domain nehmen, z. B. `sport.example.de`. Dann zeigt dir der
Befehl die DNS-Einträge (A und AAAA) an, die du bei deinem Domain-Anbieter eintragen musst.

## 4. Einstellungen

```bash
cd ~/arriba
cp .env.example .env
chmod 600 .env
nano .env
```

Trag dort diese Werte ein (den Rest kannst du löschen oder so lassen):

```ini
ARRIBA_DOMAIN=arriba.isabell.uber.space
ARRIBA_USERNAME=philipp
ARRIBA_PASSWORD=ein-langes-eigenes-passwort
ARRIBA_DISPLAY_NAME=Philipp

# Nur für dich: alle Seiten erst nach Anmeldung, im Fediverse nicht auffindbar
ARRIBA_PRIVATE=1

# Nötig auf Uberspace
ARRIBA_HOST=0.0.0.0
ARRIBA_PORT=8000
ARRIBA_FORWARDED_IPS=*
ARRIBA_DATA_DIR=/home/isabell/arriba-data
```

Hinweise:
- `ARRIBA_HOST=0.0.0.0` ist Pflicht. Mit `127.0.0.1` erreicht der Uberspace-Webserver Arriba nicht.
- Ist Port 8000 schon belegt, nimm eine andere Zahl zwischen 1024 und 65535, hier und in Schritt 6.
- Das Passwort ist der einzige Schutz deiner Instanz. Nimm ein langes, das du sonst nirgends nutzt.
- Speichern in nano: Strg+O, Enter, dann Strg+X.

## 5. Als Dienst starten

```bash
cp ~/arriba/deploy/arriba.ini ~/etc/services.d/arriba.ini
supervisorctl reread
supervisorctl update
supervisorctl status
```

Nach etwa 15 Sekunden sollte bei `arriba` der Zustand `RUNNING` stehen. Der Dienst startet
auch nach einem Neustart des Servers von selbst.

## 6. Mit dem Webserver verbinden

```bash
uberspace web backend set arriba.isabell.uber.space --http --port 8000
uberspace web backend list
```

In der Liste sollte hinter der Adresse `OK, listening` stehen. Das HTTPS-Zertifikat besorgt
Uberspace beim ersten Aufruf automatisch.

## 7. Anmelden und Mastodon verbinden

1. `https://arriba.isabell.uber.space` öffnen und mit deinem Passwort anmelden.
2. Oben rechts auf das Personen-Symbol („Profil“) gehen.
3. In Mastodon: **Einstellungen → Entwicklung → Neue Anwendung**. Name „Arriba“, Berechtigungen
   `read`, `write:statuses` und `write:media` ankreuzen, speichern.
4. Die Anwendung öffnen, „Dein Zugangs-Token“ kopieren und in Arriba unter „Mastodon“ zusammen
   mit der Adresse deiner Instanz eintragen. Arriba prüft das Token sofort.

Ab jetzt hat jedes Formular den Schalter „Auf Mastodon posten“. Er ist standardmäßig an.

## Aktualisieren

```bash
~/arriba/deploy/update.sh
```

## Wenn etwas nicht klappt

```bash
supervisorctl status                 # läuft der Dienst?
supervisorctl tail arriba stderr     # die letzten Meldungen
uberspace web backend list           # ist der Webserver verbunden?
```

## Sichern

Alle Daten liegen in `~/arriba-data`: Datenbank, Originaldateien, Bilder und das
Mastodon-Token. Diesen Ordner solltest du regelmäßig sichern.

## Später öffentlich machen

Wenn andere deine Seite sehen und dir direkt im Fediverse folgen können sollen: in `.env` die
Zeile `ARRIBA_PRIVATE=1` entfernen und `supervisorctl restart arriba` ausführen. Domain und
Benutzername sollten dann nicht mehr geändert werden.
