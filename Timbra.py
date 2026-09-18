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

st.set_page_config(page_title="Gestione Presenze v2", page_icon="🏢", layout="wide")
st.title("🏢 Sistema Timbrature Avanzato")

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
    st.subheader("🔐 Login Utente")
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
    st.sidebar.button("Logout", on_click=logout)
    st.subheader(f"Benvenuto, {user_info['name']}!")

    df = carica_dati_db()
    
    if user_info["role"] == "admin":
        st.info("Sei connesso come amministratore. Puoi visualizzare i timbri di tutti gli utenti.")
        st.markdown("---")

        admin_page = st.sidebar.radio("Sezione amministratore", ["Dati e Presenze", "Richieste ferie/permessi", "Rettifiche timbrature", "Gestione Commesse", "Resoconto Commesse", "Grafici e Classifiche", "🌱 Sostenibilità (Smart Working)", "Gestione Utenti DB"])

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
        
        # Selettore principale: Mie funzioni vs Gestione Team
        modalita = st.sidebar.radio("📌 Modalità", ["Mie funzioni personali", "Gestione Team"])
        
        if modalita == "Mie funzioni personali":
            # *** REPLICA DELLA SEZIONE UTENTE PER IL RESPONSABILE ***
            dipendente_scelto = user_info["name"]
            pagina_utente = st.sidebar.radio("Funzione personale", ["Profilo", "Timbrature", "Riepilogo personale", "Report mensile", "Richiesta ferie/permessi", "Richiesta rettifica"])

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
        
        else:
            # *** SEZIONE GESTIONE TEAM (ORIGINALE DEL RESPONSABILE) ***
            st.subheader("📊 Pannello Responsabile - Gestione Team")
            
            area_responsabile = user_info["area"]
            dipendenti_area = get_users_in_area(area_responsabile)
            
            resp_page = st.sidebar.radio("Sezione team", ["Timbrature del team", "Richieste ferie/permessi", "Rettifiche timbrature", "Statistiche area", "Resoconto Commesse", "Gestione fasi commessa"])
            
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

    else:
        # --- LATO UTENTE ---
        dipendente_scelto = user_info["name"]
        pagina_utente = st.sidebar.radio("Funzione utente", ["Profilo", "Timbrature", "Riepilogo personale", "Report mensile", "Richiesta ferie/permessi", "Richiesta rettifica"])

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