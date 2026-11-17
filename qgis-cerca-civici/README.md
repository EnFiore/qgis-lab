# Geocodifica indirizzi (ANNCSU) v1

Algoritmo di Processing per QGIS (`geocodifica_anncsu_v1.py`) che assegna le coordinate
a una tabella di indirizzi, con via, civico e comune in campi separati, cercandoli nel
database dei numeri civici **ANNCSU** (Archivio Nazionale dei Numeri Civici e delle
Strade Urbane).

Per ogni indirizzo restituisce un punto e un esito (`geo_stato`) con un punteggio di
affidabilità da 0 a 100, così si capisce subito quali risultati sono sicuri e quali
vanno controllati.

---

## Requisiti

- **QGIS 3.x** con Processing attivo (lo script usa solo librerie già incluse in QGIS:
  `sqlite3`, `difflib`, `unicodedata`).
- **Database dei civici ANNCSU** in formato SQLite/Spatialite, creato con
  `load_civici.py`. Deve contenere la tabella `tbl_anncsu_civici` con almeno questi campi:

  | Campo | Contenuto |
  |---|---|
  | `nome_comune` | nome del comune |
  | `codice_istat` | codice ISTAT del comune |
  | `odonimo` | nome della via (es. `VIA ROMA`) |
  | `progressivo_nazionale` | identificativo della via |
  | `civico` | numero civico |
  | `esponente` | esponente (A, B, BIS…) |
  | `longitude`, `latitude` | coordinate WGS84 dell'accesso |
  | `progressivo_accesso` | identificativo dell'accesso |

  Per velocizzare la lettura conviene un indice su `nome_comune`.

Il percorso proposto di default è `<cartella del progetto QGIS>/dati/db_civici_anncsu.sqlite`.

---

## Installazione

1. In QGIS apri **Strumenti di Processing**.
2. Clicca l'icona **Python**, poi **Aggiungi script agli strumenti…** e seleziona
   `geocodifica_anncsu_v1.py`.
   In alternativa apri il file nell'**Editor di script di Processing** e premi **Esegui**.
3. L'algoritmo compare in **Script → ANNCSU → Geocodifica indirizzi (ANNCSU) v1**.

---

## Parametri

| Parametro | Obbligatorio | Default | Descrizione |
|---|---|---|---|
| **Tabella/layer degli indirizzi** | sì | – | Layer o tabella (anche senza geometria: CSV, Excel, ecc.) che contiene gli indirizzi. |
| **Campo via** | sì | – | Campo con il nome della via. Accetta abbreviazioni (`V.le`, `P.zza`, `C.so`…). Se il campo civico è vuoto, il civico può anche essere scritto qui (`Via Roma 12/A`). |
| **Campo civico** | no | – | Campo con il numero civico, eventualmente con l'esponente (`12`, `12/A`, `12 bis`). `SNC`, `0` o vuoto equivalgono a "senza numero". |
| **Campo comune** | sì | – | Campo con il nome del comune. Gli errori di battitura vengono corretti se il nome è simile almeno all'85%. |
| **Campo codice ISTAT comune** | no | – | Codice ISTAT del comune (`15146`, `015146`, `15146.0` vanno tutti bene). Serve a distinguere i comuni omonimi in province diverse. |
| **Database civici ANNCSU (.sqlite)** | sì | `<progetto>/dati/db_civici_anncsu.sqlite` | Il database creato con `load_civici.py`. Viene aperto in sola lettura. |
| **Somiglianza minima per nomi con refusi (0–1)** | sì | `0.80` | Soglia del confronto approssimato sui nomi delle vie. Con un valore più alto si trovano meno vie ma con meno errori, con uno più basso il contrario. Valori ammessi: da 0.5 a 1.0. |
| **Massima differenza di numero civico accettata** | sì | `10` | Fino a quanti numeri di distanza si accetta un civico vicino (CIVICO_VICINO) o si interpola tra due civici (INTERPOLATO, se i civici che racchiudono quello cercato distano al massimo il doppio). |
| **Spostamento dei punti … dal civico usato (metri)** | sì | `5` | Per CIVICO_VICINO, INIZIO_VIA, FINE_VIA e SOLO_VIA il punto viene spostato di questi metri lungo la via, così non si sovrappone al civico reale. `0` = nessuno spostamento. Massimo 100. |
| **Se il civico manca, posiziona al centro della via** | – | ✔ attivo | Se la via esiste ma il civico non si trova, mette il punto al centro della via (SOLO_VIA). |
| **Se la via manca, posiziona al centro del comune** | – | ✘ disattivo | Se la via non si trova, mette il punto al centro del comune (SOLO_COMUNE). |
| **Sistema di riferimento dell'output** | sì | `EPSG:4326` | SR del layer in uscita (es. `EPSG:32632`, `EPSG:3003`). Le coordinate `geo_lon` e `geo_lat` restano comunque in WGS84. |
| **Indirizzi geocodificati** | sì | layer temporaneo | Output principale, di tipo MultiPoint. |
| **Indirizzi non trovati o da rivedere** | no | layer temporaneo | Tabella di controllo senza geometria. Se non ti serve, disattiva la creazione. |

### Impostazioni consigliate

- **Primo passaggio:** lascia i valori di default e indica il **codice ISTAT** se è
  disponibile (elimina il problema dei comuni omonimi).
- **Dati molto sporchi** (tanti refusi): abbassa la somiglianza a `0.70–0.75` e poi
  controlla le righe con nota "via simile a…".
