"""Wrapper per chiamare Claude dal daemon/Dashboard AFT.

⚠️ REGOLA: dai servizi Python di AFT **non si usa MAI `ANTHROPIC_API_KEY`**. Ogni
chiamata a Claude passa dal **gateway OneCLI** — lo stesso meccanismo della chat —
via `claude_gateway()` (`onecli run -- claude -p …`), senza alcuna chiave. La key
nel `.env` è volutamente vuota; non popolarla.

- `summarize_development` (comando Telegram /development) → gateway OneCLI.
- `summarize_scarti` (triage scarti SDI di `scarti_notify.py`, modalità delivery
  "discord") → ancora sull'SDK `anthropic` con chiave; di default però scarti usa
  delivery "agent" (path NanoClaw, niente chiave), quindi questo ramo è dormiente.
  TODO: portarlo anch'esso su `claude_gateway`.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile

logger = logging.getLogger("claude_client")

DEFAULT_MODEL = "claude-opus-4-8"
_MAX_TOKENS = 1500
# Timeout dell'invocazione gateway (connessione gateway + generazione Opus su input
# anche grande). Generoso: il chiamante mostra un ack prima di attendere.
_GATEWAY_TIMEOUT = 180


def is_configured() -> bool:
    """True se l'analisi Claude è disponibile (chiave presente)."""
    return bool(os.getenv("ANTHROPIC_API_KEY"))


# ─────────────────────── Claude via gateway OneCLI (no API key) ──────────────
# REGOLA: dai servizi Python di AFT non si usa MAI ANTHROPIC_API_KEY. Ogni
# chiamata a Claude passa dal **gateway OneCLI**, lo stesso meccanismo della chat:
# `onecli run -- claude -p "<prompt>" --model <model>` esegue il CLI `claude`
# headless con proxy + CLAUDE_CODE_OAUTH_TOKEN iniettati dal gateway. Niente chiavi
# nei .env.

def gateway_available() -> bool:
    """True se il CLI `onecli` (gateway) è invocabile su questa macchina."""
    return shutil.which("onecli") is not None


def claude_gateway(prompt: str, *, model: str | None = None,
                   timeout: int = _GATEWAY_TIMEOUT) -> str | None:
    """Invoca Claude headless via il gateway OneCLI (nessuna API key) e ritorna il
    testo della risposta, oppure None se il gateway non è disponibile o la chiamata
    fallisce. Esegue in una cwd neutra per non caricare CLAUDE.md/.mcp.json di
    progetto (one-shot pulito)."""
    if not gateway_available():
        logger.warning("onecli non trovato: salto la chiamata Claude via gateway")
        return None
    cmd = ["onecli", "run", "--", "claude", "-p", prompt,
           "--model", model or DEFAULT_MODEL]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            cwd=tempfile.gettempdir(),
        )
    except subprocess.TimeoutExpired:
        logger.warning("claude_gateway: timeout dopo %ss", timeout)
        return None
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("claude_gateway: non eseguibile (%s)", e)
        return None
    if proc.returncode != 0:
        logger.warning("claude_gateway: rc=%s err=%s",
                       proc.returncode, (proc.stderr or "").strip()[:300])
        return None
    out = (proc.stdout or "").strip()
    if not out:
        logger.warning("claude_gateway: output vuoto (stderr=%s)",
                       (proc.stderr or "").strip()[:200])
        return None
    return out


