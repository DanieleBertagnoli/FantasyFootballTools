# Fantasy Football Tools

Applicazione Flask locale per preparare e gestire l'asta del fantacalcio. Include un Bid Manager con controllo dei crediti, ruoli progressivi, storico modificabile e backup JSON, oltre a un Notes Manager per organizzare la strategia sui giocatori prima dell’asta.

## Avvio con Docker Compose

La configurazione completa comprende Flask e MariaDB. È il modo consigliato
per usare l'app, perché abilita anche gli account e la persistenza dei dati.

```bash
cp .env.example .env
# modifica le password di esempio e FLASK_SECRET_KEY
docker compose up --build -d
```

L'app è disponibile su `http://127.0.0.1:5010`, oppure sulla porta impostata
con `APP_PORT`. Il processo nel container usa sempre la porta interna `5010`;
`APP_PORT` modifica solo la porta pubblicata sull'host.

Imposta in `.env` un valore lungo e casuale per `FLASK_SECRET_KEY`, password
MariaDB diverse per `DB_PASSWORD` e `MARIADB_ROOT_PASSWORD`, e il vero URL
pubblico in `APP_BASE_URL`. Se l'app viene esposta in HTTPS, abilita anche
`SESSION_COOKIE_SECURE=1`.

Per inviare le email con Gmail, lascia `MAIL_USERNAME` e `MAIL_FROM` su
`wedev.danielebertagnoli@gmail.com` e inserisci in `MAIL_PASSWORD` una
Password per le app di Google, non la password normale dell'account. Il
servizio MariaDB non espone porte verso l'host: l'app lo raggiunge internamente
tramite `DB_HOST=db`.

La cartella gitignorata `persistent_data/` conserva `mariadb/`, il catalogo
`player_catalogue.json` e i JSON dell'app; non eliminarla se vuoi mantenere i
dati tra gli aggiornamenti dei container.

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

L'asta attiva viene conservata localmente per la sessione del browser. Esporta sempre il JSON per un backup portabile e per riprendere l'asta su un altro browser o dispositivo.

## Notes Manager

1. Crea un nuovo taccuino e indica le fasce che vuoi usare per valutare i giocatori.
2. Aggiungi nome, ruolo, fascia, percentuale ideale di crediti e note personali per ogni giocatore.
3. Correggi o elimina una scheda in qualsiasi momento; l'archivio resta organizzato per ruolo e fascia.
4. Esporta il JSON per riprendere il lavoro in seguito oppure il Markdown per condividere una tabella ordinata con la tua lega.

Anche le note attive vengono conservate localmente per la sessione del browser. Per un backup portabile usa l'esportazione JSON.

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
docker compose exec app flask --app main:app players-sync --force
```

La stagione viene calcolata automaticamente: da luglio in poi `YYYY-(YYYY+1)`, prima di luglio quella iniziata l'anno precedente. Per esempio, a settembre 2026 viene usata `Serie A 2026-2027`; l'anno prossimo cambierà da sola. Puoi forzare una stagione specifica con `--season-start 2026`.

Il catalogo viene sempre salvato in `persistent_data/player_catalogue.json`, sia in locale sia nel container Docker. Variabili opzionali: `PLAYER_CATALOGUE_SYNC_INTERVAL_HOURS` (predefinito `24`), `PLAYER_CATALOGUE_TIMEOUT_SECONDS` (predefinito `20`) e `PLAYER_CATALOGUE_SEASON_START_MONTH` (predefinito `7`). Per aggiornare automaticamente all'avvio imposta `PLAYER_CATALOGUE_SYNC_ON_STARTUP=1`; per impostazione predefinita resta disattivato, così l'app si avvia subito anche senza rete.

Durante la sincronizzazione vengono mostrati i progressi squadra per squadra. Il JSON viene scritto in un file temporaneo univoco e sostituito atomicamente solo a catalogo completo; un lock sul file impedisce a più processi dell'app di sincronizzare lo stesso dataset contemporaneamente. Le ricerche degli utenti restano sempre in sola lettura e non creano file temporanei.
