"""
Messaggio settimanale "Stato azienda" — generazione deterministica + invio Telegram.

Sostituisce il vecchio task NanoClaw agent-driven del giovedì: niente più prompt a
Claude, qui i numeri si calcolano da fonti certe e si inviano con i **Rich Messages**
Telegram (Bot API 10.1, giugno 2026) via il metodo `sendRichMessage`, passando il
contenuto come **Rich Markdown** (`InputRichMessage.markdown`) — heading `##` e
tabelle native. Due varianti (flag `monthly`): **settimanale** (finestra ultimi
`window_days` giorni) e **mensile** (tutto il mese solare precedente, es. l'1°
luglio → giugno). Chiamanti che condividono la stessa funzione:

  - scheduler APScheduler (`scheduler.py`): job `summary_weekly` (giovedì 09:00) e
    `summary_monthly` (1° del mese 09:00, `monthly=True`);
  - il comando Telegram `/summary` (intercettato lato host Node in
    `src/channels/telegram.ts`, che fa POST su /api/internal/summary/send) —
    variante settimanale.

Tre sezioni:
  • 🚀 Distribution — numero di demo (meeting cal.com) prenotate nella finestra
    (gcal_events.created, non annullate) + tabella dei nuovi clienti paganti
    negli ultimi `window_days` giorni (stripe_subscriptions active/trialing,
    is_error=0). Zero clienti → ⚠ in grassetto.
  • 💻 Development   — elenco puntato delle PR integrate (merged) negli ultimi
    `window_days` giorni sul repo prodotto, mostrate per titolo, via API GitHub
    (PAT read-only) con fallback alla CLI `gh`.
  • 💶 Economics     — billing posticipato a consumo: il ricavo del mese M è ciò
    che si incassa il mese M+1 (charges Stripe del mese solare successivo,
    ritardatari inclusi). Tabella con gli ultimi 3 mesi conclusi + il mese
    corrente (in corso) come ultima riga: maturato-a-oggi · proiezione fine
    periodo da _ricavi_overview().

Se `sendRichMessage` fallisce, fallback a `sendMessage` testuale (markdown grezzo).
"""

import json
import logging
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

# Default sovrascrivibili da sync-config.json (vedi scheduler.py).
DEFAULT_DEV_REPO = "focolarestudio/autofatturiamo"
DEFAULT_WINDOW_DAYS = 7
TELEGRAM_API = "https://api.telegram.org"

_MESI_IT = (
    "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
    "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre",
)

# services/autofatturiamo-sync → services → repo root
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_ROOT_ENV = _REPO_ROOT / ".env"


# ───────────────────────────── helpers fuso/formato ─────────────────────────

def _rome_tz():
    """Europe/Rome — riusa fattura_xml.ROME_TZ se disponibile, altrimenti zoneinfo."""
    try:
        import fattura_xml
        return fattura_xml.ROME_TZ
    except Exception:  # pragma: no cover
        from zoneinfo import ZoneInfo
        return ZoneInfo("Europe/Rome")


def _eur(value) -> str:
    """Arrotonda a euro interi con separatore migliaia italiano: 1044 → '1.044€'."""
    n = int(round(value or 0))
    return f"{n:,}".replace(",", ".") + "€"


def _mese_label(anno: int, mese: int) -> str:
    """(2026, 5) → 'Maggio'."""
    return _MESI_IT[mese - 1].capitalize()


def _add_month(anno: int, mese: int, delta: int):
    """Aritmetica sui mesi solari. (2026, 12) +1 → (2027, 1)."""
    idx = (anno * 12 + (mese - 1)) + delta
    return idx // 12, (idx % 12) + 1


# ───────────────────────────── segreti (.env di root) ───────────────────────

def _root_env_value(key: str) -> str | None:
    """Valore di `key` da os.environ o, in fallback, dal `.env` di ROOT (non quello
    locale del servizio sync, che non contiene i segreti del bot). None se assente."""
    v = (os.environ.get(key) or "").strip()
    if v:
        return v
    try:
        for raw in _ROOT_ENV.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, val = line.split("=", 1)
            if k.strip() != key:
                continue
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                val = val[1:-1]
            return val or None
    except OSError:
        return None
    return None


