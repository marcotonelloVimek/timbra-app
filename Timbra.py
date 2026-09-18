import streamlit as st
import datetime
import pandas as pd
import os
import calendar
import altair as alt
import sqlite3
import pytz
import smtplib
import importlib
import hashlib
import secrets
import re
import base64
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from io import BytesIO


def _load_optional_dependency(module_name):
    """Carica una dipendenza opzionale senza bloccare l'app se manca."""
    try:
        return importlib.import_module(module_name)
    except ImportError:
        return None


try:
    autorefresh_module = _load_optional_dependency("streamlit_autorefresh")
    if autorefresh_module is not None:
        st_autorefresh = autorefresh_module.st_autorefresh
    else:
        def st_autorefresh(*_args, **_kwargs):
            return None
except Exception:
    def st_autorefresh(*_args, **_kwargs):
        return None


try:
    reportlab_pagesizes = _load_optional_dependency("reportlab.lib.pagesizes")
    reportlab_styles = _load_optional_dependency("reportlab.lib.styles")
    reportlab_units = _load_optional_dependency("reportlab.lib.units")
    reportlab_platypus = _load_optional_dependency("reportlab.platypus")
    reportlab_lib = _load_optional_dependency("reportlab.lib")
    if reportlab_pagesizes and reportlab_styles and reportlab_units and reportlab_platypus and reportlab_lib:
        letter = reportlab_pagesizes.letter
        A4 = reportlab_pagesizes.A4
        landscape = reportlab_pagesizes.landscape
        getSampleStyleSheet = reportlab_styles.getSampleStyleSheet
        ParagraphStyle = reportlab_styles.ParagraphStyle
        inch = reportlab_units.inch
        SimpleDocTemplate = reportlab_platypus.SimpleDocTemplate
        Table = reportlab_platypus.Table
        TableStyle = reportlab_platypus.TableStyle
        Paragraph = reportlab_platypus.Paragraph
        Spacer = reportlab_platypus.Spacer
        PageBreak = reportlab_platypus.PageBreak
        colors = reportlab_lib.colors
        PDF_AVAILABLE = True
    else:
        PDF_AVAILABLE = False
        letter = A4 = landscape = None
        getSampleStyleSheet = ParagraphStyle = None
        inch = None
        SimpleDocTemplate = Table = TableStyle = Paragraph = Spacer = PageBreak = None
        colors = None
except Exception:
    PDF_AVAILABLE = False
    letter = A4 = landscape = None
    getSampleStyleSheet = ParagraphStyle = None
    inch = None
    SimpleDocTemplate = Table = TableStyle = Paragraph = Spacer = PageBreak = None
    colors = None

try:
    PIL_Image = _load_optional_dependency("PIL.Image")
    PIL_AVAILABLE = PIL_Image is not None
except Exception:
    PIL_Image = None
    PIL_AVAILABLE = False

# --- CONFIGURAZIONE DATABASE SQLite ---
DB_FILE = "timbrature_aziendali.db"
DB_TIMEOUT = 30  # Timeout connessione database in secondi
TIMEZONE = pytz.timezone('Europe/Rome')  # Configurare il fuso orario appropriato

# --- CONNESSIONE AL DATABASE: file locale o database esterno persistente ---
# Di default Timbra usa il file SQLite locale DB_FILE, esattamente come sempre: va
# benissimo per l'uso in locale, per i test e per un hosting con disco persistente
# (es. un server/VPS proprio). Su un hosting SENZA disco persistente (es. Streamlit
# Community Cloud, dove ogni riavvio/redeploy dell'app cancella i file locali creati
# dall'app, .db incluso), il file locale verrebbe perso periodicamente: in quel caso va
# configurato un database esterno persistente (es. Turso, compatibile con SQLite),
# impostando le variabili TURSO_DATABASE_URL e TURSO_AUTH_TOKEN come variabili
# d'ambiente o come "Secrets" dell'app su Streamlit Community Cloud. Quando sono
# presenti, Timbra si collega automaticamente lì invece che al file locale: nessun
# altro codice va cambiato.
# NOTA - pacchetto Python del client Turso: qui si usa "libsql" (pacchetto ufficiale
# "tursodatabase/libsql-python", installabile con "pip install libsql"), che offre
# un'interfaccia sincrona identica a sqlite3 (connect/cursor/execute/commit, usabile
# con "with conn:"). È stato scelto dopo un test pratico con un database Turso reale:
# un'altra libreria candidata, "turso_serverless", si è rivelata inaffidabile (errore
# nella verifica del token di autenticazione reale: "JWT error: Base64 error"), mentre
# "libsql" si è confermata quella attivamente mantenuta e consigliata nella
# documentazione ufficiale di Turso per una connessione remota sincrona. Prima di ogni
# nuovo deploy conviene comunque rilanciare lo script di test "test_connessione_turso.py"
# fornito a parte, per riconfermare che tutto funzioni ancora con la versione della
# libreria disponibile in quel momento.
_turso_client_module = None
_turso_client_module_caricato = False

def _load_turso_client():
    """Carica il modulo client di Turso (senza bloccare l'app se non è installato: serve
    solo quando è configurato un database esterno)."""
    global _turso_client_module, _turso_client_module_caricato
    if not _turso_client_module_caricato:
        _turso_client_module = _load_optional_dependency("libsql")
        _turso_client_module_caricato = True
    return _turso_client_module

def get_turso_config():
    """Restituisce (url, auth_token) se è configurato un database Turso esterno
    (variabili d'ambiente o Secrets di Streamlit), altrimenti (None, None)."""
    url = os.getenv("TURSO_DATABASE_URL")
    token = os.getenv("TURSO_AUTH_TOKEN")
    if not url:
        try:
            url = st.secrets.get("TURSO_DATABASE_URL")
            token = st.secrets.get("TURSO_AUTH_TOKEN")
        except Exception:
            pass
    return (url, token) if url else (None, None)

def db_connect():
    """Apre una connessione al database, da usare al posto di sqlite3.connect(...) in
    tutto il codice: se è configurato un database Turso esterno la usa (persistente
    anche su hosting senza disco persistente), altrimenti apre il file SQLite locale
    DB_FILE come sempre."""
    url, token = get_turso_config()
    if url:
        turso = _load_turso_client()
        if turso is None:
            raise RuntimeError(
                "TURSO_DATABASE_URL è configurato ma la libreria client di Turso "
                "(libsql) non è installata: aggiungila a requirements.txt "
                "('pip install libsql') o correggi _load_turso_client() se il "
                "nome del pacchetto risultasse cambiato (vedi il commento sopra)."
            )
        return turso.connect(url, auth_token=token)
    return sqlite3.connect(DB_FILE, timeout=DB_TIMEOUT)

def db_error_classes(nome_eccezione):
    """Classi di eccezione da usare in 'except' per gli errori del database (es.
    'IntegrityError', 'OperationalError'), valide sia per il file SQLite locale sia per
    il client di un database esterno come Turso, che pur seguendo lo standard DB-API di
    Python può sollevare proprie classi di eccezione invece di quelle di sqlite3."""
    classi = [getattr(sqlite3, nome_eccezione)]
    turso = _load_turso_client()
    if turso is not None and hasattr(turso, nome_eccezione):
        classi.append(getattr(turso, nome_eccezione))
    return tuple(classi)

def db_migration_error_classes():
    """Classi di eccezione da ignorare nelle guardie di migrazione 'ALTER TABLE ADD
    COLUMN ... già esistente' in init_db(). Con il file SQLite locale basta
    OperationalError, ma verificato con un database Turso reale il client 'libsql'
    segnala il tentativo di aggiungere una colonna già esistente con un semplice
    ValueError invece che con un'eccezione OperationalError: quando è configurato
    Turso viene quindi ignorato anche quello."""
    classi = list(db_error_classes("OperationalError"))
    if get_turso_config()[0]:
        classi.append(ValueError)
    return tuple(classi)

# Ferie/permessi annuali di default, usati come punto di partenza per ogni livello
# CCNL in "livelli_ferie_permessi" e come fallback per chi non ha un livello impostato.
DEFAULT_FERIE_GIORNI = 20
DEFAULT_PERMESSO_ORE = 16.0
DEFAULT_COSTO_ORARIO = 25.0  # €/h di partenza per il costo del personale per livello (da personalizzare)
ORE_ORDINARIE_GIORNALIERE = 8.0  # Soglia oltre la quale le ore lavorate in un giorno sono straordinario

# Stime di partenza per la sezione "Sostenibilità": quanto risparmia (in costi di
# gestione ufficio) e quanta CO2 evita l'azienda per ogni ora di smart working invece
# che in presenza. Sono stime esterne (Osservatorio Smart Working del Politecnico di
# Milano / osservatori.net), non calcolate sui dati reali dell'azienda: vanno
# personalizzate da "Sostenibilità" in base ai costi energetici e alla situazione
# reale dell'ufficio.
# - Risparmio energetico: ~500 €/anno per postazione -> /~1600 ore lavorative annue
# - CO2: ~50 kg/anno per dipendente con 2 giorni/settimana di smart working
#   -> /(2gg * ~46 settimane lavorative * 8h/giorno) = /736 ore
DEFAULT_COSTO_ORARIO_UFFICIO_EVITATO = 0.31  # €/h di smart working
DEFAULT_CO2_KG_ORARIO_EVITATO = 0.07  # kg CO2/h di smart working

# Fattore di emissione medio (kg CO2/km) usato per stimare la CO2 evitata dal mancato
# tragitto casa-lavoro nei giorni di smart working, quando il dipendente ha impostato la
# propria distanza casa-lavoro in "Area Personale". Valore di partenza: media nazionale
# 2022 delle emissioni reali su strada dell'intero parco auto italiano (161,7 g CO2/km),
# secondo i dati ISPRA (indicatoriambientali.isprambiente.it). Non tiene conto del mezzo
# realmente usato da ciascun dipendente (auto, moto, mezzi pubblici, bici): personalizza
# il valore da "Sostenibilità" se hai un dato più preciso per la tua azienda.
DEFAULT_CO2_KG_PER_KM_PENDOLARISMO = 0.162

# --- SICUREZZA PASSWORD ---
# Le password NON vengono più salvate in chiaro nel database: vengono trasformate
# con PBKDF2-HMAC-SHA256 + salt casuale per utente prima di essere salvate.
PASSWORD_HASH_ITERATIONS = 200_000

def hash_password(password, salt=None):
    """Genera un hash sicuro (salt$hash in esadecimale) per una password in chiaro."""
    if salt is None:
        salt = secrets.token_hex(16)
    pwd_hash = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt), PASSWORD_HASH_ITERATIONS)
    return f"{salt}${pwd_hash.hex()}"

def verify_password(password, stored_value):
    """Verifica una password rispetto al valore salvato (hash salt$hash)."""
    if not stored_value or "$" not in stored_value:
        return False
    salt, hash_hex = stored_value.split("$", 1)
    try:
        expected = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt), PASSWORD_HASH_ITERATIONS)
    except ValueError:
        return False
    return secrets.compare_digest(expected.hex(), hash_hex)

def is_legacy_plaintext_password(stored_value):
    """Le password già hashate hanno sempre la forma 'salt$hash'; se manca il
    separatore, si tratta di una password salvata in chiaro dalle versioni precedenti."""
    return bool(stored_value) and "$" not in stored_value

@st.cache_resource
def init_db():
    """Inizializza il database e crea le tabelle se non esistono. Il decoratore
    'cache_resource' fa sì che questa funzione venga eseguita una sola volta
    all'avvio dell'app (o dopo un riavvio/redeploy), invece che ad ogni singola
    interazione dell'utente come farebbe normalmente Streamlit: con un database
    remoto come Turso questo evita una ventina di operazioni di rete inutili ad ogni
    clic, ed è il motivo principale per cui l'app era diventata lenta."""
    with db_connect() as conn:
        c = conn.cursor()
        # Migliora la concorrenza quando più dipendenti usano l'app contemporaneamente.
        # Hanno senso solo per il file SQLite locale: un database esterno come Turso
        # gestisce la concorrenza lato server, e potrebbe non supportare queste PRAGMA.
        if not get_turso_config()[0]:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=30000")

        # Tabella Utenti - AGGIUNTO RUOLO INTERMEDIO
        c.execute('''CREATE TABLE IF NOT EXISTS utenti (
                        username TEXT PRIMARY KEY,
                        nome TEXT,
                        password TEXT,
                        role TEXT,
                        area TEXT,
                        posizione TEXT,
                        responsabile_area TEXT DEFAULT NULL,
                        colore TEXT DEFAULT '#4fa8ff',
                        livello TEXT DEFAULT '',
                        data_assunzione TEXT DEFAULT '',
                        codice_fiscale TEXT DEFAULT '',
                        data_nascita TEXT DEFAULT '',
                        distanza_km REAL DEFAULT 0
                    )''')

        # Migrazione: aggiungi le colonne livello (inquadramento CCNL), data di
        # assunzione e i dati dell'Area Personale (codice fiscale, data di nascita,
        # distanza casa-lavoro) se il DB esisteva già prima di queste versioni
        for colonna_utente, tipo_colonna in [
            ("livello", "TEXT DEFAULT ''"), ("data_assunzione", "TEXT DEFAULT ''"),
            ("codice_fiscale", "TEXT DEFAULT ''"), ("data_nascita", "TEXT DEFAULT ''"),
            ("distanza_km", "REAL DEFAULT 0"),
        ]:
            try:
                c.execute(f"ALTER TABLE utenti ADD COLUMN {colonna_utente} {tipo_colonna}")
                conn.commit()
            except db_migration_error_classes():
                pass  # Colonna già esistente

        # Tabella Livelli CCNL - ferie/permessi annuali e costo orario per livello di
        # inquadramento (nel settore metalmeccanico il livello incide sulla paga, non
        # sulle ferie/permessi previsti dal CCNL; qui però è l'azienda a impostare i
        # propri valori per livello, configurabili liberamente dall'admin).
        c.execute('''CREATE TABLE IF NOT EXISTS livelli_ferie_permessi (
                        livello TEXT PRIMARY KEY,
                        ferie_giorni_anno REAL NOT NULL,
                        permesso_ore_anno REAL NOT NULL,
                        costo_orario REAL NOT NULL DEFAULT 25.0
                    )''')
        # Migrazione: aggiungi la colonna costo_orario se la tabella esisteva già
        try:
            c.execute(f"ALTER TABLE livelli_ferie_permessi ADD COLUMN costo_orario REAL NOT NULL DEFAULT {DEFAULT_COSTO_ORARIO}")
            conn.commit()
        except db_migration_error_classes():
            pass  # Colonna già esistente
        # Valori di partenza per i livelli standard del CCNL Metalmeccanico Industria
        # (uguali per tutti come punto di partenza: l'admin li personalizza da
        # "Gestione Utenti DB" -> "Livelli CCNL"). Il costo orario NON è un dato
        # ufficiale del CCNL: è solo un valore di partenza da correggere con il costo
        # aziendale reale del personale per livello.
        for livello_default in ["1", "2", "3", "3S", "4", "5", "5S", "6", "7"]:
            c.execute("INSERT OR IGNORE INTO livelli_ferie_permessi (livello, ferie_giorni_anno, permesso_ore_anno, costo_orario) VALUES (?,?,?,?)",
                      (livello_default, DEFAULT_FERIE_GIORNI, DEFAULT_PERMESSO_ORE, DEFAULT_COSTO_ORARIO))

        # Tabella Sostenibilità - parametri (personalizzabili) per stimare risparmio
        # energetico e CO2 evitata dallo smart working; riga singola (id=1).
        c.execute('''CREATE TABLE IF NOT EXISTS impostazioni_sostenibilita (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        costo_orario_ufficio_evitato REAL NOT NULL,
                        co2_kg_orario_evitato REAL NOT NULL,
                        co2_kg_per_km_pendolarismo REAL NOT NULL DEFAULT 0.162
                    )''')
        # Migrazione: aggiungi il fattore di emissione per il tragitto casa-lavoro se la
        # tabella esisteva già prima di questa versione
        try:
            c.execute(f"ALTER TABLE impostazioni_sostenibilita ADD COLUMN co2_kg_per_km_pendolarismo REAL NOT NULL DEFAULT {DEFAULT_CO2_KG_PER_KM_PENDOLARISMO}")
            conn.commit()
        except db_migration_error_classes():
            pass  # Colonna già esistente
        c.execute("INSERT OR IGNORE INTO impostazioni_sostenibilita (id, costo_orario_ufficio_evitato, co2_kg_orario_evitato, co2_kg_per_km_pendolarismo) VALUES (1, ?, ?, ?)",
                  (DEFAULT_COSTO_ORARIO_UFFICIO_EVITATO, DEFAULT_CO2_KG_ORARIO_EVITATO, DEFAULT_CO2_KG_PER_KM_PENDOLARISMO))

        c.execute('''CREATE TABLE IF NOT EXISTS fasi_area (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        area TEXT NOT NULL,
                        fase TEXT NOT NULL,
                        UNIQUE(area, fase)
                    )''')

        # Tabella Commesse - creabili solo dall'admin
        c.execute('''CREATE TABLE IF NOT EXISTS commesse (
                        nome TEXT PRIMARY KEY,
                        descrizione TEXT,
                        cliente TEXT,
                        tipologia_impianto TEXT,
                        anno_produzione INTEGER,
                        paese TEXT,
                        creata_da TEXT,
                        data_creazione TEXT
                    )''')

        # Migrazione: aggiungi le colonne anagrafiche se il DB esisteva già prima di questa versione
        for colonna_commessa, tipo_colonna in [("cliente", "TEXT"), ("tipologia_impianto", "TEXT"), ("anno_produzione", "INTEGER"), ("paese", "TEXT")]:
            try:
                c.execute(f"ALTER TABLE commesse ADD COLUMN {colonna_commessa} {tipo_colonna}")
                conn.commit()
            except db_migration_error_classes():
                pass  # Colonna già esistente

        # Tabella Fasi per Commessa - ogni fase appartiene a UNA commessa e UNA area:
        # l'admin può gestire fasi di qualsiasi area, i responsabili solo quelle della propria.
        # Le ore stimate sono obbligatorie: sono la base del calcolo di produttività
        # (ore stimate / ore effettive: >100% = si è finiti prima, <100% = si è sforato).
        c.execute('''CREATE TABLE IF NOT EXISTS fasi_commessa (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        commessa TEXT NOT NULL,
                        area TEXT NOT NULL,
                        fase TEXT NOT NULL,
                        ore_stimate REAL NOT NULL,
                        creata_da TEXT,
                        data_creazione TEXT,
                        UNIQUE(commessa, area, fase)
                    )''')

        # Tabella Template Fasi - fasi/ore standard per tipologia di impianto, riusabili
        # su più commesse. Una tipologia con almeno una riga qui è "standard": creando
        # una nuova commessa di quella tipologia, le fasi si possono copiare in automatico
        # invece di inserirle a mano (utile per le macchine standard, ripetitive).
        c.execute('''CREATE TABLE IF NOT EXISTS template_fasi (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        tipologia TEXT NOT NULL,
                        area TEXT NOT NULL,
                        fase TEXT NOT NULL,
                        ore_stimate REAL NOT NULL,
                        creata_da TEXT,
                        data_creazione TEXT,
                        UNIQUE(tipologia, area, fase)
                    )''')

        # Tabella Timbrature
        c.execute('''CREATE TABLE IF NOT EXISTS timbrature (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        Data TEXT,
                        Dipendente TEXT,
                        Ora TEXT,
                        Azione TEXT,
                        Luogo TEXT,
                        Dettaglio_Trasferta TEXT,
                        Commessa TEXT,
                        Fase TEXT,
                        Dettaglio_Fase TEXT,
                        Ruolo TEXT,
                        Stato TEXT DEFAULT 'Valida'
                    )''')
        
        # Migrazione: aggiungi colonna Stato se manca
        try:
            c.execute("ALTER TABLE timbrature ADD COLUMN Stato TEXT DEFAULT 'Valida'")
            conn.commit()
        except db_migration_error_classes():
            pass  # Colonna già esiste
        
        # Tabella Richieste
        c.execute('''CREATE TABLE IF NOT EXISTS richieste (
                        ID TEXT PRIMARY KEY,
                        Dipendente TEXT,
                        Tipo TEXT,
                        Data_inizio TEXT,
                        Data_fine TEXT,
                        Ore_permesso TEXT,
                        Motivo TEXT,
                        Stato TEXT,
                        Data_richiesta TEXT,
                        Approvatore_Richiesto TEXT DEFAULT 'admin'
                    )''')
        
        # Migrazione: aggiungi colonna Approvatore_Richiesto se manca
        try:
            c.execute("ALTER TABLE richieste ADD COLUMN Approvatore_Richiesto TEXT DEFAULT 'admin'")
            conn.commit()
        except db_migration_error_classes():
            pass  # Colonna già esiste
        
        # Tabella Rettifiche Timbrature - NUOVA
        c.execute('''CREATE TABLE IF NOT EXISTS rettifiche (
                        ID TEXT PRIMARY KEY,
                        Dipendente TEXT,
                        Azione TEXT,
                        Data_prevista TEXT,
                        Ora_prevista TEXT,
                        Motivo TEXT,
                        Stato TEXT,
                        Data_richiesta TEXT,
                        Approvato_da TEXT DEFAULT NULL
                    )''')

        # Tabella Trasferte - programmate da chi fa parte dell'area Service (o
        # dall'admin) per un operatore scelto tra tutti i dipendenti. Il cliente si
        # ricava dalla commessa scelta (non si inserisce a mano); luogo di lavoro
        # (stato/indirizzo) e albergo sono campi liberi, dato che l'azienda lavora in
        # tutto il mondo; i mezzi di trasporto sono più di uno selezionabile insieme
        # (es. Aereo per arrivare + Auto a noleggio per muoversi sul posto).
        c.execute('''CREATE TABLE IF NOT EXISTS trasferte (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        dipendente TEXT NOT NULL,
                        data_inizio TEXT NOT NULL,
                        data_fine TEXT NOT NULL,
                        commessa TEXT,
                        cliente TEXT,
                        stato TEXT,
                        indirizzo TEXT,
                        albergo TEXT,
                        mezzi TEXT,
                        dettaglio_auto TEXT,
                        auto_propria INTEGER NOT NULL DEFAULT 0,
                        dettaglio_treno TEXT,
                        dettaglio_aereo TEXT,
                        dettaglio_auto_noleggio TEXT,
                        note TEXT,
                        creata_da TEXT,
                        data_creazione TEXT
                    )''')
        # Migrazione: aggiungi le nuove colonne se la tabella esisteva già in una
        # versione precedente (con un solo campo "cliente_luogo" e un solo "mezzo").
        # Le vecchie colonne mezzo/dettaglio_mezzo/cliente_luogo restano nel database
        # per compatibilità con eventuali righe già create, ma non vengono più usate.
        for colonna_trasferta, tipo_colonna in [
            ("commessa", "TEXT"), ("cliente", "TEXT"), ("stato", "TEXT"), ("indirizzo", "TEXT"),
            ("albergo", "TEXT"), ("mezzi", "TEXT"), ("dettaglio_auto", "TEXT"),
            ("dettaglio_treno", "TEXT"), ("dettaglio_aereo", "TEXT"), ("dettaglio_auto_noleggio", "TEXT"),
        ]:
            try:
                c.execute(f"ALTER TABLE trasferte ADD COLUMN {colonna_trasferta} {tipo_colonna}")
                conn.commit()
            except db_migration_error_classes():
                pass  # Colonna già esistente

        # Tabella Report Interventi - compilata dal dipendente per una sua trasferta
        # (una trasferta può avere più report, es. più clienti visitati nello stesso viaggio).
        c.execute('''CREATE TABLE IF NOT EXISTS report_interventi (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        trasferta_id INTEGER NOT NULL,
                        dipendente TEXT NOT NULL,
                        cliente_luogo TEXT,
                        descrizione TEXT,
                        ore_lavoro REAL,
                        commessa TEXT,
                        fase TEXT,
                        data_intervento TEXT,
                        data_creazione TEXT
                    )''')
        # Migrazione: aggiungi la colonna data_intervento se la tabella esisteva già
        # (il giorno specifico a cui si riferiscono le ore calcolate dalle timbrature)
        try:
            c.execute("ALTER TABLE report_interventi ADD COLUMN data_intervento TEXT")
            conn.commit()
        except db_migration_error_classes():
            pass  # Colonna già esistente

        # Tabella Foto Report Interventi - le foto vengono ridimensionate/compresse
        # prima di essere salvate (vedi comprimi_immagine_upload) per non appesantire
        # troppo il database, soprattutto su un database esterno come Turso.
        c.execute('''CREATE TABLE IF NOT EXISTS report_interventi_foto (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        report_id INTEGER NOT NULL,
                        nome_file TEXT,
                        foto BLOB NOT NULL,
                        data_caricamento TEXT
                    )''')

        # Inserimento dell'utente Admin di base se il DB è vuoto
        c.execute("SELECT COUNT(*) FROM utenti")
        if c.fetchone()[0] == 0:
            c.execute("INSERT INTO utenti (username, nome, password, role, area, posizione) VALUES (?,?,?,?,?,?)",
                      ("admin", "Amministratore", hash_password("admin123"), "admin", "All", "Amministratore"))
        conn.commit()

        # Migrazione: elimina spazi accidentali in area/posizione, che impedivano
        # l'abbinamento corretto tra dipendenti e responsabile della stessa area
        c.execute("SELECT username, area, posizione FROM utenti")
        for row_username, row_area, row_posizione in c.fetchall():
            trimmed_area = row_area.strip() if isinstance(row_area, str) else row_area
            trimmed_posizione = row_posizione.strip() if isinstance(row_posizione, str) else row_posizione
            if trimmed_area != row_area or trimmed_posizione != row_posizione:
                c.execute("UPDATE utenti SET area=?, posizione=? WHERE username=?",
                          (trimmed_area, trimmed_posizione, row_username))
        conn.commit()

# Inizializza il database all'avvio
init_db()

# --- FUNZIONI HELPER ---

def get_current_datetime():
    """Restituisce l'ora attuale nel fuso orario corretto."""
    return datetime.datetime.now(TIMEZONE).replace(tzinfo=None)

TIMBRATURA_STALE_HOURS = 20  # oltre questa soglia una timbratura "aperta" è considerata dimenticata

def get_last_timbratura(nome_dipendente):
    """Recupera l'ultima timbratura registrata per un dipendente, a prescindere dal
    giorno in cui è stata fatta (così un turno che attraversa la mezzanotte non
    impedisce di registrare l'uscita). Se però l'ultima timbratura risale a più di
    TIMBRATURA_STALE_HOURS ore fa, viene considerata dimenticata/anomala e ignorata,
    per evitare che un dipendente rimanga bloccato a causa di una timbratura mancata
    nei giorni precedenti."""
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("""SELECT Azione, Data, Ora FROM timbrature
                     WHERE Dipendente = ?
                     ORDER BY Data DESC, Ora DESC, id DESC
                     LIMIT 1""", (nome_dipendente,))
        row = c.fetchone()
    if not row:
        return None
    azione, data_str, ora_str = row
    try:
        ultimo_timestamp = datetime.datetime.strptime(f"{data_str} {ora_str}", "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return azione
    ore_trascorse = (get_current_datetime() - ultimo_timestamp).total_seconds() / 3600
    if ore_trascorse > TIMBRATURA_STALE_HOURS:
        return None
    return azione

def get_fase_aperta(nome_dipendente):
    """Se il dipendente ha in questo momento una fase aperta (ultima azione valida =
    'Inizio fase'), restituisce (Commessa, Fase) di quella fase; altrimenti None.
    Usata per far chiudere sempre la fase giusta con 'Fine fase', senza dipendere
    da cosa è selezionato nelle tendine al momento del click."""
    if get_last_timbratura(nome_dipendente) != "Inizio fase":
        return None
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("""SELECT Commessa, Fase FROM timbrature
                     WHERE Dipendente = ? AND Azione = 'Inizio fase'
                     ORDER BY Data DESC, Ora DESC, id DESC LIMIT 1""", (nome_dipendente,))
        row = c.fetchone()
    return (row[0], row[1]) if row else None

def validate_timbratura(nome_dipendente, tipo_azione):
    """
    Valida se la timbratura è logica.
    Restituisce (valida: bool, messaggio: str, stato: str)
    """
    last_action = get_last_timbratura(nome_dipendente)
    
    # Validazione Ingresso/Uscita
    if tipo_azione == "Ingresso":
        if last_action in ["Ingresso", "Inizio fase"]:
            return False, "❌ Hai già fatto un ingresso! Registra un'uscita prima di un nuovo ingresso.", "Anomalia"
        return True, "", "Valida"
    
    elif tipo_azione == "Uscita":
        if last_action in ["Uscita", None]:
            return False, "❌ Non puoi registrare un'uscita senza aver fatto un ingresso!", "Anomalia"
        return True, "", "Valida"
    
    # Validazione Fasi
    elif tipo_azione == "Inizio fase":
        if last_action == "Inizio fase":
            return False, "❌ Hai già iniziato una fase! Termina la fase attuale prima di iniziarne una nuova.", "Anomalia"
        if last_action is None:
            return False, "❌ Devi fare un INGRESSO prima di iniziare una fase!", "Anomalia"
        return True, "", "Valida"
    
    elif tipo_azione == "Fine fase":
        if last_action not in ["Inizio fase"]:
            return False, "❌ Non puoi terminare una fase se non l'hai ancora iniziata!", "Anomalia"
        return True, "", "Valida"
    
    # Pausa Pranzo
    elif tipo_azione == "Inizio Pausa":
        if last_action not in ["Ingresso", "Fine fase"]:
            return False, "❌ Devi fare un ingresso o terminare una fase prima della pausa.", "Anomalia"
        return True, "", "Valida"
    
    elif tipo_azione == "Fine Pausa":
        if last_action != "Inizio Pausa":
            return False, "❌ Non puoi terminare la pausa se non l'hai ancora iniziata!", "Anomalia"
        return True, "", "Valida"
    
    return True, "", "Valida"

def send_email(destinatario, oggetto, corpo):
    """Invia una email (configurare credenziali SMTP)."""
    try:
        # CONFIGURARE QUESTI PARAMETRI CON LE CREDENZIALI AZIENDALI
        SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
        SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
        EMAIL_SENDER = os.getenv("EMAIL_SENDER", "no-reply@azienda.com")
        EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")
        
        if not EMAIL_PASSWORD:
            st.warning("⚠️ Email non configurate. Contatta l'amministratore.")
            return False
        
        msg = MIMEMultipart()
        msg['From'] = EMAIL_SENDER
        msg['To'] = destinatario
        msg['Subject'] = oggetto
        msg.attach(MIMEText(corpo, 'plain'))
        
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()
        return True
    except Exception as e:
        st.error(f"Errore nell'invio email: {e}")
        return False

def generate_pdf_report(employee_name, period_start, period_end, df_timbrature, _df_requests=None):
    """Genera un rapporto PDF professionale."""
    if not PDF_AVAILABLE:
        st.error("❌ Libreria ReportLab non installata. Installa con: pip install reportlab")
        return None
    
    try:
        buffer = BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=A4, topMargin=0.5*inch, bottomMargin=0.5*inch)
        story = []
        styles = getSampleStyleSheet()
        
        # Titolo
        title_style = ParagraphStyle(
            'CustomTitle',
            parent=styles['Heading1'],
            fontSize=24,
            textColor=colors.HexColor('#1f77b4'),
            spaceAfter=6,
            alignment=1
        )
        story.append(Paragraph("📊 RAPPORTO TIMBRATURE", title_style))
        story.append(Spacer(1, 0.2*inch))
        
        # Informazioni dipendente
        info_style = styles['Normal']
        story.append(Paragraph(f"<b>Dipendente:</b> {employee_name}", info_style))
        story.append(Paragraph(f"<b>Periodo:</b> {period_start.isoformat()} → {period_end.isoformat()}", info_style))
        story.append(Paragraph(f"<b>Data generazione:</b> {datetime.date.today().isoformat()}", info_style))
        story.append(Spacer(1, 0.3*inch))
        
        # Tabella timbrature
        if not df_timbrature.empty:
            story.append(Paragraph("<b>Dettaglio Timbrature</b>", styles['Heading2']))

            # Se il report copre più dipendenti, aggiungi la colonna Dipendente e
            # colora ogni riga con il colore assegnato a quel dipendente, per
            # individuarli a colpo d'occhio.
            dipendenti_presenti = df_timbrature['Dipendente'].astype(str).unique().tolist() if 'Dipendente' in df_timbrature.columns else []
            multi_dipendente = len(dipendenti_presenti) > 1
            colori_dipendenti = get_users_colors_map() if multi_dipendente else {}

            intestazione = (["Dipendente"] if multi_dipendente else []) + ["Data", "Azione", "Luogo", "Ora", "Commessa"]
            table_data = [intestazione]
            for _, row in df_timbrature.iterrows():
                riga = []
                if multi_dipendente:
                    riga.append(str(row.get('Dipendente', '-'))[:18])
                riga += [
                    str(row.get('Data', '-'))[:10],
                    str(row.get('Azione', '-'))[:15],
                    str(row.get('Luogo', '-'))[:12],
                    str(row.get('Ora', '-'))[:8],
                    str(row.get('Commessa', '-'))[:15]
                ]
                table_data.append(riga)

            col_widths = ([1.1*inch] if multi_dipendente else []) + [1.1*inch, 1.2*inch, 1*inch, 0.8*inch, 1.4*inch]
            table = Table(table_data, colWidths=col_widths)
            stile_tabella = [
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1f77b4')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 9),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
                ('GRID', (0, 0), (-1, -1), 1, colors.black),
                ('FONTSIZE', (0, 1), (-1, -1), 8),
            ]
            if multi_dipendente:
                for i, (_, row) in enumerate(df_timbrature.iterrows(), start=1):
                    colore_riga = colori_dipendenti.get(str(row.get('Dipendente', '')), '#f5f5dc')
                    stile_tabella.append(('BACKGROUND', (0, i), (-1, i), colors.HexColor(colore_riga)))
            else:
                stile_tabella.append(('BACKGROUND', (0, 1), (-1, -1), colors.beige))
            table.setStyle(TableStyle(stile_tabella))
            story.append(table)
            story.append(Spacer(1, 0.2*inch))

            if multi_dipendente:
                story.append(Paragraph("<b>Legenda dipendenti</b>", styles['Heading3']))
                for dip in sorted(dipendenti_presenti):
                    colore_dip = colori_dipendenti.get(dip, '#f5f5dc')
                    story.append(Paragraph(
                        f'<font backColor="{colore_dip}">&nbsp;&nbsp;&nbsp;&nbsp;</font>&nbsp; {dip}',
                        styles['Normal']
                    ))
                story.append(Spacer(1, 0.2*inch))
        
        doc.build(story)
        buffer.seek(0)
        return buffer
    except Exception as e:
        st.error(f"Errore nella generazione PDF: {e}")
        return None

