"""
Geocodifica di indirizzi (via, civico, comune in campi separati) usando il
database dei civici ANNCSU in Spatialite (creato con load_civici.py).

E' un algoritmo Processing di QGIS. Per usarlo:
    Strumenti di Processing -> icona Python -> "Aggiungi script agli strumenti..."
    (oppure apri il file nell'Editor di script di Processing e premi "Esegui").
Lo trovi poi in: Script -> ANNCSU -> "Geocodifica indirizzi (ANNCSU) v1".

Strategia di ricerca, per ogni indirizzo:
  1. COMUNE : nome normalizzato (maiuscole, senza accenti/apostrofi);
              se non trovato -> confronto approssimato (refusi).
  2. VIA    : a) nome completo identico       (es. "VIA ROMA")
              b) nome senza il tipo di strada  (es. "ROMA" o "V.LE ROMA")
              c) parole compatibili            (es. "VIA MARCONI" -> "VIA GUGLIELMO MARCONI",
                                                     "VIA G. MARCONI" -> idem)
              d) confronto approssimato         (refusi, soglia regolabile)
  3. CIVICO : a) civico + esponente esatti
              b) stesso civico, esponente diverso/mancante
              c) interpolazione tra i civici vicini dello stesso lato (pari/dispari)
              d) civico piu' vicino sulla stessa via (entro una tolleranza)
              e) civico oltre l'ultimo della via (es. 999 con ultimo 120):
                 punto sul civico con il numero maggiore (fine della via);
                 civico prima del primo (es. 1 con primo 40): punto sul
                 civico con il numero minore (inizio della via)
              f) "centro" della via (opzionale)
  Se la via non si trova: "centro" del comune (opzionale).
  Il "centro" e' il civico reale piu' vicino al baricentro, cosi' il punto
  cade sempre su un accesso esistente e non in mezzo a un campo.

Ogni riga in uscita riporta i campi originali piu':
  geo_stato   OK | ESPONENTE_DIVERSO | INTERPOLATO | CIVICO_VICINO |
              INIZIO_VIA | FINE_VIA | SOLO_VIA | SOLO_COMUNE | NON_TROVATO
  geo_punti   affidabilita' 0-100
  geo_via, geo_civico, geo_esp, geo_comune, geo_istat  -> cosa e' stato trovato
  geo_id_via (progressivo nazionale), geo_id_acc (progressivo accesso)
  geo_lon, geo_lat (WGS84)
  geo_rivedere 'SI' se la via coincide solo per nome (tipo di strada assente o
              diverso, es. 'Cavour' o 'Corso Cavour' con VIA e PIAZZA CAVOUR) o e'
              ambigua: il punto viene messo, ma con punteggio massimo 50.
              Se invece esiste la via con lo stesso tipo cercato, vale solo quella.
  geo_note    spiegazione dell'esito (alternative scartate, tipo diverso, ...)
Le righe non trovate vengono scritte senza geometria (nessuna riga si perde).
Non trovate, SOLO_COMUNE e da rivedere finiscono anche nella tabella "non trovati o da rivedere"
(nel riepilogo, DA_RIVEDERE e' un conteggio gia' incluso negli stati sopra).
"""

import difflib
import math
import os
import re
import sqlite3
import unicodedata
from collections import defaultdict

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsFeatureSink,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsPointXY,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterCrs,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterField,
    QgsProcessingParameterFile,
    QgsProcessingParameterNumber,
    QgsProject,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QVariant


# ===========================================================================
# IMPOSTAZIONI
# ===========================================================================
TABELLA_CIVICI = "tbl_anncsu_civici"
DB_DEFAULT_RELATIVO = os.path.join("dati", "db_civici_anncsu.sqlite")

# Punteggio di affidabilita' per ogni esito (0-100)
PUNTI = {
    "OK": 100,
    "ESPONENTE_DIVERSO": 90,
    "INTERPOLATO": 75,
    "CIVICO_VICINO": 60,
    "INIZIO_VIA": 50,
    "FINE_VIA": 50,
    "SOLO_VIA": 40,
    "SOLO_COMUNE": 10,
    "NON_TROVATO": 0,
}
# Penalita' applicata quando la via e' stata trovata in modo approssimato
# (l'ordine conta: e' anche la priorita' tra comuni omonimi)
PENALITA_VIA = {"esatta": 0, "parole": 5, "fuzzy": 15, "ambigua": 0, "solo_nome": 0}
# Modi di ricerca della via che rendono il risultato "da rivedere" e punteggio massimo
DA_RIVEDERE = {"ambigua", "solo_nome"}
PUNTI_MAX_DA_RIVEDERE = 50

