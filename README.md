# Ticket-Wächter – WRedSocks'02 Kartenkontingent

Prüft alle 10 Minuten die Tabelle **Verfügbares Kartenkontingent** auf
<https://www.wredsocks.de/hiv/uebersicht_spiele.php> und schickt dir eine Push-Nachricht aufs Handy, wenn

- ein **neues Spiel** freigeschaltet wird, oder
- bei einem Spiel in **Kat2, Kat3, Kat4 oder Kat5** die Kartenanzahl **steigt**.

Verkäufe (Zahl sinkt) und Änderungen bei „nur Bus“ lösen keine Nachricht aus.
Jede Meldung hat einen **Bestellen**-Knopf, der direkt die Bestellseite des Spiels öffnet.
Läuft kostenlos über GitHub Actions, dein Rechner muss nicht an sein.

---

## Einrichtung (ca. 10 Minuten)

### 1. ntfy-App einrichten (am Handy)

1. App **ntfy** installieren ([iPhone](https://apps.apple.com/app/ntfy/id1625396347) / [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)). Kein Konto nötig.
2. In der App auf **+** tippen und einen **geheimen Kanalnamen** abonnieren, z. B. `wredsocks-` gefolgt von
   zufälligen Zeichen. Den Namen kann jeder abonnieren, der ihn kennt, also nichts Erratbares wie `bayern` nehmen.
3. Benachrichtigungen für die App erlauben.

### 2. GitHub-Repository anlegen

1. Auf <https://github.com/new> ein Repository anlegen, z. B. `ticket-waechter`.
   **Public** wählen: Dann sind die Actions-Minuten unbegrenzt kostenlos.
   (Der Kanalname bleibt trotzdem geheim, siehe Schritt 3.
   Bei *Private* reichen die Gratisminuten nur für ein 30-Minuten-Intervall: dann in der Workflow-Datei `*/10` durch `*/30` ersetzen.)
2. Auf der leeren Repo-Seite auf **„uploading an existing file“** klicken und
   `check_tickets.py` und `README.md` hochladen → **Commit changes**.
3. **Add file → Create new file**. Als Dateinamen exakt eintippen:
   `.github/workflows/ticket-waechter.yml`
   und den Inhalt aus dem Abschnitt [Workflow-Datei](#workflow-datei) unten einfügen → **Commit changes**.
   (Der Ordner `.github` ist im ZIP enthalten, wird auf dem Mac aber versteckt, darum der Weg über „Create new file“.)

> Repo schon angelegt (alte WhatsApp-Version)? Dann einfach `check_tickets.py` neu hochladen und den
> Inhalt der Workflow-Datei komplett ersetzen.

### 3. Kanalnamen als Secret hinterlegen

Im Repo: **Settings → Secrets and variables → Actions → New repository secret**

| Name | Wert |
|---|---|
| `NTFY_TOPIC` | dein Kanalname aus Schritt 1, z. B. `wredsocks-uv72…` |

### 4. Testen

**Actions** → links **Ticket-Wächter** → **Run workflow** → (Haken „Testnachricht“ lassen) → **Run workflow**.

Nach ca. 1 Minute kommt eine Push-Nachricht mit der aktuellen Übersicht. Damit ist auch der
Ausgangsstand gespeichert (Datei `state.json` erscheint im Repo). Ab jetzt läuft alles automatisch.

---

## Später zusätzlich WhatsApp (optional)

Sobald CallMeBot wieder neue Nutzer annimmt (<https://www.callmebot.com/blog/free-api-whatsapp-messages/>):
API-Key holen und zwei weitere Secrets anlegen. Danach kommt jede Meldung per ntfy **und** WhatsApp.

| Name | Wert |
|---|---|
| `CALLMEBOT_PHONE` | deine WhatsApp-Nummer mit Ländervorwahl, z. B. `+4917612345678` |
| `CALLMEBOT_APIKEY` | der API-Key vom CallMeBot |

Fällt ein Kanal aus, kommt die Meldung trotzdem über den anderen, und GitHub schickt dir eine Fehler-Mail.

---

## Gut zu wissen

- **Verzögerung:** GitHub startet geplante Läufe oft 5–15 Minuten später als angegeben. Für „Karten sind wieder da“ reicht das meist, sekundengenau ist es nicht.
- **Fehler:** Wenn ein Lauf scheitert (Seite down, Tabelle umgebaut), schickt GitHub dir eine E-Mail. Den Log siehst du unter **Actions**.
- **Sommerpause:** GitHub pausiert Zeitpläne in öffentlichen Repos nach 60 Tagen ohne Aktivität. Der Workflow verhindert das selbst (wöchentlicher Keepalive-Schritt).
- **Pausieren:** Actions → Ticket-Wächter → „…“ → *Disable workflow*.

## Anpassen

- **Welche Kategorien eine Nachricht auslösen:** in `check_tickets.py` die Liste `ALERT_ON_INCREASE`
  (z. B. `"nur Bus"` ergänzen oder `"Sitz Kat2"` entfernen).
- **Intervall:** in der Workflow-Datei die Zeile `cron: "*/10 * * * *"` (`*/5` ist das Minimum bei GitHub).
- **Eigener ntfy-Server / geschützter Kanal:** Umgebungsvariablen `NTFY_SERVER` bzw. Secret `NTFY_TOKEN`.

## Lokal ausprobieren (optional)

```bash
python3 check_tickets.py --dry-run   # zeigt die aktuelle Tabelle und die Nachricht, sendet nichts
```

## Workflow-Datei

Inhalt für `.github/workflows/ticket-waechter.yml`:

```yaml
name: Ticket-Wächter

on:
  schedule:
    - cron: "*/10 * * * *"   # alle 10 Minuten (GitHub startet oft ein paar Minuten später)
    - cron: "17 4 * * 1"     # montags: hält den Zeitplan aktiv (siehe Schritt "Keepalive")
  workflow_dispatch:
    inputs:
      testnachricht:
        description: "Aktuelle Übersicht als Testnachricht schicken"
        type: boolean
        default: true

permissions:
  contents: write   # state.json zurückschreiben
  actions: write    # Keepalive

concurrency:
  group: ticket-waechter
  cancel-in-progress: false

jobs:
  check:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@v5

      - name: Kartenkontingent prüfen
        env:
          NTFY_TOPIC: ${{ secrets.NTFY_TOPIC }}
          NTFY_TOKEN: ${{ secrets.NTFY_TOKEN }}             # optional
          CALLMEBOT_PHONE: ${{ secrets.CALLMEBOT_PHONE }}   # optional (WhatsApp)
          CALLMEBOT_APIKEY: ${{ secrets.CALLMEBOT_APIKEY }} # optional (WhatsApp)
          TESTNACHRICHT: ${{ github.event_name == 'workflow_dispatch' && inputs.testnachricht }}
        run: |
          if [ "$TESTNACHRICHT" = "true" ]; then
            python3 check_tickets.py --test
          else
            python3 check_tickets.py
          fi

      - name: Stand speichern
        if: always()   # auch speichern, wenn nur ein Kanal gescheitert ist
        run: |
          if [ -n "$(git status --porcelain -- state.json)" ]; then
            git config user.name "github-actions[bot]"
            git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
            git add state.json
            git commit -m "Kartenstand aktualisiert"
            git pull --rebase --quiet
            git push
          else
            echo "Keine Änderung am Stand."
          fi

      # GitHub pausiert Zeitpläne in öffentlichen Repos nach 60 Tagen ohne
      # Aktivität (z. B. in der Sommerpause). Das hier verhindert das.
      - name: Keepalive
        if: github.event.schedule == '17 4 * * 1'
        env:
          GH_TOKEN: ${{ github.token }}
        run: gh api -X PUT "repos/${{ github.repository }}/actions/workflows/ticket-waechter.yml/enable"
```