def _telegram_token() -> str | None:
    return _root_env_value("TELEGRAM_BOT_TOKEN")


def _github_token() -> str | None:
    """PAT GitHub read-only per la sezione Development (opzionale). Se assente o
    senza accesso al repo, _dev_stats fa fallback alla CLI `gh`."""
    return _root_env_value("GITHUB_SUMMARY_TOKEN")


# ───────────────────────────── sezione Distribution ─────────────────────────

def _nuovi_clienti(since_dt: datetime, until_dt: datetime | None = None):
    """Nuovi clienti paganti con subscription creata nell'intervallo
    [since_dt, until_dt) (until_dt None = fino ad ora).

    Definizione (come il vecchio task): subscription Stripe creata nella finestra,
    attualmente active/trialing e non in errore. Ritorna lista di (nome, data_dt).
    """
    from app import db
    out = []
    sql = (
        "SELECT customer_name, created_iso FROM stripe_subscriptions "
        "WHERE created >= ? AND status IN ('active','trialing') AND is_error=0 "
    )
    params = [int(since_dt.timestamp())]
    if until_dt is not None:
        sql += "AND created < ? "
        params.append(int(until_dt.timestamp()))
    sql += "ORDER BY created DESC"
    with db() as conn:
        rows = conn.execute(sql, params).fetchall()
    for r in rows:
        nome = (r["customer_name"] or "").strip() or "(senza nome)"
        dt = None
        if r["created_iso"]:
            try:
                dt = datetime.fromisoformat(r["created_iso"])
            except ValueError:
                dt = None
        out.append((nome, dt))
    return out


# Solo i veri meeting di vendita: l'event type cal.com «Demo Autofatturiamo — <nome>».
# Gli altri tipi (Assistenza, Attivazione, Prova Gratuita, Meeting, Check…) NON
# sono demo e vanno esclusi dal conteggio.
_DEMO_TYPE_PREFIX = "Demo Autofatturiamo"


def _demo_prenotazioni(since_dt: datetime, until_dt: datetime | None = None) -> list[dict]:
    """Demo (event type cal.com «Demo Autofatturiamo — …») **prenotate**
    nell'intervallo [since_dt, until_dt) (until_dt None = fino ad ora).

    «Prenotata» = la PRENOTAZIONE è avvenuta nella finestra (colonna `created`
    del booking), non l'orario in cui la demo si svolge — è l'indicatore di
    attività commerciale del periodo. Esclude gli annullati. `gcal_events.created`
    è sempre ISO 8601 in UTC (gcal_client._format_dt), quindi convertiamo a UTC
    anche i bound e confrontiamo come stringhe ISO omogenee.

    Ritorna lista di dict `{nome, created}` (nome = prospect che ha prenotato),
    ordinata per prenotazione più recente.
    """
    from app import db
    since_utc = since_dt.astimezone(timezone.utc).isoformat()
    like = _DEMO_TYPE_PREFIX + "%"
    sql = (
        "SELECT cal_prospect_name, summary, created FROM gcal_events "
        "WHERE cal_booking_id IS NOT NULL AND created IS NOT NULL "
        "  AND created >= ? "
        "  AND (status IS NULL OR upper(status) != 'CANCELLED') "
        "  AND (cal_event_type LIKE ? "
        "       OR (cal_event_type IS NULL AND summary LIKE ?)) "
    )
    params = [since_utc, like, like]
    if until_dt is not None:
        sql += "AND created < ? "
        params.append(until_dt.astimezone(timezone.utc).isoformat())
    sql += "ORDER BY created DESC"
    with db() as conn:
        rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        nome = (r["cal_prospect_name"] or "").strip()
        if not nome:  # fallback: «Demo Autofatturiamo — <Nome>» dal summary
            s = r["summary"] or ""
            nome = s.split("—", 1)[-1].strip() if "—" in s else ""
        out.append({"nome": nome or "(senza nome)", "created": r["created"]})
    return out


def _demo_prenotate(since_dt: datetime, until_dt: datetime | None = None) -> int:
    """Numero di demo prenotate nella finestra (vedi `_demo_prenotazioni`)."""
    return len(_demo_prenotazioni(since_dt, until_dt))