- **Precisione prima di tutto:** alza la somiglianza a `0.90`, metti la tolleranza a
  `2–4` e disattiva i due "posiziona al centro".

---

## Come funziona la ricerca

Per ogni indirizzo:

1. **Comune**: confronta il nome normalizzato (maiuscolo, senza accenti né apostrofi,
   `S.` → `SAN`); se non lo trova, usa un confronto approssimato. Il codice ISTAT,
   se c'è, sceglie tra i comuni omonimi.
2. **Via**, in quest'ordine:
   1. nome completo identico (`VIA ROMA`);
   2. stesso nome senza tipo di strada (`ROMA`, `V.LE ROMA`): vale solo la via dello
      stesso tipo, se esiste;
   3. parole compatibili (`VIA MARCONI` o `VIA G. MARCONI` → `VIA GUGLIELMO MARCONI`);
   4. confronto approssimato (refusi), con la soglia impostata.

   I numeri nei nomi vengono uniformati: `XX SETTEMBRE` = `VENTI SETTEMBRE` =
   `20 SETTEMBRE`, `VITTORIO EMANUELE II` = `… SECONDO`. `SS 9` diventa
   `STRADA STATALE 9` e `SS. Trinità` diventa `SANTISSIMA TRINITA`.
3. **Civico**, in quest'ordine:
   1. civico ed esponente identici;
   2. stesso civico con esponente diverso o mancante;
   3. interpolazione tra i civici vicini dello stesso lato (pari/dispari);
   4. civico più vicino sulla stessa via, entro la tolleranza;
   5. civico oltre l'ultimo della via → fine via; prima del primo → inizio via;
   6. centro della via (se l'opzione è attiva).

Il "centro" di una via o di un comune è il **civico reale più vicino al baricentro**:
così il punto cade sempre su un accesso esistente.

---

## Campi in uscita

Ogni riga riporta i campi originali più i seguenti. Se un nome esiste già nella tabella
di input, gli viene aggiunto il prefisso `_`.

| Campo | Descrizione |
|---|---|
| `geo_stato` | Esito della ricerca (vedi tabella sotto). |
| `geo_punti` | Affidabilità da 0 a 100. |
| `geo_via` | Nome della via trovata in ANNCSU. |
| `geo_civico`, `geo_esp` | Civico ed esponente del civico usato. |
| `geo_comune`, `geo_istat` | Comune trovato e suo codice ISTAT (6 cifre). |
| `geo_id_via` | `progressivo_nazionale` della via. |
| `geo_id_acc` | `progressivo_accesso` del civico (vuoto se il punto è interpolato). |
| `geo_lon`, `geo_lat` | Coordinate WGS84 del punto. |
| `geo_rivedere` | `SI` se il risultato va controllato a mano. |
| `geo_note` | Spiegazione dell'esito (alternative scartate, tipo di strada diverso, spostamento…). |

### Valori di `geo_stato`

| Stato | Punti | Significato |
|---|---|---|
| `OK` | 100 | Civico ed esponente trovati esattamente. |
| `ESPONENTE_DIVERSO` | 90 | Civico trovato, esponente diverso o mancante. |
| `INTERPOLATO` | 75 | Punto calcolato tra due civici dello stesso lato. |
| `CIVICO_VICINO` | 60 | Usato il civico più vicino, entro la tolleranza. |
| `INIZIO_VIA` / `FINE_VIA` | 50 | Civico fuori dalla numerazione della via: punto all'inizio o alla fine della via. |
| `SOLO_VIA` | 40 | Via trovata, civico no: punto al centro della via. |
| `SOLO_COMUNE` | 10 | Via non trovata: punto al centro del comune. |
| `NON_TROVATO` | 0 | Nessun punto: la riga viene scritta senza geometria. |

**Penalità:** −5 punti se la via è stata trovata "per parole", −15 se trovata con il
confronto approssimato.

**Da rivedere** (`geo_rivedere = SI`, punteggio massimo 50):
- coincide solo il nome della via, con tipo di strada assente o diverso
  (es. `Cavour` o `Corso Cavour` quando esistono solo `VIA CAVOUR` e `PIAZZA CAVOUR`);
- più vie dello stesso tipo sono compatibili (es. `VIA G. MARCONI` / `VIA M. MARCONI`);
- la stessa via esiste in più comuni omonimi e manca il codice ISTAT.

Nessuna riga va persa: le righe non trovate, quelle SOLO_COMUNE e quelle da rivedere
finiscono anche nella tabella **Indirizzi non trovati o da rivedere**.

---

## Riepilogo nel registro

A fine elaborazione il registro di Processing mostra quante righe hanno avuto ciascun
esito, con la percentuale. La voce `DA_RIVEDERE` è un conteggio già incluso negli stati
sopra, non un esito a parte.

---

## Problemi frequenti

| Problema | Causa / soluzione |
|---|---|
| "Impossibile aprire il database" | Percorso del `.sqlite` sbagliato, oppure il file è aperto in scrittura da un altro programma. |
| Tutte le righe `NON_TROVATO` con nota "comune non presente nel DB" | Il database non contiene quei comuni. |
| Molte righe `SOLO_VIA` | Civici mancanti in ANNCSU oppure tolleranza troppo bassa. |
| Punti nel comune sbagliato | Comuni omonimi: aggiungi il campo del codice ISTAT. |
| `geo_note` troncato | Il campo è limitato a 254 caratteri. |