# Tipi di strada (DUG) e relative abbreviazioni -> forma estesa ANNCSU
ABBREVIAZIONI = {
    "V": "VIA", "VA": "VIA",
    "VLE": "VIALE", "VL": "VIALE",
    "P": "PIAZZA", "PZA": "PIAZZA", "PZZA": "PIAZZA", "PIAZ": "PIAZZA", "PZ": "PIAZZA",
    "PLE": "PIAZZALE", "PZLE": "PIAZZALE", "PZALE": "PIAZZALE",
    "PTTA": "PIAZZETTA", "PZTTA": "PIAZZETTA",
    "C": "CORSO", "CSO": "CORSO", "CS": "CORSO",
    "VC": "VICOLO", "VCLO": "VICOLO", "VICO": "VICOLO",
    "LGO": "LARGO", "LG": "LARGO",
    "LUNGOT": "LUNGOTEVERE",
    "STR": "STRADA", "SDA": "STRADA",
    # chiavi senza punti: il testo arriva gia' normalizzato ('Loc.ta' -> 'LOCTA')
    "LOC": "LOCALITA", "LOCAL": "LOCALITA", "LOCTA": "LOCALITA",
    "FRAZ": "FRAZIONE", "FR": "FRAZIONE",
    "CASC": "CASCINA", "CNA": "CASCINA",
    "BGO": "BORGO", "SAL": "SALITA", "VCO": "VICOLO",
    "CTRADA": "CONTRADA", "CDA": "CONTRADA",
    "TRAV": "TRAVERSA", "VILL": "VILLAGGIO",
    # "SS" non e' qui: e' gestito in normalizza_via (strada statale solo se seguito
    # da un numero, Santissima se scritto "SS.")
    "SP": "STRADA PROVINCIALE", "SC": "STRADA COMUNALE",
}
# SS seguito da un numero (SS 9, SS9, S.S. 36, SS. 36) -> STRADA STATALE
_SS_STATALE = re.compile(r"(?<![A-Z])S\s*\.?\s*S\s*\.?\s*(?=\d)", re.IGNORECASE)
# "SS." (con il punto, non seguito da un numero) -> SANTISSIMA
_SS_SANTISSIMA = re.compile(r"(?<![A-Z])SS\.(?!\s*\d)", re.IGNORECASE)
# forme di "santissimo" ridotte a una sola, cosi' 'SS. Crocifisso' trova
# 'SANTISSIMO CROCIFISSO' e 'SS. Trinita'' trova 'SANTISSIMA TRINITA''
SANTISSIMO = {"SANTISSIMA", "SANTISSIMO", "SANTISSIMI", "SANTISSIME"}
# Parole iniziali considerate "tipo di strada" (rimosse per il confronto "senza DUG")
DUG = {
    "VIA", "VIALE", "PIAZZA", "PIAZZALE", "PIAZZETTA", "CORSO", "VICOLO", "LARGO",
    "LUNGOTEVERE", "LUNGOMARE", "LUNGOLAGO", "LUNGARNO", "STRADA", "STRADONE",
    "STRADELLA", "LOCALITA", "FRAZIONE", "CASCINA", "BORGO", "SALITA", "DISCESA",
    "CONTRADA", "TRAVERSA", "VILLAGGIO", "RIONE", "QUARTIERE", "VIOTTOLO",
    "PASSAGGIO", "GALLERIA", "PORTICO", "RAMPA", "SCALINATA", "ROTONDA", "CALLE",
    "FONDAMENTA", "CAMPO", "CAMPIELLO", "SESTIERE", "CORTE", "CORTILE", "VIOTTOLA",
    "STATALE", "PROVINCIALE", "COMUNALE", "VICINALE", "PRIVATA", "VIALETTO",
    "ALZAIA", "BALUARDO", "BASTIONI", "CIRCONVALLAZIONE", "TANGENZIALE",
    "SOTTOPASSO", "PONTE", "MOLO", "BANCHINA", "CALATA", "PARCO", "ZONA", "NUCLEO",
    "LOGGIA", "VIA", "STRADELLO", "VIUZZA", "ANDRONA", "CARRARA", "VARCO",
}
# Parole che non contano nel confronto per parole (articoli/preposizioni)
PAROLE_VUOTE = {"DI", "DEL", "DELLO", "DELLA", "DEI", "DEGLI", "DELLE", "D",
                "DA", "DAL", "DALLA", "E", "ED", "IL", "LO", "LA", "I", "GLI", "LE",
                "L", "A", "AL", "ALLA", "AI", "AGLI", "ALLE", "IN", "SU",
                "DELL", "DALL", "ALL", "SULL", "NELL", "DEGL", "SUL"}
# Valori del campo civico che significano "senza numero"
SENZA_NUMERO = {"", "SNC", "SN", "SENZA NUMERO", "0", "NULL", "NONE"}


# ===========================================================================
# NORMALIZZAZIONE TESTI (funzioni pure, senza QGIS: testabili da sole)
# ===========================================================================
def testo_base(s):
    """Maiuscolo, senza accenti, punteggiatura -> spazio, spazi compattati."""
    if s is None:
        return ""
    s = str(s).strip()
    if s.upper() in ("NULL", "NONE"):
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).upper()
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------------------
# NUMERI NEI NOMI DI VIA
# ANNCSU li scrive in modi diversi anche nello stesso comune:
#   'PIAZZA XX SETTEMBRE' / 'VIA VENTI SETTEMBRE', 'VITTORIO EMANUELE II' /
#   'VITTORIO EMANUELE SECONDO', 'UMBERTO PRIMO', 'QUATTRO NOVEMBRE'.
# Romani, cifre, numeri in lettere e ordinali vengono tutti ridotti alla
# forma '#20', sia nell'indirizzo cercato sia nel DB, cosi' si confrontano.
# ---------------------------------------------------------------------------
def _numeri_in_lettere():
    unita = ["", "UNO", "DUE", "TRE", "QUATTRO", "CINQUE", "SEI", "SETTE", "OTTO", "NOVE"]
    speciali = ["DIECI", "UNDICI", "DODICI", "TREDICI", "QUATTORDICI", "QUINDICI",
                "SEDICI", "DICIASSETTE", "DICIOTTO", "DICIANNOVE"]
    decine = {20: "VENTI", 30: "TRENTA", 40: "QUARANTA", 50: "CINQUANTA",
              60: "SESSANTA", 70: "SETTANTA", 80: "OTTANTA", 90: "NOVANTA"}
    d = {w: i for i, w in enumerate(unita) if w}
    d.update({w: 10 + i for i, w in enumerate(speciali)})
    for n, w in decine.items():
        d[w] = n
        for u in range(1, 10):
            parola = w + unita[u]
            if u in (1, 8):                 # VENTUNO, VENTOTTO: cade la vocale
                parola = w[:-1] + unita[u]
            d[parola] = n + u
    d.update({"CENTO": 100, "MILLE": 1000})
    ordinali = ["PRIMO", "SECONDO", "TERZO", "QUARTO", "QUINTO", "SESTO", "SETTIMO",
                "OTTAVO", "NONO", "DECIMO", "UNDICESIMO", "DODICESIMO", "TREDICESIMO",
                "QUATTORDICESIMO", "QUINDICESIMO", "SEDICESIMO", "DICIASSETTESIMO",
                "DICIOTTESIMO", "DICIANNOVESIMO", "VENTESIMO"]
    for i, w in enumerate(ordinali, 1):
        d[w] = i
        d[w[:-1] + "A"] = i                # PRIMA, SECONDA, TERZA...
    return d