# ───────────────────────────── sezione Development ──────────────────────────

def _dev_stats(repo: str, since_str: str, until_str: str | None = None):
    """PR integrate (merged) con `mergedAt` in [since_str, until_str), con titolo.
    Date in formato 'YYYY-MM-DD'; `until_str` None = nessun limite superiore
    (caso settimanale). Per il mensile l'intervallo è il mese solare precedente.

    Sorgente primaria: API GitHub col PAT read-only (`GITHUB_SUMMARY_TOKEN`); se
    il token è assente o non ha accesso al repo, fallback automatico alla CLI
    `gh`. Ritorna {'ok', 'n_pr', 'prs': [{'number','title','mergedAt'}], 'source'},
    con `prs` ordinate per merge più recente. ok=False (solo se anche `gh` fallisce)
    → la sezione mostra 'dati non disponibili' invece di rompere tutto.
    """
    token = _github_token()
    raw = _dev_stats_api_raw(repo, token) if token else None
    source = "api"
    if raw is None:  # token assente/senza accesso/errore → fallback a gh
        raw = _dev_stats_gh_raw(repo, since_str)
        source = "gh"
    if raw is None:
        return {"ok": False, "n_pr": 0, "prs": [], "source": "gh"}
    win = [
        r for r in raw
        if (r.get("mergedAt") or "")[:10] >= since_str
        and (until_str is None or (r.get("mergedAt") or "")[:10] < until_str)
    ]
    prs = _normalizza_prs(win)
    return {"ok": True, "n_pr": len(prs), "prs": prs, "source": source}


def _dev_stats_api_raw(repo: str, token: str):
    """GraphQL `repository.pullRequests` (no search index → ok per fine-grained
    PAT): lista delle 100 PR merged aggiornate più di recente, come nodi grezzi
    {number,title,mergedAt}. None se il repo non è risolvibile/accessibile o su
    errore di rete (→ il chiamante fa fallback a `gh`). Il filtro per intervallo
    lo applica `_dev_stats`."""
    try:
        owner, name = repo.split("/", 1)
    except ValueError:
        return None
    query = (
        "query($o:String!,$n:String!){repository(owner:$o,name:$n){"
        "pullRequests(states:MERGED,first:100,orderBy:{field:UPDATED_AT,direction:DESC})"
        "{nodes{number title body mergedAt}}}}"
    )
    try:
        res = requests.post(
            "https://api.github.com/graphql",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"query": query, "variables": {"o": owner, "n": name}},
            timeout=20,
        )
        data = res.json() if res.content else {}
    except (requests.RequestException, ValueError) as e:
        logger.warning("weekly_summary: GitHub API non raggiungibile (%s)", e)
        return None
    if data.get("errors"):
        logger.warning("weekly_summary: GitHub API NOT_FOUND/errore (token senza "
                       "accesso al repo?) %s", str(data["errors"])[:200])
        return None
    repo_node = (data.get("data") or {}).get("repository")
    if not repo_node:
        return None
    nodes = repo_node["pullRequests"]["nodes"]
    if len(nodes) >= 100:
        logger.warning("weekly_summary: 100 PR nella pagina, finestra forse troncata")
    return nodes


