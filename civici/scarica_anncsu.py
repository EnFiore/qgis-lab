"""
Scarica l'ultima versione del dataset ANNCSU 'anncsu-indirizzi.parquet'
(GeoParquet) nella cartella 'dati' accanto a questo script.

Fonte: https://github.com/anncsu-open/anncsu-viewer/tree/main/data/indirizzi

Funziona con qualsiasi Python 3 (usa solo la libreria standard):

    python scarica_anncsu.py            # scarica solo se c'e' una versione nuova
    python scarica_anncsu.py --force    # scarica comunque

Passi:
  1. chiede al server dimensione e data dell'ultima modifica del file;
  2. se il file locale e' gia' aggiornato avvisa ed esce;
  3. scarica in un file temporaneo '.part' (il vecchio file resta intatto);
  4. verifica il file scaricato (dimensione + firma Parquet 'PAR1');
  5. sposta il vecchio parquet in 'dati/backup' (si tiene UN solo backup);
  6. mette il nuovo file al posto del vecchio.
"""

import argparse
import glob
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from email.utils import parsedate_to_datetime


# ---------------------------------------------------------------------------
# IMPOSTAZIONI
# I percorsi sono relativi alla cartella che contiene questo script.
# ---------------------------------------------------------------------------
URL = "https://pub-1e760dc850cb4a5aa5f8afb77713f8cd.r2.dev/anncsu-indirizzi.parquet"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "dati")
BACKUP_DIR = os.path.join(DATA_DIR, "backup")
FILE_NAME = "anncsu-indirizzi.parquet"

TARGET = os.path.join(DATA_DIR, FILE_NAME)        # file definitivo
PART = TARGET + ".part"                           # file temporaneo durante il download
# Nome del backup: anncsu-indirizzi_bkp_AAAAMMGG.parquet (data del vecchio file)
BACKUP_PREFIX = "anncsu-indirizzi_bkp_"

CHUNK = 1024 * 1024        # il file viene scaricato a blocchi da 1 MB
TIMEOUT = 60               # secondi di attesa massima senza risposta dal server
USER_AGENT = "anncsu-downloader/1.0"


def parse_args():
    p = argparse.ArgumentParser(description="Scarica il GeoParquet ANNCSU nella cartella dati")
    p.add_argument("--force", action="store_true",
                   help="scarica anche se il file locale risulta gia' aggiornato")
    return p.parse_args()


def mb(n):
    """Byte -> stringa in MB, formattata all'italiana (1.234,5 MB)."""
    s = f"{n / 1024 / 1024:,.1f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".") + " MB"


