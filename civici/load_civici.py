"""
Carica il file ANNCSU 'anncsu-indirizzi.parquet' (GeoParquet, punti EPSG:4326)
in un database Spatialite, usando GDAL/OGR.

Da eseguire con il Python di OSGeo4W/QGIS (serve il modulo osgeo), ad esempio
dalla "OSGeo4W Shell":

    python load_civici.py                                  # tutta Italia (~20,7 mln righe)
    python load_civici.py --comune Codogno                 # solo un comune
    python load_civici.py --comune Codogno --comune Maleo --srid 32632
    python load_civici.py --where "CODICE_ISTAT LIKE '098%'"

Se la tabella esiste gia' viene ricreata da zero; le altre tabelle del DB
restano intatte. In una tabella di servizio (META_TABLE) viene registrato
quale parquet e' stato caricato, quando, con quale filtro e in quale SRID.

Campi disponibili nel parquet (utili per --where):
    CODICE_COMUNE, CODICE_ISTAT, NOME_COMUNE, PROGRESSIVO_NAZIONALE,
    CODICE_COMUNALE, ODONIMO, DIZIONE_LINGUA1, DIZIONE_LINGUA2,
    PROGRESSIVO_ACCESSO, CODICE_COMUNALE_ACCESSO, CIVICO, ESPONENTE,
    SPECIFICITA, METRICO, PROGRESSIVO_SNC, longitude, latitude, QUOTA,
    METODO, oob_distance_m, out_of_bounds
"""

import argparse
import os
import sys
import time
from datetime import datetime

# GDAL non e' nel Python "normale" di Windows: c'e' solo in quello di
# OSGeo4W/QGIS. Se l'import fallisce, lo script si ferma con un messaggio chiaro.
try:
    from osgeo import gdal
except ImportError:
    sys.exit("Modulo 'osgeo' non trovato: esegui lo script dalla OSGeo4W Shell "
             "o con il Python di QGIS.")

# Con le eccezioni attive gli errori di GDAL diventano RuntimeError Python
# (con il messaggio dell'errore) invece di restituire None in silenzio.
gdal.UseExceptions()


# ---------------------------------------------------------------------------
# IMPOSTAZIONI DI DEFAULT
# Valori usati quando non si passano --parquet / --db / --table da riga di
# comando. Per cambiarli in modo permanente basta modificarli qui.
# I percorsi sono relativi alla cartella che contiene questo script.
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PARQUET = os.path.join(BASE_DIR, "dati", "anncsu-indirizzi.parquet")
DEFAULT_DB = os.path.join(BASE_DIR, "dati", "db_civici_anncsu.sqlite")
DEFAULT_TABLE = "tbl_anncsu_civici"   # ATTENZIONE: se esiste viene sostituita

# Campi su cui creare un indice (normale, non spaziale) dopo il caricamento.
# Velocizzano le query tipo  WHERE CODICE_ISTAT = '098019'.
# Aggiungere/togliere nomi di campo a piacere (es. "ODONIMO").
INDEX_FIELDS = ("CODICE_ISTAT", "NOME_COMUNE")

# Tabella di servizio (senza geometria) con le informazioni sui caricamenti:
# una riga per ogni tabella di civici presente nel DB.
META_TABLE = "anncsu_info_caricamento"

# Ogni quante righe fare un commit. Valori alti = caricamento piu' veloce
# (ma piu' RAM usata). 100000 e' un buon compromesso.
COMMIT_EVERY = 100000