def summarize_scarti(*, contesto: str, dati: str, model: str | None = None,
                     extra_prompt: str = "") -> str | None:
    """Riassunto di triage in italiano degli scarti SDI.

    `contesto` = contesto di dominio (es. context.md), `dati` = batch raggruppato.
    Ritorna il testo, oppure `None` se l'LLM non è disponibile/fallisce."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY assente: salto l'analisi Claude (fallback plain-text)")
        return None
    try:
        import anthropic
    except ImportError:
        logger.warning("pacchetto 'anthropic' non installato: fallback plain-text")
        return None

    system = _build_system_prompt(contesto, extra_prompt)
    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=model or DEFAULT_MODEL,
            max_tokens=_MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": dati}],
        )
    except Exception:
        logger.exception("Chiamata Claude fallita: fallback plain-text")
        return None

    parts = [getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text"]
    text = "\n".join(p for p in parts if p).strip()
    return text or None


def summarize_development(*, periodo: str, prs_bundle: str,
                          model: str | None = None,
                          extra_prompt: str = "") -> str | None:
    """Resoconto in italiano, per pubblico aziendale NON tecnico, delle modifiche
    alla piattaforma (Pull Request integrate) nel `periodo` indicato.

    `prs_bundle` = elenco testuale delle PR (titolo + descrizione) preparato dal
    chiamante. Ritorna il testo (Rich Markdown Telegram: grassetto + elenchi),
    oppure `None` se l'LLM non è disponibile/fallisce (il chiamante fa fallback a
    un semplice elenco di titoli).

    Usa **Claude via gateway OneCLI** (stesso meccanismo della chat, nessuna API
    key): istruzioni e dati sono uniti in un unico prompt passato a `claude -p`."""
    system = _build_development_system_prompt(periodo, extra_prompt)
    prompt = (
        f"{system}\n\n"
        "--- Modifiche del periodo (Pull Request integrate: titolo + descrizione) ---\n\n"
        f"{prs_bundle}"
    )
    return claude_gateway(prompt, model=model)


def _build_development_system_prompt(periodo: str, extra_prompt: str) -> str:
    base = (
        "Sei il responsabile di prodotto di Autofatturiamo (servizio italiano di "
        "fatturazione elettronica FatturaPA/SDI). Ti arriva l'elenco delle modifiche "
        "tecniche (Pull Request integrate nel codice) fatte alla piattaforma nel "
        f"periodo «{periodo}», ciascuna con titolo e descrizione. Scrivi UN messaggio "
        "per il team aziendale — pubblico GENERALE e NON tecnico (founder, marketing, "
        "supporto clienti) — che racconti in modo chiaro e concreto COSA è stato fatto "
        "in questo periodo: nuove funzionalità, miglioramenti, problemi/bug risolti.\n\n"
        "Regole:\n"
        "- Pubblico non tecnico: vietato il gergo (niente nomi di file, funzioni, "
        "framework, librerie, numeri di PR, branch, commit, SQL). Traduci ogni "
        "modifica nel BENEFICIO concreto per l'azienda o per i clienti, non nel "
        "dettaglio implementativo.\n"
        "- Sii BREVE e sintetico: una frase di apertura che riassume il periodo, poi "
        "**al massimo 4-5 punti elenco in totale** (riga che inizia con '- '), senza "
        "titoletti di sezione. Accorpa aggressivamente le modifiche simili e tieni "
        "solo ciò che conta davvero per l'azienda o i clienti.\n"
        "- Ometti del tutto le modifiche puramente interne e irrilevanti per chi non è "
        "tecnico (refactor, aggiornamenti di dipendenze, sistemazioni di test, "
        "modifiche alla documentazione) a meno che non abbiano un impatto visibile per "
        "utenti o azienda.\n"
        "- Tono positivo, concreto e sobrio. Italiano corretto.\n"
        "- Formato Telegram Rich Markdown: SOLO grassetto **testo** ed elenchi puntati "
        "con '- '. NIENTE tabelle, NIENTE blocchi di codice, NIENTE titoli con '#'. "
        "Massimo ~900 caratteri.\n"
        "- Rispondi SOLO con il messaggio finale: niente preamboli, niente meta-commenti "
        "sul tuo processo, niente note sul fatto che alcune modifiche sono tecniche."
    )
    if extra_prompt:
        base += "\n\n--- Istruzioni aggiuntive dell'operatore ---\n" + extra_prompt
    return base


def _build_system_prompt(contesto: str, extra_prompt: str) -> str:
    base = (
        "Sei l'assistente operativo di Autofatturiamo (servizio italiano di "
        "fatturazione elettronica FatturaPA/SDI). Ti arriva un batch di notifiche "
        "di scarto SDI (autofatture rifiutate dallo SDI). Scrivi UN UNICO messaggio "
        "conciso in italiano per il team interno, che spieghi PERCHÉ sono avvenuti "
        "gli scarti e cosa fare.\n\n"
        "Regole:\n"
        "- Raggruppa per cliente e per tipo di errore.\n"
        "- Distingui nettamente gli scarti INNOCUI da quelli che richiedono AZIONE. "
        "Uno scarto è innocuo quando la stessa fattura risulta già confermata "
        "(campo 'risolto: sì' = è poi arrivata una ricevuta di consegna RC): caso "
        "tipico, 'fattura duplicata' (codice 00404) di un'autofattura già inviata e "
        "accettata → NESSUNA azione necessaria.\n"
        "- Gli scarti con 'risolto: no' sono ancora aperti: spiega l'errore e l'azione.\n"
        "- Chiudi con un verdetto operativo di una riga (es. «Nessuna azione: tutti "
        "duplicati di fatture già accettate» oppure «Da gestire: N fatture ancora aperte»).\n"
        "- Massimo ~1500 caratteri (va su una chat). Testo semplice, niente Markdown "
        "pesante. Rispondi SOLO con il messaggio finale: niente preamboli, niente "
        "ragionamento, niente meta-commenti sul tuo processo."
    )
    if contesto:
        base += "\n\n--- Contesto di dominio (riferimento) ---\n" + contesto[:6000]
    if extra_prompt:
        base += "\n\n--- Istruzioni aggiuntive dell'operatore ---\n" + extra_prompt
    return base