NUMERI_IN_LETTERE = _numeri_in_lettere()
MESI = {"GENNAIO", "FEBBRAIO", "MARZO", "APRILE", "MAGGIO", "GIUGNO", "LUGLIO",
        "AGOSTO", "SETTEMBRE", "OTTOBRE", "NOVEMBRE", "DICEMBRE"}
# romano "stretto" con le sole lettere I V X L (1-89): esclude parole come DI, MI, CI
_ROMANO = re.compile(r"^L?X{0,3}(IX|IV|V?I{0,3})$")
_VALORI_ROMANI = {"I": 1, "V": 5, "X": 10, "L": 50}


def romano_a_intero(s):
    if not s or not _ROMANO.match(s):
        return None
    tot = 0
    for a, b in zip(s, s[1:] + " "):
        v = _VALORI_ROMANI[a]
        tot += -v if _VALORI_ROMANI.get(b, 0) > v else v
    return tot or None


def converti_numeri(parole):
    """
    ['VIA', 'XX', 'SETTEMBRE'] -> ['VIA', '#20', 'SETTEMBRE'];
    ['VIA', 'VITTORIO', 'EMANUELE', 'SECONDO'] -> [..., '#2'].
    Una lettera singola (I, V, X, L) e' considerata numero romano solo se e'
    l'ultima parola ('CARLO V', 'PIO X') o se precede un mese ('I MAGGIO'):
    altrimenti e' l'iniziale di un nome ('VIA V. ALFIERI').
    """
    out = []
    for i, w in enumerate(parole):
        n = None
        if w.isdigit():
            n = int(w)
        elif w in NUMERI_IN_LETTERE:
            n = NUMERI_IN_LETTERE[w]
        elif re.fullmatch(r"\d+(O|A|MO|MA)", w):          # 1O (1°), 2A, 1MO
            n = int(re.match(r"\d+", w).group())
        elif i > 0 or len(parole) > 1:
            if len(w) > 1:
                n = romano_a_intero(w)
            elif i > 0 and (i == len(parole) - 1 or parole[i + 1] in MESI):
                n = romano_a_intero(w)
        out.append(f"#{n}" if n else w)
    return out


def normalizza_comune(nome):
    """'Sant'Angelo Lodigiano' -> 'SANT ANGELO LODIGIANO'; 'S. Colombano' -> 'SAN COLOMBANO'."""
    t = testo_base(nome).split()
    if t and t[0] == "S" and len(t) > 1:
        t[0] = "SAN"
    return " ".join(t)


def normalizza_via(via):
    """
    Restituisce (completo, senza_dug, parole):
      completo   'VIALE PIETRO NENNI'   (abbreviazioni del tipo strada espanse)
      senza_dug  'PIETRO NENNI'
      parole     ['PIETRO', 'NENNI']    (senza articoli/preposizioni)
    """
    raw = "" if via is None else str(via).strip()
    # SS: prima la strada statale (seguito da numero), poi "SS." = Santissima
    raw = _SS_STATALE.sub("STRADA STATALE ", raw)
    raw = _SS_SANTISSIMA.sub("SANTISSIMA ", raw)
    # abbreviazione puntata in prima posizione: 'P.zza', 'V.le', 'C.so' -> 'PZZA', 'VLE', 'CSO'
    primo, _, resto_raw = raw.partition(" ")
    primo_compatto = testo_base(primo).replace(" ", "")
    if "." in primo and primo_compatto in ABBREVIAZIONI:
        raw = primo_compatto + " " + resto_raw
    t = testo_base(raw).split()
    if not t:
        return "", "", []
    # espande l'abbreviazione del tipo strada (solo in prima posizione)
    if t[0] in ABBREVIAZIONI:
        t = ABBREVIAZIONI[t[0]].split() + t[1:]
    t = ["SANTISSIMA" if w in SANTISSIMO else w for w in t]
    t = converti_numeri(t)
    completo = " ".join(t)
    i = 0
    while i < len(t) - 1 and t[i] in DUG:
        i += 1
    resto = t[i:]
    senza_dug = " ".join(resto)
    parole = [w for w in resto if w not in PAROLE_VUOTE]
    return completo, senza_dug, parole


def tipo_strada(via):
    """
    Tipo di strada (l'ultima parola DUG, la piu' specifica):
    'V.le Roma' -> 'VIALE'; 'Via Loggia V. E. II' -> 'LOGGIA'; 'Roma' -> ''.
    """
    completo, senza_dug, _ = normalizza_via(via)
    prefisso = completo[: len(completo) - len(senza_dug)].split()
    return prefisso[-1] if prefisso else ""


def separa_civico(valore):
    """
    '12' -> (12, '');  '12/A' '12 a' '12A' -> (12, 'A');  '12 bis' -> (12, 'BIS')
    'SNC' / vuoto -> (None, '').  Restituisce sempre (numero|None, esponente).
    """
    if valore is None:
        return None, ""
    if isinstance(valore, float) and valore.is_integer():
        valore = int(valore)
    s = testo_base(valore)
    if s in SENZA_NUMERO:
        return None, ""
    m = re.match(r"^(\d+)\s*(.*)$", s)
    if not m:
        return None, ""
    num = int(m.group(1))
    esp = m.group(2).replace(" ", "")
    return (num if num > 0 else None), esp


def normalizza_istat(valore):
    """
    Codice ISTAT comune a 6 cifre: 15146 / 15146.0 / '15146.0' / '015146' -> '015146'.
    Restituisce '' se il valore e' vuoto o non numerico.
    """
    if valore is None:
        return ""
    if isinstance(valore, float):
        if not valore.is_integer():
            return ""
        valore = int(valore)
    s = str(valore).strip()
    if s.upper() in ("", "NULL", "NONE"):
        return ""
    m = re.fullmatch(r"(\d+)(?:\.0+)?", s)
    return m.group(1).zfill(6) if m else ""


# strade numerate: il numero che segue e' il numero della strada, non il civico
# (SS 9, S.S. 36, SP 415, SC 12, Strada Statale 9, Strada Provinciale 415)
_STRADA_NUMERATA = re.compile(
    r"(?:^|[\s,])(?:S\s*\.?\s*[SPC]\s*\.?|STRADA\s+(?:STATALE|PROVINCIALE|COMUNALE))\s*$",
    re.IGNORECASE)