def parse_args():
    """Legge le opzioni da riga di comando (python load_civici.py --help)."""
    p = argparse.ArgumentParser(description="Carica ANNCSU (GeoParquet) in Spatialite")
    p.add_argument("--parquet", default=DEFAULT_PARQUET, help="file .parquet di input")
    p.add_argument("--db", default=DEFAULT_DB, help="database Spatialite di destinazione")
    p.add_argument("--table", default=DEFAULT_TABLE, help="nome della tabella da creare")
    # action="append": l'opzione si puo' ripetere e i valori finiscono in una lista
    p.add_argument("--comune", action="append", default=[],
                   help="filtra per NOME_COMUNE (ripetibile)")
    p.add_argument("--where", help="filtro SQL libero sugli attributi del parquet")
    p.add_argument("--srid", type=int,
                   help="riproietta nell'EPSG indicato (es. 32632, 7791); default: 4326")
    return p.parse_args()


def sql_val(v):
    """Valore Python -> letterale SQL (stringhe tra apici, None -> NULL)."""
    if v is None:
        return "NULL"
    if isinstance(v, int):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def scrivi_info(ds, args, where, n):
    """
    Registra nella tabella META_TABLE le informazioni sul caricamento.
    La riga della tabella appena caricata viene sostituita (una riga per tabella).

    data_parquet = data di modifica del file parquet. Se il file e' stato
    scaricato con scarica_anncsu.py corrisponde alla data della versione online.
    """
    data_parquet = datetime.fromtimestamp(os.path.getmtime(args.parquet))
    ds.ExecuteSQL(
        f'CREATE TABLE IF NOT EXISTS "{META_TABLE}" ('
        "tabella TEXT PRIMARY KEY, "      # nome della tabella dei civici
        "file_parquet TEXT, "             # percorso del parquet caricato
        "data_parquet TEXT, "             # versione del parquet
        "data_caricamento TEXT, "         # quando e' stato eseguito lo script
        "filtro TEXT, "                   # filtro applicato (NULL = tutta Italia)
        "srid INTEGER, "                  # sistema di riferimento della tabella
        "n_record INTEGER)")              # numero di civici caricati
    valori = [args.table,
              os.path.abspath(args.parquet),
              data_parquet.strftime("%Y-%m-%d %H:%M"),
              datetime.now().strftime("%Y-%m-%d %H:%M"),
              where,
              args.srid or 4326,
              n]
    ds.ExecuteSQL(f'INSERT OR REPLACE INTO "{META_TABLE}" VALUES ('
                  + ", ".join(sql_val(v) for v in valori) + ")")


def build_where(args):
    """
    Costruisce il filtro sugli attributi a partire da --comune e --where.
    Restituisce None se non c'e' nessun filtro (= carica tutto).

    Il filtro e' in dialetto "OGR SQL" (non SQLite): funzioni come UPPER()
    non esistono, ma il LIKE non distingue maiuscole/minuscole, quindi
    'codogno', 'Codogno' e 'CODOGNO' vanno bene tutti.
    """
    clauses = []
    if args.comune:
        # Piu' comuni -> NOME_COMUNE LIKE 'A' OR NOME_COMUNE LIKE 'B' ...
        # replace("'", "''") serve per i nomi con apostrofo (es. Sant'Angelo).
        nomi = " OR ".join("NOME_COMUNE LIKE '" + c.replace("'", "''") + "'"
                           for c in args.comune)
        clauses.append(f"({nomi})")
    if args.where:
        clauses.append(f"({args.where})")
    # --comune e --where insieme vengono combinati in AND
    return " AND ".join(clauses) or None


def progress(pct, _msg, _data, _state={"last": -1}):
    """
    Callback chiamata da GDAL durante la copia (pct va da 0.0 a 1.0).
    Stampa la percentuale a passi del 5% sulla stessa riga.
    Deve restituire 1 per continuare (0 interromperebbe il caricamento).
    """
    step = int(pct * 100) // 5 * 5
    if step != _state["last"]:
        _state["last"] = step
        print(f"\r  avanzamento: {step:3d}%", end="", flush=True)
    return 1


