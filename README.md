# Fantasy Football Tools

Applicazione Flask locale per preparare e gestire l'asta del fantacalcio. Include un Bid Manager con controllo dei crediti, ruoli progressivi, storico modificabile e backup JSON, oltre a un Notes Manager per organizzare la strategia sui giocatori prima dell'asta.

## Avvio con Docker Compose

La configurazione completa comprende Flask e MariaDB. È il modo consigliato
per usare l'app, perché abilita anche gli account e la persistenza dei dati.

```bash
cp .env.example .env
# modifica le password di esempio e FLASK_SECRET_KEY
docker compose up --build -d
```

L'app è disponibile su `http://127.0.0.1:5010`, oppure sulla porta impostata
con `APP_PORT`. Il processo nel container usa Gunicorn sulla porta interna
`5010`; `APP_PORT` modifica solo la porta pubblicata su `127.0.0.1` dell'host.

Imposta in `.env` un valore lungo e casuale per `FLASK_SECRET_KEY`, password
MariaDB diverse per `DB_PASSWORD` e `MARIADB_ROOT_PASSWORD`, e il vero URL
pubblico in `APP_BASE_URL`.

## Pubblicazione in produzione

Il container applicativo usa Gunicorn, gira come utente non root, ha filesystem
di sola lettura (tranne `persistent_data/`) ed espone solo il loopback. Pubblica
quindi il servizio dietro un reverse proxy HTTPS gestito (Caddy, Nginx o
equivalente), inoltrando le richieste a `127.0.0.1:${APP_PORT:-5010}`.

Prima della pubblicazione:

- imposta `APP_ENV=production`, `APP_BASE_URL=https://tuo-dominio`,
  `TRUST_PROXY_HEADERS=1` e `ALLOWED_HOSTS=tuo-dominio`;
- usa HTTPS end-to-end fino al browser: con un `APP_BASE_URL` HTTPS, cookie
  Secure e HSTS si attivano automaticamente;
- genera segreti e password unici, non usare mai i valori d'esempio;
- mantieni un backup cifrato e verificato di `persistent_data/`, che include
  MariaDB e i documenti delle aste;
- monitora il percorso `/healthz` dal reverse proxy o dalla piattaforma;
- imposta `LEGAL_DATA_CONTROLLER_NAME`, `LEGAL_CONTACT_EMAIL` e
  `LEGAL_BUSINESS_ADDRESS`, poi sottoponi l'informativa al tuo consulente
  legale prima della messa online.

Non attivare `TRUST_PROXY_HEADERS` se l'app è raggiungibile direttamente da
client non fidati: i relativi header devono essere sovrascritti dal proxy.

Per inviare le email con Gmail, lascia `MAIL_USERNAME` e `MAIL_FROM` su
`wedev.danielebertagnoli@gmail.com` e inserisci in `MAIL_PASSWORD` una
Password per le app di Google, non la password normale dell'account. Il
servizio MariaDB non espone porte verso l'host: l'app lo raggiunge internamente
tramite `DB_HOST=db`.

La cartella gitignorata `persistent_data/` conserva `mariadb/`, il catalogo
`player_catalogue.json` e i JSON dell'app; non eliminarla se vuoi mantenere i
dati tra gli aggiornamenti dei container.

Compose esegue prima `storage-init`, che assegna la cartella dei dati e le
sottocartelle di aste e note all'utente applicativo (UID/GID 1000). Questo
permette al processo non root di salvare i JSON anche quando Docker ha creato
il bind mount come root. I permessi della sottocartella MariaDB non vengono
modificati.

## Account e profilo

La registrazione richiede nome, cognome, nome utente, email e password.
Il nome utente contiene da 3 a 32 caratteri (lettere senza accenti, numeri,
punti e underscore), inizia con una lettera o un numero ed è univoco senza
distinguere maiuscole e minuscole. La disponibilità viene verificata nel
modulo e nuovamente dal database al salvataggio. Il login accetta sia email
sia nome utente, dopo la conferma dell'email.

Dal nome utente nel banner si apre `/profile`, dove è possibile aggiornare
i dati personali e cambiare password inserendo quella attuale. Il cambio
password invalida le altre sessioni e i precedenti link di recupero.

Al primo accesso al database dopo l'aggiornamento viene eseguita una migrazione
automatica: gli account esistenti conservano ID, email, password e sessioni;
ricevono un nome utente provvisorio `utente_<id>` (con suffisso in caso di
collisione). Nome e cognome restano vuoti finché l'utente completa il profilo.
La migrazione è ripetibile ed è serializzata tra i worker Gunicorn.

I test usano un database MariaDB separato e temporaneo, senza inviare email:

```bash
docker compose -f tests/compose.yml run --build --rm tests
docker compose -f tests/compose.yml down
```

## Sviluppo locale

Per lavorare senza Docker serve una MariaDB già raggiungibile e un `.env`
con `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` e le impostazioni
email appropriate al tuo ambiente. Carica le variabili e avvia Flask dalla
cartella `src`:

```bash
uv sync
set -a && . ./.env && set +a
uv run --directory src flask --app main run --debug --port 5010
```

Apri quindi `http://127.0.0.1:5010` nel browser.

## Account e accesso

La dashboard, i manager e le API richiedono un account con email confermata.
La registrazione salva in MariaDB una password hashata e lo stato
`email_confirmed`; finché il link ricevuto via email non viene aperto, non
viene creata alcuna sessione di accesso. Sono inclusi reinvio della conferma,
recupero password e link di reset monouso.