def _dev_stats_gh_raw(repo: str, since_str: str):
    """Fallback via CLI `gh` (usa l'auth del keychain dell'utente): nodi grezzi
    {number,title,mergedAt} delle PR merged da `since_str` in poi. L'eventuale
    limite superiore (mensile) lo applica `_dev_stats`. None su errore."""
    try:
        proc = subprocess.run(
            [
                "gh", "pr", "list", "--repo", repo, "--state", "merged",
                "--limit", "200", "--json", "number,title,body,mergedAt",
                "--search", f"merged:>={since_str}",
            ],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            logger.warning("weekly_summary: gh pr list rc=%s err=%s",
                           proc.returncode, (proc.stderr or "").strip()[:200])
            return None
        return json.loads(proc.stdout or "[]")
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        logger.warning("weekly_summary: gh non disponibile (%s)", e)
        return None


def _normalizza_prs(rows):
    """Normalizza i nodi PR (da API GraphQL o da `gh --json`) in una lista di
    dict {'number','title','body','mergedAt'} ordinata per merge più recente.
    `body` (descrizione della PR) serve al resoconto AI di development_summary;
    il summary settimanale/mensile lo ignora."""
    prs = [
        {
            "number": r.get("number"),
            "title": (r.get("title") or "").strip() or f"PR #{r.get('number')}",
            "body": (r.get("body") or "").strip(),
            "mergedAt": r.get("mergedAt") or "",
        }
        for r in rows
    ]
    prs.sort(key=lambda p: p["mergedAt"], reverse=True)
    return prs


# ───────────────────────────── sezione Economics ────────────────────────────

def _incassato_mese(anno: int, mese: int) -> float:
    """Lordo incassato (charges succeeded/paid, non rimborsate) nel mese solare
    (anno, mese), in euro. È il dato che chiude un mese di CONSUMO precedente."""
    from app import db
    ym = f"{anno:04d}-{mese:02d}"
    with db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS tot FROM stripe_charges "
            "WHERE status IN ('succeeded','paid') AND refunded=0 "
            "  AND substr(created_iso,1,7) = ?",
            (ym,),
        ).fetchone()
    return (row["tot"] or 0) / 100.0


def _economics(now: datetime):
    """Quadro ricavi: 3 mesi conclusi (consumo → incassato il mese dopo) + mese
    corrente (maturato + proiezione). Ritorna dict pronto per il rendering."""
    from app import _ricavi_overview

    mesi = []
    # Ultimi 3 mesi conclusi prima di quello corrente. Il ricavo del mese M
    # (consumo) = incassato nel mese M+1.
    for back in (3, 2, 1):
        y, m = _add_month(now.year, now.month, -back)
        iy, im = _add_month(y, m, 1)  # mese di incasso
        mesi.append({
            "label": _mese_label(y, m),
            "incassato": _incassato_mese(iy, im),
        })

    overview = _ricavi_overview(now=now)
    return {
        "mesi": mesi,
        "corrente": {
            "label": _mese_label(now.year, now.month),
            "maturato": overview["accrued"]["gross"],
            "proiezione": overview["projection"]["gross"],
            "has_data": overview.get("has_data", False),
        },
    }


def _proiezione_mese(anno: int, mese: int):
    """Proiezione lordo (euro) del mese di CONSUMO (anno, mese) dallo snapshot
    `ricavi_mensili` (vedi app._snapshot_ricavi_mese_corrente). È il dato del
    report mensile: l'1° del mese dopo, l'incasso del mese chiuso non è ancora in
    `stripe_charges`, ma la sua proiezione fine-periodo è stata congelata qui.
    None se la riga non esiste (snapshot non ancora registrato)."""
    from app import db
    ym = f"{anno:04d}-{mese:02d}"
    try:
        with db() as conn:
            row = conn.execute(
                "SELECT proiezione_gross FROM ricavi_mensili WHERE ym = ?", (ym,)
            ).fetchone()
    except Exception:  # tabella assente o DB non pronto → degrada a 'non disponibile'
        return None
    if not row or row["proiezione_gross"] is None:
        return None
    return row["proiezione_gross"]


# ───────────────────────────── composizione Rich Markdown ───────────────────

def _md(s: str) -> str:
    """Escape dei caratteri Markdown problematici nelle celle/inline (soprattutto
    `|`, che spezzerebbe una riga di tabella)."""
    return re.sub(r'([\\`*_~|\[\]#])', r'\\\1', s or "")