def generate_pdf_cartellino_mensile(nome_dipendente, anno, mese, cartellino_df, totali, saldo):
    """Genera il PDF del cartellino mensile di un dipendente: una riga per ogni giorno
    (ingresso/uscita, pausa pranzo, commesse/fasi lavorate, luogo, ore ordinarie/
    straordinarie), seguita dai totali del mese e dal saldo ferie/permessi maturato."""
    if not PDF_AVAILABLE:
        st.error("❌ Libreria ReportLab non installata. Installa con: pip install reportlab")
        return None

    try:
        buffer = BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=landscape(A4), topMargin=0.4*inch, bottomMargin=0.4*inch)
        story = []
        styles = getSampleStyleSheet()

        title_style = ParagraphStyle('CartellinoTitle', parent=styles['Heading1'], fontSize=18,
                                      textColor=colors.HexColor('#1f77b4'), spaceAfter=6, alignment=1)
        story.append(Paragraph(f"🗓️ CARTELLINO MENSILE — {calendar.month_name[mese].capitalize()} {anno}", title_style))
        story.append(Spacer(1, 0.15*inch))

        info_style = styles['Normal']
        story.append(Paragraph(f"<b>Dipendente:</b> {nome_dipendente}", info_style))
        story.append(Paragraph(f"<b>Data generazione:</b> {datetime.date.today().isoformat()}", info_style))
        story.append(Spacer(1, 0.2*inch))

        if not cartellino_df.empty:
            intestazione = ["Giorno", "Ingresso", "Uscita", "Inizio\nPausa", "Fine\nPausa", "Commesse/Fasi", "Dove", "Ore\nord.", "Ore\nstraord."]
            table_data = [intestazione]
            for _, row in cartellino_df.iterrows():
                table_data.append([
                    str(row.get("Giorno", "-")),
                    str(row.get("Ingresso", "-")),
                    str(row.get("Uscita", "-")),
                    str(row.get("Inizio Pausa", "-")),
                    str(row.get("Fine Pausa", "-")),
                    str(row.get("Commesse/Fasi", "-"))[:60],
                    str(row.get("Dove", "-"))[:20],
                    str(row.get("Ore ordinarie", 0)),
                    str(row.get("Ore straordinarie", 0)),
                ])
            col_widths = [0.85*inch, 0.65*inch, 0.65*inch, 0.65*inch, 0.65*inch, 3.1*inch, 1.0*inch, 0.55*inch, 0.6*inch]
            table = Table(table_data, colWidths=col_widths, repeatRows=1)
            stile_tabella = [
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1f77b4')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 8),
                ('FONTSIZE', (0, 1), (-1, -1), 7),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 6),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
                ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
            ]
            table.setStyle(TableStyle(stile_tabella))
            story.append(table)
            story.append(Spacer(1, 0.25*inch))

        story.append(Paragraph("<b>Totali del mese</b>", styles['Heading2']))
        story.append(Paragraph(
            f"Giorni lavorati: {totali['giorni_lavorati']} &nbsp;|&nbsp; "
            f"Ore ordinarie: {totali['ore_ordinarie']:.2f}h &nbsp;|&nbsp; "
            f"Ore straordinarie: {totali['ore_straordinarie']:.2f}h &nbsp;|&nbsp; "
            f"Ore su commesse: {totali['ore_su_commesse']:.2f}h", info_style))
        story.append(Spacer(1, 0.15*inch))

        story.append(Paragraph("<b>Saldo ferie e permessi (maturati fino a questo mese)</b>", styles['Heading2']))
        story.append(Paragraph(f"Livello CCNL: {saldo['livello']}", info_style))
        story.append(Paragraph(
            f"Ferie: maturate {saldo['ferie_maturate']:.1f}gg — usate {saldo['ferie_usate']}gg — residue {saldo['ferie_residue']:.1f}gg", info_style))
        story.append(Paragraph(
            f"Permesso/ROL: maturate {saldo['permesso_maturate']:.1f}h — usate {saldo['permesso_usate']:.1f}h — residue {saldo['permesso_residue']:.1f}h", info_style))

        doc.build(story)
        buffer.seek(0)
        return buffer
    except Exception as e:
        st.error(f"Errore nella generazione PDF del cartellino: {e}")
        return None

# --- FINE CONFIGURAZIONE DATABASE ---

# --- IDENTITA' VISIVA VIMEK (logo, colori, font) -----------------------------------
# Stile grafico aziendale scelto da Marco tra tre template di confronto mostrati come
# mockup HTML (blu scuro Vimek + bianco, menu laterale scuro, font "Titillium Web" -
# alternativa gratuita dallo spirito simile all'Eurostile del logo aziendale). Tutto
# quello che segue e' solo CSS/HTML di presentazione: non tocca in alcun modo la logica
# dell'app, quindi puo' essere modificato o rimosso senza alcun impatto funzionale.
VIMEK_NAVY = "#052341"
VIMEK_NAVY_2 = "#0a3a63"
VIMEK_LOGO_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAACcAAAAl4CAYAAABKkIZJAAABgmlDQ1BJQ0MgcHJvZmlsZQAAKM+VkTlIA0EYhT+jEhXFwhQigltEKwUvxFKiGAQFSSJ4Fe5uTBSya9iN2FgKtgELj8arsLHW1sJWEAQPEFsbK0UbkfWfjZAgRHBgmI838x4zbyBwkDEtt6obLDvnxKIRbXpmVgs+E6SNWnpAN93sRHw0QdnxcUuFWm+6VBb/Gw3JRdeECk14yMw6OeEF4YG1XFbxjnDIXNKTwqfCnY5cUPhe6UaBXxSnfQ6ozJCTiA0Lh4S1dAkbJWwuOZZwv3A4admSH5gucFLxumIrs2r+3FO9sH7RnoorXWYrUcaYYBINg1WWyZCjS1ZbFJeY7EfK+Ft8/6S4DHEtY4pjhBUsdN+P+oPf3bqpvt5CUn0Eqp88760dglvwlfe8z0PP+zqCyke4sIv+lQMYfBc9X9TC+9C4AWeXRc3YhvNNaH7I6o7uS5UyA6kUvJ7IN81A0zXUzRV6+9nn+A4S0tX4FezuQUdasufLvLumtLc/z/j9EfkGgh1yrdx1/okAAAAJcEhZcwAADsQAAA7EAZUrDhsAAAAHdElNRQfnBx8KBDlLMdKZAAD+IUlEQVR4XuzddXhY5d3/8W+aOrQUCsV9wGC4bdgYDNcBgyHDrVDaUlzqxaGUBnfosMHQAcPdHYoVl5YKdUkqkR/Jc2+/Pc+AQRs5J+f1uq5eOe877E/oknxyn5Ka7wQAAAAAAAAAAADkTIv0EQAAAAAAAAAAAHLFAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAAAHLJAA4AAAAAAAAAAIBcMoADAAAAAAAAAAAglwzgAAAAAAAAAAAAyCUDOAAAAAAAAAAA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SQAHAAAAAAAAAABAJgngAAAAAAAAAAAAyCQBHAAAAAAAAAAAAJkkgAMAAAAAAAAAACCTBHAAAAAAAAAAAABkkgAOAAAAAAAAAACATBLAAQAAAAAAAAAAkEkCOAAAAAAAAAAAADJJAAcAAAAAAAAAAEAmCeAAAAAAAAAAAADIJAEcAAAAAAAAAAAAmSSAAwAAAAAAAAAAIJMEcAAAAAAAAAAAAGSSAA4AAAAAAAAAAIBMEsABAAAAAAAAAACQSQI4AAAAAAAAAAAAMkkABwAAAAAAAAAAQCYJ4AAAAAAAAAAAAMgkARwAAAAAAAAAAACZJIADAAAAAAAAAAAgkwRwAAAAAAAAAAAAZJIADgAAAAAAAAAAgEwSwAEAAAAAAAAAAJBJAjgAAAAAAAAAAAAySQAHAAAAAAAAAABAJgngAAAAAAAAAAAAyCQBHAAAAAAAAAAAAJkkgAMAAAAAAAAAACCTBHAAAAAAAAAAAABkkgAOAAAAAAAAAACATBLAAQAAAAAAAAAAkEkCOAAAAAAAAAAAADJJAAcAAAAAAAAAAEAmCeAAAAAAAAAAAADIJAEcAAAAAAAAAAAAmSSAAwAAAAAAAAAAIJMEcAAAAAAAAAAAAGSSAA4AAAAAAAAAAIBMEsABAAAAAAAAAACQSQI4AAAAAAAAAAAAMkkABwAAAAAAAAAAQCYJ4AAAAAAAAAAAAMgkARwAAAAAAAAAAACZJIADAAAAAAAAAAAgkwRwAAAAAAAAAAAAZJIADgAAAAAAAAAAgEwSwAEAAAAAAAAAAJBJAjgAAAAAAAAAAAAySQAHAAAAAAAAAABAJgngAAAAAAAAAAAAyCQBHAAAAAAAAAAAAJkkgAMAAAAAAAAAACCTBHAAAAAAAAAAAABkkgAOAAAAAAAAAACATBLAAQAAAAAAAAAAkEkCOAAAAAAAAAAAADJJAAcAAAAAAAAAAEAmCeAAAAAAAAAAAADIJAEcAAAAAAAAAAAAmSSAAwAAAAAAAAAAIJMEcAAAAAAAAAAAAGSSAA4AAAAAAAAAAIBMEsABAAAAAAAAAACQSQI4AAAAAAAAAAAAMkkABwAAAAAAAAAAQCYJ4AAAAAAAAAAAAMgkARwAAAAAAAAAAACZJIADAAAAAAAAAAAgkwRwAAAAAAAAAAAAZJIADgAAAAAAAAAAgEwSwAEAAAAAAAAAAJBJAjgA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AAAAIJMEcAAAAAAAAAAAAGSSAA4AAAAAAAAAAIBMEsABAAAAAAAAAACQSQI4AAAAAAAAAAAAMkkABwAAAAAAAAAAQCYJ4AAAAAAAAAAAAMgkARwAAAAAAAAAAACZJIADAAAAAAAAAAAgkwRwAAAAAAAAAAAAZJIADgAAAAAAAAAAgEwSwAEAAAAAAAAAAJBJAjgAAAAAAAAAAAAySQAHAAAAAAAAAABAJgngAAAAAAAAAAAAyCQBHAAAAAAAAAAAAJkkgAMAAAAAAAAAACCTBHAAAAAAAAAAAABkkgAOAAAAAAAAAACATBLAAQAAAAAAAAAAkEkCOAAAAAAAAAAAADJJAAcAAAAAAAAAAEAmCeAAAAAAAAAAAADIJAEcAAAAAAAAAAAAmSSAAwAAAAAAAAAAIJMEcAAAAAAAAAAAAGSSAA4AAAAAAAAAAIBMEsABAAAAAAAAAACQSQI4AAAAAAAAAAAAMkkABwAAAAAAAAAAQCYJ4AAAAAAAAAAAAMgkARwAAAAAAAAAAACZJIADAAAAAAAAAAAgkwRwAAAAAAAAAAAAZJIADgAAAAAAAAAAgEwSwAEAAAAAAAAAAJBJAjgAAAAAAAAAAAAySQAHAAAAAAAAAABAJgngAAAAAAAAAAAAyCQBHAAAAAAAAAAAAJkkgAMAAAAAAAAAACCTBHAAAAAAAAAAAABkkgAOAAAAAAAAAACATBLAAQAAAAAAAAAAkEkCOAAAAAAAAAAAADJJAAcAAAAAAAAAAEAmCeAAAAAAAAAAAADIJAEcAAAAAAAAAAAAmSSAAwAAAAAAAAAAIJMEcAAAAAAAAAAAAGSSAA4AAAAAAAAAAIBMEsABAAAAAAAAAACQSQI4AAAAAAAAAAAAMkkABwAAAAAAAAAAQCYJ4AAAAAAAAAAAAMgkARwAAAAAAAAAAACZJIADAAAAAAAAAAAgkwRwAAAAAAAAAAAAZJIADgAAAAAAAAAAgEwSwAEAAAAAAAAAAJBJAjgAAAAAAAAAAAAySQAHAAAAAAAAAABAJgngAAAAAAAAAAAAyCQBHAAAAAAAAAAAAJkkgAMAAAAAAAAAACCTBHAAAAAAAAAAAABkkgAOAAAAAAAAAACATBLAAQAAAAAAAAAAkEkCOAAAAAAAAAAAADJJAAcAAAAAAAAAAEAmCeAAAAAAAAAAAADIJAEcAAAAAAAAAAAA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4AAAAAAAAAAAAEglATgAAAAAAAAAAABSSQAOAAAAAAAAAACAVBKAAwAAAAAAAAAAIJUE4AAAAAAAAAAAAEglATgAAAAAAAAAAABSSQAOAAAAAAAAAACAVBKAAwAAAAAAAAAAIJUE4AAAAAAAAAAAAEglATgAAAAAAAAAAABSSQAOAAAAAAAAAACAVBKAAwAAAAAAAAAAIJUE4AAAAAAAAAAAAEglATgAAAAAAAAAAABSSQAOAAAAAAAAAACAVBKAAwAAAAAAAAAAIJUE4AAAAAAAAAAAAEglATgAAAAAAAAAAABSSQAOAAAAAAAAAACAVBKAAwAAAAAAAAAAIJUE4AAAAAAAAAAAAEglATgAAAAAAAAAAABSSQAOAAAAAAAAAACAVBKAAwAAAAAAAAAAIJUE4AAAAAAAAAAAAEglATgAAAAAAAAAAABSSQAOAAAAAAAAAACAVBKAAwAAAAAAAAAAIJUE4AAAAAAAAAAAAEglATgAAAAAAAAAAABSSQAOAAAAAAAAAACAVBKAAwAAAAAAAAAAIJUE4AAAAAAAAAAAAEglATgAAAAAAAAAAABSSQAOAAAAAAAAAACAVBKAAwAAAAAAAAAAIJUE4AAAAAAAAAAAAEglATgAAAAAAAAAAABSSQAOAAAAAAAAAACAVBKAAwAAAAAAAAAAIJUE4AAAAAAAAAAAAEglATgAAAAAAAAAAABSSQAOAAAAAAAAAACAVBKAAwAAAAAAAAAAIJUE4AAAAAAAAAAAAEglATgAAAAAAAAAAABSSQAOAAAAAAAAAACAVBKAAwAAAAAAAAAAIJUE4AAAAAAAAAAAAEglATgAAAAAAAAAAABSSQAOAAAAAAAAAACAVBKAAwAAAAAAAAAAIJUE4AAAAAAAAAAAAEglATgAAAAAAAAAAABSSQAOAAAAAAAAAACAVBKAAwAAAAAAAAAAIJUE4AAAAAAAAAAAAEglATgAAAAAAAAAAABSSQAOAAAAAAAAAACAVBKAAwAAAAAAAAAAIJUE4AAAAAAAAAAAAEglATgAAAAAAAAAAABSSQAOAAAAAAAAAACAVBKAAwAAAAAAAAAAIJUE4AAAAAAAAAAAAEglATgAAAAAAAAAAABSSQAOAAAAAAAAAACAVBKAAwAAAAAAAAAAIJUE4AAAAAAAAAAAAEglATgAAAAAAAAAAABSSQAOAAAAAAAAAACAVBKAAwAAAAAAAAAAIJUE4AAAAAAAAAAAAEglATgAAAAAAAAAAABSSQAOAAAAAAAAAACAVBKAAwAAAAAAAAAAIJUE4AAAAAAAAAAAAEglATgAAAAAAAAAAABSSQAOAAAAAAAAAACAVBKAAwAAAAAAAAAAIJUE4AAAAAAAAAAAAEglATgAAAAAAAAAAABSSQAOAAAAAAAAAACAVBKAAwAAAAAAAAAAIJUE4AAAAAAAAAAAAEglATgAAAAAAAAAAABSSQAOAAAAAAAAAACAVBKAAwAAAAAAAAAAIJUE4AAAAAAAAAAAAEglATgAAAAAAAAAAABSSQAOAAAAAAAAAACAVBKAAwAAAAAAAAAAIJUE4AAAAAAAAAAAAEglATgAAAAAAAAAAABSSQAOAAAAAAAAAACAVBKAAwAAAAAAAAAAIJUE4AAAAAAAAAAAAEglATgAAAAAAAAAAABSSQAOAAAAAAAAAACAVBKAAwAAAAAAAAAAIJUE4AAAAAAAAAAAAEglATgAAAAAAAAAAABSSQAOAAAAAAAAAACAVBKAAwAAAAAAAAAAIJUE4AAAAAAAAAAAAEglATgAAAAAAAAAAABSSQAOAAAAAAAAAACAVBKAAwAAAAAAAAAAIJUE4AAAAAAAAAAAAEglATgAAAAAAAAAAABSSQAOAAAAAAAAAACAVBKAAwAAAAAAAAAAIJUE4AAAAAAAAAAAAEglATgAAAAAAAAAAABSSQAOAAAAAAAAAACAVBKAAwAAAAAAAAAAIJUE4AAAAAAAAAAAAEglATgAAAAAAAAAAABSSQAOAAAAAAAAAACAVBKAAwAAAAAAAAAAIJUE4AAAAAAAAAAAAEglATgAAAAAAAAAAABSSQAOAAAAAAAAAACAVBKAAwAAAAAAAAAAIJUE4AAAAAAAAAAAAEglATgAAAAAAAAAAABSSQAOAAAAAAAAAACAVBKAAwAAAAAAAAAAIJUE4AAAAAAAAAAAAEglATgAAAAAAAAAAABSSQAOAAAAAAAAAACAVBKAAwAAAAAAAAAAIJUE4AAAAAAAAAAAAEglATgAAAAAAAAAAABSSQAOAAAAAAAAAACAVBKAAwAAAAAAAAAAIJUE4AAAAAAAAAAAAEglATgAAAAAAAAAAABSSQAOAAAAAAAAAACAVBKAAwAAAAAAAAAAIJUE4AAAAAAAAAAAAEglATgAAAAAAAAAAABSSQAOAAAAAAAAAACAVBKAAwAAAAAAAAAAIJUE4AAAAAAAAAAAAEglATgAAAAAAAAAAABSSQAOAAAAAAAAAACAVBKAAwAAAAAAAAAAIJUE4AAAAAAAAAAAAEglATgAAAAAAAAAAABSSQAOAAAAAAAAAACAVBKAAwAAAAAAAAAAIJUE4AAAAAAAAAAAAEglATgAAAAAAAAAAABSSQAOAAAAAAAAAACAVBKAAwAAAAAAAAAAIJUE4AAAAAAAAAAAAEglATgAAAAAAAAAAABSSQAOAAAAAAAAAACAVBKAAwAAAAAAAAAAIJUE4AAAAAAAAAAAAEglATgAAAAAAAAAAABSSQAOAAAAAAAAAACAVBKAAwAAAAAAAAAAIJUE4AAAAAAAAAAAAEglATgAAAAAAAAAAABSSQAOAAAAAAAAAACAVBKAAwAAAAAAAAAAIJUE4AAAAAAAAAAAAEglATgAAAAAAAAAAABSKOL/B1LIMem+rxtEAAAAAElFTkSuQmCC"
)


def _inject_stile_vimek():
    """Inietta lo stile grafico aziendale Vimek: font Titillium Web, menu laterale blu
    scuro con testo chiaro, pulsanti e schede (metriche) in stile "card" moderno."""
    st.markdown(f"""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Titillium+Web:wght@300;400;600;700;900&display=swap');
    html, body, [class*="css"] {{
        font-family: 'Titillium Web', sans-serif;
    }}
    h1, h2, h3 {{
        color: {VIMEK_NAVY};
    }}

    /* Menu laterale blu scuro, come nel template scelto */
    section[data-testid="stSidebar"] {{
        background-color: {VIMEK_NAVY};
    }}
    section[data-testid="stSidebar"] * {{
        color: #eaf1f8 !important;
    }}
    section[data-testid="stSidebar"] hr {{
        border-color: rgba(255,255,255,.15);
    }}
    section[data-testid="stSidebar"] .stButton>button {{
        background-color: rgba(255,255,255,.12);
        border: 1px solid rgba(255,255,255,.35);
    }}
    section[data-testid="stSidebar"] .stButton>button:hover {{
        background-color: rgba(255,255,255,.22);
    }}

    /* Pulsanti nell'area principale */
    .stButton>button, .stDownloadButton>button, .stFormSubmitButton>button {{
        background-color: {VIMEK_NAVY};
        color: #fff;
        border: none;
        border-radius: 8px;
        font-weight: 600;
    }}
    .stButton>button:hover, .stDownloadButton>button:hover, .stFormSubmitButton>button:hover {{
        background-color: {VIMEK_NAVY_2};
        color: #fff;
    }}

    /* Schede metriche in stile "card" */
    div[data-testid="stMetric"] {{
        background: #fff;
        border: 1px solid #eef1f4;
        border-radius: 12px;
        padding: 14px 16px;
        box-shadow: 0 1px 3px rgba(20,30,45,.08);
    }}

    .vimek-logo-header {{
        display: flex;
        align-items: center;
        gap: 12px;
        margin-bottom: 10px;
    }}
    .vimek-logo-header img {{
        height: 42px;
    }}
    .vimek-logo-header span {{
        font-weight: 800;
        letter-spacing: .06em;
        font-size: 19px;
        color: {VIMEK_NAVY};
    }}
    </style>
    """, unsafe_allow_html=True)


def _render_logo_header(container=None, centrato=False):
    """Mostra il logo Vimek con la scritta 'VIMEK' a fianco. Se 'container' e'
    st.sidebar viene mostrato nel menu laterale (diventa bianco grazie allo stile
    sopra), altrimenti nell'area principale (rimane blu scuro)."""
    if container is None:
        container = st
    allineamento = "justify-content:center;" if centrato else ""
    container.markdown(f"""
    <div class="vimek-logo-header" style="{allineamento}">
        <img src="data:image/png;base64,{VIMEK_LOGO_BASE64}">
        <span>VIMEK</span>
    </div>
    """, unsafe_allow_html=True)
# --- FINE IDENTITA' VISIVA VIMEK ----------------------------------------------------

st.set_page_config(page_title="Timbra - Vimek", page_icon="🏢", layout="wide")
_inject_stile_vimek()

EXPECTED_COLS = ["Data", "Dipendente", "Ora", "Azione", "Luogo", "Dettaglio Trasferta", "Commessa", "Fase", "Dettaglio Fase", "Ruolo"]
REQUESTS_COLS = ["ID", "Dipendente", "Tipo", "Data_inizio", "Data_fine", "Ore_permesso", "Motivo", "Stato", "Data_richiesta", "Approvatore_Richiesto"]

def init_session():
    if "logged_in" not in st.session_state:
        st.session_state.logged_in = False
    if "username" not in st.session_state:
        st.session_state.username = ""
    if "role" not in st.session_state:
        st.session_state.role = ""

init_session()

# --- FUNZIONI DATABASE UTENTI IN TEMPO REALE ---

def login(username, password):
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT role, password FROM utenti WHERE username=?", (username,))
        row = c.fetchone()
        if not row:
            return False
        role, stored_password = row

        if verify_password(password, stored_password):
            autenticato = True
        elif is_legacy_plaintext_password(stored_password) and stored_password == password:
            # Password salvata in chiaro da una versione precedente: aggiorna
            # automaticamente all'hash sicuro al primo login riuscito.
            c.execute("UPDATE utenti SET password=? WHERE username=?", (hash_password(password), username))
            conn.commit()
            autenticato = True
        else:
            autenticato = False

        if autenticato:
            st.session_state.logged_in = True
            st.session_state.username = username
            st.session_state.role = role
            return True
    return False

def logout():
    st.session_state.logged_in = False
    st.session_state.username = ""
    st.session_state.role = ""

def get_user_info():
    if not st.session_state.logged_in:
        return None
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT nome, password, role, area, posizione FROM utenti WHERE username=?", (st.session_state.username,))
        row = c.fetchone()
        if row:
            return {"name": row[0], "password": row[1], "role": row[2], "area": row[3], "position": row[4]}
    return None

def is_supported_user_role(role_value):
    return str(role_value or "").strip().lower() in {"user", "responsabile"}

def get_user_names():
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT nome FROM utenti WHERE LOWER(COALESCE(role, '')) IN ('user', 'responsabile') ORDER BY nome")
        return [row[0] for row in c.fetchall()]

def get_user_area_by_name(name):
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT area FROM utenti WHERE nome=?", (name,))
        row = c.fetchone()
        return row[0] if row else "Unknown"

def get_users_area_map():
    """Mappa {nome_dipendente: area}, per abbinare l'area a più dipendenti in blocco
    (una sola query invece di una per dipendente/riga). Da preferire a
    get_user_area_by_name() ogni volta che serve l'area di più dipendenti insieme
    (es. dentro un .apply() su una colonna): con un database locale una query in più
    non si notava, ma con un database remoto come Turso ogni query è un giro di rete,
    e farne una per riga può rendere una pagina molto lenta."""
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT nome, area FROM utenti")
        return {row[0]: (row[1] or "Unknown") for row in c.fetchall()}

def get_users_livello_map():
    """Mappa {nome_dipendente: livello}, stesso motivo di get_users_area_map()."""
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT nome, livello FROM utenti")
        return {row[0]: (row[1] or "") for row in c.fetchall()}

def get_livelli_costo_orario_map():
    """Mappa {livello: costo_orario}, stesso motivo di get_users_area_map(): da usare
    insieme a get_users_livello_map() invece di chiamare get_costo_orario_per_livello()
    per ogni dipendente in un ciclo o in un .apply()."""
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT livello, costo_orario FROM livelli_ferie_permessi")
        return {row[0]: float(row[1]) for row in c.fetchall() if row[1] is not None}

def get_users_distanza_map():
    """Mappa {nome_dipendente: distanza_km}, stesso motivo di get_users_area_map(): da
    usare al posto di get_distanza_km_utente() quando serve la distanza di più
    dipendenti insieme (es. nel calcolo CO2 evitata della sezione Sostenibilità)."""
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT nome, distanza_km FROM utenti")
        risultato = {}
        for nome, distanza in c.fetchall():
            try:
                risultato[nome] = float(distanza) if distanza is not None else 0.0
            except (TypeError, ValueError):
                risultato[nome] = 0.0
        return risultato

def get_area_names():
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT DISTINCT area FROM utenti WHERE area != 'All' ORDER BY area")
        return ["Tutte le aree"] + [row[0] for row in c.fetchall()]

def get_users_in_area(area):
    with db_connect() as conn:
        c = conn.cursor()
        if area == "Tutte le aree":
            c.execute("SELECT nome FROM utenti WHERE LOWER(COALESCE(role, '')) IN ('user', 'responsabile') ORDER BY nome")
        else:
            c.execute("SELECT nome FROM utenti WHERE LOWER(COALESCE(role, '')) IN ('user', 'responsabile') AND area=? ORDER BY nome", (area,))
        return [row[0] for row in c.fetchall()]

def prepare_registro_for_export(df, selected_user=None, start_date=None, end_date=None):
    if df is None or df.empty:
        return df.copy() if df is not None else pd.DataFrame(columns=EXPECTED_COLS)

    df_export = df.copy()
    if "Data" in df_export.columns:
        df_export["Data"] = pd.to_datetime(df_export["Data"], errors="coerce").dt.strftime("%Y-%m-%d")

    if isinstance(selected_user, (list, tuple, set)):
        if selected_user:
            df_export = df_export[df_export["Dipendente"].isin(list(selected_user))]
    elif selected_user not in (None, "", "Tutti"):
        df_export = df_export[df_export["Dipendente"] == selected_user]
    if start_date is not None and end_date is not None:
        data_series = pd.to_datetime(df_export["Data"], errors="coerce").dt.date
        df_export = df_export[(data_series >= pd.Timestamp(start_date).date()) & (data_series <= pd.Timestamp(end_date).date())]

    if not df_export.empty:
        sort_cols = [col for col in ["Dipendente", "Data", "Ora"] if col in df_export.columns]
        if sort_cols:
            df_export = df_export.sort_values(by=sort_cols, kind="mergesort")
    return df_export


def get_fasi_area(area=None, posizione=None):
    fallback = []
    position_text = (posizione or "").lower()
    if any(keyword in position_text for keyword in ["programmat", "svilupp", "developer"]):
        fallback = ["Sviluppo", "Messa in servizio", "Assistenza", "Ricambio", "Altro"]
    elif any(keyword in position_text for keyword in ["tecnico", "service", "manutenz", "install"]):
        fallback = ["Messa in servizio", "Assistenza", "Ricambio", "Sviluppo", "Altro"]
    else:
        fallback = ["Lavoro ordinario", "Assistenza", "Ricambio", "Altro"]

    if not area:
        return fallback

    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT fase FROM fasi_area WHERE area=? ORDER BY fase", (area,))
        rows = [row[0] for row in c.fetchall()]
    return rows if rows else fallback


def aggiungi_fase_area(area, fase):
    if not area or not fase:
        return False
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO fasi_area (area, fase) VALUES (?, ?)", (area, fase.strip()))
        conn.commit()
    return True


def rimuovi_fase_area(area, fase):
    if not area or not fase:
        return False
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM fasi_area WHERE area=? AND fase=?", (area, fase))
        conn.commit()
    return True