def main():
    args = parse_args()

    # --- Controlli preliminari ---------------------------------------------
    if not os.path.isfile(args.parquet):
        sys.exit(f"File parquet non trovato: {args.parquet}")
    if gdal.GetDriverByName("Parquet") is None:
        sys.exit("Questa installazione di GDAL non ha il driver Parquet.")

    db_exists = os.path.isfile(args.db)
    where = build_where(args)

    print(f"Input : {args.parquet}")
    print(f"Output: {args.db}  ->  tabella '{args.table}'")
    if where:
        print(f"Filtro: {where}")

    # --- Opzioni di conversione (equivalenti ai parametri di ogr2ogr) -------
    options = dict(
        format="SQLite",                 # driver di output (Spatialite = SQLite + SPATIALITE=YES)
        layerName=args.table,            # nome della tabella di destinazione (-nln)
        geometryType="POINT",            # tipo geometria (-nlt)
        # SRID di output (-t_srs): se diverso da 4326 GDAL riproietta al volo
        dstSRS=f"EPSG:{args.srid}" if args.srid else "EPSG:4326",
        where=where,                     # filtro attributi (-where); None = tutto
        layerCreationOptions=[           # opzioni della tabella (-lco)
            "SPATIAL_INDEX=YES",         # crea l'indice spaziale (R-tree)
            "GEOMETRY_NAME=geom",        # nome della colonna geometrica
            "FID=pk_uid",                # nome della chiave primaria
        ],
        # Opzioni del database (-dsco): servono solo se il file va creato.
        # SPATIALITE=YES crea un DB Spatialite invece di un SQLite "semplice".
        datasetCreationOptions=None if db_exists else ["SPATIALITE=YES"],
        # Se il DB esiste gia' si usa "overwrite" (-overwrite): ricrea SOLO la
        # tabella indicata, le altre tabelle del DB restano intatte.
        # Se il DB non esiste (None) viene creato.
        accessMode="overwrite" if db_exists else None,
        # Con un filtro GDAL non conosce il totale e disattiva comunque la barra
        callback=None if where else progress,
    )

    # --- Ottimizzazioni per la velocita' di scrittura ----------------------
    # SYNCHRONOUS=OFF: SQLite non forza la scrittura su disco a ogni commit
    # (molto piu' veloce; in caso di crash a meta' basta rilanciare lo script).
    gdal.SetConfigOption("OGR_SQLITE_SYNCHRONOUS", "OFF")
    # Cache di SQLite in MB
    gdal.SetConfigOption("OGR_SQLITE_CACHE", "512")
    # -gt = numero di righe per transazione
    extra = ["-gt", str(COMMIT_EVERY)]

    # --- Caricamento --------------------------------------------------------
    t0 = time.time()
    ds = gdal.VectorTranslate(args.db, args.parquet,
                              options=gdal.VectorTranslateOptions(options=extra, **options))
    print()
    if ds is None:
        sys.exit("Caricamento fallito.")
    n = ds.GetLayerByName(args.table).GetFeatureCount()
    ds = None   # chiude il DB (in GDAL si chiude mettendo la variabile a None)

    # formattazione all'italiana: 20.728.959 invece di 20,728,959
    print(f"Caricati {n:,} record in {time.time() - t0:.0f} s".replace(",", "."))

    # --- Indici sugli attributi --------------------------------------------
    # Riapre il DB in scrittura e crea un indice per ogni campo di INDEX_FIELDS.
    # Nome indice: idx_<tabella>_<campo>.
    ds = gdal.OpenEx(args.db, gdal.OF_VECTOR | gdal.OF_UPDATE)
    for campo in INDEX_FIELDS:
        print(f"Creo indice su {campo}...")
        ds.ExecuteSQL(f'CREATE INDEX IF NOT EXISTS "idx_{args.table}_{campo.lower()}" '
                      f'ON "{args.table}" ("{campo}")')

    # --- Informazioni sul caricamento -------------------------------------
    scrivi_info(ds, args, where, n)
    print(f"Info caricamento salvate nella tabella '{META_TABLE}'")
    ds = None


if __name__ == "__main__":
    main()