def info_remota():
    """
    Richiesta HEAD: chiede al server le informazioni sul file senza scaricarlo.
    Restituisce (dimensione in byte, data ultima modifica come timestamp).
    Entrambi possono essere None se il server non li comunica.
    """
    req = urllib.request.Request(URL, method="HEAD", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        size = r.headers.get("Content-Length")
        lm = r.headers.get("Last-Modified")
    size = int(size) if size else None
    mtime = parsedate_to_datetime(lm).timestamp() if lm else None
    return size, mtime


def gia_aggiornato(remote_size, remote_mtime):
    """
    Il file locale e' considerato aggiornato se ha la stessa dimensione di quello
    sul server e non e' piu' vecchio della sua data di ultima modifica.
    """
    if not os.path.isfile(TARGET) or remote_size is None:
        return False
    st = os.stat(TARGET)
    if st.st_size != remote_size:
        return False
    # Se il server non da' la data, basta la dimensione uguale
    return remote_mtime is None or st.st_mtime >= remote_mtime - 1


def scarica(remote_size):
    """Scarica URL nel file .part mostrando l'avanzamento."""
    req = urllib.request.Request(URL, headers={"User-Agent": USER_AGENT})
    t0 = time.time()
    last_print = 0
    done = 0
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r, open(PART, "wb") as f:
        total = remote_size or int(r.headers.get("Content-Length") or 0)
        while True:
            buf = r.read(CHUNK)
            if not buf:
                break
            f.write(buf)
            done += len(buf)
            now = time.time()
            if now - last_print >= 0.5:          # aggiorna la riga 2 volte al secondo
                last_print = now
                speed = done / max(now - t0, 0.001)
                if total:
                    print(f"\r  {done / total * 100:5.1f}%  {mb(done)} di {mb(total)}"
                          f"  ({mb(speed)}/s)   ", end="", flush=True)
                else:
                    print(f"\r  {mb(done)}  ({mb(speed)}/s)   ", end="", flush=True)
    print()
    return done, time.time() - t0


def verifica(expected_size):
    """
    Controlla che il file scaricato sia completo e sia davvero un Parquet:
    un file Parquet inizia e finisce con i 4 byte 'PAR1'.
    Restituisce None se e' tutto ok, altrimenti il messaggio d'errore.
    """
    size = os.path.getsize(PART)
    if expected_size is not None and size != expected_size:
        return f"dimensione {size} byte, attesi {expected_size}: download incompleto"
    if size < 12:
        return "file troppo piccolo per essere un Parquet"
    with open(PART, "rb") as f:
        head = f.read(4)
        f.seek(-4, os.SEEK_END)
        tail = f.read(4)
    if head != b"PAR1" or tail != b"PAR1":
        return "il file scaricato non e' un Parquet valido"
    return None


def backup_vecchio():
    """
    Sposta il parquet attuale in dati/backup, cancellando prima il backup
    precedente (si tiene un solo backup). Restituisce il percorso del backup.
    """
    if not os.path.isfile(TARGET):
        return None
    os.makedirs(BACKUP_DIR, exist_ok=True)
    for vecchio in glob.glob(os.path.join(BACKUP_DIR, BACKUP_PREFIX + "*.parquet")):
        os.remove(vecchio)
    data = datetime.fromtimestamp(os.path.getmtime(TARGET)).strftime("%Y%m%d")
    dest = os.path.join(BACKUP_DIR, f"{BACKUP_PREFIX}{data}.parquet")
    os.replace(TARGET, dest)
    return dest


def rimuovi_part():
    if os.path.isfile(PART):
        os.remove(PART)


def main():
    args = parse_args()
    os.makedirs(DATA_DIR, exist_ok=True)

    # --- 1. Informazioni sul file remoto ------------------------------------
    print(f"Sorgente: {URL}")
    try:
        remote_size, remote_mtime = info_remota()
    except (urllib.error.URLError, OSError) as e:
        sys.exit(f"Impossibile contattare il server: {e}")

    if remote_mtime:
        print(f"Versione online: {datetime.fromtimestamp(remote_mtime):%d/%m/%Y %H:%M}"
              + (f", {mb(remote_size)}" if remote_size else ""))

    # --- 2. File locale gia' aggiornato? ------------------------------------
    if not args.force and gia_aggiornato(remote_size, remote_mtime):
        print(f"\nIl file presente e' gia' aggiornato all'ultima versione disponibile:\n"
              f"  {TARGET}\nNessun download necessario (usa --force per scaricarlo comunque).")
        return

    # Spazio su disco: serve posto per il nuovo file mentre il vecchio esiste ancora
    if remote_size:
        libero = shutil.disk_usage(DATA_DIR).free
        if libero < remote_size * 1.05:
            sys.exit(f"Spazio su disco insufficiente: servono {mb(remote_size)}, "
                     f"liberi {mb(libero)}.")

    # --- 3. Download nel file temporaneo ------------------------------------
    print(f"Download in corso -> {PART}")
    try:
        done, secs = scarica(remote_size)
    except KeyboardInterrupt:
        print()
        rimuovi_part()
        sys.exit("Download interrotto dall'utente. Il file esistente non e' stato modificato.")
    except (urllib.error.URLError, OSError) as e:
        print()
        rimuovi_part()
        sys.exit(f"Errore durante il download: {e}\nIl file esistente non e' stato modificato.")

    # --- 4. Verifica ----------------------------------------------------------
    errore = verifica(remote_size)
    if errore:
        rimuovi_part()
        sys.exit(f"Verifica fallita: {errore}.\nIl file esistente non e' stato modificato.")

    # La data del file locale diventa quella della versione online: serve al
    # controllo "gia' aggiornato" delle esecuzioni successive.
    if remote_mtime:
        os.utime(PART, (remote_mtime, remote_mtime))

    # --- 5. Backup del vecchio file e 6. sostituzione -------------------------
    bkp = backup_vecchio()
    os.replace(PART, TARGET)

    # --- 7. Riepilogo ----------------------------------------------------------
    print(f"\nScaricati {mb(done)} in {secs:.0f} s")
    print(f"Nuovo file : {TARGET}")
    print(f"Backup     : {bkp}" if bkp else "Backup     : nessuno (non c'era un file precedente)")


if __name__ == "__main__":
    main()
