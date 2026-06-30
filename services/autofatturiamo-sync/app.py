import hashlib
import hmac
import html as _html
import io
import os
import re
import subprocess
import zipfile
import json
import sqlite3
import time
import logging
import unicodedata
import markdown
from collections import defaultdict, OrderedDict
from datetime import date, datetime, timedelta, timezone
from itertools import groupby as itertools_groupby
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote
from flask import Flask, abort, redirect, render_template, request, jsonify, send_file, session
from dotenv import load_dotenv
from stripe_client import get_stripe_customers_data, get_stripe_billing_resources, get_stripe_subscriptions, get_stripe_upcoming_invoices
import fattura_xml
from odoo_client import get_all_partners, get_all_opportunities, get_partner_current_stage, clear_partner_vat
from imap_client import fetch_emails, get_all_email_ids, fetch_emails_by_ids
from scheduler import start_scheduler
import actions_db
import action_executors
import scheduled_tasks_db
import calcom_parser

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", os.urandom(32).hex())
DB_PATH = os.path.join(os.path.dirname(__file__), "local.db")
# Copia read-only del database di PRODUZIONE (prenotazioni, fiscal_communication,
# users_profile, …) importata dalla skill `ppp-import-produzione`. Alimenta le
# pagine /prenotazioni-clienti. È un singolo file, separato da local.db.
PROD_DB_PATH = os.path.join(os.path.dirname(__file__), "produzione.db")

BOOT_ID = int(time.time() * 1000)
_BASE_DIR = Path(__file__).parent
_REPO_ROOT = _BASE_DIR.parent.parent  # services/autofatturiamo-sync → services → repo root
_NANOCLAW_DB = _REPO_ROOT / "data" / "v2.db"
_CONTEXT_DIR = _BASE_DIR / "context-files"
CONTEXT_MD_PATH = _CONTEXT_DIR / "context.md"
CONTEXT_OVERVIEW_MD_PATH = _CONTEXT_DIR / "context_overview.md"
CONTEXT_MD_BACKUP_PATH = _CONTEXT_DIR / "context.md.bak"
CONTEXT_CLIENT_MD_PATH = _CONTEXT_DIR / "context-client.md"
CONTEXT_CLIENT_OVERVIEW_MD_PATH = _CONTEXT_DIR / "context_overview_client.md"
CONTEXT_CLIENT_MD_BACKUP_PATH = _CONTEXT_DIR / "context-client.md.bak"

CONTEXT_AUDIENCES = ("interno", "clienti")

# Map audience → (markdown path, overview path, backup path)
CONTEXT_PATHS_BY_AUDIENCE = {
    "interno": (CONTEXT_MD_PATH, CONTEXT_OVERVIEW_MD_PATH, CONTEXT_MD_BACKUP_PATH),
    "clienti": (CONTEXT_CLIENT_MD_PATH, CONTEXT_CLIENT_OVERVIEW_MD_PATH, CONTEXT_CLIENT_MD_BACKUP_PATH),
}

# Il label del job NanoClaw è dinamico: il setup aggiunge un suffix UUID
# (es. com.nanoclaw-v2-81ab8806) per evitare collisioni tra installazioni.
# Lo risolviamo a runtime cercando il plist installato in ~/Library/LaunchAgents/.
_LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
_BOT_STATE_CACHE: dict = {"value": None, "expires_at": 0.0}


def _nanoclaw_plist() -> Path | None:
    matches = sorted(_LAUNCH_AGENTS_DIR.glob("com.nanoclaw*.plist"))
    return matches[0] if matches else None


def _nanoclaw_label() -> str | None:
    plist = _nanoclaw_plist()
    return plist.stem if plist else None


def _nanoclaw_domain() -> str | None:
    label = _nanoclaw_label()
    return f"gui/{os.getuid()}/{label}" if label else None


def _bot_is_running() -> bool:
    """Returns True if the NanoClaw launchd job is loaded. Cached 2s."""
    now = time.time()
    if _BOT_STATE_CACHE["value"] is not None and _BOT_STATE_CACHE["expires_at"] > now:
        return _BOT_STATE_CACHE["value"]
    domain = _nanoclaw_domain()
    if domain is None:
        value = False
    else:
        r = subprocess.run(
            ["launchctl", "print", domain],
            capture_output=True, text=True,
        )
        value = r.returncode == 0
    _BOT_STATE_CACHE["value"] = value
    _BOT_STATE_CACHE["expires_at"] = now + 2.0
    return value


def _bot_state_invalidate() -> None:
    _BOT_STATE_CACHE["expires_at"] = 0.0


def _sources_mtime() -> int:
    paths = [
        _BASE_DIR / "app.py",
        _BASE_DIR / "actions_db.py",
        CONTEXT_MD_PATH,
        CONTEXT_OVERVIEW_MD_PATH,
        CONTEXT_CLIENT_MD_PATH,
        CONTEXT_CLIENT_OVERVIEW_MD_PATH,
        *_BASE_DIR.glob("templates/*.html"),
        *_BASE_DIR.glob("action_executors/*.py"),
        Path(PROD_DB_PATH),  # rebuild della copia di produzione → auto-reload tab
    ]
    return int(max((p.stat().st_mtime for p in paths if p.exists()), default=0) * 1000)


# Per-section layout for the dashboard "Contesto" block.
# Slug computed from each `## ...` header in context.md → layout, icon, accent.
# Sections with an unknown slug fall back to the generic card.
CONTEXT_SECTION_REGISTRY_INTERNO = {
    "cos-e-autofatturiamo":      ("definition",         "💼", "sky"),
    "scadenza-normativa":        ("rule_with_examples", "📅", "amber"),
    "modifiche-e-cancellazioni": ("cases",              "✏️", "violet"),
    "flusso-di-invio-pec-sdi":   ("stepper",            "🔄", "rose"),
    "clienti-odoo-stripe":       ("comparison",         "👥", "emerald"),
    "sito-web-e-demo":           ("definition",         "🌐", "slate"),
}

CONTEXT_SECTION_REGISTRY_CLIENTI = {
    "cos-e-autofatturiamo":      ("definition",         "💼", "sky"),
    "scadenza-normativa":        ("rule_with_examples", "📅", "amber"),
    "modifiche-e-cancellazioni": ("cases",              "✏️", "violet"),
    "sito-web-e-demo":           ("definition",         "🌐", "slate"),
}

# Backward-compat alias: existing callers read the interno registry.
CONTEXT_SECTION_REGISTRY = CONTEXT_SECTION_REGISTRY_INTERNO


def _slugify(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = "".join(c for c in normalized if not unicodedata.combining(c))
    ascii_text = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower())
    return ascii_text.strip("-")


def _md_inline(text: str) -> str:
    """Render inline markdown (bold/code/em) without an outer <p>."""
    html = markdown.markdown(text, extensions=["extra"]).strip()
    m = re.fullmatch(r"<p>(.*)</p>", html, flags=re.DOTALL)
    return m.group(1) if m else html


def _split_intro_and_list(body_md: str):
    """Split a section body into (intro_md_before_list, list_block, outro_md_after_list)."""
    intro, list_block, outro = [], [], []
    state = "intro"  # intro → list → outro
    for line in body_md.splitlines():
        if state == "intro":
            if line.startswith(("- ", "1. ")):
                state = "list"
                list_block.append(line)
            else:
                intro.append(line)
        elif state == "list":
            is_item = bool(re.match(r"^(?:- |\d+\. )", line))
            is_cont = line.startswith(("  ", "   ")) or not line.strip()
            if is_item or is_cont:
                list_block.append(line)
            else:
                state = "outro"
                outro.append(line)
        else:
            outro.append(line)
    return "\n".join(intro).strip(), "\n".join(list_block).strip(), "\n".join(outro).strip()


def _extract_bullets(list_md: str) -> list[str]:
    """Top-level bullet items (`- ...`), continuation lines folded into the same item."""
    items, current = [], []
    in_item = False
    for line in list_md.splitlines():
        if line.startswith("- "):
            if in_item:
                items.append(" ".join(current).strip())
                current = []
            in_item = True
            current.append(line[2:])
        elif in_item and (line.startswith("  ") or line.startswith("\t")):
            current.append(line.strip())
        elif in_item and not line.strip():
            items.append(" ".join(current).strip())
            current = []
            in_item = False
    if in_item and current:
        items.append(" ".join(current).strip())
    return items


def _extract_numbered(list_md: str) -> list[str]:
    """Top-level numbered items (`N. ...`), continuation lines folded."""
    items, current = [], []
    in_item = False
    for line in list_md.splitlines():
        m = re.match(r"^\d+\.\s+(.+)$", line)
        if m:
            if in_item:
                items.append(" ".join(current).strip())
                current = []
            in_item = True
            current.append(m.group(1))
        elif in_item and (line.startswith("   ") or line.startswith("\t")):
            current.append(line.strip())
        elif in_item and not line.strip():
            items.append(" ".join(current).strip())
            current = []
            in_item = False
    if in_item and current:
        items.append(" ".join(current).strip())
    return items


def _parse_definition(body_md: str) -> dict:
    return {"html": markdown.markdown(body_md, extensions=["extra", "sane_lists"])}


def _parse_rule_with_examples(body_md: str) -> dict:
    intro_md, list_md, _ = _split_intro_and_list(body_md)
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", intro_md) if p.strip()]
    rule_html = _md_inline(paragraphs[0]) if paragraphs else ""
    intro_html = (
        markdown.markdown("\n\n".join(paragraphs[1:]), extensions=["extra"])
        if len(paragraphs) > 1 else ""
    )
    examples = [_md_inline(b) for b in _extract_bullets(list_md)]
    return {"rule_html": rule_html, "intro_html": intro_html, "examples": examples}


def _parse_cases(body_md: str) -> dict:
    intro_md, list_md, _ = _split_intro_and_list(body_md)
    intro_html = markdown.markdown(intro_md, extensions=["extra"]) if intro_md else ""
    cases = []
    for raw in _extract_bullets(list_md):
        m = re.match(r"\*\*(.+?)\*\*\s*(?:→|—|–)\s*(.+)", raw, flags=re.DOTALL)
        if m:
            cases.append({
                "name": m.group(1).strip(),
                "action": _md_inline(m.group(2).strip().rstrip(".")),
            })
        else:
            cases.append({"name": "", "action": _md_inline(raw)})
    return {"intro_html": intro_html, "cases": cases}


def _parse_stepper(body_md: str) -> dict:
    _, list_md, _ = _split_intro_and_list(body_md)
    return {"steps": [_md_inline(it) for it in _extract_numbered(list_md)]}


def _parse_comparison(body_md: str) -> dict:
    intro_md, list_md, outro_md = _split_intro_and_list(body_md)
    intro_html = markdown.markdown(intro_md, extensions=["extra"]) if intro_md else ""
    outro_html = markdown.markdown(outro_md, extensions=["extra"]) if outro_md else ""
    entries = []
    for raw in _extract_bullets(list_md):
        m = re.match(r"\*\*(.+?)\*\*\s*(?:—|–|-)\s*(.+)", raw, flags=re.DOTALL)
        if m:
            entries.append({
                "name": m.group(1).strip(),
                "desc": _md_inline(m.group(2).strip().rstrip(".")),
            })
        else:
            entries.append({"name": "", "desc": _md_inline(raw)})
    return {"intro_html": intro_html, "entries": entries, "outro_html": outro_html}


def _parse_generic(body_md: str) -> dict:
    return {"html": markdown.markdown(body_md, extensions=["extra", "sane_lists"])}


_LAYOUT_PARSERS = {
    "definition":         _parse_definition,
    "rule_with_examples": _parse_rule_with_examples,
    "cases":              _parse_cases,
    "stepper":            _parse_stepper,
    "comparison":         _parse_comparison,
    "generic":            _parse_generic,
}


def _read_context_md() -> str:
    try:
        return CONTEXT_MD_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _render_context_overview() -> str:
    try:
        text = CONTEXT_OVERVIEW_MD_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    return markdown.markdown(text, extensions=["extra"])


def _parse_context_md() -> list[dict]:
    text = _read_context_md()
    if not text:
        return []
    chunks = re.split(r"(?:\A|\n)## ", text)
    sections = []
    for chunk in chunks[1:]:
        title_line, _, body = chunk.partition("\n")
        title = title_line.strip()
        slug = _slugify(title)
        layout, icon, accent = CONTEXT_SECTION_REGISTRY.get(
            slug, ("generic", "📄", "slate")
        )
        try:
            data = _LAYOUT_PARSERS[layout](body)
        except Exception:
            layout = "generic"
            data = _parse_generic(body)
        sections.append({
            "title":  title,
            "slug":   slug,
            "icon":   icon,
            "accent": accent,
            "layout": layout,
            "data":   data,
        })
    return sections


@app.route("/api/version")
def api_version():
    return jsonify({"boot_id": BOOT_ID, "mtime": _sources_mtime()})


@app.route("/api/internal/refresh-meetings-card/<mg_id>", methods=["POST"])
def api_refresh_meetings_card(mg_id: str):
    """Rigenera /workspace/extra/autofatturiamo/customer-meetings/<mg_id>.md
    per ogni agent group wirato al mg. Chiamato dal router NanoClaw all'arrivo
    di un inbound WhatsApp prima del wake del container. Fire-and-forget lato
    chiamante: ritorna 204 anche se non c'erano meeting nella finestra."""
    touched = _write_meetings_card(mg_id)
    app.logger.info("refresh-meetings-card mg_id=%s touched=%d", mg_id, touched)
    return ("", 204)


def _summary_config():
    """Legge dev_repo / window_days / chat_id del summary da sync-config.json.
    Tollera file assente o chiavi mancanti (fallback ai default del modulo)."""
    import weekly_summary
    cfg = {}
    try:
        cfg = json.loads(
            (Path(__file__).parent / "sync-config.json").read_text(encoding="utf-8")
        )
    except Exception:
        cfg = {}
    return {
        "dev_repo": cfg.get("summary_dev_repo", weekly_summary.DEFAULT_DEV_REPO),
        "window_days": int(cfg.get("summary_window_days", weekly_summary.DEFAULT_WINDOW_DAYS)),
        "chat_id": cfg.get("summary_chat_id"),
    }


@app.route("/api/internal/summary/preview")
def api_summary_preview():
    """Anteprima del messaggio (Rich Markdown Telegram), senza inviarlo. Usata per
    debug/verifica. Esente da login (prefisso /api/internal/).

    Query param: `monthly=1` → variante mensile (resoconto del mese precedente);
    `now=YYYY-MM-DD` → override della data (solo per testare il mensile)."""
    import weekly_summary
    sc = _summary_config()
    monthly = request.args.get("monthly") in ("1", "true", "yes")
    now = None
    if request.args.get("now"):
        try:
            now = datetime.fromisoformat(request.args["now"]).replace(
                tzinfo=fattura_xml.ROME_TZ)
        except ValueError:
            now = None
    text = weekly_summary.build_summary_markdown(
        now=now, dev_repo=sc["dev_repo"], window_days=sc["window_days"],
        monthly=monthly,
    )
    return jsonify({"ok": True, "text": text})


# Cache TTL per il blocco summary "live" della dashboard: summary_live_data fa
# una chiamata di rete a GitHub (numero PR), troppo lenta per ogni load della
# home. Cache in-process da 10 min, con degrado all'ultima versione buona se un
# rebuild fallisce. La home lo carica lazy via fetch (vedi sotto).
_summary_live_cache = {"ts": 0.0, "html": None}
_SUMMARY_LIVE_TTL = 600  # secondi


def _summary_live_html():
    """HTML della tabellina compatta «Stato azienda» (variante settimanale) per il
    blocco live in cima alla dashboard: numeri principali (demo, clienti, PR,
    ricavi mese corrente proiezione+reale), NON il testo completo del messaggio
    Telegram. Cache TTL 10 min; su errore ritorna l'ultima versione buona (o None
    se non ne esiste ancora)."""
    import time
    import weekly_summary
    now_ts = time.time()
    if (_summary_live_cache["html"] is not None
            and (now_ts - _summary_live_cache["ts"]) < _SUMMARY_LIVE_TTL):
        return _summary_live_cache["html"]
    sc = _summary_config()
    try:
        data = weekly_summary.summary_live_data(
            dev_repo=sc["dev_repo"], window_days=sc["window_days"],
        )
        html = render_template("_summary_live.html", **data)
    except Exception:
        app.logger.exception("dashboard: build summary live fallita")
        return _summary_live_cache["html"]
    _summary_live_cache.update(ts=now_ts, html=html)
    return html


@app.route("/api/internal/summary/live")
def api_summary_live():
    """HTML del summary settimanale per il blocco live della dashboard (caricato
    lazy dal client). Esente da login (prefisso /api/internal/). `ok=False` se il
    summary non è ancora disponibile → il client nasconde il blocco."""
    html = _summary_live_html()
    if not html:
        return jsonify({"ok": False, "html": None})
    return jsonify({"ok": True, "html": html})


@app.route("/api/internal/summary/send", methods=["POST"])
def api_summary_send():
    """Costruisce e invia il messaggio "Stato azienda" su Telegram.

    Body JSON opzionale: {"chat_id": <id>, "monthly": bool, "now": "YYYY-MM-DD"}.
    `chat_id` assente → summary_chat_id da sync-config.json. `monthly` → variante
    mensile. `now` → override data (test). Chiamato dal comando /summary (host
    Node, weekly) e riusabile a mano.
    """
    import weekly_summary
    sc = _summary_config()
    body = request.get_json(silent=True) or {}
    chat_id = body.get("chat_id") or sc["chat_id"]
    if chat_id in (None, ""):
        return jsonify({"ok": False, "error": "missing_chat_id"}), 400
    now = None
    if body.get("now"):
        try:
            now = datetime.fromisoformat(body["now"]).replace(tzinfo=fattura_xml.ROME_TZ)
        except (ValueError, TypeError):
            now = None
    res = weekly_summary.invia_summary(
        chat_id, now=now, dev_repo=sc["dev_repo"], window_days=sc["window_days"],
        monthly=bool(body.get("monthly")),
    )
    return jsonify(res), (200 if res.get("ok") else 502)


def _development_days(raw, default=7):
    """Normalizza il parametro `days` del resoconto sviluppo in [1, 365]."""
    try:
        return max(1, min(365, int(raw)))
    except (TypeError, ValueError):
        return default


@app.route("/api/internal/development/preview")
def api_development_preview():
    """Anteprima del resoconto sviluppo (Rich Markdown Telegram), senza inviarlo.
    Esente da login (prefisso /api/internal/). ATTENZIONE: chiama l'API Anthropic
    (Opus) → qualche secondo di latenza e costo. Query param: `days=N` (default 7),
    `now=YYYY-MM-DD` (override data, test)."""
    import development_summary
    sc = _summary_config()
    days = _development_days(request.args.get("days"), default=7)
    month = request.args.get("month") in ("1", "true", "yes")
    now = None
    if request.args.get("now"):
        try:
            now = datetime.fromisoformat(request.args["now"]).replace(
                tzinfo=fattura_xml.ROME_TZ)
        except ValueError:
            now = None
    text = development_summary.build_development_markdown(
        now=now, dev_repo=sc["dev_repo"], days=days, month=month,
    )
    return jsonify({"ok": True, "text": text})


@app.route("/api/internal/development/send", methods=["POST"])
def api_development_send():
    """Costruisce e invia il resoconto sviluppo "Cosa è stato fatto" su Telegram:
    raccoglie le PR integrate nella finestra (titolo + descrizione) e le fa
    riassumere a Claude (Opus) in linguaggio non tecnico per il team aziendale.

    Body JSON opzionale: {"chat_id": <id>, "days": int, "now": "YYYY-MM-DD"}.
    `chat_id` assente → summary_chat_id da sync-config.json. Chiamato dal comando
    /development (host Node) e riusabile a mano per i test.
    """
    import development_summary
    sc = _summary_config()
    body = request.get_json(silent=True) or {}
    chat_id = body.get("chat_id") or sc["chat_id"]
    if chat_id in (None, ""):
        return jsonify({"ok": False, "error": "missing_chat_id"}), 400
    days = _development_days(body.get("days"), default=7)
    month = bool(body.get("month"))
    now = None
    if body.get("now"):
        try:
            now = datetime.fromisoformat(body["now"]).replace(tzinfo=fattura_xml.ROME_TZ)
        except (ValueError, TypeError):
            now = None
    res = development_summary.invia_development(
        chat_id, now=now, dev_repo=sc["dev_repo"], days=days, month=month,
    )
    return jsonify(res), (200 if res.get("ok") else 502)


@app.template_filter("nospace")
def nospace_filter(s):
    """Pulizia SOLO in visualizzazione: rimuove gli spazi (es. dai telefoni
    `+39 392 054 3683` → `+393920543683`). Non tocca il dato salvato."""
    if not s:
        return s
    return re.sub(r"\s+", "", str(s))


@app.template_filter("fromjson")
def fromjson_filter(s):
    if not s:
        return {}
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return {}


@app.template_filter("timeago")
def timeago_filter(iso_ts):
    if not iso_ts:
        return "mai"
    dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    diff = datetime.now(dt.tzinfo) - dt
    seconds = int(diff.total_seconds())
    if seconds < 60:
        return "adesso"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min fa"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} ore fa" if hours > 1 else "1 ora fa"
    days = hours // 24
    return f"{days} giorni fa" if days > 1 else "1 giorno fa"


@app.template_filter("is_stale")
def is_stale_filter(iso_ts, hours=24):
    """True se la sincronizzazione è più vecchia di `hours` (default 24h) o mai avvenuta."""
    if not iso_ts:
        return True
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return True
    return (datetime.now(dt.tzinfo) - dt).total_seconds() > hours * 3600


# Mapping IT esplicito (no setlocale): più portabile dei nomi via locale di sistema.
_GIORNI_IT = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
_MESI_IT_ABBR = ["", "gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic"]
_GIORNI_ABBR = ["Lun", "Mar", "Mer", "Gio", "Ven", "Sab", "Dom"]  # indice 0=lun … 6=dom
_MESI_IT_FULL = ["", "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
                 "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"]


def _it_day_badge(dt):
    """lunedì 20 maggio — badge giorno chat WhatsApp (anno scontato)."""
    return f"{_GIORNI_IT[dt.weekday()]} {dt.day} {_MESI_IT_FULL[dt.month]}"


def _it_date(dt):
    """20 mag 2026 — data sola compatta."""
    return f"{dt.day} {_MESI_IT_ABBR[dt.month]} {dt.year}"


def _it_datetime(dt):
    """20 mag 2026, 14:30 — data+ora per tabelle dashboard."""
    return f"{dt.day} {_MESI_IT_ABBR[dt.month]} {dt.year}, {dt:%H:%M}"


def _it_time(dt):
    return dt.strftime("%H:%M")


def _it_last_seen(dt, now=None):
    """Pattern sidebar WhatsApp: oggi → 14:30 / ieri → 'ieri' / entro 7gg → 'lun' / oltre → 20/05."""
    now = now or datetime.now(dt.tzinfo)
    today = now.date()
    d = dt.date()
    if d == today:
        return _it_time(dt)
    delta = (today - d).days
    if delta == 1:
        return "ieri"
    if 0 < delta < 7:
        return _GIORNI_IT[dt.weekday()][:3]
    return f"{dt.day:02d}/{dt.month:02d}"


@app.template_filter("it_day_badge")
def it_day_badge_filter(dt):
    return _it_day_badge(dt) if dt else "—"


@app.template_filter("it_date")
def it_date_filter(dt):
    return _it_date(dt) if dt else "—"


@app.template_filter("it_dt")
def it_dt_filter(dt):
    return _it_datetime(dt) if dt else "—"


@app.template_filter("it_time")
def it_time_filter(dt):
    return _it_time(dt) if dt else "—"


@app.template_filter("it_last_seen")
def it_last_seen_filter(dt):
    return _it_last_seen(dt) if dt else "—"


@app.template_filter("iso_to_it_dt")
def iso_to_it_dt_filter(iso_ts):
    """Parsa una stringa ISO 8601 (con o senza 'Z') e la formatta in italiano."""
    if not iso_ts:
        return "—"
    try:
        return _it_datetime(datetime.fromisoformat(iso_ts.replace("Z", "+00:00")))
    except (ValueError, TypeError):
        return iso_ts


def _parse_iso_aware(iso_ts):
    dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


@app.template_filter("unix_datetime")
def unix_datetime_filter(ts):
    """Formatta un timestamp unix (secondi) in formato italiano '20 mag 2026, 14:30'."""
    if ts is None:
        return "—"
    try:
        return _it_datetime(datetime.fromtimestamp(int(ts)))
    except (ValueError, TypeError, OSError):
        return "—"


@app.template_filter("seconds_pretty")
def seconds_pretty_filter(seconds):
    """Formatta una durata in secondi (float) come '5m 23s' o '53s'."""
    if seconds is None:
        return "—"
    try:
        s = int(float(seconds))
    except (ValueError, TypeError):
        return "—"
    if s < 60:
        return f"{s}s"
    m, sec = divmod(s, 60)
    if m < 60:
        return f"{m}m {sec}s" if sec else f"{m}m"
    h, m = divmod(m, 60)
    return f"{h}h {m}m" if m else f"{h}h"


@app.template_filter("duration")
def duration_filter(start_iso, end_iso):
    if not start_iso or not end_iso:
        return "—"
    a = _parse_iso_aware(start_iso)
    b = _parse_iso_aware(end_iso)
    seconds = int((b - a).total_seconds())
    if seconds < 0:
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s" if sec else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}g {hours}h" if hours else f"{days}g"


@app.context_processor
def inject_last_sync():
    try:
        with db() as conn:
            rows = conn.execute("SELECT key, value FROM sync_state WHERE key IN ('stripe_last_sync', 'odoo_last_sync', 'email_last_sync', 'bluedot_last_sync', 'gcal_last_sync', 'fatture_last_sync', 'subscriptions_last_sync', 'upcoming_last_sync', 'produzione_last_sync')").fetchall()
            return {"last_sync": {r["key"].replace("_last_sync", ""): r["value"] for r in rows}}
    except Exception:
        return {"last_sync": {}}


@app.context_processor
def inject_pending_actions_count():
    return {"pending_actions_count": actions_db.count_pending()}


@app.context_processor
def inject_subscriptions_error_count():
    """Conteggio abbonamenti Stripe in errore, per il badge nella navbar.
    None-safe se la tabella non esiste ancora (mai sincronizzato)."""
    try:
        with db() as conn:
            has = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='stripe_subscriptions'"
            ).fetchone()
            if not has:
                return {"subscriptions_error_count": 0}
            n = conn.execute(
                "SELECT COUNT(*) FROM stripe_subscriptions WHERE is_error=1"
            ).fetchone()[0]
        return {"subscriptions_error_count": n}
    except Exception:
        return {"subscriptions_error_count": 0}


@app.context_processor
def inject_scarti_count():
    """Conteggio scarti SDI aperti (NS senza RC), per il badge nella navbar."""
    return {"scarti_count": _count_scarti_aperti()}


@app.context_processor
def inject_sidebar_actions():
    return {"sidebar_actions": actions_db.list_recent_all(limit=20)}


# Catalogo delle azioni che il sistema può PROPORRE. È documentazione, non un
# motore a regole come PIPELINE_RULES: lato azioni non esistono toggle per-azione
# — vengono proposte dall'assistente AI (MCP tool) o dal gate WhatsApp, e
# richiedono sempre approvazione manuale. Le chiavi combaciano con
# action_executors.ACTIONS così catalogo ed executor restano allineati.
ACTION_RULES = [
    {
        "key": "notify_team",
        "title": "Notifica al team",
        "icon": "fa-bullhorn",
        "brand": False,
        "source": "Assistente AI (autonomo)",
        "when": "L'assistente decide di avvisare il team. Va sul canale Discord "
                "operations (o notifications se è una celebrazione).",
        "does": "Pubblica un messaggio su Discord SENZA approvazione "
                "(auto-dispatch nel daemon, ogni ~15s).",
    },
    {
        "key": "send_email_to_clients",
        "title": "Email ai clienti",
        "icon": "fa-envelope",
        "brand": False,
        "source": "Assistente AI (autonomo)",
        "when": "L'assistente decide di contattare uno o più clienti via email.",
        "does": "Oggi in modalità stub: inoltra su Discord (SMTP non ancora "
                "attivo). Richiede approvazione.",
    },
    {
        "key": "payment_failed_whatsapp",
        "title": "Avviso pagamento fallito (WhatsApp)",
        "icon": "fa-credit-card",
        "brand": True,
        "source": "Automatica (sync Stripe)",
        "when": "A ogni sync, se un abbonamento ha un rinnovo non riuscito, "
                "viene accodata UNA azione per fattura insoluta (Stripe ritenta "
                "più volte ma l'avviso si propone una sola volta).",
        "does": "Inserisci a mano il numero WhatsApp del cliente e approva: "
                "l'host invia l'avviso con il link di rinnovo via Baileys.",
    },
    {
        "key": "whatsapp_reply",
        "title": "Risposta WhatsApp",
        "icon": "fa-whatsapp",
        "brand": True,
        "source": "Composer / Avviso pagamento / Gate (rete di sicurezza)",
        "when": "Quando scrivi a mano dal composer della chat, quando usi "
                "«Avvisa su WhatsApp» su un pagamento non riuscito in "
                "/pagamenti (avviso + link di rinnovo), o — come rete di "
                "sicurezza — se un messaggio in uscita del bot finisse verso "
                "un numero NON in whitelist (gate di approvazione).",
        "does": "Dopo l'approvazione, l'host invia il messaggio via Baileys.",
    },
]


def _action_rules_view():
    """Arricchisce ACTION_RULES con lo stato runtime ('attiva') per ogni tipo,
    usando i segnali già esistenti (webhook Discord, bot acceso). Ritorna dict
    pronti per il template, senza callable — come rules_view per la pipeline."""
    discord_ok = bool(os.getenv("DISCORD_WEBHOOK_OPERATIONS"))
    bot_ok = _bot_is_running()
    out = []
    for r in ACTION_RULES:
        item = dict(r)
        if r["key"] in ("notify_team", "send_email_to_clients"):
            item["active"] = discord_ok
            item["active_label"] = "Attiva" if discord_ok else "Spenta — webhook Discord non configurato"
            if r["key"] == "notify_team":
                item["active_detail"] = "Automatica: parte senza approvazione (auto-dispatch)." if discord_ok else ""
            else:
                item["active_detail"] = (
                    "Modalità stub: invia su Discord (operations)." if discord_ok else ""
                )
        elif r["key"] == "whatsapp_reply":
            item["active"] = bot_ok
            item["active_label"] = "Attiva (bot acceso)" if bot_ok else "Spenta — bot fermo"
            item["active_detail"] = "Filtrata dalla whitelist dei numeri fidati."
        elif r["key"] == "payment_failed_whatsapp":
            item["active"] = bot_ok
            item["active_label"] = "Attiva (bot acceso)" if bot_ok else "Spenta — bot fermo"
            item["active_detail"] = "Accodata in automatico a ogni sync; richiede approvazione."
        else:
            item["active"] = False
            item["active_label"] = "Sconosciuto"
            item["active_detail"] = ""
        out.append(item)
    return out


@app.context_processor
def inject_action_rules():
    return {"action_rules": _action_rules_view()}


@app.context_processor
def inject_bot_state():
    return {"bot_running": _bot_is_running()}


SCHEMA_STRIPE = """
CREATE TABLE IF NOT EXISTS stripe_clienti (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stripe_id TEXT UNIQUE NOT NULL,
    address TEXT,
    balance INTEGER NOT NULL DEFAULT 0,
    business_name TEXT,
    created INTEGER,
    delinquent INTEGER NOT NULL DEFAULT 0,
    email TEXT,
    metadata TEXT,
    name TEXT,
    phone TEXT,
    vat TEXT
)
"""

SCHEMA_ODOO = """
CREATE TABLE IF NOT EXISTS odoo_clienti (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    odoo_id INTEGER UNIQUE NOT NULL,
    name TEXT,
    display_name TEXT,
    vat TEXT,
    stage TEXT
)
"""

SCHEMA_CLIENTI = """
CREATE TABLE IF NOT EXISTS clienti (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nome TEXT NOT NULL,
    vat TEXT,
    odoo_cliente_id INTEGER REFERENCES odoo_clienti(id) ON DELETE SET NULL
)
"""
SCHEMA_CLIENTI_INDEX_VAT = "CREATE INDEX IF NOT EXISTS idx_clienti_vat ON clienti(vat)"
SCHEMA_CLIENTI_INDEX_ODOO = "CREATE INDEX IF NOT EXISTS idx_clienti_odoo ON clienti(odoo_cliente_id)"

# Tabella di collegamento cliente↔fonti: il "cliente" è un CONTENITORE con
# anagrafica editabile (vedi colonne aggiunte a `clienti`) e N link per-ID alle
# fonti (Stripe, Piattaforma, Meeting, WhatsApp, Odoo). UNIQUE(source, ext_id):
# un riferimento esterno può stare su un solo cliente. manual=1 → collegato a
# mano (non toccato dalla materializzazione automatica).
SCHEMA_CLIENTE_LINKS = """
CREATE TABLE IF NOT EXISTS cliente_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cliente_id INTEGER NOT NULL REFERENCES clienti(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    ext_id TEXT NOT NULL,
    ext_label TEXT,
    manual INTEGER NOT NULL DEFAULT 0,
    created_at TEXT,
    UNIQUE(source, ext_id)
)
"""
SCHEMA_CLIENTE_LINKS_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_cliente_links_cliente ON cliente_links(cliente_id)"
)

SCHEMA_CLIENTE_NOTE = """
CREATE TABLE IF NOT EXISTS cliente_note (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cliente_id INTEGER NOT NULL REFERENCES clienti(id) ON DELETE CASCADE,
    testo TEXT NOT NULL,
    origine TEXT,
    created_at TEXT NOT NULL
)
"""
SCHEMA_CLIENTE_NOTE_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_cliente_note_cliente ON cliente_note(cliente_id)"
)

# Storico VISIBILE delle transizioni di colonna pipeline di un cliente: una riga
# per ogni spostamento reale (backfill iniziale, drag&drop manuale, regola auto).
# `auto`=1 → mossa da una regola automatica; =0 → trascinata a mano. `rule_key`
# dice quale regola/origine ('backfill'|'manual'|'stripe'|'piattaforma'|'demo_schedulata').
SCHEMA_CLIENTE_PIPELINE_LOG = """
CREATE TABLE IF NOT EXISTS cliente_pipeline_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cliente_id INTEGER NOT NULL REFERENCES clienti(id) ON DELETE CASCADE,
    from_col TEXT,
    to_col TEXT NOT NULL,
    auto INTEGER NOT NULL DEFAULT 0,
    rule_key TEXT,
    reason TEXT,
    created_at TEXT NOT NULL
)
"""
SCHEMA_CLIENTE_PIPELINE_LOG_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_cliente_pipeline_log_cliente "
    "ON cliente_pipeline_log(cliente_id)"
)

# Bookkeeping INTERNO (non mostrato): segna che una regola è già stata osservata
# vera per un cliente, così non rifà fuoco sulla stessa condizione permanente
# (semantica "solo su nuovo evento"). UNIQUE(cliente_id, rule_key).
SCHEMA_CLIENTE_PIPELINE_RULE_STATE = """
CREATE TABLE IF NOT EXISTS cliente_pipeline_rule_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cliente_id INTEGER NOT NULL REFERENCES clienti(id) ON DELETE CASCADE,
    rule_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(cliente_id, rule_key)
)
"""

SCHEMA_ODOO_OPPORTUNITA = """
CREATE TABLE IF NOT EXISTS odoo_opportunita (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    odoo_id INTEGER UNIQUE NOT NULL,
    name TEXT,
    partner_name TEXT,
    partner_vat TEXT,
    expected_revenue REAL,
    probability REAL,
    stage_name TEXT,
    user_name TEXT,
    create_date TEXT
)
"""

SCHEMA_EMAIL = """
CREATE TABLE IF NOT EXISTS email_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT UNIQUE,
    subject TEXT,
    from_addr TEXT,
    to_addr TEXT,
    date_str TEXT,
    snippet TEXT,
    attachments TEXT
)
"""

SCHEMA_EMAIL_ATTACHMENTS = """
CREATE TABLE IF NOT EXISTS email_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email_message_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    content TEXT NOT NULL,
    email_date TEXT,
    att_type TEXT,
    UNIQUE(email_message_id, filename)
)
"""

SCHEMA_ZIP_INVIATI = """
CREATE TABLE IF NOT EXISTS zip_inviati (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email_message_id TEXT NOT NULL,
    zip_filename TEXT NOT NULL,
    zip_size INTEGER,
    email_date TEXT,
    num_xml INTEGER DEFAULT 0,
    UNIQUE(email_message_id, zip_filename)
)
"""

SCHEMA_AUTOFATTURE = """
CREATE TABLE IF NOT EXISTS autofatture (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zip_id INTEGER NOT NULL,
    filename TEXT NOT NULL,
    content TEXT NOT NULL,
    email_date TEXT,
    email_date_iso TEXT,
    piva_cliente TEXT,
    UNIQUE(zip_id, filename),
    FOREIGN KEY (zip_id) REFERENCES zip_inviati(id)
)
"""

SCHEMA_SYNC_STATE = """
CREATE TABLE IF NOT EXISTS sync_state (
    key TEXT PRIMARY KEY,
    value TEXT
)
"""

# Registro persistente degli eventi già annunciati su Discord dalle automazioni
# (automations.py). NON viene mai droppato dai sync (a differenza di
# stripe_charges / stripe_subscriptions, ricreate a ogni run): è la memoria che
# distingue "evento nuovo da notificare" da "già visto". `kind` discrimina il
# tipo di automazione (es. 'payment_ok', 'subscription_new', 'gcal_event'),
# `ext_id` è l'id stabile dell'entità (charge_id, subscription_id, gcal uid).
SCHEMA_NOTIFIED_EVENTS = """
CREATE TABLE IF NOT EXISTS notified_events (
    kind TEXT NOT NULL,
    ext_id TEXT NOT NULL,
    notified_at TEXT NOT NULL,
    PRIMARY KEY (kind, ext_id)
)
"""

# Impostazioni configurabili da UI (pagina /impostazioni), condivise tra il Flask
# e il daemon (scheduler) via local.db — entrambi i processi leggono fresco, niente
# IPC. Coppia key/value (i valori sono stringhe; il parsing tipizzato sta negli
# helper get_setting / get_scarti_notify_settings).
SCHEMA_APP_SETTINGS = """
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT
)
"""

SCHEMA_FORCE_SYNC_STATE = """
CREATE TABLE IF NOT EXISTS force_sync_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    total_ids INTEGER DEFAULT 0,
    processed_ids INTEGER DEFAULT 0,
    last_updated TEXT
)
"""

SCHEMA_RISPOSTE_SDI = """
CREATE TABLE IF NOT EXISTS risposte_SDI (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email_message_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    content TEXT NOT NULL,
    email_date TEXT,
    email_date_iso TEXT,
    tipo TEXT,
    numero_fattura TEXT,
    UNIQUE(email_message_id, filename)
)
"""

SCHEMA_BLUEDOT_EVENTS = """
CREATE TABLE IF NOT EXISTS bluedot_events (
    id INTEGER PRIMARY KEY,
    event_type TEXT NOT NULL,
    meeting_id TEXT,
    video_id TEXT,
    title TEXT,
    bluedot_created_at INTEGER,
    received_at INTEGER NOT NULL,
    payload TEXT NOT NULL,
    synced_at INTEGER NOT NULL
)
"""

SCHEMA_BLUEDOT_EVENTS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_bluedot_events_received_at
    ON bluedot_events(received_at)
"""

SCHEMA_GCAL_EVENTS = """
CREATE TABLE IF NOT EXISTS gcal_events (
    uid TEXT PRIMARY KEY,
    summary TEXT,
    description TEXT,
    location TEXT,
    dtstart TEXT NOT NULL,
    dtend TEXT,
    all_day INTEGER NOT NULL DEFAULT 0,
    status TEXT,
    organizer TEXT,
    attendees TEXT,
    url TEXT,
    created TEXT,
    last_modified TEXT,
    raw_ics TEXT,
    synced_at TEXT NOT NULL,
    cal_event_type TEXT,
    cal_prospect_name TEXT,
    cal_prospect_phone TEXT,
    cal_prospect_email TEXT,
    cal_prospect_timezone TEXT,
    cal_booking_id TEXT,
    cal_reschedule_url TEXT,
    cal_attendees_external TEXT,
    linked_messaging_group_id_auto TEXT,
    linked_messaging_group_id_manual TEXT
)
"""

SCHEMA_GCAL_EVENTS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_gcal_events_dtstart
    ON gcal_events(dtstart)
"""

# Charges Stripe (full refresh ad ogni sync_fatture, come stripe_clienti):
# una riga per transazione, con invoice/customer abbinati per id.
SCHEMA_STRIPE_CHARGES = """
CREATE TABLE IF NOT EXISTS stripe_charges (
    charge_id TEXT PRIMARY KEY,
    amount INTEGER,
    currency TEXT,
    status TEXT,
    created INTEGER,
    created_iso TEXT,
    customer_id TEXT,
    customer_email TEXT,
    customer_name TEXT,
    codice_sd TEXT,
    description TEXT,
    failure_reason TEXT,
    invoice_id TEXT,
    invoice_number TEXT,
    pm_type TEXT,
    pm_brand TEXT,
    pm_last4 TEXT,
    refunded INTEGER NOT NULL DEFAULT 0,
    customer_json TEXT
)
"""

# Subscription Stripe (full refresh ad ogni sync_subscriptions, come stripe_charges):
# una riga per abbonamento, con customer e latest_invoice abbinati. is_error=1
# quando lo stato della subscription o dell'ultima fattura segnala un pagamento
# fallito/insoluto (vedi _subscription_is_error).
SCHEMA_STRIPE_SUBSCRIPTIONS = """
CREATE TABLE IF NOT EXISTS stripe_subscriptions (
    subscription_id TEXT PRIMARY KEY,
    customer_id TEXT,
    customer_name TEXT,
    customer_email TEXT,
    vat TEXT,
    status TEXT,
    plan_amount INTEGER,
    currency TEXT,
    plan_interval TEXT,
    plan_nickname TEXT,
    cancel_at_period_end INTEGER NOT NULL DEFAULT 0,
    current_period_end INTEGER,
    current_period_end_iso TEXT,
    created INTEGER,
    created_iso TEXT,
    latest_invoice_id TEXT,
    latest_invoice_status TEXT,
    latest_invoice_amount_due INTEGER,
    latest_invoice_attempt_count INTEGER,
    latest_invoice_url TEXT,
    is_error INTEGER NOT NULL DEFAULT 0
)
"""

# Anteprime "upcoming" delle fatture in maturazione (full refresh ad ogni
# sync_upcoming_invoices), una riga per subscription attiva. È l'unico dato che
# riflette il ricavo del MESE IN CORSO: con billing a consumo posticipato, gli
# importi (subtotal/total) sono il consumo maturato finora nel periodo corrente,
# che verrà addebitato a fine periodo (next_payment_attempt). Tutti gli importi
# in centesimi. `error` valorizzato se l'anteprima per quella sub è fallita.
SCHEMA_STRIPE_UPCOMING = """
CREATE TABLE IF NOT EXISTS stripe_upcoming_invoices (
    subscription_id TEXT PRIMARY KEY,
    customer_id TEXT,
    customer_name TEXT,
    vat TEXT,
    currency TEXT,
    subtotal INTEGER,
    tax INTEGER,
    total INTEGER,
    amount_due INTEGER,
    period_start INTEGER,
    period_start_iso TEXT,
    period_end INTEGER,
    period_end_iso TEXT,
    next_payment_attempt INTEGER,
    next_payment_attempt_iso TEXT,
    status TEXT,
    lines_json TEXT,
    error TEXT,
    fetched_at TEXT
)
"""

# Snapshot mensile della proiezione ricavi (billing posticipato a consumo).
# Una riga per mese di CONSUMO (`ym` = 'YYYY-MM'), aggiornata (upsert) ad ogni
# sync_upcoming_invoices con la proiezione fine-periodo corrente da
# _ricavi_overview(). Mentre il mese è in corso la riga viene continuamente
# riscritta; quando il mese cambia resta "congelata" sull'ultimo valore (≈ totale
# di quel mese, periodo ~100% trascorso). È così che il report mensile dell'1°
# conosce il ricavo del mese appena chiuso, il cui incasso Stripe avviene durante
# il mese che inizia (e quindi non è ancora visibile in stripe_charges).
SCHEMA_RICAVI_MENSILI = """
CREATE TABLE IF NOT EXISTS ricavi_mensili (
    ym TEXT PRIMARY KEY,
    proiezione_gross REAL,
    accrued_gross REAL,
    n_subscriptions INTEGER,
    captured_at TEXT
)
"""

# Fatture FatturaPA generate da noi (fattura_xml.generate_for_charges),
# keyed per charge_id. xml_error: well-formedness check server-side.
SCHEMA_FATTURE_GENERATE = """
CREATE TABLE IF NOT EXISTS fatture_generate (
    charge_id TEXT PRIMARY KEY,
    xml TEXT,
    generabile INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    matched_by TEXT,
    numero TEXT,
    xml_error TEXT
)
"""

# Stato SDI delle fatture inviate, letto dall'API Aruba Fatturazione Elettronica
# (aruba_client.get_invoices_out). Una riga per fattura Aruba, keyed per `numero`
# (= <Numero> FatturaPA = fatture_generate.numero): più charge dello stesso
# cliente confluiscono in una sola fattura, quindi la correlazione è per numero,
# non per charge. status_code è il valore grezzo Aruba (stringa); status_label la
# sua traduzione IT (vedi _SDI_STATUS). Upsert ad ogni sync (no drop/recreate).
SCHEMA_FATTURE_SDI_STATO = """
CREATE TABLE IF NOT EXISTS fatture_sdi_stato (
    numero TEXT PRIMARY KEY,
    aruba_id TEXT,
    id_sdi TEXT,
    filename TEXT,
    status_code TEXT,
    status_label TEXT,
    status_description TEXT,
    receiver_piva TEXT,
    invoice_date TEXT,
    creation_date TEXT,
    last_update TEXT,
    last_checked TEXT
)
"""


def _rename_corrupt_db():
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    corrupt_path = DB_PATH.replace(".db", f"_corrupt_{timestamp}.db")
    os.rename(DB_PATH, corrupt_path)
    print(f"DB corrotto rinominato in: {corrupt_path}")


def _check_db_integrity():
    if not os.path.exists(DB_PATH):
        return
    try:
        conn = sqlite3.connect(DB_PATH)
        result = conn.execute("PRAGMA integrity_check").fetchone()
        conn.close()
        if result[0] != "ok":
            _rename_corrupt_db()
            from discord_client import send_notification_to_team
            send_notification_to_team("⚠ Database corrotto rilevato e rinominato all'avvio")
    except sqlite3.DatabaseError:
        _rename_corrupt_db()
        from discord_client import send_notification_to_team
        send_notification_to_team("⚠ Database corrotto rilevato e rinominato all'avvio")


def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    # journal_mode=DELETE (non WAL): local.db viene montato read-only nei
    # container agente (groups/*/container.json) e un reader read-only senza i
    # file -wal/-shm legge solo il main file, vedendo una vista stantia/vuota.
    # In DELETE tutti i dati committati vivono nel singolo file principale →
    # visibilità cross-mount corretta. Vedi CLAUDE.md ("load-bearing").
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init_db():
    with db() as conn:
        conn.execute(SCHEMA_STRIPE)
        conn.execute(SCHEMA_ODOO)
        conn.execute(SCHEMA_CLIENTI)
        conn.execute(SCHEMA_CLIENTI_INDEX_VAT)
        conn.execute(SCHEMA_CLIENTI_INDEX_ODOO)
        conn.execute(SCHEMA_CLIENTE_LINKS)
        conn.execute(SCHEMA_CLIENTE_LINKS_INDEX)
        # Estensione di `clienti` a contenitore (anagrafica editabile + meta).
        # auto=1 → riga gestita dalla materializzazione; passa a 0 se editata a
        # mano, così la materializzazione non sovrascrive più l'anagrafica.
        for _col, _sql in (
            ("codice_fiscale", "ALTER TABLE clienti ADD COLUMN codice_fiscale TEXT"),
            ("email", "ALTER TABLE clienti ADD COLUMN email TEXT"),
            ("phone", "ALTER TABLE clienti ADD COLUMN phone TEXT"),
            ("note", "ALTER TABLE clienti ADD COLUMN note TEXT"),
            ("auto", "ALTER TABLE clienti ADD COLUMN auto INTEGER NOT NULL DEFAULT 1"),
            ("updated_at", "ALTER TABLE clienti ADD COLUMN updated_at TEXT"),
            ("archived", "ALTER TABLE clienti ADD COLUMN archived INTEGER NOT NULL DEFAULT 0"),
            # Pipeline come proprietà POSSEDUTA del cliente (non più derivata a
            # render). pipeline_col = colonna corrente; pipeline_auto = 1 se la
            # posizione corrente è stata impostata da una regola, 0 se a mano.
            ("pipeline_col", "ALTER TABLE clienti ADD COLUMN pipeline_col TEXT"),
            ("pipeline_auto", "ALTER TABLE clienti ADD COLUMN pipeline_auto INTEGER NOT NULL DEFAULT 1"),
        ):
            try:
                conn.execute(_sql)
            except sqlite3.OperationalError:
                pass
        conn.execute(SCHEMA_ODOO_OPPORTUNITA)
        conn.execute(SCHEMA_EMAIL)
        conn.execute(SCHEMA_EMAIL_ATTACHMENTS)
        conn.execute(SCHEMA_ZIP_INVIATI)
        conn.execute(SCHEMA_AUTOFATTURE)
        conn.execute(SCHEMA_SYNC_STATE)
        conn.execute(SCHEMA_NOTIFIED_EVENTS)
        conn.execute(SCHEMA_APP_SETTINGS)
        conn.execute(SCHEMA_FORCE_SYNC_STATE)
        conn.execute(SCHEMA_RISPOSTE_SDI)
        conn.execute(SCHEMA_BLUEDOT_EVENTS)
        conn.execute(SCHEMA_BLUEDOT_EVENTS_INDEX)
        conn.execute(SCHEMA_CLIENTE_NOTE)
        conn.execute(SCHEMA_CLIENTE_NOTE_INDEX)
        conn.execute(SCHEMA_CLIENTE_PIPELINE_LOG)
        conn.execute(SCHEMA_CLIENTE_PIPELINE_LOG_INDEX)
        conn.execute(SCHEMA_CLIENTE_PIPELINE_RULE_STATE)
        conn.execute(SCHEMA_GCAL_EVENTS)
        conn.execute(SCHEMA_GCAL_EVENTS_INDEX)
        conn.execute(SCHEMA_STRIPE_CHARGES)
        conn.execute(SCHEMA_STRIPE_UPCOMING)
        conn.execute(SCHEMA_RICAVI_MENSILI)
        conn.execute(SCHEMA_FATTURE_GENERATE)
        conn.execute(SCHEMA_FATTURE_SDI_STATO)
        for _col, _sql in (
            ("attendees", "ALTER TABLE gcal_events ADD COLUMN attendees TEXT"),
            ("created", "ALTER TABLE gcal_events ADD COLUMN created TEXT"),
            ("last_modified", "ALTER TABLE gcal_events ADD COLUMN last_modified TEXT"),
            ("cal_event_type", "ALTER TABLE gcal_events ADD COLUMN cal_event_type TEXT"),
            ("cal_prospect_name", "ALTER TABLE gcal_events ADD COLUMN cal_prospect_name TEXT"),
            ("cal_prospect_phone", "ALTER TABLE gcal_events ADD COLUMN cal_prospect_phone TEXT"),
            ("cal_prospect_email", "ALTER TABLE gcal_events ADD COLUMN cal_prospect_email TEXT"),
            ("cal_prospect_timezone", "ALTER TABLE gcal_events ADD COLUMN cal_prospect_timezone TEXT"),
            ("cal_booking_id", "ALTER TABLE gcal_events ADD COLUMN cal_booking_id TEXT"),
            ("cal_reschedule_url", "ALTER TABLE gcal_events ADD COLUMN cal_reschedule_url TEXT"),
            ("cal_attendees_external", "ALTER TABLE gcal_events ADD COLUMN cal_attendees_external TEXT"),
            ("linked_messaging_group_id_auto", "ALTER TABLE gcal_events ADD COLUMN linked_messaging_group_id_auto TEXT"),
            ("linked_messaging_group_id_manual", "ALTER TABLE gcal_events ADD COLUMN linked_messaging_group_id_manual TEXT"),
        ):
            try:
                conn.execute(_sql)
            except sqlite3.OperationalError:
                pass
        try:
            conn.execute("ALTER TABLE email_messages ADD COLUMN attachments TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE autofatture ADD COLUMN email_date_iso TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE autofatture ADD COLUMN piva_cliente TEXT")
        except sqlite3.OperationalError:
            pass
        # Popola piva_cliente per i record esistenti che non ce l'hanno
        missing = conn.execute("SELECT id, content FROM autofatture WHERE piva_cliente IS NULL").fetchall()
        for row in missing:
            piva = _extract_piva_from_xml(row["content"])
            if piva:
                conn.execute("UPDATE autofatture SET piva_cliente = ? WHERE id = ?", (piva, row["id"]))
        # Migrazione una-tantum: sposta clienti.note (campo legacy singolo) in cliente_note (log).
        _migrate_legacy_note_to_log(conn)
        # Backfill iniziale della pipeline: i clienti senza pipeline_col ereditano
        # la posizione calcolata dallo stato attuale (Odoo/Stripe) — "il posto
        # dove sono adesso". Idempotente: tocca solo le righe NULL.
        _backfill_pipeline_col(conn)
        conn.commit()


def sync_stripe():
    rows = get_stripe_customers_data()
    with db() as conn:
        conn.execute("DROP TABLE IF EXISTS stripe_clienti")
        conn.execute(SCHEMA_STRIPE)
        for r in rows:
            conn.execute(
                """INSERT INTO stripe_clienti
                   (stripe_id, address, balance, business_name, created, delinquent, email, metadata, name, phone, vat)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (r["stripe_id"], r["address"], r["balance"], r["business_name"],
                 r["created"], 1 if r["delinquent"] else 0, r["email"],
                 r["metadata"], r["name"], r["phone"], r["vat"]),
            )
        conn.execute(
            "INSERT OR REPLACE INTO sync_state (key, value) VALUES (?, ?)",
            ("stripe_last_sync", datetime.utcnow().isoformat() + "Z"),
        )
        conn.commit()


def sync_fatture():
    """Scarica charges/invoices/customers/tax_rates da Stripe e rigenera gli
    XML FatturaPA per ogni charge con invoice abbinata (full refresh)."""
    resources = get_stripe_billing_resources()
    rows = fattura_xml.build_charge_rows(resources)
    generata = fattura_xml.generate_for_charges(resources)
    with db() as conn:
        conn.execute("DROP TABLE IF EXISTS stripe_charges")
        conn.execute(SCHEMA_STRIPE_CHARGES)
        conn.execute("DROP TABLE IF EXISTS fatture_generate")
        conn.execute(SCHEMA_FATTURE_GENERATE)
        for r in rows:
            conn.execute(
                """INSERT INTO stripe_charges
                   (charge_id, amount, currency, status, created, created_iso,
                    customer_id, customer_email, customer_name, codice_sd,
                    description, failure_reason, invoice_id, invoice_number,
                    pm_type, pm_brand, pm_last4, refunded, customer_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (r["charge_id"], r["amount"], r["currency"], r["status"],
                 r["created"], r["created_iso"], r["customer_id"],
                 r["customer_email"], r["customer_name"], r["codice_sd"],
                 r["description"], r["failure_reason"], r["invoice_id"],
                 r["invoice_number"], r["pm_type"], r["pm_brand"], r["pm_last4"],
                 r["refunded"],
                 json.dumps(r["customer_json"], ensure_ascii=False) if r["customer_json"] else None),
            )
        for cid, g in generata.items():
            conn.execute(
                """INSERT INTO fatture_generate
                   (charge_id, xml, generabile, note, matched_by, numero, xml_error)
                   VALUES (?,?,?,?,?,?,?)""",
                (cid, g["xml"], 1 if g["generabile"] else 0, g["note"],
                 g["matched_by"], g["numero"], g["xml_error"]),
            )
        conn.execute(
            "INSERT OR REPLACE INTO sync_state (key, value) VALUES (?, ?)",
            ("fatture_last_sync", datetime.utcnow().isoformat() + "Z"),
        )
        conn.commit()


def sync_aruba_stato():
    """Legge lo stato SDI delle fatture inviate dall'API Aruba FE e fa upsert in
    `fatture_sdi_stato` (keyed per numero fattura). Sola lettura verso Aruba; non
    droppa la tabella per non perdere lo storico se una finestra fallisce."""
    import aruba_client
    days = int(os.getenv("ARUBA_FE_LOOKBACK_DAYS", "120"))
    sender = (os.getenv("ARUBA_FE_SENDER_VAT", "") or "").strip() or None
    records = aruba_client.get_invoices_out(days_back=days, sender_vat=sender)
    now_iso = datetime.utcnow().isoformat() + "Z"
    n = 0
    with db() as conn:
        for r in records:
            numero = r.get("numero")
            if not numero:
                continue
            code = r.get("status")
            label, _color = _sdi_status_meta(code, r.get("status_description"))
            conn.execute(
                """INSERT INTO fatture_sdi_stato
                   (numero, aruba_id, id_sdi, filename, status_code, status_label,
                    status_description, receiver_piva, invoice_date, creation_date,
                    last_update, last_checked)
                   VALUES (:numero,:aruba_id,:id_sdi,:filename,:status_code,:status_label,
                    :status_description,:receiver_piva,:invoice_date,:creation_date,
                    :last_update,:last_checked)
                   ON CONFLICT(numero) DO UPDATE SET
                    aruba_id=excluded.aruba_id, id_sdi=excluded.id_sdi,
                    filename=excluded.filename, status_code=excluded.status_code,
                    status_label=excluded.status_label,
                    status_description=excluded.status_description,
                    receiver_piva=excluded.receiver_piva,
                    invoice_date=excluded.invoice_date,
                    creation_date=excluded.creation_date,
                    last_update=excluded.last_update, last_checked=excluded.last_checked""",
                {
                    "numero": numero, "aruba_id": r.get("id"), "id_sdi": r.get("id_sdi"),
                    "filename": r.get("filename"), "status_code": code, "status_label": label,
                    "status_description": r.get("status_description"),
                    "receiver_piva": r.get("receiver_piva"), "invoice_date": r.get("invoice_date"),
                    "creation_date": r.get("creation_date"), "last_update": r.get("last_update"),
                    "last_checked": now_iso,
                },
            )
            n += 1
        conn.execute(
            "INSERT OR REPLACE INTO sync_state (key, value) VALUES (?, ?)",
            ("aruba_stato_last_sync", now_iso),
        )
        conn.commit()
    return {"fatture_stato": n}


# Stati subscription/fattura che indicano un pagamento fallito o insoluto.
_SUBSCRIPTION_ERROR_STATUSES = {"past_due", "unpaid", "incomplete", "incomplete_expired"}
_INVOICE_ERROR_STATUSES = {"open", "uncollectible"}


def _subscription_is_error(status, inv_status, attempt_count, amount_due):
    """True se l'abbonamento ha un problema di pagamento: stato della subscription
    problematico, oppure ultima fattura aperta/insoluta con importo dovuto e almeno
    un tentativo di addebito già effettuato (rinnovo fallito)."""
    if status in _SUBSCRIPTION_ERROR_STATUSES:
        return True
    if (inv_status in _INVOICE_ERROR_STATUSES
            and (amount_due or 0) > 0
            and (attempt_count or 0) >= 1):
        return True
    return False


def _normalize_subscription(s):
    """StripeObject piano (da get_stripe_subscriptions) → dict riga per la tabella
    stripe_subscriptions. customer/latest_invoice sono espansi; estrae importo e
    intervallo dal primo item del piano."""
    cust = s.get("customer")
    if isinstance(cust, dict):
        customer_id = cust.get("id")
        customer_name = cust.get("name") or None
        customer_email = cust.get("email") or None
    else:
        customer_id = cust or None
        customer_name = None
        customer_email = None

    items = ((s.get("items") or {}).get("data") or [])
    plan_amount = currency = plan_interval = plan_nickname = None
    if items:
        price = items[0].get("price") or {}
        qty = items[0].get("quantity") or 1
        if price.get("unit_amount") is not None:
            plan_amount = price["unit_amount"] * qty
        currency = price.get("currency") or s.get("currency")
        plan_interval = (price.get("recurring") or {}).get("interval")
        plan_nickname = price.get("nickname") or None
    currency = currency or s.get("currency")

    inv = s.get("latest_invoice")
    if isinstance(inv, dict):
        inv_id = inv.get("id")
        inv_status = inv.get("status")
        inv_amount_due = inv.get("amount_due")
        inv_attempt = inv.get("attempt_count")
        inv_url = inv.get("hosted_invoice_url")
    else:
        inv_id = inv or None
        inv_status = inv_amount_due = inv_attempt = inv_url = None

    cpe = s.get("current_period_end")
    created = s.get("created")
    return {
        "subscription_id": s.get("id"),
        "customer_id": customer_id,
        "customer_name": customer_name,
        "customer_email": customer_email,
        "status": s.get("status"),
        "plan_amount": plan_amount,
        "currency": currency,
        "plan_interval": plan_interval,
        "plan_nickname": plan_nickname,
        "cancel_at_period_end": 1 if s.get("cancel_at_period_end") else 0,
        "current_period_end": cpe,
        "current_period_end_iso": _unix_to_iso(cpe),
        "created": created,
        "created_iso": _unix_to_iso(created),
        "latest_invoice_id": inv_id,
        "latest_invoice_status": inv_status,
        "latest_invoice_amount_due": inv_amount_due,
        "latest_invoice_attempt_count": inv_attempt,
        "latest_invoice_url": inv_url,
        "is_error": 1 if _subscription_is_error(s.get("status"), inv_status, inv_attempt, inv_amount_due) else 0,
    }


def _unix_to_iso(ts):
    """Unix epoch → ISO 8601 in tz Europe/Rome (None-safe)."""
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=fattura_xml.ROME_TZ).isoformat()


def sync_subscriptions():
    """Scarica tutte le subscription Stripe e rigenera la tabella
    stripe_subscriptions (full refresh). La P.IVA è riempita via lookup su
    stripe_clienti per customer_id."""
    subs = [_normalize_subscription(s) for s in get_stripe_subscriptions()]
    with db() as conn:
        vat_by_cust = {
            r["stripe_id"]: r["vat"]
            for r in conn.execute("SELECT stripe_id, vat FROM stripe_clienti").fetchall()
        }
        conn.execute("DROP TABLE IF EXISTS stripe_subscriptions")
        conn.execute(SCHEMA_STRIPE_SUBSCRIPTIONS)
        for r in subs:
            conn.execute(
                """INSERT INTO stripe_subscriptions
                   (subscription_id, customer_id, customer_name, customer_email, vat,
                    status, plan_amount, currency, plan_interval, plan_nickname,
                    cancel_at_period_end, current_period_end, current_period_end_iso,
                    created, created_iso, latest_invoice_id, latest_invoice_status,
                    latest_invoice_amount_due, latest_invoice_attempt_count,
                    latest_invoice_url, is_error)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (r["subscription_id"], r["customer_id"], r["customer_name"],
                 r["customer_email"], vat_by_cust.get(r["customer_id"]),
                 r["status"], r["plan_amount"], r["currency"], r["plan_interval"],
                 r["plan_nickname"], r["cancel_at_period_end"], r["current_period_end"],
                 r["current_period_end_iso"], r["created"], r["created_iso"],
                 r["latest_invoice_id"], r["latest_invoice_status"],
                 r["latest_invoice_amount_due"], r["latest_invoice_attempt_count"],
                 r["latest_invoice_url"], r["is_error"]),
            )
        conn.execute(
            "INSERT OR REPLACE INTO sync_state (key, value) VALUES (?, ?)",
            ("subscriptions_last_sync", datetime.utcnow().isoformat() + "Z"),
        )
        conn.commit()


def sync_upcoming_invoices():
    """Scarica l'anteprima della fattura in maturazione ("upcoming") per ogni
    subscription ATTIVA e rigenera `stripe_upcoming_invoices` (full refresh).

    Con il billing a consumo posticipato di AFT è l'unico dato che riflette il
    ricavo del mese in corso: il `subtotal` dell'anteprima = consumo maturato
    finora nel periodo di fatturazione corrente. Va eseguito DOPO
    sync_subscriptions (legge da stripe_subscriptions per sapere quali sono
    attive e per il lookup di nome/P.IVA)."""
    with db() as conn:
        has = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='stripe_subscriptions'"
        ).fetchone()
        active = []
        if has:
            active = [dict(r) for r in conn.execute(
                "SELECT subscription_id, customer_id, customer_name, vat "
                "FROM stripe_subscriptions WHERE status IN ('active','trialing')"
            ).fetchall()]
    previews = get_stripe_upcoming_invoices(active)
    name_by_sub = {a["subscription_id"]: a.get("customer_name") for a in active}
    vat_by_sub = {a["subscription_id"]: a.get("vat") for a in active}
    cust_by_sub = {a["subscription_id"]: a.get("customer_id") for a in active}
    now_iso = datetime.utcnow().isoformat() + "Z"
    n_ok = 0
    with db() as conn:
        conn.execute("DROP TABLE IF EXISTS stripe_upcoming_invoices")
        conn.execute(SCHEMA_STRIPE_UPCOMING)
        for p in previews:
            sub_id = p.get("_subscription_id")
            if not sub_id:
                continue
            cust_id = p.get("_customer_id") or cust_by_sub.get(sub_id)
            if p.get("_error"):
                conn.execute(
                    """INSERT OR REPLACE INTO stripe_upcoming_invoices
                       (subscription_id, customer_id, customer_name, vat, error, fetched_at)
                       VALUES (?,?,?,?,?,?)""",
                    (sub_id, cust_id, name_by_sub.get(sub_id),
                     vat_by_sub.get(sub_id), p.get("_error"), now_iso),
                )
                continue
            ps = p.get("period_start")
            pe = p.get("period_end")
            npa = p.get("next_payment_attempt")
            lines = ((p.get("lines") or {}).get("data")) or []
            lines_summary = [
                {"description": l.get("description"), "amount": l.get("amount")}
                for l in lines
            ]
            conn.execute(
                """INSERT OR REPLACE INTO stripe_upcoming_invoices
                   (subscription_id, customer_id, customer_name, vat, currency,
                    subtotal, tax, total, amount_due, period_start, period_start_iso,
                    period_end, period_end_iso, next_payment_attempt,
                    next_payment_attempt_iso, status, lines_json, error, fetched_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sub_id, cust_id, name_by_sub.get(sub_id), vat_by_sub.get(sub_id),
                 p.get("currency"), p.get("subtotal"), p.get("tax"), p.get("total"),
                 p.get("amount_due"), ps, _unix_to_iso(ps), pe, _unix_to_iso(pe),
                 npa, _unix_to_iso(npa), p.get("status"),
                 json.dumps(lines_summary, ensure_ascii=False) if lines_summary else None,
                 None, now_iso),
            )
            n_ok += 1
        conn.execute(
            "INSERT OR REPLACE INTO sync_state (key, value) VALUES (?, ?)",
            ("upcoming_last_sync", now_iso),
        )
        conn.commit()
    # Congela la proiezione del mese corrente in ricavi_mensili (vedi
    # SCHEMA_RICAVI_MENSILI): alimenta il report mensile dell'1°. Non deve mai
    # far fallire la sync.
    try:
        _snapshot_ricavi_mese_corrente()
    except Exception:
        app.logger.exception("snapshot ricavi_mensili fallito (ignorato)")
    return {"upcoming": n_ok, "errori": len(previews) - n_ok}


def _snapshot_ricavi_mese_corrente(now=None):
    """Upsert in `ricavi_mensili` della proiezione fine-periodo del mese corrente
    (da _ricavi_overview). Chiamata ad ogni sync_upcoming_invoices: mentre il mese
    è in corso la riga si aggiorna, poi resta congelata sul valore finale. Noop se
    non ci sono dati upcoming (has_data=False)."""
    tz = fattura_xml.ROME_TZ
    now = now or datetime.now(tz)
    ov = _ricavi_overview(now=now)
    if not ov.get("has_data"):
        return
    ym = now.strftime("%Y-%m")
    with db() as conn:
        conn.execute(SCHEMA_RICAVI_MENSILI)
        conn.execute(
            """INSERT OR REPLACE INTO ricavi_mensili
               (ym, proiezione_gross, accrued_gross, n_subscriptions, captured_at)
               VALUES (?,?,?,?,?)""",
            (ym, ov["projection"]["gross"], ov["accrued"]["gross"],
             ov["n_subscriptions"], datetime.utcnow().isoformat() + "Z"),
        )
        conn.commit()


def _ricavi_overview(now=None):
    """Aggrega `stripe_upcoming_invoices` nel quadro ricavi del mese in corso.

    Ritorna un dict con: maturato-a-oggi (somma delle anteprime, netto/IVA/lordo),
    proiezione a fine periodo (proratizzata per-subscription sul proprio periodo
    di fatturazione, gestisce periodi non allineati al mese solare), ultimo ciclo
    già addebitato (charges succeeded del mese solare più recente, = ciò che si
    vede "il 1°"), conteggio abbonamenti e dettaglio per cliente. Importi
    convertiti in euro (i valori grezzi Stripe sono in centesimi)."""
    tz = fattura_xml.ROME_TZ
    now = now or datetime.now(tz)
    now_ts = now.timestamp()
    out = {
        "has_data": False,
        "currency": "EUR",
        "synced_at": None,
        "accrued": {"net": 0.0, "tax": 0.0, "gross": 0.0},
        "projection": {"net": 0.0, "gross": 0.0},
        "n_subscriptions": 0,
        "n_errors": 0,
        "rows": [],
        "last_billed": None,
    }
    with db() as conn:
        has = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='stripe_upcoming_invoices'"
        ).fetchone()
        if not has:
            return out
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM stripe_upcoming_invoices ORDER BY total DESC"
        ).fetchall()]
        st = conn.execute(
            "SELECT value FROM sync_state WHERE key='upcoming_last_sync'"
        ).fetchone()
        out["synced_at"] = st["value"] if st else None

        # Ultimo ciclo addebitato: charges succeeded raggruppati per mese solare
        # di addebito, si prende il mese più recente. È la cifra che appare "il 1°"
        # e si riferisce al consumo del mese PRECEDENTE (billing posticipato).
        try:
            ch = conn.execute(
                "SELECT substr(created_iso,1,7) AS ym, currency, "
                "       SUM(amount) AS tot, COUNT(*) AS n "
                "FROM stripe_charges "
                "WHERE status IN ('succeeded','paid') AND refunded=0 "
                "GROUP BY ym ORDER BY ym DESC LIMIT 1"
            ).fetchone()
            if ch and ch["ym"]:
                out["last_billed"] = {
                    "month": ch["ym"],
                    "gross": (ch["tot"] or 0) / 100.0,
                    "count": ch["n"],
                    "currency": (ch["currency"] or "eur").upper(),
                }
        except sqlite3.OperationalError:
            pass

    if not rows:
        return out
    out["has_data"] = True

    detail = []
    for r in rows:
        if r.get("error"):
            out["n_errors"] += 1
            continue
        out["n_subscriptions"] += 1
        if r.get("currency"):
            out["currency"] = r["currency"].upper()
        # net = ricavo netto post-sconto pre-IVA = total - tax. NON usare
        # `subtotal`: è al lordo degli sconti e sovrastima il ricavo (es. un
        # cliente con sconto può avere subtotal > total). gross = total (IVA incl.).
        tax = (r.get("tax") or 0) / 100.0
        gross = (r.get("total") or 0) / 100.0
        net = gross - tax
        out["accrued"]["net"] += net
        out["accrued"]["tax"] += tax
        out["accrued"]["gross"] += gross
        # Proiezione: scala il maturato sulla frazione di periodo trascorsa.
        ps = r.get("period_start")
        pe = r.get("period_end")
        proj_net = net
        proj_gross = gross
        if ps and pe and pe > ps:
            elapsed = max(0.0, min(now_ts, pe) - ps)
            frac = elapsed / (pe - ps)
            if frac > 0.02:  # evita esplosioni a inizio periodo
                proj_net = net / frac
                proj_gross = gross / frac
        out["projection"]["net"] += proj_net
        out["projection"]["gross"] += proj_gross
        detail.append({
            "subscription_id": r.get("subscription_id"),
            "customer_name": r.get("customer_name") or r.get("customer_id"),
            "vat": r.get("vat"),
            "net": net,
            "gross": gross,
            "proj_net": proj_net,
            "period_end_iso": r.get("period_end_iso"),
            "next_payment_attempt_iso": r.get("next_payment_attempt_iso"),
            "lines": json.loads(r["lines_json"]) if r.get("lines_json") else [],
        })
    detail.sort(key=lambda d: d["gross"], reverse=True)
    out["rows"] = detail
    out["days_in_month"] = (date(now.year + (now.month // 12), (now.month % 12) + 1, 1) - date(now.year, now.month, 1)).days
    out["day_of_month"] = now.day
    return out


def _format_address(raw):
    if not raw:
        return None
    try:
        a = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw
    parts = []
    if a.get("line1"):
        parts.append(a["line1"])
    if a.get("line2"):
        parts.append(a["line2"])
    cap_city = " ".join(filter(None, [a.get("postal_code"), a.get("city")]))
    if cap_city:
        prov = f" ({a['state']})" if a.get("state") else ""
        parts.append(cap_city + prov)
    if a.get("country"):
        parts.append(a["country"])
    return ", ".join(parts) if parts else None


def _time_ago(ts):
    if not ts:
        return None
    diff = int(time.time()) - int(ts)
    if diff < 60:
        return "adesso"
    if diff < 3600:
        m = diff // 60
        return f"{m} minut{'o' if m == 1 else 'i'} fa"
    if diff < 86400:
        h = diff // 3600
        return f"{h} or{'a' if h == 1 else 'e'} fa"
    if diff < 2592000:
        d = diff // 86400
        return f"{d} giorn{'o' if d == 1 else 'i'} fa"
    if diff < 31536000:
        m = diff // 2592000
        return f"{m} mes{'e' if m == 1 else 'i'} fa"
    y = diff // 31536000
    return f"{y} ann{'o' if y == 1 else 'i'} fa"


def _parse_metadata(raw):
    if not raw:
        return {}
    try:
        m = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    out = {}
    if "codice_destinatario" in m:
        out["codice_destinatario"] = m["codice_destinatario"]
    if "referral" in m:
        out["referral"] = m["referral"]
    return out


def get_clienti():
    with db() as conn:
        rows = conn.execute("SELECT * FROM stripe_clienti ORDER BY created DESC").fetchall()
        clienti = []
        for r in rows:
            c = dict(r)
            c["address"] = _format_address(c.get("address"))
            c["created_ago"] = _time_ago(c.get("created"))
            meta = _parse_metadata(c.get("metadata"))
            c["codice_destinatario"] = meta.get("codice_destinatario")
            c["referral"] = meta.get("referral")
            clienti.append(c)
        return clienti


def _normalize_vat(raw):
    if not raw or raw == "False":
        return None
    v = str(raw).strip().upper().replace(" ", "").replace("-", "")
    if len(v) > 2 and v[:2].isalpha():
        v = v[2:]
    return v or None


_RE_CESSIONARIO_PIVA = re.compile(
    r"<CessionarioCommittente>.*?<IdFiscaleIVA>.*?<IdCodice>\s*([^<]+?)\s*</IdCodice>",
    re.DOTALL,
)

def _extract_piva_from_xml(xml_content):
    """Estrae la P.IVA del cessionario/committente dal contenuto XML di un'autofattura."""
    m = _RE_CESSIONARIO_PIVA.search(xml_content or "")
    if m:
        return _normalize_vat(m.group(1))
    return None


def _html_to_text(html):
    """Rimuove i tag HTML e decodifica le entità comuni (per il campo `comment` di Odoo)."""
    if not html:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"<p[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">") \
               .replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ")
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


def _migrate_legacy_note_to_log(conn):
    """Migrazione una-tantum: se il flag 'note_legacy_migrated' non è ancora in
    sync_state, copia clienti.note non vuote come prima voce del log cliente_note
    con origine='manuale', poi segna la migrazione come completata."""
    flag = conn.execute(
        "SELECT value FROM sync_state WHERE key='note_legacy_migrated'"
    ).fetchone()
    if flag is not None:
        return
    legacy = conn.execute(
        "SELECT id, note, updated_at FROM clienti WHERE note IS NOT NULL AND note != ''"
    ).fetchall()
    now_iso = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    for row in legacy:
        created = row["updated_at"] or now_iso
        conn.execute(
            "INSERT INTO cliente_note(cliente_id, testo, origine, created_at) VALUES(?,?,?,?)",
            (row["id"], row["note"].strip(), "manuale", created),
        )
    conn.execute(
        "INSERT OR REPLACE INTO sync_state(key, value) VALUES('note_legacy_migrated', ?)",
        (now_iso,),
    )


def sync_odoo():
    rows = get_all_partners(fields=["name", "display_name", "vat"])
    if not rows:
        logging.warning("sync_odoo: Odoo ha restituito 0 partner — tabella non azzerata (guardia anti-svuotamento)")
        return
    try:
        stage_by_partner = get_partner_current_stage()
    except Exception:
        logging.exception("get_partner_current_stage fallita; stage Odoo non aggiornati")
        stage_by_partner = {}
    with db() as conn:
        conn.execute(SCHEMA_ODOO)  # no-op se la tabella esiste già
        # Migrazione in-place: aggiunge la colonna stage se la tabella esiste in schema legacy (senza stage).
        odoo_cols = {r["name"] for r in conn.execute("PRAGMA table_info(odoo_clienti)").fetchall()}
        if "stage" not in odoo_cols:
            conn.execute("ALTER TABLE odoo_clienti ADD COLUMN stage TEXT")
        conn.execute("DELETE FROM odoo_clienti")  # DML → dentro la transazione, atomico col commit finale
        for r in rows:
            conn.execute(
                "INSERT INTO odoo_clienti (odoo_id, name, display_name, vat, stage) VALUES (?,?,?,?,?)",
                (r["id"], r.get("name"), r.get("display_name"), _normalize_vat(r.get("vat")),
                 stage_by_partner.get(r["id"])),
            )
        conn.execute(
            "INSERT OR REPLACE INTO sync_state (key, value) VALUES (?, ?)",
            ("odoo_last_sync", datetime.utcnow().isoformat() + "Z"),
        )
        conn.commit()


def _enrich_stripe_row(r):
    c = dict(r)
    c["created_ago"] = _time_ago(c.get("created"))
    return c


def get_paganti():
    """Stripe con almeno un'opportunità (match P.IVA = partner_vat). Più opportunità Odoo in fase 'Cliente pagante' il cui partner non è su Stripe."""
    with db() as conn:
        rows_stripe = conn.execute("""
            SELECT DISTINCT
                s.stripe_id, s.name AS stripe_name, s.email AS stripe_email,
                s.phone AS stripe_phone, s.business_name, s.created, s.vat,
                (SELECT o.partner_name FROM odoo_opportunita o WHERE o.partner_vat = s.vat LIMIT 1) AS partner_name,
                (SELECT o.partner_vat FROM odoo_opportunita o WHERE o.partner_vat = s.vat LIMIT 1) AS partner_vat
            FROM stripe_clienti s
            INNER JOIN odoo_opportunita o ON s.vat IS NOT NULL AND s.vat = o.partner_vat
            ORDER BY s.created DESC
        """).fetchall()
        rows_odoo_paganti = conn.execute("""
            SELECT o.odoo_id, o.name, o.partner_name, o.partner_vat AS vat, o.create_date
            FROM odoo_opportunita o
            WHERE o.stage_name = 'Cliente pagante'
              AND (o.partner_vat IS NULL OR o.partner_vat NOT IN (SELECT vat FROM stripe_clienti WHERE vat IS NOT NULL))
            ORDER BY o.create_date DESC
        """).fetchall()
    out = [_enrich_stripe_row(r) for r in rows_stripe]
    for r in rows_odoo_paganti:
        out.append({
            "stripe_id": None,
            "stripe_name": r["partner_name"] or r["name"],
            "stripe_email": None,
            "stripe_phone": None,
            "business_name": r["name"] if r["partner_name"] else None,
            "created": None,
            "created_ago": None,
            "vat": r["vat"],
            "odoo_id": r["odoo_id"],
            "partner_name": r["partner_name"],
        })
    return out


def get_stripe_senza_odoo():
    """Su Stripe ma senza opportunità (P.IVA non presente in nessuna opportunità)."""
    with db() as conn:
        rows = conn.execute("""
            SELECT
                s.stripe_id, s.name AS stripe_name, s.email AS stripe_email,
                s.phone AS stripe_phone, s.business_name, s.created, s.vat
            FROM stripe_clienti s
            WHERE s.vat IS NOT NULL AND s.vat NOT IN (
                SELECT DISTINCT partner_vat FROM odoo_opportunita WHERE partner_vat IS NOT NULL
            )
            ORDER BY s.created DESC
        """).fetchall()
        return [_enrich_stripe_row(r) for r in rows]


def get_stripe_senza_codice_destinatario():
    """Clienti Stripe il cui metadata non ha `codice_destinatario` (chiave assente o valore vuoto).
    `"0000000"` conta come impostato (placeholder PEC, valore esplicito)."""
    with db() as conn:
        rows = conn.execute("""
            SELECT
                s.stripe_id, s.name AS stripe_name, s.email AS stripe_email,
                s.phone AS stripe_phone, s.business_name, s.created, s.vat, s.metadata
            FROM stripe_clienti s
            ORDER BY s.created DESC
        """).fetchall()
        out = []
        for r in rows:
            meta = _parse_metadata(r["metadata"])
            cd = (meta.get("codice_destinatario") or "").strip()
            if cd:
                continue
            out.append(_enrich_stripe_row(r))
        return out


def get_clienti_unified():
    """Lista unica di clienti con tag: Aggiungi P.IVA, Prospect, Paganti. Ogni riga ha: stripe_business_name, vat, odoo_name, stripe_email."""
    stripe_no_odoo = get_stripe_senza_odoo()
    prospect = get_prospect()
    paganti = get_paganti()
    out = []
    for c in stripe_no_odoo:
        out.append({
            "tag": "Aggiungi P.IVA",
            "stripe_business_name": c.get("business_name"),
            "opportunity_name": None,
            "vat": c.get("vat"),
            "odoo_name": None,
            "stripe_email": c.get("stripe_email"),
            "created_ago": c.get("created_ago"),
            "odoo_id": None,
        })
    for o in prospect:
        out.append({
            "tag": "Prospect",
            "stripe_business_name": None,
            "opportunity_name": o.get("name"),
            "vat": o.get("partner_vat"),
            "odoo_name": o.get("partner_name"),
            "stripe_email": None,
            "created_ago": None,
            "odoo_id": o.get("odoo_id"),
            "stage_name": o.get("stage_name"),
        })
    for c in paganti:
        stripe_vat = c.get("vat") if c.get("stripe_id") else None
        odoo_vat = c.get("partner_vat") if c.get("stripe_id") else c.get("vat")
        out.append({
            "tag": "Paganti",
            "stripe_business_name": c.get("business_name"),
            "opportunity_name": None,
            "vat": stripe_vat or odoo_vat,
            "odoo_name": c.get("partner_name"),
            "stripe_email": c.get("stripe_email"),
            "created_ago": c.get("created_ago"),
            "odoo_id": c.get("odoo_id"),
        })
    return out


def _clients_minimal_list():
    """Lista per la sidebar /clients. Ogni voce: { key, piva, display_name, tag, subtitle }.

    - Righe con VAT (Paganti, Aggiungi P.IVA, prospect con P.IVA) sono dedup per VAT; key=piva, subtitle=piva.
    - Righe senza VAT (prospect Odoo): una voce per lead; key='odoo-<odoo_id>', subtitle=stage_name o '— senza P.IVA'.
    """
    seen_vat = {}
    no_vat = []
    for c in get_clienti_unified():
        name = (
            c.get("stripe_business_name")
            or c.get("odoo_name")
            or c.get("opportunity_name")
            or "(senza nome)"
        )
        piva = c.get("vat")
        if piva:
            if piva not in seen_vat:
                seen_vat[piva] = {
                    "key": piva,
                    "piva": piva,
                    "display_name": name,
                    "tag": c.get("tag"),
                    "subtitle": piva,
                }
            continue
        odoo_id = c.get("odoo_id")
        if not odoo_id:
            continue
        no_vat.append({
            "key": f"odoo-{odoo_id}",
            "piva": "",
            "display_name": name,
            "tag": c.get("tag"),
            "subtitle": c.get("stage_name") or "— senza P.IVA",
        })
    return sorted(
        list(seen_vat.values()) + no_vat,
        key=lambda x: x["display_name"].lower(),
    )


def get_cliente_detail(piva):
    """Dati completi di un cliente per P.IVA: Stripe + opportunità Odoo + autofatture."""
    piva_norm = _normalize_vat(piva)
    if not piva_norm:
        return None
    with db() as conn:
        stripe_row = conn.execute(
            "SELECT * FROM stripe_clienti WHERE vat = ?", (piva_norm,)
        ).fetchone()
        odoo_rows = conn.execute(
            "SELECT * FROM odoo_opportunita WHERE partner_vat = ? ORDER BY create_date DESC",
            (piva_norm,),
        ).fetchall()
        af_rows = conn.execute("""
            SELECT a.id, a.filename, a.email_date, a.email_date_iso, a.zip_id,
                   REPLACE(SUBSTR(a.filename, INSTR(a.filename, '_') + 1), '.xml', '') AS numero_fattura,
                   (SELECT COUNT(*) FROM risposte_SDI r WHERE r.numero_fattura = REPLACE(SUBSTR(a.filename, INSTR(a.filename, '_') + 1), '.xml', '') AND r.tipo = 'RC') AS count_rc,
                   (SELECT COUNT(*) FROM risposte_SDI r WHERE r.numero_fattura = REPLACE(SUBSTR(a.filename, INSTR(a.filename, '_') + 1), '.xml', '') AND r.tipo = 'NS') AS count_ns
            FROM autofatture a
            WHERE a.piva_cliente = ?
            ORDER BY a.email_date_iso IS NULL, a.email_date_iso DESC, a.id DESC
        """, (piva_norm,)).fetchall()

    if not stripe_row and not odoo_rows and not af_rows:
        return None

    stripe = None
    if stripe_row:
        stripe = _enrich_stripe_row(stripe_row)
        meta = _parse_metadata(stripe.get("metadata"))
        stripe["codice_destinatario"] = meta.get("codice_destinatario")
        stripe["referral"] = meta.get("referral")
        stripe["address_fmt"] = _format_address(stripe.get("address"))

    odoo = [dict(r) for r in odoo_rows]

    autofatture = []
    for r in af_rows:
        af = dict(r)
        count_rc = af.get("count_rc") or 0
        count_ns = af.get("count_ns") or 0
        if count_ns > 0 and count_rc == 0:
            af["stato"] = "scartata"
        elif count_rc == 0 and count_ns == 0:
            af["stato"] = "in_attesa"
        else:
            af["stato"] = "confermata"
        autofatture.append(af)

    display_name = (
        (stripe or {}).get("business_name")
        or (stripe or {}).get("name")
        or (odoo[0].get("partner_name") if odoo else None)
        or (odoo[0].get("name") if odoo else None)
        or "(senza nome)"
    )

    return {
        "key": piva_norm,
        "piva": piva_norm,
        "display_name": display_name,
        "stripe": stripe,
        "odoo": odoo,
        "autofatture": autofatture,
    }


def get_cliente_detail_odoo_lead(lead_id):
    """Detail per un lead Odoo senza P.IVA. Mostra il lead + eventuali altri lead dello stesso partner_name."""
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM odoo_opportunita WHERE odoo_id = ?", (lead_id,)
        ).fetchone()
        if not row:
            return None
        lead = dict(row)
        partner_name = (lead.get("partner_name") or "").strip().lower()
        if partner_name:
            sibling_rows = conn.execute(
                """SELECT * FROM odoo_opportunita
                   WHERE LOWER(TRIM(COALESCE(partner_name,''))) = ?
                   ORDER BY create_date DESC""",
                (partner_name,),
            ).fetchall()
            odoo = [dict(r) for r in sibling_rows]
        else:
            odoo = [lead]
    display_name = lead.get("partner_name") or lead.get("name") or "(senza nome)"
    return {
        "key": f"odoo-{lead_id}",
        "piva": "",
        "display_name": display_name,
        "stripe": None,
        "odoo": odoo,
        "autofatture": [],
    }


def sync_odoo_opportunita():
    try:
        rows = get_all_opportunities()
    except Exception:
        return
    if not rows:
        logging.warning("sync_odoo_opportunita: Odoo ha restituito 0 opportunità — tabella non azzerata (guardia anti-svuotamento)")
        return
    with db() as conn:
        conn.execute(SCHEMA_ODOO_OPPORTUNITA)  # no-op se la tabella esiste già
        conn.execute("DELETE FROM odoo_opportunita")  # DML → dentro la transazione, atomico col commit finale
        for r in rows:
            partner = r.get("partner_id")
            partner_name = partner[1] if isinstance(partner, (list, tuple)) and len(partner) > 1 else None
            stage = r.get("stage_id")
            stage_name = stage[1] if isinstance(stage, (list, tuple)) and len(stage) > 1 else None
            user = r.get("user_id")
            user_name = user[1] if isinstance(user, (list, tuple)) and len(user) > 1 else None
            conn.execute(
                """INSERT INTO odoo_opportunita (odoo_id, name, partner_name, partner_vat, expected_revenue, probability, stage_name, user_name, create_date)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (r["id"], r.get("name"), partner_name, _normalize_vat(r.get("partner_vat")), r.get("expected_revenue") or 0, r.get("probability") or 0, stage_name, user_name, r.get("create_date")),
            )
        conn.execute(
            "INSERT OR REPLACE INTO sync_state (key, value) VALUES (?, ?)",
            ("odoo_last_sync", datetime.utcnow().isoformat() + "Z"),
        )
        conn.commit()


def _get_email_last_sync():
    with db() as conn:
        row = conn.execute("SELECT value FROM sync_state WHERE key = ?", ("email_last_sync",)).fetchone()
        return row["value"] if row else None


def _set_email_last_sync(iso_ts):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sync_state (key, value) VALUES (?, ?)",
            ("email_last_sync", iso_ts),
        )
        conn.commit()


def _att_type_from_filename(filename):
    if "_RC_" in filename.upper():
        return "RC"
    if "_NS_" in filename.upper():
        return "NS"
    return ""


def sync_email(force=False):
    from datetime import datetime, timedelta
    
    if force:
        # Force sync: process in batches
        BATCH_SIZE = 50
        with db() as conn:
            state = conn.execute("SELECT total_ids, processed_ids FROM force_sync_state WHERE id = 1").fetchone()
            
            if not state or state["processed_ids"] >= state["total_ids"]:
                # Start new force sync
                all_ids = get_all_email_ids()
                conn.execute("DELETE FROM force_sync_state WHERE id = 1")
                conn.execute("INSERT INTO force_sync_state (id, total_ids, processed_ids, last_updated) VALUES (1, ?, 0, ?)",
                           (len(all_ids), datetime.utcnow().isoformat() + "Z"))
                processed = 0
            else:
                # Continue existing force sync
                all_ids = get_all_email_ids()
                processed = state["processed_ids"]
            
            # Process next batch
            batch_ids = all_ids[processed:processed + BATCH_SIZE]
            if not batch_ids:
                return {"status": "complete", "total": state["total_ids"] if state else 0}
            
            rows = fetch_emails_by_ids(batch_ids)
            _process_email_rows(rows, conn)
            
            # Update progress
            new_processed = processed + len(batch_ids)
            conn.execute("UPDATE force_sync_state SET processed_ids = ?, last_updated = ? WHERE id = 1",
                       (new_processed, datetime.utcnow().isoformat() + "Z"))
            
            total = state["total_ids"] if state else len(all_ids)
            
            if new_processed >= total:
                return {"status": "complete", "processed": new_processed, "total": total}
            else:
                return {"status": "in_progress", "processed": new_processed, "total": total}
    else:
        # Normal incremental sync
        last = _get_email_last_sync()
        if last:
            try:
                since_dt = datetime.fromisoformat(last.replace("Z", "+00:00")).replace(tzinfo=None) - timedelta(hours=24)
            except Exception:
                since_dt = datetime.utcnow() - timedelta(days=30)
        else:
            since_dt = None
        
        rows = fetch_emails(since=since_dt, limit=None)
        if since_dt is not None and len(rows) == 0:
            rows = fetch_emails(since=None, limit=200)
        
        with db() as conn:
            _process_email_rows(rows, conn)
            _set_email_last_sync(datetime.utcnow().isoformat() + "Z")
        
        return {"status": "complete", "synced": len(rows)}


def _process_email_rows(rows, conn):
    """Process email rows and insert into database"""
    _zips_inserted = 0
    for r in rows:
            mid = r.get("message_id") or ""
            sdi = r.get("sdi_attachments") or []
            zips = r.get("zip_attachments") or []
            att_names = [a["filename"] for a in sdi] if sdi else []
            existing = conn.execute("SELECT 1 FROM email_messages WHERE message_id = ?", (mid,)).fetchone()
            if not existing:
                conn.execute(
                    """INSERT INTO email_messages (message_id, subject, from_addr, to_addr, date_str, snippet, attachments)
                       VALUES (?,?,?,?,?,?,?)""",
                    (mid, r.get("subject"), r.get("from_addr"), r.get("to_addr"), r.get("date_str"), r.get("snippet"), json.dumps(att_names)),
                )
            for a in sdi:
                filename = a["filename"]
                # Salva in email_attachments (come prima)
                exists_att = conn.execute(
                    "SELECT 1 FROM email_attachments WHERE email_message_id = ? AND filename = ?",
                    (mid, filename),
                ).fetchone()
                if not exists_att:
                    conn.execute(
                        """INSERT INTO email_attachments (email_message_id, filename, content, email_date, att_type)
                           VALUES (?,?,?,?,?)""",
                        (mid, filename, a["content"], r.get("date_str", ""), _att_type_from_filename(filename)),
                    )
                # Se è una risposta SDI (_RC_ o _NS_), salva anche in risposte_SDI
                if "_RC_" in filename or "_NS_" in filename:
                    date_str = r.get("date_str", "")
                    try:
                        email_date_iso = parsedate_to_datetime(date_str).strftime("%Y-%m-%dT%H:%M:%S") if date_str else ""
                    except Exception:
                        email_date_iso = ""
                    # Estrai tipo e numero fattura dal filename (es: IT02763130222_01732_RC_002.xml)
                    tipo = "RC" if "_RC_" in filename else "NS"
                    parts = filename.replace(".xml", "").split("_")
                    numero_fattura = parts[1] if len(parts) > 1 else ""
                    
                    exists_risposta = conn.execute(
                        "SELECT 1 FROM risposte_SDI WHERE email_message_id = ? AND filename = ?",
                        (mid, filename),
                    ).fetchone()
                    if not exists_risposta:
                        conn.execute(
                            """INSERT INTO risposte_SDI (email_message_id, filename, content, email_date, email_date_iso, tipo, numero_fattura)
                               VALUES (?,?,?,?,?,?,?)""",
                            (mid, filename, a["content"], date_str, email_date_iso, tipo, numero_fattura),
                        )
            for z in zips:
                exists_zip = conn.execute(
                    "SELECT id FROM zip_inviati WHERE email_message_id = ? AND zip_filename = ?",
                    (mid, z["zip_filename"]),
                ).fetchone()
                if not exists_zip:
                    conn.execute(
                        """INSERT INTO zip_inviati (email_message_id, zip_filename, zip_size, email_date, num_xml)
                           VALUES (?,?,?,?,?)""",
                        (mid, z["zip_filename"], z.get("zip_size", 0), r.get("date_str", ""), len(z.get("xmls", []))),
                    )
                    _zips_inserted += 1
                    zip_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                else:
                    zip_id = exists_zip["id"]
                date_str = r.get("date_str", "")
                try:
                    email_date_iso = parsedate_to_datetime(date_str).strftime("%Y-%m-%dT%H:%M:%S") if date_str else ""
                except Exception:
                    email_date_iso = ""
                for xml in z.get("xmls", []):
                    exists_xml = conn.execute(
                        "SELECT 1 FROM autofatture WHERE zip_id = ? AND filename = ?",
                        (zip_id, xml["filename"]),
                    ).fetchone()
                    if not exists_xml:
                        piva_cliente = _extract_piva_from_xml(xml["content"])
                        conn.execute(
                            """INSERT INTO autofatture (zip_id, filename, content, email_date, email_date_iso, piva_cliente)
                               VALUES (?,?,?,?,?,?)""",
                            (zip_id, xml["filename"], xml["content"], date_str, email_date_iso, piva_cliente),
                        )
    conn.commit()


def _email_row_to_dict(r):
    d = dict(r)
    try:
        raw = json.loads(d["attachments"]) if d.get("attachments") else []
    except (json.JSONDecodeError, TypeError):
        raw = []
    atts = []
    for item in raw:
        if isinstance(item, str):
            atts.append({"filename": item, "url": "/email/allegato/" + quote(item, safe="")})
        elif isinstance(item, dict):
            atts.append({"filename": item.get("filename", ""), "url": "/email/allegato/" + quote(item.get("filename", ""), safe="")})
    d["attachments"] = atts
    # Riformat l'header Date grezzo (RFC 2822, inglese) in italiano leggibile.
    raw_date = d.get("date_str")
    if raw_date:
        try:
            dt = parsedate_to_datetime(raw_date)
            d["date_str"] = _it_datetime(dt) if dt else raw_date
        except (TypeError, ValueError):
            pass
    return d


def get_emails():
    with db() as conn:
        rows = conn.execute("SELECT * FROM email_messages ORDER BY id DESC").fetchall()
        return [_email_row_to_dict(r) for r in rows]


def search_emails(q):
    with db() as conn:
        q = (q or "").strip()
        if not q:
            rows = conn.execute("SELECT * FROM email_messages ORDER BY id DESC").fetchall()
        else:
            pattern = f"%{q}%"
            rows = conn.execute(
                """SELECT * FROM email_messages
                   WHERE subject LIKE ? OR from_addr LIKE ? OR to_addr LIKE ? OR snippet LIKE ?
                   ORDER BY id DESC""",
                (pattern, pattern, pattern, pattern),
            ).fetchall()
        return [_email_row_to_dict(r) for r in rows]


def get_opportunita():
    with db() as conn:
        rows = conn.execute("SELECT * FROM odoo_opportunita ORDER BY create_date DESC").fetchall()
        return [dict(r) for r in rows]


def get_prospect():
    """Opportunità il cui partner non è su Stripe, esclusa fase 'Cliente pagante' (quelle in Cliente pagante sono in Paganti)."""
    with db() as conn:
        rows = conn.execute("""
            SELECT odoo_id, name, partner_name, partner_vat, stage_name, user_name, create_date
            FROM odoo_opportunita
            WHERE (partner_vat IS NULL OR partner_vat NOT IN (SELECT vat FROM stripe_clienti WHERE vat IS NOT NULL))
              AND (stage_name IS NULL OR stage_name != 'Cliente pagante')
            ORDER BY create_date DESC
        """).fetchall()
        return [dict(r) for r in rows]


def get_stripe_senza_odoo_stats():
    """Stats per il tile dashboard "Senza P.IVA su Odoo" — clienti Stripe senza opportunità Odoo con quella P.IVA."""
    rows = get_stripe_senza_odoo()
    items = [
        {
            "name": r.get("business_name") or r.get("stripe_name") or "(senza nome)",
            "vat": r.get("vat"),
            "stripe_email": r.get("stripe_email"),
            "stripe_id": r.get("stripe_id"),
            "created_ago": r.get("created_ago"),
        }
        for r in rows
    ]
    return {"total": len(items), "items": items}


def get_stripe_senza_codice_destinatario_stats():
    """Stats per il tile dashboard "Senza codice destinatario" — clienti Stripe senza CD nei metadata."""
    rows = get_stripe_senza_codice_destinatario()
    items = [
        {
            "name": r.get("business_name") or r.get("stripe_name") or "(senza nome)",
            "vat": r.get("vat"),
            "stripe_email": r.get("stripe_email"),
            "stripe_id": r.get("stripe_id"),
            "created_ago": r.get("created_ago"),
        }
        for r in rows
    ]
    return {"total": len(items), "items": items}


def _verify_password(password, stored_hash, salt):
    derived = hashlib.scrypt(
        password.encode(), salt=bytes.fromhex(salt),
        n=16384, r=8, p=1, dklen=64,
    )
    return hmac.compare_digest(derived.hex(), stored_hash)


_check_db_integrity()
_db_initialized = False


@app.before_request
def ensure_db():
    global _db_initialized
    if not _db_initialized:
        init_db()
        actions_db.init_db()
        _db_initialized = True


@app.before_request
def require_login():
    if (
        request.path == "/login"
        or request.path.startswith("/static")
        or request.path == "/api/version"
        or request.path.startswith("/api/internal/")
    ):
        return
    if not session.get("authenticated"):
        return redirect("/login")


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("authenticated"):
        return redirect("/")
    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        admin_user = os.getenv("ADMIN_USERNAME")
        admin_hash = os.getenv("ADMIN_PASSWORD_HASH")
        admin_salt = os.getenv("ADMIN_PASSWORD_SALT")
        if admin_user and admin_hash and admin_salt and username == admin_user:
            if _verify_password(password, admin_hash, admin_salt):
                session["authenticated"] = True
                return redirect("/")
        error = "Credenziali non valide"
    return render_template("login.html", error=error)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect("/login")


@app.route("/")
def dashboard():
    month = _competenza_default_month()
    customers, _, month_eff, _ready = _get_controllo_competenza(month)
    af_problems = [c for c in customers if c["has_problems"]]
    pag_subscriptions, _ = _pagamenti_subscriptions()
    pag_errori = [s for s in pag_subscriptions if s["is_error"]]
    return render_template(
        "dashboard.html",
        af_month=month_eff,
        af_problems=af_problems,
        af_all=customers,
        issued_sync=_issued_da_allineare(),
        pag_errori=pag_errori,
        scarti_aperti=_get_scarti_aperti(),
        pipeline_cols=PIPELINE_COLS,
        pipeline_counts=_pipeline_counts(),
        senza_piva_stats=get_stripe_senza_odoo_stats(),
        senza_cd_stats=get_stripe_senza_codice_destinatario_stats(),
        eventi_settimana=_get_week_grid_events(),
    )


@app.route("/contesto")
def contesto():
    return render_template(
        "contesto.html",
        context_overview_html=_render_context_overview(),
        context_sections=_parse_context_md(),
    )


def _is_summary_event(t):
    # Accetta sia `meeting.summary.created` (BlueDot reale) sia `video.summary.created`
    # (vecchio formato di esempio nella doc). Match per suffisso → robusto a future varianti.
    return isinstance(t, str) and t.endswith(".summary.created")


def _is_transcript_event(t):
    return isinstance(t, str) and t.endswith(".transcript.created")


def _norm_meet_url(url: str | None) -> str | None:
    """Normalizza un Google Meet URL per il join BlueDot↔gcal (strip https://, lowercase, no trailing /)."""
    if not url:
        return None
    u = url.strip().lower()
    for prefix in ("https://", "http://"):
        if u.startswith(prefix):
            u = u[len(prefix):]
    return u.rstrip("/") or None


def _render_bluedot_summary(payload: dict) -> str | None:
    """Markdown → HTML del riassunto BlueDot (summaryV2 preferito). None se assente."""
    md_source = payload.get("summaryV2") or payload.get("summary") or ""
    if not md_source:
        return None
    return markdown.markdown(md_source, extensions=["extra"])


def _bluedot_summaries_by_meet_url() -> dict:
    """Mappa {meet_url_norm: {meeting_id, summary_html, has_transcript}}.

    Legge bluedot_events, raggruppa per meeting_id normalizzato; per ogni gruppo
    prende il primo evento .summary.created e segnala la presenza del transcript.
    """
    with db() as conn:
        rows = conn.execute(
            "SELECT event_type, meeting_id, payload FROM bluedot_events "
            "WHERE meeting_id IS NOT NULL "
            "ORDER BY meeting_id, bluedot_created_at DESC, received_at DESC"
        ).fetchall()
    out: dict = {}
    for r in rows:
        key = _norm_meet_url(r["meeting_id"])
        if not key:
            continue
        entry = out.setdefault(key, {
            "meeting_id": r["meeting_id"],
            "summary_html": None,
            "has_transcript": False,
        })
        try:
            payload = json.loads(r["payload"]) if r["payload"] else {}
        except (ValueError, TypeError):
            payload = {}
        if _is_summary_event(r["event_type"]) and entry["summary_html"] is None:
            entry["summary_html"] = _render_bluedot_summary(payload)
        if _is_transcript_event(r["event_type"]):
            entry["has_transcript"] = True
    return out


TEAM_SPEAKER_NAMES = (
    "claudio postinghel",
    "gian claudio merella",
    "luca federizzi",
    "riccardo frau",
)


def _is_team_speaker(speaker):
    if not isinstance(speaker, str):
        return False
    s = speaker.strip().lower()
    return any(name in s for name in TEAM_SPEAKER_NAMES)


def _bluedot_meeting_rows():
    """Aggrega gli eventi BlueDot per meeting_id.

    Per ogni meeting torna un dict con: meeting_id, title, created_at (unix s,
    il più grande tra gli eventi), attendees (lista, dal payload più recente),
    has_summary, has_transcript, summary_event_id, transcript_event_id, duration.
    """
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, event_type, meeting_id, video_id, title,
                   bluedot_created_at, received_at, payload
              FROM bluedot_events
             WHERE meeting_id IS NOT NULL
             ORDER BY meeting_id, bluedot_created_at DESC, received_at DESC
            """
        ).fetchall()

    meetings = {}
    for r in rows:
        mid = r["meeting_id"]
        m = meetings.setdefault(
            mid,
            {
                "meeting_id": mid,
                "title": r["title"],
                "created_at": r["bluedot_created_at"],
                "attendees": [],
                "has_summary": False,
                "has_transcript": False,
                "summary_event_id": None,
                "transcript_event_id": None,
                "duration": None,
            },
        )
        try:
            payload = json.loads(r["payload"]) if r["payload"] else {}
        except (ValueError, TypeError):
            payload = {}

        att = payload.get("attendees")
        if isinstance(att, list) and not m["attendees"]:
            m["attendees"] = [a for a in att if isinstance(a, str)]

        if _is_summary_event(r["event_type"]):
            m["has_summary"] = True
            if m["summary_event_id"] is None:
                m["summary_event_id"] = r["id"]
        elif _is_transcript_event(r["event_type"]):
            m["has_transcript"] = True
            if m["transcript_event_id"] is None:
                m["transcript_event_id"] = r["id"]
            dur = payload.get("duration")
            if isinstance(dur, (int, float)) and m["duration"] is None:
                m["duration"] = dur

        if r["bluedot_created_at"] and (
            m["created_at"] is None or r["bluedot_created_at"] > m["created_at"]
        ):
            m["created_at"] = r["bluedot_created_at"]
        if not m["title"] and r["title"]:
            m["title"] = r["title"]

    out = list(meetings.values())
    out.sort(key=lambda x: x["created_at"] or 0, reverse=True)
    return out


@app.route("/meeting-bluedot")
def meeting_bluedot():
    meetings = _bluedot_meeting_rows()
    return render_template("meeting_bluedot.html", meetings=meetings)


@app.route("/meeting-bluedot/<path:meeting_id>")
def meeting_bluedot_detail(meeting_id):
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, event_type, meeting_id, video_id, title,
                   bluedot_created_at, received_at, payload
              FROM bluedot_events
             WHERE meeting_id = ?
             ORDER BY bluedot_created_at DESC, received_at DESC
            """,
            (meeting_id,),
        ).fetchall()

    if not rows:
        return f"Meeting {meeting_id} non trovato", 404

    summary_html = None
    summary_payload = None
    transcript = None
    transcript_payload = None
    attendees = []
    created_at = None
    title = None
    duration = None

    for r in rows:
        try:
            payload = json.loads(r["payload"]) if r["payload"] else {}
        except (ValueError, TypeError):
            payload = {}

        if not title and r["title"]:
            title = r["title"]
        if r["bluedot_created_at"] and (
            created_at is None or r["bluedot_created_at"] > created_at
        ):
            created_at = r["bluedot_created_at"]
        att = payload.get("attendees")
        if isinstance(att, list) and not attendees:
            attendees = [a for a in att if isinstance(a, str)]

        if _is_summary_event(r["event_type"]) and summary_html is None:
            md_source = payload.get("summaryV2") or payload.get("summary") or ""
            if md_source:
                summary_html = markdown.markdown(md_source, extensions=["extra"])
            summary_payload = payload
        elif _is_transcript_event(r["event_type"]) and transcript is None:
            tr = payload.get("transcript")
            if isinstance(tr, list):
                transcript = [
                    {
                        "speaker": item.get("speaker", ""),
                        "text": item.get("text", ""),
                        "is_team": _is_team_speaker(item.get("speaker", "")),
                    }
                    for item in tr
                    if isinstance(item, dict)
                ]
            dur = payload.get("duration")
            if isinstance(dur, (int, float)):
                duration = dur
            transcript_payload = payload

    meeting = {
        "meeting_id": meeting_id,
        "title": title,
        "created_at": created_at,
        "attendees": attendees,
        "duration": duration,
        "summary_html": summary_html,
        "transcript": transcript,
        "summary_payload": summary_payload,
        "transcript_payload": transcript_payload,
    }
    return render_template("meeting_bluedot_detail.html", meeting=meeting)


# ─────────────────────────── Google Calendar ───────────────────────────


@app.route("/calendar")
def calendar_page():
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Europe/Rome")
    except Exception:
        tz = None

    with db() as conn:
        rows = conn.execute(
            """
            SELECT uid, summary, description, location, dtstart, dtend,
                   all_day, status, organizer, attendees, url,
                   created, last_modified
              FROM gcal_events
             WHERE date(dtstart) >= date('now', '-30 days')
             ORDER BY dtstart ASC
            """
        ).fetchall()

    today = datetime.now(tz).date() if tz else datetime.utcnow().date()

    def _parse(ts: str | None) -> datetime | None:
        if not ts:
            return None
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    upcoming: list[dict] = []
    past: list[dict] = []

    for r in rows:
        all_day = bool(r["all_day"])
        if all_day:
            try:
                event_date = date.fromisoformat(r["dtstart"])
            except ValueError:
                continue
            time_label = "Tutto il giorno"
        else:
            start_dt = _parse(r["dtstart"])
            if start_dt is None:
                continue
            local = start_dt.astimezone(tz) if tz else start_dt
            event_date = local.date()
            end_dt = _parse(r["dtend"])
            end_local = end_dt.astimezone(tz) if (end_dt and tz) else end_dt
            if end_local and end_local.date() == event_date:
                time_label = f"{local.strftime('%H:%M')}–{end_local.strftime('%H:%M')}"
            else:
                time_label = local.strftime("%H:%M")

        try:
            attendees = json.loads(r["attendees"]) if r["attendees"] else []
        except (ValueError, TypeError):
            attendees = []

        def _fmt_iso(ts: str | None) -> str | None:
            dt = _parse(ts)
            if dt is None:
                return None
            local = dt.astimezone(tz) if tz else dt
            return _it_datetime(local)

        details: list[dict] = []
        details.append({"label": "Title", "value": r["summary"] or "(senza titolo)", "kind": "text"})
        if r["description"]:
            details.append({"label": "Description", "value": r["description"], "kind": "multiline"})
        if r["location"]:
            details.append({"label": "Place", "value": r["location"], "kind": "auto-link"})
        if r["status"]:
            details.append({"label": "Status", "value": r["status"], "kind": "text"})
        if r["organizer"]:
            details.append({"label": "Organizer", "value": r["organizer"], "kind": "text"})
        if attendees:
            details.append({"label": "Attendees", "value": attendees, "kind": "list"})
        if r["url"]:
            details.append({"label": "URL", "value": r["url"], "kind": "auto-link"})
        if r["created"]:
            details.append({"label": "Created", "value": _fmt_iso(r["created"]), "kind": "text"})
        if r["last_modified"]:
            details.append({"label": "Modified", "value": _fmt_iso(r["last_modified"]), "kind": "text"})
        details.append({"label": "UID", "value": r["uid"], "kind": "mono"})

        item = {
            "uid": r["uid"],
            "date": event_date,
            "date_pretty": _it_date(event_date),
            "date_label": _format_day_label(event_date, today),
            "is_today": event_date == today,
            "time_label": time_label,
            "all_day": all_day,
            "summary": r["summary"] or "(senza titolo)",
            "cancelled": (r["status"] or "").upper() == "CANCELLED",
            "details": details,
        }
        (upcoming if event_date >= today else past).append(item)

    past.reverse()  # most-recent past first under "Ultimi 30 giorni"

    return render_template(
        "calendar.html",
        upcoming=upcoming,
        past=past,
        total=len(upcoming) + len(past),
    )


def _clean_event_title(summary: str | None) -> str:
    """Rimuove il ridondante "Autofatturiamo" dai titoli Google Calendar.

    Esempi:
      "Demo Autofatturiamo — Umberto"                            → "Demo Umberto"
      "Autofatturiamo - Assistenza tra Autofatturiamo e Chiara" → "Assistenza Chiara"
      "Assistenza tra Autofatturiamo e Cristina Schirru"        → "Assistenza Cristina Schirru"
      "Autofatturiamo - Primo invio"                             → "Primo invio"
      "15 Minuti per Te [Gratis] con Vita da Host"              → invariato
    """
    if not summary:
        return summary or ""
    t = summary.strip()
    # 1. "Assistenza tra Autofatturiamo e X" → "Assistenza X" (prima degli step generici)
    t = re.sub(r"\bAssistenza tra Autofatturiamo e\b", "Assistenza", t, flags=re.I)
    # 2. Prefisso "Autofatturiamo <sep> ..." a inizio titolo
    t = re.sub(r"^Autofatturiamo\s*[—\-:]\s*", "", t, flags=re.I)
    # 3. "<tipo> Autofatturiamo <sep> Nome" → "<tipo> Nome"
    t = re.sub(r"\s*Autofatturiamo\s*[—\-]\s*", " ", t, flags=re.I)
    # 4. Eventuale "Autofatturiamo" isolato residuo
    t = re.sub(r"\s*\bAutofatturiamo\b\s*", " ", t, flags=re.I)
    t = re.sub(r"\s{2,}", " ", t).strip()
    return t or summary  # fallback se svuota per qualche motivo


def _event_local(r, tz) -> tuple | None:
    """Parsa una riga gcal_events → (event_date, time_label, all_day) oppure None se non parsabile."""
    all_day = bool(r["all_day"])
    if all_day:
        try:
            return date.fromisoformat(r["dtstart"]), "Tutto il giorno", True
        except ValueError:
            return None
    try:
        start_dt = datetime.fromisoformat(r["dtstart"])
    except ValueError:
        return None
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=timezone.utc)
    local = start_dt.astimezone(tz) if tz else start_dt
    event_date = local.date()
    time_label = local.strftime("%H:%M")
    if r["dtend"]:
        try:
            end_dt = datetime.fromisoformat(r["dtend"])
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            end_local = end_dt.astimezone(tz) if tz else end_dt
            if end_local.date() == event_date:
                time_label = f"{local.strftime('%H:%M')}–{end_local.strftime('%H:%M')}"
        except ValueError:
            pass
    return event_date, time_label, False


def _format_day_label(d: date, today: date) -> str:
    delta = (d - today).days
    if delta == 0:
        return "Oggi"
    if delta == 1:
        return "Domani"
    if delta == -1:
        return "Ieri"
    giorni = ["Lun", "Mar", "Mer", "Gio", "Ven", "Sab", "Dom"]
    mesi = ["gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic"]
    return f"{giorni[d.weekday()]} {d.day} {mesi[d.month - 1]} {d.year}"


def _get_upcoming_events(limit: int = 4) -> list[dict]:
    """Ritorna i prossimi eventi da gcal_events (non cancellati, da oggi in poi)."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Europe/Rome")
    except Exception:
        tz = None

    today = datetime.now(tz).date() if tz else datetime.utcnow().date()

    with db() as conn:
        rows = conn.execute(
            """
            SELECT uid, summary, dtstart, dtend, all_day, status
              FROM gcal_events
             WHERE date(dtstart) >= date('now')
               AND (status IS NULL OR upper(status) != 'CANCELLED')
             ORDER BY dtstart ASC
             LIMIT :limit
            """,
            {"limit": limit},
        ).fetchall()

    result: list[dict] = []
    for r in rows:
        parsed = _event_local(r, tz)
        if parsed is None:
            continue
        event_date, time_label, all_day = parsed
        result.append({
            "date_label": _format_day_label(event_date, today),
            "time_label": time_label,
            "summary": r["summary"] or "(senza titolo)",
            "is_today": event_date == today,
            "all_day": all_day,
        })

    return result


def _get_week_grid_events() -> list[list[dict]]:
    """Ritorna una griglia 2×7 (settimana corrente + prossima) per la dashboard.

    Struttura ritornata:
      [ [day_dict × 7], [day_dict × 7] ]
    dove ogni day_dict = {weekday_label, day_num, is_today, is_past, events: [{summary, time_label, all_day}]}
    """
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Europe/Rome")
    except Exception:
        tz = None

    today = datetime.now(tz).date() if tz else datetime.utcnow().date()
    monday = today - timedelta(days=today.weekday())           # lunedì della settimana corrente
    end_day = monday + timedelta(days=13)                      # domenica della settimana prossima

    with db() as conn:
        rows = conn.execute(
            """
            SELECT summary, dtstart, dtend, all_day, status
              FROM gcal_events
             WHERE date(dtstart) >= :start AND date(dtstart) <= :end
               AND (status IS NULL OR upper(status) != 'CANCELLED')
             ORDER BY dtstart ASC
            """,
            {"start": monday.isoformat(), "end": end_day.isoformat()},
        ).fetchall()

    events_by_date: dict[date, list[dict]] = {}
    for r in rows:
        parsed = _event_local(r, tz)
        if parsed is None:
            continue
        event_date, time_label, all_day = parsed
        events_by_date.setdefault(event_date, []).append({
            "summary": _clean_event_title(r["summary"]) or "(senza titolo)",
            "time_label": time_label,
            "all_day": all_day,
        })

    weeks = []
    for w in range(2):
        days = []
        for i in range(7):
            d = monday + timedelta(days=w * 7 + i)
            days.append({
                "date_label": f"{d.day} {_MESI_IT_ABBR[d.month]}",
                "is_today": d == today,
                "is_past": d < today,
                "events": events_by_date.get(d, []),
            })
        weeks.append(days)
    return weeks


# ─────────────────────────── WhatsApp viewer ───────────────────────────
# Legge in read-only le conversazioni WhatsApp dalle session DB di NanoClaw.
# `data/v2.db` → quali messaging_group sono WhatsApp e quali session le servono.
# `data/v2-sessions/<ag>/<sess>/inbound.db|outbound.db` → testo dei messaggi.

NANOCLAW_DATA_DIR = _BASE_DIR.parent.parent / "data"

try:
    from zoneinfo import ZoneInfo  # py>=3.9
    _TZ_ROME = ZoneInfo("Europe/Rome")
except Exception:
    _TZ_ROME = None  # fallback: lasciamo naive (degradazione graziosa)


def _ro_sqlite(db_path: Path) -> sqlite3.Connection | None:
    if not db_path.exists():
        return None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_ts(value: str) -> datetime | None:
    """Ritorna datetime aware in Europe/Rome (o naive se zoneinfo manca).

    Formati supportati:
    - "YYYY-MM-DDTHH:MM:SS[.fff]Z"   → UTC esplicito (ISO con Z)
    - "YYYY-MM-DDTHH:MM:SS[+HH:MM]"  → ISO con offset (aware)
    - "YYYY-MM-DDTHH:MM:SS"          → naive, locale Rome (Python datetime.now().isoformat())
    - "YYYY-MM-DD HH:MM:SS"          → naive, UTC (SQLite datetime('now'))
    """
    if not value:
        return None
    s = value.strip()
    sqlite_format = " " in s and "T" not in s
    try:
        if s.endswith("Z"):
            dt = datetime.fromisoformat(s[:-1] + "+00:00")
        elif sqlite_format:
            dt = datetime.fromisoformat(s.replace(" ", "T"))
        else:
            dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None and _TZ_ROME is not None:
        if sqlite_format:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.replace(tzinfo=_TZ_ROME)
    if _TZ_ROME is not None and dt.tzinfo is not None:
        dt = dt.astimezone(_TZ_ROME)
    return dt


def _jid_short(jid: str) -> str:
    """`393791234555@s.whatsapp.net` → `393791234555`; gruppi: id puro."""
    return jid.split("@", 1)[0] if "@" in jid else jid


def _phone_fmt(jid: str) -> str:
    """Stampa il numero come `+39 379 123 4555` (best-effort, IT-friendly)."""
    n = _jid_short(jid)
    if not n.isdigit():
        return n
    if n.startswith("39") and len(n) >= 11:
        return f"+{n[:2]} {n[2:5]} {n[5:8]} {n[8:]}"
    return f"+{n}"


def _whatsapp_session_paths():
    """Yield (mg_row, [(inbound_path, outbound_path), ...]) per ogni mg WhatsApp."""
    central = _ro_sqlite(NANOCLAW_DATA_DIR / "v2.db")
    if central is None:
        return
    try:
        mgs = central.execute(
            "SELECT id, platform_id, name, is_group "
            "FROM messaging_groups WHERE channel_type='whatsapp'"
        ).fetchall()
        for mg in mgs:
            sessions = central.execute(
                "SELECT id, agent_group_id FROM sessions WHERE messaging_group_id=?",
                (mg["id"],),
            ).fetchall()
            paths = []
            for s in sessions:
                base = NANOCLAW_DATA_DIR / "v2-sessions" / s["agent_group_id"] / s["id"]
                inb = base / "inbound.db"
                outb = base / "outbound.db"
                if inb.exists() or outb.exists():
                    paths.append((inb, outb))
            yield mg, paths
    finally:
        central.close()


def _whatsapp_avatar_url(jid: str | None) -> str | None:
    """URL static relativo dell'avatar WhatsApp di una chat se il file esiste.

    Il file viene scaricato dal host NanoClaw (src/channels/whatsapp.ts:
    ensureProfilePicture) e salvato in <static>/avatars/<jid_sanitized>.jpg
    quando il messaging_group si presenta per la prima volta. Qui controlliamo
    solo l'esistenza: niente fetch, niente fallback HTTP.
    """
    if not jid:
        return None
    safe = re.sub(r"[@.:]", "_", jid)
    filename = f"{safe}.jpg"
    static_dir = Path(app.static_folder or "static") / "avatars"
    if not (static_dir / filename).exists():
        return None
    return f"/static/avatars/{filename}"


def _whatsapp_conversations() -> list[dict]:
    """Una entry per ogni messaging_group WhatsApp che ha almeno un messaggio."""
    out = []
    for mg, session_paths in _whatsapp_session_paths():
        last_ts: datetime | None = None
        last_text = ""
        last_from_me = False
        last_sender_name = ""
        for inb_path, outb_path in session_paths:
            inb = _ro_sqlite(inb_path)
            if inb is not None:
                try:
                    row = inb.execute(
                        "SELECT timestamp, content FROM messages_in "
                        "WHERE channel_type='whatsapp' ORDER BY timestamp DESC LIMIT 1"
                    ).fetchone()
                    if row:
                        ts = _parse_ts(row["timestamp"])
                        if ts and (last_ts is None or ts > last_ts):
                            last_ts = ts
                            try:
                                payload = json.loads(row["content"])
                                last_text = payload.get("text", "") or ""
                                last_sender_name = payload.get("senderName", "") or ""
                            except (ValueError, TypeError):
                                last_text = ""
                            last_from_me = False
                finally:
                    inb.close()
            outb = _ro_sqlite(outb_path)
            if outb is not None:
                try:
                    row = outb.execute(
                        "SELECT timestamp, content FROM messages_out "
                        "WHERE channel_type='whatsapp' ORDER BY timestamp DESC LIMIT 1"
                    ).fetchone()
                    if row:
                        ts = _parse_ts(row["timestamp"])
                        if ts and (last_ts is None or ts > last_ts):
                            last_ts = ts
                            try:
                                payload = json.loads(row["content"])
                                last_text = payload.get("text", "") or ""
                            except (ValueError, TypeError):
                                last_text = ""
                            last_from_me = True
                finally:
                    outb.close()
        if last_ts is None:
            continue  # nessun messaggio: salta
        display_name = mg["name"] or last_sender_name or _phone_fmt(mg["platform_id"])
        out.append({
            "jid": mg["platform_id"],
            "display_name": display_name,
            "is_group": bool(mg["is_group"]),
            "last_ts": last_ts,
            "last_text": last_text,
            "last_from_me": last_from_me,
            "avatar_url": _whatsapp_avatar_url(mg["platform_id"]),
        })
    out.sort(key=lambda c: c["last_ts"], reverse=True)
    return out


def _whatsapp_messages(jid: str) -> dict | None:
    """Tutti i messaggi di una conversazione, ordinati cronologicamente asc."""
    central = _ro_sqlite(NANOCLAW_DATA_DIR / "v2.db")
    if central is None:
        return None
    try:
        mg = central.execute(
            "SELECT id, platform_id, name, is_group FROM messaging_groups "
            "WHERE channel_type='whatsapp' AND platform_id=?",
            (jid,),
        ).fetchone()
        if mg is None:
            return None
        sessions = central.execute(
            "SELECT id, agent_group_id FROM sessions WHERE messaging_group_id=?",
            (mg["id"],),
        ).fetchall()
    finally:
        central.close()

    # Carichiamo PRIMA le azioni (incluse le done consegnate) così sappiamo
    # quali messaggi outbound dedupare: ogni azione consegnata via host porta
    # già il suo testo + tutti i metadati (#id, stato, reason), quindi il
    # messaggio "gemello" che atterra in messages_in/_out sarebbe un doppione.
    action_rows = actions_db.list_for_whatsapp_jid(jid, include_delivered=True)

    # Set di msg_id (id originale in messages_out) per cui esiste un'azione di
    # qualunque stato. Serve per dedupare le bolle outbound: se un messaggio è
    # gestito come pending_action, l'unica fonte di verità è la bolla 'action'
    # (con stato pending/approved/done/rejected). La bolla "normale" letta da
    # messages_out sarebbe un gemello visibile prima dell'approvazione.
    action_msg_ids: set[str] = set()
    for arow in action_rows:
        try:
            _payload_for_ids = json.loads(arow["payload_json"])
        except (ValueError, TypeError):
            _payload_for_ids = None
        if isinstance(_payload_for_ids, dict):
            _mid = _payload_for_ids.get("msg_id")
            if isinstance(_mid, str) and _mid:
                action_msg_ids.add(_mid)

    delivered_index: list[tuple[datetime, str]] = []
    for arow in action_rows:
        if arow["status"] != "done":
            continue
        if not arow["host_delivered_at"] or arow["host_delivery_error"]:
            continue
        delivered_ts = _parse_ts(arow["host_delivered_at"])
        if delivered_ts is None:
            continue
        try:
            payload = json.loads(arow["payload_json"])
        except (ValueError, TypeError):
            continue
        content_raw = payload.get("content_json")
        content: dict = {}
        if isinstance(content_raw, str):
            try:
                parsed = json.loads(content_raw)
                if isinstance(parsed, dict):
                    content = parsed
            except (ValueError, TypeError):
                content = {}
        delivered_text = content.get("text", "") if isinstance(content.get("text"), str) else ""
        delivered_index.append((delivered_ts, delivered_text.strip()))

    def _is_action_duplicate(ts: datetime, text: str) -> bool:
        # Match conservativo: timestamp entro ±90s dall'host_delivered_at e
        # testo identico (strippato). Non basta uno dei due — i timestamp da
        # soli matcherebbero messaggi diversi spediti a raffica, e il testo
        # da solo matcherebbe messaggi storici riusati.
        if not text:
            return False
        target = text.strip()
        for d_ts, d_text in delivered_index:
            if d_text != target:
                continue
            if abs((ts - d_ts).total_seconds()) <= 90:
                return True
        return False

    messages = []
    sender_name_fallback = ""
    for s in sessions:
        base = NANOCLAW_DATA_DIR / "v2-sessions" / s["agent_group_id"] / s["id"]
        # Bozze trattenute dal gate di approvazione: il host marca il
        # messages_out come consegnato ritornando il sentinel sintetico
        # `pending-approval:<id>` (vedi src/delivery.ts), ma il messaggio NON
        # è mai stato inviato al cliente. La tabella `delivered` è l'unica
        # fonte di verità autoritativa: queste righe vanno escluse dalla
        # timeline (la bolla reale, se l'azione esiste ancora ed è stata
        # consegnata, arriva dal ramo pending_actions sotto).
        pending_approval_out_ids: set[str] = set()
        inb = _ro_sqlite(base / "inbound.db")
        if inb is not None:
            try:
                for drow in inb.execute(
                    "SELECT message_out_id FROM delivered "
                    "WHERE platform_message_id LIKE 'pending-approval:%'"
                ):
                    if drow["message_out_id"]:
                        pending_approval_out_ids.add(drow["message_out_id"])
            except sqlite3.Error:
                pass
            try:
                for row in inb.execute(
                    "SELECT timestamp, content FROM messages_in "
                    "WHERE channel_type='whatsapp' ORDER BY timestamp ASC"
                ):
                    ts = _parse_ts(row["timestamp"])
                    if ts is None:
                        continue
                    try:
                        payload = json.loads(row["content"])
                    except (ValueError, TypeError):
                        continue
                    text = payload.get("text", "") or ""
                    sender_name = payload.get("senderName", "") or ""
                    if sender_name:
                        sender_name_fallback = sender_name
                    has_attachments = bool(payload.get("attachments"))
                    from_me = bool(payload.get("fromMe", False))
                    if from_me and _is_action_duplicate(ts, text):
                        continue
                    messages.append({
                        "ts": ts,
                        "kind": "message",
                        "text": text,
                        "from_me": from_me,
                        "sender_name": sender_name,
                        "has_attachments": has_attachments,
                        "side": "out" if from_me else "in",
                    })
            finally:
                inb.close()
        outb = _ro_sqlite(base / "outbound.db")
        if outb is not None:
            try:
                for row in outb.execute(
                    "SELECT id, timestamp, content FROM messages_out "
                    "WHERE channel_type='whatsapp' ORDER BY timestamp ASC"
                ):
                    ts = _parse_ts(row["timestamp"])
                    if ts is None:
                        continue
                    try:
                        payload = json.loads(row["content"])
                    except (ValueError, TypeError):
                        continue
                    text = payload.get("text", "") or ""
                    # Bozza trattenuta in approvazione e mai inviata al cliente:
                    # esclusa in modo autoritativo (la verità è in `delivered`).
                    if row["id"] in pending_approval_out_ids:
                        continue
                    # Dedup per identità: se esiste una pending_action per
                    # questo msg_id, la bolla 'action' è l'unica fonte di
                    # verità (mostra anche lo stato pending). Lasciamo
                    # l'heuristic _is_action_duplicate come fallback per
                    # casi senza msg_id associata.
                    if row["id"] in action_msg_ids:
                        continue
                    if _is_action_duplicate(ts, text):
                        continue
                    messages.append({
                        "ts": ts,
                        "kind": "message",
                        "text": text,
                        "from_me": True,
                        "sender_name": "",
                        "has_attachments": False,
                        "side": "out",
                    })
            finally:
                outb.close()

    # Merge: pending_actions whatsapp_reply per questo JID. Includiamo anche
    # le done già consegnate (i messaggi gemelli su messages_in/_out sono
    # stati skippati sopra), così ogni bolla outbound conserva i metadati di
    # azione (badge stato, #id, eventuale errore). Le rejected sono escluse:
    # rappresentano messaggi mai inviati al cliente. Mostrarli qui nel viewer
    # di chat creerebbe ambiguità sulla cronologia reale della conversazione.
    for arow in action_rows:
        if arow["status"] == "rejected":
            continue
        ts = _parse_ts(arow["created_at"])
        if ts is None:
            continue
        try:
            payload = json.loads(arow["payload_json"])
        except (ValueError, TypeError):
            continue
        content_raw = payload.get("content_json")
        content = {}
        if isinstance(content_raw, str):
            try:
                parsed = json.loads(content_raw)
                if isinstance(parsed, dict):
                    content = parsed
            except (ValueError, TypeError):
                content = {}
        body = content.get("text", "") if isinstance(content.get("text"), str) else ""
        files = payload.get("files") or content.get("files") or []
        delivered = bool(arow["host_delivered_at"]) and not arow["host_delivery_error"]
        # Quando l'azione è done+delivered usiamo l'host_delivered_at come
        # posizione cronologica nella chat (l'invio reale), non il created_at
        # (che è quando l'agent ha proposto l'azione, spesso molto prima).
        if delivered:
            delivered_ts = _parse_ts(arow["host_delivered_at"])
            if delivered_ts is not None:
                ts = delivered_ts
        messages.append({
            "ts": ts,
            "text": body,
            "from_me": True,
            "sender_name": "",
            "has_attachments": bool(files),
            "side": "out",
            "kind": "action",
            "action_id": arow["id"],
            "action_status": arow["status"],
            "delivered": delivered,
            "files": files,
            "error": arow["error"] or arow["host_delivery_error"],
            "reason": payload.get("reason"),
            "recipient_label": payload.get("recipient_label"),
        })

    # Meeting cal.com collegati: appaiono inline alla loro dtstart, non in un
    # pannello separato. Passati al loro punto temporale, futuri in fondo
    # (sono dopo l'ultimo messaggio reale). CANCELLED inclusi col loro stile
    # barrato lato template. Riusa la stessa fonte di `/whatsapp/<jid>` per la
    # vista "card", così resta una sola query di link.
    for mt in _meetings_for_messaging_group_id(mg["id"]):
        ts_mt = _parse_ts(mt["dtstart"])
        if ts_mt is None:
            continue
        messages.append({
            "ts": ts_mt,
            "kind": "meeting",
            "meeting": mt,
        })

    if not messages:
        return None

    messages.sort(key=lambda m: m["ts"])
    display_name = mg["name"] or sender_name_fallback or _phone_fmt(mg["platform_id"])
    return {
        "jid": mg["platform_id"],
        "messaging_group_id": mg["id"],
        "display_name": display_name,
        "is_group": bool(mg["is_group"]),
        "phone_fmt": _phone_fmt(mg["platform_id"]),
        "avatar_url": _whatsapp_avatar_url(mg["platform_id"]),
        "messages": messages,
        "has_whatsapp": True,
    }


@app.route("/whatsapp")
def whatsapp_page():
    conversations = _whatsapp_conversations()
    return render_template(
        "whatsapp.html",
        conversations=conversations,
        selected=None,
        embed=bool(request.args.get("embed")),
    )


# ── "Contesto caricato": elenco dei file che NanoClaw mette nel prompt
# dell'agente per una specifica chat WhatsApp. Strumento di debug: vediamo
# in cima alla chat ogni file (host base + fragments + CLAUDE.local +
# context-client + scheda cliente + eventi) con contenuto integrale inline.
# Read-only su un DB di terzi (data/v2.db di NanoClaw) — apriamo in URI
# `mode=ro` per non interferire mai col writer del bot.

_FRAGMENT_ROLES = {
    "module-agents": "Tool: lancia sub-agent",
    "module-cli": "Tool: ncl admin CLI",
    "module-core": "Core: workflow base agent",
    "module-interactive": "Tool: pending questions",
    "module-operational-actions": "Tool: operational actions",
    "module-scheduling": "Tool: scheduling messaggi",
    "module-send-chart": "Tool: invio grafici",
    "skill-onecli-gateway": "Skill: gateway credenziali OneCLI",
}


def _resolve_app_symlink(host_path: Path) -> Path:
    """I fragments in .claude-fragments/ sono symlink che puntano a path
    container-side (/app/src/..., /app/skills/...). Sul host quei file
    vivono dentro container/agent-runner/ (per src/) o container/ (per
    skills/). Rimappa senza dipendere dall'esistenza del path container."""
    if not host_path.is_symlink():
        return host_path
    target = os.readlink(host_path)
    if target.startswith("/app/src/"):
        return _REPO_ROOT / "container" / "agent-runner" / target[len("/app/"):]
    if target.startswith("/app/skills/"):
        return _REPO_ROOT / "container" / target[len("/app/"):]
    if target.startswith("/app/"):
        return _REPO_ROOT / "container" / "agent-runner" / target[len("/app/"):]
    return host_path


def _read_file_block(host_path: Path, *, category: str, title: str, role: str,
                     key: str | None = None) -> dict:
    """Costruisce il dict-blocco per un singolo file."""
    real = _resolve_app_symlink(host_path)
    try:
        content = real.read_text(encoding="utf-8")
        exists = True
        size = len(content.encode("utf-8"))
        lines = content.count("\n") + (0 if content.endswith("\n") or not content else 1)
    except (OSError, UnicodeDecodeError):
        content = ""
        exists = False
        size = 0
        lines = 0
    try:
        path_rel = str(host_path.relative_to(_REPO_ROOT))
    except ValueError:
        path_rel = str(host_path)
    return {
        "key": key or path_rel,
        "category": category,
        "title": title,
        "role": role,
        "path_rel": path_rel,
        "lines": lines,
        "bytes": size,
        "exists": exists,
        "content": content,
    }


def _resolve_claude_local_import(import_path: str, claude_local_dir: Path,
                                 mounts: list[dict]) -> Path | None:
    """Risolve un `@import` da CLAUDE.local.md ad un path host.

    L'import vive in /workspace/agent/CLAUDE.local.md lato container; il
    path `import_path` è relativo a quella posizione. Risultato canonico
    (POSIX) tipo /workspace/extra/<containerPath>/<rest> → mappato sul
    `hostPath` del mount che combacia col prefisso.
    """
    container_local_md = "/workspace/agent/CLAUDE.local.md"
    container_target = os.path.normpath(
        os.path.join(os.path.dirname(container_local_md), import_path)
    )
    # I mount addizionali vivono sotto /workspace/extra/<containerPath>
    extra_prefix = "/workspace/extra/"
    if not container_target.startswith(extra_prefix):
        # Per ora gestiamo solo gli @import che puntano dentro /workspace/extra/.
        return None
    inside_extra = container_target[len(extra_prefix):]
    for m in mounts:
        cp = (m.get("containerPath") or "").strip("/")
        if not cp:
            continue
        if inside_extra == cp or inside_extra.startswith(cp + "/"):
            rest = inside_extra[len(cp):].lstrip("/")
            host_root = os.path.expanduser(m.get("hostPath") or "")
            return Path(host_root) / rest if rest else Path(host_root)
    return None


def _loaded_context_for_jid(jid: str) -> list[dict]:
    """Per la chat WhatsApp <jid>, ritorna i file caricati nel prompt
    dell'agente NanoClaw (debug pane). Lista vuota se la chat non è
    wirata a nessun agent group o se il DB centrale non è raggiungibile."""
    if not _NANOCLAW_DB.exists():
        return []
    try:
        uri = f"file:{_NANOCLAW_DB}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=2.0) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT mg.id AS mg_id, mga.agent_group_id, ag.folder
                FROM messaging_groups mg
                JOIN messaging_group_agents mga ON mga.messaging_group_id = mg.id
                JOIN agent_groups          ag  ON ag.id  = mga.agent_group_id
                WHERE mg.channel_type = 'whatsapp' AND mg.platform_id = ?
                LIMIT 1
                """,
                (jid,),
            ).fetchone()
    except sqlite3.DatabaseError as e:
        app.logger.warning("_loaded_context_for_jid: DB error (%s)", e)
        return []
    if row is None:
        return []

    mg_id = row["mg_id"]
    group_folder = row["folder"]
    group_dir = _REPO_ROOT / "groups" / group_folder

    blocks: list[dict] = []

    # 1. Host base (target del symlink .claude-shared.md)
    blocks.append(_read_file_block(
        _REPO_ROOT / "container" / "CLAUDE.md",
        category="Host base",
        title="CLAUDE.md (host)",
        role="System prompt base, uguale per tutti gli agent NanoClaw",
        key="host/CLAUDE.md",
    ))

    # 2. Group entry (manifest @import)
    blocks.append(_read_file_block(
        group_dir / "CLAUDE.md",
        category="Group entry",
        title="CLAUDE.md (group)",
        role="Manifest @import composto a spawn-time",
        key="group/CLAUDE.md",
    ))

    # 3. Fragments NanoClaw (.claude-fragments/*.md, alfabetico)
    fragments_dir = group_dir / ".claude-fragments"
    if fragments_dir.is_dir():
        for frag in sorted(fragments_dir.glob("*.md")):
            stem = frag.stem
            role = _FRAGMENT_ROLES.get(stem, "Fragment NanoClaw")
            blocks.append(_read_file_block(
                frag,
                category="Fragments NanoClaw",
                title=frag.name,
                role=role,
                key=f"fragments/{stem}",
            ))

    # 4. Identità gruppo
    claude_local = group_dir / "CLAUDE.local.md"
    blocks.append(_read_file_block(
        claude_local,
        category="Identità gruppo",
        title="CLAUDE.local.md",
        role="Identità + istruzioni operative del gruppo",
        key="group/CLAUDE.local.md",
    ))

    # 5. @import risolti da CLAUDE.local.md (es. context-client.md)
    container_json = group_dir / "container.json"
    mounts: list[dict] = []
    if container_json.is_file():
        try:
            mounts = json.loads(container_json.read_text("utf-8")).get("additionalMounts") or []
        except (OSError, json.JSONDecodeError):
            mounts = []
    if claude_local.is_file() and mounts:
        try:
            local_text = claude_local.read_text("utf-8")
        except OSError:
            local_text = ""
        for m in re.finditer(r"(?m)^@(\S+)\s*$", local_text):
            import_path = m.group(1)
            host_path = _resolve_claude_local_import(import_path, claude_local.parent, mounts)
            if host_path is None or not host_path.is_file():
                continue
            blocks.append(_read_file_block(
                host_path,
                category="Contesto servizio",
                title=host_path.name,
                role="Editabile da /contesto/edit (no restart necessario)",
                key=f"import/{host_path.name}",
            ))

    # 6. Scheda cliente (per-conversazione)
    customer_md = group_dir / "customer-context" / f"{mg_id}.md"
    if customer_md.is_file():
        blocks.append(_read_file_block(
            customer_md,
            category="Scheda cliente",
            title=customer_md.name,
            role=f"Stato dinamico per messaging_group_id={mg_id}",
            key=f"customer-context/{mg_id}",
        ))

    # 7. Eventi cliente (on-demand letti dall'agent)
    events_dir = group_dir / "customer-events" / mg_id
    if events_dir.is_dir():
        for ev in sorted(events_dir.glob("*.md")):
            blocks.append(_read_file_block(
                ev,
                category="Eventi cliente",
                title=ev.name,
                role="Letto on-demand dall'agente quando rilevante",
                key=f"customer-events/{mg_id}/{ev.stem}",
            ))

    # 8. Incontri pianificati (cal.com via Google Calendar sync)
    meetings_md = group_dir / "customer-meetings" / f"{mg_id}.md"
    if meetings_md.is_file():
        blocks.append(_read_file_block(
            meetings_md,
            category="Incontri pianificati",
            title=meetings_md.name,
            role=f"Meeting cal.com (ultimi 14gg + futuri) per messaging_group_id={mg_id} — rigenerato a ogni messaggio in arrivo",
            key=f"customer-meetings/{mg_id}",
        ))

    return blocks


_MEETING_COLS = """
    uid, dtstart, dtend, status, created,
    cal_event_type, cal_booking_id, cal_reschedule_url,
    cal_prospect_name, cal_prospect_phone, cal_prospect_email,
    location,
    (linked_messaging_group_id_manual IS NOT NULL) AS is_manual_link
"""


def _meeting_row_to_dict(row, summaries_map: dict | None = None) -> dict:
    """Converte una row di gcal_events nel dict usato dalla timeline.

    Calcola `dtstart_time` (Europe/Rome), `is_future`, e — se `summaries_map`
    e' fornita — arricchisce con i campi Blue Dot: `summary_html`, `has_summary`,
    `has_transcript`, `bluedot_meeting_id`.
    """
    from zoneinfo import ZoneInfo
    d = dict(row)
    tz_rome = ZoneInfo("Europe/Rome")
    try:
        dt = datetime.fromisoformat(d["dtstart"]).astimezone(tz_rome)
        d["dtstart_time"] = _it_time(dt)
        d["is_future"] = dt > datetime.now(tz_rome)
    except (ValueError, TypeError):
        d["dtstart_time"] = d.get("dtstart", "")
        d["is_future"] = False
    # Arricchimento Blue Dot (opzionale)
    d["summary_html"] = None
    d["has_summary"] = False
    d["has_transcript"] = False
    d["bluedot_meeting_id"] = None
    if summaries_map is not None:
        loc_key = _norm_meet_url(d.get("location"))
        if loc_key and loc_key in summaries_map:
            bd = summaries_map[loc_key]
            d["summary_html"] = bd["summary_html"]
            d["has_summary"] = bool(bd["summary_html"])
            d["has_transcript"] = bd["has_transcript"]
            d["bluedot_meeting_id"] = bd["meeting_id"]
    return d


def _meetings_for_messaging_group_id(mg_id: str | None) -> list[dict]:
    """Eventi cal.com associati al messaging_group (link auto o manuale).

    Ritorna lista ordinata per data evento DESC. Vuota se mg_id e' None o
    nessun evento e' linkato. Ogni dict include `dtstart_time` (es.
    "12:45") gia' in fuso `Europe/Rome` per la UI, piu' i campi Blue Dot
    se disponibili (summary_html, has_summary, has_transcript, bluedot_meeting_id).
    """
    if not mg_id:
        return []
    with db() as conn:
        rows = conn.execute(
            f"""
            SELECT {_MEETING_COLS}
            FROM gcal_events
            WHERE COALESCE(linked_messaging_group_id_manual, linked_messaging_group_id_auto) = ?
            ORDER BY dtstart DESC
            """,
            (mg_id,),
        ).fetchall()
    summaries_map = _bluedot_summaries_by_meet_url()
    return [_meeting_row_to_dict(r, summaries_map) for r in rows]


def _meetings_card_markdown(mg_id: str) -> str | None:
    """Card markdown dei meeting cal.com (ultimi 14gg + futuri) per il bot.
    Ritorna None se nessun meeting nella finestra."""
    from zoneinfo import ZoneInfo
    meetings = _meetings_for_messaging_group_id(mg_id)
    if not meetings:
        return None
    tz_rome = ZoneInfo("Europe/Rome")
    cutoff = datetime.now(tz_rome) - timedelta(days=14)

    future: list[tuple] = []
    recent: list[tuple] = []
    for mt in meetings:
        try:
            dt = datetime.fromisoformat(mt["dtstart"]).astimezone(tz_rome)
        except (ValueError, TypeError):
            continue
        if mt.get("is_future"):
            future.append((dt, mt))
        elif dt >= cutoff:
            recent.append((dt, mt))

    if not future and not recent:
        return None

    def fmt(dt, mt, *, allow_reschedule):
        title = (mt.get("cal_event_type") or "Demo").strip()
        prospect = (mt.get("cal_prospect_name") or "").strip()
        is_cancelled = mt.get("status") == "CANCELLED"
        head = f"- **{_it_datetime(dt)}** — {title}"
        if prospect:
            head += f" (con {prospect})"
        head += "."
        sub = []
        if mt.get("created"):
            try:
                cdt = datetime.fromisoformat(mt["created"]).astimezone(tz_rome)
                sub.append(f"Prenotato il **{_it_datetime(cdt)}**.")
            except (ValueError, TypeError):
                pass
        if is_cancelled:
            sub.append("Stato: **annullato**.")
        if allow_reschedule and not is_cancelled and mt.get("cal_reschedule_url"):
            sub.append(f"[Riprogramma]({mt['cal_reschedule_url']})")
        if sub:
            head += "  \n  " + " ".join(sub)
        return head

    lines = [
        "# Incontri pianificati con questo contatto",
        "",
        "_Fonte: cal.com via Google Calendar sync. Finestra: ultimi 14 giorni + tutti i futuri. File rigenerato all'arrivo di ogni messaggio — sola lettura._",
        "",
    ]
    if future:
        lines += ["## Prossimi", ""]
        future.sort(key=lambda x: x[0])
        for dt, mt in future:
            lines.append(fmt(dt, mt, allow_reschedule=True))
        lines.append("")
    if recent:
        lines += ["## Recenti (ultimi 14 giorni)", ""]
        recent.sort(key=lambda x: x[0], reverse=True)
        for dt, mt in recent:
            lines.append(fmt(dt, mt, allow_reschedule=False))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _meetings_for_cliente(cliente: dict) -> list[dict]:
    """Meeting cal.com per un cliente, senza dipendere dal messaging_group WhatsApp.

    Match per email (normalizzata) e/o telefono (ultime 10 cifre), usando i
    normalizzatori del motore dedup. Arricchisce con i dati Blue Dot via
    `_meeting_row_to_dict`. Ritorna lista ordinata per dtstart DESC.
    """
    email_key = _tc_norm_email(cliente.get("email"))
    phone_key = _tc_norm_phone(cliente.get("phone"))
    if not email_key and not phone_key:
        return []

    seen_uid: set = set()
    merged: list = []

    with db() as conn:
        # Match per email (esatto, normalizzato — SQL sufficiente)
        if email_key:
            for r in conn.execute(
                f"SELECT {_MEETING_COLS} FROM gcal_events "
                "WHERE LOWER(TRIM(cal_prospect_email)) = ? ORDER BY dtstart DESC",
                (email_key,),
            ).fetchall():
                if r["uid"] not in seen_uid:
                    seen_uid.add(r["uid"])
                    merged.append(r)

        # Match per telefono (ultime 10 cifre, filtro in Python — SQLite non ha REGEXP_REPLACE)
        if phone_key:
            for r in conn.execute(
                f"SELECT {_MEETING_COLS} FROM gcal_events "
                "WHERE cal_prospect_phone IS NOT NULL AND cal_prospect_phone != '' "
                "ORDER BY dtstart DESC"
            ).fetchall():
                if _tc_norm_phone(r["cal_prospect_phone"]) == phone_key and r["uid"] not in seen_uid:
                    seen_uid.add(r["uid"])
                    merged.append(r)

    if not merged:
        return []

    # Ri-ordina globalmente per dtstart DESC (le due query erano separate)
    merged.sort(key=lambda r: r["dtstart"], reverse=True)
    summaries_map = _bluedot_summaries_by_meet_url()
    return [_meeting_row_to_dict(r, summaries_map) for r in merged]


def _agent_group_folders_for_mg_id(mg_id: str) -> list[str]:
    """Folder degli agent group wirati a questo messaging_group_id."""
    if not _NANOCLAW_DB.exists():
        return []
    try:
        uri = f"file:{_NANOCLAW_DB}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=2.0) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT DISTINCT ag.folder
                FROM messaging_group_agents mga
                JOIN agent_groups ag ON ag.id = mga.agent_group_id
                WHERE mga.messaging_group_id = ?
                """,
                (mg_id,),
            ).fetchall()
        return [r["folder"] for r in rows if r["folder"]]
    except sqlite3.DatabaseError as e:
        app.logger.warning("_agent_group_folders_for_mg_id: DB error (%s)", e)
        return []


def _write_meetings_card(mg_id: str) -> int:
    """Rigenera la card meeting per ogni agent group wirato al mg_id. Scrive
    atomicamente o rimuove il file se non ci sono meeting nella finestra.
    Ritorna il numero di file scritti/aggiornati."""
    folders = _agent_group_folders_for_mg_id(mg_id)
    if not folders:
        return 0
    md = _meetings_card_markdown(mg_id)
    touched = 0
    for folder in folders:
        target = _REPO_ROOT / "groups" / folder / "customer-meetings" / f"{mg_id}.md"
        if md is None:
            try:
                target.unlink(missing_ok=True)
            except OSError as e:
                app.logger.warning("_write_meetings_card: unlink failed (%s)", e)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".md.tmp")
        tmp.write_text(md, encoding="utf-8")
        os.replace(tmp, target)
        touched += 1
    return touched


@app.route("/whatsapp/<path:jid>")
def whatsapp_chat(jid):
    conversations = _whatsapp_conversations()
    selected = _whatsapp_messages(jid)
    if selected is None:
        return f"Conversazione {jid} non trovata", 404
    return render_template(
        "whatsapp.html",
        conversations=conversations,
        selected=selected,
        loaded_context=_loaded_context_for_jid(jid),
        embed=bool(request.args.get("embed")),
    )


@app.route("/whatsapp/<path:jid>/state")
def whatsapp_state(jid):
    """Polling endpoint leggero: ritorna una `version` che cambia quando
    arriva un nuovo messaggio o cambia lo stato di un'azione, così la UI
    può rilanciare il refresh del dettaglio chat.

    Riusa _whatsapp_messages così la nozione di 'ultimo messaggio' è identica
    a quella renderizzata (stessa dedup, stesso sort). Il costo è quello di
    un parse di pochi messaggi: accettabile per un poll a 1.5s."""
    selected = _whatsapp_messages(jid)
    if selected is None:
        return {"error": "not_found"}, 404
    msgs = selected["messages"]
    if not msgs:
        return {"version": "0"}
    last = msgs[-1]
    last_ts = last["ts"].isoformat()
    # Version chiave: timestamp ultimo evento + identità (per cogliere cambi
    # di stato di un'azione che mantengono il created_at).
    parts = [last_ts, last.get("kind", "msg"), str(last.get("action_id", ""))]
    parts.append(str(last.get("action_status", "")))
    parts.append("1" if last.get("delivered") else "0")
    parts.append(str(len(msgs)))
    return {"version": "|".join(parts)}


@app.route("/whatsapp/<path:jid>/send", methods=["POST"])
def whatsapp_send(jid):
    """Composer user-initiated: scrive un whatsapp_reply in pending_actions
    e lo porta subito a status='done' così il host NanoClaw lo invia via Baileys.

    Stesso pipeline di /actions/<id>/approve, applicato in un colpo solo
    (l'utente è già l'approvatore — non serve la fase pending).
    """
    text = (request.form.get("text") or "").strip()
    if not text:
        return {"error": "testo vuoto"}, 400
    if _whatsapp_messages(jid) is None:
        return {"error": "chat non trovata"}, 404

    ts_ms = int(time.time() * 1000)
    payload = {
        "platform_id": jid,
        "thread_id": None,
        "in_reply_to": None,
        "session_id": "dashboard",
        "msg_id": f"dashboard-{ts_ms}",
        "content_json": json.dumps({"text": text}, ensure_ascii=False),
        "files": None,
    }

    action_id = actions_db.enqueue("whatsapp_reply", payload)
    if actions_db.approve(action_id) is None:
        return {"error": "stato inatteso"}, 500
    try:
        result = action_executors.dispatch("whatsapp_reply", payload)
        actions_db.mark_done(action_id, result)
    except Exception as e:
        app.logger.exception("whatsapp_send: dispatch failed (action=%s)", action_id)
        actions_db.mark_failed(action_id, str(e))
        return {"error": str(e)}, 500

    return {"ok": True, "action_id": action_id}


@app.route("/contesto/edit", methods=["GET", "POST"])
def contesto_edit():
    audience = request.values.get("audience", "interno")
    if audience not in CONTEXT_PATHS_BY_AUDIENCE:
        audience = "interno"
    md_path, _overview_path, backup_path = CONTEXT_PATHS_BY_AUDIENCE[audience]

    if request.method == "POST":
        new_text = request.form.get("content", "")
        if not new_text.strip():
            return render_template(
                "contesto_edit.html",
                content=new_text,
                error="Il contenuto non può essere vuoto.",
                audience=audience,
                audiences=CONTEXT_AUDIENCES,
            ), 400
        # Backup the previous version (single rolling .bak — git holds real history).
        if md_path.exists():
            backup_path.write_bytes(md_path.read_bytes())
        # Normalize line endings and ensure trailing newline.
        normalized = new_text.replace("\r\n", "\n").rstrip() + "\n"
        md_path.write_text(normalized, encoding="utf-8")
        return redirect("/contesto")

    try:
        content = md_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        content = ""
    return render_template(
        "contesto_edit.html",
        content=content,
        error=None,
        audience=audience,
        audiences=CONTEXT_AUDIENCES,
    )


# ============================================================================
# Impostazioni applicative (tabella app_settings) — pagina /impostazioni.
# Oggi ospita i parametri della notifica "denoised" + triage AI degli scarti SDI
# (vedi scarti_notify.py). Letti freschi dal daemon a ogni tick e dal Flask al
# render del form: nessun lag da reload file, niente IPC (entrambi su local.db).
# ============================================================================
SCARTI_NOTIFY_DEFAULTS = {
    "enabled": True,
    "quiet_min": 30,      # finestra di silenzio: invia quando da N min non arriva un nuovo scarto
    "max_wait_min": 120,  # tetto: invia comunque dopo N min dal primo scarto del batch
    "lookback_days": 7,   # orizzonte oltre cui non si considerano scarti pendenti
    "model": "claude-opus-4-8",
    "extra_prompt": "",
    # Consegna: "agent" = fai elaborare il riassunto a un agente NanoClaw (Claude via
    # gateway, niente API key) che risponde nel chat target; "discord" = chiama l'API
    # Anthropic dal daemon e posta su Discord operations (richiede ANTHROPIC_API_KEY).
    "delivery": "agent",
    "target_ag": "",      # agent_group_id target (modalità agent)
    "target_mg": "",      # messaging_group_id target (modalità agent)
}
SCARTI_NOTIFY_MODELS = ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"]
SCARTI_NOTIFY_DELIVERY_MODES = ["agent", "discord"]


def get_setting(key, default=None):
    """Legge un valore da app_settings (stringa) o ritorna `default`."""
    try:
        with db() as conn:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row is not None else default
    except Exception:
        return default


def set_setting(key, value):
    """Upsert di un valore in app_settings (salvato come stringa)."""
    with db() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


def get_scarti_notify_settings():
    """Impostazioni tipizzate della notifica scarti SDI (con fallback ai default).
    Usata sia dalla pagina /impostazioni sia dal job scarti_notify nel daemon."""
    rows = {}
    try:
        with db() as conn:
            for r in conn.execute("SELECT key, value FROM app_settings"):
                rows[r["key"]] = r["value"]
    except Exception:
        pass

    def _int(key, d):
        try:
            return int(rows.get(key) if rows.get(key) not in (None, "") else d)
        except (TypeError, ValueError):
            return d

    enabled_raw = rows.get("scarti_notify_enabled")
    return {
        "enabled": (enabled_raw == "1") if enabled_raw is not None
                   else SCARTI_NOTIFY_DEFAULTS["enabled"],
        "quiet_min": _int("scarti_notify_quiet_min", SCARTI_NOTIFY_DEFAULTS["quiet_min"]),
        "max_wait_min": _int("scarti_notify_max_wait_min", SCARTI_NOTIFY_DEFAULTS["max_wait_min"]),
        "lookback_days": _int("scarti_notify_lookback_days", SCARTI_NOTIFY_DEFAULTS["lookback_days"]),
        "model": rows.get("scarti_notify_model") or SCARTI_NOTIFY_DEFAULTS["model"],
        "extra_prompt": rows.get("scarti_notify_extra_prompt") or SCARTI_NOTIFY_DEFAULTS["extra_prompt"],
        "delivery": rows.get("scarti_notify_delivery") or SCARTI_NOTIFY_DEFAULTS["delivery"],
        "target_ag": rows.get("scarti_notify_target_ag") or SCARTI_NOTIFY_DEFAULTS["target_ag"],
        "target_mg": rows.get("scarti_notify_target_mg") or SCARTI_NOTIFY_DEFAULTS["target_mg"],
    }


def _list_agent_chats():
    """Elenco dei chat (agent_group × messaging_group) wired in NanoClaw, per il
    dropdown 'destinazione' in /impostazioni. Read-only su data/v2.db; lista vuota
    se il DB non è leggibile."""
    conn = _ro_sqlite(_NANOCLAW_DB)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            """
            SELECT ag.id AS ag_id, ag.folder AS ag_folder,
                   mg.id AS mg_id, mg.channel_type AS channel_type,
                   mg.platform_id AS platform_id, mg.name AS name, mg.is_group AS is_group
            FROM messaging_group_agents mga
            JOIN agent_groups ag ON ag.id = mga.agent_group_id
            JOIN messaging_groups mg ON mg.id = mga.messaging_group_id
            ORDER BY ag.folder, mg.created_at DESC
            """
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    out = []
    for r in rows:
        ch = (r["channel_type"] or "?")
        kind = "gruppo" if r["is_group"] else "diretto"
        ident = r["name"] or r["platform_id"] or r["mg_id"]
        out.append({
            "ag_id": r["ag_id"],
            "mg_id": r["mg_id"],
            "label": f"{r['ag_folder']} · {ch} {kind} · {ident}",
        })
    return out


@app.route("/impostazioni", methods=["GET", "POST"])
def impostazioni():
    if request.method == "POST":
        f = request.form

        def _clamp(name, lo, hi, d):
            try:
                v = int(f.get(name, d))
            except (TypeError, ValueError):
                v = d
            return max(lo, min(hi, v))

        set_setting("scarti_notify_enabled", "1" if f.get("scarti_notify_enabled") else "0")
        set_setting("scarti_notify_quiet_min",
                    _clamp("scarti_notify_quiet_min", 0, 1440, SCARTI_NOTIFY_DEFAULTS["quiet_min"]))
        set_setting("scarti_notify_max_wait_min",
                    _clamp("scarti_notify_max_wait_min", 1, 10080, SCARTI_NOTIFY_DEFAULTS["max_wait_min"]))
        set_setting("scarti_notify_lookback_days",
                    _clamp("scarti_notify_lookback_days", 1, 90, SCARTI_NOTIFY_DEFAULTS["lookback_days"]))
        model = f.get("scarti_notify_model", "")
        if model in SCARTI_NOTIFY_MODELS:
            set_setting("scarti_notify_model", model)
        set_setting("scarti_notify_extra_prompt", (f.get("scarti_notify_extra_prompt") or "").strip())

        delivery = f.get("scarti_notify_delivery", "")
        if delivery in SCARTI_NOTIFY_DELIVERY_MODES:
            set_setting("scarti_notify_delivery", delivery)
        # Il target arriva come "ag_id|mg_id" dalla select; lo splittiamo.
        target = f.get("scarti_notify_target", "")
        if "|" in target:
            ag, mg = target.split("|", 1)
            set_setting("scarti_notify_target_ag", ag.strip())
            set_setting("scarti_notify_target_mg", mg.strip())
        return redirect("/impostazioni?saved=1")

    return render_template(
        "impostazioni.html",
        settings=get_scarti_notify_settings(),
        models=SCARTI_NOTIFY_MODELS,
        agent_chats=_list_agent_chats(),
        anthropic_key_present=bool(os.getenv("ANTHROPIC_API_KEY")),
        saved=request.args.get("saved") == "1",
    )


@app.route("/impostazioni/comunicazioni")
def impostazioni_comunicazioni():
    # Pagina esplicativa (sola lettura): mappa di tutti i canali con cui il
    # sistema comunica con l'esterno. Nessun dato dinamico — contenuto statico.
    return render_template("comunicazioni.html")


@app.route("/controllo-autofatture")
def controllo_autofatture():
    month = request.args.get("month", _competenza_default_month())
    customers, available_months, month, db_ready = _get_controllo_competenza(month)
    return render_template("controllo_autofatture.html",
        customers=customers,
        current_month=month,
        available_months=available_months,
        db_ready=db_ready,
        embed=True,
    )


# ============================================================================
# TEMP CLIENTI — MOCK (sola lettura). Aggrega gli eventi a calendario (cal.com
# via Google Calendar sync) in oggetti "cliente" PROPOSTI, deduplicando per
# identita' (email → telefono → nome) cosi' che piu' meeting dello stesso
# contatto collassino in una sola riga. Ogni proposta e' poi confrontata con le
# altre fonti (Stripe, Odoo, utenti Piattaforma, WhatsApp) per costruire una
# MATRICE DI DEDUP: per ciascuna fonte un indicatore ✓/✗ che dice se il cliente
# e' stato agganciato. Cosi' si vedono i buchi: un cliente solo su Stripe (✓
# Stripe, ✗ altrove) e' un dedup fallito o un dato isolato. Nessuna scrittura
# su DB: e' solo l'anteprima di cosa il sistema collegherebbe.
# ============================================================================

def _tc_norm_email(e):
    e = (e or "").strip().lower()
    return e or None


def _tc_norm_phone(p):
    """Chiave fuzzy: ultime 10 cifre (ignora prefisso/spazi/segni)."""
    if not p:
        return None
    digits = re.sub(r"\D", "", p)
    return digits[-10:] if len(digits) >= 6 else None


def _tc_norm_name(n):
    """Chiave nome: lowercase, accenti rimossi, spazi collassati. None se vuoto."""
    if not n:
        return None
    s = unicodedata.normalize("NFKD", n)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s or None


# ---------------------------------------------------------------------------
# Referral dei meeting demo (pagina /referrals)
# ---------------------------------------------------------------------------
# Chi prenota un meeting cal.com può inserire un codice referral nel campo
# "Come ci hai conosciuto?". Quel campo è però sovraccarico (contiene anche fonti
# generiche: "gruppo facebook", "lattanzio", "Gruppo oltre i cento - …"). I veri
# codici referral si riconoscono dal formato: token alfanumerico con almeno un
# trattino e senza spazi (es. ANDREA-ERER).
_REFERRAL_CODE_RE = re.compile(r"^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+$")


def _is_referral_code(answer):
    if not answer:
        return False
    code = answer.strip()
    if not (3 <= len(code) <= 40):
        return False
    return bool(_REFERRAL_CODE_RE.match(code))


def _referral_meetings():
    """Meeting cal.com con un codice referral nel campo "Come ci hai conosciuto?".

    Una riga per prospect (identità email→telefono→nome→uid) + codice, tenendo il
    meeting più recente. Risolve il cliente master collegato via cliente_links
    (source='meeting', ext_id='m:<key>'); se assente la riga resta "non
    ricondotta" (mostra solo il meeting). Parsing a render-time, come /calendar —
    nessuna colonna/sync dedicata.
    """
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Europe/Rome")
    except Exception:
        tz = None

    with db() as conn:
        rows = conn.execute(
            """
            SELECT uid, summary, description, dtstart,
                   cal_prospect_name, cal_prospect_email, cal_prospect_phone,
                   cal_booking_id, cal_reschedule_url
              FROM gcal_events
             WHERE description LIKE '%cal.com/booking%'
             ORDER BY dtstart DESC
            """
        ).fetchall()

        # Indice ext_id ('m:<key>') → cliente master, per il collegamento meeting.
        link_to_cli = {}
        for l in conn.execute(
            """
            SELECT cl.ext_id AS ext_id, c.id AS cliente_id, c.nome AS cliente_nome
              FROM cliente_links cl
              JOIN clienti c ON c.id = cl.cliente_id
             WHERE cl.source = 'meeting' AND COALESCE(c.archived, 0) = 0
            """
        ).fetchall():
            link_to_cli[l["ext_id"]] = {"id": l["cliente_id"], "nome": l["cliente_nome"]}

    out = {}
    for r in rows:
        answer = calcom_parser.extract_referral_field(r["description"])
        if not _is_referral_code(answer):
            continue
        code = answer.strip()

        em = _tc_norm_email(r["cal_prospect_email"])
        ph = _tc_norm_phone(r["cal_prospect_phone"])
        nk = _tc_norm_name(r["cal_prospect_name"])
        key = em or ph or nk or r["uid"]

        # Dedup per (prospect, codice): le righe sono già dtstart DESC, quindi la
        # prima inserita è la più recente.
        dedup_key = (key, code.lower())
        if dedup_key in out:
            continue

        dt = None
        try:
            dt = datetime.fromisoformat((r["dtstart"] or "").replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if tz:
                dt = dt.astimezone(tz)
        except (ValueError, TypeError):
            dt = None

        out[dedup_key] = {
            "dtstart": dt,
            "prospect_name": (r["cal_prospect_name"] or "").strip() or None,
            "email": r["cal_prospect_email"],
            "phone": r["cal_prospect_phone"],
            "summary": r["summary"],
            "codice": code,
            "cliente": link_to_cli.get(f"m:{key}"),
            "booking_url": r["cal_reschedule_url"]
            or (f"https://cal.com/booking/{r['cal_booking_id']}" if r["cal_booking_id"] else None),
        }

    _epoch = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(out.values(), key=lambda m: m["dtstart"] or _epoch, reverse=True)


@app.route("/referrals")
def referrals_page():
    meetings = _referral_meetings()
    return render_template("referrals.html", meetings=meetings)


@app.route("/clienti-fonti")
def clienti_fonti_page():
    clienti = get_clienti_unified()
    odoo_url = (os.getenv("ODOO_URL") or "").rstrip("/")
    active_tab = request.args.get("tab", "paganti")
    if active_tab not in ("paganti", "prospect", "tutti"):
        active_tab = "paganti"
    return render_template(
        "clienti.html",
        clienti=clienti,
        odoo_url=odoo_url,
        active_tab=active_tab,
    )


# ============================================================================
# DEDUP CLIENTI v2 — motore a livelli di confidenza.
#
# Il "cliente" è un CONTENITORE persistente (tabella `clienti`, anagrafica
# editabile) con N collegamenti per-ID alle fonti (`cliente_links`). Due livelli:
#
#   FORTE  → "cliente sicuro" materializzato in automatico (sync orario):
#            stripe_customer_id↔stripe_id, P.IVA, Codice Fiscale, email esatta.
#            Un cluster diventa cliente solo se contiene almeno una presenza
#            Stripe o Piattaforma (chi paga / è onboardato è un cliente vero;
#            meeting/WhatsApp da soli sono prospect).
#
#   DEBOLE → "proposta" calcolata live (telefono ultime-10-cifre, nome ≥2 token):
#            l'unico ponte verso WhatsApp. Non fonde nulla: propone di CREARE un
#            cliente o di COLLEGARE l'orfano a un cliente esistente.
#
# Perché il vecchio algoritmo di proposte (union-find debole, ora rimosso)
# bucava: fondeva solo email/telefono/nome e ignorava stripe_customer_id e P.IVA
# → con email diverse (es. SMC Salento di Bruno Valerio: smcsalento@ vs
# salentoslc@, stessa P.IVA) non agganciava.
# ============================================================================

def _dedup_gather():
    """Raccoglie da tutte le fonti gli 'entity' per il dedup. Ogni entity:
    {source, ext_id, ext_label, name, vat, cf, email, phone, strong}.
    `strong` = set di token per il match DETERMINISTICO (stripe:<id> condiviso
    tra stripe_id e stripe_customer_id, vat:, cf:, email:). WhatsApp non ha
    token forti (solo telefono → debole). Sola lettura su tutte le fonti."""
    ents: list[dict] = []

    with db() as conn:
        stripe_rows = conn.execute(
            "SELECT stripe_id, name, business_name, email, phone, vat FROM stripe_clienti"
        ).fetchall()
        odoo_cols = {r["name"] for r in conn.execute("PRAGMA table_info(odoo_clienti)").fetchall()}
        _stage_sel = "stage" if "stage" in odoo_cols else "NULL AS stage"
        odoo_rows = conn.execute(
            f"SELECT odoo_id, name, display_name, vat, {_stage_sel} FROM odoo_clienti"
        ).fetchall()
        odoo_opp_rows = conn.execute(
            "SELECT name, partner_name, partner_vat, stage_name, create_date "
            "FROM odoo_opportunita ORDER BY create_date"
        ).fetchall()
        ev_rows = conn.execute(
            """SELECT uid, cal_prospect_name, cal_prospect_email, cal_prospect_phone
                 FROM gcal_events
                WHERE cal_prospect_name IS NOT NULL OR cal_prospect_email IS NOT NULL
                   OR cal_prospect_phone IS NOT NULL"""
        ).fetchall()

    # Stripe
    for s in stripe_rows:
        vat = _normalize_vat(s["vat"])
        em = _tc_norm_email(s["email"])
        label = (s["business_name"] or s["name"] or s["email"] or "").strip()
        strong = {f"stripe:{s['stripe_id']}"}
        if vat:
            strong.add(f"vat:{vat}")
        if em:
            strong.add(f"email:{em}")
        ents.append({
            "source": "stripe", "ext_id": s["stripe_id"],
            "ext_label": label or s["stripe_id"],
            "name": (s["business_name"] or s["name"] or "").strip() or None,
            "vat": vat, "cf": None, "email": s["email"], "phone": s["phone"],
            "strong": strong,
        })

    # Odoo (partner anagrafici): VAT → token forte; niente email/telefono/CF.
    # Lo "stato" (stage CRM) arriva dalle opportunità, matchate per P.IVA o nome;
    # con ORDER BY create_date l'ultima opportunità vince (stato più recente).
    stage_by_vat: dict[str, str] = {}
    stage_by_name: dict[str, str] = {}
    for op in odoo_opp_rows:
        stg = (op["stage_name"] or "").strip()
        if not stg:
            continue
        v = _normalize_vat(op["partner_vat"])
        if v:
            stage_by_vat[v] = stg
        for nm in (op["partner_name"], op["name"]):
            nk = _tc_norm_name(nm)
            if nk:
                stage_by_name[nk] = stg
    for o in odoo_rows:
        nome = (o["name"] or o["display_name"] or "").strip()
        if nome in ("", "."):  # placeholder Odoo senza anagrafica utile
            nome = None
        vat = _normalize_vat(o["vat"])
        strong = set()
        if vat:
            strong.add(f"vat:{vat}")
        # Stato: prima la colonna stage (per partner_id, qualsiasi fase), poi
        # fallback al match per P.IVA/nome sulle opportunità del funnel.
        stato = (o["stage"] or "").strip() or None
        if not stato:
            stato = (vat and stage_by_vat.get(vat)) or stage_by_name.get(_tc_norm_name(nome))
        ents.append({
            "source": "odoo", "ext_id": str(o["odoo_id"]),
            "ext_label": nome or (o["display_name"] or "").strip() or str(o["odoo_id"]),
            "name": nome, "vat": vat, "cf": None, "email": None, "phone": None,
            "stato": stato or None,
            "strong": strong,
        })

    # Piattaforma (produzione.db, read-only)
    pconn = prod_db()
    if pconn is not None:
        try:
            def _cl(v):
                v = (v or "").strip()
                return v if v and v != "0" else None

            scid_by_uid: dict[int, str] = {}
            for r in pconn.execute(
                "SELECT user_id, stripe_customer_id FROM subscriptions_usersubscription"
            ).fetchall():
                sid = _cl(r["stripe_customer_id"])
                if sid and r["user_id"] not in scid_by_uid:
                    scid_by_uid[r["user_id"]] = sid
            for u in pconn.execute(
                """SELECT u.id, u.email, u.first_name, u.last_name,
                          p.piva, p.codice_fiscale, p.ragione_sociale
                     FROM auth_user u
                     LEFT JOIN users_profile p ON p.user_id = u.id"""
            ).fetchall():
                full = f"{_cl(u['first_name']) or ''} {_cl(u['last_name']) or ''}".strip()
                vat = _normalize_vat(_cl(u["piva"]))
                cf = (_cl(u["codice_fiscale"]) or "").upper() or None
                em = _tc_norm_email(u["email"])
                scid = scid_by_uid.get(u["id"])
                strong = set()
                if scid:
                    strong.add(f"stripe:{scid}")
                if vat:
                    strong.add(f"vat:{vat}")
                if cf:
                    strong.add(f"cf:{cf}")
                if em:
                    strong.add(f"email:{em}")
                label = _cl(u["ragione_sociale"]) or full or (u["email"] or "")
                ents.append({
                    "source": "piattaforma", "ext_id": str(u["id"]),
                    "ext_label": label,
                    "name": full or _cl(u["ragione_sociale"]),
                    "persona": full or None,
                    "vat": vat, "cf": cf, "email": u["email"], "phone": None,
                    "strong": strong,
                })
        finally:
            pconn.close()

    # Meeting (cal.com): aggregati per identità (email→telefono→nome→uid).
    cal: dict[str, dict] = {}
    for r in ev_rows:
        nome = (r["cal_prospect_name"] or "").strip()
        em = _tc_norm_email(r["cal_prospect_email"])
        ph = _tc_norm_phone(r["cal_prospect_phone"])
        nk = _tc_norm_name(nome)
        key = em or ph or nk or r["uid"]
        a = cal.get(key)
        if a is None:
            a = {
                "source": "meeting", "ext_id": f"m:{key}",
                "ext_label": nome or (r["cal_prospect_email"] or key),
                "name": nome or None, "vat": None, "cf": None,
                "email": r["cal_prospect_email"], "phone": r["cal_prospect_phone"],
                "strong": set(),
            }
            cal[key] = a
        if nome and not a["name"]:
            a["name"] = nome
        if not a["email"] and r["cal_prospect_email"]:
            a["email"] = r["cal_prospect_email"]
        if not a["phone"] and r["cal_prospect_phone"]:
            a["phone"] = r["cal_prospect_phone"]
    for a in cal.values():
        em = _tc_norm_email(a["email"])
        if em:
            a["strong"].add(f"email:{em}")
        ents.append(a)

    # WhatsApp (v2.db NanoClaw): solo telefono → nessun token forte.
    wa_conn = _ro_sqlite(NANOCLAW_DATA_DIR / "v2.db")
    if wa_conn is not None:
        try:
            for m in wa_conn.execute(
                "SELECT id, platform_id, name FROM messaging_groups "
                "WHERE channel_type='whatsapp' AND is_group=0"
            ).fetchall():
                jid = m["platform_id"] or ""
                tel = _phone_fmt(jid) if jid else None
                ents.append({
                    "source": "whatsapp", "ext_id": jid or f"mg:{m['id']}",
                    "ext_label": (m["name"] or "").strip() or tel or jid,
                    "name": (m["name"] or "").strip() or None,
                    "persona": (m["name"] or "").strip() or None,
                    "vat": None, "cf": None, "email": None, "phone": tel,
                    "strong": set(),
                })
        finally:
            wa_conn.close()

    return ents


def _dedup_strong_clusters(ents):
    """Connected-components sui SOLI token forti. Le entity senza token forte
    (WhatsApp, meeting senza email) restano singleton."""
    parent = list(range(len(ents)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    tok: dict[str, int] = {}
    for i, e in enumerate(ents):
        for t in e["strong"]:
            if t in tok:
                union(i, tok[t])
            else:
                tok[t] = i

    groups: dict[int, list[dict]] = {}
    for i in range(len(ents)):
        groups.setdefault(find(i), []).append(ents[i])
    return list(groups.values())


def _cluster_anagrafica(cluster):
    """Anagrafica migliore del contenitore, scegliendo per priorità di fonte."""
    def pick(field, order):
        for src in order:
            for e in cluster:
                if e["source"] == src and e.get(field):
                    return e[field]
        return None

    nome = pick("name", ["stripe", "piattaforma", "odoo", "meeting", "whatsapp"])
    if not nome:
        nome = pick("ext_label", ["stripe", "piattaforma", "odoo", "meeting", "whatsapp"])
    return {
        "nome": nome or "—",
        "vat": pick("vat", ["stripe", "piattaforma", "odoo"]),
        "codice_fiscale": pick("cf", ["piattaforma"]),
        "email": pick("email", ["piattaforma", "stripe", "meeting"]),
        "phone": pick("phone", ["meeting", "whatsapp", "stripe", "piattaforma"]),
    }


def materializza_clienti():
    """Crea/aggiorna i 'clienti sicuri' (cluster con presenza Stripe o
    Piattaforma) e i loro link. Idempotente e manual-safe: non sovrascrive
    l'anagrafica dei clienti editati a mano (auto=0) né i link manuali."""
    ents = _dedup_gather()
    clusters = _dedup_strong_clusters(ents)
    now = datetime.now().isoformat(timespec="seconds")
    current_exts = {(e["source"], e["ext_id"]) for e in ents}
    created = updated = 0

    with db() as conn:
        for cluster in clusters:
            if not any(e["source"] in ("stripe", "piattaforma") for e in cluster):
                continue  # prospect puro → resta nelle proposte, non è cliente sicuro
            # Cliente target: quello che già possiede più ext del cluster.
            owners: dict[int, int] = {}
            for e in cluster:
                row = conn.execute(
                    "SELECT cliente_id FROM cliente_links WHERE source=? AND ext_id=?",
                    (e["source"], e["ext_id"]),
                ).fetchone()
                if row:
                    owners[row["cliente_id"]] = owners.get(row["cliente_id"], 0) + 1
            if owners:
                cliente_id = max(owners, key=lambda k: (owners[k], -k))
            else:
                ana = _cluster_anagrafica(cluster)
                cur = conn.execute(
                    "INSERT INTO clienti(nome,vat,codice_fiscale,email,phone,auto,updated_at) "
                    "VALUES(?,?,?,?,?,1,?)",
                    (ana["nome"], ana["vat"], ana["codice_fiscale"], ana["email"],
                     ana["phone"], now),
                )
                cliente_id = cur.lastrowid
                created += 1
            # Refresh anagrafica SOLO se il cliente è ancora auto (mai editato).
            crow = conn.execute("SELECT auto FROM clienti WHERE id=?", (cliente_id,)).fetchone()
            if crow and crow["auto"] == 1 and owners:
                ana = _cluster_anagrafica(cluster)
                conn.execute(
                    "UPDATE clienti SET nome=?,vat=?,codice_fiscale=?,email=?,phone=?,updated_at=? "
                    "WHERE id=?",
                    (ana["nome"], ana["vat"], ana["codice_fiscale"], ana["email"],
                     ana["phone"], now, cliente_id),
                )
                updated += 1
            # Upsert dei link (manual=0). I link manuali non vengono spostati.
            for e in cluster:
                row = conn.execute(
                    "SELECT cliente_id, manual FROM cliente_links WHERE source=? AND ext_id=?",
                    (e["source"], e["ext_id"]),
                ).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO cliente_links(cliente_id,source,ext_id,ext_label,manual,created_at) "
                        "VALUES(?,?,?,?,0,?)",
                        (cliente_id, e["source"], e["ext_id"], e["ext_label"], now),
                    )
                elif row["manual"] == 0 and row["cliente_id"] != cliente_id:
                    conn.execute(
                        "UPDATE cliente_links SET cliente_id=?, ext_label=? WHERE source=? AND ext_id=?",
                        (cliente_id, e["ext_label"], e["source"], e["ext_id"]),
                    )

        # Prune: link auto il cui ext è sparito dalle fonti.
        for row in conn.execute(
            "SELECT source, ext_id FROM cliente_links WHERE manual=0"
        ).fetchall():
            if (row["source"], row["ext_id"]) not in current_exts:
                conn.execute(
                    "DELETE FROM cliente_links WHERE source=? AND ext_id=?",
                    (row["source"], row["ext_id"]),
                )
        # Elimina clienti auto rimasti senza alcun link e senza note manuali.
        conn.execute(
            "DELETE FROM clienti WHERE auto=1 AND (note IS NULL OR note='') "
            "AND id NOT IN (SELECT DISTINCT cliente_id FROM cliente_links)"
        )
        conn.commit()

    # Pipeline: backfilla i clienti nuovi e applica le regole di spostamento
    # automatico (event-based, forward-only) sui nuovi eventi (Stripe/piattaforma/demo).
    try:
        apply_pipeline_rules()
    except Exception:
        app.logger.exception("apply_pipeline_rules failed")

    return {"created": created, "updated": updated, "clusters": len(clusters)}


def _ent_detail(e):
    """Dati 'grezzi' di una entity di fonte, per il confronto e la ricerca."""
    return {
        "name": e.get("name"), "email": e.get("email"), "phone": e.get("phone"),
        "vat": e.get("vat"), "cf": e.get("cf"), "label": e.get("ext_label"),
        "stato": e.get("stato"), "persona": e.get("persona"),
    }


def _persona_cliente(det):
    """Nome della persona fisica dietro al cliente: prima il profilo Piattaforma
    (nome+cognome reale), poi il pushName WhatsApp. `det` = dict source->_ent_detail."""
    if not det:
        return None
    return ((det.get("piattaforma") or {}).get("persona")
            or (det.get("whatsapp") or {}).get("persona"))


def _persona_per_card(persona, nome):
    """Persona da mostrare in card/lista accanto alla ragione sociale: solo se
    esiste ed è diversa (case-insensitive) dal nome già mostrato, per non duplicare."""
    if persona and persona.strip().lower() != (nome or "").strip().lower():
        return persona
    return None


def _dedup_ent_index(ents=None):
    """Mappa (source, ext_id) → entity, per recuperare i dati per-fonte."""
    if ents is None:
        ents = _dedup_gather()
    return {(e["source"], e["ext_id"]): e for e in ents}


def _clienti_full_index(ent_by_ext=None):
    """Tutti i clienti con anagrafica + fonti collegate. Se `ent_by_ext` è dato,
    aggiunge `details` = {source: {name,email,phone,vat,cf,label}} coi dati grezzi
    di ogni fonte collegata (per il confronto a tabella e la ricerca estesa)."""
    with db() as conn:
        cli_rows = conn.execute(
            "SELECT id, nome, vat, codice_fiscale, email, phone FROM clienti"
        ).fetchall()
        link_rows = conn.execute(
            "SELECT cliente_id, source, ext_id FROM cliente_links"
        ).fetchall()
    links_by_cli: dict[int, list] = {}
    for l in link_rows:
        links_by_cli.setdefault(l["cliente_id"], []).append((l["source"], l["ext_id"]))
    out = []
    for c in cli_rows:
        d = dict(c)
        links = links_by_cli.get(c["id"], [])
        srcs = {s for s, _ in links}
        d["sources"] = [s for s in ("stripe", "piattaforma", "odoo", "meeting", "whatsapp") if s in srcs]
        if ent_by_ext is not None:
            det: dict[str, dict] = {}
            for (src, ext) in links:
                e = ent_by_ext.get((src, ext))
                if e and src not in det:
                    det[src] = _ent_detail(e)
            d["details"] = det
        out.append(d)
    return out


def _cliente_haystack(c):
    """Tutto il testo cercabile di un cliente: anagrafica + dati di ogni fonte
    collegata (così 'Marchetti' trova M-HOUSES SRL via il profilo Piattaforma)."""
    parts = [c.get("nome"), c.get("vat"), c.get("email"), c.get("phone"),
             c.get("codice_fiscale")]
    for det in (c.get("details") or {}).values():
        parts += [det.get("name"), det.get("email"), det.get("phone"),
                  det.get("vat"), det.get("cf")]
    return " ".join(str(p) for p in parts if p).lower()


def _clienti_proposte_live(ents=None):
    """Suggerimenti (non persistiti): per ogni ORFANO (record di fonte non ancora
    collegato a un cliente), produce i dati dell'orfano + l'elenco dei CANDIDATI
    cliente che combaciano debolmente (telefono ultime-10-cifre, nome ≥2 token),
    con i dati completi per il confronto a tabella nel modale. Se non c'è alcun
    candidato → sarà un "crea nuovo" (o match manuale via ricerca)."""
    if ents is None:
        ents = _dedup_gather()
    ent_by_ext = _dedup_ent_index(ents)
    with db() as conn:
        linked = {
            (r["source"], r["ext_id"])
            for r in conn.execute("SELECT source, ext_id FROM cliente_links").fetchall()
        }
    clienti = _clienti_full_index(ent_by_ext)

    # Indici deboli sui clienti: indicizziamo per telefono/nome SIA del cliente
    # SIA di ogni fonte collegata (così il meeting "Sara Marchetti" aggancia il
    # cliente "M-HOUSES SRL" via il nome del profilo Piattaforma).
    phone_idx: dict[str, list] = {}
    name_idx: dict[str, list] = {}
    for c in clienti:
        phones = {c.get("phone")}
        names = {c.get("nome")}
        for det in (c.get("details") or {}).values():
            phones.add(det.get("phone"))
            names.add(det.get("name"))
        seen_p, seen_n = set(), set()
        for ph in phones:
            pk = _tc_norm_phone(ph)
            if pk and pk not in seen_p:
                seen_p.add(pk)
                phone_idx.setdefault(pk, []).append(c)
        for nm in names:
            nk = _tc_norm_name(nm)
            if nk and len(nk.split()) >= 2 and nk not in seen_n:
                seen_n.add(nk)
                name_idx.setdefault(nk, []).append(c)

    def _ref(e):
        return {
            "source": e["source"], "ext_id": e["ext_id"],
            "ext_label": e["ext_label"], "phone": e.get("phone"),
            "email": e.get("email"), "name": e.get("name"),
        }

    orphans = [e for e in ents if (e["source"], e["ext_id"]) not in linked]

    # Raggruppa gli orfani tra loro per telefono/nome → un solo "elemento nuovo".
    parent = list(range(len(orphans)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    pk_map: dict[str, int] = {}
    nk_map: dict[str, int] = {}
    for i, e in enumerate(orphans):
        pk = _tc_norm_phone(e.get("phone"))
        nk = _tc_norm_name(e.get("name"))
        nk = nk if (nk and len(nk.split()) >= 2) else None
        for val, mp in ((pk, pk_map), (nk, nk_map)):
            if not val:
                continue
            if val in mp:
                union(i, mp[val])
            else:
                mp[val] = i

    grp: dict[int, list[dict]] = {}
    for i in range(len(orphans)):
        grp.setdefault(find(i), []).append(orphans[i])

    proposte = []
    for gi, members in enumerate(grp.values()):
        # Anagrafica dell'orfano (elemento nuovo).
        nome = None
        for src in ("meeting", "whatsapp", "stripe", "piattaforma", "odoo"):
            for e in members:
                if e["source"] == src and e.get("name"):
                    nome = e["name"]
                    break
            if nome:
                break
        phone = next((e.get("phone") for e in members if e.get("phone")), None)
        email = next((e.get("email") for e in members if e.get("email")), None)
        sources = sorted({e["source"] for e in members})

        # Candidati cliente: combaciano per telefono o nome con un membro.
        cand_map: dict[int, dict] = {}
        for e in members:
            pk = _tc_norm_phone(e.get("phone"))
            nk = _tc_norm_name(e.get("name"))
            nk = nk if (nk and len(nk.split()) >= 2) else None
            if pk and pk in phone_idx:
                for c in phone_idx[pk]:
                    cm = cand_map.setdefault(c["id"], {"cliente": c, "matches": []})
                    cm["matches"].append({"field": "telefono", "self": e.get("phone"), "target": c["phone"]})
            if nk and nk in name_idx:
                for c in name_idx[nk]:
                    cm = cand_map.setdefault(c["id"], {"cliente": c, "matches": []})
                    cm["matches"].append({"field": "nome", "self": e.get("name"), "target": c["nome"]})

        candidates = []
        for v in cand_map.values():
            c = v["cliente"]
            candidates.append({
                "id": c["id"], "nome": c["nome"], "vat": c["vat"],
                "codice_fiscale": c["codice_fiscale"], "email": c["email"],
                "phone": c["phone"], "sources": c["sources"],
                "details": c.get("details", {}),
                "matches": v["matches"],
            })

        # Dati per-fonte dell'orfano (primo record per ciascuna fonte).
        orphan_details: dict[str, dict] = {}
        for e in members:
            if e["source"] not in orphan_details:
                orphan_details[e["source"]] = _ent_detail(e)

        display_nome = nome or (candidates[0]["nome"] if candidates else None) or (members[0]["ext_label"])
        proposte.append({
            "id": gi,
            "nome": nome or members[0]["ext_label"],
            "display_nome": display_nome,
            "phone": phone, "email": email, "sources": sources,
            "details": orphan_details,
            "refs": [_ref(e) for e in members],
            "n_candidates": len(candidates),
            "candidates": candidates,
        })

    proposte.sort(key=lambda p: (
        0 if p["n_candidates"] else 1,
        -len(p["refs"]),
        (p["display_nome"] or "").lower(),
    ))
    return proposte


def _clienti_cerca(q, limit=20):
    """Ricerca clienti per nome / telefono / P.IVA / email / CF, includendo i
    dati di TUTTE le fonti collegate (così 'Marchetti' trova M-HOUSES SRL)."""
    q = (q or "").strip().lower()
    if not q:
        return []
    qphone = _tc_norm_phone(q)
    out = []
    for c in _clienti_full_index(_dedup_ent_index()):
        hay = _cliente_haystack(c)
        ok = q in hay
        if not ok and qphone:
            phones = [c.get("phone")] + [d.get("phone") for d in (c.get("details") or {}).values()]
            ok = any(qphone == _tc_norm_phone(p) for p in phones)
        if ok:
            out.append(c)
        if len(out) >= limit:
            break
    return out


_SRC_META = {
    "manuale": {"label": "Manuale", "icon": "fa-pen", "brand": False, "color": "amber"},
    "stripe": {"label": "Stripe", "icon": "fa-stripe-s", "brand": True, "color": "indigo"},
    "piattaforma": {"label": "Piattaforma", "icon": "fa-user-gear", "brand": False, "color": "emerald"},
    "meeting": {"label": "Meeting", "icon": "fa-calendar-check", "brand": False, "color": "sky"},
    "whatsapp": {"label": "WhatsApp", "icon": "fa-whatsapp", "brand": True, "color": "green"},
    "odoo": {"label": "Odoo", "icon": "fa-cube", "brand": False, "color": "violet"},
}


def _link_fields(src, link, det):
    """Restituisce la lista ordinata di {label, value} per un link da mostrare nella scheda dettaglio.

    `link` è il dict del link (chiavi: source, ext_id, ext_label, manual, stato, ecc.).
    `det` è l'output di _ent_detail(e) per la entity corrispondente (può essere {}).
    Restituisce solo le coppie con value non None e non stringa vuota."""

    def _f(label, value):
        v = (value or "").strip() if isinstance(value, str) else value
        return {"label": label, "value": v} if v else None

    ext_id = (link.get("ext_id") or "").strip()
    ext_label = (link.get("ext_label") or "").strip()
    det_name = (det.get("name") or det.get("label") or "").strip()
    det_persona = (det.get("persona") or "").strip()
    det_phone = (det.get("phone") or "").strip()
    det_email = (det.get("email") or "").strip()
    det_vat = (det.get("vat") or "").strip()
    det_cf = (det.get("cf") or "").strip()
    det_stato = (det.get("stato") or link.get("stato") or "").strip()

    field_map = {
        "manuale": [
            _f("Nome", link.get("nome")),
            _f("P.IVA", link.get("vat")),
            _f("Codice Fiscale", link.get("cf")),
            _f("Email", link.get("email")),
            _f("Telefono", link.get("phone")),
        ],
        "whatsapp": [
            _f("Identificativo", ext_id),
            _f("Numero", det_phone),
            _f("Nome", det_persona or det_name or ext_label),
        ],
        "meeting": [
            _f("Identificativo", ext_id),
            _f("Nome", det_name or ext_label),
            _f("Email", det_email),
            _f("Telefono", det_phone),
        ],
        "piattaforma": [
            _f("Identificativo", ext_id),
            _f("Nome", det_name or ext_label),
            _f("Persona", det_persona),
            _f("Email", det_email),
            _f("Telefono", det_phone),
            _f("P.IVA", det_vat),
            _f("Codice Fiscale", det_cf),
        ],
        "stripe": [
            _f("Stripe ID", ext_id),
            _f("Nome", det_name or ext_label),
            _f("Email", det_email),
            _f("P.IVA", det_vat),
        ],
        "odoo": [
            _f("Odoo ID", ext_id),
            _f("Nome", det_name or ext_label),
            _f("Stato CRM", det_stato),
            _f("P.IVA", det_vat),
            _f("Email", det_email),
        ],
    }
    return [f for f in field_map.get(src, [_f("Identificativo", ext_id)]) if f]


def _clienti_master_list(archived=0, ent_by_ext=None):
    """Lista clienti master + riepilogo fonti collegate (cliente_links).

    `archived=0` → clienti attivi (default); `archived=1` → clienti archiviati.
    Se `ent_by_ext` (indice (source,ext_id)->entity) è dato, ogni cliente porta
    `persona` = nome della persona fisica (Piattaforma → WhatsApp)."""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT c.id, c.nome, c.vat, c.email, c.phone, c.auto, c.archived,
                   c.odoo_cliente_id,
                   o.display_name AS odoo_display, o.odoo_id AS odoo_odoo_id
              FROM clienti c
              LEFT JOIN odoo_clienti o ON o.id = c.odoo_cliente_id
              WHERE c.archived = ?
              ORDER BY c.nome COLLATE NOCASE
            """,
            (archived,),
        ).fetchall()
        links = conn.execute(
            "SELECT cliente_id, source, ext_id FROM cliente_links"
        ).fetchall()
    by_cli: dict[int, set] = {}
    wa_jid_by_cli: dict[int, str] = {}
    det_by_cli: dict[int, dict] = {}
    for l in links:
        by_cli.setdefault(l["cliente_id"], set()).add(l["source"])
        if l["source"] == "whatsapp":
            wa_jid_by_cli.setdefault(l["cliente_id"], l["ext_id"])
        if ent_by_ext is not None:
            e = ent_by_ext.get((l["source"], l["ext_id"]))
            if e and l["source"] not in det_by_cli.get(l["cliente_id"], {}):
                det_by_cli.setdefault(l["cliente_id"], {})[l["source"]] = _ent_detail(e)
    out = []
    for r in rows:
        d = dict(r)
        srcs = by_cli.get(r["id"], set())
        d["sources"] = [s for s in ("stripe", "piattaforma", "odoo", "meeting", "whatsapp") if s in srcs]
        d["n_sources"] = len(d["sources"])
        d["avatar_url"] = _whatsapp_avatar_url(wa_jid_by_cli.get(r["id"]))
        d["persona"] = _persona_per_card(_persona_cliente(det_by_cli.get(r["id"])), d["nome"])
        out.append(d)
    return out


def _cliente_master_detail(cliente_id):
    with db() as conn:
        row = conn.execute(
            """
            SELECT c.id, c.nome, c.vat, c.codice_fiscale, c.email, c.phone, c.note,
                   c.auto, c.archived, c.updated_at, c.odoo_cliente_id,
                   c.pipeline_col, c.pipeline_auto,
                   o.display_name AS odoo_display, o.odoo_id AS odoo_odoo_id, o.vat AS odoo_vat
              FROM clienti c
              LEFT JOIN odoo_clienti o ON o.id = c.odoo_cliente_id
              WHERE c.id = ?
            """,
            (cliente_id,),
        ).fetchone()
        if row is None:
            return None
        link_rows = conn.execute(
            "SELECT source, ext_id, ext_label, manual, created_at "
            "FROM cliente_links WHERE cliente_id=? ORDER BY source",
            (cliente_id,),
        ).fetchall()
        note_rows = conn.execute(
            "SELECT id, testo, origine, created_at "
            "FROM cliente_note WHERE cliente_id=? ORDER BY created_at DESC, id DESC",
            (cliente_id,),
        ).fetchall()
        pipeline_log_rows = conn.execute(
            "SELECT from_col, to_col, auto, rule_key, reason, created_at "
            "FROM cliente_pipeline_log WHERE cliente_id=? ORDER BY created_at DESC, id DESC",
            (cliente_id,),
        ).fetchall()
    d = dict(row)
    d["links"] = [dict(l) for l in link_rows]
    d["note_entries"] = [dict(n) for n in note_rows]
    # Storico pipeline arricchito con i titoli/accent leggibili delle colonne.
    d["pipeline_log"] = [
        {
            **dict(p),
            "to_title": _PIPELINE_SHORT.get(p["to_col"], p["to_col"]),
            "to_accent": _PIPELINE_ACCENT_BY_KEY.get(p["to_col"], "slate"),
            "from_title": _PIPELINE_SHORT.get(p["from_col"]) if p["from_col"] else None,
        }
        for p in pipeline_log_rows
    ]
    d["pipeline_title"] = _PIPELINE_TITLE.get(d.get("pipeline_col"), d.get("pipeline_col"))
    d["pipeline_accent"] = _PIPELINE_ACCENT_BY_KEY.get(d.get("pipeline_col"), "slate")
    wa_jid = next((l["ext_id"] for l in d["links"] if l["source"] == "whatsapp"), None)
    d["avatar_url"] = _whatsapp_avatar_url(wa_jid)
    # Arricchisci tutti i link con i dati di dettaglio dalla entity originale
    # (nome, numero, email, P.IVA, ecc.) e costruisci la lista di campi per-fonte.
    # In parallelo accumula src_vals per l'anagrafica multi-fonte.
    ent_by_ext = _dedup_ent_index()
    _ANA_SRC_ORDER = ["manuale", "meeting", "whatsapp", "piattaforma", "stripe", "odoo"]
    src_vals: dict = {
        "manuale": {
            "name": d.get("nome"), "vat": d.get("vat"), "cf": d.get("codice_fiscale"),
            "email": d.get("email"), "phone": d.get("phone"),
        }
    }
    for l in d["links"]:
        e = ent_by_ext.get((l["source"], l["ext_id"]))
        det = _ent_detail(e) if e else {}
        if l["source"] == "odoo":
            l["stato"] = e.get("stato") if e else None
        l["fields"] = _link_fields(l["source"], l, det)
        # Primo valore non vuoto per-fonte (per l'anagrafica multi-fonte).
        cur = src_vals.setdefault(l["source"], {})
        for k in ("name", "vat", "cf", "email", "phone"):
            if not cur.get(k) and det.get(k):
                cur[k] = det[k]
    # Fonte sintetica "manuale": dati anagrafici inseriti a mano, separata dal
    # conteggio dei link reali (per non alterare cliente.links|length nel template).
    manual_link = {
        "source": "manuale",
        "manual": True,
        "nome": d.get("nome"),
        "vat": d.get("vat"),
        "cf": d.get("codice_fiscale"),
        "email": d.get("email"),
        "phone": d.get("phone"),
    }
    manual_link["fields"] = _link_fields("manuale", manual_link, {})
    d["manual_source"] = manual_link
    # Anagrafica multi-fonte: per ogni campo (Nome/P.IVA/CF/Email/Tel), raccoglie
    # i valori distinti da tutte le fonti con le icone fonte corrispondenti.
    _ANA_FIELDS = [
        ("Nome", "name"), ("P.IVA", "vat"), ("Codice Fiscale", "cf"),
        ("Email", "email"), ("Telefono", "phone"),
    ]
    anagrafica_multi = []
    for label, key in _ANA_FIELDS:
        by_value: dict = {}  # norm_key -> {"value":..., "sources":[...]}
        for src in _ANA_SRC_ORDER:
            v = (src_vals.get(src) or {}).get(key)
            if isinstance(v, str):
                v = v.strip()
            if not v:
                continue
            norm = _tc_norm_phone(str(v)) if key == "phone" else str(v).strip().lower()
            if norm in by_value:
                if src not in by_value[norm]["sources"]:
                    by_value[norm]["sources"].append(src)
            else:
                by_value[norm] = {"value": v, "sources": [src]}
        anagrafica_multi.append({"label": label, "vals": list(by_value.values())})
    d["anagrafica_multi"] = anagrafica_multi
    return d


# ---------------------------------------------------------------------------
# Pipeline di vendita (kanban). Vista read-only dei clienti master disposti
# nelle colonne del funnel commerciale. La "fase" è lo stage CRM di Odoo,
# risolto per ogni cliente via cliente_links (source='odoo') -> odoo_clienti.
# Gli stage grezzi di Odoo vengono consolidati in 6 colonne ordinate (vedi
# PIPELINE_COLS): è una scelta editoriale, non un mapping 1:1 con Odoo.
# ---------------------------------------------------------------------------

# Colonne del kanban, in ordine sinistra->destra (cima imbuto -> acquisito).
# `desc` è la spiegazione mostrata in cima a ogni colonna ("cosa ho messo qui
# e perché"). `accent` è la tinta semantica del bordo/header.
PIPELINE_COLS = [
    {
        "key": "lead", "title": "Lead — da qualificare", "short": "Lead", "icon": "fa-seedling", "accent": "slate",
        "desc": "Contatti entrati (piattaforma, WhatsApp, meeting o lead Odoo) ma prima di una "
                "demo, più gli stage Odoo \"Qualificato\"/\"Limbo\" e chi non ha ancora uno stato "
                "CRM. È la cima dell'imbuto: gente da agganciare e portare a una demo.",
    },
    {
        "key": "demo_schedulata", "title": "Demo schedulata", "short": "Demo", "icon": "fa-calendar-day", "accent": "sky",
        "desc": "Opportunità con una demo già fissata a calendario. Il prossimo passo concreto è "
                "presentare il prodotto.",
    },
    {
        "key": "demo_fatta", "title": "Demo fatta — follow-up", "short": "Follow-up", "icon": "fa-comments", "accent": "violet",
        "desc": "Ha già visto il prodotto. Unisco le due sotto-fasi Odoo (Follow-up e → Onboarding) "
                "perché sono lo stesso momento post-demo: è il punto caldo della trattativa, dove si "
                "decide.",
    },
    {
        "key": "onboarding", "title": "Onboarding", "short": "Onboarding", "icon": "fa-rocket", "accent": "amber",
        "desc": "Ha detto sì: onboarding schedulato o in corso, ma non ancora a regime pagante. Va "
                "seguito per non perderlo prima del primo pagamento.",
    },
    {
        "key": "cliente", "title": "Cliente — account attivo", "short": "Cliente", "icon": "fa-user-check", "accent": "teal",
        "desc": "Ha un account creato/collegato sulla piattaforma ma non ancora un abbonamento "
                "Stripe. È diventato un utente vero del prodotto: il passo successivo è la "
                "sottoscrizione.",
    },
    {
        "key": "paganti", "title": "Pagante", "short": "Paganti", "icon": "fa-circle-check", "accent": "emerald",
        "desc": "Acquisiti: hanno un abbonamento Stripe collegato (= sottoscritto, a prescindere "
                "dal singolo pagamento) oppure stage Odoo \"Cliente pagante\". Obiettivo raggiunto, "
                "da mantenere e far crescere.",
    },
    {
        "key": "persi", "title": "Persi / Annullati", "short": "Persi", "icon": "fa-circle-xmark", "accent": "red",
        "desc": "Meeting annullati o trattative perse. Tenuti fuori dal funnel attivo per non sporcare "
                "i conteggi, ma a vista per un eventuale recupero.",
    },
]

# Bordo/header per accent di colonna.
_PIPELINE_ACCENT = {
    "slate": "border-slate-600",
    "sky": "border-sky-700",
    "violet": "border-violet-700",
    "amber": "border-amber-700",
    "teal": "border-teal-700",
    "emerald": "border-emerald-700",
    "red": "border-red-700",
}

# Rank del funnel: serve alle regole automatiche per spostare SOLO in avanti
# (target più avanzato della posizione corrente). `persi` è laterale: non è mai
# target di una regola, quindi resta fuori dal rank forward.
_PIPELINE_RANK = {
    "lead": 0,
    "demo_schedulata": 1,
    "demo_fatta": 2,
    "onboarding": 3,
    "cliente": 4,
    "paganti": 5,
}

# Mappe derivate da PIPELINE_COLS, per modale regole e badge dello storico.
_PIPELINE_TITLE = {c["key"]: c["title"] for c in PIPELINE_COLS}
_PIPELINE_SHORT = {c["key"]: c["short"] for c in PIPELINE_COLS}
_PIPELINE_ACCENT_BY_KEY = {c["key"]: c["accent"] for c in PIPELINE_COLS}

# Regole di spostamento automatico — FONTE UNICA per il motore (apply_pipeline_rules)
# e per il modale "Visualizza regole" (così descrizione e comportamento non
# divergono mai). Semantica: una regola fa fuoco UNA volta per cliente, quando la
# sua condizione è osservata vera per la prima volta (vedi cliente_pipeline_rule_state),
# e sposta solo se il target è più avanti della posizione corrente ("solo avanti,
# mai indietro"). `cond` riceve il set di fonti collegate + lo stage Odoo risolto.
PIPELINE_RULES = [
    {
        "key": "stripe",
        "title": "Abbonamento Stripe",
        "when": "Quando compare un collegamento Stripe (abbonamento creato), a prescindere dal pagamento.",
        "target": "paganti",
        "reason": "Abbonamento Stripe collegato",
        "cond": lambda sources, stage: "stripe" in sources,
    },
    {
        "key": "piattaforma",
        "title": "Account piattaforma",
        "when": "Quando viene creato o collegato l'utente sulla piattaforma.",
        "target": "cliente",
        "reason": "Account piattaforma creato/collegato",
        "cond": lambda sources, stage: "piattaforma" in sources,
    },
    {
        "key": "demo_schedulata",
        "title": "Demo schedulata",
        "when": "Quando viene fissata una demo (stage Odoo \"Demo schedulata\"), mentre il cliente è ancora indietro nel funnel.",
        "target": "demo_schedulata",
        "reason": "Demo fissata",
        "cond": lambda sources, stage: (stage or "").strip() == "Demo schedulata",
    },
]

# Rank per scegliere lo stage "più avanzato" quando un cliente ha più link Odoo.
_PIPELINE_STAGE_RANK = {
    "[E] Qualificato": 1,
    "Limbo": 1,
    "Demo schedulata": 2,
    "[E] Demo fatta -> Follow-up": 3,
    "[E] Demo fatta -> Onboarding": 3,
    "Onboarding schedulato": 4,
    "Onboardato": 4,
    "Cliente pagante": 5,
}


def _pipeline_col_for(stage, sources):
    """Mappa (stage Odoo grezzo, set fonti) -> chiave colonna kanban.
    Ordine di valutazione: l'essere pagante (Stripe o stage 'Cliente pagante')
    vince su tutto; i persi prima del funnel attivo."""
    s = (stage or "").strip()
    if s == "Cliente pagante" or "stripe" in sources:
        return "paganti"
    if s in ("Meeting annullato", "Lost", "Perso"):
        return "persi"
    if s in ("Onboarding schedulato", "Onboardato"):
        return "onboarding"
    if s in ("[E] Demo fatta -> Follow-up", "[E] Demo fatta -> Onboarding"):
        return "demo_fatta"
    if s == "Demo schedulata":
        return "demo_schedulata"
    return "lead"


def _pipeline_resolve(conn):
    """Per ogni cliente attivo: (set fonti collegate, stage Odoo più avanzato).
    È la risoluzione condivisa da backfill e motore regole — stesso stage che
    `_pipeline_clienti` mostra sulla card (P.IVA→opportunità + link partner)."""
    rows = conn.execute("SELECT id, vat FROM clienti WHERE archived = 0").fetchall()
    links = conn.execute("SELECT cliente_id, source, ext_id FROM cliente_links").fetchall()
    stage_by_vat: dict[str, str] = {}
    for o in conn.execute(
        "SELECT partner_vat, stage_name FROM odoo_opportunita "
        "WHERE partner_vat IS NOT NULL AND stage_name IS NOT NULL"
    ).fetchall():
        v, st = o["partner_vat"], o["stage_name"]
        if _PIPELINE_STAGE_RANK.get(st, 0) >= _PIPELINE_STAGE_RANK.get(stage_by_vat.get(v), -1):
            stage_by_vat[v] = st
    odoo_cols = {r["name"] for r in conn.execute("PRAGMA table_info(odoo_clienti)").fetchall()}
    stage_by_odoo: dict[str, str] = {}
    if "stage" in odoo_cols:
        stage_by_odoo = {
            str(r["odoo_id"]): r["stage"]
            for r in conn.execute("SELECT odoo_id, stage FROM odoo_clienti").fetchall()
            if r["stage"]
        }
    src_by_cli: dict[int, set] = {}
    odoo_ext_by_cli: dict[int, list] = {}
    for l in links:
        src_by_cli.setdefault(l["cliente_id"], set()).add(l["source"])
        if l["source"] == "odoo":
            odoo_ext_by_cli.setdefault(l["cliente_id"], []).append(str(l["ext_id"]))
    out: dict[int, tuple] = {}
    for r in rows:
        srcs = src_by_cli.get(r["id"], set())
        candidates = []
        if r["vat"] and stage_by_vat.get(r["vat"]):
            candidates.append(stage_by_vat[r["vat"]])
        for ext in odoo_ext_by_cli.get(r["id"], []):
            if stage_by_odoo.get(ext):
                candidates.append(stage_by_odoo[ext])
        best_stage = max(candidates, key=lambda s: _PIPELINE_STAGE_RANK.get(s, 0)) if candidates else None
        out[r["id"]] = (srcs, best_stage)
    return out


def _backfill_pipeline_col(conn):
    """Assegna `pipeline_col` ai clienti che ne sono privi, calcolandolo dallo
    stato attuale (Odoo/Stripe) — "il posto dove sono adesso" — e scrivendo la
    riga 'backfill' nello storico. Idempotente: tocca solo le righe NULL/vuote,
    quindi non sovrascrive mai una posizione già stabilita."""
    pending = conn.execute(
        "SELECT id FROM clienti WHERE archived = 0 AND (pipeline_col IS NULL OR pipeline_col = '')"
    ).fetchall()
    if not pending:
        return 0
    resolved = _pipeline_resolve(conn)
    now = datetime.now().isoformat(timespec="seconds")
    n = 0
    for r in pending:
        cid = r["id"]
        srcs, stage = resolved.get(cid, (set(), None))
        col = _pipeline_col_for(stage, srcs)
        conn.execute("UPDATE clienti SET pipeline_col=?, pipeline_auto=1 WHERE id=?", (col, cid))
        conn.execute(
            "INSERT INTO cliente_pipeline_log(cliente_id, from_col, to_col, auto, rule_key, reason, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (cid, None, col, 1, "backfill", "Stato iniziale (backfill)", now),
        )
        n += 1
    return n


def apply_pipeline_rules():
    """Applica le regole di spostamento automatico (PIPELINE_RULES) ai clienti
    attivi. Event-based + forward-only: ogni regola fa fuoco UNA volta per cliente
    — quando la sua condizione è osservata vera per la prima volta, tracciata in
    `cliente_pipeline_rule_state` — e sposta solo se il target è più avanti nel
    funnel della posizione corrente. Così un nuovo evento (abbonamento, account,
    demo) avanza il cliente, ma uno spostamento manuale non viene ri-corretto in
    assenza di un nuovo evento. Ritorna il numero di spostamenti effettuati."""
    now = datetime.now().isoformat(timespec="seconds")
    moved = 0
    with db() as conn:
        # Garantisce pipeline_col anche per clienti appena materializzati.
        _backfill_pipeline_col(conn)
        resolved = _pipeline_resolve(conn)
        cur_cols = {
            r["id"]: r["pipeline_col"]
            for r in conn.execute("SELECT id, pipeline_col FROM clienti WHERE archived = 0").fetchall()
        }
        fired = {
            (r["cliente_id"], r["rule_key"])
            for r in conn.execute("SELECT cliente_id, rule_key FROM cliente_pipeline_rule_state").fetchall()
        }
        for cid, (srcs, stage) in resolved.items():
            cur = cur_cols.get(cid) or "lead"
            new_rules = [
                rule for rule in PIPELINE_RULES
                if (cid, rule["key"]) not in fired and rule["cond"](srcs, stage)
            ]
            if not new_rules:
                continue
            # Segna TUTTE le regole nuove come viste (anche se non spostano):
            # evita il re-firing sulla stessa condizione permanente.
            for rule in new_rules:
                conn.execute(
                    "INSERT OR IGNORE INTO cliente_pipeline_rule_state(cliente_id, rule_key, created_at) "
                    "VALUES(?,?,?)",
                    (cid, rule["key"], now),
                )
            # Sposta verso il target più avanzato tra le regole nuove, solo avanti.
            best = max(new_rules, key=lambda rl: _PIPELINE_RANK.get(rl["target"], -1))
            target = best["target"]
            if _PIPELINE_RANK.get(target, -1) > _PIPELINE_RANK.get(cur, -1):
                conn.execute(
                    "UPDATE clienti SET pipeline_col=?, pipeline_auto=1, updated_at=? WHERE id=?",
                    (target, now, cid),
                )
                conn.execute(
                    "INSERT INTO cliente_pipeline_log(cliente_id, from_col, to_col, auto, rule_key, reason, created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (cid, cur, target, 1, best["key"], best["reason"], now),
                )
                moved += 1
        conn.commit()
    return moved


def _pipeline_freshness(cands: list, today: date) -> dict | None:
    """Da una lista di datetime (ultimi contatti + eventi a calendario) ricava il
    badge "freschezza" mostrato in alto a destra sulla card.

    - se c'è almeno un evento FUTURO → il più vicino: label "+N giorni" (con check);
    - altrimenti il contatto/evento PASSATO più recente: label "N giorni"/"ieri"/"oggi";
    - nessun candidato → None (nessun badge; in coda all'ordinamento).

    `ref` è la data usata anche come chiave di ordinamento della colonna."""
    dates = [c.date() for c in cands if c is not None]
    if not dates:
        return None
    future = sorted(d for d in dates if d >= today)
    if future:
        ref = future[0]
        n = (ref - today).days
        label = "oggi" if n == 0 else ("+1 giorno" if n == 1 else f"+{n} giorni")
        return {"label": label, "future": True, "ref": ref}
    ref = max(dates)
    n = (today - ref).days
    label = "oggi" if n == 0 else ("ieri" if n == 1 else f"{n} giorni")
    return {"label": label, "future": False, "ref": ref}


def _pipeline_sorted_by_ref(items: list, *, desc: bool) -> list:
    """Ordina le card di una colonna per `_sort_ref`, tenendo SEMPRE in coda quelle
    senza data di riferimento (a prescindere dalla direzione)."""
    with_ref = [d for d in items if d.get("_sort_ref") is not None]
    without = [d for d in items if d.get("_sort_ref") is None]
    with_ref.sort(key=lambda d: d["_sort_ref"], reverse=desc)
    return with_ref + without


def _pipeline_clienti():
    """Clienti master attivi raggruppati nelle colonne della pipeline.
    Ritorna {col_key: [cliente, ...]}. Ogni cliente porta sources[] e lo stage
    Odoo risolto (il più avanzato tra le fonti disponibili).

    Lo stage viene da DUE fonti combinate (si tiene il più avanzato):
    - `odoo_opportunita.stage_name`, match per P.IVA (fonte primaria, affidabile);
    - `odoo_clienti.stage`, via i link Odoo del cliente (più ricca — copre Limbo,
      Meeting annullato, ecc. — ma la colonna può mancare a seconda dell'ultimo
      sync: lettura difensiva, così la pagina non va mai in 500)."""
    with db() as conn:
        rows = conn.execute(
            "SELECT id, nome, vat, email, phone, pipeline_col, pipeline_auto FROM clienti "
            "WHERE archived = 0 ORDER BY nome COLLATE NOCASE"
        ).fetchall()
        links = conn.execute(
            "SELECT cliente_id, source, ext_id FROM cliente_links"
        ).fetchall()
        # Ultima nota per cliente (anteprima sulla card).
        note_preview_rows = conn.execute(
            """
            SELECT cn.cliente_id, cn.testo
              FROM cliente_note cn
              INNER JOIN (
                SELECT cliente_id, MAX(created_at) AS max_at
                  FROM cliente_note GROUP BY cliente_id
              ) latest ON cn.cliente_id = latest.cliente_id AND cn.created_at = latest.max_at
            """
        ).fetchall()
        note_preview_by_cli = {r["cliente_id"]: r["testo"] for r in note_preview_rows}
        # Fonte primaria: stage_name dell'opportunità, per P.IVA (più avanzato).
        stage_by_vat: dict[str, str] = {}
        for o in conn.execute(
            "SELECT partner_vat, stage_name FROM odoo_opportunita "
            "WHERE partner_vat IS NOT NULL AND stage_name IS NOT NULL"
        ).fetchall():
            v, st = o["partner_vat"], o["stage_name"]
            if _PIPELINE_STAGE_RANK.get(st, 0) >= _PIPELINE_STAGE_RANK.get(stage_by_vat.get(v), -1):
                stage_by_vat[v] = st
        # Fonte secondaria: odoo_clienti.stage (colonna opzionale → difensiva).
        odoo_cols = {r["name"] for r in conn.execute("PRAGMA table_info(odoo_clienti)").fetchall()}
        stage_by_odoo: dict[str, str] = {}
        if "stage" in odoo_cols:
            stage_by_odoo = {
                str(r["odoo_id"]): r["stage"]
                for r in conn.execute("SELECT odoo_id, stage FROM odoo_clienti").fetchall()
                if r["stage"]
            }
        # Data creazione cliente su Stripe (unix) → ordina la colonna "paganti".
        stripe_created_by_id = {
            r["stripe_id"]: r["created"]
            for r in conn.execute("SELECT stripe_id, created FROM stripe_clienti").fetchall()
        }
        # Date eventi a calendario linkati a un messaging group (badge freshness).
        event_rows = conn.execute(
            "SELECT COALESCE(linked_messaging_group_id_manual, linked_messaging_group_id_auto) AS mgid, dtstart "
            "FROM gcal_events "
            "WHERE COALESCE(linked_messaging_group_id_manual, linked_messaging_group_id_auto) IS NOT NULL "
            "AND (status IS NULL OR upper(status) != 'CANCELLED')"
        ).fetchall()

    event_dts_by_mgid: dict[str, list] = {}
    for ev in event_rows:
        dt = _parse_ts(ev["dtstart"])
        if dt is not None:
            event_dts_by_mgid.setdefault(str(ev["mgid"]), []).append(dt)

    src_by_cli: dict[int, set] = {}
    odoo_ext_by_cli: dict[int, list] = {}
    wa_jid_by_cli: dict[int, str] = {}
    stripe_ext_by_cli: dict[int, str] = {}
    piatt_ext_by_cli: dict[int, str] = {}
    for l in links:
        src_by_cli.setdefault(l["cliente_id"], set()).add(l["source"])
        if l["source"] == "odoo":
            odoo_ext_by_cli.setdefault(l["cliente_id"], []).append(str(l["ext_id"]))
        elif l["source"] == "whatsapp":
            wa_jid_by_cli.setdefault(l["cliente_id"], l["ext_id"])
        elif l["source"] == "stripe":
            stripe_ext_by_cli.setdefault(l["cliente_id"], l["ext_id"])
        elif l["source"] == "piattaforma":
            piatt_ext_by_cli.setdefault(l["cliente_id"], str(l["ext_id"]))

    # Nome "umano" delle chat WhatsApp: spesso il cliente è stato materializzato
    # quando il pushName non era ancora arrivato, così `clienti.nome` è rimasto
    # il numero. Lo stesso senderName che la scheda mostra nel pannello chat è
    # ricostruito da `_whatsapp_conversations()`: lo riusiamo per dare un nome
    # vero alla card (teniamo solo i display_name che contengono lettere, cioè
    # non sono il numero di fallback).
    wa_name_by_jid: dict[str, str] = {}
    wa_last_ts_by_jid: dict[str, datetime] = {}
    if wa_jid_by_cli:
        for c in _whatsapp_conversations():
            if c["last_ts"]:
                wa_last_ts_by_jid[c["jid"]] = c["last_ts"]
            if any(ch.isalpha() for ch in (c["display_name"] or "")):
                wa_name_by_jid[c["jid"]] = c["display_name"]

    # jid → messaging_group id, per agganciare gli eventi a calendario alla card.
    mg_id_by_jid: dict[str, str] = {}
    if wa_jid_by_cli:
        central = _ro_sqlite(NANOCLAW_DATA_DIR / "v2.db")
        if central is not None:
            try:
                for m in central.execute(
                    "SELECT id, platform_id FROM messaging_groups WHERE channel_type='whatsapp'"
                ).fetchall():
                    mg_id_by_jid.setdefault(m["platform_id"], str(m["id"]))
            finally:
                central.close()

    # Data iscrizione account piattaforma (auth_user.date_joined) → ordina "cliente".
    platform_joined_by_id: dict[str, str] = {}
    pconn = prod_db()
    if pconn is not None:
        try:
            for r in pconn.execute("SELECT id, date_joined FROM auth_user").fetchall():
                platform_joined_by_id[str(r["id"])] = r["date_joined"]
        finally:
            pconn.close()

    today = datetime.now(_TZ_ROME).date() if _TZ_ROME else datetime.utcnow().date()

    # Stringa cercabile full-depth per ogni cliente (ricerca client-side nella
    # pagina): anagrafica + dati di TUTTE le fonti collegate, gli stessi mattoni
    # di /clienti/cerca. In coda le chiavi telefono normalizzate (ultime 10 cifre)
    # così il match per numero funziona anche senza prefisso/spazi.
    hay_by_id: dict[int, str] = {}
    persona_by_id: dict[int, str] = {}
    for c in _clienti_full_index(_dedup_ent_index()):
        hay = _cliente_haystack(c)
        phones = [c.get("phone")] + [d.get("phone") for d in (c.get("details") or {}).values()]
        digits = " ".join(k for k in (_tc_norm_phone(p) for p in phones) if k)
        hay_by_id[c["id"]] = (hay + " " + digits).strip()
        persona_by_id[c["id"]] = _persona_cliente(c.get("details"))

    cols = {c["key"]: [] for c in PIPELINE_COLS}
    for r in rows:
        d = dict(r)
        srcs = src_by_cli.get(r["id"], set())
        # Card WhatsApp: numero formattato in piccolo sotto il nome; se il nome
        # è di fatto il numero (nessuna lettera), promuovi il pushName a titolo.
        wa_jid = wa_jid_by_cli.get(r["id"])
        d["phone"] = _phone_fmt(wa_jid) if wa_jid else None
        d["avatar_url"] = _whatsapp_avatar_url(wa_jid) if wa_jid else None
        if wa_jid and not any(ch.isalpha() for ch in (d["nome"] or "")):
            d["nome"] = wa_name_by_jid.get(wa_jid) or d["nome"]
        # Nome della persona fisica (Piattaforma → WhatsApp), accanto alla ragione sociale.
        d["persona"] = _persona_per_card(persona_by_id.get(r["id"]), d["nome"])
        d["sources"] = [s for s in ("stripe", "piattaforma", "odoo", "meeting", "whatsapp") if s in srcs]
        # Candidati stage: P.IVA (opportunità) + link Odoo (partner). Vince il più avanzato.
        candidates = []
        if r["vat"] and stage_by_vat.get(r["vat"]):
            candidates.append(stage_by_vat[r["vat"]])
        for ext in odoo_ext_by_cli.get(r["id"], []):
            if stage_by_odoo.get(ext):
                candidates.append(stage_by_odoo[ext])
        best_stage = None
        if candidates:
            best_stage = max(candidates, key=lambda s: _PIPELINE_STAGE_RANK.get(s, 0))
        d["stage"] = best_stage
        d["pipeline_auto"] = r["pipeline_auto"]
        d["note_preview"] = note_preview_by_cli.get(r["id"])
        # Per la card: nome/numero promosso da WhatsApp non sono nell'haystack
        # master, aggiungili così la ricerca combacia con ciò che si vede.
        base_hay = hay_by_id.get(r["id"], "")
        extra = " ".join(str(p) for p in (d.get("nome"), d.get("phone")) if p).lower()
        d["search"] = (base_hay + " " + extra).strip()
        # Colonna POSSEDUTA dal cliente (pipeline_col); fallback al calcolo legacy
        # solo per righe non ancora backfillate (non dovrebbe capitare a regime).
        col = r["pipeline_col"] if r["pipeline_col"] in cols else _pipeline_col_for(best_stage, srcs)
        # Badge "freschezza" + chiave di ordinamento della colonna.
        if col == "paganti":
            d["freshness"] = None
            sid = stripe_ext_by_cli.get(r["id"])
            d["_sort_ref"] = stripe_created_by_id.get(sid) if sid else None
        elif col == "cliente":
            d["freshness"] = None
            uid = piatt_ext_by_cli.get(r["id"])
            d["_sort_ref"] = _parse_ts(platform_joined_by_id.get(uid)) if uid else None
        else:
            cands = []
            if wa_jid:
                ts = wa_last_ts_by_jid.get(wa_jid)
                if ts:
                    cands.append(ts)
                mgid = mg_id_by_jid.get(wa_jid)
                if mgid:
                    cands.extend(event_dts_by_mgid.get(mgid, []))
            fr = _pipeline_freshness(cands, today)
            d["freshness"] = {"label": fr["label"], "future": fr["future"]} if fr else None
            d["_sort_ref"] = fr["ref"] if fr else None
        cols[col].append(d)
    # Ordinamento: badge → meno recente in cima (asc); acquisiti → più recente in cima (desc).
    for key, items in cols.items():
        cols[key] = _pipeline_sorted_by_ref(items, desc=key in ("cliente", "paganti"))
    return cols


def _pipeline_counts():
    """Conteggio clienti master attivi per colonna pipeline, letto direttamente
    dalla proprietà posseduta `clienti.pipeline_col`. Ritorna {col_key: int}.
    Le righe non ancora backfillate (pipeline_col NULL) cadono in 'lead'."""
    counts = {c["key"]: 0 for c in PIPELINE_COLS}
    with db() as conn:
        rows = conn.execute(
            "SELECT pipeline_col, COUNT(*) AS n FROM clienti WHERE archived = 0 GROUP BY pipeline_col"
        ).fetchall()
    for r in rows:
        key = r["pipeline_col"] if r["pipeline_col"] in counts else "lead"
        counts[key] += r["n"]
    return counts


@app.route("/pipeline")
def pipeline_page():
    cols_data = _pipeline_clienti()
    total = sum(len(v) for v in cols_data.values())
    # Le regole per il modale "Visualizza regole": stessa lista del motore, senza
    # il callable `cond` (non serializzabile / non serve in template).
    rules_view = [
        {"key": r["key"], "title": r["title"], "when": r["when"],
         "target": r["target"], "target_title": _PIPELINE_TITLE.get(r["target"], r["target"])}
        for r in PIPELINE_RULES
    ]
    return render_template(
        "clienti_pipeline.html",
        pipeline_cols=PIPELINE_COLS,
        cols_data=cols_data,
        accent_cls=_PIPELINE_ACCENT,
        src_meta=_SRC_META,
        total=total,
        pipeline_rules=rules_view,
    )


@app.route("/pipeline-lista")
def pipeline_lista_page():
    """Vista "Lista" della pipeline: layout a due pannelli in stile /whatsapp —
    sidebar a sinistra coi clienti master RAGGRUPPATI per colonna del funnel (lo
    stesso ordine di PIPELINE_COLS della board kanban) e, a destra, la scheda del
    cliente selezionato (caricata via iframe, /clienti/<id>/scheda?embed=1). Riusa
    lo stesso _pipeline_clienti() della board: nessuna logica dati aggiuntiva."""
    cols_data = _pipeline_clienti()
    total = sum(len(v) for v in cols_data.values())
    return render_template(
        "clienti_pipeline_lista.html",
        pipeline_cols=PIPELINE_COLS,
        cols_data=cols_data,
        accent_cls=_PIPELINE_ACCENT,
        src_meta=_SRC_META,
        total=total,
    )


@app.route("/pipeline/move/<int:cliente_id>", methods=["POST"])
def pipeline_move(cliente_id):
    """Spostamento MANUALE (drag&drop) di un cliente in un'altra colonna.
    Aggiorna la proprietà posseduta `pipeline_col` (pipeline_auto=0) e registra
    la transizione nello storico (auto=0, rule_key='manual')."""
    to_col = (request.json or {}).get("to_col") if request.is_json else request.form.get("to_col")
    to_col = (to_col or "").strip()
    if to_col not in {c["key"] for c in PIPELINE_COLS}:
        return jsonify({"ok": False, "error": "colonna non valida"}), 400
    now = datetime.now().isoformat(timespec="seconds")
    with db() as conn:
        row = conn.execute(
            "SELECT pipeline_col FROM clienti WHERE id=? AND archived=0", (cliente_id,)
        ).fetchone()
        if row is None:
            return jsonify({"ok": False, "error": "cliente inesistente"}), 404
        cur = row["pipeline_col"]
        if cur == to_col:
            return jsonify({"ok": True, "unchanged": True, "counts": _pipeline_counts()})
        conn.execute(
            "UPDATE clienti SET pipeline_col=?, pipeline_auto=0, updated_at=? WHERE id=?",
            (to_col, now, cliente_id),
        )
        conn.execute(
            "INSERT INTO cliente_pipeline_log(cliente_id, from_col, to_col, auto, rule_key, reason, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (cliente_id, cur, to_col, 0, "manual", "Spostamento manuale", now),
        )
        conn.commit()
    return jsonify({"ok": True, "counts": _pipeline_counts()})


@app.route("/clienti")
def clienti_master_page():
    ents = _dedup_gather()
    ent_by_ext = _dedup_ent_index(ents)
    clienti = _clienti_master_list(ent_by_ext=ent_by_ext)
    clienti_archiviati = _clienti_master_list(archived=1, ent_by_ext=ent_by_ext)
    proposte = _clienti_proposte_live(ents)
    odoo_url = (os.getenv("ODOO_URL") or "").rstrip("/")
    return render_template(
        "clienti_master.html",
        clienti=clienti,
        clienti_archiviati=clienti_archiviati,
        proposte=proposte,
        selected=None,
        odoo_url=odoo_url,
        src_meta=_SRC_META,
    )


@app.route("/clienti/<int:cliente_id>")
def clienti_master_detail(cliente_id):
    selected = _cliente_master_detail(cliente_id)
    if selected is None:
        abort(404)
    ents = _dedup_gather()
    ent_by_ext = _dedup_ent_index(ents)
    clienti = _clienti_master_list(ent_by_ext=ent_by_ext)
    clienti_archiviati = _clienti_master_list(archived=1, ent_by_ext=ent_by_ext)
    proposte = _clienti_proposte_live(ents)
    odoo_url = (os.getenv("ODOO_URL") or "").rstrip("/")
    return render_template(
        "clienti_master.html",
        clienti=clienti,
        clienti_archiviati=clienti_archiviati,
        proposte=proposte,
        selected=selected,
        odoo_url=odoo_url,
        src_meta=_SRC_META,
    )


@app.route("/clienti/<int:cliente_id>/scheda")
def clienti_master_scheda(cliente_id):
    """Scheda focalizzata di un singolo cliente, pensata per il modale aperto
    dal click sulle card della pipeline: dati del cliente a sinistra e la
    cronologia (WhatsApp + meeting) a destra. Se il cliente non ha WhatsApp
    mostra comunque i meeting disponibili (via email/telefono). Riusa
    `_cliente_master_detail`, `_whatsapp_messages` e `_meetings_for_cliente`."""
    cliente = _cliente_master_detail(cliente_id)
    if cliente is None:
        abort(404)
    wa_jid = next((l["ext_id"] for l in cliente["links"] if l["source"] == "whatsapp"), None)
    if wa_jid:
        chat = _whatsapp_messages(wa_jid)  # include già i meeting arricchiti Blue Dot
    else:
        # Nessun WhatsApp: costruisce un chat minimale con i soli meeting
        meetings = _meetings_for_cliente(cliente)
        if meetings:
            from zoneinfo import ZoneInfo
            tz_rome = ZoneInfo("Europe/Rome")
            messages = []
            for mt in meetings:
                ts_mt = _parse_ts(mt["dtstart"])
                if ts_mt is None:
                    continue
                messages.append({"ts": ts_mt, "kind": "meeting", "meeting": mt})
            messages.sort(key=lambda m: m["ts"])
            chat = {
                "display_name": cliente["nome"] or "",
                "messages": messages,
                "has_whatsapp": False,
                "jid": None,
                "is_group": False,
                "phone_fmt": "",
                "avatar_url": None,
            }
        else:
            chat = None
    odoo_url = (os.getenv("ODOO_URL") or "").rstrip("/")
    return render_template(
        "clienti_scheda.html",
        cliente=cliente,
        chat=chat,
        wa_jid=wa_jid,
        src_meta=_SRC_META,
        odoo_url=odoo_url,
        embed=bool(request.args.get("embed")),
    )


@app.route("/clienti/cerca")
def clienti_cerca_route():
    """Ricerca clienti (JSON) per il match manuale dal modale dei suggerimenti."""
    return jsonify(_clienti_cerca(request.args.get("q", "")))


@app.route("/clienti/materializza", methods=["POST"])
def clienti_materializza_route():
    """Ricostruzione manuale on-demand dei clienti sicuri + link."""
    try:
        materializza_clienti()
    except Exception:
        app.logger.exception("materializza_clienti failed (manuale)")
    return redirect(request.referrer or "/clienti")


@app.route("/clienti/crea-da-proposta", methods=["POST"])
def clienti_crea_da_proposta():
    """Crea un nuovo cliente CONTENITORE da una proposta e collega i suoi
    riferimenti (manual=1). `refs` è un JSON [{source,ext_id,ext_label}, ...]."""
    nome = (request.form.get("nome") or "").strip() or "—"
    vat = _normalize_vat(request.form.get("vat"))
    cf = (request.form.get("codice_fiscale") or "").strip().upper() or None
    email = (request.form.get("email") or "").strip() or None
    phone = (request.form.get("phone") or "").strip() or None
    archived = 1 if request.form.get("archived") == "1" else 0
    try:
        refs = json.loads(request.form.get("refs") or "[]")
    except (ValueError, TypeError):
        refs = []
    now = datetime.now().isoformat(timespec="seconds")
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO clienti(nome,vat,codice_fiscale,email,phone,auto,archived,updated_at) "
            "VALUES(?,?,?,?,?,0,?,?)",
            (nome, vat, cf, email, phone, archived, now),
        )
        cliente_id = cur.lastrowid
        for ref in refs:
            if not ref.get("source") or not ref.get("ext_id"):
                continue
            conn.execute(
                "INSERT OR REPLACE INTO cliente_links"
                "(cliente_id,source,ext_id,ext_label,manual,created_at) VALUES(?,?,?,?,1,?)",
                (cliente_id, ref["source"], ref["ext_id"], ref.get("ext_label"), now),
            )
        conn.commit()
    if request.form.get("ajax"):
        return jsonify({"ok": True, "cliente_id": cliente_id})
    return redirect(f"/clienti/{cliente_id}")


@app.route("/clienti/<int:cliente_id>/collega", methods=["POST"])
def clienti_collega(cliente_id):
    """Collega un riferimento (o più, JSON `refs`) a un cliente esistente,
    come link manuale (manual=1, sposta da eventuale cliente precedente)."""
    with db() as conn:
        if conn.execute("SELECT 1 FROM clienti WHERE id=?", (cliente_id,)).fetchone() is None:
            abort(404)
        now = datetime.now().isoformat(timespec="seconds")
        refs_raw = request.form.get("refs")
        if refs_raw:
            try:
                refs = json.loads(refs_raw)
            except (ValueError, TypeError):
                refs = []
        else:
            refs = [{
                "source": request.form.get("source"),
                "ext_id": request.form.get("ext_id"),
                "ext_label": request.form.get("ext_label"),
            }]
        for ref in refs:
            if not ref.get("source") or not ref.get("ext_id"):
                continue
            conn.execute(
                "INSERT OR REPLACE INTO cliente_links"
                "(cliente_id,source,ext_id,ext_label,manual,created_at) VALUES(?,?,?,?,1,?)",
                (cliente_id, ref["source"], ref["ext_id"], ref.get("ext_label"), now),
            )
        conn.commit()
    if request.form.get("ajax"):
        return jsonify({"ok": True, "cliente_id": cliente_id})
    return redirect(f"/clienti/{cliente_id}")


@app.route("/clienti/<int:cliente_id>/scollega", methods=["POST"])
def clienti_scollega(cliente_id):
    source = request.form.get("source")
    ext_id = request.form.get("ext_id")
    with db() as conn:
        conn.execute(
            "DELETE FROM cliente_links WHERE cliente_id=? AND source=? AND ext_id=?",
            (cliente_id, source, ext_id),
        )
        conn.commit()
    return redirect(f"/clienti/{cliente_id}")


@app.route("/clienti/<int:cliente_id>/modifica", methods=["POST"])
def clienti_modifica(cliente_id):
    """Modifica l'anagrafica del contenitore. Mette auto=0 → la
    materializzazione automatica non sovrascriverà più questi campi."""
    nome = (request.form.get("nome") or "").strip()
    if not nome:
        return redirect(f"/clienti/{cliente_id}")
    vat = _normalize_vat(request.form.get("vat"))
    cf = (request.form.get("codice_fiscale") or "").strip().upper() or None
    email = (request.form.get("email") or "").strip() or None
    phone = (request.form.get("phone") or "").strip() or None
    now = datetime.now().isoformat(timespec="seconds")
    with db() as conn:
        conn.execute(
            "UPDATE clienti SET nome=?,vat=?,codice_fiscale=?,email=?,phone=?,"
            "auto=0,updated_at=? WHERE id=?",
            (nome, vat, cf, email, phone, now, cliente_id),
        )
        conn.commit()
    return redirect(f"/clienti/{cliente_id}")


@app.route("/clienti/<int:cliente_id>/note/aggiungi", methods=["POST"])
def clienti_nota_aggiungi(cliente_id):
    """Aggiunge una voce al log note del cliente."""
    testo = (request.form.get("testo") or "").strip()
    if not testo:
        return redirect(request.referrer or f"/clienti/{cliente_id}")
    now = datetime.now().isoformat(timespec="seconds")
    with db() as conn:
        if conn.execute("SELECT 1 FROM clienti WHERE id=?", (cliente_id,)).fetchone() is None:
            abort(404)
        conn.execute(
            "INSERT INTO cliente_note(cliente_id, testo, origine, created_at) VALUES(?,?,?,?)",
            (cliente_id, testo, "manuale", now),
        )
        conn.commit()
    return redirect(request.referrer or f"/clienti/{cliente_id}")


@app.route("/clienti/<int:cliente_id>/note/<int:nota_id>/elimina", methods=["POST"])
def clienti_nota_elimina(cliente_id, nota_id):
    """Elimina una voce del log note del cliente."""
    with db() as conn:
        conn.execute(
            "DELETE FROM cliente_note WHERE id=? AND cliente_id=?",
            (nota_id, cliente_id),
        )
        conn.commit()
    return redirect(request.referrer or f"/clienti/{cliente_id}")


@app.route("/odoo/partner/<int:odoo_id>/elimina-piva", methods=["POST"])
def odoo_elimina_piva(odoo_id):
    """Azzera la P.IVA del partner Odoo (res.partner.vat = False) e aggiorna
    la cache locale odoo_clienti."""
    clear_partner_vat(odoo_id)
    with db() as conn:
        conn.execute("UPDATE odoo_clienti SET vat = NULL WHERE odoo_id = ?", (odoo_id,))
        conn.commit()
    return redirect(request.referrer or "/clienti")


@app.route("/clienti/note/backfill-odoo", methods=["POST"])
def clienti_note_backfill_odoo():
    """Backfill una-tantum: copia il campo 'comment' (Note interne) dai partner Odoo
    nei clienti AFT che non hanno ancora alcuna nota nel log. Non sovrascrive nulla."""
    try:
        partners = get_all_partners(fields=["id", "comment"])
    except Exception:
        app.logger.exception("backfill-odoo: get_all_partners fallita")
        return redirect(request.referrer or "/clienti"), 302
    comment_by_odoo_id = {
        str(p["id"]): _html_to_text(p.get("comment") or "")
        for p in partners
    }
    now = datetime.now().isoformat(timespec="seconds")
    inserite = 0
    with db() as conn:
        # Clienti che NON hanno ancora nessuna voce in cliente_note.
        senza_note = {
            r["id"]
            for r in conn.execute(
                "SELECT c.id FROM clienti c "
                "WHERE NOT EXISTS (SELECT 1 FROM cliente_note n WHERE n.cliente_id = c.id)"
            ).fetchall()
        }
        if not senza_note:
            return redirect(request.referrer or "/clienti")
        # Recupera tutti i link Odoo (source='odoo', ext_id = odoo_id del partner).
        odoo_links = conn.execute(
            "SELECT cliente_id, ext_id FROM cliente_links WHERE source='odoo'"
        ).fetchall()
        # Anche via clienti.odoo_cliente_id → odoo_clienti.odoo_id.
        odoo_via_fk = conn.execute(
            "SELECT c.id AS cliente_id, o.odoo_id "
            "FROM clienti c JOIN odoo_clienti o ON o.id = c.odoo_cliente_id "
            "WHERE c.odoo_cliente_id IS NOT NULL"
        ).fetchall()
        # Costruiamo: cliente_id → set di odoo_id candidati
        cand: dict[int, set] = {}
        for l in odoo_links:
            cand.setdefault(l["cliente_id"], set()).add(str(l["ext_id"]))
        for l in odoo_via_fk:
            cand.setdefault(l["cliente_id"], set()).add(str(l["odoo_id"]))
        for cliente_id, odoo_ids in cand.items():
            if cliente_id not in senza_note:
                continue
            for oid in odoo_ids:
                testo = comment_by_odoo_id.get(oid, "")
                if testo:
                    conn.execute(
                        "INSERT INTO cliente_note(cliente_id, testo, origine, created_at) "
                        "VALUES(?,?,?,?)",
                        (cliente_id, testo, "odoo", now),
                    )
                    inserite += 1
                    break  # una sola voce per cliente
        conn.commit()
    app.logger.info("backfill-odoo note: %d voci inserite", inserite)
    return redirect(request.referrer or "/clienti")


@app.route("/clienti/<int:cliente_id>/archivia", methods=["POST"])
def clienti_archivia(cliente_id):
    """Mette il flag archived: il cliente sparisce dalle liste attive e dalla
    pipeline. Chiamata via fetch dal page-modal → risponde 204 (niente redirect)."""
    now = datetime.now().isoformat(timespec="seconds")
    with db() as conn:
        conn.execute(
            "UPDATE clienti SET archived=1, updated_at=? WHERE id=?", (now, cliente_id)
        )
        conn.commit()
    return ("", 204)


@app.route("/clienti/<int:cliente_id>/ripristina", methods=["POST"])
def clienti_ripristina(cliente_id):
    """Toglie il flag archived: il cliente torna nella lista attiva."""
    now = datetime.now().isoformat(timespec="seconds")
    with db() as conn:
        conn.execute(
            "UPDATE clienti SET archived=0, updated_at=? WHERE id=?", (now, cliente_id)
        )
        conn.commit()
    return redirect(f"/clienti/{cliente_id}")


@app.route("/clienti/<int:cliente_id>/elimina", methods=["POST"])
def clienti_elimina(cliente_id):
    """Elimina il contenitore (i link cadono per ON DELETE CASCADE). Le righe
    sorgente tornano orfane → ricompaiono come proposte."""
    with db() as conn:
        conn.execute("DELETE FROM clienti WHERE id=?", (cliente_id,))
        conn.commit()
    return redirect("/clienti")


@app.route("/clienti/senza-codice-destinatario")
def clienti_senza_codice_destinatario_page():
    clienti = get_stripe_senza_codice_destinatario()
    return render_template("clienti_senza_codice_destinatario.html", clienti=clienti)


# ============================================================================
# Prenotazioni clienti — pagine alimentate dalla copia del DB di PRODUZIONE
# (produzione.db), portate da `analisi` (/clienti + /clienti/<piva>). Lo stato
# SDI per prenotazione è integrato con le autofatture/risposte SDI già presenti
# in local.db (nessun DB SDI aggiuntivo). Vedi skill `ppp-import-produzione`.
# ============================================================================

def prod_db():
    """Connessione read-only a produzione.db. None se non ancora costruito."""
    if not os.path.exists(PROD_DB_PATH):
        return None
    conn = sqlite3.connect(f"file:{PROD_DB_PATH}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _controllo_platform_data(month):
    """Dati Piattaforma (produzione.db) per la vista Controllo autofatture.

    Legge produzione.db UNA volta e ritorna due mappe keyed per P.IVA normalizzata
    (_normalize_vat), entrambe vuote se produzione.db non è ancora stato importato:

      - user_id_by_piva: {piva_norm: user_id} → link /console/self-invoices/<id>.
        Se una P.IVA ha più utenti piattaforma, vince quello con più prenotazioni
        (a parità, user_id più alto = più recente).
      - comunicazioni_by_piva: {piva_norm: N} comunicazioni fiscali create nel `month`
        (SUBSTR(emission_date,1,7)), QUALSIASI kind (incluse le scartate NS).
        fiscal_communication → reservations_reservation → users_profile.
    """
    conn = prod_db()
    if conn is None:
        return {}, {}
    best = {}  # piva_norm -> (nres, user_id)
    comunicazioni_by_piva = {}
    with conn:
        for r in conn.execute("""
            SELECT p.piva piva, p.user_id user_id, COUNT(r.id) nres
            FROM users_profile p
            LEFT JOIN reservations_reservation r ON r.user_id = p.user_id
            WHERE p.piva IS NOT NULL AND TRIM(p.piva) != ''
            GROUP BY p.user_id
        """):
            pv = _normalize_vat(r["piva"])
            if not pv:
                continue
            cand = (r["nres"] or 0, r["user_id"])
            if pv not in best or cand > best[pv]:
                best[pv] = cand
        for r in conn.execute("""
            SELECT p.piva piva, COUNT(*) cnt
            FROM fiscal_communication fc
            JOIN reservations_reservation r ON CAST(r.id AS TEXT) = fc.reservation_id
            JOIN users_profile p ON p.user_id = r.user_id
            WHERE p.piva IS NOT NULL AND TRIM(p.piva) != ''
              AND SUBSTR(fc.emission_date, 1, 7) = :month
            GROUP BY p.piva
        """, {"month": month}):
            pv = _normalize_vat(r["piva"])
            if not pv:
                continue
            comunicazioni_by_piva[pv] = comunicazioni_by_piva.get(pv, 0) + (r["cnt"] or 0)
    user_id_by_piva = {pv: uid for pv, (_n, uid) in best.items()}
    return user_id_by_piva, comunicazioni_by_piva


_PROD_MESI_IT = ["", "Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
                 "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"]
# Codice di conferma Airbnb dentro l'XML dell'autofattura (stesso pattern di
# analisi/build_xml_index.py) → lega un'autofattura a una prenotazione.
_RE_CODICE_CONFERMA = re.compile(r"codice di conferma\s+([A-Z0-9]{8,})", re.I)
# Colori della mini-torta mensile (come analisi): ok=verde, invio=giallo, todo=rosso.
# Ordine delle fette in senso orario partendo dal centro in alto (ore 12):
# rosso (todo) → giallo (invio) → verde (ok). conic-gradient parte da ore 12 e
# gira in senso orario, quindi il primo colore è quello in alto a destra.
_PIE_COLORS = (("todo", "#f87171"), ("invio", "#fbbf24"), ("ok", "#34d399"))
# Fetta minima garantita per ogni categoria presente: 45° su 360° (= 12.5%).
# Senza questo minimo una categoria con pochi elementi diventa una fettina
# invisibile sul cerchietto da 14–18px; con max 3 categorie il minimo totale è
# 135°, restano 225° distribuiti in proporzione ai conteggi.
_PIE_MIN_FRAC = 45 / 360


def _pie_gradient(ok, invio, todo):
    """Stringa CSS conic-gradient per la mini-torta dello stato di gestione mensile.

    Ogni categoria presente (conteggio > 0) occupa almeno _PIE_MIN_FRAC del cerchio
    così da restare visibile anche con un solo elemento; la quota eccedente il minimo
    è proporzionale ai conteggi. Con una sola categoria presente → cerchio pieno."""
    counts = {"ok": ok, "invio": invio, "todo": todo}
    total = ok + invio + todo
    if total == 0:
        return "#1e293b"
    present = [(color, counts[key]) for key, color in _PIE_COLORS if counts[key]]
    remaining = 1.0 - len(present) * _PIE_MIN_FRAC  # quota proporzionale residua
    parts, acc = [], 0.0
    for color, n in present:
        start = acc * 100
        acc += _PIE_MIN_FRAC + remaining * (n / total)
        end = acc * 100
        parts.append(f"{color} {start:.2f}% {end:.2f}%")
    return "conic-gradient(" + ", ".join(parts) + ")"


def _all_autofatture_by_piva():
    """Mappa {piva_norm: {confirmation_code(UPPER): {af_id, numero, ricevuta}}}.

    Integra i dati SDI già presenti in local.db: scansiona UNA volta tutte le
    autofatture, estrae il codice di conferma dal contenuto XML e raggruppa per
    P.IVA normalizzata; marca `ricevuta` se il numero fattura ha una RC e porta
    `rc_date` (data della risposta positiva PEC). Usata sia dal dettaglio (una
    P.IVA) sia dalla lista clienti (torte aggregate) sia da _issued_da_allineare."""
    out = {}
    with db() as conn:
        # rc_numeri = tutti i numeri con almeno una RC (ricevuta di consegna).
        # rc_date_by_numero = data RC più ANTICA per numero (la "risposta positiva
        # dal PEC"), usata per la colonna data informativa GG-MM-AAAA. Restano
        # separati così un numero con RC ma senza email_date_iso conta comunque
        # come ricevuto (ricevuta=True) anche se privo di data.
        rc_numeri = set()
        rc_date_by_numero = {}
        for r in conn.execute(
            "SELECT numero_fattura, email_date_iso FROM risposte_SDI WHERE tipo='RC'"
        ):
            n = r["numero_fattura"]
            rc_numeri.add(n)
            iso = r["email_date_iso"]
            if iso and (n not in rc_date_by_numero or iso < rc_date_by_numero[n]):
                rc_date_by_numero[n] = iso
        rows = conn.execute(
            "SELECT id, filename, content, piva_cliente, email_date_iso FROM autofatture"
        ).fetchall()
    for r in rows:
        piva_norm = _normalize_vat(r["piva_cliente"])
        if not piva_norm:
            continue
        cc_list = _RE_CODICE_CONFERMA.findall(r["content"] or "")
        if not cc_list:
            continue
        filename = r["filename"] or ""
        numero = filename.replace(".xml", "").split("_")[-1] if filename else ""
        info = {
            "af_id": r["id"],
            "numero": numero,
            "ricevuta": numero in rc_numeri,
            # Data della risposta positiva PEC (RC); fallback alla data d'invio
            # dell'autofattura quando la RC non ha email_date_iso.
            "rc_date": rc_date_by_numero.get(numero) or r["email_date_iso"],
        }
        ccmap = out.setdefault(piva_norm, {})
        for cc in cc_list:
            ccmap.setdefault(cc.upper(), info)
    return out


def _autofatture_by_cc_for_piva(piva):
    """Mappa confirmation_code(UPPER) → {af_id, numero, ricevuta} per un cliente."""
    target = _normalize_vat(piva)
    if not target:
        return {}
    return _all_autofatture_by_piva().get(target, {})


def _bucket(af, status, cancel_type):
    """Classifica una prenotazione nello stato di gestione (come the_algorithm di
    analisi): RC ricevuta → ok; cancellata senza penale (WOP) e senza autofattura
    → ok (gestita, nulla da fare); autofattura presente senza RC → invio; altrimenti
    todo. Ritorna (bucket, ricevuta, gestita)."""
    ricevuta = bool(af and af["ricevuta"])
    gestita = status == "CA" and af is None and cancel_type != "WPE"
    if ricevuta or gestita:
        return "ok", ricevuta, gestita
    if af is not None:
        return "invio", ricevuta, gestita
    return "todo", ricevuta, gestita


def _clienti_con_pie():
    """Elenco clienti (produzione.db) con torta aggregata dello stato di gestione di
    TUTTE le loro prenotazioni. Una scansione autofatture + una query prenotazioni.

    Per ogni cliente calcola due set di conteggi: quello completo
    (`ok/invio/todo/nres/pie`) e quello che ESCLUDE il mese corrente
    (`ok_ex/invio_ex/todo_ex/nres_ex/pie_ex`), così che il filtro "Escludi <mese>"
    lato client alterni le due viste senza un secondo round-trip. Il mese di una
    prenotazione è `last_email_date[:7]` (coerente col raggruppamento mensile del
    dettaglio)."""
    conn = prod_db()
    if conn is None:
        return None
    cur_month = datetime.now().strftime("%Y-%m")
    by_piva = _all_autofatture_by_piva()
    with conn:
        clienti = conn.execute("""
            SELECT u.id uid,
                   COALESCE(NULLIF(TRIM(p.ragione_sociale), ''), u.username) name,
                   p.piva piva
            FROM auth_user u
            JOIN users_profile p ON p.user_id = u.id
            WHERE p.piva IS NOT NULL AND TRIM(p.piva) != ''
        """).fetchall()
        res = conn.execute("""
            SELECT p.piva piva, UPPER(COALESCE(r.confirmation_code, '')) cc,
                   r.status, r.cancel_type, r.last_email_date led
            FROM reservations_reservation r
            JOIN users_profile p ON p.user_id = r.user_id
            WHERE p.piva IS NOT NULL AND TRIM(p.piva) != ''
        """).fetchall()
    # Per ciascun cliente accumula due torte: tutto e "senza mese corrente".
    counts = {}
    for r in res:
        ccmap = by_piva.get(_normalize_vat(r["piva"]), {})
        bucket, _, _ = _bucket(ccmap.get(r["cc"]), r["status"], r["cancel_type"])
        c = counts.setdefault(r["piva"], {
            "ok": 0, "invio": 0, "todo": 0,
            "ok_ex": 0, "invio_ex": 0, "todo_ex": 0,
        })
        c[bucket] += 1
        led = r["led"] or ""
        if led[:7] != cur_month:
            c[bucket + "_ex"] += 1
    out = []
    for c in clienti:
        cnt = counts.get(c["piva"], {
            "ok": 0, "invio": 0, "todo": 0, "ok_ex": 0, "invio_ex": 0, "todo_ex": 0,
        })
        total = cnt["ok"] + cnt["invio"] + cnt["todo"]
        total_ex = cnt["ok_ex"] + cnt["invio_ex"] + cnt["todo_ex"]
        out.append({
            "name": c["name"], "piva": c["piva"], "nres": total,
            "ok": cnt["ok"], "invio": cnt["invio"], "todo": cnt["todo"],
            "pie": _pie_gradient(cnt["ok"], cnt["invio"], cnt["todo"]),
            "nres_ex": total_ex,
            "ok_ex": cnt["ok_ex"], "invio_ex": cnt["invio_ex"], "todo_ex": cnt["todo_ex"],
            "pie_ex": _pie_gradient(cnt["ok_ex"], cnt["invio_ex"], cnt["todo_ex"]),
        })
    # Ordina sulla vista di default (filtro ESCLUDI mese corrente attivo): prima per
    # numero di prenotazioni escluse il mese corrente, poi per nome → i clienti
    # svuotati dal filtro finiscono in fondo già dal render iniziale.
    out.sort(key=lambda c: (-c["nres_ex"], -c["nres"], c["name"].lower()))
    return out


def _competenza_default_month():
    """Mese di competenza su cui aprire di default Controllo Autofatture: il MESE
    PRECEDENTE a oggi. È quello con la scadenza d'invio già maturata/imminente (le
    autofatture di competenza M vanno inviate entro il 15 di M+1), quindi il mese
    azionabile. Il mese corrente sarebbe quasi tutto "mancante" (competenza non ancora
    scaduta) e nasconderebbe il segnale reale."""
    now = datetime.now()
    y, m = now.year, now.month - 1
    if m == 0:
        y, m = y - 1, 12
    return f"{y:04d}-{m:02d}"


def _get_controllo_competenza(month):
    """Controllo autofatture ancorato al MESE DI COMPETENZA (last_email_date della
    prenotazione), non al mese d'invio dell'autofattura.

    A differenza di _get_autofatture_by_month (che parte dalla tabella `autofatture`
    e misura solo lo stato SDI delle autofatture già inviate), qui si parte dalle
    PRENOTAZIONI della piattaforma (produzione.db) e per ognuna si verifica se esiste
    un'autofattura collegata (codice di conferma nell'XML, via _all_autofatture_by_piva).
    Così emergono anche i clienti senza alcuna autofattura in local.db — il caso che il
    vecchio componente non vedeva (es. prenotazioni di maggio non ancora fatturate).

    Bucket per cliente (riuso di _bucket):
      - confermate = ok    (RC ricevuta, o cancellata WOP senza autofattura → gestita)
      - in_attesa  = invio (autofattura presente ma senza RC)
      - mancanti   = todo  (nessuna autofattura collegata → da generare)

    Ritorna (customers, available_months, month, db_ready). Lista vuota +
    db_ready=False se produzione.db non è ancora stato importato.
    """
    conn = prod_db()
    if conn is None:
        return [], [], month, False

    by_piva = _all_autofatture_by_piva()

    with conn:
        available_months = [
            r["m"] for r in conn.execute(
                "SELECT DISTINCT SUBSTR(last_email_date, 1, 7) AS m "
                "FROM reservations_reservation "
                "WHERE last_email_date IS NOT NULL AND TRIM(last_email_date) != '' "
                "ORDER BY m DESC"
            ).fetchall()
        ]
        if month not in available_months and available_months:
            month = available_months[0]

        res = conn.execute("""
            SELECT p.piva piva, p.user_id user_id,
                   COALESCE(NULLIF(TRIM(p.ragione_sociale), ''), u.username) name,
                   UPPER(COALESCE(r.confirmation_code, '')) cc,
                   r.status, r.cancel_type
            FROM reservations_reservation r
            JOIN users_profile p ON p.user_id = r.user_id
            JOIN auth_user u ON u.id = p.user_id
            WHERE p.piva IS NOT NULL AND TRIM(p.piva) != ''
              AND SUBSTR(r.last_email_date, 1, 7) = :month
        """, {"month": month}).fetchall()

    # Nome "commerciale" + flag matched da Stripe/Odoo (come _get_autofatture_by_month).
    with db() as conn2:
        vat_to_customer = {}
        for r in conn2.execute("SELECT vat, business_name, name FROM stripe_clienti WHERE vat IS NOT NULL AND vat != ''"):
            v = _normalize_vat(r["vat"])
            if v:
                vat_to_customer[v] = r["business_name"] or r["name"] or v
        for r in conn2.execute("SELECT partner_vat, partner_name, name FROM odoo_opportunita WHERE partner_vat IS NOT NULL AND partner_vat != ''"):
            v = _normalize_vat(r["partner_vat"])
            if v and v not in vat_to_customer:
                vat_to_customer[v] = r["partner_name"] or r["name"] or v

    groups = {}
    for r in res:
        pv_norm = _normalize_vat(r["piva"])
        g = groups.get(pv_norm)
        if g is None:
            g = groups[pv_norm] = {
                "piva": r["piva"], "piva_norm": pv_norm,
                "prod_name": r["name"], "user_id": r["user_id"],
                "confermate": 0, "in_attesa": 0, "mancanti": 0,
            }
        bucket, _r, _g = _bucket(by_piva.get(pv_norm, {}).get(r["cc"]), r["status"], r["cancel_type"])
        if bucket == "ok":
            g["confermate"] += 1
        elif bucket == "invio":
            g["in_attesa"] += 1
        else:
            g["mancanti"] += 1

    customers = []
    for g in groups.values():
        matched = g["piva_norm"] in vat_to_customer
        customers.append({
            "piva": g["piva"],
            "customer_name": vat_to_customer.get(g["piva_norm"]) or g["prod_name"] or g["piva"] or "n/d",
            "matched": matched,
            "confermate": g["confermate"],
            "in_attesa": g["in_attesa"],
            "mancanti": g["mancanti"],
            "has_problems": g["mancanti"] > 0 or g["in_attesa"] > 0,
            "platform_user_id": g["user_id"],
        })

    customers.sort(key=lambda c: (
        0 if c["matched"] else 1,
        0 if (c["mancanti"] > 0 or c["in_attesa"] > 0) else 1,
        -c["mancanti"],
        -c["in_attesa"],
        c["customer_name"].lower(),
    ))

    return customers, available_months, month, True


def _iso_to_ggmmaaaa(iso):
    """Converte una data ISO (YYYY-MM-DD[...]) nel formato GG-MM-AAAA atteso dalla
    funzione bulk della piattaforma. Stringa vuota se non parsabile."""
    if not iso:
        return ""
    s = str(iso)[:10]
    try:
        y, m, d = s.split("-")
        return f"{int(d):02d}-{int(m):02d}-{int(y):04d}"
    except (ValueError, TypeError):
        return ""


def _issued_da_allineare():
    """Prenotazioni che la piattaforma tiene ancora in 'to emit' (status='TE') ma
    per cui è già arrivata la risposta positiva dal PEC (autofattura con RC) →
    andrebbero messe in 'Issued'.

    La funzione bulk della piattaforma (console staff "metti in Issued per data",
    `console/views/page_issue_reservations_by_date.py`) è GLOBALE e fa match per
    solo `confirmation_code` (cancellate escluse, già-issued saltate), quindi qui
    si produce un UNICO blocco di testo `CODICE;GG-MM-AAAA` (data = RC, solo
    informativa) pronto da incollare. Il match prenotazione↔autofattura è per
    confirmation_code globale (i codici Airbnb sono univoci) riusando la mappa di
    _all_autofatture_by_piva() appiattita.

    Ritorna dict {db_ready, count, items, text, by_customer}. db_ready=False se
    produzione.db non è ancora importato.
    """
    conn = prod_db()
    if conn is None:
        return {"db_ready": False, "count": 0, "items": [], "text": "", "by_customer": []}

    cc_info = {}
    for ccmap in _all_autofatture_by_piva().values():
        for cc, info in ccmap.items():
            cc_info.setdefault(cc, info)

    with conn:
        res = conn.execute("""
            SELECT r.confirmation_code cc_raw,
                   UPPER(COALESCE(r.confirmation_code, '')) cc,
                   r.guest_name guest, r.last_email_date led,
                   p.piva piva,
                   COALESCE(NULLIF(TRIM(p.ragione_sociale), ''), u.username) name
            FROM reservations_reservation r
            LEFT JOIN users_profile p ON p.user_id = r.user_id
            LEFT JOIN auth_user u ON u.id = r.user_id
            WHERE r.status = 'TE'
              AND r.confirmation_code IS NOT NULL
              AND TRIM(r.confirmation_code) != ''
            ORDER BY r.last_email_date DESC, r.id DESC
        """).fetchall()

    items = []
    for r in res:
        info = cc_info.get(r["cc"])
        if not info or not info.get("ricevuta"):
            continue
        items.append({
            "code": r["cc_raw"],
            "data": _iso_to_ggmmaaaa(info.get("rc_date")),
            "guest": r["guest"] or "",
            "customer_name": r["name"] or r["piva"] or "n/d",
            "piva": r["piva"] or "",
            "numero": info.get("numero", ""),
            "af_id": info.get("af_id"),
            "mese": (r["led"] or "")[:7],
        })

    # Blocco testo: solo righe con data valida (la piattaforma rifiuta date
    # malformate). I rari item senza data restano in elenco ma fuori dal blocco.
    text = "\n".join(f"{it['code']};{it['data']}" for it in items if it["data"])

    groups = OrderedDict()
    for it in items:
        key = it["piva"] or it["customer_name"]
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                "customer_name": it["customer_name"],
                "piva": it["piva"],
                "prenotazioni": [],
            }
        g["prenotazioni"].append(it)
    by_customer = sorted(
        groups.values(),
        key=lambda g: (-len(g["prenotazioni"]), g["customer_name"].lower()),
    )

    return {
        "db_ready": True,
        "count": len(items),
        "items": items,
        "text": text,
        "by_customer": by_customer,
    }


@app.route("/issued-da-allineare")
def issued_da_allineare_page():
    """Prenotazioni confermate via PEC ma ancora 'to emit' sulla piattaforma, col
    blocco CODICE;GG-MM-AAAA pronto da incollare nella funzione staff."""
    return render_template("issued_da_allineare.html", **_issued_da_allineare())


@app.route("/prenotazioni-clienti")
def prenotazioni_clienti_page():
    """Pagina unica a 3 colonne — nessun cliente selezionato (colonna centro vuota).
    La colonna sinistra (lista clienti + torte aggregate) è condivisa col dettaglio."""
    escludi_mese_label = _PROD_MESI_IT[datetime.now().month].lower()
    clienti = _clienti_con_pie()
    if clienti is None:
        return render_template("prenotazioni_clienti.html", clienti=[], db_ready=False,
                               selected_piva=None, month_groups=None, n_tot=0,
                               cliente_nome=None, cliente_piva=None,
                               escludi_mese_label=escludi_mese_label)
    return render_template("prenotazioni_clienti.html",
                           clienti=clienti, db_ready=True, selected_piva=None,
                           month_groups=None, n_tot=0,
                           cliente_nome=None, cliente_piva=None,
                           escludi_mese_label=escludi_mese_label)


@app.route("/prenotazioni-clienti/<piva>")
def prenotazioni_cliente_detail(piva):
    """Pagina unica a 3 colonne — cliente selezionato: colonna centro popolata coi mesi
    (produzione.db) e stato SDI integrato (local.db). Sinistra = lista clienti condivisa."""
    conn = prod_db()
    if conn is None:
        abort(404)
    with conn:
        prof = conn.execute(
            "SELECT COALESCE(NULLIF(TRIM(ragione_sociale), ''), '') rs "
            "FROM users_profile WHERE piva = ? LIMIT 1", (piva,)
        ).fetchone()
        if prof is None:
            abort(404)
        res = conn.execute("""
            SELECT r.id rid, UPPER(COALESCE(r.confirmation_code, '')) cc,
                   r.guest_name guest, r.status, r.cancel_type,
                   r.last_email_date led, r.booking_date bdate
            FROM reservations_reservation r
            JOIN users_profile p ON p.user_id = r.user_id
            WHERE p.piva = ?
            ORDER BY r.last_email_date DESC, r.id DESC
        """, (piva,)).fetchall()
    ccmap = _autofatture_by_cc_for_piva(piva)
    # Raggruppa per mese (preservando l'ordine DESC) con i conteggi per stato di
    # gestione — come la mini-torta mensile di analisi: ok (RC ricevuta, verde),
    # invio (inviata senza RC, giallo), todo (nessuna autofattura, rosso).
    # Le prenotazioni CANCELLATE seguono lo stesso trattamento di analisi
    # (the_algorithm, ramo "✅ non ci sono autofatture ed è cancellata, non
    # facciamo nulla"): una cancellazione senza penale (WOP) e senza autofattura
    # collegata non richiede alcun documento → è "gestita" → conta come ok (verde)
    # e va marcata "done" (opaca, esclusa da "Solo da gestire"). Una cancellazione
    # con penale (WPE) potrebbe richiedere lo storno → resta "todo" se priva di af.
    months = OrderedDict()
    for r in res:
        af = ccmap.get(r["cc"])
        led = r["led"] or ""
        month = led[:7] if len(led) >= 7 else ""
        try:
            month_label = (_PROD_MESI_IT[int(month[5:7])] + " " + month[:4]) if month else "Senza data"
        except (ValueError, IndexError):
            month_label = "Senza data"
        bucket, ricevuta, gestita = _bucket(af, r["status"], r["cancel_type"])
        g = months.setdefault(month, {
            "key": month or "none", "label": month_label,
            "ok": 0, "invio": 0, "todo": 0, "prenotazioni": [],
        })
        g[bucket] += 1
        g["prenotazioni"].append({
            "cc": r["cc"],
            "guest": r["guest"] or "",
            "status": r["status"] or "",
            "af_id": af["af_id"] if af else None,
            "numero": af["numero"] if af else "",
            "inviata": af is not None,
            "ricevuta": ricevuta,
            "gestita": gestita,
            "done": ricevuta or gestita,
        })
    month_groups = list(months.values())
    for g in month_groups:
        g["total"] = g["ok"] + g["invio"] + g["todo"]
        g["pie"] = _pie_gradient(g["ok"], g["invio"], g["todo"])
    n_tot = sum(g["total"] for g in month_groups)
    return render_template("prenotazioni_clienti.html",
        clienti=_clienti_con_pie() or [], db_ready=True, selected_piva=piva,
        cliente_nome=(prof["rs"] or piva), cliente_piva=piva,
        month_groups=month_groups, n_tot=n_tot,
        escludi_mese_label=_PROD_MESI_IT[datetime.now().month].lower())


@app.route("/clients")
def clients_index():
    clients = _clients_minimal_list()
    odoo_url = (os.getenv("ODOO_URL") or "").rstrip("/")
    return render_template("clients.html", clients=clients, selected=None, odoo_url=odoo_url, embed=True)


@app.route("/clients/<key>")
def clients_detail(key):
    if key.startswith("odoo-"):
        try:
            lead_id = int(key[len("odoo-"):])
        except ValueError:
            abort(404)
        detail = get_cliente_detail_odoo_lead(lead_id)
    else:
        detail = get_cliente_detail(key)
    if detail is None:
        abort(404)
    clients = _clients_minimal_list()
    odoo_url = (os.getenv("ODOO_URL") or "").rstrip("/")
    return render_template("clients.html", clients=clients, selected=detail, odoo_url=odoo_url, embed=True)


@app.route("/opportunita")
def opportunita_page():
    opportunita = get_opportunita()
    odoo_url = (os.getenv("ODOO_URL") or "").rstrip("/")
    return render_template("opportunita.html", opportunita=opportunita, odoo_url=odoo_url)


def _autofattura_urgency(month_str, today=None):
    """Urgenza per "nessuna autofattura inviata" nel mese `month_str` (YYYY-MM).

    Le autofatture vanno inviate entro il 15 del mese successivo a quello di
    competenza. La pagina raggruppa per mese di invio (= mese di scadenza), quindi
    il "giorno del mese" del mese mostrato indica quanto siamo vicini al deadline.
    """
    today = today or date.today()
    cur_month = today.strftime("%Y-%m")
    if month_str > cur_month:
        return {"level": "normal", "label": "Mese futuro"}
    if month_str < cur_month:
        return {"level": "late", "label": "In ritardo (mese passato)"}
    day = today.day
    if day <= 5:
        return {"level": "normal",  "label": "Normale (inizio mese)"}
    if day <= 10:
        return {"level": "watch",   "label": f"Da tenere d'occhio (giorno {day})"}
    if day <= 15:
        return {"level": "warning", "label": f"Molto preoccupante — deadline il 15 ({15 - day} gg)"}
    return {"level": "late", "label": f"In ritardo (giorno {day})"}


def _get_autofatture_by_month(month):
    """Ritorna (customers, all_autofatture, available_months, month_effettivo)."""
    STATO_ORDER = {"scartata": 0, "in_attesa": 1, "confermata": 2}

    with db() as conn:
        available_months = [
            r["m"] for r in conn.execute(
                "SELECT DISTINCT SUBSTR(email_date_iso, 1, 7) AS m FROM autofatture WHERE email_date_iso IS NOT NULL ORDER BY m DESC"
            ).fetchall()
        ]
        if month not in available_months and available_months:
            month = available_months[0]

        month_prefix = month + "-"
        rows = conn.execute("""
            WITH af AS (
                SELECT a.id, a.filename, a.email_date, a.email_date_iso, a.zip_id, z.zip_filename,
                       REPLACE(SUBSTR(a.filename, INSTR(a.filename, '_') + 1), '.xml', '') AS numero_fattura,
                       a.piva_cliente AS piva
                FROM autofatture a
                LEFT JOIN zip_inviati z ON a.zip_id = z.id
                WHERE a.email_date_iso LIKE :month_prefix || '%'
            ),
            rc AS (
                SELECT numero_fattura, COUNT(*) AS cnt FROM risposte_SDI WHERE tipo = 'RC' GROUP BY numero_fattura
            ),
            ns AS (
                SELECT numero_fattura, COUNT(*) AS cnt FROM risposte_SDI WHERE tipo = 'NS' GROUP BY numero_fattura
            )
            SELECT af.*, COALESCE(rc.cnt, 0) AS count_rc, COALESCE(ns.cnt, 0) AS count_ns
            FROM af
            LEFT JOIN rc ON rc.numero_fattura = af.numero_fattura
            LEFT JOIN ns ON ns.numero_fattura = af.numero_fattura
            ORDER BY af.email_date_iso DESC
        """, {"month_prefix": month_prefix}).fetchall()

        vat_to_customer = {}
        for r in conn.execute("SELECT vat, business_name, name FROM stripe_clienti WHERE vat IS NOT NULL AND vat != ''"):
            v = _normalize_vat(r["vat"])
            if v:
                vat_to_customer[v] = r["business_name"] or r["name"] or v
        for r in conn.execute("SELECT partner_vat, partner_name, name FROM odoo_opportunita WHERE partner_vat IS NOT NULL AND partner_vat != ''"):
            v = _normalize_vat(r["partner_vat"])
            if v and v not in vat_to_customer:
                vat_to_customer[v] = r["partner_name"] or r["name"] or v

    groups = defaultdict(lambda: {"confermate": 0, "scartate": 0, "in_attesa": 0, "autofatture": []})
    all_autofatture = []

    for r in rows:
        af = dict(r)
        count_rc = af.get("count_rc") or 0
        count_ns = af.get("count_ns") or 0
        if count_ns > 0 and count_rc == 0:
            af["stato"] = "scartata"
        elif count_rc == 0 and count_ns == 0:
            af["stato"] = "in_attesa"
        else:
            af["stato"] = "confermata"

        piva = af.get("piva", "")
        matched = piva in vat_to_customer
        af["customer_name"] = vat_to_customer.get(piva, piva or "n/d")
        af["matched"] = matched

        g = groups[piva]
        if af["stato"] == "confermata":
            g["confermate"] += 1
        elif af["stato"] == "scartata":
            g["scartate"] += 1
        else:
            g["in_attesa"] += 1
        g["autofatture"].append(af)

        if matched:
            all_autofatture.append(af)

    for piva in vat_to_customer:
        groups[piva]

    # Dati Piattaforma (produzione.db): link self-invoices + comunicazioni fiscali
    # del mese (per lo stato "blu"). Mappe vuote se produzione.db non è importato.
    platform_user_id_by_piva, comunicazioni_by_piva = _controllo_platform_data(month)

    customers = []
    for piva, g in groups.items():
        matched = piva in vat_to_customer
        customer_name = vat_to_customer.get(piva, piva or "n/d")
        piva_norm = _normalize_vat(piva)
        sorted_afs = []
        for _, grp in itertools_groupby(
            sorted(g["autofatture"], key=lambda a: STATO_ORDER.get(a["stato"], 2)),
            key=lambda a: a["stato"]
        ):
            chunk = sorted(list(grp), key=lambda a: a.get("email_date_iso") or "", reverse=True)
            sorted_afs.extend(chunk)
        g["autofatture"] = sorted_afs

        has_problems = (g["scartate"] + g["in_attesa"]) > 0
        no_autofatture = (
            g["confermate"] == 0 and g["scartate"] == 0 and g["in_attesa"] == 0
        )
        customers.append({
            "piva": piva,
            "customer_name": customer_name,
            "matched": matched,
            "confermate": g["confermate"],
            "scartate": g["scartate"],
            "in_attesa": g["in_attesa"],
            "has_problems": has_problems,
            "autofatture": g["autofatture"],
            "urgency": _autofattura_urgency(month) if no_autofatture else None,
            "comunicazioni_mese": comunicazioni_by_piva.get(piva_norm, 0),
            "platform_user_id": platform_user_id_by_piva.get(piva_norm),
        })

    def _priority(c):
        if c["scartate"] > 0 or c["in_attesa"] > 0:
            return 0
        if c["confermate"] == 0:
            return 1
        return 2

    customers.sort(key=lambda c: (
        0 if c["matched"] else 1,
        _priority(c),
        -c["scartate"],
        -c["in_attesa"],
        -c["confermate"],
        c["customer_name"].lower(),
    ))

    all_autofatture.sort(key=lambda a: a.get("email_date_iso") or "", reverse=True)

    return customers, all_autofatture, available_months, month


@app.route("/autofatture")
def autofatture_page():
    month = request.args.get("month", datetime.now().strftime("%Y-%m"))
    active_tab = request.args.get("tab", "per_cliente")
    customers, all_autofatture, available_months, month = _get_autofatture_by_month(month)

    return render_template("autofatture.html",
        customers=customers,
        all_autofatture=all_autofatture,
        current_month=month,
        available_months=available_months,
        active_tab=active_tab,
    )


@app.route("/autofattura/<int:att_id>")
def autofattura_detail(att_id):
    with db() as conn:
        row = conn.execute("SELECT * FROM autofatture WHERE id = ?", (att_id,)).fetchone()
        if not row:
            return "Autofattura non trovata", 404
        # Cerca le risposte SDI collegate per numero fattura
        att_dict = dict(row)
        filename = att_dict.get("filename", "")
        # Estrai numero fattura dal filename (es: IT02763130222_01732.xml -> 01732)
        parts = filename.replace(".xml", "").split("_")
        numero_fattura = parts[-1] if parts else ""
        risposte = []
        if numero_fattura:
            risposte = conn.execute(
                "SELECT * FROM risposte_SDI WHERE numero_fattura = ? ORDER BY email_date_iso DESC",
                (numero_fattura,),
            ).fetchall()
        att_dict["risposte"] = [dict(r) for r in risposte]
    return render_template("autofattura.html", att=att_dict)


@app.route("/risposte-sdi")
def risposte_sdi_page():
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM risposte_SDI ORDER BY email_date_iso IS NULL, email_date_iso DESC, id DESC LIMIT 100"
        ).fetchall()
    return render_template("risposte_sdi.html", risposte=[dict(r) for r in rows])


@app.route("/risposta-sdi/<int:risposta_id>")
def risposta_sdi_detail(risposta_id):
    with db() as conn:
        row = conn.execute("SELECT * FROM risposte_SDI WHERE id = ?", (risposta_id,)).fetchone()
    if not row:
        return "Risposta SDI non trovata", 404
    return render_template("risposta_sdi.html", risposta=dict(row))


# ── Scarti SDI aperti: autofatture (XML inviati) con notifica di scarto (NS) ma
# nessuna ricevuta di consegna (RC) → scartate e mai confermate. Cross-mese,
# vista focalizzata (la pagina /autofatture le mostra invece per mese/cliente). ─
def _get_scarti_aperti():
    """Lista delle autofatture scartate e mai confermate (≥1 NS, 0 RC).

    Ordinate per data dell'ultimo scarto (desc). Arricchite con il codice di
    conferma Airbnb estratto dall'XML (→ prenotazione) e, se produzione.db è
    importato, con i dati della prenotazione collegata (ospite, date, stato)."""
    with db() as conn:
        rows = conn.execute("""
            WITH af AS (
                SELECT a.id, a.filename, a.email_date_iso, a.zip_id, z.zip_filename,
                       REPLACE(SUBSTR(a.filename, INSTR(a.filename, '_') + 1), '.xml', '') AS numero_fattura,
                       a.piva_cliente AS piva
                FROM autofatture a
                LEFT JOIN zip_inviati z ON a.zip_id = z.id
            ),
            rc AS (SELECT numero_fattura, COUNT(*) AS cnt FROM risposte_SDI WHERE tipo = 'RC' GROUP BY numero_fattura),
            ns AS (SELECT numero_fattura, COUNT(*) AS cnt, MAX(email_date_iso) AS last_ns FROM risposte_SDI WHERE tipo = 'NS' GROUP BY numero_fattura)
            SELECT af.*, ns.cnt AS count_ns, ns.last_ns
            FROM af
            JOIN ns ON ns.numero_fattura = af.numero_fattura
            LEFT JOIN rc ON rc.numero_fattura = af.numero_fattura
            WHERE rc.cnt IS NULL
            ORDER BY COALESCE(ns.last_ns, af.email_date_iso) DESC
        """).fetchall()
        scarti = [dict(r) for r in rows]

        content_by_id = {}
        if scarti:
            ids = [s["id"] for s in scarti]
            qmarks = ",".join("?" * len(ids))
            content_by_id = {
                r["id"]: r["content"] for r in conn.execute(
                    f"SELECT id, content FROM autofatture WHERE id IN ({qmarks})", ids
                )
            }

        vat_to_customer = {}
        for r in conn.execute("SELECT vat, business_name, name FROM stripe_clienti WHERE vat IS NOT NULL AND vat != ''"):
            v = _normalize_vat(r["vat"])
            if v:
                vat_to_customer[v] = r["business_name"] or r["name"] or v
        for r in conn.execute("SELECT partner_vat, partner_name, name FROM odoo_opportunita WHERE partner_vat IS NOT NULL AND partner_vat != ''"):
            v = _normalize_vat(r["partner_vat"])
            if v and v not in vat_to_customer:
                vat_to_customer[v] = r["partner_name"] or r["name"] or v

    codici = set()
    for s in scarti:
        cc_list = _RE_CODICE_CONFERMA.findall(content_by_id.get(s["id"], "") or "")
        s["codice_conferma"] = cc_list[0].upper() if cc_list else None
        if s["codice_conferma"]:
            codici.add(s["codice_conferma"])
        piva_norm = _normalize_vat(s.get("piva"))
        s["customer_name"] = vat_to_customer.get(piva_norm, s.get("piva") or "n/d")
        s["matched"] = bool(piva_norm) and piva_norm in vat_to_customer

    # Arricchimento prenotazione (produzione.db, read-only): match per codice di
    # conferma. Mappa vuota se produzione.db non è importato.
    pren_by_cc = {}
    if codici:
        conn_p = prod_db()
        if conn_p is not None:
            try:
                qmarks = ",".join("?" * len(codici))
                for r in conn_p.execute(
                    "SELECT confirmation_code, guest_name, check_in, check_out, status "
                    f"FROM reservations_reservation WHERE confirmation_code IN ({qmarks})",
                    list(codici),
                ):
                    pren_by_cc[(r["confirmation_code"] or "").upper()] = dict(r)
            finally:
                conn_p.close()
    for s in scarti:
        s["prenotazione"] = pren_by_cc.get(s["codice_conferma"]) if s.get("codice_conferma") else None

    return scarti


# Errori dentro l'XML di scarto SDI (RicevutaScarto/NotificaScarto): ListaErrori →
# Errore → Codice + Descrizione (tag senza prefisso namespace). Estratti per la
# colonna "Descrizione" della tabella scarti.
_RE_ERRORE_BLOCK = re.compile(r"<Errore>(.*?)</Errore>", re.S | re.I)
_RE_ERRORE_CODICE = re.compile(r"<Codice>\s*(.*?)\s*</Codice>", re.S | re.I)
_RE_ERRORE_DESCR = re.compile(r"<Descrizione>\s*(.*?)\s*</Descrizione>", re.S | re.I)


def _errori_scarto(content):
    """Lista di {codice, descrizione} estratti dalla ListaErrori dell'XML di scarto.
    Vuota se il contenuto non è una notifica di scarto o non ha errori."""
    out = []
    if not content:
        return out
    for block in _RE_ERRORE_BLOCK.findall(content):
        dm = _RE_ERRORE_DESCR.search(block)
        if not dm:
            continue
        cm = _RE_ERRORE_CODICE.search(block)
        out.append({
            "codice": (cm.group(1).strip() if cm else ""),
            "descrizione": _html.unescape(dm.group(1).strip()),
        })
    return out


def _get_scarti_recenti(hours=24):
    """Notifiche di scarto (NS) ricevute nelle ultime `hours` ore, a prescindere
    dall'esito.

    Marca `risolto` quando per lo stesso numero fattura è poi arrivata una RC, così
    si vedono anche gli scarti già confermati (la tabella principale mostra solo gli
    aperti). Cliente risolto via autofattura collegata (per numero), quando presente;
    `errori` = problemi indicati nell'XML di scarto. Ordinate per cliente → numero
    fattura → data scarto.

    `email_date_iso` è il wall-clock del fuso del mittente (PEC italiane → ora di
    Roma = ora locale macchina), quindi confrontabile con `datetime.now()` formattato
    nello stesso modo — coerente con tutto il resto del codice su `email_date_iso`."""
    cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
    with db() as conn:
        rows = conn.execute("""
            SELECT r.id, r.numero_fattura, r.filename, r.email_date, r.email_date_iso, r.content,
                   EXISTS(SELECT 1 FROM risposte_SDI rc
                          WHERE rc.tipo = 'RC' AND rc.numero_fattura = r.numero_fattura) AS risolto
            FROM risposte_SDI r
            WHERE r.tipo = 'NS' AND r.email_date_iso >= :cutoff
            ORDER BY r.email_date_iso DESC, r.id DESC
        """, {"cutoff": cutoff}).fetchall()
        scarti = [dict(r) for r in rows]

        numeri = {s["numero_fattura"] for s in scarti if s["numero_fattura"]}
        af_by_numero = {}
        if numeri:
            qmarks = ",".join("?" * len(numeri))
            for r in conn.execute(
                f"""SELECT id, piva_cliente,
                           REPLACE(SUBSTR(filename, INSTR(filename, '_') + 1), '.xml', '') AS numero
                    FROM autofatture
                    WHERE REPLACE(SUBSTR(filename, INSTR(filename, '_') + 1), '.xml', '') IN ({qmarks})""",
                list(numeri),
            ):
                af_by_numero.setdefault(r["numero"], {"af_id": r["id"], "piva": r["piva_cliente"]})

        vat_to_customer = {}
        for r in conn.execute("SELECT vat, business_name, name FROM stripe_clienti WHERE vat IS NOT NULL AND vat != ''"):
            v = _normalize_vat(r["vat"])
            if v:
                vat_to_customer[v] = r["business_name"] or r["name"] or v
        for r in conn.execute("SELECT partner_vat, partner_name, name FROM odoo_opportunita WHERE partner_vat IS NOT NULL AND partner_vat != ''"):
            v = _normalize_vat(r["partner_vat"])
            if v and v not in vat_to_customer:
                vat_to_customer[v] = r["partner_name"] or r["name"] or v

    for s in scarti:
        af = af_by_numero.get(s["numero_fattura"])
        s["af_id"] = af["af_id"] if af else None
        piva = af["piva"] if af else None
        piva_norm = _normalize_vat(piva)
        s["piva"] = piva
        s["customer_name"] = (vat_to_customer.get(piva_norm) if piva_norm else None) or piva
        s["risolto"] = bool(s.get("risolto"))
        s["errori"] = _errori_scarto(s.pop("content", None))
        # Data scarto (giorno) per raggruppamento + display gg/mm/aaaa.
        day_iso = (s["email_date_iso"] or "")[:10]
        s["data_scarto_iso"] = day_iso
        s["data_scarto"] = (
            f"{day_iso[8:10]}/{day_iso[5:7]}/{day_iso[0:4]}"
            if len(day_iso) == 10 else (s["email_date"] or "—")
        )

    # Ordine base: cliente → data scarto → numero fattura (poi raggruppate per
    # tipologia di problema in _raggruppa_scarti_per_problema). Ignoti in fondo.
    scarti.sort(key=lambda s: (
        (s["customer_name"] or "￿").lower(),
        s["data_scarto_iso"],
        s["numero_fattura"] or "",
    ))
    return scarti


def _raggruppa_scarti_per_cliente_problema(scarti):
    """Raggruppa gli scarti per (cliente, tipologia di problema) — codice errore SDI.

    Ritorna gruppi `{customer_name, codice, descrizione_esempio, autofatture, count,
    n_aperti}`: tutte le autofatture dello stesso cliente scartate per lo stesso
    motivo collassano in un unico gruppo (la UI ne mostra le prime 5 + «altre N»).
    Ordinati per cliente, poi frequenza desc. Scarti senza errore estratto → gruppo
    `—`. (chiave `autofatture`, non `items`, per non collidere con `dict.items` in
    Jinja)."""
    gruppi = {}
    for s in scarti:
        err = (s.get("errori") or [{}])[0]
        codice = err.get("codice") or ""
        cliente = s.get("customer_name") or "n/d"
        key = (cliente, codice or "—")
        g = gruppi.get(key)
        if g is None:
            g = gruppi[key] = {
                "customer_name": cliente,
                "codice": codice,
                "descrizione_esempio": err.get("descrizione") or "",
                "autofatture": [],
            }
        if not g["descrizione_esempio"] and err.get("descrizione"):
            g["descrizione_esempio"] = err["descrizione"]
        g["autofatture"].append(s)
    out = []
    for g in gruppi.values():
        g["autofatture"].sort(key=lambda s: s["numero_fattura"] or "")
        g["count"] = len(g["autofatture"])
        g["n_aperti"] = sum(1 for s in g["autofatture"] if not s["risolto"])
        out.append(g)
    out.sort(key=lambda g: (g["customer_name"].lower(), -g["count"], g["codice"]))
    return out


def _count_scarti_aperti():
    """Conteggio scarti aperti per il badge navbar — niente XML/prenotazioni."""
    try:
        with db() as conn:
            return conn.execute("""
                WITH af AS (
                    SELECT REPLACE(SUBSTR(filename, INSTR(filename, '_') + 1), '.xml', '') AS numero_fattura
                    FROM autofatture
                ),
                rc AS (SELECT DISTINCT numero_fattura FROM risposte_SDI WHERE tipo = 'RC'),
                ns AS (SELECT DISTINCT numero_fattura FROM risposte_SDI WHERE tipo = 'NS')
                SELECT COUNT(*) FROM af
                JOIN ns ON ns.numero_fattura = af.numero_fattura
                LEFT JOIN rc ON rc.numero_fattura = af.numero_fattura
                WHERE rc.numero_fattura IS NULL
            """).fetchone()[0]
    except Exception:
        return 0


@app.route("/scarti")
def scarti_page():
    scarti = _get_scarti_aperti()
    scarti_recenti = _get_scarti_recenti(24)
    scarti_recenti_gruppi = _raggruppa_scarti_per_cliente_problema(scarti_recenti)
    return render_template(
        "scarti.html",
        scarti=scarti,
        scarti_recenti=scarti_recenti,
        scarti_recenti_gruppi=scarti_recenti_gruppi,
    )


@app.route("/zip-inviati")
def zip_inviati_page():
    with db() as conn:
        rows = conn.execute("""
            SELECT * FROM zip_inviati
            ORDER BY
              CASE WHEN zip_filename LIKE 'IT02763130222%' THEN 0 ELSE 1 END,
              zip_filename COLLATE NOCASE DESC
        """).fetchall()
    zips = []
    for r in rows:
        d = dict(r)
        raw = d.get("email_date")
        try:
            d["email_date_fmt"] = parsedate_to_datetime(raw).strftime("%Y-%m-%d") if raw else None
        except (TypeError, ValueError):
            d["email_date_fmt"] = None
        zips.append(d)
    return render_template("zip_inviati.html", zips=zips)


@app.route("/zip-inviati/<int:zip_id>/download")
def zip_inviati_download(zip_id):
    """Ricostruisce lo ZIP dagli XML in autofatture e lo scarica."""
    with db() as conn:
        zip_row = conn.execute("SELECT zip_filename FROM zip_inviati WHERE id = ?", (zip_id,)).fetchone()
        if not zip_row:
            return "ZIP non trovato", 404
        rows = conn.execute(
            "SELECT filename, content FROM autofatture WHERE zip_id = ?",
            (zip_id,),
        ).fetchall()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for r in rows:
            zf.writestr(r["filename"], (r["content"] or "").encode("utf-8"))
    buf.seek(0)
    download_name = zip_row["zip_filename"] or "download.zip"
    return send_file(buf, mimetype="application/zip", as_attachment=True, download_name=download_name)


@app.route("/email")
def email_page():
    emails = get_emails()
    return render_template("email.html", emails=emails)


@app.route("/email/search")
def email_search():
    q = request.args.get("q", "")
    emails = search_emails(q)
    return {"emails": emails}


@app.route("/email/allegato/<filename>")
def email_allegato(filename):
    with db() as conn:
        row = conn.execute("SELECT * FROM email_attachments WHERE filename = ?", (filename,)).fetchone()
    if not row:
        return "Allegato non trovato", 404
    return render_template("allegato.html", att=dict(row))


# ── Pagina Fatture (charges Stripe + XML FatturaPA generati da noi) ─────────
# Replica della prima parte di /nuove-fatture del tool xml-visualizer-fatture:
# tabella transazioni del mese + pannello dettaglio con vista Fattura/XML.

_MESI_IT = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
            "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"]
_MESI_IT_SHORT = ["gen", "feb", "mar", "apr", "mag", "giu",
                  "lug", "ago", "set", "ott", "nov", "dic"]

# Stato charge → (label italiana, rank di ordinamento: riusciti, sospesi, falliti)
_CHARGE_STATUS = {
    "succeeded": ("Riuscito", 0),
    "pending": ("In sospeso", 1),
    "failed": ("Non riuscito", 2),
}

# Stato SDI Aruba (campo invoices[].status, codice come stringa) → (label IT, colore).
# Codici dal ciclo SDI: 3 inviata, 4 scartata, 5 non consegnata, 6 recapito
# impossibile, 7 consegnata, 8 accettata, 9 rifiutata, 10 decorrenza termini.
# colore: green=ok (consegnata/accettata/decorrenza), red=problema (scarto/rifiuto/
# recapito impossibile), yellow=in corso (inviata/non consegnata), grey=sconosciuto.
_SDI_STATUS = {
    "1": ("Presa in carico", "yellow"),
    "2": ("Errore invio", "red"),
    "3": ("Inviata", "yellow"),
    "4": ("Scartata", "red"),
    "5": ("Non consegnata", "yellow"),
    "6": ("Recapito impossibile", "red"),
    "7": ("Consegnata", "green"),
    "8": ("Accettata", "green"),
    "9": ("Rifiutata", "red"),
    "10": ("Decorrenza termini", "green"),
}


def _sdi_status_meta(code, description=None):
    """(label, colore) per un codice stato SDI Aruba. Fallback su description o
    'Sconosciuto' se il codice non è mappato."""
    if code is not None and str(code) in _SDI_STATUS:
        return _SDI_STATUS[str(code)]
    return (description or "Sconosciuto", "grey")

_DECLINE_IT = {
    "generic_decline": "Rifiuto generico",
    "insufficient_funds": "Fondi insufficienti",
    "card_declined": "Carta rifiutata",
    "expired_card": "Carta scaduta",
    "incorrect_cvc": "CVC errato",
    "processing_error": "Errore di elaborazione",
    "authentication_required": "Autenticazione richiesta",
    "do_not_honor": "Rifiuto della banca",
    "payment_intent_payment_attempt_failed": "Tentativo di addebito fallito",
}


def _fatture_month_label(key):
    y, m = key.split("-")
    return f"{_MESI_IT[int(m) - 1].capitalize()} {y}"


def _fmt_amount_cents(amount, currency):
    if amount is None:
        return "—"
    s = f"{amount / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{s} {(currency or 'eur').upper()}"


def _fmt_charge_date(unix_ts):
    if not unix_ts:
        return "—"
    dt = datetime.fromtimestamp(unix_ts, tz=fattura_xml.ROME_TZ)
    return f"{dt.day} {_MESI_IT_SHORT[dt.month - 1]}, {dt.strftime('%H:%M')}"


def _pm_label(pm_type, pm_brand, pm_last4):
    if not pm_type:
        return "—"
    if pm_type == "link":
        return "Link"
    if pm_type == "sepa_debit":
        return "SEPA" + (f" {pm_last4}" if pm_last4 else "")
    if pm_type == "card":
        brand = pm_brand.capitalize() if pm_brand else "Carta"
        return brand + (f" {pm_last4}" if pm_last4 else "")
    return pm_type


def _customer_tooltip(customer_json):
    """Riduce il customer Stripe ai campi mostrati nel tooltip cliente."""
    if not customer_json:
        return None
    try:
        c = json.loads(customer_json)
    except (TypeError, ValueError):
        return None
    addr = c.get("address") or {}
    addr_parts = [addr.get("line1")]
    if addr.get("postal_code") and addr.get("city"):
        addr_parts.append(f"{addr['postal_code']} {addr['city']}")
    else:
        addr_parts.append(addr.get("city") or addr.get("postal_code"))
    addr_parts.append(addr.get("country"))
    tax_ids = [t.get("value") for t in ((c.get("tax_ids") or {}).get("data") or []) if t.get("value")]
    return {
        "name": c.get("business_name") or c.get("name"),
        "email": c.get("email"),
        "phone": c.get("phone"),
        "tax_ids": tax_ids,
        "address": ", ".join(p for p in addr_parts if p),
        "description": c.get("description"),
    }


def _fatture_green_rows(conn, month):
    """Transazioni "tutto apposto" del mese: pagamento riuscito + fattura
    generabile con XML ben formato. Ordinate per data crescente (numerazione
    progressiva mensile)."""
    return conn.execute(
        """SELECT c.charge_id, c.created, g.xml, g.numero
           FROM stripe_charges c
           JOIN fatture_generate g ON g.charge_id = c.charge_id
           WHERE substr(c.created_iso, 1, 7) = ?
             AND c.status = 'succeeded'
             AND g.generabile = 1
             AND g.xml_error IS NULL
           ORDER BY c.created""",
        (month,),
    ).fetchall()


@app.route("/fatture")
def fatture_page():
    requested = request.args.get("month")
    with db() as conn:
        months = [r["m"] for r in conn.execute(
            "SELECT DISTINCT substr(created_iso, 1, 7) AS m FROM stripe_charges "
            "WHERE created_iso IS NOT NULL ORDER BY m DESC"
        ).fetchall()]
        now_month = datetime.now(fattura_xml.ROME_TZ).strftime("%Y-%m")
        if requested in months:
            month = requested
        elif now_month in months:
            month = now_month
        else:
            month = months[0] if months else now_month
        total_count = conn.execute("SELECT COUNT(*) FROM stripe_charges").fetchone()[0]
        rows = conn.execute(
            """SELECT c.*, g.generabile, g.note, g.xml_error, g.numero
               FROM stripe_charges c
               LEFT JOIN fatture_generate g ON g.charge_id = c.charge_id
               WHERE substr(c.created_iso, 1, 7) = ?""",
            (month,),
        ).fetchall()
        progressive = {r["charge_id"]: i + 1 for i, r in enumerate(_fatture_green_rows(conn, month))}
        # Stato SDI (da Aruba) per numero fattura: più charge → una sola fattura,
        # quindi correliamo per `numero`. Mappa numero → riga fatture_sdi_stato.
        sdi_by_numero = {
            r["numero"]: r for r in conn.execute(
                "SELECT numero, status_code, status_label, status_description, id_sdi, last_checked FROM fatture_sdi_stato"
            ).fetchall()
        }

    transactions = []
    tooltips = {}
    for r in rows:
        t = dict(r)
        status_label, rank = _CHARGE_STATUS.get(t["status"], (t["status"] or "—", 99))
        t["status_label"] = status_label
        t["status_rank"] = rank
        sdi = sdi_by_numero.get(t.get("numero"))
        if sdi:
            t["sdi_status_label"], t["sdi_status_color"] = _sdi_status_meta(
                sdi["status_code"], sdi["status_description"])
        else:
            t["sdi_status_label"], t["sdi_status_color"] = (None, "none")
        t["progressive"] = progressive.get(t["charge_id"])
        t["amount_fmt"] = _fmt_amount_cents(t["amount"], t["currency"])
        t["created_fmt"] = _fmt_charge_date(t["created"])
        t["pm_label"] = _pm_label(t["pm_type"], t["pm_brand"], t["pm_last4"])
        t["failure_label"] = _DECLINE_IT.get(t["failure_reason"], t["failure_reason"]) if t["failure_reason"] else None
        tooltip = _customer_tooltip(t.pop("customer_json", None))
        if tooltip:
            tooltips[t["charge_id"]] = tooltip
        transactions.append(t)
    transactions.sort(key=lambda t: (t["status_rank"], t["created"] or 0))

    return render_template(
        "fatture.html",
        transactions=transactions,
        months=[(m, _fatture_month_label(m)) for m in months],
        current_month=month,
        total_count=total_count,
        zip_count=len(progressive),
        tooltips_json=json.dumps(tooltips, ensure_ascii=False).replace("</", "<\\/"),
    )


@app.route("/invio-fatture")
def invio_fatture_page():
    """Indicatore compatto di sola lettura: elenco delle fatture del mese
    (stessi dati di /fatture) con la scadenza visiva del 13. Nessuna azione."""
    requested = request.args.get("month")
    with db() as conn:
        months = [r["m"] for r in conn.execute(
            "SELECT DISTINCT substr(created_iso, 1, 7) AS m FROM stripe_charges "
            "WHERE created_iso IS NOT NULL ORDER BY m DESC"
        ).fetchall()]
        now = datetime.now(fattura_xml.ROME_TZ)
        now_month = now.strftime("%Y-%m")
        if requested in months:
            month = requested
        elif now_month in months:
            month = now_month
        else:
            month = months[0] if months else now_month
        rows = conn.execute(
            """SELECT c.*, g.generabile, g.note, g.xml_error
               FROM stripe_charges c
               LEFT JOIN fatture_generate g ON g.charge_id = c.charge_id
               WHERE substr(c.created_iso, 1, 7) = ?""",
            (month,),
        ).fetchall()
        n_generabili = len(_fatture_green_rows(conn, month))

    transactions = []
    n_errore = 0
    for r in rows:
        t = dict(r)
        t.pop("customer_json", None)
        status_label, rank = _CHARGE_STATUS.get(t["status"], (t["status"] or "—", 99))
        t["status_label"] = status_label
        t["status_rank"] = rank
        t["amount_fmt"] = _fmt_amount_cents(t["amount"], t["currency"])
        t["created_fmt"] = _fmt_charge_date(t["created"])
        if t.get("xml_error"):
            n_errore += 1
        transactions.append(t)
    transactions.sort(key=lambda t: (t["status_rank"], t["created"] or 0))

    # Scadenza visiva: invio fatture entro il 13 del mese (nessun automatismo).
    deadline = {"giorno": 13, "mancano": 13 - now.day, "scaduto": now.day > 13}

    return render_template(
        "invio_fatture.html",
        transactions=transactions,
        months=[(m, _fatture_month_label(m)) for m in months],
        current_month=month,
        n_totali=len(transactions),
        n_generabili=n_generabili,
        n_errore=n_errore,
        deadline=deadline,
        has_data=bool(months),
    )


@app.route("/api/fatture/generata")
def api_fatture_generata():
    """Fatture generate del mese richiesto, keyed per charge_id (per il
    pannello dettaglio della pagina /fatture)."""
    month = request.args.get("month", "")
    with db() as conn:
        rows = conn.execute(
            """SELECT g.charge_id, g.xml, g.generabile, g.note
               FROM fatture_generate g
               JOIN stripe_charges c ON c.charge_id = g.charge_id
               WHERE substr(c.created_iso, 1, 7) = ?""",
            (month,),
        ).fetchall()
    return jsonify({
        r["charge_id"]: {"xml": r["xml"], "generabile": bool(r["generabile"]), "note": r["note"]}
        for r in rows
    })


@app.route("/fatture/download-zip")
def fatture_download_zip():
    """ZIP delle fatture generate del mese, con nomenclatura SDI obbligatoria
    IT<CF trasmittente>_<progressivo max 5 alfanumerici>.xml — il progressivo
    deriva dal numero fattura (univoco e crescente, mai riusato)."""
    month = request.args.get("month", "")
    with db() as conn:
        rows = _fatture_green_rows(conn, month)
    if not rows:
        return "Nessuna fattura generabile nel mese richiesto", 404
    cf = fattura_xml.ID_TRASMITTENTE["codice"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, r in enumerate(rows, start=1):
            suffix = re.sub(r"[^0-9A-Za-z]", "", (r["numero"] or "").split("-")[-1])[-5:]
            prog = (suffix or str(i)).rjust(5, "0")
            zf.writestr(f"IT{cf}_{prog}.xml", (r["xml"] or "").encode("utf-8"))
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"fatture-{month}.zip")


@app.route("/sync-fatture", methods=["POST"])
def sync_fatture_route():
    sync_fatture()
    # Stato SDI da Aruba: isolato, così un errore (es. delega FAW-R mancante) non
    # rompe il sync delle fatture.
    try:
        sync_aruba_stato()
    except Exception:
        logging.getLogger(__name__).exception("sync_aruba_stato (route) fallita")
    return redirect(request.referrer or "/fatture")


# Etichette IT degli stati subscription, con rank per ordinamento (errori in cima).
_SUBSCRIPTION_STATUS = {
    "past_due": ("Pagamento in ritardo", "error", 0),
    "unpaid": ("Non pagato", "error", 0),
    "incomplete": ("Incompleto", "error", 0),
    "incomplete_expired": ("Incompleto scaduto", "error", 0),
    "active": ("Attivo", "ok", 1),
    "trialing": ("In prova", "ok", 1),
    "paused": ("In pausa", "warn", 2),
    "canceled": ("Annullato", "neutral", 3),
}


def _pagamenti_subscriptions():
    """Legge gli abbonamenti Stripe arricchiti (status_label/kind/rank, amount_fmt,
    failed_payment dell'ultimo addebito fallito del mese) e li ordina con gli errori in
    cima. Ritorna (subscriptions, has_data). Riusato da /pagamenti e dalla dashboard."""
    now_month = datetime.now(fattura_xml.ROME_TZ).strftime("%Y-%m")
    with db() as conn:
        has_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='stripe_subscriptions'"
        ).fetchone()
        rows = conn.execute(
            "SELECT * FROM stripe_subscriptions"
        ).fetchall() if has_table else []
        # Telefono per customer_id (da stripe_clienti) → serve al bottone
        # "Avvisa su WhatsApp" sugli abbonamenti in errore (#20).
        phone_by_cust = {
            r["stripe_id"]: r["phone"]
            for r in conn.execute(
                "SELECT stripe_id, phone FROM stripe_clienti WHERE phone IS NOT NULL AND phone != ''"
            ).fetchall()
        }
        # Ultimo charge fallito del mese corrente per customer_id → evidenza
        # "pagamento fallito questo mese" (data + motivo).
        failed = {}
        for c in conn.execute(
            """SELECT customer_id, created, failure_reason FROM stripe_charges
               WHERE status = 'failed' AND substr(created_iso, 1, 7) = ?
               ORDER BY created DESC""",
            (now_month,),
        ).fetchall():
            cid = c["customer_id"]
            if cid and cid not in failed:
                reason = c["failure_reason"]
                failed[cid] = {
                    "date": _fmt_charge_date(c["created"]),
                    "reason": _DECLINE_IT.get(reason, reason) if reason else None,
                }

    subscriptions = []
    for r in rows:
        s = dict(r)
        label, kind, rank = _SUBSCRIPTION_STATUS.get(
            s["status"], (s["status"] or "—", "neutral", 4))
        s["status_label"] = label
        s["status_kind"] = kind
        s["status_rank"] = rank
        s["amount_fmt"] = _fmt_amount_cents(s["plan_amount"], s["currency"])
        s["failed_payment"] = failed.get(s["customer_id"])
        s["phone"] = phone_by_cust.get(s["customer_id"])
        subscriptions.append(s)
    # Errori in cima, poi per stato; a parità, più recenti prima.
    subscriptions.sort(key=lambda s: (0 if s["is_error"] else 1, s["status_rank"],
                                      -(s["created"] or 0)))
    return subscriptions, bool(rows)


@app.route("/pagamenti")
def pagamenti_page():
    subscriptions, has_data = _pagamenti_subscriptions()
    errori = [s for s in subscriptions if s["is_error"]]

    return render_template(
        "pagamenti.html",
        subscriptions=subscriptions,
        errori=errori,
        n_errore=len(errori),
        n_attive=sum(1 for s in subscriptions if s["status"] in ("active", "trialing")),
        n_totali=len(subscriptions),
        has_data=has_data,
    )


@app.route("/ricavi")
def ricavi_page():
    """Quadro ricavi del mese in corso a partire dalle anteprime "upcoming".
    Con billing a consumo posticipato è l'unico dato che riflette quanto si sta
    maturando ADESSO (vs. l'ultima fattura emessa, che è il mese precedente)."""
    overview = _ricavi_overview()
    return render_template("ricavi.html", r=overview)


@app.route("/sync-upcoming", methods=["POST"])
def sync_upcoming_route():
    """Sincronizza le anteprime upcoming. Richiede stripe_subscriptions già
    popolata: se manca, esegue prima sync_subscriptions."""
    try:
        with db() as conn:
            has = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='stripe_subscriptions'"
            ).fetchone()
        if not has:
            sync_subscriptions()
        sync_upcoming_invoices()
    except Exception:
        app.logger.exception("sync_upcoming_invoices failed in /sync-upcoming")
    return redirect(request.referrer or "/ricavi")


@app.route("/sync-subscriptions", methods=["POST"])
def sync_subscriptions_route():
    sync_subscriptions()
    return redirect(request.referrer or "/pagamenti")


@app.route("/pagamenti/<path:subscription_id>/avvisa-whatsapp", methods=["POST"])
def pagamenti_avvisa_whatsapp(subscription_id):
    """#20 — Accoda (PENDING, richiede approvazione) un messaggio WhatsApp al
    cliente con un abbonamento in errore: avviso di pagamento non riuscito +
    link di rinnovo (hosted_invoice_url della fattura insoluta). L'admin lo
    approva su /actions; poi il host NanoClaw lo consegna via Baileys.

    Riusa l'action_type `whatsapp_reply` con payload sintetico verso il numero
    del cliente (stessa forma del composer dashboard: session_id='dashboard',
    agent_group_id NULL) — il host consegna a qualunque JID, anche senza una
    chat preesistente."""
    from customer_link import normalize_phone

    with db() as conn:
        sub = conn.execute(
            "SELECT s.*, c.phone AS cust_phone FROM stripe_subscriptions s "
            "LEFT JOIN stripe_clienti c ON c.stripe_id = s.customer_id "
            "WHERE s.subscription_id = ?",
            (subscription_id,),
        ).fetchone()
    if sub is None:
        return redirect("/pagamenti?wa=notfound")
    digits = normalize_phone(sub["cust_phone"])
    if not digits:
        return redirect("/pagamenti?wa=nophone")
    link = sub["latest_invoice_url"]
    if not link:
        return redirect("/pagamenti?wa=nolink")

    name = (sub["customer_name"] or "").strip()
    first = name.split()[0] if name else None
    saluto = f"Ciao {first}" if first else "Ciao"
    text = (
        f"{saluto}, ti scrivo da Autofatturiamo 👋\n"
        "Il rinnovo del tuo abbonamento non è andato a buon fine, probabilmente "
        "un problema con la carta. Puoi sistemare il pagamento da qui:\n"
        f"{link}\n"
        "Se hai bisogno scrivimi pure. Grazie!"
    )

    jid = f"{digits}@s.whatsapp.net"
    ts_ms = int(time.time() * 1000)
    payload = {
        "platform_id": jid,
        "thread_id": None,
        "in_reply_to": None,
        "session_id": "dashboard",
        "msg_id": f"payfail-{subscription_id}-{ts_ms}",
        "content_json": json.dumps({"text": text}, ensure_ascii=False),
        "files": None,
        "recipient_label": name or _phone_fmt(jid),
        "reason": (
            f"Pagamento abbonamento non riuscito (stato {sub['status']}). "
            "Avviso al cliente su WhatsApp con link di rinnovo."
        ),
    }
    actions_db.enqueue("whatsapp_reply", payload)
    return redirect("/pagamenti?wa=queued")


@app.route("/sync", methods=["POST"])
def sync():
    sync_stripe()
    sync_odoo_opportunita()
    sync_odoo()
    sync_email()
    sync_fatture()
    sync_subscriptions()
    try:
        sync_upcoming_invoices()
    except Exception:
        app.logger.exception("sync_upcoming_invoices failed in /sync")
    try:
        materializza_clienti()
    except Exception:
        app.logger.exception("materializza_clienti failed in /sync")
    return redirect(request.referrer or "/")


@app.route("/sync-stripe", methods=["POST"])
def sync_stripe_route():
    sync_stripe()
    return redirect(request.referrer or "/")


@app.route("/sync-odoo", methods=["POST"])
def sync_odoo_route():
    sync_odoo_opportunita()
    sync_odoo()
    return redirect(request.referrer or "/")


@app.route("/sync-email", methods=["POST"])
def sync_email_route():
    force = request.form.get("force") == "1"
    result = sync_email(force=force)
    if force and result.get("status") == "in_progress":
        return jsonify(result)
    return redirect(request.referrer or "/")


@app.route("/bot/toggle", methods=["POST"])
def bot_toggle():
    plist = _nanoclaw_plist()
    domain = _nanoclaw_domain()
    if plist is None or domain is None:
        app.logger.error("bot toggle: no com.nanoclaw*.plist in %s", _LAUNCH_AGENTS_DIR)
        return redirect(request.referrer or "/")
    if _bot_is_running():
        r = subprocess.run(
            ["launchctl", "bootout", domain],
            capture_output=True, text=True,
        )
    else:
        r = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)],
            capture_output=True, text=True,
        )
    if r.returncode != 0:
        app.logger.error(
            "bot toggle: launchctl exit=%d stderr=%r stdout=%r",
            r.returncode, r.stderr.strip(), r.stdout.strip(),
        )
    _bot_state_invalidate()
    return redirect(request.referrer or "/")


# --- Operational actions: coda gated dall'utente ---------------------------

# Numero WhatsApp di test per il pulsante "Simula": l'azione NON viene approvata
# né inviata al destinatario reale; ne creiamo una copia diretta a questo JID per
# vedere come arriva il messaggio sul proprio telefono. +39 379 123 4555.
SIMULAZIONE_WHATSAPP_JID = "393791234555@s.whatsapp.net"


def _whatsapp_text_for_simulation(action_type, payload, form):
    """Testo del messaggio WhatsApp da inviare in simulazione, per action_type.

    whatsapp_reply  → testo da payload.content_json.text
    payment_failed_whatsapp → message_text dal form (eventualmente modificato),
        altrimenti quello salvato nel payload, altrimenti ricomposto dal link.
    Ritorna stringa vuota se non c'è nulla da inviare.
    """
    if action_type == "whatsapp_reply":
        raw = payload.get("content_json")
        if isinstance(raw, str):
            try:
                content = json.loads(raw)
            except (ValueError, TypeError):
                content = {}
            if isinstance(content, dict):
                text = content.get("text")
                if isinstance(text, str) and text.strip():
                    return text.strip()
        return ""
    if action_type == "payment_failed_whatsapp":
        text = (form.get("message_text") or "").strip()
        if text:
            return text
        text = (payload.get("message_text") or "").strip()
        if text:
            return text
        link = payload.get("invoice_url")
        if isinstance(link, str) and link.strip():
            from action_executors.payment_failed_whatsapp import compose_message
            return compose_message(
                payload.get("customer_name"), link.strip(), payload.get("customer_first_name")
            )
        return ""
    return ""


@app.route("/actions")
def actions_list():
    actions = actions_db.list_recent_all(limit=100)
    pending = [a for a in actions if a["status"] == "pending"]
    storico = [a for a in actions if a["status"] != "pending"]
    return render_template("actions.html", actions=actions, pending=pending, storico=storico)


@app.route("/alternatives")
def alternatives():
    return render_template("alternatives.html")


@app.route("/actions/<int:action_id>/approve", methods=["POST"])
def actions_approve(action_id):
    row = actions_db.approve(action_id)
    if row is None:
        return redirect(request.referrer or "/actions")
    try:
        payload = json.loads(row["payload_json"])
        # Alcune azioni (es. payment_failed_whatsapp) richiedono dati inseriti
        # a mano in fase di approvazione: se il form li porta, li iniettiamo nel
        # payload prima del dispatch (numero WhatsApp + testo eventualmente
        # modificato dall'operatore).
        phone = (request.form.get("phone") or "").strip()
        if phone:
            payload["phone"] = phone
        message_text = (request.form.get("message_text") or "").strip()
        if message_text:
            payload["message_text"] = message_text
        result = action_executors.dispatch(row["action_type"], payload)
        actions_db.mark_done(action_id, result)
    except Exception as e:
        app.logger.exception("Action %s (type=%s) failed", action_id, row["action_type"])
        actions_db.mark_failed(action_id, str(e))
    return redirect(request.referrer or "/actions")


@app.route("/actions/<int:action_id>/simulate", methods=["POST"])
def actions_simulate(action_id):
    """Invia una COPIA del messaggio WhatsApp al numero di test, senza approvare.

    L'azione originale resta in stato `pending`: serve solo a vedere come arriva
    il messaggio sul proprio telefono prima di approvarlo per il cliente reale.
    Crea una `whatsapp_reply` già `done` (come payment_failed_whatsapp) verso
    SIMULAZIONE_WHATSAPP_JID, che l'host consegna via Baileys.
    """
    row = actions_db.get(action_id)
    if row is None:
        return redirect(request.referrer or "/actions")
    try:
        payload = json.loads(row["payload_json"])
        text = _whatsapp_text_for_simulation(row["action_type"], payload, request.form)
        if not text:
            # Niente testo da simulare (action_type non-WhatsApp o payload vuoto).
            return redirect(request.referrer or "/actions")
        wa_payload = {
            "platform_id": SIMULAZIONE_WHATSAPP_JID,
            "thread_id": None,
            "in_reply_to": None,
            "session_id": "dashboard",
            "msg_id": f"simulazione-{action_id}-{int(time.time())}",
            "content_json": json.dumps({"text": text}, ensure_ascii=False),
            "files": None,
            "recipient_label": "Simulazione (test)",
            "reason": (
                f"Simulazione dell'azione #{action_id} ({row['action_type']}): "
                "copia inviata al numero di test, l'azione originale resta in attesa."
            ),
        }
        wa_id = actions_db.enqueue("whatsapp_reply", wa_payload)
        actions_db.mark_done(
            wa_id,
            {
                "status": "queued_for_host_delivery",
                "recipient": SIMULAZIONE_WHATSAPP_JID,
                "simulated_action_id": action_id,
            },
        )
    except Exception:
        app.logger.exception("Simulate action %s failed", action_id)
    return redirect(request.referrer or "/actions")


@app.route("/actions/<int:action_id>/reject", methods=["POST"])
def actions_reject(action_id):
    feedback = (request.form.get("feedback") or "").strip()
    if not feedback:
        # Required dal browser via attributo `required` sul textarea; questo
        # è solo un fallback server-side. Senza feedback non rifiutiamo.
        return redirect(request.referrer or "/actions")
    actions_db.reject(action_id, feedback)
    return redirect(request.referrer or "/actions")


@app.route("/actions/<int:action_id>/delete", methods=["POST"])
def actions_delete(action_id):
    deleted = actions_db.delete(action_id)
    if request.headers.get("X-Requested-With") == "fetch":
        return ("", 204) if deleted else ("", 404)
    return redirect(request.referrer or "/actions")


# --- Task pianificati del bot (NanoClaw scheduling) ------------------------
# Vista umana sui task schedulati dal bot (righe kind='task' nei session DB).
# Lettura: sola lettura su ogni inbound.db. Scrittura (modifica/pausa/annulla):
# diretta in DELETE-mode su inbound.db — vedi scheduled_tasks_db.py.

@app.route("/tasks")
def tasks_page():
    all_tasks = scheduled_tasks_db.list_all_tasks()
    ricorrenti = [t for t in all_tasks if t["is_recurring"]]
    one_time = [t for t in all_tasks if not t["is_recurring"]]
    return render_template(
        "tasks.html",
        ricorrenti=ricorrenti,
        one_time=one_time,
        notice=request.args.get("notice"),
    )


@app.route("/tasks/<session_id>/<task_id>/update", methods=["POST"])
def tasks_update(session_id, task_id):
    ag = scheduled_tasks_db.agent_group_for_session(session_id)
    if not ag:
        return redirect("/tasks?notice=notfound")
    prompt = request.form.get("prompt")
    recurrence_raw = (request.form.get("recurrence") or "").strip()
    next_run_local = (request.form.get("next_run") or "").strip()
    process_after = None
    if next_run_local:
        try:
            process_after = scheduled_tasks_db.local_to_utc_iso(next_run_local)
        except ValueError:
            return redirect("/tasks?notice=badtime")
    try:
        n = scheduled_tasks_db.update_task_row(
            ag, session_id, task_id,
            prompt=prompt,
            recurrence=(recurrence_raw or None),  # campo vuoto → one-shot
            process_after=process_after,
        )
    except Exception:
        app.logger.exception("tasks_update failed (%s/%s)", session_id, task_id)
        return redirect("/tasks?notice=error")
    return redirect("/tasks" if n else "/tasks?notice=justfired")


@app.route("/tasks/<session_id>/<task_id>/pause", methods=["POST"])
def tasks_pause(session_id, task_id):
    ag = scheduled_tasks_db.agent_group_for_session(session_id)
    if not ag:
        return redirect("/tasks?notice=notfound")
    n = scheduled_tasks_db.pause_task_row(ag, session_id, task_id)
    return redirect("/tasks" if n else "/tasks?notice=justfired")


@app.route("/tasks/<session_id>/<task_id>/resume", methods=["POST"])
def tasks_resume(session_id, task_id):
    ag = scheduled_tasks_db.agent_group_for_session(session_id)
    if not ag:
        return redirect("/tasks?notice=notfound")
    n = scheduled_tasks_db.resume_task_row(ag, session_id, task_id)
    return redirect("/tasks" if n else "/tasks?notice=justfired")


@app.route("/tasks/<session_id>/<task_id>/cancel", methods=["POST"])
def tasks_cancel(session_id, task_id):
    ag = scheduled_tasks_db.agent_group_for_session(session_id)
    if not ag:
        return redirect("/tasks?notice=notfound")
    n = scheduled_tasks_db.cancel_task_row(ag, session_id, task_id)
    return redirect("/tasks" if n else "/tasks?notice=justfired")


if __name__ == "__main__":
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug:
        start_scheduler()
    app.run(debug=True, port=5001)