def build_summary_markdown(now: datetime | None = None,
                           *, dev_repo: str = DEFAULT_DEV_REPO,
                           window_days: int = DEFAULT_WINDOW_DAYS,
                           monthly: bool = False) -> str:
    """Costruisce il messaggio in **Rich Markdown** Telegram (per sendRichMessage):
    heading `##` nativi, tabelle/elenco per sezione.

    Due varianti:
      • settimanale (`monthly=False`): finestra = ultimi `window_days` giorni;
        Economics = ultimi 3 mesi conclusi + mese in corso (maturato · proiezione).
      • mensile (`monthly=True`): copre **tutto il mese solare precedente** (es.
        l'1° luglio → giugno); Economics = solo quel mese come **proiezione**
        (snapshot `ricavi_mensili`), perché il suo incasso Stripe avviene durante
        il mese che inizia e non è ancora rilevabile.
    """
    now = now or datetime.now(_rome_tz())
    L = []

    if monthly:
        py, pm = _add_month(now.year, now.month, -1)
        period_start = datetime(py, pm, 1, tzinfo=now.tzinfo)
        period_end = datetime(now.year, now.month, 1, tzinfo=now.tzinfo)
        L.append(f"# 📊 Stato azienda · {_mese_label(py, pm)} {py}")
    else:
        period_start = now - timedelta(days=window_days)
        period_end = None
        data_lbl = f"{now.day} {_MESI_IT[now.month - 1]}"
        L.append(f"# 📊 Stato azienda · settimana del {data_lbl}")

    since_str = period_start.strftime("%Y-%m-%d")
    until_str = period_end.strftime("%Y-%m-%d") if period_end is not None else None

    # 🚀 Distribution
    L += ["", "## 🚀 Distribution", ""]
    n_demo = _demo_prenotate(period_start, period_end)
    L += [f"📅 Demo prenotate: **{n_demo}**", "", "Nuovi clienti paganti:", ""]
    clienti = _nuovi_clienti(period_start, period_end)
    if clienti:
        L.append("| Cliente | Data |")
        L.append("|:--------|-----:|")
        for nome, dt in clienti:
            quando = f"{dt.day} {_MESI_IT[dt.month - 1]}" if dt else "—"
            L.append(f"| {_md(nome)} | {_md(quando)} |")
    else:
        L.append("⚠️ **Nessun cliente onboardato**")

    # 💻 Development — elenco puntato delle PR integrate (titolo)
    L += ["", "## 💻 Development", "", "Miglioramenti della piattaforma:", ""]
    dev = _dev_stats(dev_repo, since_str, until_str)
    if not dev["ok"]:
        L.append("_Dati GitHub non disponibili_")
    elif not dev["prs"]:
        L.append("_Nessuna PR integrata nel periodo_")
    else:
        for pr in dev["prs"]:
            L.append(f"- {_md(pr['title'])}")

    # 💶 Economics
    L += ["", "## 💶 Economics", ""]
    if monthly:
        # Solo il mese chiuso, come proiezione del totale (parte già incassata da
        # Stripe + parte ancora in arrivo per addebiti tardivi/recuperi).
        proj = _proiezione_mese(py, pm)
        L.append("| Mese | Ricavi (proiezione) |")
        L.append("|:-----|--------------------:|")
        val = _eur(proj) if proj is not None else "n/d"
        L.append(f"| {_md(_mese_label(py, pm))} {py} | {val} |")
        if proj is None:
            L += ["", "_Proiezione non ancora disponibile (snapshot mensile assente)._"]
    else:
        # Ultimi 3 mesi conclusi + mese corrente nella stessa tabella.
        eco = _economics(now)
        L.append("| Mese | Ricavi |")
        L.append("|:-----|-------:|")
        for mese in eco["mesi"]:
            L.append(f"| {_md(mese['label'])} | {_eur(mese['incassato'])} |")
        corr = eco["corrente"]
        if corr["has_data"]:
            corr_val = f"{_eur(corr['maturato'])} · proiez. {_eur(corr['proiezione'])}"
        else:
            corr_val = "dati non disponibili"
        L.append(f"| {_md(corr['label'])} (in corso) | {corr_val} |")

    return "\n".join(L)