def render_gestione_fasi_area(default_area=None):
    st.subheader("🧩 Gestione fasi per area")
    aree = [area for area in get_area_names() if area != "Tutte le aree"]
    if not aree:
        st.info("Nessuna area disponibile da configurare.")
        return
    selected_area = st.selectbox("Area", options=aree, index=0 if default_area not in aree else aree.index(default_area), key="fase_area_select")
    fasi_esistenti = get_fasi_area(area=selected_area, posizione="")
    new_phase = st.text_input("Nuova fase", key=f"new_phase_{selected_area}")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Aggiungi fase", key=f"add_phase_{selected_area}"):
            if aggiungi_fase_area(selected_area, new_phase):
                st.success(f"Fase aggiunta per {selected_area}.")
                st.rerun()
    with col2:
        if fasi_esistenti:
            phase_to_remove = st.selectbox("Fase da rimuovere", options=fasi_esistenti, key=f"phase_remove_{selected_area}")
            if st.button("Rimuovi fase", key=f"remove_phase_{selected_area}"):
                if rimuovi_fase_area(selected_area, phase_to_remove):
                    st.success(f"Fase rimossa da {selected_area}.")
                    st.rerun()
        else:
            st.info("Nessuna fase configurata per questa area.")

# --- FUNZIONI COMMESSE E FASI PER COMMESSA ---
# Le commesse le crea solo l'admin. Le fasi al loro interno sono sempre legate a
# UNA commessa e UNA area con delle ore stimate obbligatorie: l'admin può gestirle
# per qualsiasi area, i responsabili solo per la propria. Sono queste ore stimate
# la base del calcolo di produttività (ore stimate / ore effettive lavorate).

def get_commesse_names():
    """Elenco di tutte le commesse esistenti, in ordine alfabetico."""
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT nome FROM commesse ORDER BY nome")
        return [row[0] for row in c.fetchall()]

def get_commesse_dettaglio():
    """Tutte le commesse con la loro anagrafica completa, per la vista d'insieme dell'admin."""
    with db_connect() as conn:
        return pd.read_sql(
            "SELECT nome AS Nome, cliente AS Cliente, tipologia_impianto AS 'Tipologia impianto', "
            "anno_produzione AS 'Anno produzione', paese AS Paese, descrizione AS Descrizione "
            "FROM commesse ORDER BY nome", conn)

def crea_commessa(nome, descrizione, creata_da, cliente="", tipologia_impianto="", anno_produzione=None, paese=""):
    """Crea una nuova commessa (solo admin), con la sua anagrafica (cliente, tipologia
    impianto, anno di produzione, paese di installazione). Restituisce (ok, messaggio_errore)."""
    nome = (nome or "").strip()
    if not nome:
        return False, "Il nome della commessa è obbligatorio."
    with db_connect() as conn:
        c = conn.cursor()
        try:
            c.execute("""INSERT INTO commesse (nome, descrizione, cliente, tipologia_impianto, anno_produzione, paese, creata_da, data_creazione)
                         VALUES (?,?,?,?,?,?,?,?)""",
                      (nome, (descrizione or "").strip(), (cliente or "").strip(), (tipologia_impianto or "").strip(),
                       int(anno_produzione) if anno_produzione else None, (paese or "").strip(),
                       creata_da, datetime.date.today().isoformat()))
            conn.commit()
            return True, ""
        except db_error_classes("IntegrityError"):
            return False, "Esiste già una commessa con questo nome."

def elimina_commessa(nome):
    """Elimina una commessa e tutte le fasi (di ogni area) configurate al suo interno."""
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM fasi_commessa WHERE commessa=?", (nome,))
        c.execute("DELETE FROM commesse WHERE nome=?", (nome,))
        conn.commit()

# --- FUNZIONI TEMPLATE FASI (MACCHINE STANDARD) ---
# Una "tipologia impianto" con almeno una riga in template_fasi è considerata
# "standard": le sue fasi/ore per reparto si possono copiare automaticamente su
# una nuova commessa invece di inserirle a mano. Le tipologie senza template
# restano "su misura": le fasi si aggiungono manualmente come prima.

def get_tipologie_standard():
    """Elenco delle tipologie di impianto che hanno un template (almeno una fase configurata)."""
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT DISTINCT tipologia FROM template_fasi ORDER BY tipologia")
        return [row[0] for row in c.fetchall()]

def get_template_fasi(tipologia, area=None):
    """Fasi del template di una tipologia (eventualmente filtrate per area), con ore stimate.
    Restituisce tuple (id, fase, area, ore_stimate)."""
    with db_connect() as conn:
        c = conn.cursor()
        if area and area != "Tutte le aree":
            c.execute("SELECT id, fase, area, ore_stimate FROM template_fasi WHERE tipologia=? AND area=? ORDER BY fase", (tipologia, area))
        else:
            c.execute("SELECT id, fase, area, ore_stimate FROM template_fasi WHERE tipologia=? ORDER BY area, fase", (tipologia,))
        return c.fetchall()

def aggiungi_template_fase(tipologia, area, fase, ore_stimate, creata_da):
    """Aggiunge una fase al template di una tipologia, per una specifica area, con ore
    stimate obbligatorie (> 0). Restituisce (ok, messaggio_errore)."""
    tipologia = (tipologia or "").strip()
    area = (area or "").strip()
    fase = (fase or "").strip()
    if not tipologia or not area or not fase:
        return False, "Tipologia, area e fase sono tutte obbligatorie."
    try:
        ore_stimate = float(ore_stimate)
    except (TypeError, ValueError):
        return False, "Le ore stimate sono obbligatorie e devono essere un numero."
    if ore_stimate <= 0:
        return False, "Le ore stimate sono obbligatorie e devono essere maggiori di zero."
    with db_connect() as conn:
        c = conn.cursor()
        try:
            c.execute("INSERT INTO template_fasi (tipologia, area, fase, ore_stimate, creata_da, data_creazione) VALUES (?,?,?,?,?,?)",
                      (tipologia, area, fase, ore_stimate, creata_da, datetime.date.today().isoformat()))
            conn.commit()
            return True, ""
        except db_error_classes("IntegrityError"):
            return False, "Questa fase esiste già nel template di questa tipologia/area."

def aggiorna_ore_template_fase(template_id, ore_stimate):
    try:
        ore_stimate = float(ore_stimate)
    except (TypeError, ValueError):
        return False, "Le ore stimate devono essere un numero."
    if ore_stimate <= 0:
        return False, "Le ore stimate devono essere maggiori di zero."
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("UPDATE template_fasi SET ore_stimate=? WHERE id=?", (ore_stimate, template_id))
        conn.commit()
    return True, ""

def elimina_template_fase(template_id):
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM template_fasi WHERE id=?", (template_id,))
        conn.commit()

def applica_template_a_commessa(tipologia, commessa, creata_da):
    """Copia tutte le fasi del template di una tipologia (per ogni area configurata)
    sulla commessa indicata. Non sovrascrive fasi già esistenti sulla commessa (usa
    INSERT OR IGNORE). Restituisce il numero di fasi effettivamente copiate."""
    righe_template = get_template_fasi(tipologia)
    if not righe_template:
        return 0
    copiate = 0
    with db_connect() as conn:
        c = conn.cursor()
        for _id, fase, area, ore_stimate in righe_template:
            c.execute("""INSERT OR IGNORE INTO fasi_commessa (commessa, area, fase, ore_stimate, creata_da, data_creazione)
                         VALUES (?,?,?,?,?,?)""",
                      (commessa, area, fase, ore_stimate, creata_da, datetime.date.today().isoformat()))
            if c.rowcount:
                copiate += 1
        conn.commit()
    return copiate

def render_gestione_template_fasi(scope_area=None, attore=""):
    """UI di gestione dei template fasi/ore per tipologia di impianto (macchine standard).
    Se scope_area è impostata (caso responsabile), l'area è fissa: il responsabile può
    definire il template solo per il proprio reparto. L'admin può scegliere qualsiasi area."""
    st.subheader("🏭 Template fasi per macchine standard")
    st.caption("Definisci qui le fasi/ore tipiche di una tipologia di impianto: quando si crea una nuova commessa di questa tipologia, si possono copiare automaticamente invece di inserirle a mano.")

    tipologie_esistenti = get_tipologie_standard()
    modalita_tipologia = st.radio("Tipologia", ["Esistente", "Nuova"], horizontal=True, key=f"template_tipologia_mode_{scope_area}") if tipologie_esistenti else "Nuova"
    if modalita_tipologia == "Esistente" and tipologie_esistenti:
        tipologia_sel = st.selectbox("Seleziona tipologia", options=tipologie_esistenti, key=f"template_tipologia_select_{scope_area}")
    else:
        tipologia_sel = st.text_input("Nome nuova tipologia", key=f"template_tipologia_new_{scope_area}").strip()

    if not tipologia_sel:
        st.info("Indica una tipologia per gestirne il template.")
        return

    if scope_area:
        area_sel = scope_area
        st.caption(f"Stai gestendo il template per il reparto **{area_sel}**.")
    else:
        aree_disponibili = [a for a in get_area_names() if a != "Tutte le aree"]
        if not aree_disponibili:
            st.info("Nessuna area disponibile: crea prima un utente con un'area.")
            return
        area_sel = st.selectbox("Area", options=aree_disponibili, key=f"template_area_select_{scope_area}")

    fasi_template = get_template_fasi(tipologia_sel, area=area_sel)
    if fasi_template:
        st.dataframe(pd.DataFrame(fasi_template, columns=["ID", "Fase", "Area", "Ore stimate"]), use_container_width=True)
    else:
        st.info("Nessuna fase nel template per questa combinazione tipologia/area.")

    col_add, col_edit = st.columns(2)
    with col_add:
        st.markdown("**Aggiungi fase al template**")
        nuova_fase_t = st.text_input("Nome fase", key=f"nuova_fase_template_{tipologia_sel}_{area_sel}")
        nuove_ore_t = st.number_input("Ore stimate", min_value=0.0, step=0.5, key=f"nuove_ore_template_{tipologia_sel}_{area_sel}")
        if st.button("➕ Aggiungi al template", key=f"btn_add_template_{tipologia_sel}_{area_sel}"):
            ok, errore = aggiungi_template_fase(tipologia_sel, area_sel, nuova_fase_t, nuove_ore_t, attore)
            if ok:
                st.success(f"Fase '{nuova_fase_t.strip()}' aggiunta al template.")
                st.rerun()
            else:
                st.error(errore)

    with col_edit:
        if fasi_template:
            st.markdown("**Modifica / elimina fase del template**")
            opzioni_template = {f"{row[1]} ({row[3]:g}h)": row[0] for row in fasi_template}
            fase_template_label = st.selectbox("Fase", options=list(opzioni_template.keys()), key=f"template_modifica_{tipologia_sel}_{area_sel}")
            template_id_sel = opzioni_template[fase_template_label]
            nuove_ore_modifica_t = st.number_input("Nuove ore stimate", min_value=0.0, step=0.5, key=f"ore_modifica_template_{template_id_sel}")
            col_mod1, col_mod2 = st.columns(2)
            with col_mod1:
                if st.button("💾 Aggiorna ore", key=f"btn_update_template_{template_id_sel}"):
                    ok, errore = aggiorna_ore_template_fase(template_id_sel, nuove_ore_modifica_t)
                    if ok:
                        st.success("Ore stimate aggiornate.")
                        st.rerun()
                    else:
                        st.error(errore)
            with col_mod2:
                if st.button("🗑️ Elimina dal template", key=f"btn_delete_template_{template_id_sel}"):
                    elimina_template_fase(template_id_sel)
                    st.success("Fase eliminata dal template.")
                    st.rerun()

def get_fasi_commessa(commessa, area=None):
    """Fasi configurate per una commessa (eventualmente filtrate per area), con ore stimate.
    Restituisce tuple (id, fase, area, ore_stimate)."""
    with db_connect() as conn:
        c = conn.cursor()
        if area and area != "Tutte le aree":
            c.execute("SELECT id, fase, area, ore_stimate FROM fasi_commessa WHERE commessa=? AND area=? ORDER BY fase", (commessa, area))
        else:
            c.execute("SELECT id, fase, area, ore_stimate FROM fasi_commessa WHERE commessa=? ORDER BY area, fase", (commessa,))
        return c.fetchall()

def get_fasi_per_commessa_e_area(commessa, area):
    """Solo i nomi delle fasi disponibili per la tendina di timbratura (Inizio fase)."""
    if not commessa or not area:
        return []
    return [row[1] for row in get_fasi_commessa(commessa, area=area)]

def aggiungi_fase_commessa(commessa, area, fase, ore_stimate, creata_da):
    """Aggiunge una fase a una commessa per una specifica area, con ore stimate
    obbligatorie (> 0). Restituisce (ok, messaggio_errore)."""
    commessa = (commessa or "").strip()
    area = (area or "").strip()
    fase = (fase or "").strip()
    if not commessa or not area or not fase:
        return False, "Commessa, area e fase sono tutte obbligatorie."
    try:
        ore_stimate = float(ore_stimate)
    except (TypeError, ValueError):
        return False, "Le ore stimate sono obbligatorie e devono essere un numero."
    if ore_stimate <= 0:
        return False, "Le ore stimate sono obbligatorie e devono essere maggiori di zero."
    with db_connect() as conn:
        c = conn.cursor()
        try:
            c.execute("INSERT INTO fasi_commessa (commessa, area, fase, ore_stimate, creata_da, data_creazione) VALUES (?,?,?,?,?,?)",
                      (commessa, area, fase, ore_stimate, creata_da, datetime.date.today().isoformat()))
            conn.commit()
            return True, ""
        except db_error_classes("IntegrityError"):
            return False, "Questa fase esiste già per questa commessa e area."

def aggiorna_ore_stimate_fase(fase_id, ore_stimate):
    """Modifica le ore stimate di una fase già esistente. Restituisce (ok, messaggio_errore)."""
    try:
        ore_stimate = float(ore_stimate)
    except (TypeError, ValueError):
        return False, "Le ore stimate devono essere un numero."
    if ore_stimate <= 0:
        return False, "Le ore stimate devono essere maggiori di zero."
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("UPDATE fasi_commessa SET ore_stimate=? WHERE id=?", (ore_stimate, fase_id))
        conn.commit()
    return True, ""

def elimina_fase_commessa(fase_id):
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM fasi_commessa WHERE id=?", (fase_id,))
        conn.commit()

def render_gestione_fasi_commessa(scope_area=None, allow_create_commessa=False, attore=""):
    """UI di gestione fasi/ore-stimate per commessa. Se scope_area è impostata
    (caso responsabile), l'area è fissa e non modificabile: il responsabile può
    gestire fasi solo per il proprio reparto. Se allow_create_commessa è True
    (caso admin), consente anche di creare/eliminare commesse e di scegliere
    qualsiasi area."""
    if allow_create_commessa:
        st.subheader("🏗️ Crea nuova commessa")
        nuovo_nome = st.text_input("Nome commessa", key="nuova_commessa_nome")
        nuovo_cliente = st.text_input("Cliente", key="nuova_commessa_cliente")

        tipologie_esistenti_cc = get_tipologie_standard()
        modalita_tipologia_cc = st.radio(
            "Tipologia impianto", ["Esistente (standard)", "Nuova / su misura"],
            horizontal=True, key="nuova_commessa_tipologia_mode"
        ) if tipologie_esistenti_cc else "Nuova / su misura"
        if modalita_tipologia_cc == "Esistente (standard)" and tipologie_esistenti_cc:
            nuova_tipologia = st.selectbox("Seleziona tipologia", options=tipologie_esistenti_cc, key="nuova_commessa_tipologia_select")
        else:
            nuova_tipologia = st.text_input("Nome tipologia impianto", key="nuova_commessa_tipologia_new").strip()

        col_anno, col_paese = st.columns(2)
        with col_anno:
            nuovo_anno = st.number_input("Anno di produzione", min_value=2000, max_value=datetime.date.today().year + 5, value=datetime.date.today().year, step=1, key="nuova_commessa_anno")
        with col_paese:
            nuovo_paese = st.text_input("Paese di installazione", key="nuova_commessa_paese")

        nuova_descrizione = st.text_area("Descrizione (opzionale)", key="nuova_commessa_descrizione")

        ha_template_cc = bool(nuova_tipologia) and nuova_tipologia in tipologie_esistenti_cc
        applica_template_cc = st.checkbox(
            f"Applica alla creazione il template fasi standard di '{nuova_tipologia}'" if ha_template_cc else "Applica template fasi standard (nessun template trovato per questa tipologia)",
            value=ha_template_cc, disabled=not ha_template_cc, key="nuova_commessa_applica_template"
        )

        if st.button("Crea commessa", key="btn_crea_commessa"):
            ok, errore = crea_commessa(
                nuovo_nome, nuova_descrizione, attore,
                cliente=nuovo_cliente, tipologia_impianto=nuova_tipologia, anno_produzione=nuovo_anno, paese=nuovo_paese
            )
            if ok:
                nome_creata = nuovo_nome.strip()
                st.success(f"Commessa '{nome_creata}' creata.")
                if applica_template_cc and ha_template_cc:
                    n_copiate = applica_template_a_commessa(nuova_tipologia, nome_creata, attore)
                    if n_copiate:
                        st.success(f"Applicate {n_copiate} fasi dal template '{nuova_tipologia}'.")
                    else:
                        st.info("Nessuna fase copiata dal template (era vuoto).")
                st.rerun()
            else:
                st.error(errore)
        st.markdown("---")

        st.subheader("📋 Elenco commesse")
        commesse_dettaglio = get_commesse_dettaglio()
        if commesse_dettaglio.empty:
            st.info("Nessuna commessa ancora creata.")
        else:
            st.dataframe(commesse_dettaglio, use_container_width=True)
        st.markdown("---")

    commesse = get_commesse_names()
    st.subheader("🧩 Fasi e ore stimate per commessa")
    if not commesse:
        st.info("Nessuna commessa ancora creata." if not allow_create_commessa else "Crea prima una commessa qui sopra.")
        return

    commessa_sel = st.selectbox("Commessa", options=commesse, key="fase_commessa_select")

    if allow_create_commessa:
        aree_disponibili = [a for a in get_area_names() if a != "Tutte le aree"]
        if not aree_disponibili:
            st.info("Nessuna area disponibile: crea prima un utente con un'area.")
            return
        area_sel = st.selectbox("Area", options=aree_disponibili, key="fase_commessa_area_select")
    else:
        area_sel = scope_area
        st.caption(f"Stai gestendo le fasi del reparto **{area_sel}** per questa commessa.")

    fasi_esistenti = get_fasi_commessa(commessa_sel, area=area_sel)
    if fasi_esistenti:
        st.dataframe(pd.DataFrame(fasi_esistenti, columns=["ID", "Fase", "Area", "Ore stimate"]), use_container_width=True)
    else:
        st.info("Nessuna fase configurata per questa combinazione commessa/area.")

    col_add, col_edit = st.columns(2)
    with col_add:
        st.markdown("**Aggiungi fase**")
        nuova_fase = st.text_input("Nome fase", key=f"nuova_fase_{commessa_sel}_{area_sel}")
        nuove_ore = st.number_input("Ore stimate", min_value=0.0, step=0.5, key=f"nuove_ore_{commessa_sel}_{area_sel}")
        if st.button("➕ Aggiungi fase", key=f"btn_add_fase_{commessa_sel}_{area_sel}"):
            ok, errore = aggiungi_fase_commessa(commessa_sel, area_sel, nuova_fase, nuove_ore, attore)
            if ok:
                st.success(f"Fase '{nuova_fase.strip()}' aggiunta.")
                st.rerun()
            else:
                st.error(errore)

    with col_edit:
        if fasi_esistenti:
            st.markdown("**Modifica / elimina fase**")
            opzioni_fase = {f"{row[1]} ({row[3]:g}h)": row[0] for row in fasi_esistenti}
            fase_scelta_label = st.selectbox("Fase", options=list(opzioni_fase.keys()), key=f"fase_modifica_{commessa_sel}_{area_sel}")
            fase_id_scelta = opzioni_fase[fase_scelta_label]
            nuove_ore_modifica = st.number_input("Nuove ore stimate", min_value=0.0, step=0.5, key=f"ore_modifica_{fase_id_scelta}")
            col_mod1, col_mod2 = st.columns(2)
            with col_mod1:
                if st.button("💾 Aggiorna ore", key=f"btn_update_fase_{fase_id_scelta}"):
                    ok, errore = aggiorna_ore_stimate_fase(fase_id_scelta, nuove_ore_modifica)
                    if ok:
                        st.success("Ore stimate aggiornate.")
                        st.rerun()
                    else:
                        st.error(errore)
            with col_mod2:
                if st.button("🗑️ Elimina fase", key=f"btn_delete_fase_{fase_id_scelta}"):
                    elimina_fase_commessa(fase_id_scelta)
                    st.success("Fase eliminata.")
                    st.rerun()

    if allow_create_commessa:
        st.markdown("---")
        st.subheader("🗑️ Elimina commessa")
        st.caption("Elimina la commessa e tutte le fasi configurate per ogni area al suo interno.")
        commessa_da_eliminare = st.selectbox("Commessa da eliminare", options=commesse, key="commessa_delete_select")
        if st.button("Elimina definitivamente la commessa"):
            elimina_commessa(commessa_da_eliminare)
            st.success(f"Commessa '{commessa_da_eliminare}' eliminata.")
            st.rerun()

# --- FUNZIONI DATABASE TIMBRATURE/RICHIESTE ---

@st.cache_data(ttl=30)
def carica_dati_db():
    """Carica tutte le timbrature. È la lettura dal database più pesante e più
    frequente di tutta l'app (viene fatta ad ogni pagina, per ogni utente): con un
    database locale non aveva costi, ma con un database remoto come Turso rifarla ad
    ogni singolo clic (anche solo per cambiare una tendina) è molto lento. La cache la
    tiene in memoria per 30 secondi invece di rileggerla ogni volta; le funzioni che
    scrivono nuove timbrature (registra_orario, approva_rettifica) chiamano
    carica_dati_db.clear() subito dopo, cosi' chi ha appena timbrato vede subito il
    proprio aggiornamento invece di aspettare la scadenza della cache."""
    with db_connect() as conn:
        df = pd.read_sql("SELECT Data, Dipendente, Ora, Azione, Luogo, Dettaglio_Trasferta, Commessa, Fase, Dettaglio_Fase, Ruolo FROM timbrature", conn)
    if df.empty:
        return pd.DataFrame(columns=EXPECTED_COLS)
    df = df.rename(columns={"Dettaglio_Trasferta": "Dettaglio Trasferta", "Dettaglio_Fase": "Dettaglio Fase"})
    return df

def registra_orario(nome_dipendente, tipo_azione, luogo_lavoro, dettaglio, commessa="", fase="", dettaglio_fase="", ruolo=""):
    # Validazione timbratura
    valida, messaggio, stato = validate_timbratura(nome_dipendente, tipo_azione)

    if not valida:
        st.error(messaggio)
        return False

    # Se si registra un'Uscita mentre è ancora aperta una fase (l'ultima azione
    # valida è "Inizio fase" senza il relativo "Fine fase"), chiudila automaticamente
    # all'orario dell'uscita: altrimenti la fase resterebbe aperta indefinitamente e
    # le sue ore non verrebbero mai conteggiate nelle statistiche di produttività.
    fase_da_chiudere = get_fase_aperta(nome_dipendente) if tipo_azione == "Uscita" else None

    # Orario con fuso corretto
    ora_attuale = get_current_datetime()
    data_str = ora_attuale.strftime("%Y-%m-%d")
    ora_str = ora_attuale.strftime("%H:%M:%S")

    if luogo_lavoro != "Trasferta":
        dettaglio = "-"
    if tipo_azione not in ["Inizio fase", "Fine fase"]:
        commessa = ""; fase = ""; dettaglio_fase = ""

    with db_connect() as conn:
        c = conn.cursor()
        if fase_da_chiudere:
            commessa_chiusura, fase_chiusura = fase_da_chiudere
            c.execute('''INSERT INTO timbrature (Data, Dipendente, Ora, Azione, Luogo, Dettaglio_Trasferta, Commessa, Fase, Dettaglio_Fase, Ruolo, Stato)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                      (data_str, nome_dipendente, ora_str, "Fine fase", luogo_lavoro, dettaglio, commessa_chiusura, fase_chiusura, "", ruolo, "Valida"))
        c.execute('''INSERT INTO timbrature (Data, Dipendente, Ora, Azione, Luogo, Dettaglio_Trasferta, Commessa, Fase, Dettaglio_Fase, Ruolo, Stato)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                  (data_str, nome_dipendente, ora_str, tipo_azione, luogo_lavoro, dettaglio, commessa, fase, dettaglio_fase, ruolo, stato))
        conn.commit()
    carica_dati_db.clear()  # la nuova timbratura deve essere visibile subito, non solo dopo la cache

    if fase_da_chiudere:
        st.info(f"🧩 La fase '{fase_da_chiudere[1]}' su '{fase_da_chiudere[0]}' era ancora aperta: chiusa automaticamente con l'uscita.")
    if tipo_azione in ["Inizio fase", "Fine fase"]:
        st.success(f"🧩 {tipo_azione} registrato per {nome_dipendente} - Fase: {fase or '-'} - Commessa: {commessa or '-'} - alle {ora_str}!")
    elif tipo_azione in ["Inizio Pausa", "Fine Pausa"]:
        st.success(f"☕ {tipo_azione} registrato per {nome_dipendente} - alle {ora_str}!")
    else:
        st.success(f"📌 {tipo_azione} registrato per {nome_dipendente} ({luogo_lavoro}) - Commessa: {commessa} - alle {ora_str}!")
    st.rerun()
    return True

def carica_richieste():
    with db_connect() as conn:
        df = pd.read_sql("SELECT * FROM richieste", conn)
    return df if not df.empty else pd.DataFrame(columns=REQUESTS_COLS)

def salva_richiesta(dipendente, tipo, data_inizio, data_fine, ore_permesso, motivo, ruolo_richiedente="user"):
    """Salva una richiesta con flusso di approvazione gerarchico:
    - Se ruolo_richiedente = 'user': approva il responsabile dell'area
    - Se ruolo_richiedente = 'responsabile': approva solo l'admin
    """
    request_id = str(int(datetime.datetime.now().timestamp() * 1000))
    ore_str = str(ore_permesso) if ore_permesso else ""
    oggi_str = datetime.date.today().isoformat()
    
    # Determina l'approvatore in base al ruolo di chi fa la richiesta
    if ruolo_richiedente == "responsabile":
        approvatore_richiesto = "admin"  # I responsabili sono approvati direttamente dall'admin
    else:
        # Gli utenti normali vengono approvati dal responsabile della loro area
        approvatore_richiesto = "admin"  # default
        with db_connect() as conn:
            c = conn.cursor()
            # Leggi l'area del dipendente
            c.execute("SELECT area FROM utenti WHERE nome = ?", (dipendente,))
            area_result = c.fetchone()
            
            if area_result and area_result[0]:
                dipendente_area = area_result[0]
                # Cerca il responsabile di quella area
                c.execute("SELECT nome FROM utenti WHERE role = 'responsabile' AND area = ?", (dipendente_area,))
                resp_result = c.fetchone()
                if resp_result:
                    approvatore_richiesto = resp_result[0]
    
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("""INSERT INTO richieste 
                     (ID, Dipendente, Tipo, Data_inizio, Data_fine, Ore_permesso, Motivo, Stato, Data_richiesta, Approvatore_Richiesto) 
                     VALUES (?,?,?,?,?,?,?,?,?,?)""",
                  (request_id, dipendente, tipo, data_inizio.isoformat(), data_fine.isoformat(), 
                   ore_str, motivo, "In attesa", oggi_str, approvatore_richiesto))
        conn.commit()
    
    # 📧 Invia email di notifica all'approvatore appropriato
    email_approvatore = approvatore_richiesto.replace(" ", ".").lower() + "@azienda.com"
    oggetto = f"📋 Nuova richiesta {tipo} da {dipendente}"
    corpo = f"""
Ciao,

{dipendente} ha inviato una nuova richiesta di {tipo.lower()}.

Dettagli:
- Dipendente: {dipendente}
- Tipo: {tipo}
- Data inizio: {data_inizio.isoformat()}
- Data fine: {data_fine.isoformat()}
- Motivo: {motivo}
- Ore (se permesso): {ore_str}

Accedi al sistema per approvarla o rifiutarla.

Cordiali saluti,
Sistema Timbrature
    """
    send_email(email_approvatore, oggetto, corpo)
    
    st.success("Richiesta inviata correttamente. In attesa di approvazione.")

def aggiorna_stato_richiesta(request_id, stato):
    with db_connect() as conn:
        c = conn.cursor()
        # Recupera dati richiesta per inviare email
        c.execute("SELECT Dipendente, Tipo, Data_inizio, Data_fine FROM richieste WHERE ID = ?", (str(request_id),))
        row = c.fetchone()
        
        # Aggiorna stato
        c.execute("UPDATE richieste SET Stato = ? WHERE ID = ?", (stato, str(request_id)))
        conn.commit()
    
    # 📧 Invia email di notifica al dipendente
    if row:
        dipendente, tipo_richiesta, data_inizio, data_fine = row
        
        # Recupera email del dipendente
        with db_connect() as conn:
            c = conn.cursor()
            c.execute("SELECT username FROM utenti WHERE nome = ?", (dipendente,))
            email_result = c.fetchone()
            email_dipendente = email_result[0] + "@azienda.com" if email_result else None
        
        if email_dipendente:
            emoji = "✅" if stato == "Approvato" else "❌"
            oggetto = f"{emoji} {tipo_richiesta} - Richiesta {stato.lower()}"
            corpo = f"""
Ciao {dipendente},

La tua richiesta di {tipo_richiesta.lower()} è stata {stato.lower()}.

Dettagli:
- Tipo: {tipo_richiesta}
- Dal: {data_inizio}
- Al: {data_fine}
- Stato: {stato}

Cordiali saluti,
Amministrazione Timbrature
            """
            send_email(email_dipendente, oggetto, corpo)

# --- FUNZIONI RETTIFICHE TIMBRATURE ---

def salva_richiesta_rettifica(dipendente, azione, data_prevista, ora_prevista, motivo):
    """Salva una richiesta di rettifica timbratura."""
    rettifica_id = str(int(datetime.datetime.now().timestamp() * 1000))
    oggi_str = datetime.date.today().isoformat()
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("""INSERT INTO rettifiche 
                     (ID, Dipendente, Azione, Data_prevista, Ora_prevista, Motivo, Stato, Data_richiesta)
                     VALUES (?,?,?,?,?,?,?,?)""",
                  (rettifica_id, dipendente, azione, data_prevista.isoformat(), ora_prevista, motivo, "In attesa", oggi_str))
        conn.commit()
    st.success("📝 Richiesta di rettifica inviata. Attendi l'approvazione dell'amministratore.")

def carica_rettifiche():
    """Carica tutte le richieste di rettifica."""
    with db_connect() as conn:
        df = pd.read_sql("SELECT * FROM rettifiche", conn)
    return df if not df.empty else pd.DataFrame(columns=["ID", "Dipendente", "Azione", "Data_prevista", "Ora_prevista", "Motivo", "Stato", "Data_richiesta", "Approvato_da"])

