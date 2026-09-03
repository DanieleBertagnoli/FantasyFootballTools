# Fantasy Football Tools

Applicazione Flask locale per preparare e gestire l'asta del fantacalcio. Include un Bid Manager con controllo dei crediti, ruoli progressivi, storico modificabile e backup JSON, oltre a un Notes Manager per organizzare la strategia sui giocatori prima dell’asta.

## Avvio

```bash
uv sync
uv run flask --app main run --debug
```

Apri quindi `http://127.0.0.1:5000` nel browser.

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