def summary_live_data(now: datetime | None = None, *,
                      dev_repo: str = DEFAULT_DEV_REPO,
                      window_days: int = DEFAULT_WINDOW_DAYS) -> dict:
    """Dati compatti per il blocco «Stato azienda» live della Dashboard (variante
    settimanale). A differenza di `build_summary_markdown` (testo completo per
    Telegram), qui torniamo solo i **numeri principali** da mostrare in una
    tabellina:

      • Demo prenotate e nuovi clienti nella finestra → conteggi;
      • Development → **solo il numero** di PR integrate (niente titoli/descrizioni);
      • Economics → **solo il mese corrente**: proiezione (valore principale) +
        maturato-a-oggi (dato reale).
    """
    now = now or datetime.now(_rome_tz())
    period_start = now - timedelta(days=window_days)
    since_str = period_start.strftime("%Y-%m-%d")

    dev = _dev_stats(dev_repo, since_str, None)
    eco = _economics(now)
    corr = eco["corrente"]
    demo = _demo_prenotazioni(period_start, None)
    return {
        "window_days": window_days,
        "n_demo": len(demo),
        "demo_nomi": [d["nome"] for d in demo],
        "n_clienti": len(_nuovi_clienti(period_start, None)),
        "dev_ok": dev["ok"],
        "n_pr": dev["n_pr"],
        "eco": {
            "label": corr["label"],
            "has_data": corr["has_data"],
            "proiezione_eur": _eur(corr["proiezione"]) if corr["has_data"] else None,
            "maturato_eur": _eur(corr["maturato"]) if corr["has_data"] else None,
        },
    }


# ───────────────────────────── invio Telegram ───────────────────────────────

def invia_markdown(chat_id, md: str) -> dict:
    """Invia `md` al `chat_id` Telegram come **Rich Message** (Bot API 10.1,
    tabelle + heading nativi); su fallimento, fallback a `sendMessage` testuale
    (markdown grezzo, ancora leggibile). Condiviso da `invia_summary` (messaggio
    "Stato azienda") e da `development_summary.invia_development` (resoconto AI).

    Ritorna {'ok': bool, 'error': str|None}. Non solleva: i fallimenti sono
    loggati e riportati nel dict, così cron e route HTTP non crashano.
    """
    token = _telegram_token()
    if not token:
        logger.error("weekly_summary: TELEGRAM_BOT_TOKEN assente (.env di root)")
        return {"ok": False, "error": "missing_token"}

    # Primario: Rich Message (tabelle + heading nativi, Bot API 10.1).
    try:
        res = requests.post(
            f"{TELEGRAM_API}/bot{token}/sendRichMessage",
            json={"chat_id": chat_id, "rich_message": {"markdown": md}},
            timeout=20,
        )
        data = res.json() if res.content else {}
        if res.ok and data.get("ok"):
            return {"ok": True, "error": None}
        logger.warning("weekly_summary: sendRichMessage non-OK %s %s",
                       res.status_code, str(data)[:300])
    except requests.RequestException as e:
        logger.warning("weekly_summary: sendRichMessage failed %s", e)

    # Fallback: messaggio testuale semplice (markdown grezzo, ancora leggibile).
    try:
        res = requests.post(
            f"{TELEGRAM_API}/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": md, "disable_web_page_preview": True},
            timeout=15,
        )
        data = res.json() if res.content else {}
        if res.ok and data.get("ok"):
            logger.info("weekly_summary: inviato in fallback testuale (no rich)")
            return {"ok": True, "error": "fallback_plain"}
        logger.warning("weekly_summary: fallback sendMessage non-OK %s %s",
                       res.status_code, str(data)[:300])
        return {"ok": False, "error": f"telegram:{res.status_code}"}
    except requests.RequestException as e:
        logger.warning("weekly_summary: sendMessage failed %s", e)
        return {"ok": False, "error": f"request:{e}"}


def invia_summary(chat_id, *, now: datetime | None = None,
                  dev_repo: str = DEFAULT_DEV_REPO,
                  window_days: int = DEFAULT_WINDOW_DAYS,
                  monthly: bool = False) -> dict:
    """Costruisce e invia il messaggio "Stato azienda" al `chat_id` Telegram.
    `monthly=True` → variante mensile (resoconto del mese precedente).

    Ritorna {'ok': bool, 'error': str|None}. Non solleva: i fallimenti sono
    loggati e riportati nel dict, così cron e route HTTP non crashano.
    """
    try:
        md = build_summary_markdown(now=now, dev_repo=dev_repo,
                                    window_days=window_days, monthly=monthly)
    except Exception as e:  # build non deve mai propagare al chiamante
        logger.exception("weekly_summary: build fallita")
        return {"ok": False, "error": f"build:{e}"}
    return invia_markdown(chat_id, md)
