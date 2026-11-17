# Civici ANNCSU → Spatialite

Due script per scaricare i civici dell'**ANNCSU** (Archivio Nazionale dei Numeri Civici e delle Strade Urbane) e caricarli in un database **Spatialite** da usare in QGIS.

| Script | A cosa serve |
|---|---|
| `scarica_anncsu.py` | Scarica l'ultima versione del file `anncsu-indirizzi.parquet` nella cartella `dati/` |
| `load_civici.py` | Carica il parquet (tutto o filtrato) in una tabella Spatialite |

## Struttura delle cartelle

```
Load civici ANNCUS/
├── scarica_anncsu.py
├── load_civici.py
└── dati/
    ├── anncsu-indirizzi.parquet                  <- scaricato da scarica_anncsu.py
    ├── db_civici_anncsu.sqlite                   <- creato da load_civici.py
    └── backup/
        └── anncsu-indirizzi_bkp_AAAAMMGG.parquet  <- versione precedente del parquet
```

## Uso rapido

Dalla **OSGeo4W Shell** (installata con QGIS), nella cartella degli script:

```bash
python scarica_anncsu.py      # 1. scarica o aggiorna il parquet
python load_civici.py         # 2. carica tutta Italia nel database
```

Il database si apre in QGIS da **Browser → SpatiaLite → Nuova connessione**, oppure trascinando il file `.sqlite` nella mappa.

---

## scarica_anncsu.py

Scarica il file pubblicato dal progetto [anncsu-open/anncsu-viewer](https://github.com/anncsu-open/anncsu-viewer/tree/main/data/indirizzi):

```
https://pub-1e760dc850cb4a5aa5f8afb77713f8cd.r2.dev/anncsu-indirizzi.parquet
```

Il dominio `r2.dev` è lo spazio di archiviazione di Cloudflare usato dal progetto, non un sito istituzionale.

Usa solo la libreria standard, quindi funziona con qualsiasi Python 3.

```bash
python scarica_anncsu.py            # scarica solo se online c'è una versione più recente
python scarica_anncsu.py --force    # scarica comunque
```

### Funzionamento
1. Confronta dimensione e data del file online con quelle del file locale. Se il file locale è già aggiornato, lo segnala ed esce.
2. Scarica in un file temporaneo `.part`. Il file esistente non viene toccato finché il download non è finito.
3. Verifica che il file scaricato sia completo e sia un Parquet valido.
4. Sposta il vecchio file in `dati/backup/`. **Si tiene un solo backup**: quello precedente viene cancellato.
5. Mette il nuovo file al suo posto.

Se il download si interrompe o la verifica fallisce, il parquet esistente resta com'è.

### Impostazioni
All'inizio dello script si possono cambiare `URL`, `DATA_DIR`, `BACKUP_DIR` e `FILE_NAME`.

---

## load_civici.py

Richiede il **Python di OSGeo4W o di QGIS**, perché usa GDAL (modulo `osgeo`).

### Opzioni

| Opzione | Descrizione |
|---|---|
| `--comune NOME` | Carica solo il comune indicato. Si può ripetere per più comuni |
| `--where "FILTRO"` | Filtro SQL libero sui campi del parquet |
| `--srid EPSG` | Riproietta nell'EPSG indicato (es. `32632`, `7791`). Default: `4326` |
| `--table NOME` | Nome della tabella. Default: `tbl_anncsu_civici` |
| `--db FILE` | Database di destinazione. Default: `dati/db_civici_anncsu.sqlite` |
| `--parquet FILE` | File di input. Default: `dati/anncsu-indirizzi.parquet` |

Senza filtri viene caricata tutta Italia.

### Esempi

```bash
# Un comune
python load_civici.py --comune Codogno

# Più comuni, riproiettati in UTM 32N
python load_civici.py --comune Codogno --comune Maleo --srid 32632

# Una provincia, tramite il codice ISTAT
python load_civici.py --where "CODICE_ISTAT LIKE '098%'"

# Due province in un solo comando
python load_civici.py --where "CODICE_ISTAT LIKE '098%' OR CODICE_ISTAT LIKE '019%'"

# Tabella separata nello stesso database
python load_civici.py --table civici_lodi --comune Lodi
```

### Filtri
- `--comune` non distingue maiuscole e minuscole e gestisce i nomi con apostrofo (es. `Sant'Angelo Lodigiano`). Se si indicano più comuni, vengono caricati tutti.
- `--where` usa il dialetto **OGR SQL**, non SQLite: alcune funzioni come `UPPER()` non sono disponibili.
- Se si usano `--comune` e `--where` insieme, devono valere entrambi i filtri.

Campi utilizzabili nei filtri:

```
CODICE_COMUNE, CODICE_ISTAT, NOME_COMUNE, PROGRESSIVO_NAZIONALE,
CODICE_COMUNALE, ODONIMO, DIZIONE_LINGUA1, DIZIONE_LINGUA2,
PROGRESSIVO_ACCESSO, CODICE_COMUNALE_ACCESSO, CIVICO, ESPONENTE,
SPECIFICITA, METRICO, PROGRESSIVO_SNC, longitude, latitude, QUOTA,
METODO, oob_distance_m, out_of_bounds
```

### Comportamento sul database
- Se il database non esiste, viene creato.
- Se esiste, viene ricreata **solo la tabella indicata**: le altre tabelle del database restano intatte.
- Non si possono accodare dati a una tabella esistente. Per più zone si combina il filtro in un solo comando; per tenerle separate si usa `--table` o `--db`.

### Cosa viene creato
- **Tabella dei civici**: punti nella colonna `geom`, chiave primaria `pk_uid`, indice spaziale e indici su `codice_istat` e `nome_comune`. I nomi dei campi diventano minuscoli.
- **Tabella `anncsu_info_caricamento`** (senza geometria): una riga per ogni tabella caricata, con:
  - il parquet usato e la sua data (`data_parquet`);
  - la data del caricamento;
  - il filtro applicato;
  - lo SRID;
  - il numero di record.

  Si vede in QGIS dal **Gestore DB**.

### Impostazioni
All'inizio dello script, nella sezione **IMPOSTAZIONI DI DEFAULT**:
- i valori di default di parquet, database e tabella;
- i campi da indicizzare (`INDEX_FIELDS`);
- il numero di righe per transazione (`COMMIT_EVERY`);
- il nome della tabella delle informazioni di caricamento (`META_TABLE`).

---

## Aggiornare i dati

1. `python scarica_anncsu.py`
2. `python load_civici.py` (con gli stessi filtri usati la volta precedente): la tabella viene ricreata con i dati nuovi.

La versione caricata si controlla nel campo `data_parquet` della tabella `anncsu_info_caricamento`.

Il file pubblicato può cambiare struttura tra un rilascio e l'altro. Dopo un aggiornamento conviene controllare i campi con:

```
ogrinfo -so dati\anncsu-indirizzi.parquet anncsu-indirizzi
```