def approva_rettifica(rettifica_id, admin_username):
    """Approva una rettifica e registra la timbratura."""
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT Dipendente, Azione, Data_prevista, Ora_prevista FROM rettifiche WHERE ID = ?", (rettifica_id,))
        row = c.fetchone()
        if not row:
            st.error("Rettifica non trovata.")
            return False
        
        dipendente, azione, data_prevista, ora_prevista = row
        
        # Registra la timbratura
        c.execute("""INSERT INTO timbrature 
                     (Data, Dipendente, Ora, Azione, Luogo, Dettaglio_Trasferta, Commessa, Fase, Dettaglio_Fase, Ruolo, Stato)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                  (data_prevista, dipendente, ora_prevista, azione, "Rettifica", "-", "", "", "", "", "Rettificata"))
        
        # Aggiorna lo stato della rettifica
        c.execute("UPDATE rettifiche SET Stato = ?, Approvato_da = ? WHERE ID = ?",
                  ("Approvato", admin_username, rettifica_id))
        conn.commit()
    carica_dati_db.clear()  # è stata aggiunta una nuova timbratura (quella rettificata)

    # 📧 Invia email di notifica al dipendente
    email_dipendente = dipendente.replace(" ", ".").lower() + "@azienda.com"
    oggetto = "✅ Rettifica timbratura approvata"
    corpo = f"""
Ciao {dipendente},

La tua richiesta di rettifica è stata approvata.

Dettagli:
- Azione: {azione}
- Data: {data_prevista}
- Ora: {ora_prevista}
- Approvato da: {admin_username}

La timbratura è stata registrata nel sistema.

Cordiali saluti,
Amministrazione Timbrature
    """
    send_email(email_dipendente, oggetto, corpo)
    
    st.success("✅ Rettifica approvata e timbratura registrata.")
    return True

def rifiuta_rettifica(rettifica_id):
    """Rifiuta una rettifica."""
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("UPDATE rettifiche SET Stato = ? WHERE ID = ?", ("Rifiutato", rettifica_id))
        conn.commit()
    st.success("❌ Rettifica rifiutata.")

# --- FUNZIONI TRASFERTE SERVICE E REPORT INTERVENTI ---
# Chi programma le trasferte: l'amministratore, oppure chiunque abbia l'area
# "Service" impostata sul proprio utente (indipendentemente dal ruolo). Chi compila
# il report dell'intervento (testo + foto): il dipendente stesso, per una propria
# trasferta già programmata. La vista "Disponibilità Team" (chi è in sede/trasferta/
# ferie) è visibile ad amministratore e responsabili di reparto.

def is_area_service(area):
    """Vero se l'area indicata è (senza distinguere maiuscole/minuscole o spazi) l'area
    Service, usata per sbloccare la programmazione trasferte a chiunque ne faccia parte."""
    return str(area or "").strip().lower() == "service"

MEZZI_TRASPORTO_DISPONIBILI = ["Auto", "Treno", "Aereo", "Auto a noleggio"]

def get_cliente_commessa(commessa):
    """Cliente associato a una commessa (l'anagrafica cliente si inserisce solo in
    'Gestione Commesse': qui si ricava, non si reinserisce a mano)."""
    if not commessa:
        return ""
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT cliente FROM commesse WHERE nome = ?", (commessa,))
        row = c.fetchone()
        return row[0] or "" if row else ""

def get_paese_commessa(commessa):
    """Paese di installazione di una commessa, usato solo come suggerimento di
    partenza per il campo 'Stato' della trasferta (resta comunque modificabile,
    perché la trasferta potrebbe non essere nel paese della commessa)."""
    if not commessa:
        return ""
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT paese FROM commesse WHERE nome = ?", (commessa,))
        row = c.fetchone()
        return row[0] or "" if row else ""

def valida_trasferta(dipendente, data_inizio, data_fine, commessa, mezzi_selezionati, dettaglio_auto, dettaglio_treno, dettaglio_aereo):
    """Validazione dati di una trasferta prima del salvataggio. Restituisce (ok, errore)."""
    if not dipendente:
        return False, "Seleziona un operatore."
    if not commessa:
        return False, "Seleziona la commessa su cui si baserà la trasferta (serve a determinare il cliente)."
    if data_fine < data_inizio:
        return False, "La data di fine trasferta non può essere precedente alla data di inizio."
    if not mezzi_selezionati:
        return False, "Seleziona almeno un mezzo di trasporto."
    if "Auto" in mezzi_selezionati and not str(dettaglio_auto or "").strip():
        return False, "Inserisci la targa dell'auto."
    if "Treno" in mezzi_selezionati and not str(dettaglio_treno or "").strip():
        return False, "Inserisci il numero del treno."
    if "Aereo" in mezzi_selezionati and not str(dettaglio_aereo or "").strip():
        return False, "Inserisci il numero del volo."
    return True, ""

def crea_trasferta(dipendente, data_inizio, data_fine, commessa, stato, indirizzo, albergo, mezzi_selezionati,
                    dettaglio_auto, auto_propria, dettaglio_treno, dettaglio_aereo, dettaglio_auto_noleggio,
                    note, creata_da):
    """Programma una nuova trasferta per un operatore, su una commessa (da cui si
    ricava il cliente), con uno o più mezzi di trasporto insieme (es. aereo per
    arrivare + auto a noleggio per muoversi sul posto). Restituisce (ok, errore)."""
    ok, errore = valida_trasferta(dipendente, data_inizio, data_fine, commessa, mezzi_selezionati,
                                   dettaglio_auto, dettaglio_treno, dettaglio_aereo)
    if not ok:
        return False, errore
    cliente = get_cliente_commessa(commessa)
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("""INSERT INTO trasferte
                     (dipendente, data_inizio, data_fine, commessa, cliente, stato, indirizzo, albergo, mezzi,
                      dettaglio_auto, auto_propria, dettaglio_treno, dettaglio_aereo, dettaglio_auto_noleggio,
                      note, creata_da, data_creazione)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (dipendente, data_inizio.isoformat(), data_fine.isoformat(), commessa, cliente,
                   str(stato or "").strip(), str(indirizzo or "").strip(), str(albergo or "").strip(),
                   ",".join(mezzi_selezionati),
                   str(dettaglio_auto or "").strip() if "Auto" in mezzi_selezionati else "",
                   1 if (auto_propria and "Auto" in mezzi_selezionati) else 0,
                   str(dettaglio_treno or "").strip() if "Treno" in mezzi_selezionati else "",
                   str(dettaglio_aereo or "").strip() if "Aereo" in mezzi_selezionati else "",
                   str(dettaglio_auto_noleggio or "").strip() if "Auto a noleggio" in mezzi_selezionati else "",
                   str(note or "").strip(), creata_da, datetime.date.today().isoformat()))
        conn.commit()
    return True, ""

def get_trasferte_dettaglio():
    """Tutte le trasferte programmate, più recenti prima, per la tabella riassuntiva
    di chi gestisce l'area Service."""
    with db_connect() as conn:
        df = pd.read_sql("""SELECT id AS ID, dipendente AS Dipendente, data_inizio AS Dal, data_fine AS Al,
                                    commessa AS Commessa, cliente AS Cliente, stato AS Stato, indirizzo AS Indirizzo,
                                    albergo AS Albergo, mezzi AS Mezzi, dettaglio_auto AS Targa,
                                    CASE WHEN auto_propria=1 THEN 'Sì' ELSE 'No' END AS 'Auto propria',
                                    dettaglio_treno AS 'Numero treno', dettaglio_aereo AS 'Numero volo',
                                    dettaglio_auto_noleggio AS 'Note noleggio', note AS Note, creata_da AS 'Programmata da'
                             FROM trasferte ORDER BY data_inizio DESC, id DESC""", conn)
    return df

def get_trasferte_dipendente(dipendente):
    """Trasferte di un singolo dipendente (per la tendina del report intervento),
    più recenti prima."""
    with db_connect() as conn:
        df = pd.read_sql("""SELECT id AS ID, data_inizio AS Dal, data_fine AS Al, cliente AS Cliente, stato AS Stato
                             FROM trasferte WHERE dipendente = ? ORDER BY data_inizio DESC, id DESC""",
                          conn, params=(dipendente,))
    return df

def elimina_trasferta(trasferta_id):
    """Elimina una trasferta programmata, ma solo se non ha già un report intervento
    collegato (altrimenti chiede di eliminare prima quello). Restituisce (ok, errore)."""
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM report_interventi WHERE trasferta_id = ?", (trasferta_id,))
        if c.fetchone()[0] > 0:
            return False, "Questa trasferta ha già un report intervento collegato: elimina prima quello."
        c.execute("DELETE FROM trasferte WHERE id = ?", (trasferta_id,))
        conn.commit()
    return True, ""

def comprimi_immagine_upload(uploaded_file, max_dimensione=1600, qualita=80):
    """Ridimensiona e comprime una foto caricata (JPEG) prima di salvarla nel
    database, per non appesantirlo troppo: soprattutto su un database esterno come
    Turso, foto non compresse potrebbero esaurire rapidamente lo spazio disponibile."""
    if not PIL_AVAILABLE:
        return uploaded_file.getvalue()
    immagine = PIL_Image.open(uploaded_file)
    if immagine.mode not in ("RGB",):
        immagine = immagine.convert("RGB")
    larghezza, altezza = immagine.size
    lato_massimo = max(larghezza, altezza)
    if lato_massimo > max_dimensione:
        fattore = max_dimensione / lato_massimo
        immagine = immagine.resize((int(larghezza * fattore), int(altezza * fattore)))
    buffer_immagine = BytesIO()
    immagine.save(buffer_immagine, format="JPEG", quality=qualita)
    return buffer_immagine.getvalue()

def calcola_ore_lavorate_periodo(df, dipendente, data_inizio, data_fine):
    """Ore effettivamente lavorate da un dipendente tra data_inizio e data_fine
    (estremi inclusi), calcolate dalle sue timbrature reali (coppie Ingresso/Uscita)
    invece di farle inserire a mano nel report intervento."""
    giornaliero = compute_daily_work(df)
    if giornaliero.empty:
        return 0.0
    filtrato = giornaliero[(giornaliero["Dipendente"] == dipendente) &
                            (giornaliero["Data"] >= data_inizio) & (giornaliero["Data"] <= data_fine)]
    return round(float(filtrato["Ore_lavorate"].sum()), 2) if not filtrato.empty else 0.0

def salva_report_intervento(trasferta_id, dipendente, cliente_luogo, descrizione, ore_lavoro, commessa, fase, foto_caricate, data_intervento=None):
    """Salva il report di un intervento (testo + eventuali foto compresse) per una
    trasferta del dipendente. Le ore di lavoro sono calcolate dal chiamante dalle
    timbrature reali (vedi calcola_ore_lavorate_periodo), non inserite a mano.
    Restituisce (ok, errore)."""
    if not descrizione or not str(descrizione).strip():
        return False, "Inserisci una descrizione del lavoro svolto."
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("""INSERT INTO report_interventi
                     (trasferta_id, dipendente, cliente_luogo, descrizione, ore_lavoro, commessa, fase, data_intervento, data_creazione)
                     VALUES (?,?,?,?,?,?,?,?,?)""",
                  (trasferta_id, dipendente, str(cliente_luogo or "").strip(), str(descrizione).strip(),
                   float(ore_lavoro) if ore_lavoro else 0.0, commessa, fase,
                   data_intervento.isoformat() if hasattr(data_intervento, "isoformat") else data_intervento,
                   datetime.date.today().isoformat()))
        report_id = c.lastrowid
        oggi_str = datetime.date.today().isoformat()
        for foto in (foto_caricate or []):
            dati_foto = comprimi_immagine_upload(foto)
            c.execute("INSERT INTO report_interventi_foto (report_id, nome_file, foto, data_caricamento) VALUES (?,?,?,?)",
                      (report_id, foto.name, dati_foto, oggi_str))
        conn.commit()
    return True, ""

def get_report_interventi_dettaglio(dipendente=None, area=None):
    """Report interventi (con dati della trasferta collegata e numero di foto
    allegate), filtrabili per dipendente o per area."""
    query = """SELECT r.id AS ID, r.dipendente AS Dipendente, t.data_inizio AS 'Trasferta dal', t.data_fine AS 'Trasferta al',
                      r.cliente_luogo AS 'Cliente/Luogo', r.descrizione AS Descrizione, r.data_intervento AS 'Giorno intervento',
                      r.ore_lavoro AS 'Ore lavoro', r.commessa AS Commessa, r.fase AS Fase, r.data_creazione AS 'Data report',
                      (SELECT COUNT(*) FROM report_interventi_foto f WHERE f.report_id = r.id) AS 'N. foto'
               FROM report_interventi r LEFT JOIN trasferte t ON r.trasferta_id = t.id"""
    condizioni, parametri = [], []
    if dipendente:
        condizioni.append("r.dipendente = ?")
        parametri.append(dipendente)
    if area and area != "Tutte le aree":
        mappa_aree = get_users_area_map()
        dipendenti_area = [nome for nome, area_nome in mappa_aree.items() if area_nome == area]
        if not dipendenti_area:
            return pd.DataFrame(columns=["ID", "Dipendente", "Trasferta dal", "Trasferta al", "Cliente/Luogo",
                                          "Descrizione", "Giorno intervento", "Ore lavoro", "Commessa", "Fase", "Data report", "N. foto"])
        condizioni.append(f"r.dipendente IN ({','.join('?' for _ in dipendenti_area)})")
        parametri.extend(dipendenti_area)
    if condizioni:
        query += " WHERE " + " AND ".join(condizioni)
    query += " ORDER BY r.data_creazione DESC, r.id DESC"
    with db_connect() as conn:
        df = pd.read_sql(query, conn, params=tuple(parametri))
    return df

def get_foto_report(report_id):
    """Elenco (nome_file, dati_foto) delle foto allegate a un report intervento."""
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT nome_file, foto FROM report_interventi_foto WHERE report_id = ?", (report_id,))
        return c.fetchall()

def compute_disponibilita_giornaliera(data_riferimento, area_filter=None):
    """Per il giorno indicato, restituisce lo stato di ogni dipendente: 'Ferie/
    Permesso' se ha una richiesta approvata che copre quel giorno, 'In trasferta' se
    ha una trasferta programmata che lo copre, altrimenti 'In sede'. È una vista di
    pianificazione basata sulle richieste approvate e sulle trasferte programmate,
    NON sulle timbrature effettive del giorno."""
    area_scelta = area_filter or "Tutte le aree"
    dipendenti = get_users_in_area(area_scelta)
    if not dipendenti:
        return pd.DataFrame(columns=["Dipendente", "Area", "Stato", "Dettaglio"])

    mappa_aree = get_users_area_map()
    data_iso = data_riferimento.isoformat()

    with db_connect() as conn:
        c = conn.cursor()
        c.execute("""SELECT Dipendente, Tipo FROM richieste WHERE Stato = 'Approvato'
                     AND Data_inizio <= ? AND Data_fine >= ?""", (data_iso, data_iso))
        ferie_permessi = {row[0]: row[1] for row in c.fetchall()}
        c.execute("""SELECT dipendente, cliente, stato FROM trasferte
                     WHERE data_inizio <= ? AND data_fine >= ?""", (data_iso, data_iso))
        trasferte_in_corso = {}
        for nome_dip, cliente_trasf, stato_trasf in c.fetchall():
            descrizione_trasf = cliente_trasf or ""
            if stato_trasf:
                descrizione_trasf = f"{descrizione_trasf} ({stato_trasf})" if descrizione_trasf else stato_trasf
            trasferte_in_corso[nome_dip] = descrizione_trasf

    righe = []
    for nome in dipendenti:
        if nome in ferie_permessi:
            righe.append({"Dipendente": nome, "Area": mappa_aree.get(nome, "Unknown"),
                          "Stato": ferie_permessi[nome], "Dettaglio": ""})
        elif nome in trasferte_in_corso:
            righe.append({"Dipendente": nome, "Area": mappa_aree.get(nome, "Unknown"),
                          "Stato": "In trasferta", "Dettaglio": trasferte_in_corso[nome] or ""})
        else:
            righe.append({"Dipendente": nome, "Area": mappa_aree.get(nome, "Unknown"),
                          "Stato": "In sede", "Dettaglio": ""})
    return pd.DataFrame(righe)

def render_gestione_trasferte_service(attore_nome, key_prefix):
    """Pagina di programmazione trasferte: scelta dell'operatore tra tutti i
    dipendenti, della commessa (da cui si ricava il cliente), del luogo di lavoro
    (stato/indirizzo, dato che l'azienda lavora in tutto il mondo), dell'albergo, e
    di uno o più mezzi di trasporto insieme (es. aereo per arrivare + auto a
    noleggio per muoversi sul posto). Accessibile all'amministratore e a chiunque
    faccia parte dell'area Service."""
    st.subheader("🧳 Programmazione Trasferte (Service)")

    st.markdown("**Nuova trasferta**")
    operatori_disponibili = get_user_names()
    if not operatori_disponibili:
        st.warning("Nessun dipendente disponibile.")
        return
    operatore_scelto = st.selectbox("Operatore", operatori_disponibili, key=f"{key_prefix}_trasf_operatore")

    commesse_disponibili_trasf = get_commesse_names()
    if not commesse_disponibili_trasf:
        st.warning("Nessuna commessa configurata: creane una in 'Gestione Commesse' prima di programmare una trasferta (serve a determinare il cliente).")
        return
    commessa_scelta_trasf = st.selectbox("Commessa (determina il cliente)", commesse_disponibili_trasf, key=f"{key_prefix}_trasf_commessa")
    cliente_derivato_trasf = get_cliente_commessa(commessa_scelta_trasf)
    st.text_input("Cliente (ricavato dalla commessa)", value=cliente_derivato_trasf or "(nessun cliente impostato per questa commessa)",
                  disabled=True, key=f"{key_prefix}_trasf_cliente_display")

    col_data1, col_data2 = st.columns(2)
    with col_data1:
        data_inizio_trasf = st.date_input("Data inizio trasferta", value=datetime.date.today(), key=f"{key_prefix}_trasf_inizio")
    with col_data2:
        data_fine_trasf = st.date_input("Data fine trasferta", value=datetime.date.today(), key=f"{key_prefix}_trasf_fine")

    st.markdown("**Luogo di lavoro e alloggio**")
    col_luogo1, col_luogo2 = st.columns(2)
    with col_luogo1:
        stato_trasf = st.text_input("Stato", value=get_paese_commessa(commessa_scelta_trasf) or "", key=f"{key_prefix}_trasf_stato")
    with col_luogo2:
        indirizzo_trasf = st.text_input("Indirizzo", key=f"{key_prefix}_trasf_indirizzo")
    albergo_trasf = st.text_input("Albergo (nome e/o indirizzo)", key=f"{key_prefix}_trasf_albergo")

    st.markdown("**Mezzi di trasporto**")
    st.caption("Puoi selezionarne più di uno: es. Aereo per arrivare e Auto a noleggio per muoversi una volta lì.")
    mezzi_scelti_trasf = st.multiselect("Seleziona uno o più mezzi", MEZZI_TRASPORTO_DISPONIBILI, key=f"{key_prefix}_trasf_mezzi")

    dettaglio_auto_trasf, auto_propria_trasf = "", False
    dettaglio_treno_trasf, dettaglio_aereo_trasf, dettaglio_noleggio_trasf = "", "", ""

    if "Auto" in mezzi_scelti_trasf:
        col_auto1, col_auto2 = st.columns(2)
        with col_auto1:
            dettaglio_auto_trasf = st.text_input("Targa", key=f"{key_prefix}_trasf_targa")
        with col_auto2:
            auto_propria_trasf = st.checkbox("Auto propria (non aziendale)", key=f"{key_prefix}_trasf_auto_propria")
    if "Treno" in mezzi_scelti_trasf:
        dettaglio_treno_trasf = st.text_input("Numero treno", key=f"{key_prefix}_trasf_treno")
    if "Aereo" in mezzi_scelti_trasf:
        dettaglio_aereo_trasf = st.text_input("Numero volo", key=f"{key_prefix}_trasf_volo")
    if "Auto a noleggio" in mezzi_scelti_trasf:
        dettaglio_noleggio_trasf = st.text_input("Note auto a noleggio (agenzia, prenotazione, ecc. - opzionale)", key=f"{key_prefix}_trasf_noleggio")

    note_trasf = st.text_area("Note (opzionale)", key=f"{key_prefix}_trasf_note")

    if st.button("💾 Programma trasferta", key=f"{key_prefix}_trasf_salva"):
        ok, errore = crea_trasferta(operatore_scelto, data_inizio_trasf, data_fine_trasf, commessa_scelta_trasf,
                                     stato_trasf, indirizzo_trasf, albergo_trasf, mezzi_scelti_trasf,
                                     dettaglio_auto_trasf, auto_propria_trasf, dettaglio_treno_trasf,
                                     dettaglio_aereo_trasf, dettaglio_noleggio_trasf, note_trasf, attore_nome)
        if ok:
            st.success(f"Trasferta programmata per {operatore_scelto}.")
            st.rerun()
        else:
            st.error(errore)

    st.markdown("---")
    st.markdown("**Trasferte programmate**")
    trasferte_df = get_trasferte_dettaglio()
    if trasferte_df.empty:
        st.info("Nessuna trasferta programmata.")
    else:
        st.dataframe(trasferte_df, use_container_width=True)

        st.markdown("**Elimina una trasferta**")
        opzioni_elimina = [f"#{id_} - {dip} ({dal} → {al}) - {cliente or 'nessun cliente'}" for id_, dip, dal, al, cliente in
                            zip(trasferte_df["ID"], trasferte_df["Dipendente"], trasferte_df["Dal"], trasferte_df["Al"], trasferte_df["Cliente"])]
        mappa_id_elimina = dict(zip(opzioni_elimina, trasferte_df["ID"].tolist()))
        if opzioni_elimina:
            scelta_elimina = st.selectbox("Trasferta da eliminare", opzioni_elimina, key=f"{key_prefix}_trasf_elimina_select")
            if st.button("🗑️ Elimina trasferta selezionata", key=f"{key_prefix}_trasf_elimina_btn"):
                ok, errore = elimina_trasferta(mappa_id_elimina[scelta_elimina])
                if ok:
                    st.success("Trasferta eliminata.")
                    st.rerun()
                else:
                    st.error(errore)

def render_report_intervento(dipendente_nome, df, key_prefix):
    """Il dipendente compila il report di un intervento svolto durante una propria
    trasferta già programmata (cliente/luogo, descrizione, commessa/fase) e può
    allegare una o più foto. Le ore di lavoro NON si inseriscono a mano: vengono
    calcolate automaticamente dalle sue timbrature reali (Ingresso/Uscita) del
    giorno dell'intervento scelto."""
    st.subheader("📷 Report Intervento")
    trasferte_dipendente = get_trasferte_dipendente(dipendente_nome)
    if trasferte_dipendente.empty:
        st.info("Non hai ancora trasferte programmate a cui associare un report. Contatta l'area Service.")
        return

    opzioni_trasferta = [
        f"{dal} → {al}" + (f" ({cliente}" + (f", {stato})" if stato else ")") if cliente else "")
        for dal, al, cliente, stato in zip(trasferte_dipendente["Dal"], trasferte_dipendente["Al"],
                                            trasferte_dipendente["Cliente"], trasferte_dipendente["Stato"])
    ]
    mappa_trasferta_id = dict(zip(opzioni_trasferta, trasferte_dipendente["ID"].tolist()))
    trasferta_scelta_label = st.selectbox("Trasferta di riferimento", opzioni_trasferta, key=f"{key_prefix}_rep_trasferta")
    trasferta_id_scelta = mappa_trasferta_id[trasferta_scelta_label]

    cliente_luogo_rep = st.text_input("Cliente / luogo dell'intervento", key=f"{key_prefix}_rep_luogo")
    descrizione_rep = st.text_area("Descrizione del lavoro svolto", key=f"{key_prefix}_rep_descrizione")

    giorno_intervento_rep = st.date_input("Giorno dell'intervento", value=datetime.date.today(), key=f"{key_prefix}_rep_giorno")
    ore_calcolate_rep = calcola_ore_lavorate_periodo(df, dipendente_nome, giorno_intervento_rep, giorno_intervento_rep)
    st.metric("Ore di lavoro (calcolate dalle tue timbrature)", f"{ore_calcolate_rep:.2f} h")
    st.caption("Le ore vengono calcolate automaticamente dalle timbrature Ingresso/Uscita registrate per il giorno indicato sopra: se risultano 0, verifica di aver timbrato quel giorno.")

    commesse_rep = get_commesse_names()
    commessa_rep, fase_rep = None, None
    if commesse_rep:
        commessa_scelta_rep = st.selectbox("Commessa (opzionale)", ["(nessuna)"] + commesse_rep, key=f"{key_prefix}_rep_commessa")
        if commessa_scelta_rep != "(nessuna)":
            commessa_rep = commessa_scelta_rep
            fasi_rep = get_fasi_per_commessa_e_area(commessa_rep, get_user_area_by_name(dipendente_nome))
            if fasi_rep:
                fase_scelta_rep = st.selectbox("Fase (opzionale)", ["(nessuna)"] + fasi_rep, key=f"{key_prefix}_rep_fase")
                fase_rep = fase_scelta_rep if fase_scelta_rep != "(nessuna)" else None

    if not PIL_AVAILABLE:
        st.caption("ℹ️ Le foto verranno salvate senza compressione (libreria Pillow non disponibile).")
    foto_caricate = st.file_uploader("Foto dell'intervento (opzionale, puoi caricarne più di una)",
                                      type=["png", "jpg", "jpeg"], accept_multiple_files=True,
                                      key=f"{key_prefix}_rep_foto")

    if st.button("💾 Salva report intervento", key=f"{key_prefix}_rep_salva"):
        ok, errore = salva_report_intervento(trasferta_id_scelta, dipendente_nome, cliente_luogo_rep,
                                              descrizione_rep, ore_calcolate_rep, commessa_rep, fase_rep, foto_caricate,
                                              data_intervento=giorno_intervento_rep)
        if ok:
            st.success("Report salvato.")
            st.rerun()
        else:
            st.error(errore)

    st.markdown("---")
    st.markdown("**I tuoi report inviati**")
    report_dip = get_report_interventi_dettaglio(dipendente=dipendente_nome)
    if report_dip.empty:
        st.info("Nessun report inviato finora.")
    else:
        st.dataframe(report_dip.drop(columns=["Dipendente"]), use_container_width=True)

def render_disponibilita_team(area_default=None, forza_area=False, key_prefix=""):
    """Vista 'chi è dove' per un giorno scelto: in sede, in trasferta o in ferie/
    permesso. È una vista di pianificazione basata sulle trasferte programmate e
    sulle richieste ferie/permessi già approvate, non sulle timbrature effettive."""
    st.subheader("📅 Disponibilità Team")
    data_riferimento = st.date_input("Giorno", value=datetime.date.today(), key=f"{key_prefix}_disp_data")

    if forza_area:
        area_scelta = area_default
        st.caption(f"Reparto: {area_scelta}")
    else:
        aree_disponibili = get_area_names()
        area_scelta = st.selectbox("Reparto", aree_disponibili, key=f"{key_prefix}_disp_area")

    disponibilita_df = compute_disponibilita_giornaliera(data_riferimento, area_scelta)
    if disponibilita_df.empty:
        st.info("Nessun dipendente trovato per questo reparto.")
        return
    st.dataframe(disponibilita_df, use_container_width=True)
    st.caption("Basata sulle trasferte programmate e sulle richieste ferie/permessi già approvate: se un dipendente non risulta né in trasferta né in ferie/permesso viene mostrato come 'In sede', indipendentemente dalle timbrature effettive del giorno.")

# --- FUNZIONI RUOLO RESPONSABILE ---

def get_responsabile_area(area):
    """Recupera il responsabile di un'area."""
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT nome FROM utenti WHERE area = ? AND role = 'responsabile' LIMIT 1", (area,))
        row = c.fetchone()
        return row[0] if row else None

def get_utenti_responsabile(username):
    """Recupera i dipendenti di cui è responsabile."""
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT area FROM utenti WHERE username = ?", (username,))
        row = c.fetchone()
        if not row:
            return []
        area = row[0]
        c.execute("SELECT nome FROM utenti WHERE area = ? AND role = 'user' ORDER BY nome", (area,))
        return [r[0] for r in c.fetchall()]

# --- FUNZIONI DI CALCOLO E TABELLE (Invariate rispetto a prima, ma alleggerite) ---

def attach_request_area(df_requests):
    if df_requests.empty: return df_requests.copy()
    df = df_requests.copy()
    df["Area"] = df["Dipendente"].map(get_users_area_map()).fillna("Unknown")
    return df

def calculate_ferie_days(start_date, end_date):
    if pd.isna(start_date) or pd.isna(end_date) or end_date < start_date: return 0
    return (end_date - start_date).days + 1

def compute_leave_balances(df_requests, year=None, area="Tutte le aree"):
    requests = attach_request_area(df_requests)
    required_cols = ["Dipendente", "Stato", "Tipo", "Data_inizio", "Data_fine", "Ore_permesso"]
    if requests.empty or not all(col in requests.columns for col in required_cols):
        return pd.DataFrame(columns=["Dipendente", "Area", "Ferie disponibili", "Ferie usate", "Ferie residue", "Permesso ore disponibili", "Permesso ore usate", "Permesso ore residue"])
    if not requests.empty:
        requests["Data_inizio"] = pd.to_datetime(requests["Data_inizio"], errors="coerce").dt.date
        requests["Data_fine"] = pd.to_datetime(requests["Data_fine"], errors="coerce").dt.date
    if year is not None: requests = requests[requests["Data_inizio"].apply(lambda d: pd.notna(d) and d.year == year)]
    if area != "Tutte le aree" and "Area" in requests.columns: requests = requests[requests["Area"] == area]

    rows = []
    mappa_aree = get_users_area_map()  # una sola query, invece di una per dipendente nel ciclo
    for name in get_users_in_area(area):
        area_name = mappa_aree.get(name, "Unknown")
        user_requests = requests[(requests["Dipendente"] == name) & (requests["Stato"] == "Approvato")] if "Dipendente" in requests.columns else pd.DataFrame()
        ferie_presi = 0; permesso_ore_presi = 0.0
        for _, row in user_requests.iterrows():
            if row["Tipo"] == "Ferie": ferie_presi += calculate_ferie_days(row["Data_inizio"], row["Data_fine"])
            elif row["Tipo"] == "Permesso":
                try: permesso_ore_presi += float(row["Ore_permesso"]) if row["Ore_permesso"] else 0.0
                except: pass
        rows.append({"Dipendente": name, "Area": area_name, "Ferie disponibili": DEFAULT_FERIE_GIORNI, "Ferie usate": ferie_presi, "Ferie residue": max(DEFAULT_FERIE_GIORNI - ferie_presi, 0), "Permesso ore disponibili": DEFAULT_PERMESSO_ORE, "Permesso ore usate": round(permesso_ore_presi, 1), "Permesso ore residue": round(max(DEFAULT_PERMESSO_ORE - permesso_ore_presi, 0), 1)})
    return pd.DataFrame(rows)

# --- LIVELLI CCNL E CARTELLINO MENSILE ---
# Nel CCNL Metalmeccanico il livello di inquadramento determina normalmente la paga,
# non i giorni di ferie o le ore di permesso (uguali per tutti a parità di CCNL). Qui
# però l'azienda vuole poter differenziare ferie/permessi anche per livello: la tabella
# "livelli_ferie_permessi" è liberamente configurabile dall'admin per riflettere le
# regole realmente in vigore in azienda, con valori di partenza uguali per tutti.

def get_livelli_disponibili():
    """Elenco dei livelli CCNL configurati (con le relative ferie/permessi annuali)."""
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT livello FROM livelli_ferie_permessi ORDER BY livello")
        return [row[0] for row in c.fetchall()]

def get_livelli_ferie_permessi_dettaglio():
    """Tutti i livelli con ferie/permessi annuali e costo orario, per la tabella di gestione admin."""
    with db_connect() as conn:
        return pd.read_sql(
            "SELECT livello AS Livello, ferie_giorni_anno AS 'Ferie (giorni/anno)', "
            "permesso_ore_anno AS 'Permesso (ore/anno)', costo_orario AS 'Costo orario (€/h)' "
            "FROM livelli_ferie_permessi ORDER BY livello", conn)

def get_ferie_permessi_per_livello(livello):
    """(ferie_giorni_anno, permesso_ore_anno) per il livello indicato. Se il livello
    non è impostato o non risulta configurato, usa i valori di default aziendali."""
    if livello:
        with db_connect() as conn:
            c = conn.cursor()
            c.execute("SELECT ferie_giorni_anno, permesso_ore_anno FROM livelli_ferie_permessi WHERE livello=?", (livello,))
            row = c.fetchone()
            if row:
                return float(row[0]), float(row[1])
    return float(DEFAULT_FERIE_GIORNI), float(DEFAULT_PERMESSO_ORE)