def estrai_civico_da_via(via):
    """
    'Via Roma 12/A' -> ('Via Roma', '12/A'). Se non c'e' un numero finale -> (via, '').
    Il numero dopo SS / SP / SC (o Strada Statale/Provinciale/Comunale) non e' un
    civico: 'SS 9' -> ('SS 9', '').
    """
    if not via:
        return via, ""
    m = re.match(r"^(.*?\D)[\s,]+(\d+\s*(?:[/\-]?\s*[A-Za-z]{1,3}|\s+BIS|\s+TER)?)\s*$",
                 str(via).strip(), re.IGNORECASE)
    if m and not _STRADA_NUMERATA.search(m.group(1)):
        return m.group(1).strip(" ,"), m.group(2)
    return via, ""


def parole_compatibili(input_parole, db_parole):
    """
    True se ogni parola cercata compare (in ordine) tra quelle della via in banca dati,
    accettando le iniziali: 'G MARCONI' ~ 'GUGLIELMO MARCONI', 'MARCONI' ~ idem,
    'S CHIARA' ~ 'SANTA CHIARA'.
    L'ultima parola cercata deve coincidere esattamente (di solito e' il cognome).
    """
    if not input_parole or not db_parole:
        return False
    j = 0
    for k, w in enumerate(input_parole):
        ultima = k == len(input_parole) - 1
        trovato = False
        while j < len(db_parole):
            d = db_parole[j]
            j += 1
            if d == w or (not ultima and len(w) == 1 and d.startswith(w)):
                trovato = True
                break
        if not trovato:
            return False
    return True


# ===========================================================================
# INDICE DEI CIVICI DI UN COMUNE
# ===========================================================================
class Via:
    """Tutti gli accessi di una via di un comune."""

    __slots__ = ("nome", "id_via", "civici", "per_numero", "lon", "lat")

    def __init__(self, nome, id_via):
        self.nome = nome
        self.id_via = id_via
        self.civici = []                         # (num, esp, lon, lat, id_acc)
        self.per_numero = defaultdict(list)      # num -> [(esp, lon, lat, id_acc)]

    def aggiungi(self, num, esp, lon, lat, id_acc):
        self.civici.append((num, esp, lon, lat, id_acc))
        if num is not None:
            self.per_numero[num].append((esp, lon, lat, id_acc))

    def centro(self):
        """Il civico reale piu' vicino al baricentro dei civici della via."""
        return civico_centrale(self.civici)


def civico_centrale(civici):
    if not civici:
        return None
    mx = sum(c[2] for c in civici) / len(civici)
    my = sum(c[3] for c in civici) / len(civici)
    return min(civici, key=lambda c: (c[2] - mx) ** 2 + (c[3] - my) ** 2)


class Comune:
    """Civici di un comune, organizzati per via con chiavi normalizzate."""

    def __init__(self, nome, istat):
        self.nome = nome
        self.istat = istat
        self.vie = {}                          # nome ANNCSU -> Via
        self.per_completo = defaultdict(list)  # 'VIA ROMA' -> [Via]
        self.per_senza_dug = defaultdict(list) # 'ROMA'     -> [Via]
        self.parole = {}                       # nome ANNCSU -> ['ROMA']
        self.tutti = []

    def aggiungi(self, odonimo, id_via, num, esp, lon, lat, id_acc):
        v = self.vie.get(odonimo)
        if v is None:
            v = self.vie[odonimo] = Via(odonimo, id_via)
            completo, senza_dug, parole = normalizza_via(odonimo)
            self.per_completo[completo].append(v)
            self.per_senza_dug[senza_dug].append(v)
            self.parole[odonimo] = parole
        v.aggiungi(num, esp, lon, lat, id_acc)
        self.tutti.append((num, esp, lon, lat, id_acc))

    def trova_via(self, via_input, soglia):
        """
        Restituisce (lista_vie, modo, nota); lista vuota se non trova nulla.
        Modi "sicuri" (stesso tipo di strada):  esatta, parole, fuzzy
        Modi "da rivedere":
          ambigua    piu' vie dello stesso tipo compatibili (VIA G. MARCONI / VIA M. MARCONI)
          solo_nome  coincide solo il nome: tipo non indicato ('CAVOUR') o diverso
                     da quelli presenti ('CORSO CAVOUR' con VIA e PIAZZA CAVOUR)
        Con piu' vie in lista, la scelta finale la fa geocodifica() in base al civico.
        """
        completo, senza_dug, parole = normalizza_via(via_input)
        if not completo:
            return [], None, "via vuota"
        tipo_in = tipo_strada(via_input)

        def stesso_tipo(vie):
            return [v for v in vie if tipo_in and tipo_strada(v.nome) == tipo_in]

        def solo_nome(vie, nota=""):
            note = [nota] if nota else []
            if tipo_in:
                tipi = sorted({tipo_strada(v.nome) or "-" for v in vie})
                note.append(f"tipo strada diverso: cercato {tipo_in}, presenti {'/'.join(tipi)}")
            else:
                note.append("tipo strada non indicato")
            if len(vie) > 1:
                note.append("alternative: " + " / ".join(v.nome for v in vie[:5]))
            return vie, "solo_nome", "; ".join(note)

        # a) nome completo identico (tipo + nome): vince su tutto il resto
        c = self.per_completo.get(completo)
        if c:
            return c, "esatta", ""
        # b) stesso nome, tipo diverso o assente
        c = self.per_senza_dug.get(senza_dug)
        if c:
            s = stesso_tipo(c)
            return (s, "esatta", "") if s else solo_nome(c)
        # c) compatibilita' per parole (iniziali, nomi propri mancanti)
        cand = [self.vie[n] for n, p in self.parole.items() if parole_compatibili(parole, p)]
        if cand:
            cand.sort(key=lambda v: len(self.parole[v.nome]))   # meno parole in piu' prima
            s = stesso_tipo(cand)
            if not s:
                return solo_nome(cand)
            n_min = len(self.parole[s[0].nome])
            migliori = [v for v in s if len(self.parole[v.nome]) == n_min]
            if len(migliori) == 1:
                return migliori, "parole", ""
            return migliori, "ambigua", "via ambigua: " + " / ".join(v.nome for v in migliori[:5])
        # d) confronto approssimato (refusi)
        m = difflib.get_close_matches(senza_dug, list(self.per_senza_dug), n=1, cutoff=soglia)
        if m:
            vie = self.per_senza_dug[m[0]]
            r = difflib.SequenceMatcher(None, senza_dug, m[0]).ratio()
            nota = f"via simile a '{vie[0].nome}' ({r:.0%})"
            s = stesso_tipo(vie)
            return (s, "fuzzy", nota) if s else solo_nome(vie, nota)
        return [], None, "via non trovata"