Le password devono avere almeno 8 caratteri, maiuscola, minuscola, numero e
simbolo. La barra nella pagina di registrazione fornisce un riscontro immediato,
ma le stesse regole sono sempre verificate dal server.

## Bid Manager

1. Seleziona **Nuova asta**, imposta partecipanti, crediti e slot per ruolo.
2. Durante l'asta inserisci giocatore, prezzo e acquirente: il ruolo viene assegnato automaticamente nell'ordine P, D, C, A.
3. Seleziona un partecipante per vedere la rosa; usa lo storico per correggere prezzo o acquirente, oppure eliminare una battuta.
4. Esporta il JSON per riprendere l'asta in seguito con **Importa asta**.

Il link **Condividi** permette di consultare un'asta in sola lettura senza
registrarsi o accedere. Per le aste in modalità live, anche in pausa, serve
invece un account confermato. Se l'organizzatore attiva la modalità live,
i visitatori anonimi vengono invitati ad accedere al successivo aggiornamento.

L'asta viene conservata sul server per un massimo di **72 ore dalla creazione**.
Esporta il JSON prima della scadenza per conservarla o riprenderla su un altro dispositivo.

## Notes Manager

1. Crea un nuovo taccuino e indica le fasce che vuoi usare per valutare i giocatori.
2. Aggiungi nome, ruolo, fascia, percentuale ideale di crediti e note personali per ogni giocatore.
3. Correggi o elimina una scheda in qualsiasi momento; l'archivio resta organizzato per ruolo e fascia.
4. Esporta il JSON per riprendere il lavoro in seguito oppure il Markdown per condividere una tabella ordinata con la tua lega.

Anche le note hanno una durata massima di **72 ore dalla creazione**; la chiusura
della sessione delle note può eliminarle prima. Per conservarle usa l'esportazione JSON.

## Pulizia automatica di aste e note

Il servizio Compose `retention-worker` esegue una pulizia all'avvio e ogni minuto.
Elimina ciascun JSON scaduto nelle sole cartelle `persistent_data/bid_manager_auctions/`
e `persistent_data/notes_manager_notes/`, oltre ai file temporanei abbandonati da
almeno 72 ore. Il database degli utenti, il catalogo e i backup non sono coinvolti.

La scadenza usa `created_at` in UTC: modificare un documento non prolunga la
durata, nemmeno per le aste live. Le API rifiutano letture e salvataggi alla
scadenza; il file viene rimosso al successivo passaggio del worker (normalmente
entro un minuto). La regola vale anche per i documenti già presenti. Per file
corrotti o privi di una data valida viene usata l'ultima modifica del file.
Reimportare un JSON crea una nuova copia con altre 72 ore disponibili.

Il worker usa lo stesso utente non root dell'app, riparte automaticamente dopo
un arresto imprevisto e ha un controllo di salute. Per avviarlo insieme all'app:

```bash
docker compose up --build -d
docker compose logs retention-worker
```

In sviluppo senza Docker, esegui `python src/data_retention.py` in un processo
separato. L'opzione `--once` esegue una sola pulizia; `--data-root` permette di
indicare una cartella di dati diversa.

## Ricerca calciatori

Nei campi giocatore del Bid Manager e del Notes Manager, dopo tre caratteri vengono proposti fino a cinque calciatori della Serie A, con nome, squadra e foto. La ricerca non usa né modifica il ruolo: nel Notes Manager resta quello scelto manualmente, mentre nel Bid Manager resta invariata la progressione dell'asta.

Il catalogo è un JSON locale generato da Wikipedia italiana: parte dalla pagina della Serie A della stagione corrente, segue le squadre partecipanti e ne legge le rose. Per i giocatori con una pagina Wikipedia viene salvato anche il link alla miniatura; per i nomi non cliccabili la foto resta volutamente vuota. La ricerca non fa richieste online: legge solo il JSON già generato.

Genera o aggiorna il dataset prima di usare l'autocomplete:

```bash
uv run python src/scrape_players.py --force
```

In alternativa è disponibile lo stesso comando Flask:

```bash
cd src
uv run flask --app main players-sync --force
```

Con Docker Compose puoi eseguire lo stesso aggiornamento così:

```bash
docker compose exec fantasy-football-tools flask --app main:app players-sync --force
```

La stagione viene calcolata automaticamente: da luglio in poi `YYYY-(YYYY+1)`, prima di luglio quella iniziata l'anno precedente. Per esempio, a settembre 2026 viene usata `Serie A 2026-2027`; l'anno prossimo cambierà da sola. Puoi forzare una stagione specifica con `--season-start 2026`.

Il catalogo viene sempre salvato in `persistent_data/player_catalogue.json`, sia in locale sia nel container Docker. Variabili opzionali: `PLAYER_CATALOGUE_SYNC_INTERVAL_HOURS` (predefinito `24`), `PLAYER_CATALOGUE_TIMEOUT_SECONDS` (predefinito `20`) e `PLAYER_CATALOGUE_SEASON_START_MONTH` (predefinito `7`). Per aggiornare automaticamente all'avvio imposta `PLAYER_CATALOGUE_SYNC_ON_STARTUP=1`; per impostazione predefinita resta disattivato, così l'app si avvia subito anche senza rete.

Durante la sincronizzazione vengono mostrati i progressi squadra per squadra. Il JSON viene scritto in un file temporaneo univoco e sostituito atomicamente solo a catalogo completo; un lock sul file impedisce a più processi dell'app di sincronizzare lo stesso dataset contemporaneamente. Le ricerche degli utenti restano sempre in sola lettura e non creano file temporanei.