def get_costo_orario_per_livello(livello):
    """Costo orario (€/h) configurato per il livello indicato. Se il livello non è
    impostato o non risulta configurato, usa il costo orario di default aziendale."""
    if livello:
        with db_connect() as conn:
            c = conn.cursor()
            c.execute("SELECT costo_orario FROM livelli_ferie_permessi WHERE livello=?", (livello,))
            row = c.fetchone()
            if row and row[0] is not None:
                return float(row[0])
    return float(DEFAULT_COSTO_ORARIO)

def aggiorna_livello_ferie_permessi(livello, ferie_giorni_anno, permesso_ore_anno, costo_orario):
    """Crea (se nuovo) o aggiorna un livello CCNL con ferie/permessi annuali e costo
    orario. Restituisce (ok, messaggio_errore)."""
    livello = (livello or "").strip()
    if not livello:
        return False, "Il nome del livello è obbligatorio."
    try:
        ferie_giorni_anno = float(ferie_giorni_anno)
        permesso_ore_anno = float(permesso_ore_anno)
        costo_orario = float(costo_orario)
    except (TypeError, ValueError):
        return False, "Ferie, permessi e costo orario devono essere numeri."
    if ferie_giorni_anno < 0 or permesso_ore_anno < 0 or costo_orario < 0:
        return False, "Ferie, permessi e costo orario non possono essere negativi."
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("""INSERT INTO livelli_ferie_permessi (livello, ferie_giorni_anno, permesso_ore_anno, costo_orario) VALUES (?,?,?,?)
                     ON CONFLICT(livello) DO UPDATE SET ferie_giorni_anno=excluded.ferie_giorni_anno,
                        permesso_ore_anno=excluded.permesso_ore_anno, costo_orario=excluded.costo_orario""",
                  (livello, ferie_giorni_anno, permesso_ore_anno, costo_orario))
        conn.commit()
    return True, ""

def elimina_livello_ferie_permessi(livello):
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM livelli_ferie_permessi WHERE livello=?", (livello,))
        conn.commit()

def get_livello_utente(nome_dipendente):
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT livello FROM utenti WHERE nome=?", (nome_dipendente,))
        row = c.fetchone()
        return row[0] if row and row[0] else ""