def scegli_via(vie, num, esp):
    """Tra piu' vie candidate preferisce quella che ha il civico cercato."""
    def chiave(v):
        lista = v.per_numero.get(num, []) if num is not None else []
        return (any((e or "") == esp for e, *_ in lista), bool(lista), len(v.civici))
    return max(vie, key=chiave)


METRI_PER_GRADO = 111320.0   # metri in un grado di latitudine (approssimato)


def civico_preferito(via, n):
    """Tra gli accessi con numero n preferisce quello senza esponente: (esp, lon, lat, id_acc)."""
    return sorted(via.per_numero[n], key=lambda x: (x[0] or "") != "")[0]


def sposta_lungo_via(via, n, lon, lat, verso, metri):
    """
    Sposta il punto (lon, lat) del civico n di 'metri' lungo la via, nel verso
    della numerazione (verso > 0: verso i numeri piu' alti, < 0: piu' bassi).
    La direzione e' quella del civico vicino in quel verso; se non ce n'e'
    (n e' il primo/ultimo) si prosegue oltre, allontanandosi dal civico dall'altra parte.
    Verso un civico esistente il punto si ferma a meta' strada, per non scavalcarlo.
    Restituisce (lon, lat, metri_effettivi); metri_effettivi = 0 se non spostato.
    """
    if metri <= 0 or verso == 0:
        return lon, lat, 0
    kx = METRI_PER_GRADO * math.cos(math.radians(lat))   # metri per grado di longitudine
    ky = METRI_PER_GRADO

    def primo_distinto(numeri):
        # primo civico della lista con una posizione diversa da quella del civico n
        for k in numeri:
            _, x, y, _ = civico_preferito(via, k)
            dx, dy = (x - lon) * kx, (y - lat) * ky
            d = math.hypot(dx, dy)
            if d > 0.5:
                return dx, dy, d
        return None

    # civico senza numero (es. SNC): nessun verso di numerazione -> spostamento fisso
    numeri = sorted(via.per_numero) if n is not None else []
    avanti = [k for k in numeri if (k - n) * verso > 0]       # nel verso cercato
    indietro = [k for k in numeri if (k - n) * verso < 0]     # dalla parte opposta
    avanti.sort(key=lambda k: abs(k - n))
    indietro.sort(key=lambda k: abs(k - n))

    v = primo_distinto(avanti)
    if v:
        dx, dy, d = v
        passo = min(metri, d / 2)          # verso un civico reale: al massimo a meta'
    else:
        v = primo_distinto(indietro)
        if not v:
            # unico civico della via (o senza numero): spostamento fisso verso nord-est
            dx, dy, d = 1.0, 1.0, math.sqrt(2)
        else:
            dx, dy, d = v
            dx, dy = -dx, -dy              # si allontana dal civico dall'altra parte
        passo = metri
    return (lon + dx / d * passo / kx, lat + dy / d * passo / ky, passo)


def trova_civico(via, num, esp, tolleranza, spostamento=0.0):
    """
    Cerca il civico sulla via. Restituisce (stato, lon, lat, num, esp, id_acc, nota)
    oppure None se non c'e' nulla entro la tolleranza.
    Per CIVICO_VICINO, INIZIO_VIA e FINE_VIA (e SOLO_VIA, in geocodifica) il punto e' spostato di
    'spostamento' metri dal civico usato (0 = nessuno spostamento).
    """
    if num is None:
        return None
    lista = via.per_numero.get(num)
    if lista:
        for e, lon, lat, ida in lista:
            if (e or "") == esp:
                return "OK", lon, lat, num, e, ida, ""
        # stesso numero, esponente diverso: preferisce quello senza esponente
        lista_ord = sorted(lista, key=lambda x: (x[0] or "") != "")
        e, lon, lat, ida = lista_ord[0]
        nota = f"esponente richiesto '{esp or '-'}', trovato '{e or '-'}'"
        return "ESPONENTE_DIVERSO", lon, lat, num, e, ida, nota

    numeri = sorted(via.per_numero)
    if not numeri:
        return None
    stesso_lato = [n for n in numeri if n % 2 == num % 2]
    sotto = [n for n in stesso_lato if n < num]
    sopra = [n for n in stesso_lato if n > num]

    # interpolazione tra i due civici dello stesso lato che lo racchiudono
    if sotto and sopra and (sopra[0] - sotto[-1]) <= 2 * tolleranza:
        a, b = sotto[-1], sopra[0]
        # estremi: l'accesso senza esponente (di solito quello principale sul fronte strada)
        _, xa, ya, _ = civico_preferito(via, a)
        _, xb, yb, _ = civico_preferito(via, b)
        f = (num - a) / (b - a)
        return ("INTERPOLATO", xa + f * (xb - xa), ya + f * (yb - ya), num, esp, None,
                f"interpolato tra il {a} e il {b}")

    # Nei casi seguenti il punto si appoggia a un civico esistente: viene spostato
    # di 'spostamento' metri lungo la via, verso il civico cercato, per non
    # sovrapporsi al punto del civico reale.
    def esito(stato, n, nota):
        e, lon, lat, ida = civico_preferito(via, n)
        lon, lat, spostato = sposta_lungo_via(via, n, lon, lat, num - n, spostamento)
        if spostato:
            nota += f" (spostato di {spostato:.0f} m)"
        return stato, lon, lat, n, e, ida, nota

    # civico piu' vicino (prima lo stesso lato, poi qualsiasi)
    for gruppo in (stesso_lato, numeri):
        if gruppo:
            n = min(gruppo, key=lambda x: abs(x - num))
            if abs(n - num) <= tolleranza:
                return esito("CIVICO_VICINO", n, f"usato il civico {n}")

    # civico oltre l'ultimo della via (es. VIA ROMA 999 con ultimo civico 120):
    # il punto va dopo il civico con il numero maggiore, cioe' alla fine della via
    if num > numeri[-1]:
        n = numeri[-1]
        return esito("FINE_VIA", n,
                     f"civico {num} oltre l'ultimo della via ({n}): posizionato a fine via")
    # civico prima del primo della via (es. VIA ROMA 1 con primo civico 40):
    # il punto va prima del civico con il numero minore, cioe' all'inizio della via
    if num < numeri[0]:
        n = numeri[0]
        return esito("INIZIO_VIA", n,
                     f"civico {num} prima del primo della via ({n}): posizionato a inizio via")
    return None