def get_data_assunzione_utente(nome_dipendente):
    """Data di assunzione (date) del dipendente, o None se non impostata/non valida."""
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT data_assunzione FROM utenti WHERE nome=?", (nome_dipendente,))
        row = c.fetchone()
    if not row or not row[0]:
        return None
    try:
        return datetime.datetime.strptime(row[0], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None

def get_distanza_km_utente(nome_dipendente):
    """Distanza casa-lavoro (km, sola andata) impostata dal dipendente nella sua Area
    Personale. 0 se non impostata: in tal caso il dipendente non contribuisce alla stima
    di CO2 evitata dal mancato tragitto casa-lavoro nella sezione Sostenibilità."""
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT distanza_km FROM utenti WHERE nome=?", (nome_dipendente,))
        row = c.fetchone()
    try:
        return float(row[0]) if row and row[0] is not None else 0.0
    except (TypeError, ValueError):
        return 0.0

CODICE_FISCALE_REGEX = re.compile(r'^[A-Z0-9]{16}$')

def get_dati_personali_utente(username):
    """Dati dell'Area Personale di un utente (per username): livello CCNL e data di
    assunzione (informativi, impostati dall'amministratore) più codice fiscale, data di
    nascita e distanza casa-lavoro (modificabili direttamente dal dipendente)."""
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("""SELECT livello, data_assunzione, codice_fiscale, data_nascita, distanza_km
                     FROM utenti WHERE username=?""", (username,))
        row = c.fetchone()
    if not row:
        return {"livello": "", "data_assunzione": None, "codice_fiscale": "", "data_nascita": None, "distanza_km": 0.0}

    def _parse_data(valore):
        try:
            return datetime.datetime.strptime(valore, "%Y-%m-%d").date() if valore else None
        except (ValueError, TypeError):
            return None

    livello, data_assunzione, codice_fiscale, data_nascita, distanza_km = row
    return {
        "livello": livello or "",
        "data_assunzione": _parse_data(data_assunzione),
        "codice_fiscale": codice_fiscale or "",
        "data_nascita": _parse_data(data_nascita),
        "distanza_km": float(distanza_km) if distanza_km is not None else 0.0,
    }

def aggiorna_dati_personali_utente(username, codice_fiscale, data_nascita, distanza_km):
    """Aggiorna i dati dell'Area Personale che il dipendente può modificare da solo
    (codice fiscale, data di nascita, distanza casa-lavoro). Livello CCNL e data di
    assunzione NON sono toccati qui: restano impostabili solo dall'amministratore,
    perché incidono sul calcolo di ferie/permessi e sul costo del personale.
    Restituisce (ok, messaggio_errore)."""
    codice_fiscale = (codice_fiscale or "").strip().upper()
    if codice_fiscale and not CODICE_FISCALE_REGEX.match(codice_fiscale):
        return False, "Il Codice Fiscale deve essere di 16 caratteri alfanumerici."
    try:
        distanza_km = float(distanza_km)
    except (TypeError, ValueError):
        return False, "La distanza casa-lavoro deve essere un numero."
    if distanza_km < 0:
        return False, "La distanza casa-lavoro non può essere negativa."
    if isinstance(data_nascita, (datetime.date, datetime.datetime)):
        if data_nascita > datetime.date.today():
            return False, "La data di nascita non può essere nel futuro."
        data_nascita_str = data_nascita.isoformat() if isinstance(data_nascita, datetime.date) and not isinstance(data_nascita, datetime.datetime) else data_nascita.date().isoformat()
    elif not data_nascita:
        data_nascita_str = ""
    else:
        return False, "Data di nascita non valida."

    with db_connect() as conn:
        c = conn.cursor()
        c.execute("UPDATE utenti SET codice_fiscale=?, data_nascita=?, distanza_km=? WHERE username=?",
                  (codice_fiscale, data_nascita_str, distanza_km, username))
        conn.commit()
    return True, ""

def mesi_maturazione_nell_anno(data_assunzione, anno, mese):
    """Quanti mesi, da 0 a `mese`, un dipendente ha diritto a maturare ferie/permessi
    nell'anno indicato, tenendo conto della data di assunzione (se nota): se assunto
    prima dell'anno, matura dal mese 1; se assunto durante l'anno, matura dal mese di
    assunzione; se assunto dopo il mese indicato (o dopo l'anno), 0 mesi maturati."""
    primo_mese_utile = 1
    if data_assunzione is not None:
        if data_assunzione.year > anno:
            return 0
        if data_assunzione.year == anno:
            primo_mese_utile = data_assunzione.month
    if mese < primo_mese_utile:
        return 0
    return mese - primo_mese_utile + 1

def compute_saldo_ferie_permessi_mensile(df_requests, nome_dipendente, anno, mese):
    """Saldo di ferie e permessi di un dipendente, maturato mese per mese fino al mese
    indicato incluso (es. a marzo è maturato 3/12 del monte annuale previsto dal suo
    livello CCNL), tenendo conto della data di assunzione quando impostata: se assunto
    a metà anno, la maturazione parte dal mese di assunzione invece che da gennaio. Le
    richieste approvate con Data_inizio nell'anno, fino alla fine del mese indicato,
    vengono conteggiate come già usate."""
    livello = get_livello_utente(nome_dipendente)
    ferie_giorni_anno, permesso_ore_anno = get_ferie_permessi_per_livello(livello)
    data_assunzione = get_data_assunzione_utente(nome_dipendente)
    mesi_maturati = mesi_maturazione_nell_anno(data_assunzione, anno, mese)

    ferie_maturate = round(ferie_giorni_anno / 12 * mesi_maturati, 2)
    permesso_maturate = round(permesso_ore_anno / 12 * mesi_maturati, 2)

    ferie_usate = 0
    permesso_usate = 0.0
    requests = df_requests.copy() if df_requests is not None else pd.DataFrame()
    required_cols = ["Dipendente", "Stato", "Tipo", "Data_inizio", "Data_fine", "Ore_permesso"]
    if not requests.empty and all(col in requests.columns for col in required_cols):
        requests["Data_inizio"] = pd.to_datetime(requests["Data_inizio"], errors="coerce").dt.date
        requests["Data_fine"] = pd.to_datetime(requests["Data_fine"], errors="coerce").dt.date
        fine_periodo = datetime.date(anno, mese, calendar.monthrange(anno, mese)[1])
        mask = ((requests["Dipendente"] == nome_dipendente) & (requests["Stato"] == "Approvato") &
                requests["Data_inizio"].apply(lambda d: pd.notna(d) and d.year == anno and d <= fine_periodo))
        for _, row in requests[mask].iterrows():
            if row["Tipo"] == "Ferie":
                ferie_usate += calculate_ferie_days(row["Data_inizio"], row["Data_fine"])
            elif row["Tipo"] == "Permesso":
                try:
                    permesso_usate += float(row["Ore_permesso"]) if row["Ore_permesso"] else 0.0
                except (TypeError, ValueError):
                    pass

    return {
        "livello": livello or "(non impostato)",
        "data_assunzione": data_assunzione,
        "ferie_giorni_anno": ferie_giorni_anno,
        "permesso_ore_anno": permesso_ore_anno,
        "ferie_maturate": ferie_maturate,
        "ferie_usate": ferie_usate,
        "ferie_residue": round(max(ferie_maturate - ferie_usate, 0), 2),
        "permesso_maturate": permesso_maturate,
        "permesso_usate": round(permesso_usate, 2),
        "permesso_residue": round(max(permesso_maturate - permesso_usate, 0), 2),
    }

def compute_costo_dipendenti(df, start_date, end_date, employee_name=None):
    """Costo del personale nel periodo: ore lavorate (Ingresso->Uscita) per ciascun
    dipendente, moltiplicate per il costo orario del suo livello CCNL. Il costo orario
    è quello configurato in 'Gestione Utenti DB' -> 'Livelli CCNL' (o il default
    aziendale se il dipendente non ha un livello impostato): è una stima, non un dato
    di paga ufficiale."""
    ore_per_dipendente = compute_daily_work(df)
    if ore_per_dipendente.empty:
        return pd.DataFrame(columns=["Dipendente", "Area", "Livello", "Ore_lavorate", "Costo_orario", "Costo_totale"])
    ore_per_dipendente = ore_per_dipendente[(ore_per_dipendente["Data"] >= start_date) & (ore_per_dipendente["Data"] <= end_date)]
    if employee_name:
        ore_per_dipendente = ore_per_dipendente[ore_per_dipendente["Dipendente"] == employee_name]
    if ore_per_dipendente.empty:
        return pd.DataFrame(columns=["Dipendente", "Area", "Livello", "Ore_lavorate", "Costo_orario", "Costo_totale"])

    agg = ore_per_dipendente.groupby("Dipendente", as_index=False).agg({"Ore_lavorate": "sum"})
    # Tre mappe con una query ciascuna, invece di tre query per ogni dipendente in tabella.
    mappa_aree = get_users_area_map()
    mappa_livelli = get_users_livello_map()
    mappa_costi_livello = get_livelli_costo_orario_map()
    agg["Area"] = agg["Dipendente"].map(lambda n: mappa_aree.get(n, "Unknown"))
    agg["Livello"] = agg["Dipendente"].map(lambda n: mappa_livelli.get(n) or "(non impostato)")
    agg["Costo_orario"] = agg["Dipendente"].map(lambda n: mappa_costi_livello.get(mappa_livelli.get(n), float(DEFAULT_COSTO_ORARIO)))
    agg["Ore_lavorate"] = agg["Ore_lavorate"].round(2)
    agg["Costo_totale"] = (agg["Ore_lavorate"] * agg["Costo_orario"]).round(2)
    return agg.sort_values("Costo_totale", ascending=False).reset_index(drop=True)

def compute_costo_per_area(df, start_date, end_date, area_filter=None):
    """Costo del personale (stimato in base a ore lavorate x costo orario per livello),
    aggregato per area."""
    dettaglio = compute_costo_dipendenti(df, start_date, end_date)
    if dettaglio.empty:
        return pd.DataFrame(columns=["Area", "Ore_lavorate", "Costo_totale"])
    if area_filter and area_filter != "Tutte le aree":
        dettaglio = dettaglio[dettaglio["Area"] == area_filter]
    if dettaglio.empty:
        return pd.DataFrame(columns=["Area", "Ore_lavorate", "Costo_totale"])
    agg = dettaglio.groupby("Area", as_index=False).agg({"Ore_lavorate": "sum", "Costo_totale": "sum"})
    agg["Ore_lavorate"] = agg["Ore_lavorate"].round(2)
    agg["Costo_totale"] = agg["Costo_totale"].round(2)
    return agg.sort_values("Costo_totale", ascending=False).reset_index(drop=True)

def get_parametri_sostenibilita():
    """Coefficienti configurati per la sezione Sostenibilità: quanto si stima che
    l'azienda risparmi (€/h di smart working, in costi di gestione ufficio) e quanta
    CO2 (kg/h di smart working) si eviti facendo lavorare un dipendente da casa invece
    che in sede. Restituisce (costo_orario_evitato, co2_kg_orario_evitato)."""
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT costo_orario_ufficio_evitato, co2_kg_orario_evitato FROM impostazioni_sostenibilita WHERE id=1")
        row = c.fetchone()
    if row:
        return float(row[0]), float(row[1])
    return float(DEFAULT_COSTO_ORARIO_UFFICIO_EVITATO), float(DEFAULT_CO2_KG_ORARIO_EVITATO)

def aggiorna_parametri_sostenibilita(costo_orario_evitato, co2_kg_orario_evitato):
    """Aggiorna i coefficienti €/h e kg CO2/h usati per stimare il risparmio dello
    smart working. Restituisce (ok, messaggio_errore)."""
    try:
        costo_orario_evitato = float(costo_orario_evitato)
        co2_kg_orario_evitato = float(co2_kg_orario_evitato)
    except (TypeError, ValueError):
        return False, "I valori devono essere numeri."
    if costo_orario_evitato < 0 or co2_kg_orario_evitato < 0:
        return False, "I valori non possono essere negativi."
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("UPDATE impostazioni_sostenibilita SET costo_orario_ufficio_evitato=?, co2_kg_orario_evitato=? WHERE id=1",
                  (costo_orario_evitato, co2_kg_orario_evitato))
        conn.commit()
    return True, ""

def get_co2_kg_per_km_pendolarismo():
    """Fattore di emissione (kg CO2/km) configurato per stimare la CO2 evitata dal
    mancato tragitto casa-lavoro nei giorni di smart working, in base alla distanza
    casa-lavoro impostata da ciascun dipendente nella sua Area Personale."""
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT co2_kg_per_km_pendolarismo FROM impostazioni_sostenibilita WHERE id=1")
        row = c.fetchone()
    if row and row[0] is not None:
        return float(row[0])
    return float(DEFAULT_CO2_KG_PER_KM_PENDOLARISMO)

def aggiorna_co2_kg_per_km_pendolarismo(co2_kg_per_km):
    """Aggiorna il fattore di emissione (kg CO2/km) usato per stimare la CO2 evitata dal
    tragitto casa-lavoro. Restituisce (ok, messaggio_errore)."""
    try:
        co2_kg_per_km = float(co2_kg_per_km)
    except (TypeError, ValueError):
        return False, "Il valore deve essere un numero."
    if co2_kg_per_km < 0:
        return False, "Il valore non può essere negativo."
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("UPDATE impostazioni_sostenibilita SET co2_kg_per_km_pendolarismo=? WHERE id=1", (co2_kg_per_km,))
        conn.commit()
    return True, ""

def compute_ore_per_luogo(df, start_date, end_date, area_filter=None, employee_name=None):
    """Ore lavorate (Ingresso->Uscita) nel periodo, aggregate per modalità di lavoro
    (Luogo: Vimek, Smart, Trasferta) registrata all'Ingresso di ciascuna sessione.
    "Vimek" è la modalità "in sede" (comprende sia chi lavora in ufficio che chi lavora
    in officina). Usata per confrontare le ore in sede/smart/trasferta nella sezione
    Sostenibilità."""
    dfn = normalize_datetime(df) if df is not None else pd.DataFrame()
    if dfn.empty:
        return pd.DataFrame(columns=["Luogo", "Ore_lavorate"])
    dfn = dfn[(dfn["Data"] >= start_date) & (dfn["Data"] <= end_date)]
    if employee_name:
        dfn = dfn[dfn["Dipendente"] == employee_name]
    if area_filter and area_filter != "Tutte le aree":
        mappa_aree = get_users_area_map()
        dfn = dfn[dfn["Dipendente"].map(lambda n: mappa_aree.get(n, "Unknown")) == area_filter]
    if dfn.empty:
        return pd.DataFrame(columns=["Luogo", "Ore_lavorate"])

    righe = []
    for (_, _), gruppo in dfn.groupby(["Dipendente", "Data"]):
        gruppo = gruppo.sort_values("Timestamp")
        ingresso_queue = []
        for _, riga in gruppo.iterrows():
            if riga["Azione"] == "Ingresso":
                ingresso_queue.append(riga)
            elif riga["Azione"] == "Uscita" and ingresso_queue:
                inizio = ingresso_queue.pop(0)
                if pd.notna(inizio["Timestamp"]) and pd.notna(riga["Timestamp"]) and riga["Timestamp"] > inizio["Timestamp"]:
                    ore = (riga["Timestamp"] - inizio["Timestamp"]).total_seconds() / 3600
                    # "Vimek" è la modalità "in sede" (comprende sia chi lavora in ufficio
                    # che chi lavora in officina): eventuali valori storici o sconosciuti
                    # (es. il vecchio "Ufficio") ricadono qui.
                    luogo = str(inizio.get("Luogo") or "Vimek").strip()
                    if luogo not in ("Vimek", "Smart", "Trasferta"):
                        luogo = "Vimek"
                    righe.append({"Luogo": luogo, "Ore_lavorate": ore})

    if not righe:
        return pd.DataFrame(columns=["Luogo", "Ore_lavorate"])
    agg = pd.DataFrame(righe).groupby("Luogo", as_index=False).agg({"Ore_lavorate": "sum"})
    agg["Ore_lavorate"] = agg["Ore_lavorate"].round(2)
    return agg.sort_values("Ore_lavorate", ascending=False).reset_index(drop=True)

def compute_risparmio_smart_working(ore_smart, costo_orario_evitato=None, co2_kg_orario_evitato=None):
    """Risparmio stimato (€) e CO2 evitata (kg) per un dato numero di ore di smart
    working, in base ai coefficienti configurati in 'Sostenibilità' (o passati
    esplicitamente). È una stima basata su studi di settore, non un dato aziendale
    misurato: va tarata sui costi energetici reali dell'azienda."""
    if costo_orario_evitato is None or co2_kg_orario_evitato is None:
        costo_default, co2_default = get_parametri_sostenibilita()
        if costo_orario_evitato is None:
            costo_orario_evitato = costo_default
        if co2_kg_orario_evitato is None:
            co2_kg_orario_evitato = co2_default
    risparmio_euro = round(float(ore_smart) * float(costo_orario_evitato), 2)
    co2_kg = round(float(ore_smart) * float(co2_kg_orario_evitato), 2)
    return risparmio_euro, co2_kg

def compute_giorni_smart_per_dipendente(df, start_date, end_date, area_filter=None, employee_name=None):
    """Numero di giorni distinti, nel periodo, in cui ciascun dipendente ha timbrato
    l'Ingresso in modalità Smart almeno una volta. Usato per stimare la CO2 evitata dal
    mancato tragitto casa-lavoro (vedi compute_co2_risparmiata_pendolarismo)."""
    dfn = normalize_datetime(df) if df is not None else pd.DataFrame()
    if dfn.empty:
        return pd.DataFrame(columns=["Dipendente", "Giorni_smart"])
    dfn = dfn[(dfn["Data"] >= start_date) & (dfn["Data"] <= end_date)]
    if employee_name:
        dfn = dfn[dfn["Dipendente"] == employee_name]
    if area_filter and area_filter != "Tutte le aree":
        mappa_aree = get_users_area_map()
        dfn = dfn[dfn["Dipendente"].map(lambda n: mappa_aree.get(n, "Unknown")) == area_filter]
    if dfn.empty:
        return pd.DataFrame(columns=["Dipendente", "Giorni_smart"])

    ingressi_smart = dfn[(dfn["Azione"] == "Ingresso") & (dfn["Luogo"].astype(str) == "Smart")]
    if ingressi_smart.empty:
        return pd.DataFrame(columns=["Dipendente", "Giorni_smart"])
    agg = ingressi_smart.groupby("Dipendente")["Data"].nunique().reset_index()
    agg.columns = ["Dipendente", "Giorni_smart"]
    return agg.sort_values("Giorni_smart", ascending=False).reset_index(drop=True)

def compute_co2_risparmiata_pendolarismo(df, start_date, end_date, area_filter=None, employee_name=None, co2_kg_per_km=None):
    """CO2 evitata (kg) grazie al mancato tragitto casa-lavoro nei giorni di smart
    working: per ciascun dipendente, giorni di smart working x distanza casa-lavoro
    (andata e ritorno) x fattore di emissione medio (kg CO2/km). Richiede che il
    dipendente abbia impostato la propria distanza casa-lavoro in 'Area Personale': chi
    non l'ha impostata (distanza 0) non contribuisce alla stima. Restituisce (dettaglio
    per dipendente, CO2 totale evitata in kg)."""
    if co2_kg_per_km is None:
        co2_kg_per_km = get_co2_kg_per_km_pendolarismo()
    giorni_smart = compute_giorni_smart_per_dipendente(df, start_date, end_date, area_filter=area_filter, employee_name=employee_name)
    colonne = ["Dipendente", "Giorni_smart", "Distanza_km", "CO2_evitata_kg"]
    if giorni_smart.empty:
        return pd.DataFrame(columns=colonne), 0.0

    giorni_smart["Distanza_km"] = giorni_smart["Dipendente"].map(get_users_distanza_map()).fillna(0.0)
    giorni_smart["CO2_evitata_kg"] = (giorni_smart["Giorni_smart"] * giorni_smart["Distanza_km"] * 2 * float(co2_kg_per_km)).round(2)
    totale_kg = round(float(giorni_smart["CO2_evitata_kg"].sum()), 2)
    return giorni_smart[colonne].sort_values("CO2_evitata_kg", ascending=False).reset_index(drop=True), totale_kg

GIORNI_SETTIMANA_IT = ["Lun", "Mar", "Mer", "Gio", "Ven", "Sab", "Dom"]

def compute_cartellino_mensile(df, nome_dipendente, anno, mese, df_requests=None):
    """Cartellino mensile di un dipendente: una riga per ogni giorno del mese con
    ingresso/uscita, inizio/fine pausa pranzo, tutte le commesse/fasi lavorate (un
    dipendente può lavorare su più commesse diverse nella stessa giornata), luogo di
    lavoro, ore ordinarie e ore straordinarie (oltre ORE_ORDINARIE_GIORNALIERE ore/
    giorno, al netto della pausa pranzo). I giorni coperti da una richiesta di ferie/
    permesso approvata vengono marcati come tali."""
    dfn = normalize_datetime(df) if df is not None else pd.DataFrame()
    if not dfn.empty:
        dfn = dfn[dfn["Dipendente"] == nome_dipendente]

    giorni_ferie, giorni_permesso = set(), set()
    if df_requests is not None and not df_requests.empty and all(
        col in df_requests.columns for col in ["Dipendente", "Stato", "Tipo", "Data_inizio", "Data_fine"]
    ):
        richieste_dip = df_requests[(df_requests["Dipendente"] == nome_dipendente) & (df_requests["Stato"] == "Approvato")].copy()
        richieste_dip["Data_inizio"] = pd.to_datetime(richieste_dip["Data_inizio"], errors="coerce").dt.date
        richieste_dip["Data_fine"] = pd.to_datetime(richieste_dip["Data_fine"], errors="coerce").dt.date
        for _, row in richieste_dip.iterrows():
            inizio, fine = row["Data_inizio"], row["Data_fine"]
            if pd.isna(inizio) or pd.isna(fine):
                continue
            giorno_corrente = inizio
            while giorno_corrente <= fine:
                if giorno_corrente.year == anno and giorno_corrente.month == mese:
                    (giorni_ferie if row["Tipo"] == "Ferie" else giorni_permesso).add(giorno_corrente)
                giorno_corrente += datetime.timedelta(days=1)

    n_giorni = calendar.monthrange(anno, mese)[1]
    righe = []
    for giorno in range(1, n_giorni + 1):
        data_corrente = datetime.date(anno, mese, giorno)
        gruppo = dfn[dfn["Data"] == data_corrente] if not dfn.empty else pd.DataFrame()
        gruppo = gruppo.sort_values("Timestamp") if not gruppo.empty else gruppo

        ingresso_txt, uscita_txt, fasi_txt, dove_txt = "-", "-", "-", "-"
        inizio_pausa_txt, fine_pausa_txt = "-", "-"
        ore_ordinarie, ore_straordinarie = 0.0, 0.0

        if not gruppo.empty:
            ingressi = gruppo[gruppo["Azione"] == "Ingresso"]
            uscite = gruppo[gruppo["Azione"] == "Uscita"]
            if not ingressi.empty:
                ingresso_txt = str(ingressi.iloc[0]["Ora"])[:8]
            if not uscite.empty:
                uscita_txt = str(uscite.iloc[-1]["Ora"])[:8]

            inizio_pause = gruppo[gruppo["Azione"] == "Inizio Pausa"]
            fine_pause = gruppo[gruppo["Azione"] == "Fine Pausa"]
            if not inizio_pause.empty:
                inizio_pausa_txt = str(inizio_pause.iloc[0]["Ora"])[:8]
            if not fine_pause.empty:
                fine_pausa_txt = str(fine_pause.iloc[-1]["Ora"])[:8]

            # Commesse e fasi lavorate nel giorno: un dipendente può lavorare su più
            # commesse/fasi diverse nella stessa giornata, quindi si elencano tutte le
            # combinazioni distinte Commessa/Fase registrate (con la commessa in
            # evidenza per primo), non solo l'ultima.
            fasi_lavorate = sorted(set(
                f"{r['Commessa']}: {r['Fase']}" for _, r in gruppo.iterrows()
                if r["Azione"] in ("Inizio fase", "Fine fase") and r.get("Fase") and r.get("Commessa")
            ))
            if fasi_lavorate:
                fasi_txt = ", ".join(fasi_lavorate)

            luoghi = sorted(set(str(l) for l in gruppo["Luogo"].tolist() if l and str(l) != "nan"))
            if luoghi:
                dove_txt = ", ".join(luoghi)

            # Ore lavorate nette: sessioni Ingresso->Uscita meno le pause pranzo registrate.
            coda_ingressi, sessioni = [], []
            for _, r in gruppo.iterrows():
                if r["Azione"] == "Ingresso":
                    coda_ingressi.append(r["Timestamp"])
                elif r["Azione"] == "Uscita" and coda_ingressi:
                    inizio_sessione = coda_ingressi.pop(0)
                    if pd.notna(inizio_sessione) and pd.notna(r["Timestamp"]) and r["Timestamp"] > inizio_sessione:
                        sessioni.append((inizio_sessione, r["Timestamp"]))
            secondi_lavorati = sum((fine - inizio).total_seconds() for inizio, fine in sessioni)

            coda_pause, secondi_pausa = [], 0
            for _, r in gruppo.iterrows():
                if r["Azione"] == "Inizio Pausa":
                    coda_pause.append(r["Timestamp"])
                elif r["Azione"] == "Fine Pausa" and coda_pause:
                    inizio_pausa = coda_pause.pop(0)
                    if pd.notna(inizio_pausa) and pd.notna(r["Timestamp"]) and r["Timestamp"] > inizio_pausa:
                        secondi_pausa += (r["Timestamp"] - inizio_pausa).total_seconds()

            ore_nette = max(secondi_lavorati - secondi_pausa, 0) / 3600
            ore_ordinarie = round(min(ore_nette, ORE_ORDINARIE_GIORNALIERE), 2)
            ore_straordinarie = round(max(ore_nette - ORE_ORDINARIE_GIORNALIERE, 0), 2)

        if dove_txt == "-":
            if data_corrente in giorni_ferie:
                dove_txt = "Ferie"
            elif data_corrente in giorni_permesso:
                dove_txt = "Permesso"

        nome_giorno = GIORNI_SETTIMANA_IT[data_corrente.weekday()]
        righe.append({
            "Giorno": f"{data_corrente.strftime('%d/%m')} ({nome_giorno})",
            "Ingresso": ingresso_txt,
            "Uscita": uscita_txt,
            "Inizio Pausa": inizio_pausa_txt,
            "Fine Pausa": fine_pausa_txt,
            "Commesse/Fasi": fasi_txt,
            "Dove": dove_txt,
            "Ore ordinarie": ore_ordinarie,
            "Ore straordinarie": ore_straordinarie,
        })

    return pd.DataFrame(righe)

def compute_totali_cartellino(cartellino_df, df, nome_dipendente, anno, mese):
    """Totali di riepilogo del cartellino mensile: ore ordinarie/straordinarie lavorate
    nel mese, ore effettivamente tracciate su fasi/commesse, e giorni lavorati."""
    if cartellino_df is None or cartellino_df.empty:
        ore_ordinarie_tot, ore_straordinarie_tot, giorni_lavorati = 0.0, 0.0, 0
    else:
        ore_ordinarie_tot = round(float(cartellino_df["Ore ordinarie"].sum()), 2)
        ore_straordinarie_tot = round(float(cartellino_df["Ore straordinarie"].sum()), 2)
        giorni_lavorati = int(((cartellino_df["Ore ordinarie"] + cartellino_df["Ore straordinarie"]) > 0).sum())

    inizio_mese = datetime.date(anno, mese, 1)
    fine_mese = datetime.date(anno, mese, calendar.monthrange(anno, mese)[1])
    ore_fase_df = compute_phase_hours_by_employee(df, inizio_mese, fine_mese, employee_name=nome_dipendente)
    ore_su_commesse = round(float(ore_fase_df["Ore_fase"].sum()), 2) if not ore_fase_df.empty else 0.0

    return {
        "ore_ordinarie": ore_ordinarie_tot,
        "ore_straordinarie": ore_straordinarie_tot,
        "ore_su_commesse": ore_su_commesse,
        "giorni_lavorati": giorni_lavorati,
    }

def compute_kpi_summary(df_requests, year=None, area="Tutte le aree"):
    requests = attach_request_area(df_requests)
    if requests.empty: return {"Totale richieste": 0, "Richieste approvate": 0, "Richieste rifiutate": 0, "Richieste in attesa": 0, "Tasso approvazione": "0%", "Giorni ferie approvati": 0, "Ore permesso approvate": 0.0}
    requests["Data_inizio"] = pd.to_datetime(requests["Data_inizio"], errors="coerce").dt.date
    requests["Data_fine"] = pd.to_datetime(requests["Data_fine"], errors="coerce").dt.date
    if area != "Tutte le aree" and "Area" in requests.columns: requests = requests[requests["Area"] == area]
    if year is not None: requests = requests[requests["Data_inizio"].apply(lambda d: pd.notna(d) and d.year == year)]

    total = len(requests)
    approvate = len(requests[requests["Stato"] == "Approvato"])
    rifiutate = len(requests[requests["Stato"] == "Rifiutato"])
    in_attesa = len(requests[requests["Stato"] == "In attesa"])
    ferie_giorni = 0; permesso_ore = 0.0
    for _, row in requests[requests["Stato"] == "Approvato"].iterrows():
        if row["Tipo"] == "Ferie": ferie_giorni += calculate_ferie_days(row["Data_inizio"], row["Data_fine"])
        elif row["Tipo"] == "Permesso":
            try: permesso_ore += float(row["Ore_permesso"])
            except: pass
    return {"Totale richieste": total, "Richieste approvate": approvate, "Richieste rifiutate": rifiutate, "Richieste in attesa": in_attesa, "Tasso approvazione": f"{round(approvate/total*100,1)}%" if total else "0%", "Giorni ferie approvati": ferie_giorni, "Ore permesso approvate": round(permesso_ore, 1)}

def normalize_datetime(df):
    if df.empty: return df
    df = df.copy()
    df["Data"] = pd.to_datetime(df["Data"], errors="coerce").dt.date
    df["Ora"] = pd.to_datetime(df["Ora"], format="%H:%M:%S", errors="coerce").dt.time
    df["Timestamp"] = pd.to_datetime(df["Data"].astype(str) + " " + df["Ora"].astype(str), errors="coerce")
    return df

def compute_daily_work(df):
    df = normalize_datetime(df)
    if df.empty: return pd.DataFrame(columns=["Dipendente", "Data", "Ore_lavorate", "Timbrature"])
    rows = []
    for (user, work_date), group in df.groupby(["Dipendente", "Data"]):
        group = group.sort_values("Timestamp")
        total_seconds = 0; ingresso_queue = []
        for _, row in group.iterrows():
            if row["Azione"] == "Ingresso": ingresso_queue.append(row["Timestamp"])
            elif row["Azione"] == "Uscita" and ingresso_queue:
                start = ingresso_queue.pop(0)
                if pd.notna(start) and pd.notna(row["Timestamp"]) and row["Timestamp"] > start:
                    total_seconds += (row["Timestamp"] - start).total_seconds()
        rows.append({"Dipendente": user, "Data": work_date, "Ore_lavorate": total_seconds / 3600, "Timbrature": len(group)})
    return pd.DataFrame(rows)

def build_gantt_segments(df, year, month, user_name=None):
    df = normalize_datetime(df)
    if df.empty: return pd.DataFrame(columns=["Dipendente", "start", "end", "Data", "Luogo"])
    if user_name: df = df[df["Dipendente"] == user_name]
    df = df[df["Data"].apply(lambda d: d.year == year and d.month == month)]
    if df.empty: return pd.DataFrame(columns=["Dipendente", "start", "end", "Data", "Luogo"])
    rows = []
    for (user, date), group in df.groupby(["Dipendente", "Data"]):
        group = group.sort_values("Timestamp")
        ingressi = group[group["Azione"] == "Ingresso"]["Timestamp"]
        uscite = group[group["Azione"] == "Uscita"]["Timestamp"]
        if ingressi.empty or uscite.empty: continue
        start = ingressi.iloc[0]; end = uscite.iloc[-1]
        if pd.isna(start) or pd.isna(end) or end <= start: continue
        luogo = ", ".join(sorted(set(group["Luogo"].astype(str).replace("nan", "-").tolist())))
        rows.append({"Dipendente": user, "start": start, "end": end, "Data": date, "Luogo": luogo})
    return pd.DataFrame(rows)

def get_day_records(df, selected_date, user_name=None):
    day_rows = df[df["Data"] == selected_date.isoformat()]
    if user_name: day_rows = day_rows[day_rows["Dipendente"] == user_name]
    return day_rows

def get_phase_options(user_info=None):
    area = (user_info.get("area", "") if user_info else "") or ""
    position = (user_info.get("position", "") if user_info else "") or ""
    return get_fasi_area(area=area, posizione=position)

def compute_hours_by_commessa(df, start_date, end_date, employee_name=None):
    df = normalize_datetime(df)
    if df.empty: return pd.DataFrame(columns=["Dipendente", "Commessa", "Ore_lavorate"])
    df = df[df["Data"].apply(lambda d: start_date <= d <= end_date)]
    if employee_name: df = df[df["Dipendente"] == employee_name]
    rows = []
    for (user, date, commessa), group in df.groupby(["Dipendente", "Data", "Commessa"]):
        if not commessa: continue
        group = group.sort_values("Timestamp")
        ingresso_queue = []
        for _, row in group.iterrows():
            if row["Azione"] == "Inizio fase": ingresso_queue.append(row["Timestamp"])
            elif row["Azione"] == "Fine fase" and ingresso_queue:
                start = ingresso_queue.pop(0)
                if pd.notna(start) and pd.notna(row["Timestamp"]) and row["Timestamp"] > start:
                    duration = (row["Timestamp"] - start).total_seconds() / 3600
                    rows.append({"Dipendente": user, "Commessa": commessa, "Ore_lavorate": duration})
    if not rows: return pd.DataFrame(columns=["Dipendente", "Commessa", "Ore_lavorate"])
    return pd.DataFrame(rows).groupby(["Dipendente", "Commessa"], as_index=False).agg({"Ore_lavorate": "sum"}).assign(Ore_lavorate=lambda d: d["Ore_lavorate"].round(2))

def compute_phase_hours_by_employee(df, start_date, end_date, employee_name=None):
    """Ore totali passate con una fase/commessa attiva (Inizio fase -> Fine fase),
    per dipendente, nel periodo indicato. È il tempo "tracciato" su un'attività
    specifica, a differenza delle ore di presenza (Ingresso -> Uscita) che
    includono anche il tempo non assegnato a nessuna fase."""
    df = normalize_datetime(df)
    if df.empty: return pd.DataFrame(columns=["Dipendente", "Ore_fase"])
    df = df[df["Data"].apply(lambda d: start_date <= d <= end_date)]
    if employee_name: df = df[df["Dipendente"] == employee_name]
    if df.empty: return pd.DataFrame(columns=["Dipendente", "Ore_fase"])
    rows = []
    for (user, work_date), group in df.groupby(["Dipendente", "Data"]):
        group = group.sort_values("Timestamp")
        fase_queue = []
        for _, row in group.iterrows():
            if row["Azione"] == "Inizio fase": fase_queue.append(row["Timestamp"])
            elif row["Azione"] == "Fine fase" and fase_queue:
                start = fase_queue.pop(0)
                if pd.notna(start) and pd.notna(row["Timestamp"]) and row["Timestamp"] > start:
                    rows.append({"Dipendente": user, "Ore_fase": (row["Timestamp"] - start).total_seconds() / 3600})
    if not rows: return pd.DataFrame(columns=["Dipendente", "Ore_fase"])
    return pd.DataFrame(rows).groupby("Dipendente", as_index=False).agg({"Ore_fase": "sum"})

def compute_productivity_by_area(df, start_date, end_date, area_filter="Tutte le aree"):
    """Calcola, per reparto, la percentuale di ore di presenza effettivamente
    tracciate su una fase/commessa: Produttivita_% = Ore_fase / Ore_presenza * 100.
    Non è un giudizio assoluto (dipende da quanto rigorosamente si usano Inizio/Fine
    fase), ma è un indicatore confrontabile tra reparti con i dati già raccolti oggi."""
    presence = compute_daily_work(df)
    if not presence.empty:
        presence = presence[(presence["Data"] >= start_date) & (presence["Data"] <= end_date)]
    presence_tot = presence.groupby("Dipendente", as_index=False).agg({"Ore_lavorate": "sum"}) if not presence.empty else pd.DataFrame(columns=["Dipendente", "Ore_lavorate"])

    phase_tot = compute_phase_hours_by_employee(df, start_date, end_date)

    merged = presence_tot.merge(phase_tot, on="Dipendente", how="left")
    if merged.empty:
        return pd.DataFrame(columns=["Area", "Ore_presenza", "Ore_fase", "Produttivita_%"])
    merged["Ore_fase"] = merged["Ore_fase"].fillna(0.0)
    merged["Area"] = merged["Dipendente"].map(get_users_area_map()).fillna("Unknown")

    if area_filter and area_filter != "Tutte le aree":
        merged = merged[merged["Area"] == area_filter]
    if merged.empty:
        return pd.DataFrame(columns=["Area", "Ore_presenza", "Ore_fase", "Produttivita_%"])

    agg = merged.groupby("Area", as_index=False).agg({"Ore_lavorate": "sum", "Ore_fase": "sum"})
    agg = agg.rename(columns={"Ore_lavorate": "Ore_presenza"})
    agg["Ore_presenza"] = agg["Ore_presenza"].round(2)
    agg["Ore_fase"] = agg["Ore_fase"].round(2)
    agg["Produttivita_%"] = agg.apply(lambda r: round(r["Ore_fase"] / r["Ore_presenza"] * 100, 1) if r["Ore_presenza"] > 0 else 0.0, axis=1)
    return agg.sort_values("Produttivita_%", ascending=False).reset_index(drop=True)

def get_area_productivity_ranking(productivity_df, area_name):
    """Restituisce la posizione in classifica di un'area per produttività, insieme
    al proprio valore e alla media aziendale — senza esporre i valori delle altre
    aree singolarmente (utile per una vista "anonima" ad uso dei responsabili)."""
    if productivity_df.empty or area_name not in productivity_df["Area"].values:
        return None
    ordered = productivity_df.sort_values("Produttivita_%", ascending=False).reset_index(drop=True)
    position = int(ordered.index[ordered["Area"] == area_name][0]) + 1
    own_pct = float(ordered.loc[ordered["Area"] == area_name, "Produttivita_%"].iloc[0])
    company_avg = float(ordered["Produttivita_%"].mean())
    return {"position": position, "total_areas": len(ordered), "own_pct": own_pct, "company_avg_pct": round(company_avg, 1)}

def compute_fasi_commessa_ore_effettive(df, start_date, end_date, area_filter=None):
    """Ore effettive lavorate (Inizio fase -> Fine fase) per ogni combinazione
    Commessa/Fase/Area, nel periodo indicato. Somma le ore di tutti i dipendenti
    che hanno lavorato quella fase di quella commessa."""
    dfn = normalize_datetime(df)
    if dfn.empty: return pd.DataFrame(columns=["Commessa", "Fase", "Area", "Ore_effettive"])
    dfn = dfn[dfn["Data"].apply(lambda d: start_date <= d <= end_date)]
    if dfn.empty: return pd.DataFrame(columns=["Commessa", "Fase", "Area", "Ore_effettive"])
    dfn = dfn.copy()
    dfn["Area"] = dfn["Dipendente"].map(get_users_area_map()).fillna("Unknown")
    if area_filter and area_filter != "Tutte le aree":
        dfn = dfn[dfn["Area"] == area_filter]
    if dfn.empty: return pd.DataFrame(columns=["Commessa", "Fase", "Area", "Ore_effettive"])

    rows = []
    for (_dip, _date, commessa, fase, area), group in dfn.groupby(["Dipendente", "Data", "Commessa", "Fase", "Area"]):
        if not commessa or not fase: continue
        group = group.sort_values("Timestamp")
        queue = []
        for _, row in group.iterrows():
            if row["Azione"] == "Inizio fase": queue.append(row["Timestamp"])
            elif row["Azione"] == "Fine fase" and queue:
                start = queue.pop(0)
                if pd.notna(start) and pd.notna(row["Timestamp"]) and row["Timestamp"] > start:
                    rows.append({"Commessa": commessa, "Fase": fase, "Area": area, "Ore_effettive": (row["Timestamp"] - start).total_seconds() / 3600})
    if not rows: return pd.DataFrame(columns=["Commessa", "Fase", "Area", "Ore_effettive"])
    return pd.DataFrame(rows).groupby(["Commessa", "Fase", "Area"], as_index=False).agg({"Ore_effettive": "sum"})

def compute_produttivita_commesse(df, start_date, end_date, area_filter=None, commessa_filter=None):
    """Confronta, per ogni Commessa/Fase/Area configurata, le ore stimate (impostate
    da admin/responsabile) con le ore effettivamente lavorate nel periodo.
    Produttivita_% = ore_stimate / ore_effettive * 100: sopra 100% significa che si
    è finito prima del previsto (bene), sotto 100% che si è sforato (male).
    Le fasi non ancora lavorate nel periodo hanno Produttivita_% = None (non ancora avviate)."""
    with db_connect() as conn:
        stime = pd.read_sql("SELECT commessa AS Commessa, fase AS Fase, area AS Area, ore_stimate AS Ore_stimate FROM fasi_commessa", conn)
    if stime.empty:
        return pd.DataFrame(columns=["Commessa", "Fase", "Area", "Ore_stimate", "Ore_effettive", "Produttivita_%"])

    if area_filter and area_filter != "Tutte le aree":
        stime = stime[stime["Area"] == area_filter]
    if commessa_filter and commessa_filter != "Tutte le commesse":
        stime = stime[stime["Commessa"] == commessa_filter]
    if stime.empty:
        return pd.DataFrame(columns=["Commessa", "Fase", "Area", "Ore_stimate", "Ore_effettive", "Produttivita_%"])

    effettive = compute_fasi_commessa_ore_effettive(df, start_date, end_date, area_filter=area_filter)
    merged = stime.merge(effettive, on=["Commessa", "Fase", "Area"], how="left")
    merged["Ore_effettive"] = merged["Ore_effettive"].fillna(0.0).round(2)
    merged["Ore_stimate"] = merged["Ore_stimate"].round(2)
    merged["Produttivita_%"] = merged.apply(
        lambda r: round(r["Ore_stimate"] / r["Ore_effettive"] * 100, 1) if r["Ore_effettive"] > 0 else None, axis=1
    )
    return merged.sort_values(["Commessa", "Area", "Fase"]).reset_index(drop=True)

def compute_produttivita_per_area(produttivita_df):
    """Aggrega la produttività per area a partire dal dettaglio per fase, pesando
    sulle ore (rapporto tra somma ore stimate e somma ore effettive) invece di fare
    una semplice media delle percentuali, così le fasi più grandi pesano di più."""
    if produttivita_df.empty:
        return pd.DataFrame(columns=["Area", "Ore_stimate", "Ore_effettive", "Produttivita_%"])
    valide = produttivita_df[produttivita_df["Ore_effettive"] > 0]
    if valide.empty:
        return pd.DataFrame(columns=["Area", "Ore_stimate", "Ore_effettive", "Produttivita_%"])
    agg = valide.groupby("Area", as_index=False).agg({"Ore_stimate": "sum", "Ore_effettive": "sum"})
    agg["Ore_stimate"] = agg["Ore_stimate"].round(2)
    agg["Ore_effettive"] = agg["Ore_effettive"].round(2)
    agg["Produttivita_%"] = agg.apply(lambda r: round(r["Ore_stimate"] / r["Ore_effettive"] * 100, 1) if r["Ore_effettive"] > 0 else None, axis=1)
    return agg.sort_values("Produttivita_%", ascending=False).reset_index(drop=True)

def get_user_color(username):
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT colore FROM utenti WHERE nome=?", (username,))
        row = c.fetchone()
        return row[0] if row else '#4fa8ff'

def get_users_colors_map():
    """Mappa {nome_dipendente: colore}, per colorare in blocco le righe di un report
    che mostra più dipendenti insieme (una sola query invece di una per riga)."""
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("SELECT nome, colore FROM utenti")
        return {row[0]: (row[1] or '#4fa8ff') for row in c.fetchall()}

def _testo_leggibile_su(colore_hex):
    """'black' o 'white': qualunque sia più leggibile sopra uno sfondo colore_hex,
    in base alla luminanza percepita del colore."""
    try:
        colore_hex = (colore_hex or "").lstrip("#")
        r, g, b = int(colore_hex[0:2], 16), int(colore_hex[2:4], 16), int(colore_hex[4:6], 16)
        luminanza = (0.299 * r + 0.587 * g + 0.114 * b) / 255
        return "black" if luminanza > 0.55 else "white"
    except (ValueError, IndexError):
        return "black"

def evidenzia_dipendenti_per_colore(df):
    """Restituisce uno Styler che colora lo sfondo di ogni riga con il colore
    assegnato al dipendente di quella riga (colonna 'Dipendente'): utile per
    individuare a colpo d'occhio le righe di ciascuno in un report che ne mostra
    più di uno insieme. Se il DataFrame non ha una colonna 'Dipendente', o è
    vuoto, viene restituito invariato."""
    if df is None or df.empty or "Dipendente" not in df.columns:
        return df
    colori = get_users_colors_map()

    def _stile_riga(row):
        colore = colori.get(row["Dipendente"])
        if not colore:
            return [''] * len(row)
        return [f'background-color: {colore}; color: {_testo_leggibile_su(colore)}'] * len(row)

    return df.style.apply(_stile_riga, axis=1)

# Colori delle righe nelle tabelle di richieste ferie/permessi, in base a stato e tipo:
# Approvato = verde, Rifiutato = grigio, In attesa - Ferie = rosso, In attesa - Permesso = giallo.
COLORI_RICHIESTE = {
    "Approvato": ("#d4edda", "#155724"),
    "Rifiutato": ("#e2e3e5", "#383d41"),
    ("In attesa", "Ferie"): ("#f8d7da", "#721c24"),
    ("In attesa", "Permesso"): ("#fff3cd", "#856404"),
}
LEGENDA_COLORI_RICHIESTE = "🟩 Approvata &nbsp;·&nbsp; 🟥 Ferie in attesa &nbsp;·&nbsp; 🟨 Permesso in attesa &nbsp;·&nbsp; ⬜ Rifiutata"

def evidenzia_richieste_per_stato(df):
    """Restituisce uno Styler che colora ogni riga di una tabella di richieste
    ferie/permessi in base a stato e tipo (vedi COLORI_RICHIESTE), per riconoscerle
    a colpo d'occhio: verde = approvata, grigio = rifiutata, rosso = ferie ancora in
    attesa, giallo = permesso ancora in attesa. Se il DataFrame non ha le colonne
    'Stato'/'Tipo', o è vuoto, viene restituito invariato."""
    if df is None or df.empty or "Stato" not in df.columns or "Tipo" not in df.columns:
        return df

    def _stile_riga(row):
        stato = row.get("Stato")
        tipo = row.get("Tipo")
        if stato in ("Approvato", "Rifiutato"):
            colori = COLORI_RICHIESTE[stato]
        elif (stato, tipo) in COLORI_RICHIESTE:
            colori = COLORI_RICHIESTE[(stato, tipo)]
        else:
            return [''] * len(row)
        sfondo, testo = colori
        return [f'background-color: {sfondo}; color: {testo}'] * len(row)

    return df.style.apply(_stile_riga, axis=1)

def render_report_mensile(nome_dipendente, df, key_prefix):
    """Report/cartellino mensile personale: giorno per giorno orari, fasi lavorate e
    luogo di lavoro, ore ordinarie/straordinarie, più il saldo di ferie e permessi
    maturato fino al mese selezionato in base al livello CCNL dell'utente."""
    st.subheader("🗓️ Report mensile")
    oggi = datetime.date.today()
    col_mese, col_anno = st.columns(2)
    with col_mese:
        mese_sel = st.selectbox("Mese", options=list(range(1, 13)),
                                 format_func=lambda m: calendar.month_name[m].capitalize(),
                                 index=oggi.month - 1, key=f"{key_prefix}_report_mese")
    with col_anno:
        anno_sel = st.number_input("Anno", min_value=2020, max_value=oggi.year + 1, value=oggi.year, key=f"{key_prefix}_report_anno")

    richieste_dipendente = carica_richieste()

    st.markdown(f"### Cartellino di {calendar.month_name[mese_sel].capitalize()} {anno_sel}")
    cartellino = compute_cartellino_mensile(df, nome_dipendente, anno_sel, mese_sel, df_requests=richieste_dipendente)
    st.dataframe(cartellino, use_container_width=True)

    totali = compute_totali_cartellino(cartellino, df, nome_dipendente, anno_sel, mese_sel)
    st.markdown("**Totale del mese**")
    col_t1, col_t2, col_t3, col_t4 = st.columns(4)
    col_t1.metric("Giorni lavorati", totali["giorni_lavorati"])
    col_t2.metric("Ore ordinarie", f"{totali['ore_ordinarie']:.2f}h")
    col_t3.metric("Ore straordinarie", f"{totali['ore_straordinarie']:.2f}h")
    col_t4.metric("Ore su commesse", f"{totali['ore_su_commesse']:.2f}h")

    st.markdown("---")
    st.markdown("**Saldo ferie e permessi (maturati fino a questo mese)**")
    saldo = compute_saldo_ferie_permessi_mensile(richieste_dipendente, nome_dipendente, anno_sel, mese_sel)
    st.caption(f"Livello CCNL: {saldo['livello']} — {saldo['ferie_giorni_anno']:g} giorni ferie e {saldo['permesso_ore_anno']:g} ore permesso/ROL all'anno. "
               f"La maturazione è proporzionale ai mesi trascorsi dell'anno ({mese_sel}/12).")
    col_f1, col_f2, col_f3 = st.columns(3)
    col_f1.metric("Ferie maturate", f"{saldo['ferie_maturate']:.1f}gg")
    col_f2.metric("Ferie usate", f"{saldo['ferie_usate']}gg")
    col_f3.metric("Ferie residue", f"{saldo['ferie_residue']:.1f}gg")
    col_p1, col_p2, col_p3 = st.columns(3)
    col_p1.metric("Permesso maturato", f"{saldo['permesso_maturate']:.1f}h")
    col_p2.metric("Permesso usato", f"{saldo['permesso_usate']:.1f}h")
    col_p3.metric("Permesso residuo", f"{saldo['permesso_residue']:.1f}h")

    st.markdown("---")
    col_dl1, col_dl2 = st.columns(2)
    with col_dl1:
        csv_cartellino = cartellino.to_csv(index=False).encode("utf-8")
        st.download_button("📥 Scarica cartellino in CSV", data=csv_cartellino,
                            file_name=f"cartellino_{nome_dipendente}_{anno_sel}_{mese_sel:02d}.csv",
                            mime="text/csv", key=f"{key_prefix}_download_cartellino_csv")
    with col_dl2:
        if PDF_AVAILABLE:
            pdf_cartellino = generate_pdf_cartellino_mensile(nome_dipendente, anno_sel, mese_sel, cartellino, totali, saldo)
            if pdf_cartellino:
                st.download_button("📄 Scarica cartellino in PDF", data=pdf_cartellino.getvalue(),
                                    file_name=f"cartellino_{nome_dipendente}_{anno_sel}_{mese_sel:02d}.pdf",
                                    mime="application/pdf", key=f"{key_prefix}_download_cartellino_pdf")
        else:
            st.warning("⚠️ PDF non disponibile. Installa ReportLab: pip install reportlab")

def render_area_personale(username, key_prefix):
    """Area Personale del dipendente: livello CCNL e data di assunzione (impostati
    dall'amministratore, mostrati come riferimento) più i dati anagrafici che il
    dipendente può inserire o modificare da solo (codice fiscale, data di nascita,
    distanza casa-lavoro)."""
    st.subheader("🪪 Area Personale")
    dati = get_dati_personali_utente(username)

    st.markdown("**Inquadramento** _(impostato dall'amministratore)_")
    col_inq1, col_inq2 = st.columns(2)
    col_inq1.metric("Livello CCNL", dati["livello"] or "(non impostato)")
    col_inq2.metric("Data di assunzione", dati["data_assunzione"].strftime("%d/%m/%Y") if dati["data_assunzione"] else "(non impostata)")

    st.markdown("---")
    st.markdown("**I tuoi dati personali**")
    st.caption("La distanza casa-lavoro viene usata per stimare in modo più preciso la CO2 risparmiata nei giorni di smart working (tragitto casa-lavoro non percorso), nella sezione Sostenibilità.")
    with st.form(f"{key_prefix}_area_personale_form"):
        codice_fiscale_input = st.text_input("Codice Fiscale", value=dati["codice_fiscale"], max_chars=16, key=f"{key_prefix}_ap_cf")
        data_nascita_input = st.date_input("Data di Nascita", value=dati["data_nascita"] or datetime.date(1990, 1, 1),
                                            min_value=datetime.date(1930, 1, 1), max_value=datetime.date.today(), key=f"{key_prefix}_ap_dob")
        distanza_km_input = st.number_input("Distanza casa-lavoro (km, sola andata)", min_value=0.0, step=0.5,
                                             value=dati["distanza_km"], key=f"{key_prefix}_ap_dist")
        if st.form_submit_button("💾 Salva dati personali"):
            ok, errore = aggiorna_dati_personali_utente(username, codice_fiscale_input, data_nascita_input, distanza_km_input)
            if ok:
                st.success("Dati personali aggiornati.")
                st.rerun()
            else:
                st.error(errore)

# ================== INTERFACCIA STREAMLIT ==================

if not st.session_state.logged_in:
    _, col_login, _ = st.columns([1, 1.1, 1])
    with col_login:
        _render_logo_header(centrato=True)
        with st.container(border=True):
            st.subheader("🔐 Accedi a Timbra")
            username = st.text_input("Nome utente", placeholder="es. mario, admin")
            password = st.text_input("Password", type="password")
            if st.button("Accedi", use_container_width=True):
                if login(username.strip(), password):
                    st.success("Accesso effettuato con successo.")
                    st.rerun()
                else:
                    st.error("Nome utente o password errati.")
else:
    # Aggiorna automaticamente la pagina ogni 60 secondi (prima erano 5): con un
    # database locale non aveva costi, ma con un database remoto come Turso ogni
    # aggiornamento richiede una richiesta di rete, e farlo ogni 5 secondi per ogni
    # persona collegata appesantiva parecchio l'app. 60 secondi è un buon compromesso
    # per un'app di presenze aziendali (i dati non devono essere aggiornati al secondo).
    st_autorefresh(interval=60000, key="datarefresh")
    user_info = get_user_info()
    _render_logo_header(container=st.sidebar)
    st.sidebar.markdown("---")
    st.sidebar.button("Logout", on_click=logout)
    st.subheader(f"Benvenuto, {user_info['name']}!")

    df = carica_dati_db()
    
    if user_info["role"] == "admin":
        st.info("Sei connesso come amministratore. Puoi visualizzare i timbri di tutti gli utenti.")
        st.markdown("---")

        admin_page = st.sidebar.radio("Sezione amministratore", ["Dati e Presenze", "Richieste ferie/permessi", "Rettifiche timbrature", "Gestione Commesse", "Resoconto Commesse", "Grafici e Classifiche", "🌱 Sostenibilità (Smart Working)", "🧳 Trasferte e Interventi (Service)", "📅 Disponibilità Team", "Gestione Utenti DB"])

        if admin_page == "Dati e Presenze":
            st.subheader("📊 Pannello Amministrazione")

            user_names = get_user_names()
            selected_users = st.multiselect("Visualizza le timbrature di (nessuna selezione = tutti)", options=user_names, default=[])
            selected_period = st.date_input("Seleziona periodo", value=(datetime.date.today().replace(day=1), datetime.date.today()))
            if isinstance(selected_period, tuple):
                start_date, end_date = selected_period
            else:
                start_date = end_date = selected_period

            selected_month = st.selectbox("Seleziona mese", options=[f"{m} - {calendar.month_name[m]}" for m in range(1, 13)], index=datetime.date.today().month - 1)
            selected_year = st.number_input("Seleziona anno", min_value=2020, max_value=datetime.date.today().year + 1, value=datetime.date.today().year)
            selected_month_num = int(selected_month.split(" - ")[0])

            selected_user = selected_users  # lista vuota = tutti, un nome = singolo, più nomi = sottoinsieme

            df_filtrato = prepare_registro_for_export(df, selected_user=selected_user, start_date=start_date, end_date=end_date)
            if not selected_users:
                st.write("### Registro completo")
            elif len(selected_users) == 1:
                st.write(f"### Timbrature dal {start_date.isoformat()} al {end_date.isoformat()} per {selected_users[0]}")
            else:
                st.write(f"### Timbrature dal {start_date.isoformat()} al {end_date.isoformat()} per {len(selected_users)} dipendenti selezionati")

            if not df_filtrato.empty and df_filtrato["Dipendente"].nunique() > 1:
                colori_legenda = get_users_colors_map()
                legenda_html = " &nbsp; ".join(
                    f'<span style="background-color:{colori_legenda.get(dip, "#4fa8ff")}; '
                    f'color:{_testo_leggibile_su(colori_legenda.get(dip, "#4fa8ff"))}; '
                    f'padding:2px 8px; border-radius:4px;">{dip}</span>'
                    for dip in sorted(df_filtrato["Dipendente"].unique())
                )
                st.markdown(legenda_html, unsafe_allow_html=True)
                st.dataframe(evidenzia_dipendenti_per_colore(df_filtrato), use_container_width=True)
            else:
                st.dataframe(df_filtrato, use_container_width=True)

            gantt_df = build_gantt_segments(df_filtrato, selected_year, selected_month_num, user_name=None)
            if not gantt_df.empty:
                st.markdown("### Gantt presenze mensili")
                gantt_chart = alt.Chart(gantt_df).mark_bar().encode(
                    x="start:T", x2="end:T", y=alt.Y("Dipendente:N", sort=alt.EncodingSortField(field="Dipendente", order="ascending")),
                    color=alt.Color("Luogo:N"), tooltip=["Dipendente:N", "Data:T", "start:T", "end:T", "Luogo:N"]
                ).properties(height=40 * len(gantt_df["Dipendente"].unique()) + 100)
                st.altair_chart(gantt_chart, use_container_width=True)
            else:
                st.info("Nessun dato valido per il Gantt in questo mese.")

        elif admin_page == "Richieste ferie/permessi":
            st.write("### Gestione Richieste")
            richieste = carica_richieste()
            richieste = attach_request_area(richieste)
            
            # Filtra solo le richieste dirette all'admin (Approvatore_Richiesto = "admin")
            richieste_per_admin = richieste[richieste.get("Approvatore_Richiesto", "admin") == "admin"]
            richieste_in_attesa = richieste_per_admin[richieste_per_admin["Stato"] == "In attesa"]
            
            if richieste_in_attesa.empty:
                st.info("Nessuna richiesta in attesa per te. Questo significa che i responsabili stanno già gestendo le loro aree.")
            else:
                st.caption(LEGENDA_COLORI_RICHIESTE)
                st.dataframe(evidenzia_richieste_per_stato(richieste_in_attesa), use_container_width=True)
                selected_id = st.selectbox("Seleziona ID richiesta da gestire", richieste_in_attesa["ID"].tolist())
                col1, col2 = st.columns(2)
                if col1.button("Approva"):
                    aggiorna_stato_richiesta(selected_id, "Approvato")
                    st.success("Approvata!"); st.rerun()
                if col2.button("Rifiuta"):
                    aggiorna_stato_richiesta(selected_id, "Rifiutato")
                    st.success("Rifiutata!"); st.rerun()
            
            st.markdown("---")
            st.subheader("ℹ️ Richieste da Responsabili (Escalate all'Admin)")
            # Mostra le richieste dei responsabili (quelle che spettano direttamente all'admin)
            richieste_resp_escalate = richieste[(richieste.get("Approvatore_Richiesto", "admin") == "admin") & 
                                                (richieste["Stato"] != "In attesa")]
            if richieste_resp_escalate.empty:
                st.info("Nessuna richiesta escalata dai responsabili.")
            else:
                st.caption(LEGENDA_COLORI_RICHIESTE)
                st.dataframe(evidenzia_richieste_per_stato(richieste_resp_escalate), use_container_width=True)

            st.markdown("---")
            st.subheader("Saldo ferie e permessi")
            anno_saldo_admin = st.number_input("Anno", min_value=2020, max_value=datetime.date.today().year + 1, value=datetime.date.today().year, key="admin_anno_saldo_ferie")
            st.dataframe(compute_leave_balances(carica_richieste(), year=anno_saldo_admin), use_container_width=True)

        elif admin_page == "Rettifiche timbrature":
            st.write("### 📝 Gestione Rettifiche Timbrature")
            rettifiche = carica_rettifiche()
            
            rettifiche_in_attesa = rettifiche[rettifiche["Stato"] == "In attesa"]
            if rettifiche_in_attesa.empty:
                st.info("Nessuna richiesta di rettifica in attesa.")
            else:
                st.dataframe(rettifiche_in_attesa, use_container_width=True)
                
                col_ret1, col_ret2 = st.columns(2)
                with col_ret1:
                    selected_rettifica = st.selectbox("Seleziona rettifica da approvare", rettifiche_in_attesa["ID"].tolist())
                    if st.button("✅ Approva"):
                        approva_rettifica(selected_rettifica, st.session_state.username)
                        st.rerun()
                
                with col_ret2:
                    selected_rettifica_rifiuta = st.selectbox("Seleziona rettifica da rifiutare", rettifiche_in_attesa["ID"].tolist(), key="rifiuta")
                    if st.button("❌ Rifiuta"):
                        rifiuta_rettifica(selected_rettifica_rifiuta)
                        st.rerun()
            
            st.markdown("---")
            st.subheader("Storico rettifiche")
            st.dataframe(rettifiche, use_container_width=True)

        elif admin_page == "Gestione Commesse":
            render_gestione_fasi_commessa(scope_area=None, allow_create_commessa=True, attore=st.session_state.username)
            st.markdown("---")
            render_gestione_template_fasi(scope_area=None, attore=st.session_state.username)

        elif admin_page == "Resoconto Commesse":
            st.write("### 📊 Ore di Lavoro per Commessa")
            commessa_start = st.date_input("Inizio", value=datetime.date.today().replace(day=1))
            commessa_end = st.date_input("Fine", value=datetime.date.today())

            st.write("#### Per dipendente")
            hours_by_commessa = compute_hours_by_commessa(df, commessa_start, commessa_end)
            if hours_by_commessa.empty:
                st.info("Nessun dato sulle commesse disponibile.")
            else:
                st.dataframe(hours_by_commessa, use_container_width=True)

            st.markdown("---")
            st.write("#### Per fase (ore stimate vs ore effettive)")
            commesse_options = ["Tutte le commesse"] + get_commesse_names()
            commessa_filtro = st.selectbox("Filtra per commessa", options=commesse_options, key="resoconto_commessa_filtro")
            produttivita_dettaglio = compute_produttivita_commesse(
                df, commessa_start, commessa_end,
                area_filter=None,
                commessa_filter=None if commessa_filtro == "Tutte le commesse" else commessa_filtro
            )
            if produttivita_dettaglio.empty:
                st.info("Nessuna fase con ore stimate configurata. Vai su 'Gestione Commesse' per crearle.")
            else:
                st.dataframe(produttivita_dettaglio, use_container_width=True)
                chart_data = produttivita_dettaglio.melt(
                    id_vars=["Commessa", "Fase", "Area"], value_vars=["Ore_stimate", "Ore_effettive"],
                    var_name="Tipo", value_name="Ore"
                )
                bar_stima_effettiva = alt.Chart(chart_data).mark_bar().encode(
                    x=alt.X("Ore:Q"), y=alt.Y("Fase:N", sort="-x"),
                    color=alt.Color("Tipo:N"), row=alt.Row("Commessa:N"),
                    tooltip=["Commessa:N", "Fase:N", "Area:N", "Tipo:N", "Ore:Q"]
                ).properties(height=30 * len(produttivita_dettaglio) + 60)
                st.altair_chart(bar_stima_effettiva, use_container_width=True)

        elif admin_page == "Grafici e Classifiche":
            st.write("### 📈 Grafici e Classifiche per Area")
            g_start = st.date_input("Inizio periodo", value=datetime.date.today().replace(day=1), key="g_start")
            g_end = st.date_input("Fine periodo", value=datetime.date.today(), key="g_end")
            st.caption("Il costo del personale qui sotto è calcolato automaticamente in base alle ore lavorate da ciascun dipendente e al costo orario del suo livello CCNL (configurabile in 'Gestione Utenti DB' → 'Livelli CCNL'). È una stima, non un dato di paga ufficiale.")

            def compute_hours_by_area(df_all, start_date, end_date, area_filter=None):
                df_hours = compute_daily_work(df_all)
                if df_hours.empty:
                    return pd.DataFrame(columns=["Area", "Ore_lavorate"])
                df_hours = df_hours[(df_hours["Data"] >= start_date) & (df_hours["Data"] <= end_date)]
                if df_hours.empty:
                    return pd.DataFrame(columns=["Area", "Ore_lavorate"])
                df_hours["Area"] = df_hours["Dipendente"].map(get_users_area_map()).fillna("Unknown")
                if area_filter and area_filter != "Tutte le aree":
                    df_hours = df_hours[df_hours["Area"] == area_filter]
                agg = df_hours.groupby("Area", as_index=False).agg({"Ore_lavorate": "sum"})
                agg["Ore_lavorate"] = agg["Ore_lavorate"].round(2)
                return agg.sort_values("Ore_lavorate", ascending=False)

            hours_by_area = compute_hours_by_area(df, g_start, g_end)
            if hours_by_area.empty:
                st.info("Nessuna timbratura valida nel periodo selezionato.")
            else:
                st.subheader("Ore lavorate per Area")
                st.dataframe(hours_by_area, use_container_width=True)

                # Grafico a torta ore
                pie_hours = alt.Chart(hours_by_area).mark_arc().encode(
                    theta=alt.Theta(field="Ore_lavorate", type="quantitative"),
                    color=alt.Color(field="Area", type="nominal"),
                    tooltip=[alt.Tooltip("Area:N"), alt.Tooltip("Ore_lavorate:Q")]
                ).properties(height=400)
                st.altair_chart(pie_hours, use_container_width=True)

                # Calcolo automatico dei costi per area, in base a ore lavorate x costo orario del livello CCNL
                costi_per_area = compute_costo_per_area(df, g_start, g_end)
                st.subheader("Costi del personale stimati per Area")
                if costi_per_area.empty:
                    st.info("Nessun costo calcolabile nel periodo selezionato.")
                else:
                    st.dataframe(costi_per_area, use_container_width=True)
                    pie_costs = alt.Chart(costi_per_area).mark_arc().encode(
                        theta=alt.Theta(field="Costo_totale", type="quantitative"),
                        color=alt.Color(field="Area", type="nominal"),
                        tooltip=[alt.Tooltip("Area:N"), alt.Tooltip("Costo_totale:Q", format=".2f")]
                    ).properties(height=400)
                    st.altair_chart(pie_costs, use_container_width=True)

                with st.expander("Dettaglio costo stimato per dipendente"):
                    costi_dipendenti = compute_costo_dipendenti(df, g_start, g_end)
                    if costi_dipendenti.empty:
                        st.info("Nessun dato nel periodo selezionato.")
                    else:
                        st.dataframe(costi_dipendenti, use_container_width=True)

                st.markdown("---")
                st.subheader("Classifica annuale reparti più produttivi")
                sel_year = st.number_input("Seleziona anno per classifica", min_value=2000, max_value=datetime.date.today().year, value=datetime.date.today().year)
                year_start = datetime.date(sel_year, 1, 1)
                year_end = datetime.date(sel_year, 12, 31)
                ranking = compute_hours_by_area(df, year_start, year_end)
                if ranking.empty:
                    st.info("Nessun dato per l'anno selezionato.")
                else:
                    st.write("Classifica ore totali per Area (anno selezionato):")
                    st.dataframe(ranking, use_container_width=True)
                    bar = alt.Chart(ranking).mark_bar().encode(
                        x=alt.X("Ore_lavorate:Q"), y=alt.Y("Area:N", sort="-x"), tooltip=[alt.Tooltip("Area:N"), alt.Tooltip("Ore_lavorate:Q")]
                    ).properties(height=400)
                    st.altair_chart(bar, use_container_width=True)

                st.markdown("---")
                st.subheader("📈 Produttività per reparto")
                st.caption("Rapporto tra ore stimate e ore effettivamente lavorate sulle fasi delle commesse, nel periodo selezionato sopra. Sopra 100% = si è finito prima del previsto (bene); sotto 100% = si è sforato (male). Calcolata solo sulle fasi con ore stimate configurate (vedi 'Gestione Commesse').")
                produttivita_dettaglio_area = compute_produttivita_commesse(df, g_start, g_end)
                productivity_by_area = compute_produttivita_per_area(produttivita_dettaglio_area)
                if productivity_by_area.empty:
                    st.info("Nessuna fase con ore stimate lavorata nel periodo selezionato. Configura le ore stimate in 'Gestione Commesse'.")
                else:
                    top_area = productivity_by_area.iloc[0]
                    st.success(f"🏆 Reparto più produttivo nel periodo: **{top_area['Area']}** ({top_area['Produttivita_%']:.1f}% — ore stimate su ore effettive)")
                    st.dataframe(productivity_by_area, use_container_width=True)
                    bar_prod = alt.Chart(productivity_by_area).mark_bar().encode(
                        x=alt.X("Produttivita_%:Q", title="% ore stimate su ore effettive"),
                        y=alt.Y("Area:N", sort="-x"),
                        tooltip=[alt.Tooltip("Area:N"), alt.Tooltip("Ore_stimate:Q"), alt.Tooltip("Ore_effettive:Q"), alt.Tooltip("Produttivita_%:Q", title="Produttività %")]
                    ).properties(height=40 * len(productivity_by_area) + 100)
                    st.altair_chart(bar_prod, use_container_width=True)

                st.markdown("---")
                st.subheader("Riepilogo per singolo utente (visualizzazione)")
                user_list = ["Tutti"] + get_user_names()
                sel_user = st.selectbox("Seleziona utente", options=user_list)
                u_start = st.date_input("Inizio periodo (utente)", value=g_start, key="u_start")
                u_end = st.date_input("Fine periodo (utente)", value=g_end, key="u_end")

                def build_user_gantt(df_all, start_date, end_date, user_name):
                    dfn = normalize_datetime(df_all)
                    if dfn.empty: return pd.DataFrame(columns=["Dipendente", "start", "end", "Data", "Luogo"])
                    if user_name and user_name != "Tutti": dfn = dfn[dfn["Dipendente"] == user_name]
                    dfn = dfn[(dfn["Data"] >= start_date) & (dfn["Data"] <= end_date)]
                    if dfn.empty: return pd.DataFrame(columns=["Dipendente", "start", "end", "Data", "Luogo"])
                    rows = []
                    for (user, date), group in dfn.groupby(["Dipendente", "Data"]):
                        group = group.sort_values("Timestamp")
                        ingressi = group[group["Azione"] == "Ingresso"]["Timestamp"]
                        uscite = group[group["Azione"] == "Uscita"]["Timestamp"]
                        if ingressi.empty or uscite.empty: continue
                        start = ingressi.iloc[0]; end = uscite.iloc[-1]
                        if pd.isna(start) or pd.isna(end) or end <= start: continue
                        luogo = ", ".join(sorted(set(group["Luogo"].astype(str).replace("nan", "-").tolist())))
                        rows.append({"Dipendente": user, "start": start, "end": end, "Data": date, "Luogo": luogo})
                    return pd.DataFrame(rows)

                user_gantt = build_user_gantt(df, u_start, u_end, sel_user)
                if user_gantt.empty:
                    st.info("Nessun dato timbrature per l'utente nel periodo selezionato.")
                else:
                    st.markdown("**Gantt temporale per utente**")
                    gantt_chart_user = alt.Chart(user_gantt).mark_bar().encode(
                        x="start:T", x2="end:T", y=alt.Y("Data:T", axis=alt.Axis(title="Data")),
                        color=alt.Color("Luogo:N"), tooltip=["Dipendente:N", "Data:T", "start:T", "end:T", "Luogo:N"]
                    ).properties(height=40 * len(user_gantt["Data"].unique()) + 100)
                    st.altair_chart(gantt_chart_user, use_container_width=True)

                    # Statistiche riepilogo
                    df_daily = compute_daily_work(df)
                    df_user_daily = df_daily[(df_daily["Data"] >= u_start) & (df_daily["Data"] <= u_end)]
                    if sel_user != "Tutti":
                        df_user_daily = df_user_daily[df_user_daily["Dipendente"] == sel_user]

                    total_hours = round(df_user_daily["Ore_lavorate"].sum(), 2) if not df_user_daily.empty else 0.0
                    days_worked = int(df_user_daily.shape[0])
                    avg_hours = round(df_user_daily["Ore_lavorate"].mean(), 2) if days_worked else 0.0
                    timbrature_count = 0
                    phases_started = 0
                    if sel_user != "Tutti":
                        dfn_all = normalize_datetime(df)
                        tmp = dfn_all[(dfn_all["Data"] >= u_start) & (dfn_all["Data"] <= u_end) & (dfn_all["Dipendente"] == sel_user)]
                        timbrature_count = tmp.shape[0]
                        phases_started = tmp[tmp["Azione"] == "Inizio fase"].shape[0]

                    st.markdown("**Statistiche riepilogo**")
                    col_a, col_b, col_c, col_d = st.columns(4)
                    col_a.metric("Ore totali", f"{total_hours}")
                    col_b.metric("Giorni lavorati", f"{days_worked}")
                    col_c.metric("Media ore/giorno", f"{avg_hours}")
                    col_d.metric("Timbrature/azioni", f"{timbrature_count}")

                    st.markdown("**Dettaglio commesse (ore)**")
                    commessa_user = compute_hours_by_commessa(df, u_start, u_end, employee_name=None if sel_user == "Tutti" else sel_user)
                    if commessa_user.empty:
                        st.info("Nessuna commessa registrata per l'utente nel periodo.")
                    else:
                        st.dataframe(commessa_user, use_container_width=True)
                        bar_comm = alt.Chart(commessa_user).mark_bar().encode(x=alt.X("Ore_lavorate:Q"), y=alt.Y("Commessa:N", sort='-x'), tooltip=[alt.Tooltip("Commessa:N"), alt.Tooltip("Ore_lavorate:Q")]).properties(height=300)
                        st.altair_chart(bar_comm, use_container_width=True)

        elif admin_page == "🌱 Sostenibilità (Smart Working)":
            st.write("### 🌱 Sostenibilità: Vimek vs Smart Working vs Trasferta")
            st.caption("Confronto tra le ore lavorate in sede (Vimek, sia ufficio che officina), in smart working e in trasferta, con una stima di quanto lo smart working fa risparmiare all'azienda in costi di gestione della sede (energia, riscaldamento, climatizzazione, servizi igienici) e di quanta CO2 evita. La modalità di ciascuna sessione è quella scelta dal dipendente all'Ingresso.")

            sost_start = st.date_input("Inizio periodo", value=datetime.date.today().replace(day=1), key="sost_start")
            sost_end = st.date_input("Fine periodo", value=datetime.date.today(), key="sost_end")
            aree_disponibili_sost = ["Tutte le aree"] + get_area_names()
            sost_area = st.selectbox("Area", options=aree_disponibili_sost, key="sost_area")

            ore_per_luogo = compute_ore_per_luogo(df, sost_start, sost_end, area_filter=sost_area)

            if ore_per_luogo.empty:
                st.info("Nessuna timbratura valida nel periodo selezionato.")
            else:
                mappa_ore = dict(zip(ore_per_luogo["Luogo"], ore_per_luogo["Ore_lavorate"]))
                ore_vimek = mappa_ore.get("Vimek", 0.0)
                ore_smart = mappa_ore.get("Smart", 0.0)
                ore_trasferta = mappa_ore.get("Trasferta", 0.0)
                ore_totali = ore_vimek + ore_smart + ore_trasferta

                costo_orario_evitato, co2_kg_orario_evitato = get_parametri_sostenibilita()
                risparmio_euro, _ = compute_risparmio_smart_working(ore_smart, costo_orario_evitato, co2_kg_orario_evitato)

                co2_kg_per_km = get_co2_kg_per_km_pendolarismo()
                dettaglio_co2_pendolarismo, co2_evitata_kg_pendolarismo = compute_co2_risparmiata_pendolarismo(
                    df, sost_start, sost_end, area_filter=sost_area, co2_kg_per_km=co2_kg_per_km)
                # La stima basata sulla distanza casa-lavoro richiede che i dipendenti
                # l'abbiano impostata in "Area Personale": se nessuno l'ha ancora fatto,
                # si usa come ripiego la stima generica per ora di smart working.
                usa_stima_precisa = co2_evitata_kg_pendolarismo > 0
                if usa_stima_precisa:
                    co2_evitata_kg = co2_evitata_kg_pendolarismo
                else:
                    _, co2_evitata_kg = compute_risparmio_smart_working(ore_smart, costo_orario_evitato, co2_kg_orario_evitato)

                st.markdown("#### 📊 Ore lavorate per modalità")
                col_o1, col_o2, col_o3 = st.columns(3)
                col_o1.metric("🏭 Vimek", f"{ore_vimek:.1f} h", f"{(ore_vimek/ore_totali*100):.0f}%" if ore_totali else None)
                col_o2.metric("🏠 Smart", f"{ore_smart:.1f} h", f"{(ore_smart/ore_totali*100):.0f}%" if ore_totali else None)
                col_o3.metric("🚗 Trasferta", f"{ore_trasferta:.1f} h", f"{(ore_trasferta/ore_totali*100):.0f}%" if ore_totali else None)

                pie_luoghi = alt.Chart(ore_per_luogo).mark_arc().encode(
                    theta=alt.Theta(field="Ore_lavorate", type="quantitative"),
                    color=alt.Color(field="Luogo", type="nominal"),
                    tooltip=[alt.Tooltip("Luogo:N"), alt.Tooltip("Ore_lavorate:Q")]
                ).properties(height=350)
                st.altair_chart(pie_luoghi, use_container_width=True)

                st.markdown("#### 🌱 Zona Green: risparmio stimato dallo Smart Working")
                col_g1, col_g2 = st.columns(2)
                col_g1.metric("💶 Risparmio stimato costi sede", f"{risparmio_euro:.2f} €")
                col_g2.metric("🌍 CO2 evitata stimata", f"{co2_evitata_kg:.2f} kg")
                if usa_stima_precisa:
                    st.caption(f"CO2 calcolata sui giorni di smart working effettivi x la distanza casa-lavoro (andata e ritorno) che ogni dipendente ha impostato in 'Area Personale' x {co2_kg_per_km:g} kg CO2/km (fattore di emissione medio). Risparmio in € calcolato come {ore_smart:.1f} ore di smart working × {costo_orario_evitato:g} €/h evitati in costi di gestione della sede.")
                    with st.expander("Dettaglio CO2 evitata per dipendente"):
                        st.dataframe(dettaglio_co2_pendolarismo, use_container_width=True)
                else:
                    st.caption(f"Nessun dipendente ha ancora impostato la propria distanza casa-lavoro in 'Area Personale': la CO2 è quindi stimata in modo generico come {ore_smart:.1f} ore di smart working × {co2_kg_orario_evitato:g} kg CO2/h evitati. Una volta impostate le distanze, la stima diventerà più precisa (basata sui km di tragitto casa-lavoro realmente risparmiati).")

                with st.expander("⚙️ Personalizza i coefficienti di stima"):
                    st.caption("Valori di partenza derivati (in modo approssimativo) dai dati dell'Osservatorio Smart Working del Politecnico di Milano (risparmio energetico) e ISPRA (emissioni medie auto) su risparmio e riduzione di CO2 per giornata di smart working. Sono medie nazionali, non i costi/dati reali della tua azienda: personalizzale in base alle tue bollette (energia, riscaldamento, climatizzazione, servizi igienici) e al parco mezzi realmente usato dai dipendenti per il tragitto casa-lavoro.")
                    nuovo_costo_evitato = st.number_input("Risparmio stimato (€ per ora di smart working)", min_value=0.0, step=0.01, value=costo_orario_evitato, format="%.2f", key="sost_costo_evitato")
                    nuovo_co2_evitato = st.number_input("CO2 evitata stimata (kg per ora di smart working, usata se nessuno ha impostato la distanza)", min_value=0.0, step=0.01, value=co2_kg_orario_evitato, format="%.2f", key="sost_co2_evitato")
                    nuovo_co2_km = st.number_input("Fattore di emissione (kg CO2 per km percorso in auto)", min_value=0.0, step=0.001, value=co2_kg_per_km, format="%.3f", key="sost_co2_km",
                                                    help="Usato insieme alla distanza casa-lavoro di ciascun dipendente (impostata in 'Area Personale') per stimare la CO2 evitata nei giorni di smart working.")
                    if st.button("💾 Salva coefficienti", key="btn_salva_sostenibilita"):
                        ok1, errore1 = aggiorna_parametri_sostenibilita(nuovo_costo_evitato, nuovo_co2_evitato)
                        ok2, errore2 = aggiorna_co2_kg_per_km_pendolarismo(nuovo_co2_km)
                        if ok1 and ok2:
                            st.success("Coefficienti aggiornati.")
                            st.rerun()
                        else:
                            st.error(errore1 or errore2)

        elif admin_page == "Gestione Utenti DB":
            st.write("### 👥 Aggiungi o Modifica Utenti nel Database")

            # Carica elenco utenti live per gestirli
            with db_connect() as conn:
                utenti_db = pd.read_sql("SELECT username, nome, role, area, posizione, livello, colore, data_assunzione, codice_fiscale, data_nascita, distanza_km FROM utenti", conn)

            livelli_disponibili = get_livelli_disponibili()

            user_action = st.radio("Seleziona azione", ["Visualizza tutti", "Aggiungi utente", "Modifica password/ruolo", "Elimina utente", "Livelli CCNL (ferie/permessi)"])

            if user_action == "Visualizza tutti":
                st.dataframe(utenti_db, use_container_width=True)

            elif user_action == "Aggiungi utente":
                new_username = st.text_input("Username (univoco)").lower().strip()
                new_name = st.text_input("Nome completo")
                new_password = st.text_input("Password", type="password")
                new_role = st.selectbox("Ruolo", ["user", "admin", "responsabile"])
                new_area = st.text_input("Area").strip()
                new_position = st.text_input("Posizione").strip()
                new_level = st.selectbox("Livello CCNL", options=livelli_disponibili, help="Determina le ferie/permessi annuali maturati e il costo orario stimato (vedi 'Livelli CCNL' qui sotto).") if livelli_disponibili else ""
                new_hire_date = st.date_input("Data di assunzione", value=datetime.date.today(), key="new_user_hire_date", help="Usata per calcolare correttamente la maturazione di ferie e permessi dal mese di assunzione invece che da gennaio.")
                new_color = st.color_picker("Colore", value="#4fa8ff" if new_role != "responsabile" else "#2f5d62")

                if st.button("Salva nuovo utente nel DB"):
                    if not new_username or not new_password:
                        st.error("Username e Password sono obbligatori.")
                    else:
                        try:
                            with db_connect() as conn:
                                c = conn.cursor()
                                c.execute("INSERT INTO utenti (username, nome, password, role, area, posizione, livello, colore, data_assunzione) VALUES (?,?,?,?,?,?,?,?,?)",
                                          (new_username, new_name, hash_password(new_password), new_role, new_area, new_position, new_level, new_color, new_hire_date.isoformat()))
                                conn.commit()
                            st.success(f"Utente {new_name} aggiunto!")
                            st.rerun()
                        except db_error_classes("IntegrityError"):
                            st.error("Errore: Questo username esiste già.")

            elif user_action == "Modifica password/ruolo":
                selected_username = st.selectbox("Seleziona username", utenti_db["username"].tolist())
                new_password = st.text_input("Nuova password (lascia vuoto per non cambiare)", type="password")
                new_role = st.selectbox("Nuovo Ruolo", ["user", "admin", "responsabile"])
                livello_attuale = utenti_db.loc[utenti_db["username"] == selected_username, "livello"].iloc[0] if selected_username in utenti_db["username"].tolist() else ""
                opzioni_livello = livelli_disponibili if livelli_disponibili else [""]
                indice_livello = opzioni_livello.index(livello_attuale) if livello_attuale in opzioni_livello else 0
                new_level = st.selectbox("Livello CCNL", options=opzioni_livello, index=indice_livello, key="modifica_livello_utente")
                data_assunzione_attuale = utenti_db.loc[utenti_db["username"] == selected_username, "data_assunzione"].iloc[0] if selected_username in utenti_db["username"].tolist() else ""
                try:
                    valore_data_assunzione = datetime.datetime.strptime(data_assunzione_attuale, "%Y-%m-%d").date() if data_assunzione_attuale else datetime.date.today()
                except (TypeError, ValueError):
                    valore_data_assunzione = datetime.date.today()
                new_hire_date = st.date_input("Data di assunzione", value=valore_data_assunzione, key="modifica_hire_date")
                new_color = st.color_picker("Nuovo colore", value="#4fa8ff")

                if st.button("Aggiorna utente"):
                    with db_connect() as conn:
                        c = conn.cursor()
                        if new_password:
                            c.execute("UPDATE utenti SET password=?, role=?, livello=?, colore=?, data_assunzione=? WHERE username=?", (hash_password(new_password), new_role, new_level, new_color, new_hire_date.isoformat(), selected_username))
                        else:
                            c.execute("UPDATE utenti SET role=?, livello=?, colore=?, data_assunzione=? WHERE username=?", (new_role, new_level, new_color, new_hire_date.isoformat(), selected_username))
                        conn.commit()
                    st.success("Utente aggiornato!")
                    st.rerun()

            elif user_action == "Elimina utente":
                to_delete = st.selectbox("Utente da eliminare", [u for u in utenti_db["username"].tolist() if u != "admin"])
                if st.button("Elimina definitivamente"):
                    with db_connect() as conn:
                        c = conn.cursor()
                        c.execute("DELETE FROM utenti WHERE username=?", (to_delete,))
                        conn.commit()
                    st.success("Utente eliminato dal database!")
                    st.rerun()

            elif user_action == "Livelli CCNL (ferie/permessi)":
                st.write("#### 🏭 Ferie e permessi annuali per livello CCNL")
                st.caption("Nel settore metalmeccanico il livello determina normalmente l'inquadramento e la paga, non le ferie o i permessi: qui puoi comunque impostare i valori realmente in vigore in azienda per ciascun livello. I nuovi utenti maturano ferie/permessi in proporzione ai mesi trascorsi nell'anno, in base a questi valori.")
                livelli_dettaglio = get_livelli_ferie_permessi_dettaglio()
                if livelli_dettaglio.empty:
                    st.info("Nessun livello configurato.")
                else:
                    st.dataframe(livelli_dettaglio, use_container_width=True)

                st.markdown("**Aggiungi o modifica un livello**")
                modalita_livello = st.radio("Livello", ["Esistente", "Nuovo"], horizontal=True, key="livello_ccnl_mode") if livelli_disponibili else "Nuovo"
                if modalita_livello == "Esistente" and livelli_disponibili:
                    livello_sel = st.selectbox("Seleziona livello", options=livelli_disponibili, key="livello_ccnl_select")
                    ferie_attuali, permesso_attuali = get_ferie_permessi_per_livello(livello_sel)
                    costo_attuale = get_costo_orario_per_livello(livello_sel)
                else:
                    livello_sel = st.text_input("Nome nuovo livello (es. 2S)", key="livello_ccnl_nuovo").strip()
                    ferie_attuali, permesso_attuali = float(DEFAULT_FERIE_GIORNI), float(DEFAULT_PERMESSO_ORE)
                    costo_attuale = float(DEFAULT_COSTO_ORARIO)

                col_liv1, col_liv2, col_liv3 = st.columns(3)
                with col_liv1:
                    nuove_ferie_livello = st.number_input("Ferie (giorni/anno)", min_value=0.0, step=0.5, value=ferie_attuali, key=f"ferie_livello_{livello_sel}")
                with col_liv2:
                    nuovo_permesso_livello = st.number_input("Permesso/ROL (ore/anno)", min_value=0.0, step=1.0, value=permesso_attuali, key=f"permesso_livello_{livello_sel}")
                with col_liv3:
                    nuovo_costo_livello = st.number_input("Costo orario (€/h)", min_value=0.0, step=0.5, value=costo_attuale, key=f"costo_livello_{livello_sel}", help="Stima del costo orario aziendale per un dipendente di questo livello: usato per calcolare automaticamente il costo del personale in 'Grafici e Classifiche'. Non è un dato di paga ufficiale: personalizzalo in base ai costi reali (retribuzione, contributi, TFR, ecc.).")

                col_liv_btn1, col_liv_btn2 = st.columns(2)
                with col_liv_btn1:
                    if st.button("💾 Salva livello", key="btn_salva_livello"):
                        ok, errore = aggiorna_livello_ferie_permessi(livello_sel, nuove_ferie_livello, nuovo_permesso_livello, nuovo_costo_livello)
                        if ok:
                            st.success(f"Livello '{livello_sel}' salvato.")
                            st.rerun()
                        else:
                            st.error(errore)
                with col_liv_btn2:
                    if modalita_livello == "Esistente" and livelli_disponibili and st.button("🗑️ Elimina livello", key="btn_elimina_livello"):
                        elimina_livello_ferie_permessi(livello_sel)
                        st.success(f"Livello '{livello_sel}' eliminato. Gli utenti con questo livello useranno i valori di default aziendali.")
                        st.rerun()

        if admin_page == "🧳 Trasferte e Interventi (Service)":
            render_gestione_trasferte_service(user_info["name"], key_prefix="admin")
            st.markdown("---")
            st.markdown("**Report interventi ricevuti**")
            report_tutti = get_report_interventi_dettaglio()
            if report_tutti.empty:
                st.info("Nessun report intervento inviato finora.")
            else:
                st.dataframe(report_tutti, use_container_width=True)
                opzioni_foto = [f"#{id_} - {dip} ({data_rep})" for id_, dip, data_rep in
                                 zip(report_tutti["ID"], report_tutti["Dipendente"], report_tutti["Data report"])]
                mappa_report_id = dict(zip(opzioni_foto, report_tutti["ID"].tolist()))
                report_da_vedere = st.selectbox("Vedi foto del report", ["(nessuno)"] + opzioni_foto, key="admin_report_foto_select")
                if report_da_vedere != "(nessuno)":
                    foto_report = get_foto_report(mappa_report_id[report_da_vedere])
                    if not foto_report:
                        st.info("Nessuna foto allegata a questo report.")
                    else:
                        for nome_file, dati_foto in foto_report:
                            st.image(dati_foto, caption=nome_file, use_container_width=True)

        if admin_page == "📅 Disponibilità Team":
            render_disponibilita_team(key_prefix="admin")

        if not df.empty and admin_page == "Dati e Presenze":
            st.markdown("---")
            export_df = prepare_registro_for_export(df, selected_user=selected_user, start_date=start_date, end_date=end_date)
            if not export_df.empty:
                col_export1, col_export2 = st.columns(2)
                with col_export1:
                    st.download_button("📥 ESPORTA REGISTRO IN CSV", data=export_df.to_csv(index=False).encode('utf-8'), file_name=f"registro_ore_{datetime.date.today()}.csv", mime="text/csv")
                
                with col_export2:
                    if PDF_AVAILABLE:
                        pdf_buffer = generate_pdf_report("Report Generale", start_date, end_date, export_df)
                        if pdf_buffer:
                            st.download_button("📄 ESPORTA REPORT PDF", data=pdf_buffer.getvalue(), file_name=f"rapporto_{datetime.date.today()}.pdf", mime="application/pdf")
                    else:
                        st.warning("⚠️ PDF non disponibile. Installa ReportLab: pip install reportlab")

    elif user_info["role"] == "responsabile":
        # --- LATO RESPONSABILE CON DOPPIO ACCESSO ---
        st.info("🔧 Sei connesso come Responsabile. Accedi a funzioni personali e di gestione team.")
        st.markdown("---")
        
        # Selettore principale: Mie funzioni vs Gestione Team (più Trasferte Service
        # se il responsabile fa parte dell'area Service)
        opzioni_modalita = ["Mie funzioni personali", "Gestione Team"]
        if is_area_service(user_info["area"]):
            opzioni_modalita.append("🧳 Trasferte Service")
        modalita = st.sidebar.radio("📌 Modalità", opzioni_modalita)

        if modalita == "Mie funzioni personali":
            # *** REPLICA DELLA SEZIONE UTENTE PER IL RESPONSABILE ***
            dipendente_scelto = user_info["name"]
            pagina_utente = st.sidebar.radio("Funzione personale", ["Profilo", "Timbrature", "Riepilogo personale", "Report mensile", "📷 Report Intervento", "Richiesta ferie/permessi", "Richiesta rettifica"])

            if pagina_utente == "Profilo":
                st.subheader("👤 Profilo personale")
                st.write(f"**Nome:** {dipendente_scelto}")
                st.write(f"**Area:** {user_info['area']}")
                st.write(f"**Ruolo:** {user_info['role']}")
                st.markdown("---")
                with st.form("change_password_form_resp"):
                    new_pw = st.text_input("Nuova password", type="password")
                    if st.form_submit_button("Cambia password"):
                        if new_pw:
                            with db_connect() as conn:
                                c = conn.cursor()
                                c.execute("UPDATE utenti SET password=? WHERE username=?", (hash_password(new_pw), st.session_state.username))
                                conn.commit()
                            st.success("Password cambiata con successo!")
                st.markdown("---")
                render_area_personale(st.session_state.username, key_prefix="resp")
                    
            elif pagina_utente == "Timbrature":
                st.subheader(f"Area Timbratura: {dipendente_scelto}")
                luogo_scelto = st.radio("Modalità di lavoro:", ["Vimek", "Smart", "Trasferta"], horizontal=True)
                dove_trasferta = st.text_input("📍 Dove?") if luogo_scelto == "Trasferta" else ""

                # Verifica stato dell'ultima timbratura per disabilitare pulsanti illogici
                last_action = get_last_timbratura(dipendente_scelto)
                
                # Ingresso - disabilitato se c'è già un ingresso attivo
                ingresso_enabled = last_action not in ["Ingresso", "Inizio fase"]
                ingresso_help = "✅ Pronto" if ingresso_enabled else "❌ Hai già un ingresso attivo"
                
                # Uscita - disabilitato se non c'è un ingresso
                uscita_enabled = last_action not in ["Uscita", None]
                uscita_help = "✅ Pronto" if uscita_enabled else "❌ Fai prima un ingresso"
                
                col1, col2 = st.columns(2)
                if col1.button("🚀 INGRESSO", use_container_width=True, disabled=not ingresso_enabled, help=ingresso_help): 
                    registra_orario(dipendente_scelto, "Ingresso", luogo_scelto, dove_trasferta)
                if col2.button("🛑 USCITA", use_container_width=True, disabled=not uscita_enabled, help=uscita_help): 
                    registra_orario(dipendente_scelto, "Uscita", luogo_scelto, dove_trasferta)

                st.markdown("---")
                st.subheader("☕ Pausa Pranzo")
                
                # Pausa - disabilitata se non c'è un ingresso o fase attiva
                pausa_enabled = last_action in ["Ingresso", "Fine fase"]
                fine_pausa_enabled = last_action == "Inizio Pausa"
                
                col_pausa1, col_pausa2 = st.columns(2)
                if col_pausa1.button("▶️ INIZIO PAUSA", use_container_width=True, disabled=not pausa_enabled, help="Inizia la pausa pranzo"):
                    registra_orario(dipendente_scelto, "Inizio Pausa", luogo_scelto, dove_trasferta)
                if col_pausa2.button("⏹️ FINE PAUSA", use_container_width=True, disabled=not fine_pausa_enabled, help="Termina la pausa pranzo"):
                    registra_orario(dipendente_scelto, "Fine Pausa", luogo_scelto, dove_trasferta)

                st.markdown("---")
                st.subheader("🧩 Fase Lavorativa")
                fase_aperta_resp = get_fase_aperta(dipendente_scelto)
                if fase_aperta_resp:
                    st.info(f"Fase attualmente aperta: **{fase_aperta_resp[0]} → {fase_aperta_resp[1]}**")

                commesse_disponibili_resp = get_commesse_names()
                if not commesse_disponibili_resp:
                    st.warning("Nessuna commessa configurata. Chiedi all'admin di crearne una prima di avviare una fase.")
                    commessa_scelta, fase_scelta = None, None
                else:
                    commessa_scelta = st.selectbox("🏗️ Commessa", commesse_disponibili_resp, key="resp_commessa_timbra")
                    fasi_disponibili_resp = get_fasi_per_commessa_e_area(commessa_scelta, user_info["area"])
                    if not fasi_disponibili_resp:
                        st.warning(f"Nessuna fase configurata per '{commessa_scelta}' nel tuo reparto. Aggiungila in 'Gestione fasi commessa'.")
                        fase_scelta = None
                    else:
                        fase_scelta = st.selectbox("Fase", fasi_disponibili_resp, key="resp_fase_timbra")

                # Fase - disabilitata se non c'è un ingresso, o se manca una commessa/fase valida
                fase_enabled = last_action in ["Ingresso", "Fine fase"] and bool(commessa_scelta) and bool(fase_scelta)
                fine_fase_enabled = last_action == "Inizio fase" and fase_aperta_resp is not None

                col3, col4 = st.columns(2)
                if col3.button("▶️ AVVIA FASE", use_container_width=True, disabled=not fase_enabled, help="Registra inizio fase"):
                    registra_orario(dipendente_scelto, "Inizio fase", luogo_scelto, dove_trasferta, commessa_scelta, fase_scelta)
                if col4.button("⏹️ CHIUDI FASE", use_container_width=True, disabled=not fine_fase_enabled, help="Registra fine fase"):
                    commessa_chiudi_resp, fase_chiudi_resp = fase_aperta_resp
                    registra_orario(dipendente_scelto, "Fine fase", luogo_scelto, dove_trasferta, commessa_chiudi_resp, fase_chiudi_resp)

                st.markdown("---")
                st.write("Le tue timbrature di oggi:")
                st.dataframe(get_day_records(df, datetime.date.today(), user_name=dipendente_scelto), use_container_width=True)
            
            elif pagina_utente == "Riepilogo personale":
                st.subheader("📊 Riepilogo Ore Lavorate")
                up_start = st.date_input("Inizio", value=datetime.date.today().replace(day=1), key="resp_up_start")
                up_end = st.date_input("Fine", value=datetime.date.today(), key="resp_up_end")
                
                if not df.empty:
                    df_user = df[df["Dipendente"] == dipendente_scelto]
                    df_user = prepare_registro_for_export(df_user, selected_user=dipendente_scelto, start_date=up_start, end_date=up_end)
                    hours_user = compute_daily_work(df_user)
                    hours_period = hours_user[(hours_user["Data"] >= up_start) & (hours_user["Data"] <= up_end)]
                    
                    if not hours_period.empty:
                        st.dataframe(hours_period, use_container_width=True)
                        st.metric("Totale ore", f"{hours_period['Ore_lavorate'].sum():.2f}h")
                    else:
                        st.info("Nessuna timbratura nel periodo.")
                        
                    st.markdown("**Dettaglio commesse (ore)**")
                    commessa_user = compute_hours_by_commessa(df, up_start, up_end, employee_name=dipendente_scelto)
                    if commessa_user.empty:
                        st.info("Nessuna commessa registrata nel periodo.")
                    else:
                        st.dataframe(commessa_user, use_container_width=True)
                        bar_comm = alt.Chart(commessa_user).mark_bar().encode(x=alt.X("Ore_lavorate:Q"), y=alt.Y("Commessa:N", sort='-x'), tooltip=[alt.Tooltip("Commessa:N"), alt.Tooltip("Ore_lavorate:Q")]).properties(height=300)
                        st.altair_chart(bar_comm, use_container_width=True)
                
                st.markdown("---")
                st.subheader("📥 Esportazione Rapporto")
                
                if st.button("Scarica report PDF"):
                    if PDF_AVAILABLE:
                        pdf_buffer = generate_pdf_report(f"Report {dipendente_scelto}", up_start, up_end, prepare_registro_for_export(df, selected_user=dipendente_scelto, start_date=up_start, end_date=up_end))
                        if pdf_buffer:
                            st.download_button("📄 SCARICA REPORT PDF", data=pdf_buffer.getvalue(), file_name=f"rapporto_{dipendente_scelto}_{up_end}.pdf", mime="application/pdf")
                    else:
                        st.info("ℹ️ Per abilitare i report PDF, installa: pip install reportlab")

            elif pagina_utente == "Report mensile":
                render_report_mensile(dipendente_scelto, df, key_prefix="resp")

            elif pagina_utente == "📷 Report Intervento":
                render_report_intervento(dipendente_scelto, df, key_prefix="resp")

            elif pagina_utente == "Richiesta ferie/permessi":
                st.subheader("Richiesta ferie / permessi")
                tipo_richiesta = st.radio("Tipo", ["Ferie", "Permesso"])
                data_inizio = st.date_input("Inizio")
                data_fine = st.date_input("Fine") if tipo_richiesta == "Ferie" else data_inizio
                ore_permesso = st.number_input("Ore", 0.5, 8.0, 0.5) if tipo_richiesta == "Permesso" else 8
                motivo = st.text_area("Motivo")
                
                if st.button("Invia richiesta"):
                    salva_richiesta(dipendente_scelto, tipo_richiesta, data_inizio, data_fine, ore_permesso, motivo, ruolo_richiedente=user_info["role"])
                    st.rerun()

                st.write("### Le tue richieste")
                st.caption(LEGENDA_COLORI_RICHIESTE)
                mie_richieste = carica_richieste()
                mie_richieste = mie_richieste[mie_richieste["Dipendente"] == dipendente_scelto]
                st.dataframe(evidenzia_richieste_per_stato(mie_richieste), use_container_width=True)

            elif pagina_utente == "Richiesta rettifica":
                st.subheader("📝 Richiesta Rettifica Timbratura")
                st.info("Hai dimenticato di timbrare? Richiedi una rettifica all'amministratore.")
                azione = st.selectbox("Azione", ["Ingresso", "Uscita", "Inizio fase", "Fine fase"])
                data_rettifica = st.date_input("Data da rettificare")
                ora_rettifica = st.time_input("Ora")
                motivo_rettifica = st.text_area("Motivo della rettifica")
                if st.button("Invia richiesta di rettifica"):
                    salva_richiesta_rettifica(dipendente_scelto, azione, data_rettifica, ora_rettifica.isoformat(), motivo_rettifica)
                    st.rerun()
                
                st.write("### Tutte le tue rettifiche")
                st.dataframe(carica_rettifiche()[carica_rettifiche()["Dipendente"] == dipendente_scelto], use_container_width=True)
        
        elif modalita == "Gestione Team":
            # *** SEZIONE GESTIONE TEAM (ORIGINALE DEL RESPONSABILE) ***
            st.subheader("📊 Pannello Responsabile - Gestione Team")

            area_responsabile = user_info["area"]
            dipendenti_area = get_users_in_area(area_responsabile)

            resp_page = st.sidebar.radio("Sezione team", ["Timbrature del team", "Richieste ferie/permessi", "Rettifiche timbrature", "Statistiche area", "Resoconto Commesse", "Gestione fasi commessa", "📅 Disponibilità Team"])

            if resp_page == "Timbrature del team":
                st.subheader(f"📋 Timbrature - Area: {area_responsabile}")
                df_filtrato = df[df["Dipendente"].isin(dipendenti_area)] if not df.empty else pd.DataFrame()
                
                selected_dipendente = st.selectbox("Seleziona dipendente", dipendenti_area)
                selected_date = st.date_input("Seleziona data", value=datetime.date.today())
                
                if not df_filtrato.empty:
                    st.dataframe(get_day_records(df_filtrato, selected_date, user_name=selected_dipendente), use_container_width=True)
                else:
                    st.info("Nessun dato disponibile.")
            
            elif resp_page == "Richieste ferie/permessi":
                st.subheader("✅ Gestione Richieste Ferie/Permessi")
                richieste = carica_richieste()
                richieste_area = richieste[richieste["Dipendente"].isin(dipendenti_area)]
                
                # Filtra solo le richieste che spettano a questo responsabile
                richieste_da_approvare = richieste_area[(richieste_area["Stato"] == "In attesa") & 
                                                        (richieste_area["Approvatore_Richiesto"] == user_info["name"])]
                if richieste_da_approvare.empty:
                    st.info("Nessuna richiesta in attesa da approvare.")
                else:
                    st.caption(LEGENDA_COLORI_RICHIESTE)
                    st.dataframe(evidenzia_richieste_per_stato(richieste_da_approvare), use_container_width=True)
                    selected_id = st.selectbox("Seleziona richiesta da gestire", richieste_da_approvare["ID"].tolist())
                    col1, col2 = st.columns(2)
                    if col1.button("✅ Approva"):
                        aggiorna_stato_richiesta(selected_id, "Approvato")
                        st.success("Richiesta approvata!"); st.rerun()
                    if col2.button("❌ Rifiuta"):
                        aggiorna_stato_richiesta(selected_id, "Rifiutato")
                        st.success("Richiesta rifiutata!"); st.rerun()
                
                st.markdown("---")
                st.subheader("Saldo ferie e permessi - Team")
                st.dataframe(compute_leave_balances(richieste_area, year=datetime.date.today().year, area=area_responsabile), use_container_width=True)
            
            elif resp_page == "Rettifiche timbrature":
                st.subheader("📝 Rettifiche Timbrature")
                rettifiche = carica_rettifiche()
                rettifiche_area = rettifiche[rettifiche["Dipendente"].isin(dipendenti_area)]
                
                rettifiche_in_attesa = rettifiche_area[rettifiche_area["Stato"] == "In attesa"]
                if rettifiche_in_attesa.empty:
                    st.info("Nessuna rettifica in attesa dal tuo team.")
                else:
                    st.dataframe(rettifiche_in_attesa, use_container_width=True)
                    col_ret1, col_ret2 = st.columns(2)
                    with col_ret1:
                        selected_rettifica = st.selectbox("Seleziona rettifica da approvare", rettifiche_in_attesa["ID"].tolist())
                        if st.button("✅ Approva"):
                            approva_rettifica(selected_rettifica, st.session_state.username)
                            st.rerun()
                    with col_ret2:
                        selected_rettifica_rifiuta = st.selectbox("Seleziona rettifica da rifiutare", rettifiche_in_attesa["ID"].tolist(), key="rifiuta_resp")
                        if st.button("❌ Rifiuta"):
                            rifiuta_rettifica(selected_rettifica_rifiuta)
                            st.rerun()
            
            elif resp_page == "Gestione fasi commessa":
                render_gestione_fasi_commessa(scope_area=area_responsabile, allow_create_commessa=False, attore=st.session_state.username)
                st.markdown("---")
                render_gestione_template_fasi(scope_area=area_responsabile, attore=st.session_state.username)

            elif resp_page == "Resoconto Commesse":
                st.write(f"### 📊 Ore di Lavoro per Commessa — Area {area_responsabile}")
                resp_commessa_start = st.date_input("Inizio", value=datetime.date.today().replace(day=1), key="resp_commessa_start")
                resp_commessa_end = st.date_input("Fine", value=datetime.date.today(), key="resp_commessa_end")

                commesse_options_resp = ["Tutte le commesse"] + get_commesse_names()
                commessa_filtro_resp = st.selectbox("Filtra per commessa", options=commesse_options_resp, key="resp_commessa_filtro")
                produttivita_resp = compute_produttivita_commesse(
                    df, resp_commessa_start, resp_commessa_end,
                    area_filter=area_responsabile,
                    commessa_filter=None if commessa_filtro_resp == "Tutte le commesse" else commessa_filtro_resp
                )
                if produttivita_resp.empty:
                    st.info("Nessuna fase con ore stimate configurata per il tuo reparto. Vai su 'Gestione fasi commessa' per crearle.")
                else:
                    st.dataframe(produttivita_resp, use_container_width=True)
                    chart_data_resp = produttivita_resp.melt(
                        id_vars=["Commessa", "Fase", "Area"], value_vars=["Ore_stimate", "Ore_effettive"],
                        var_name="Tipo", value_name="Ore"
                    )
                    bar_resp = alt.Chart(chart_data_resp).mark_bar().encode(
                        x=alt.X("Ore:Q"), y=alt.Y("Fase:N", sort="-x"),
                        color=alt.Color("Tipo:N"), row=alt.Row("Commessa:N"),
                        tooltip=["Commessa:N", "Fase:N", "Tipo:N", "Ore:Q"]
                    ).properties(height=30 * len(produttivita_resp) + 60)
                    st.altair_chart(bar_resp, use_container_width=True)

            elif resp_page == "Statistiche area":
                st.subheader(f"📊 Statistiche - Area {area_responsabile}")
                stat_start = st.date_input("Inizio periodo", value=datetime.date.today().replace(day=1))
                stat_end = st.date_input("Fine periodo", value=datetime.date.today())
                
                df_area = df[df["Dipendente"].isin(dipendenti_area)] if not df.empty else pd.DataFrame()
                hours_by_dipendente = compute_daily_work(df_area) if not df_area.empty else pd.DataFrame()
                
                if not hours_by_dipendente.empty:
                    hours_by_dipendente = hours_by_dipendente[(hours_by_dipendente["Data"] >= stat_start) & (hours_by_dipendente["Data"] <= stat_end)]
                    st.dataframe(hours_by_dipendente, use_container_width=True)
                else:
                    st.info("Nessun dato disponibile nel periodo selezionato.")

                st.markdown("---")
                st.subheader("📈 Produttività del tuo reparto")
                st.caption("Rapporto tra ore stimate e ore effettivamente lavorate sulle fasi delle commesse, nel periodo selezionato sopra. Sopra 100% = si è finito prima del previsto (bene); sotto 100% = si è sforato (male).")
                produttivita_dettaglio_resp = compute_produttivita_commesse(df, stat_start, stat_end)
                productivity_all_areas = compute_produttivita_per_area(produttivita_dettaglio_resp)
                ranking_info = get_area_productivity_ranking(productivity_all_areas, area_responsabile)
                if ranking_info is None:
                    st.info("Nessun dato sufficiente per calcolare la produttività del tuo reparto nel periodo selezionato.")
                else:
                    col_rank1, col_rank2, col_rank3 = st.columns(3)
                    col_rank1.metric("Il tuo reparto", f"{ranking_info['own_pct']:.1f}%")
                    col_rank2.metric("Media aziendale", f"{ranking_info['company_avg_pct']:.1f}%")
                    col_rank3.metric("Posizione in classifica", f"{ranking_info['position']}° su {ranking_info['total_areas']}")
                    if ranking_info["own_pct"] >= ranking_info["company_avg_pct"]:
                        st.success("Il tuo reparto è sopra la media aziendale in questo periodo.")
                    else:
                        st.info("Il tuo reparto è sotto la media aziendale in questo periodo.")

            elif resp_page == "📅 Disponibilità Team":
                render_disponibilita_team(area_default=area_responsabile, forza_area=True, key_prefix="resp")

        elif modalita == "🧳 Trasferte Service":
            render_gestione_trasferte_service(user_info["name"], key_prefix="resp_service")
            st.markdown("---")
            st.markdown("**Report interventi ricevuti**")
            report_area_service = get_report_interventi_dettaglio()
            if report_area_service.empty:
                st.info("Nessun report intervento inviato finora.")
            else:
                st.dataframe(report_area_service, use_container_width=True)

    else:
        # --- LATO UTENTE ---
        dipendente_scelto = user_info["name"]
        pagina_utente_opzioni = ["Profilo", "Timbrature", "Riepilogo personale", "Report mensile", "📷 Report Intervento", "Richiesta ferie/permessi", "Richiesta rettifica"]
        if is_area_service(user_info["area"]):
            pagina_utente_opzioni.append("🧳 Trasferte Service")
        pagina_utente = st.sidebar.radio("Funzione utente", pagina_utente_opzioni)

        if pagina_utente == "Profilo":
            st.subheader("👤 Profilo personale")
            st.write(f"**Nome:** {dipendente_scelto}")
            st.write(f"**Area:** {user_info['area']}")
            st.write(f"**Ruolo:** {user_info['role']}")
            st.markdown("---")
            with st.form("change_password_form"):
                new_pw = st.text_input("Nuova password", type="password")
                if st.form_submit_button("Cambia password"):
                    if new_pw:
                        with db_connect() as conn:
                            c = conn.cursor()
                            c.execute("UPDATE utenti SET password=? WHERE username=?", (hash_password(new_pw), st.session_state.username))
                            conn.commit()
                        st.success("Password cambiata con successo!")
            st.markdown("---")
            render_area_personale(st.session_state.username, key_prefix="user")

        elif pagina_utente == "Timbrature":
            st.subheader(f"Area Timbratura: {dipendente_scelto}")
            luogo_scelto = st.radio("Modalità di lavoro:", ["Vimek", "Smart", "Trasferta"], horizontal=True)
            dove_trasferta = st.text_input("📍 Dove?") if luogo_scelto == "Trasferta" else ""

            # Verifica stato dell'ultima timbratura per disabilitare pulsanti illogici
            last_action = get_last_timbratura(dipendente_scelto)
            
            # Ingresso - disabilitato se c'è già un ingresso attivo
            ingresso_enabled = last_action not in ["Ingresso", "Inizio fase"]
            ingresso_help = "✅ Pronto" if ingresso_enabled else "❌ Hai già un ingresso attivo"
            
            # Uscita - disabilitato se non c'è un ingresso
            uscita_enabled = last_action not in ["Uscita", None]
            uscita_help = "✅ Pronto" if uscita_enabled else "❌ Fai prima un ingresso"
            
            col1, col2 = st.columns(2)
            if col1.button("🚀 INGRESSO", use_container_width=True, disabled=not ingresso_enabled, help=ingresso_help): 
                registra_orario(dipendente_scelto, "Ingresso", luogo_scelto, dove_trasferta)
            if col2.button("🛑 USCITA", use_container_width=True, disabled=not uscita_enabled, help=uscita_help): 
                registra_orario(dipendente_scelto, "Uscita", luogo_scelto, dove_trasferta)

            st.markdown("---")
            st.subheader("☕ Pausa Pranzo")
            
            # Pausa - disabilitata se non c'è un ingresso o fase attiva
            pausa_enabled = last_action in ["Ingresso", "Fine fase"]
            fine_pausa_enabled = last_action == "Inizio Pausa"
            
            col_pausa1, col_pausa2 = st.columns(2)
            if col_pausa1.button("▶️ INIZIO PAUSA", use_container_width=True, disabled=not pausa_enabled, help="Inizia la pausa pranzo"):
                registra_orario(dipendente_scelto, "Inizio Pausa", luogo_scelto, dove_trasferta)
            if col_pausa2.button("⏹️ FINE PAUSA", use_container_width=True, disabled=not fine_pausa_enabled, help="Termina la pausa pranzo"):
                registra_orario(dipendente_scelto, "Fine Pausa", luogo_scelto, dove_trasferta)

            st.markdown("---")
            st.subheader("🧩 Fase Lavorativa")
            fase_aperta_utente = get_fase_aperta(dipendente_scelto)
            if fase_aperta_utente:
                st.info(f"Fase attualmente aperta: **{fase_aperta_utente[0]} → {fase_aperta_utente[1]}**")

            commesse_disponibili_utente = get_commesse_names()
            if not commesse_disponibili_utente:
                st.warning("Nessuna commessa configurata. Chiedi all'admin di crearne una prima di avviare una fase.")
                commessa_scelta, fase_scelta = None, None
            else:
                commessa_scelta = st.selectbox("🏗️ Commessa", commesse_disponibili_utente, key="utente_commessa_timbra")
                fasi_disponibili_utente = get_fasi_per_commessa_e_area(commessa_scelta, user_info["area"])
                if not fasi_disponibili_utente:
                    st.warning(f"Nessuna fase configurata per '{commessa_scelta}' nel tuo reparto. Chiedi al tuo responsabile di aggiungerla.")
                    fase_scelta = None
                else:
                    fase_scelta = st.selectbox("Fase", fasi_disponibili_utente, key="utente_fase_timbra")

            # Fase - disabilitata se non c'è un ingresso, o se manca una commessa/fase valida
            fase_enabled = last_action in ["Ingresso", "Fine fase"] and bool(commessa_scelta) and bool(fase_scelta)
            fine_fase_enabled = last_action == "Inizio fase" and fase_aperta_utente is not None

            col3, col4 = st.columns(2)
            if col3.button("▶️ AVVIA FASE", use_container_width=True, disabled=not fase_enabled, help="Registra inizio fase"):
                registra_orario(dipendente_scelto, "Inizio fase", luogo_scelto, dove_trasferta, commessa_scelta, fase_scelta)
            if col4.button("⏹️ CHIUDI FASE", use_container_width=True, disabled=not fine_fase_enabled, help="Registra fine fase"):
                commessa_chiudi_utente, fase_chiudi_utente = fase_aperta_utente
                registra_orario(dipendente_scelto, "Fine fase", luogo_scelto, dove_trasferta, commessa_chiudi_utente, fase_chiudi_utente)

            st.markdown("---")
            st.write("Le tue timbrature di oggi:")
            st.dataframe(get_day_records(df, datetime.date.today(), user_name=dipendente_scelto), use_container_width=True)

        elif pagina_utente == "Riepilogo personale":
            richieste = carica_richieste()
            st.subheader("📊 Riepilogo personale: ferie, Gantt e statistiche")
            saldi_completi = compute_leave_balances(richieste, year=datetime.date.today().year)
            saldi_personali = saldi_completi[saldi_completi["Dipendente"] == dipendente_scelto]
            st.dataframe(saldi_personali, use_container_width=True)

            # Periodo per riepilogo personale
            st.markdown("---")
            st.write("### Visualizza attività nel tempo")
            up_start = st.date_input("Inizio", value=datetime.date.today().replace(day=1), key="up_start")
            up_end = st.date_input("Fine", value=datetime.date.today(), key="up_end")

            def build_user_gantt_local(df_all, start_date, end_date, user_name):
                dfn = normalize_datetime(df_all)
                if dfn.empty: return pd.DataFrame(columns=["Dipendente", "start", "end", "Data", "Luogo"])
                dfn = dfn[dfn["Dipendente"] == user_name]
                dfn = dfn[(dfn["Data"] >= start_date) & (dfn["Data"] <= end_date)]
                if dfn.empty: return pd.DataFrame(columns=["Dipendente", "start", "end", "Data", "Luogo"])
                rows = []
                for (user, date), group in dfn.groupby(["Dipendente", "Data"]):
                    group = group.sort_values("Timestamp")
                    ingressi = group[group["Azione"] == "Ingresso"]["Timestamp"]
                    uscite = group[group["Azione"] == "Uscita"]["Timestamp"]
                    if ingressi.empty or uscite.empty: continue
                    start = ingressi.iloc[0]; end = uscite.iloc[-1]
                    if pd.isna(start) or pd.isna(end) or end <= start: continue
                    luogo = ", ".join(sorted(set(group["Luogo"].astype(str).replace("nan", "-").tolist())))
                    rows.append({"Dipendente": user, "start": start, "end": end, "Data": date, "Luogo": luogo})
                return pd.DataFrame(rows)

            user_name = dipendente_scelto
            user_gantt = build_user_gantt_local(df, up_start, up_end, user_name)
            if user_gantt.empty:
                st.info("Nessuna attività registrata nel periodo selezionato.")
            else:
                st.markdown("**Gantt temporale**")
                gantt_chart_user = alt.Chart(user_gantt).mark_bar().encode(
                    x="start:T", x2="end:T", y=alt.Y("Data:T", axis=alt.Axis(title="Data")),
                    color=alt.Color("Luogo:N"), tooltip=["Dipendente:N", "Data:T", "start:T", "end:T", "Luogo:N"]
                ).properties(height=40 * len(user_gantt["Data"].unique()) + 100)
                st.altair_chart(gantt_chart_user, use_container_width=True)

                # Statistiche personali
                df_daily = compute_daily_work(df)
                df_user_daily = df_daily[(df_daily["Data"] >= up_start) & (df_daily["Data"] <= up_end) & (df_daily["Dipendente"] == user_name)]
                total_hours = round(df_user_daily["Ore_lavorate"].sum(), 2) if not df_user_daily.empty else 0.0
                days_worked = int(df_user_daily.shape[0])
                avg_hours = round(df_user_daily["Ore_lavorate"].mean(), 2) if days_worked else 0.0
                dfn_all = normalize_datetime(df)
                tmp = dfn_all[(dfn_all["Data"] >= up_start) & (dfn_all["Data"] <= up_end) & (dfn_all["Dipendente"] == user_name)]
                timbrature_count = tmp.shape[0]
                phases_started = tmp[tmp["Azione"] == "Inizio fase"].shape[0]

                st.markdown("**Statistiche riepilogo**")
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Ore totali", f"{total_hours}")
                c2.metric("Giorni lavorati", f"{days_worked}")
                c3.metric("Media ore/giorno", f"{avg_hours}")
                c4.metric("Timbrature/azioni", f"{timbrature_count}")

                st.markdown("**Dettaglio commesse (ore)**")
                commessa_user = compute_hours_by_commessa(df, up_start, up_end, employee_name=user_name)
                if commessa_user.empty:
                    st.info("Nessuna commessa registrata nel periodo.")
                else:
                    st.dataframe(commessa_user, use_container_width=True)
                    bar_comm = alt.Chart(commessa_user).mark_bar().encode(x=alt.X("Ore_lavorate:Q"), y=alt.Y("Commessa:N", sort='-x'), tooltip=[alt.Tooltip("Commessa:N"), alt.Tooltip("Ore_lavorate:Q")]).properties(height=300)
                    st.altair_chart(bar_comm, use_container_width=True)
                
                st.markdown("---")
                st.subheader("📥 Esportazione Rapporto")
                if PDF_AVAILABLE:
                    pdf_buffer = generate_pdf_report(user_name, up_start, up_end, prepare_registro_for_export(df, selected_user=user_name, start_date=up_start, end_date=up_end))
                    if pdf_buffer:
                        st.download_button("📄 SCARICA RAPPORTO PDF", data=pdf_buffer.getvalue(), file_name=f"rapporto_{user_name}_{datetime.date.today()}.pdf", mime="application/pdf")
                else:
                    st.info("ℹ️ Per abilitare i report PDF, installa: pip install reportlab")

        elif pagina_utente == "Report mensile":
            render_report_mensile(dipendente_scelto, df, key_prefix="user")

        elif pagina_utente == "📷 Report Intervento":
            render_report_intervento(dipendente_scelto, df, key_prefix="user")

        elif pagina_utente == "🧳 Trasferte Service":
            render_gestione_trasferte_service(user_info["name"], key_prefix="user_service")
            st.markdown("---")
            st.markdown("**Report interventi ricevuti**")
            report_utente_service = get_report_interventi_dettaglio()
            if report_utente_service.empty:
                st.info("Nessun report intervento inviato finora.")
            else:
                st.dataframe(report_utente_service, use_container_width=True)

        elif pagina_utente == "Richiesta ferie/permessi":
            st.subheader("Richiesta ferie / permessi")
            tipo_richiesta = st.radio("Tipo", ["Ferie", "Permesso"])
            data_inizio = st.date_input("Inizio")
            data_fine = st.date_input("Fine") if tipo_richiesta == "Ferie" else data_inizio
            ore_permesso = st.number_input("Ore", 0.5, 8.0, 0.5) if tipo_richiesta == "Permesso" else 8
            motivo = st.text_area("Motivo")
            
            if st.button("Invia richiesta"):
                salva_richiesta(dipendente_scelto, tipo_richiesta, data_inizio, data_fine, ore_permesso, motivo, ruolo_richiedente="user")
                st.rerun()

            st.write("### Le tue richieste")
            st.caption(LEGENDA_COLORI_RICHIESTE)
            mie_richieste = carica_richieste()
            mie_richieste = mie_richieste[mie_richieste["Dipendente"] == dipendente_scelto]
            st.dataframe(evidenzia_richieste_per_stato(mie_richieste), use_container_width=True)

        elif pagina_utente == "Richiesta rettifica":
            st.subheader("📝 Richiesta Rettifica Timbratura")
            st.info("Hai dimenticato di timbrare? Richiedi una rettifica all'amministratore.")
            
            azione_rettifica = st.selectbox("Azione dimenticata", ["Ingresso", "Uscita", "Inizio fase", "Fine fase"])
            data_rettifica = st.date_input("Data della timbratura dimenticata")
            ora_rettifica = st.time_input("Ora della timbratura dimenticata")
            motivo_rettifica = st.text_area("Motivo della dimenticanza")
            
            if st.button("Invia richiesta di rettifica"):
                salva_richiesta_rettifica(dipendente_scelto, azione_rettifica, data_rettifica, ora_rettifica.strftime("%H:%M:%S"), motivo_rettifica)
                st.rerun()
            
            st.write("### Le tue richieste di rettifica")
            rettifiche_utente = carica_rettifiche()[carica_rettifiche()["Dipendente"] == dipendente_scelto]
            if rettifiche_utente.empty:
                st.info("Nessuna richiesta di rettifica inviata.")
            else:
                st.dataframe(rettifiche_utente, use_container_width=True)   