# ===========================================================================
# ACCESSO AL DATABASE
# ===========================================================================
class DbCivici:
    """Legge il DB ANNCSU con sqlite3 (piu' veloce di OGR, non serve Spatialite)."""

    def __init__(self, percorso):
        if not os.path.isfile(percorso):
            raise FileNotFoundError(percorso)
        uri = "file:" + percorso.replace("\\", "/") + "?mode=ro"
        self.con = sqlite3.connect(uri, uri=True)
        self.cache = {}
        # nomi dei comuni presenti (usa l'indice su NOME_COMUNE: < 1 s)
        self.comuni = defaultdict(list)    # nome normalizzato -> [nome ANNCSU]
        for (nome,) in self.con.execute(
                f'SELECT DISTINCT nome_comune FROM "{TABELLA_CIVICI}"'):
            if nome:
                self.comuni[normalizza_comune(nome)].append(nome)

    def chiudi(self):
        self.con.close()

    def risolvi_comune(self, comune_input, soglia):
        """Restituisce (nome ANNCSU | None, nota)."""
        k = normalizza_comune(comune_input)
        if not k:
            return None, "comune vuoto"
        if k in self.comuni:
            return self.comuni[k][0], ""
        m = difflib.get_close_matches(k, list(self.comuni), n=1, cutoff=max(soglia, 0.85))
        if m:
            nome = self.comuni[m[0]][0]
            return nome, f"comune simile a '{nome}'"
        return None, "comune non presente nel DB"

    def carica(self, nome_comune):
        """Restituisce {codice_istat: Comune} (piu' voci se esistono comuni omonimi)."""
        if nome_comune in self.cache:
            return self.cache[nome_comune]
        out = {}
        sql = (f'SELECT codice_istat, odonimo, progressivo_nazionale, civico, esponente, '
               f'longitude, latitude, progressivo_accesso '
               f'FROM "{TABELLA_CIVICI}" WHERE nome_comune = ? AND longitude IS NOT NULL')
        for istat, odo, idv, num, esp, lon, lat, ida in self.con.execute(sql, (nome_comune,)):
            # stessa forma usata per il codice in input (testo a 6 cifre)
            istat = normalizza_istat(istat) or str(istat)
            c = out.get(istat)
            if c is None:
                c = out[istat] = Comune(nome_comune, istat)
            n, e = separa_civico(num)
            c.aggiungi(odo or "", idv, n, testo_base(esp).replace(" ", ""), lon, lat, ida)
        self.cache[nome_comune] = out
        return out


# ===========================================================================
# GEOCODIFICA DI UN INDIRIZZO (senza QGIS)
# ===========================================================================
def geocodifica(db, via_in, civico_in, comune_in, istat_in=None, soglia=0.8,
                tolleranza=10, fallback_via=True, fallback_comune=False, spostamento=5.0):
    """Restituisce un dizionario con l'esito (chiavi = campi geo_*)."""
    r = dict(stato="NON_TROVATO", punti=0, via="", civico=None, esp="", comune="",
             istat="", id_via=None, id_acc=None, lon=None, lat=None, note=[],
             rivedere=False)

    # civico scritto dentro il campo via ("Via Roma 12") e campo civico vuoto
    num, esp = separa_civico(civico_in)
    if num is None:
        via2, civ2 = estrai_civico_da_via(via_in)
        if civ2:
            via_in = via2
            num, esp = separa_civico(civ2)
            r["note"].append("civico letto dal campo via")
    if num is None:
        r["note"].append("civico mancante")

    nome, nota = db.risolvi_comune(comune_in, soglia)
    if nota:
        r["note"].append(nota)
    if not nome:
        return r
    comuni = db.carica(nome)
    ist = normalizza_istat(istat_in)
    if ist:
        if ist in comuni:
            comuni = {ist: comuni[ist]}
        else:
            r["note"].append(f"codice ISTAT {ist} non corrisponde a '{nome}'")
    elif len(comuni) > 1:
        r["note"].append(f"comune omonimo in {len(comuni)} province")

    # cerca la via in ognuno dei comuni candidati (piu' di uno solo se omonimi)
    candidati = []                       # (rango, comune, via, modo, nota)
    ultima_nota = "via non trovata"
    for c in comuni.values():
        vie, modo, nota_via = c.trova_via(via_in, soglia)
        if vie:
            v = scegli_via(vie, num, esp)
            candidati.append((list(PENALITA_VIA).index(modo), c, v, modo, nota_via))
        else:
            ultima_nota = nota_via
    migliore = None
    comune_ambiguo = False
    if candidati:
        rango_min = min(x[0] for x in candidati)
        pari = [x for x in candidati if x[0] == rango_min]
        # a parita' di qualita' della via preferisce il comune dove esiste il civico cercato
        def ha_civico(x):
            lista = x[2].per_numero.get(num, []) if num is not None else []
            return (any((e or "") == esp for e, *_ in lista), bool(lista))
        migliore = max(pari, key=ha_civico)   # a parita', vince il primo trovato
        if len(pari) > 1:
            # la stessa via esiste in piu' comuni omonimi: il punto va controllato
            comune_ambiguo = True
            r["note"].append("via presente in piu' comuni omonimi (ISTAT "
                             + " / ".join(x[1].istat for x in pari) + ")")
    if migliore is None:
        r["note"].append(ultima_nota if comuni else "nessun civico per il comune")
        c = next(iter(comuni.values()), None)
        if c and len(comuni) == 1:
            r.update(comune=c.nome, istat=c.istat)
        if c and fallback_comune and len(comuni) == 1:
            cc = civico_centrale(c.tutti)
            r.update(stato="SOLO_COMUNE", comune=c.nome, istat=c.istat, lon=cc[2], lat=cc[3])
            r["punti"] = PUNTI["SOLO_COMUNE"]
        return r

    _, c, v, modo, nota_via = migliore
    if nota_via:
        r["note"].append(nota_via)
    r.update(comune=c.nome, istat=c.istat, via=v.nome, id_via=v.id_via)

    trovato = trova_civico(v, num, esp, tolleranza, spostamento)
    if trovato:
        stato, lon, lat, n, e, ida, nota_civ = trovato
        r.update(stato=stato, lon=lon, lat=lat, civico=n, esp=e or "", id_acc=ida)
        if nota_civ:
            r["note"].append(nota_civ)
    elif fallback_via:
        cc = v.centro()
        # spostato dal civico centrale verso il civico cercato (se manca: verso i
        # numeri piu' alti), per non sovrapporsi al punto del civico reale
        n_c = cc[0]
        verso = (num - n_c) if (num is not None and n_c is not None) else 1
        lon, lat, sp = sposta_lungo_via(v, n_c, cc[2], cc[3], verso, spostamento)
        r.update(stato="SOLO_VIA", lon=lon, lat=lat)
        if num is not None:
            r["note"].append(f"civico {num}{esp} non trovato sulla via")
        if sp:
            r["note"].append(f"centro della via, spostato di {sp:.0f} m")
    else:
        r["note"].append("civico non trovato")
        return r

    r["punti"] = max(PUNTI[r["stato"]] - PENALITA_VIA[modo], 1)
    # coincidenza solo sul nome o via ambigua: il punto c'e', ma va controllato
    if modo in DA_RIVEDERE or comune_ambiguo:
        r["rivedere"] = True
        r["punti"] = min(r["punti"], PUNTI_MAX_DA_RIVEDERE)
    return r


# ===========================================================================
# ALGORITMO PROCESSING
# ===========================================================================
CAMPI_GEO = [
    ("geo_stato", QVariant.String, 20),
    ("geo_punti", QVariant.Int, 0),
    ("geo_via", QVariant.String, 120),
    ("geo_civico", QVariant.Int, 0),
    ("geo_esp", QVariant.String, 10),
    ("geo_comune", QVariant.String, 80),
    ("geo_istat", QVariant.String, 6),
    ("geo_id_via", QVariant.LongLong, 0),
    ("geo_id_acc", QVariant.LongLong, 0),
    ("geo_lon", QVariant.Double, 0),
    ("geo_lat", QVariant.Double, 0),
    ("geo_rivedere", QVariant.String, 2),
    ("geo_note", QVariant.String, 254),
]


class GeocodificaAnncsu(QgsProcessingAlgorithm):
    INPUT = "INPUT"
    CAMPO_VIA = "CAMPO_VIA"
    CAMPO_CIVICO = "CAMPO_CIVICO"
    CAMPO_COMUNE = "CAMPO_COMUNE"
    CAMPO_ISTAT = "CAMPO_ISTAT"
    DB = "DB"
    SOGLIA = "SOGLIA"
    TOLLERANZA = "TOLLERANZA"
    SPOSTAMENTO = "SPOSTAMENTO"
    FALLBACK_VIA = "FALLBACK_VIA"
    FALLBACK_COMUNE = "FALLBACK_COMUNE"
    CRS = "CRS"
    OUTPUT = "OUTPUT"
    NON_TROVATI = "NON_TROVATI"

    def name(self):
        return "geocodifica_anncsu_v1"

    def displayName(self):
        return "Geocodifica indirizzi (ANNCSU) v1"

    def group(self):
        return "ANNCSU"

    def groupId(self):
        return "anncsu"

    def shortHelpString(self):
        return ("Geocodifica indirizzi con via, civico e comune in campi separati "
                "usando il database dei civici ANNCSU (Spatialite). "
                "Gestisce abbreviazioni (V.le, P.zza...), esponenti (12/A, 12 bis), "
                "refusi, civici mancanti (interpolazione, civico vicino, inizio o "
                "fine via se il numero e' fuori dalla numerazione della via). "
                "Il campo geo_stato indica la qualita' del risultato.")

    def createInstance(self):
        return GeocodificaAnncsu()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.INPUT, "Tabella/layer degli indirizzi", [QgsProcessing.TypeVector]))
        self.addParameter(QgsProcessingParameterField(
            self.CAMPO_VIA, "Campo via", parentLayerParameterName=self.INPUT))
        self.addParameter(QgsProcessingParameterField(
            self.CAMPO_CIVICO, "Campo civico (puo' contenere anche l'esponente)",
            parentLayerParameterName=self.INPUT, optional=True))
        self.addParameter(QgsProcessingParameterField(
            self.CAMPO_COMUNE, "Campo comune", parentLayerParameterName=self.INPUT))
        self.addParameter(QgsProcessingParameterField(
            self.CAMPO_ISTAT, "Campo codice ISTAT comune (opzionale, risolve i comuni omonimi)",
            parentLayerParameterName=self.INPUT, optional=True))

        home = QgsProject.instance().homePath()
        default_db = os.path.join(home, DB_DEFAULT_RELATIVO) if home else None
        self.addParameter(QgsProcessingParameterFile(
            self.DB, "Database civici ANNCSU (.sqlite)", extension="sqlite",
            defaultValue=default_db))

        p = QgsProcessingParameterNumber(
            self.SOGLIA, "Somiglianza minima per nomi con refusi (0-1)",
            QgsProcessingParameterNumber.Double, 0.80, minValue=0.5, maxValue=1.0)
        self.addParameter(p)
        self.addParameter(QgsProcessingParameterNumber(
            self.TOLLERANZA, "Massima differenza di numero civico accettata",
            QgsProcessingParameterNumber.Integer, 10, minValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            self.SPOSTAMENTO, "Spostamento dei punti CIVICO_VICINO / INIZIO_VIA / FINE_VIA / "
            "SOLO_VIA dal civico usato (metri, 0 = nessuno)",
            QgsProcessingParameterNumber.Double, 5.0, minValue=0.0, maxValue=100.0))
        self.addParameter(QgsProcessingParameterBoolean(
            self.FALLBACK_VIA, "Se il civico manca, posiziona al centro della via", True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.FALLBACK_COMUNE, "Se la via manca, posiziona al centro del comune", False))
        self.addParameter(QgsProcessingParameterCrs(
            self.CRS, "Sistema di riferimento dell'output", "EPSG:4326"))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, "Indirizzi geocodificati", QgsProcessing.TypeVectorPoint))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.NON_TROVATI, "Indirizzi non trovati o da rivedere", QgsProcessing.TypeVector,
            optional=True, createByDefault=True))

    def processAlgorithm(self, parameters, context, feedback):
        source = self.parameterAsSource(parameters, self.INPUT, context)
        if source is None:
            raise QgsProcessingException("Layer di input non valido")
        f_via = self.parameterAsString(parameters, self.CAMPO_VIA, context)
        f_civ = self.parameterAsString(parameters, self.CAMPO_CIVICO, context)
        f_com = self.parameterAsString(parameters, self.CAMPO_COMUNE, context)
        f_ist = self.parameterAsString(parameters, self.CAMPO_ISTAT, context)
        db_path = self.parameterAsFile(parameters, self.DB, context)
        soglia = self.parameterAsDouble(parameters, self.SOGLIA, context)
        toll = self.parameterAsInt(parameters, self.TOLLERANZA, context)
        spost = self.parameterAsDouble(parameters, self.SPOSTAMENTO, context)
        fb_via = self.parameterAsBool(parameters, self.FALLBACK_VIA, context)
        fb_com = self.parameterAsBool(parameters, self.FALLBACK_COMUNE, context)
        crs_out = self.parameterAsCrs(parameters, self.CRS, context)

        # campi in uscita: originali + geo_* (rinomina in caso di conflitto)
        fields = QgsFields(source.fields())
        for nome, tipo, lung in CAMPI_GEO:
            n = nome
            while fields.indexOf(n) >= 0:
                n = "_" + n
            fields.append(QgsField(n, tipo, len=lung))
        n_orig = source.fields().count()

        sink, dest = self.parameterAsSink(parameters, self.OUTPUT, context, fields,
                                          QgsWkbTypes.MultiPoint, crs_out)
        if sink is None:
            raise QgsProcessingException("Impossibile creare l'output")
        sink_nt, dest_nt = self.parameterAsSink(parameters, self.NON_TROVATI, context,
                                                fields, QgsWkbTypes.NoGeometry)

        feedback.pushInfo(f"Apro il database: {db_path}")
        try:
            db = DbCivici(db_path)
        except Exception as e:
            raise QgsProcessingException(f"Impossibile aprire il database: {e}")
        feedback.pushInfo(f"Comuni presenti nel database: {len(db.comuni)}")

        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        xform = None
        if crs_out.isValid() and crs_out != wgs84:
            xform = QgsCoordinateTransform(wgs84, crs_out, context.transformContext())

        # ordina per comune: ogni comune viene caricato dal DB una volta sola
        feats = sorted(source.getFeatures(),
                       key=lambda f: normalizza_comune(f[f_com]) if f_com else "")
        totale = len(feats) or 1
        conteggio = defaultdict(int)
        errori_scrittura = 0
        ultimo_comune = None

        try:
            for i, f in enumerate(feats):
                if feedback.isCanceled():
                    break
                comune_k = normalizza_comune(f[f_com])
                if comune_k != ultimo_comune:
                    # libera la memoria del comune precedente
                    db.cache.clear()
                    ultimo_comune = comune_k
                    feedback.setProgressText(f"Comune: {f[f_com]}")

                r = geocodifica(db, f[f_via], f[f_civ] if f_civ else None, f[f_com],
                                f[f_ist] if f_ist else None, soglia, toll, fb_via, fb_com,
                                spost)
                conteggio[r["stato"]] += 1

                out = QgsFeature(fields)
                attrs = list(f.attributes())[:n_orig]
                attrs += [r["stato"], r["punti"], r["via"], r["civico"], r["esp"],
                          r["comune"], r["istat"], r["id_via"], r["id_acc"],
                          r["lon"], r["lat"], "SI" if r["rivedere"] else "",
                          "; ".join(r["note"])[:254]]
                out.setAttributes(attrs)

                if r["lon"] is not None:
                    pt = QgsPointXY(r["lon"], r["lat"])
                    if xform:
                        pt = xform.transform(pt)
                    # output MultiPoint: ogni indirizzo e' un multipunto con un solo punto
                    out.setGeometry(QgsGeometry.fromMultiPointXY([pt]))
                # tabella di controllo: non trovati + solo comune + da rivedere
                if sink_nt is not None and (r["lon"] is None or r["rivedere"]
                                            or r["stato"] == "SOLO_COMUNE"):
                    # la tabella di controllo non ha geometria: copia senza il punto
                    f_nt = QgsFeature(out)
                    f_nt.clearGeometry()
                    if not sink_nt.addFeature(f_nt, QgsFeatureSink.FastInsert):
                        errori_scrittura += 1
                if r["rivedere"]:
                    conteggio["DA_RIVEDERE"] += 1
                if not sink.addFeature(out, QgsFeatureSink.FastInsert):
                    errori_scrittura += 1
                feedback.setProgress(100.0 * (i + 1) / totale)
        finally:
            db.chiudi()

        # riepilogo
        feedback.pushInfo("\n=== RIEPILOGO ===")
        for stato in list(PUNTI) + ["DA_RIVEDERE"]:
            if conteggio[stato]:
                pct = 100.0 * conteggio[stato] / totale
                feedback.pushInfo(f"{stato:<18} {conteggio[stato]:>7}  ({pct:5.1f}%)")
        if errori_scrittura:
            feedback.reportError(f"{errori_scrittura} righe non sono state scritte "
                                 f"negli output (vedi il registro di Processing)")

        risultati = {self.OUTPUT: dest}
        if sink_nt is not None:
            risultati[self.NON_TROVATI] = dest_nt
        return risultati
