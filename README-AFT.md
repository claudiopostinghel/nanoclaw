# AFT customization layer

This repo is a fork of **NanoClaw v2** customized for **Autofatturiamo (AFT)**, an Italian electronic-invoicing service. For the upstream platform (host, container, channels, entity model, etc.) see [`README.md`](README.md) and [`docs/`](docs/). **This file documents only what AFT adds or changes on top of the upstream.**

> Maintenance: every non-trivial change to AFT-specific code must be reflected here. Claude is instructed via a global memory (`feedback_aft_readme_maintenance`) to update this file as part of finishing any AFT task.

---

## Fork layout at a glance

| Path | Origin | Purpose |
|------|--------|---------|
| `src/` | NanoClaw | Host core: router, delivery, DB layer, channel infra, container runner. |
| `container/` | NanoClaw | Per-agent-group runtime (Bun) and container skills. |
| `groups/` | NanoClaw | Per-agent-group filesystem (CLAUDE.md, skills, overlay). |
| `scripts/` | NanoClaw | Host scripts and admin utilities. |
| `docs/` | NanoClaw | Upstream architecture and reference docs. |
| `setup/` | NanoClaw | First-install setup steps. |
| `bin/` | NanoClaw | CLI entry points (`ncl`, etc.). |
| **`services/autofatturiamo-sync/`** | **AFT** | Standalone Python daemon: Stripe + Odoo + IMAP → SQLite. Also hosts the operational-actions approval queue (§5). |
| **`services/bluedot-webhook/`** | **AFT** | Cloudflare Worker (TypeScript) — always-online webhook sink for BlueDot meeting events, backed by D1. The sync daemon pulls from it on a 5-minute cron. |
| **`launchd/`** | **AFT** | macOS LaunchAgent plist for the sync daemon. |
| **`logs/`** | **AFT** | Sync daemon stdout/stderr logs (gitignored). |
| `src/channels/telegram*.ts` | **AFT** | Telegram adapter, pairing flow, and Markdown sanitizer (lives inside the inherited tree but is AFT-only). |

---

## AFT additions

### 1. Autofatturiamo sync daemon — `services/autofatturiamo-sync/`

A standalone Python service that aggregates business data into a local SQLite database (`local.db`). It is **independent of NanoClaw** — no shared process, no shared DB, no IPC. NanoClaw can read `local.db` if a future skill needs it, but today the daemon stands on its own.

**Data sources → SQLite tables:**
- **Stripe** (`stripe_client.py`) → `stripe_clienti` (customers + tax IDs / P.IVA).
- **Odoo** (`odoo_client.py`, XML-RPC) → `odoo_clienti`, `odoo_opportunita` (CRM filtered by stage).
- **IMAP/PEC mailbox** (`imap_client.py`) → `email_messages`, `autofatture` (self-invoice XMLs extracted from ZIP attachments), `risposte_sdi` (Italian SDI delivery receipts / rejection notices), `zip_inviati`.
- **Authoritative clients (manually curated)** → `clienti` (master table: `id`, `nome`, `vat`, `odoo_cliente_id` FK nullable → `odoo_clienti.id`). Separato dalle viste derivate (`stripe_clienti` / `odoo_clienti`): qui i record sono creati esplicitamente da noi e fungono da àncora stabile per collegare entità interne (note, contatti WhatsApp, eventi, azioni) — la pagina autoritativa è `/clienti` (template `templates/clienti_master.html`, route `clienti_master_page` / `clienti_master_detail` in `app.py`). La vecchia vista unificata Stripe+Odoo+opportunità è rinominata `/clienti-fonti` (function `clienti_fonti_page`) e accessibile dal dropdown navbar "Pagine vecchie" come "Clienti (fonti unificate)". Schema + indici dichiarati come `SCHEMA_CLIENTI` / `SCHEMA_CLIENTI_INDEX_VAT` / `SCHEMA_CLIENTI_INDEX_ODOO`; nessun flusso di creazione/edit ancora — verrà aggiunto incrementalmente.

**Pipeline di vendita (`/pipeline`, kanban)** — board del funnel commerciale (route `pipeline_page`, template `templates/clienti_pipeline.html`). A differenza della versione precedente — dove la colonna era *calcolata* a ogni render dallo stage Odoo + Stripe — la colonna è ora una **proprietà POSSEDUTA dal cliente**: `clienti.pipeline_col` (colonna corrente) + `clienti.pipeline_auto` (1=posizione impostata da una regola, 0=trascinata a mano). Colonne in `PIPELINE_COLS` con la nuova **`cliente`** (account piattaforma attivo, senza Stripe) tra `onboarding` e `paganti`; rank del funnel in `_PIPELINE_RANK`.
- **Storico**: tabella `cliente_pipeline_log` (una riga per transizione reale: backfill iniziale, drag&drop manuale, regola auto — con `from_col`/`to_col`/`auto`/`rule_key`/`reason`/`created_at`), mostrata nella scheda cliente (`clienti_scheda.html`). Bookkeeping interno in `cliente_pipeline_rule_state` (`UNIQUE(cliente_id, rule_key)`) per non far ri-scattare una regola sulla stessa condizione permanente.
- **Drag&drop**: SortableJS via CDN nel template; `POST /pipeline/move/<cliente_id>` (`{to_col}`) aggiorna `pipeline_col` (pipeline_auto=0) e scrive una riga `manual` nel log.
- **Backfill**: `_backfill_pipeline_col(conn)` (in `init_db`, idempotente sulle righe `pipeline_col` NULL) fotografa "il posto dove sono adesso" via il calcolo legacy `_pipeline_col_for`.
- **Motore regole**: `PIPELINE_RULES` (fonte unica per il motore **e** per il modale "Visualizza regole") + `apply_pipeline_rules()`, chiamata in coda a `materializza_clienti()`. Semantica **event-based + forward-only**: ogni regola scatta una sola volta per cliente (al primo evento osservato) e sposta solo se il target è più avanti nel funnel. Regole iniziali: link Stripe → `paganti`; account piattaforma → `cliente`; stage Odoo "Demo schedulata" → `demo_schedulata`.
- **Badge freschezza + ordinamento** (`_pipeline_freshness` / `_pipeline_sorted_by_ref` in `_pipeline_clienti`): sulle colonne del funnel (tutte tranne `cliente`/`paganti`) ogni card mostra in alto a destra l'età dell'**ultimo contatto/evento** = max tra ultimo messaggio WhatsApp (`_whatsapp_conversations`, per `jid`) ed eventi a calendario linkati (`gcal_events` via `linked_messaging_group_id`, mappa `jid→mg_id` da `v2.db`). Evento futuro → "+N giorni" col check; passato → "N giorni"/"ieri"/"oggi". Ordinamento: colonne funnel per data di riferimento **crescente** (meno recente in cima); `cliente` per `auth_user.date_joined` (produzione.db) e `paganti` per `stripe_clienti.created`, entrambe **decrescente** (più recente in cima). La colonna `paganti` ha titolo "Pagante".

**Files:**

| File | Role |
|------|------|
| `sync_only.py` | **Headless daemon entrypoint** (what launchd runs). Initializes the DB, starts the scheduler, then sleeps forever handling `SIGTERM`/`SIGINT`. No Flask. |
| `app.py` | Optional Flask UI (port 5000): Jinja2 templates in Italian for client list, autofatture, SDI responses, email browser, BlueDot meetings, **WhatsApp conversations viewer** (`/whatsapp`, read-only across NanoClaw session DBs — see below); defines all DB schemas as module-level constants; no ORM (raw SQL). |
| `scheduler.py` | APScheduler config — runs the cron jobs (see below). |
| `import_produzione.py` | **Import automatico del dump di produzione.** Sorveglia `~/Downloads` per un dump `[ATF] Prod_postgres_YYYY_MM_DD_HH_MM.sql` (timestamp = istante dello snapshot, preso **dal nome**) più recente del marker `produzione.db.source`; se ne trova uno nuovo lancia `scripts/build_prod_db.py` come subprocess su un file temporaneo (`produzione.db.building`) e fa lo swap **atomico** (`os.replace`) → `produzione.db` — niente buco di servizio durante il rebuild di alcuni minuti, niente restart Flask (`prod_db()` apre RO per-request; `produzione.db` è in `/api/version` → auto-reload tab). Non cancella il file scaricato. Eseguibile a mano: `python import_produzione.py [--force]`. Sostituisce l'import manuale della skill `/ppp-import-produzione`. |
| `stripe_client.py` | Stripe customers + tax IDs; ships a `_normalize_vat()` helper. Also `get_stripe_billing_resources()` — full fetch of invoices/charges/customers/tax_rates with API version pinned to `2024-12-18.acacia` (`BILLING_API_VERSION`) for the FatturaPA generation fields (`total_tax_amounts`, `amount_excluding_tax`). |
| `fattura_xml.py` | FatturaPA v1.2 XML generation from Stripe data (port of `genera-xml.py` from `focolare-studio/tools/xml-visualizer-fatture`): `build_model()` / `build_xml()` / `generate_for_charges()` / `build_charge_rows()`. Cedente/trasmittente are explicit constants (`CEDENTE`, `ID_TRASMITTENTE` — Focolare Studio, CF 02763130222); amounts and VAT come straight from Stripe invoice fields, never recomputed. |
| `odoo_client.py` | Odoo XML-RPC wrapper — partners and CRM opportunities. |
| `imap_client.py` | IMAP fetcher; extracts SDI XMLs and ZIP-bundled autofatture from attachments. |
| `discord_client.py` | Discord notifier (e.g. for sync alerts). |
| `scarti_notify.py` | **(§5d)** Motore della notifica "denoised" + triage AI degli scarti SDI: debounce (finestra di silenzio) + cap, `sync_email()` forzato al fire, raggruppamento, riassunto Claude, invio su Discord operations, ledger `notified_events(kind='sdi_scarto')`. |
| `claude_client.py` | Wrapper per chiamare Claude. **`claude_gateway()`** (§9-bis) = via **gateway OneCLI**, nessuna API key (usato da `summarize_development`). **`summarize_scarti`** (§5d) usa ancora l'SDK Anthropic con `ANTHROPIC_API_KEY` ma solo in modalità `discord` (di default scarti usa il path agente, niente chiave). Regola: niente `ANTHROPIC_API_KEY` per nuove feature → usa `claude_gateway`. |
| `SCHEMA_APP_SETTINGS` + `get_setting`/`set_setting`/`get_scarti_notify_settings` + route `/impostazioni` in `app.py` | Tabella `app_settings(key,value)` (impostazioni UI condivise host↔daemon via `local.db`) + pagina **Impostazioni** (template `impostazioni.html`, navbar gear). In cima alla pagina un box-link rimanda a `/impostazioni/comunicazioni` (sotto). |
| `templates/comunicazioni.html` + route `/impostazioni/comunicazioni` (`impostazioni_comunicazioni`) in `app.py` | Pagina esplicativa **"Come il bot comunica con l'esterno"** (sola lettura, statica — nessun dato dinamico). Mappa operativa/visiva di tutti i canali di comunicazione esterna: in uscita (WhatsApp ai clienti con gate di approvazione, Telegram/Discord interni diretti, email non ancora attiva), diagramma del filtro WhatsApp + le 4 origini di un `whatsapp_reply`, in ingresso (conversazioni WhatsApp/Telegram/PEC + sync read-only Stripe/Odoo/Aruba/Calendar/BlueDot), tabella riepilogo "a noi vs ai clienti" e le due personalità agent (`dm-personal` / `autofatturiamo-clienti`). Raggiunta dal box-link in cima a `impostazioni.html`; navbar tiene attiva la voce "Impostazioni" via `match_prefix`. |
| `actions_db.py` | Operational actions queue — schema + accessor for `pending_actions` (see §6). |
| `action_executors/` | Python registry of executors for approved actions. One module per `action_type` + `__init__.py` registry. |
| `templates/` | Tailwind/Jinja2 dark-theme UI templates extending `base.html` (includes `actions.html` for the approval queue). |
| `static/` | Static assets served by Flask under `/static/` (e.g. `favicon.png` — 128×128 PNG referenced from `base.html` via `url_for('static', ...)` as both `rel="icon"` and `rel="apple-touch-icon"`). |
| `static/js/` | Reusable ES modules (dark-theme restyling of the `fattura-core.js` client core): `xml-view.js` (XML pretty-print + syntax highlight + parse-error box), `fattura-doc.js` (FatturaPA DOMParser `parseFattura()` + `renderDoc()` document view + decode tables TD/MP/RF/NATURA), `fattura-viewer.js` (`createFatturaViewer()` — combines both views with the Fattura/XML toggle; mounts on the `_fattura_viewer.html` partial). |
| `requirements.txt` | Python deps (Flask, APScheduler, Stripe, `markdown`, etc.). |
| `start.sh` | Convenience launch script for local dev. |
| `context-files/` | **Directory wrapper** dei file di contesto (4 markdown + 2 backup rolling). Mountata RO nei container degli agent group **`dm-personal`** (a `/workspace/extra/autofatturiamo/context-files/`) e **`autofatturiamo-clienti`** (stesso path). **Mount di dir, non di file singolo**: i bind di singolo file su Docker Desktop macOS si agganciano all'inode iniziale e ignorano gli atomic-write (rename → swap inode) di editor/tool/dashboard — i bind di dir invece gestiscono correttamente il refresh degli inode dei file contenuti, così gli edit live restano visibili al prossimo turno senza restart. |
| `context-files/context.md` | **Vista interna** del dominio. Sorgente delle card strutturate su `/contesto` (linkato dalla navbar). Ogni sezione `## ...` è parsata da `_parse_context_md()` in `app.py` e renderizzata con un layout che matcha il tipo di contenuto — mappatura slug → layout in `CONTEXT_SECTION_REGISTRY_INTERNO` (definition / rule_with_examples / cases / stepper / comparison). Le sezioni con uno slug sconosciuto cadono nel layout generic. Edit auto-reload (è in `_sources_mtime`). Importato in `groups/dm-personal/CLAUDE.local.md` via `@../extra/autofatturiamo/context-files/context.md`. Editabile da dashboard a `/contesto/edit?audience=interno` (default tab) — il save crea backup rolling `context.md.bak` nella stessa dir. **Contiene anche dettagli operativi interni (PEC/SDI, Odoo+Stripe) — non viene esposto ai clienti.** |
| `context-files/context_overview.md` | Narrativa interna corrispondente, renderizzata in cima a `/contesto`. Read da `_render_context_overview()`, convertita in HTML, inserita in `contesto.html` come singolo `<section>`. Hot-reload via `_sources_mtime`. |
| `context-files/context-client.md` | **Vista cliente** del dominio — sottoinsieme safe di `context.md` (cos'è Autofatturiamo, scadenza normativa, modifiche/cancellazioni, sito web e demo). **NON contiene riferimenti a PEC, SDI come canale, Odoo, Stripe, DB interno** — sono dettagli che il cliente non deve vedere. Importato in `groups/autofatturiamo-clienti/CLAUDE.local.md` via `@../extra/autofatturiamo/context-files/context-client.md`. Editabile da dashboard a `/contesto/edit?audience=clienti` (tab "Cliente") — backup `context-client.md.bak`. Layout registry: `CONTEXT_SECTION_REGISTRY_CLIENTI` in `app.py`. |
| `context-files/context_overview_client.md` | Narrativa cliente corrispondente. Stessa logica di `context_overview.md` ma per la vista client-facing. |
| `sync-config.json` | Sync configuration file. |
| `local.db` | SQLite database (gitignored). **`journal_mode=DELETE`, non WAL** — load-bearing: `local.db` è bind-montato **read-only** nel container `dm-personal` (`/workspace/extra/autofatturiamo/local.db`, vedi `groups/dm-personal/container.json`) e il MCP tool `get_revenue_overview` lo legge da lì. Un reader read-only cross-mount NON vede i file `-wal`/`-shm` → in WAL leggerebbe solo il main file stantio (il bot vede 0 righe). Tutti i path di connessione devono restare in DELETE: `db()` in `app.py`, più `_db()` in `gcal_client.py` e `bluedot_client.py` (stesso file `local.db`). Stessa gotcha di `actions.db` e dei session DB. |
| `actions/actions.db` | Operational actions queue SQLite (gitignored, created at daemon boot). |

**Scheduled jobs** (configurable via `sync-config.json`, hot-reloaded every 60s):

| Job | Default cadence | Config key | Function |
|-----|------------------|------------|----------|
| `sync_all` | Hourly at :00 | `sync_all_cron` | `sync_stripe()` + `sync_odoo_opportunita()` + `sync_odoo()` + `sync_fatture()` + `sync_subscriptions()` + `sync_upcoming_invoices()` (Stripe/Odoo/charges/subscriptions/upcoming tables are dropped & recreated on each run). |
| `sync_pec` | Hourly at :00 | `sync_pec_cron` | `sync_email()` — incremental IMAP fetch. |
| `sync_bluedot` | Every minute | `sync_bluedot_cron` | `sync_bluedot()` — pulls BlueDot meeting summary/transcript events from the `bluedot-webhook` Cloudflare Worker (D1) into `bluedot_events`. Implementation in `bluedot_client.py`; Worker source in `services/bluedot-webhook/` (see its README for deploy + secret rotation). |
| `sync_gcal` | Every 15 minutes | `sync_gcal_cron` | `sync_gcal()` — fetches the private Google Calendar iCal feed at `GCAL_ICAL_URL`, parses VEVENTs with the `icalendar` library, and refreshes the `gcal_events` table (DELETE + INSERT in transaction). Recurring events (with `RRULE`) are skipped for now. Implementation in `gcal_client.py`. Surfaced in the dashboard at `/calendar` (page shows events with `dtstart >= today-30d`, including future ones; cancelled events appear with strike-through). Per gli eventi cal.com il sync valorizza anche le colonne `cal_event_type`, `cal_prospect_name`, `cal_prospect_phone`, `cal_prospect_email`, `cal_prospect_timezone`, `cal_booking_id`, `cal_reschedule_url`, `cal_attendees_external` — parsing in `calcom_parser.py` (supporta description IT/EN e varianti HTML). La distinzione data evento vs data creazione è `dtstart` vs `created` (entrambi popolati dal feed iCal). **Bridge cliente**: ogni evento cal.com con `cal_prospect_phone` viene anche linkato (read-only su `data/v2.db`, `messaging_groups`) alla chat WhatsApp corrispondente — colonna `linked_messaging_group_id_auto`, helper in `customer_link.py` (`normalize_phone`, `find_whatsapp_messaging_group`). Override manuale futuro: colonna `linked_messaging_group_id_manual`, mai sovrascritta dal sync (preservata via pre-fetch in `sync_gcal`); l'identificatore "effettivo" è `COALESCE(..._manual, ..._auto)`. La pagina `/whatsapp/<jid>` mostra una sezione "Meeting cal.com" con tutti i meeting associati al cliente (`_meetings_for_messaging_group_id` in `app.py`). |
| `scarti_notify` | Every 5 min | `scarti_notify_cron` | `scarti_notify.process()` — notifica "denoised" + triage AI delle notifiche di scarto SDI (vedi §5d). Tick leggero (legge il DB); quando un'ondata di scarti si è assestata forza un `sync_email()`, raggruppa, chiede a Claude un riassunto di triage e invia **un** messaggio su Discord operations. |
| `summary_weekly` | Thursday 09:00 | `summary_weekly_cron` | `weekly_summary.invia_summary()` — messaggio deterministico **"Stato azienda"** su Telegram (vedi §9). Sostituisce il vecchio task NanoClaw agent-driven del giovedì. Chat/repo/finestra da `summary_chat_id` / `summary_dev_repo` / `summary_window_days` in `sync-config.json` (lette via `app._summary_config()`). |
| `summary_monthly` | 1° del mese 09:00 | `summary_monthly_cron` | `weekly_summary.invia_summary(monthly=True)` — stesso messaggio in versione **mensile**: copre tutto il mese solare precedente; Economics = proiezione di quel mese dallo snapshot `ricavi_mensili` (vedi §9). Stessa chat/repo del settimanale. |
| `import_produzione` | Every 10 min | `import_produzione_cron` | `import_produzione.import_latest()` — controlla `~/Downloads` per un dump `[ATF] Prod_postgres_*.sql` più recente del marker e ricostruisce `produzione.db` (vedi `import_produzione.py`). Tick leggerissimo quando non c'è nulla di nuovo (scan dir + confronto marker); rebuild via subprocess + swap atomico solo quando compare un dump nuovo. `max_instances=1` + `coalesce`: un rebuild che dura più di 10 min non si sovrappone al tick successivo. |

**Fallimenti sync = solo log, nessuna notifica.** I job di `scheduler.py` catturano le eccezioni e le scrivono solo con `logger.exception` — **non** mandano più alert Discord (`⚠ Sync … fallito` rimossi su richiesta utente: "me ne accorgo se succede"). Il segnale di un sync fermo è **visivo** nel widget "Ultima sincronizzazione" della sidebar (`_navbar.html`): ogni fonte la cui ultima esecuzione è più vecchia di 24h (o mai avvenuta) viene mostrata in **rosso + bold con icona `fa-triangle-exclamation`**; se almeno una è stale, l'icona del rail collassato diventa rossa con un pallino di allerta. Soglia nel filtro Jinja `is_stale` (`app.py`, default 24h, riusabile altrove). Le fonti sono in `sync_keys` (`_navbar.html`) e nel context processor `inject_last_sync` (`app.py`): oltre alle sync live (Stripe/Odoo/Email/BlueDot/Calendar/Fatture/Pagamenti) c'è **Piattaforma** (`produzione`), la cui "ultima sincronizzazione" NON è l'ora di import ma il **timestamp dello snapshot del dump** (dal nome `[ATF] Prod_postgres_*.sql`, ISO naive locale, scritto da `import_produzione._record_last_sync`) — così la voce mostra l'età reale dei dati di produzione e diventa rossa se non si scarica un dump nuovo da oltre 24h.

**Required environment variables** (`.env` in `services/autofatturiamo-sync/`): `STRIPE_SECRET_KEY`; `ODOO_URL`, `ODOO_DB`, `ODOO_USERNAME`, `ODOO_PASSWORD`; `IMAP_HOST`, `IMAP_PORT`, `USERNAME`, `PASSWORD`, `MAILBOX`; for the operational-actions framework (§5) also `DISCORD_WEBHOOK_OPERATIONS`, `DISCORD_WEBHOOK_NOTIFICATIONS`; for BlueDot ingestion `BLUEDOT_API_URL`, `BLUEDOT_SYNC_TOKEN`; for Google Calendar ingestion `GCAL_ICAL_URL` (private feed URL with embedded token, never log or commit); for the SDI rejection notification AI triage (§5d) `ANTHROPIC_API_KEY` (optional — if empty/absent, the notification still fires with a plain-text fallback instead of the Claude summary).

**Domain concepts** (Italian invoicing): autofatture (self-invoices), risposte SDI (RC = delivery receipt, NS = rejection), autofattura stato (`scartata` / `in_attesa` / `confermata` derived from RC/NS counts). See `services/autofatturiamo-sync/CLAUDE.md` for the full domain glossary.

**Controllo Autofatture — vista per MESE DI COMPETENZA** (`_get_controllo_competenza(month)` in `app.py`) — sia `/controllo-autofatture` sia la tile + heatmap della home `/` partono dalle **prenotazioni** della piattaforma (`produzione.db`, `reservations_reservation`), raggruppate per mese di competenza (`SUBSTR(last_email_date,1,7)`), NON dalle autofatture inviate. Per ogni prenotazione si verifica se esiste un'autofattura collegata (codice di conferma nell'XML, via `_all_autofatture_by_piva` + `_bucket`, riusati da `/prenotazioni-clienti`) e si aggrega per cliente in tre contatori: **mancanti** (rosso, `todo` = nessuna autofattura → da generare), **in attesa** (ambra, `invio` = inviata senza RC), **confermate** (verde, `ok` = RC ricevuta, o cancellata WOP gestita). Così emergono anche i clienti **senza alcuna riga** nella tabella `autofatture` — il caso che la vecchia vista (ancorata al mese d'invio `email_date_iso`) non vedeva. Default month = **mese precedente** (`_competenza_default_month()`): è quello con scadenza d'invio già maturata (autofatture di competenza M dovute entro il 15 di M+1); il mese corrente sarebbe quasi tutto "mancante" e nasconderebbe il segnale. Richiede `produzione.db` importato (`/ppp-import-produzione`): se assente, `db_ready=False` e la pagina mostra un messaggio d'import senza errori. La pagina archivio `/autofatture` resta invece sul vecchio modello (mese d'invio + stato SDI confermata/in attesa/scartata via `_get_autofatture_by_month` + `_autofattura_urgency(month)`).

**Pagina Issued da allineare (`/issued-da-allineare`) + tile dashboard** — chiude il loop tra il nostro stato PEC/SDI e lo stato della piattaforma. La piattaforma tiene una prenotazione in `status='TE'` («to emit») finché un operatore non la marca `IS` («Issued»); ma per molte di queste l'autofattura è già stata inviata **e confermata** (RC ricevuta) — andrebbero quindi messe in Issued. La piattaforma ha una **funzione staff globale** (`piattaforma/backend/console/views/page_issue_reservations_by_date.py`, `StaffRequiredMixin`) che accetta un testo con una riga per prenotazione nel formato `CODICE;GG-MM-AAAA` (la data è solo informativa; le cancellate non sono trasformabili) e fa match per solo `confirmation_code` (globale, codici Airbnb univoci). Helper `_issued_da_allineare()` in `app.py`: incrocia le prenotazioni `TE` di `produzione.db` con la mappa autofatture appiattita di `_all_autofatture_by_piva()` (esteso con `rc_date` = data della risposta positiva PEC, RC più antica per `numero_fattura`, fallback alla data d'invio), tiene solo quelle con `ricevuta=True`, e produce il **blocco di testo** `CODICE;GG-MM-AAAA` (codice = `confirmation_code` raw; data via `_iso_to_ggmmaaaa()`) pronto da incollare, più `items`/`by_customer` per la vista a tabella. Route `GET /issued-da-allineare` → template `templates/issued_da_allineare.html` (textarea readonly col blocco + bottone **Copia** a copia sincrona via clipboard API, e tabella raggruppata per cliente con link `/autofattura/<af_id>`). La dashboard `/` mostra il tile `issued_allineare_tile` (in `_dashboard_tiles.html`) **sotto** `autofatture_tile` nello step «15 del mese»: blocco verde compatto se è tutto allineato, altrimenti card ambra con conteggio + bottone Copia (dal `<textarea>` nascosto) + «Apri elenco» (modale). La pagina **non scrive** sulla piattaforma: produce solo il testo da incollare nella funzione staff. Voce navbar `/issued-da-allineare` («Issued da allineare», `fa-clipboard-check`) dopo «Prenotazioni». Richiede `produzione.db` importato (`/ppp-import-produzione`): se assente, `db_ready=False` e il tile non rende nulla.

**Scarti SDI aperti (`/scarti`)** — vista focalizzata **cross-mese** delle autofatture (XML inviati) **scartate e mai confermate**: hanno ≥1 notifica di scarto (`risposte_SDI.tipo='NS'`) e **nessuna** ricevuta di consegna (`tipo='RC'`), match per `numero_fattura` estratto dal filename — stessa definizione dello stato `scartata` di `_get_autofatture_by_month`, ma piatta e senza raggruppamento per mese/cliente. Helper `_get_scarti_aperti()` in `app.py`: query CTE (`af`/`rc`/`ns`) → righe con `count_ns` e `last_ns` (data ultimo scarto, ordinamento desc), poi per ogni riga estrae il **codice di conferma** Airbnb dall'XML (`_RE_CODICE_CONFERMA`) e, se `produzione.db` è importato, arricchisce con la **prenotazione** collegata (`reservations_reservation` per `confirmation_code`: ospite, date, stato). Template `templates/scarti.html` (tabella cliente/numero/prenotazione/scarti/ultimo scarto + ricerca; stato vuoto esplicativo). Sotto la tabella principale, una **tabella secondaria di log** mostra gli scarti ricevuti **nelle ultime 24 ore** e **a prescindere dall'esito** (helper `_get_scarti_recenti(24)`; filtro su `email_date_iso >= now-24h`, wall-clock ora di Roma). È **raggruppata per (cliente, tipologia di problema)** (`_raggruppa_scarti_per_cliente_problema()`, keyed su `(customer_name, codice)`), ma la UI raggruppa a due livelli: il **nome cliente è un heading fuori dalle card** (Jinja `groupby('customer_name')`), e sotto una card per motivo. Ogni card ha a **sinistra la descrizione del problema** (grande, bianca — niente più badge codice, basta la descrizione) e a **destra i chip delle prime 5 autofatture** (al posto del conteggio): ogni chip è il numero fattura linkato all'autofattura (`/autofattura/<af_id>`, fallback scarto XML), rosso se ancora aperto / neutro se poi confermato; il resto dietro un chip `+N` espandibile (`<details class="scarto-extra">`, toggle CSS via `<style>`, senza dipendere da varianti Tailwind del CDN). Se ci sono scarti non ancora confermati la card mostra «N ancora aperte» in rosso. Più scarti dello stesso cliente con lo stesso codice (es. `00404` «Fattura duplicata …») collassano in un unico blocco; ordinati per cliente, poi frequenza desc. La **descrizione** è estratta dall'XML di scarto (`<ListaErrori><Errore><Codice>+<Descrizione>`, via `_errori_scarto()` + regex `_RE_ERRORE_*`). In dashboard `/` c'è la **tile** `scarti_tile` (in `_dashboard_tiles.html`, stile `pagamenti_errori_tile`: compatta col check verde quando 0, card rossa con le righe quando >0) tra il blocco timeline e la pipeline. Tab "Scarti SDI" in `_navbar.html` dopo "Invio Fatture", con **badge rosso** che conta gli scarti aperti (context processor `inject_scarti_count` → `_count_scarti_aperti()`, query leggera senza XML/prenotazioni). **Nota dati**: oggi tutte le NS in `local.db` hanno una RC sullo stesso numero → la pagina è vuota e funge da **monitor** per scarti futuri/non risolti.

**Pagina Fatture (`/fatture`)** — porting della prima parte della pagina "Nuove fatture" del tool `focolare-studio/tools/xml-visualizer-fatture` (senza la sezione "Confronto fatture"), in stile dashboard. Master-detail: a sinistra la tabella delle charges Stripe del mese selezionato (progressivo SDI, data, cliente con tooltip anagrafico, Codice SD, descrizione + metodo pagamento, importo + pallino stato), a destra il pannello sticky "Fattura generata da noi" con toggle **Fattura/XML** (componente `fattura-viewer.js` + partial `_fattura_viewer.html`); navigazione con click o frecce ↑↓. Dati: `sync_fatture()` (in `app.py`) scarica le risorse Stripe via `get_stripe_billing_resources()` e rigenera in full-refresh le tabelle `stripe_charges` (una riga per charge, con invoice/customer abbinati per id) e `fatture_generate` (XML FatturaPA per charge via `fattura_xml.generate_for_charges()`, con well-formedness check server-side in `xml_error`). Route: `GET /fatture?month=YYYY-MM` (filtro mese server-side, mesi da `created_iso` in tz Europe/Rome), `GET /api/fatture/generata?month=` (dict `charge_id → {xml, generabile, note}` per il pannello dettaglio), `GET /fatture/download-zip?month=` (ZIP delle transazioni "verdi" — succeeded + generabile + XML valido — con nomenclatura SDI `IT02763130222_<progressivo 5 alfanumerici dal numero fattura>.xml`), `POST /sync-fatture`. Il sync gira anche nel job `sync_all` e nel `POST /sync` globale; ultima esecuzione in `sync_state.fatture_last_sync` (riga "Fatture" nel widget sidebar). Tab "Fatture" in `_navbar.html` dopo "Clienti". L'upload allo SDI resta manuale (portale Fatture e Corrispettivi) — la pagina produce lo ZIP pronto.

**Pagina Pagamenti (`/pagamenti`)** — vista degli abbonamenti Stripe che evidenzia i pagamenti falliti/insoluti. Dati: `sync_subscriptions()` (in `app.py`) scarica **tutte** le subscription via `get_stripe_subscriptions()` (in `stripe_client.py`, client scoped su `BILLING_API_VERSION`, `status="all"`, `expand=[data.customer, data.latest_invoice]`) e rigenera in full-refresh la tabella `stripe_subscriptions` (una riga per abbonamento: stato, importo/intervallo piano, prossimo rinnovo, dati dell'ultima fattura, `is_error`). La P.IVA è riempita via lookup su `stripe_clienti` per `customer_id`. Classificazione errore in `_subscription_is_error()`: stato subscription in `{past_due, unpaid, incomplete, incomplete_expired}` **oppure** ultima fattura `open`/`uncollectible` con `amount_due > 0` e `attempt_count ≥ 1` (rinnovo fallito — intercetta anche le subscription `canceled` da Stripe dopo aver esaurito i retry ma con fattura ancora aperta). La route `pagamenti_page` incrocia `stripe_charges` (`status='failed'` del mese corrente) per mostrare data + motivo dell'ultimo pagamento fallito (`_DECLINE_IT` traduce i codici di decline). Layout: card riepilogo (Totali / In errore / Attivi), sezione "Pagamenti in errore" in cima con link a fattura e cliente su Stripe, poi tabella di tutti gli abbonamenti (errori first, canceled de-enfatizzati). Route: `GET /pagamenti`, `POST /sync-subscriptions`. Il sync gira anche nel job `sync_all` e nel `POST /sync` globale; ultima esecuzione in `sync_state.subscriptions_last_sync` (riga "Pagamenti" nel widget sidebar). Tab "Pagamenti" in `_navbar.html` tra "WhatsApp" e "Azioni", con **badge rosso** che conta gli abbonamenti in errore (context processor `inject_subscriptions_error_count`; la macro `nav_item` accetta `badge_kind='red'`).

**Pagina Ricavi (`/ricavi`) + MCP tool `get_revenue_overview`** — risponde alla domanda "quanto stiamo guadagnando questo mese". **Perché esiste**: il billing AFT è a **consumo posticipato** (gli eventi di prenotazione si addebitano a fine mese). Il ricavo del mese in corso NON è l'ultima fattura emessa (= consumo del mese *precedente*, ciò che si vede addebitato "il 1°") né `plan_amount` (simbolico, ~€0.99/credito): vive solo nelle **upcoming invoices**, che prima non venivano sincronizzate. Dati: `sync_upcoming_invoices()` (in `app.py`) legge le subscription `active`/`trialing` da `stripe_subscriptions` e per ognuna recupera l'anteprima della fattura in maturazione via `get_stripe_upcoming_invoices()` (in `stripe_client.py`), rigenerando in full-refresh la tabella **`stripe_upcoming_invoices`** (una riga per sub: `subtotal`/`tax`/`total`/`amount_due` in centesimi, `period_start/end`, `next_payment_attempt`, `lines_json`, `error`). ⚠️ **Gotcha Stripe**: con l'SDK pinnato (`BILLING_API_VERSION = 2024-12-18.acacia`) l'endpoint legacy `/v1/invoices/upcoming` **fallisce** sulle subscription `billing_mode=flexible` (quelle AFT) → si usa **`Invoice.create_preview`**. Il **ricavo netto** è calcolato come `total - tax` (post-sconto, pre-IVA): NON usare `subtotal`, che è al lordo degli sconti e sovrastima (un cliente scontato può avere `subtotal > total`). Aggregazione in `_ricavi_overview()`: maturato-a-oggi (somma anteprime), proiezione fine periodo (proratizzata per-sub sul proprio periodo di fatturazione, gestisce periodi non allineati al mese solare), e "ultimo ciclo addebitato" (charges `succeeded` del mese solare più recente). Route: `GET /ricavi`, `POST /sync-upcoming`; ultima esecuzione in `sync_state.upcoming_last_sync` (riga "upcoming" nel widget sidebar). Tab "Ricavi" in `_navbar.html` dopo "Pagamenti". **MCP tool** `get_revenue_overview` (`container/agent-runner/src/mcp-tools/revenue.ts`): legge `local.db` in **read-only** via `bun:sqlite` dal mount `/workspace/extra/autofatturiamo/local.db` (stessa logica di `_ricavi_overview`), così l'agente interno risponde senza scrivere SQL a mano. Senza argomenti → mese in corso; `month=YYYY-MM` → totale già fatturato di un mese passato; `by_customer=true` → dettaglio per cliente. **Solo interno**: il `local.db` non è montato nel container clienti, il modulo è in `excludedModules` di `autofatturiamo-clienti/container.json`, e la registrazione è self-gated su `fs.existsSync`. Aggiunto a `CONDITIONAL_MODULES` in `mcp-tools/index.ts`.

**Pagina Referral (`/referrals`)** — elenca i meeting demo in cui l'invitato ha indicato un **codice referral**. La fonte è il campo custom cal.com **«Come ci hai conosciuto?»** (`gcal_events.description`, sincronizzato dal calendario): estratto a render-time da `calcom_parser.extract_referral_field()` (riusa `_html_to_plain` + match header IT/EN, scarta `undefined`/vuoto). Quel campo è **sovraccarico** — contiene anche fonti generiche (`gruppo facebook`, `lattanzio`, `Gruppo oltre i cento - …`) — quindi `_is_referral_code()` (in `app.py`) tiene **solo** i veri codici via euristica di formato: regex `_REFERRAL_CODE_RE` = alfanumerico con ≥1 trattino e **senza spazi** (es. `ANDREA-ERER`), lunghezza 3–40. Helper `_referral_meetings()`: legge i booking cal.com da `gcal_events`, filtra i codici, **dedup per (prospect, codice)** (identità email→telefono→nome→uid, tiene il meeting più recente) e risolve il **cliente master collegato** via `cliente_links` (`source='meeting'`, `ext_id='m:<key>'` con le stesse chiavi di `materializza_clienti`, salta archiviati); se non c'è link la riga resta "non ricondotta" (mostra solo il meeting). Nessuna colonna/sync dedicata — parsing a ogni request come `/calendar`. Route `GET /referrals` (`referrals_page`), template `templates/referrals.html` (tabella Data / Cliente·Meeting / Contatto / Codice referral + ricerca client-side). Tab "Referral" in `_navbar.html` dopo "Ricavi" (`fa-user-group`). *(Concetto distinto dal `referral` nei metadata Stripe del cliente — quello resta mostrato nella scheda cliente.)*

**WhatsApp conversations viewer (`/whatsapp`)** — read-only WhatsApp Web–style page (sidebar chat list + message pane) that reads the agent's WhatsApp conversations live from the **NanoClaw** session DBs. This is the first Flask route that crosses the boundary into NanoClaw's data; everything else in the dashboard reads only from `local.db`. Helpers in `app.py` (`_ro_sqlite()`, `_whatsapp_conversations()`, `_whatsapp_messages()`) open `data/v2.db` to enumerate `messaging_groups WHERE channel_type='whatsapp'` and their `sessions`, then for each session open `data/v2-sessions/<agent_group>/<session>/inbound.db` (host-written) and `outbound.db` (container-written) with `?mode=ro` to read `messages_in` / `messages_out`. All connections are short-lived and read-only — no lock contention with the NanoClaw writers. Inbound timestamps are ISO8601 UTC, outbound timestamps are naive local; `_parse_ts()` normalizes both to `Europe/Rome` for display. The page filters out conversations with no messages and shows a green "from agent" bubble vs. grey "from user" bubble. Template: `templates/whatsapp.html` (search autofocus + JS embed-fetch pattern cloned from `clients.html`). Navbar link in `_navbar.html` next to "Meeting Bluedot".

**Task pianificati viewer/editor (`/tasks`)** — vista umana sui task schedulati dal bot. I task NanoClaw sono righe `kind='task'` in `messages_in` dentro l'`inbound.db` di ogni sessione (`data/v2-sessions/<agent_group>/<session>/inbound.db`); finora gestibili solo dall'agente via tool MCP (`list_tasks`/`update_task`/…). La pagina li elenca in due sezioni — **ricorrenti** (`recurrence` valorizzato) in cima, **one-time** (`recurrence` NULL) sotto — e cliccando un task apre un **modale** per modificarne **prompt**, **prossima esecuzione** e **cron**, più **pausa/riprendi/annulla**. Logica isolata in **`services/autofatturiamo-sync/scheduled_tasks_db.py`** (non in `app.py`): `list_all_tasks()` enumera le sessioni da `data/v2.db` e legge ogni `inbound.db` in `?mode=ro` con la query canonica di `list_tasks` (`GROUP BY series_id`, una riga viva per serie); le mutazioni replicano `src/modules/scheduling/db.ts` (`updateTask`/`pauseTask`/`resumeTask`/`cancelTask`), matchando `(id=? OR series_id=?)`. Route in `app.py`: `GET /tasks` (`tasks_page`) + `POST /tasks/<session_id>/<task_id>/{update,pause,resume,cancel}` (l'`agent_group_id` è risolto server-side da `v2.db`, mai dal client → no path-traversal). ⚠️ **È la prima route che SCRIVE nei session DB di NanoClaw** (oltre il read-only del viewer WhatsApp): aggira l'helper `db()` (che forza WAL) e apre con una connessione dedicata in **`journal_mode=DELETE`** + `busy_timeout` — WAL romperebbe la visibilità cross-mount per il container (vedi `container/agent-runner/src/db/connection.ts`). Secondo writer accettato accanto all'host: SQLite serializza con il file-lock, le UPDATE non allocano `seq`. Fuso: `process_after` è UTC ISO (prossimo run, già pronto anche per i ricorrenti) → niente parser cron lato Python; conversione UTC↔`Europe/Rome` per display e per l'`<input datetime-local>`. Voce navbar `/tasks` ("Task pianificati", `fa-clock-rotate-left`) in `_navbar.html` dopo "Azioni".

**"Contesto caricato" debug pane.** Sopra la lista messaggi di ogni chat WhatsApp c'è un `<details>` collassabile ("Contesto caricato (N file)") che elenca tutti i file che NanoClaw mette nel prompt dell'agente per quella conversazione, con contenuto integrale espandibile inline. Helper: `_loaded_context_for_jid(jid)` in `app.py` — apre `data/v2.db` in `mode=ro`, risolve `jid → messaging_group_id → agent_group_id → folder` via JOIN su `messaging_groups`/`messaging_group_agents`/`agent_groups`, poi raccoglie nell'ordine: (1) host base `container/CLAUDE.md`, (2) group entry `groups/<folder>/CLAUDE.md`, (3) tutti i fragments `.claude-fragments/*.md` alfabetici (i symlink puntano a `/app/...` lato container e vengono rimappati a `container/agent-runner/` o `container/` via `_resolve_app_symlink()`), (4) `CLAUDE.local.md`, (5) `@import` risolti parsando le righe `^@(\S+)` di `CLAUDE.local.md` e rimappando il path `/workspace/extra/<containerPath>/...` sul `hostPath` del mount corrispondente in `container.json` (`_resolve_claude_local_import()`), (6) scheda cliente `customer-context/<mg_id>.md` se esiste, (7) eventi `customer-events/<mg_id>/*.md` alfabetici. Niente cache (~14 file <30 KB totali per request), niente JS (solo `<details>` nativi). Read-only end-to-end — non scrive mai sui file del gruppo né sul DB NanoClaw.

**Bolla outbound unificata.** Ogni messaggio uscente derivato da un'azione `whatsapp_reply` (pending/approved/done/rejected/failed) viene renderizzato come **un'unica bolla** che combina testo del messaggio + metadati di azione (badge stato, `#id`, eventuale reason/errore, bottoni Approva/Rifiuta solo se `pending`). Non c'è più una "card azione" separata accanto al messaggio reale: la stessa base estetica della bolla emerald viene declinata con modificatori sottili (bordo amber per `pending`, bordo rosso per `failed`, tinta slate per `rejected`, opacità ridotta per gli stati intermedi). Per ottenere questo, `_whatsapp_messages()` chiama `actions_db.list_for_whatsapp_jid(jid, include_delivered=True)` per caricare anche le azioni `done` già consegnate (che la query default esclude) e poi **de-duplica** i messaggi `messages_in`/`messages_out` outbound contro l'indice delle azioni delivered (match per testo identico + `host_delivered_at` entro ±90s dal timestamp del messaggio). Le azioni `done` vengono posizionate cronologicamente sul loro `host_delivered_at`, non sul `created_at` dell'agent.

**Date in italiano (filtri Jinja centralizzati).** Tutte le date user-facing della dashboard passano per un set di helper IT definiti in `app.py` accanto a `timeago` (`_GIORNI_IT` / `_MESI_IT_FULL` / `_MESI_IT_ABBR` + helper `_it_day_badge`, `_it_date`, `_it_datetime`, `_it_time`, `_it_last_seen`). L'implementazione è un mapping esplicito — **niente `locale.setlocale(LC_TIME, 'it_IT')`**, che dipende dalla locale di sistema (può non essere disponibile su tutti gli host). Filtri Jinja esposti: `it_day_badge` (`lunedì 20 maggio`, no anno — badge giorno chat WhatsApp), `it_date` (`20 mag 2026`), `it_dt` (`20 mag 2026, 14:30`), `it_time` (`14:30`), `it_last_seen` (sidebar WhatsApp: oggi → ora, ieri → `ieri`, entro 7gg → `lun`/`mar`, oltre → `20/05`), `iso_to_it_dt` (parsa stringa ISO 8601 con/senza `Z` → italiano). Il filtro legacy `unix_datetime` ora produce italiano (`_it_datetime`) invece di ISO. La bubble evento cal.com nella chat WhatsApp (`partials/timeline/meeting.html`) mostra **solo l'ora** (`mt.dtstart_time` in `_meetings_for_messaging_group_id`) perché il giorno è già coperto dal badge giorno sopra. Lista email: `_email_row_to_dict()` parsa l'header `Date` RFC 2822 grezzo con `parsedate_to_datetime` + `_it_datetime`.

**WhatsApp inbound: bot muto (di default), pannello read-only + composer manuale (nessuna generazione LLM).** L'auto-reply WhatsApp è **disattivata a livello router**: `src/router.ts` forza `trigger=0` (`effectiveWake=false`) quando il messaggio inbound ha `channelType === 'whatsapp'`, così il container non viene mai svegliato dal traffico cliente — i messaggi entrano comunque in `messages_in` per cronologia/contesto, ma nessuna chiamata LLM parte. **Eccezione — gruppi auto-reply:** i gruppi WhatsApp elencati in `TRUSTED_WHATSAPP_GROUPS` (`src/config/trusted-whatsapp-handles.ts`, helper `isAutoReplyWhatsappGroup`) sono **esentati dal mute**: lì il router rispetta la decisione di engage del wiring e sveglia davvero il container, e il gate outbound §5b consegna la risposta **senza approvazione** (lo stesso `isTrustedHandle` riconosce i JID `@g.us`). Tipico setup: wiring `engage_mode=pattern` con un nickname (es. `\b[Aa][Ff][Tt]\b` → il bot risponde solo quando lo si chiama "aft") verso l'agent group `Assistente` (dm-personal). Per gli altri gruppi/DM il bot resta muto.

> **Rimosso (cleanup):** l'intera macchina "genera una bozza di risposta con LLM e proponila per approvazione/rigenerazione" è stata smontata. Non esistono più: `services/autofatturiamo-sync/inbox_inject.py`; le route `POST /whatsapp/<jid>/generate` + `POST /whatsapp/generate-all`; il ciclo di rigenerazione `POST /actions/<id>/{regenerate,cancel-regenerate}` + `actions_db.request_regenerate`; la pagina `/feedback-loop` (+ `actions_db.list_with_feedback`, template `feedback_loop.html`, link navbar/alternatives); gli helper `_is_waiting_for_assistant`/`_has_pending_admin_injection`/`_whatsapp_chats_needing_reply`/`_resolve_whatsapp_session`/`_last_message_is_from_client` e `actions_db.pending_whatsapp_jids`; le colonne `attempts_json`/`awaiting_regenerate`/`awaiting_regenerate_since` dello schema `pending_actions` (su DB pre-esistenti restano dormienti grazie ai default); la bolla "Assistant sta generando…", i bottoni "Genera"/"Rigenera" e lo spinner live nei template. La nuova versione del flusso di risposta verrà progettata da zero, separatamente.

Sul pannello `/whatsapp` restano: la **cronologia read-only** e il **composer manuale** (`POST /whatsapp/<jid>/send`, route `whatsapp_send`) per scrivere a mano una risposta — crea un `whatsapp_reply` già portato a `done` (l'admin è l'approvatore) che il host consegna via Baileys. L'endpoint `GET /whatsapp/<jid>/state` → `{version}` resta col polling JS a 1.5s (IIFE in `whatsapp.html`): `version` concatena ts + identità dell'ultimo messaggio (`ts|kind|action_id|action_status|delivered|len`) e, quando cambia, rilancia il `loadDetail()` AJAX (sostituisce `#chat-detail` senza full reload, ripristina lo scroll). Riusa `_whatsapp_messages()` per coerenza con la dedup.

Gli unici `whatsapp_reply` nascono quindi da: **composer manuale**, **avviso pagamento automatico** `payment_failed_whatsapp` (detector in `automations.py`, accodato in `/actions` — §5), e — come **rete di sicurezza dormiente** — il gate outbound §5b (il bot è muto su WhatsApp salvo i **gruppi auto-reply** `TRUSTED_WHATSAPP_GROUPS`, le cui risposte però **bypassano** il gate e vengono consegnate dirette; quindi questo path normalmente non scatta). Tutti restano *gated* e mostrati come azioni approvabili in `/actions`. *(L'origine manuale «Avvisa su WhatsApp» dal bottone su `/pagamenti` è stata rimossa: per adesso quella pagina mostra solo l'errore — vedi §5.)*

### 2. launchd registration — `launchd/com.autofatturiamo-*.plist`

Two macOS LaunchAgents that boot at login and stay up (`RunAtLoad=true`, `KeepAlive=true`). Both plists use `{{PROJECT_ROOT}}` and `{{HOME}}` placeholders that must be substituted with absolute paths before loading.

| Plist | Label | Entrypoint | Role | Logs |
|-------|-------|------------|------|------|
| `com.autofatturiamo-sync.plist` | `com.autofatturiamo-sync` | `sync_only.py` | Headless scheduler — APScheduler jobs (sync stripe/odoo/email). No HTTP server. | `logs/autofatturiamo-sync.{log,error.log}` |
| `com.autofatturiamo-flask.plist` | `com.autofatturiamo-flask` | `flask_run.py` | Dashboard UI on `127.0.0.1:5001` (clienti, autofatture, /actions approval queue). | `logs/autofatturiamo-flask.{log,error.log}` |

Both share the same Python interpreter (`services/autofatturiamo-sync/.venv/bin/python3` — create with `python3 -m venv` + `pip install -r requirements.txt`) and working directory (`services/autofatturiamo-sync/`).

**Why two separate plists** (and not just `python app.py` in one of them):
- `sync_only.py` is the canonical owner of the APScheduler instance. `flask_run.py` deliberately skips `start_scheduler()` so the cron jobs (`sync_all`, `sync_pec`) don't double-fire.
- `flask_run.py` calls `app.run(debug=True, use_reloader=False)`. The `use_reloader=False` is load-bearing: with the Werkzeug reloader on, Flask forks a child and the parent exits — `KeepAlive=true` then sees the parent gone and ping-pongs the job. To hot-reload Python source under launchd, use `launchctl kickstart -k`. Jinja templates **do** reload per-request even without the reloader.
- Running `python app.py` from a terminal is still supported for foreground dev with full reloader.
- **Browser hot-reload**: every page extends `templates/base.html`, which polls `GET /api/version` every 1.5s. The endpoint returns `{boot_id, mtime}` — `boot_id` is set once at process start (so a server restart bumps it), `mtime` is the max mtime across `app.py`, `actions_db.py`, `templates/*.html`, and `action_executors/*.py`. When either changes the client calls `location.reload()`. Fully inert under launchd (nobody edits files in prod), so it stays on universally. To dev-loop comfortably: run `python app.py` in a terminal — Werkzeug restarts on `.py` edits → new `boot_id` → browser auto-reloads; template edits bump `mtime` → browser auto-reloads without a restart.

Install (after placeholder substitution) for both:

```bash
PROJECT_ROOT="$(pwd)"   # run from repo root
for L in com.autofatturiamo-sync com.autofatturiamo-flask; do
  sed -e "s|{{PROJECT_ROOT}}|$PROJECT_ROOT|g" -e "s|{{HOME}}|$HOME|g" \
    "launchd/$L.plist" > "$HOME/Library/LaunchAgents/$L.plist"
  launchctl load "$HOME/Library/LaunchAgents/$L.plist"
done
```

Per-job control:

```bash
launchctl unload                 ~/Library/LaunchAgents/com.autofatturiamo-flask.plist
launchctl kickstart -k gui/$(id -u)/com.autofatturiamo-flask   # restart after editing app.py
launchctl list                   com.autofatturiamo-flask      # status + PID + last exit
```

### 3. Telegram channel adapter — `src/channels/telegram*.ts`

Telegram is the primary channel for AFT. The adapter lives in the inherited `src/channels/` directory but is AFT-specific (upstream's Telegram support lives on the `channels` branch and was installed via the skill workflow).

| File | Purpose |
|------|---------|
| `telegram.ts` | Telegram adapter built on the Chat SDK bridge. Cold-start retry with backoff. Inbound interceptor calls `tryConsume()` (pairing) **before** normal routing — pairing messages never reach the router. An admin command interceptor (`COMMAND_RE`) also short-circuits `/ping`, `/restart`, `/clear`, `/summary` and `/development` before the router (vedi §9 per `/summary`, §9-bis per `/development`). |
| `telegram-pairing.ts` | Proof-of-ownership flow (see below). Storage: `data/telegram-pairings.json` under an in-process mutex. |
| `telegram-markdown-sanitize.ts` | Normalizes outbound text for Telegram's legacy `parse_mode=Markdown`: rewrites `**bold**` → `*bold*`, strips unbalanced delimiters, flattens lists/HRs to Unicode bullets. |
| `telegram-commands.test.ts` | Tests for slash-command handling. |
| `telegram-pairing.test.ts` | Tests for the pairing state machine. |
| `telegram-markdown-sanitize.test.ts` | Tests for the Markdown sanitizer. |

**Pairing flow** (`telegram-pairing.ts`) — why it exists: BotFather tokens have no user binding, so anyone who guesses the bot's username can DM it. Pairing closes that gap. Setup generates a one-time 4-digit code; the operator echoes it back from the chat they want to register. The message must be **exactly** the 4 digits (optionally prefixed by `@botname ` in privacy-ON groups) — arbitrary 4-digit numbers in messages do NOT match. On match: chat is recorded, paired user is upserted, and if no owner exists yet they are promoted to owner — all before the router sees anything.

- **`PairingIntent`**: `'main'` | `{ kind: 'wire-to', folder }` | `{ kind: 'new-agent', folder }`
- **`PairingStatus`**: `'pending'` | `'consumed'` | `'invalidated'` | `'unknown'`
- Codes do not expire — they are consumed on match or invalidated by wrong guesses (attempts capped per record).

### 3b. WhatsApp channel — dedicated number (Baileys)

Second messaging channel, installed via the upstream `/add-whatsapp` skill (Baileys 7 — files copied from `channels` branch: `src/channels/whatsapp.ts`, `setup/whatsapp-auth.ts`). The adapter itself is upstream; what's AFT-specific is the operational decision to run the bot on a **dedicated phone number** (`+39 331 162 3614`) rather than a personal WhatsApp.

- **`ASSISTANT_HAS_OWN_NUMBER=true`** in `.env` — tells the adapter to skip the bot-name prefix on outbound messages.
- **Auth state**: `store/auth/creds.json` — linked-device credentials produced by `setup/whatsapp-auth.ts --method qr`.
- **`scripts/wa-qr-browser.ts`** — AFT-local utility that wraps the upstream auth step, parses its `WHATSAPP_AUTH_QR` status blocks, and serves the rotating QR as a PNG on a small local HTTP server (auto-opens default browser). Replaces the unreadable terminal-ASCII QR on this machine. Run with `pnpm exec tsx scripts/wa-qr-browser.ts [--clean]`.

### 4. Modifications to inherited NanoClaw files

Small, surgical edits made on top of upstream:

- **`src/channels/index.ts`** — adds `import './telegram.js'` so the Telegram adapter self-registers when the channel barrel is loaded.
- **`src/db/sessions.ts`** — adds `getActiveSessionsByMessagingGroup(messagingGroupId, threadId)` returning all active sessions for a messaging group (optionally filtered by `thread_id`). Used by the Telegram pairing interceptor to look up which agents are currently wired to the chat.
- **`src/container-runner.ts`** — adds a RW bind-mount of `services/autofatturiamo-sync/actions/` → `/workspace/actions/` inside every agent container so the MCP tool can enqueue operational actions (§5). Skipped silently if the dir doesn't exist.
- **`src/delivery.ts`** — adds the outbound WhatsApp approval gate (see §5b): just before `deliveryAdapter.deliver()`, intercepts WhatsApp `chat` messages whose recipient is NOT trusted (`isTrustedHandle`, src/modules/trusted-handles/) and enqueues them as `whatsapp_reply` pending actions instead of sending. Trusted = numero in `TRUSTED_WHATSAPP_HANDLES` **o** gruppo `@g.us` in `TRUSTED_WHATSAPP_GROUPS` (consegna diretta, niente coda). Also registers `startApprovedWhatsappReplyPoll()` — a 5s loop that reads approved `whatsapp_reply` actions from `actions.db` and delivers them via Baileys post-approval.
- **`src/router.ts`** — global WhatsApp mute (`effectiveWake=false` per ogni inbound `whatsapp`/`chat`) con **eccezione** per i gruppi `TRUSTED_WHATSAPP_GROUPS` (`isAutoReplyWhatsappGroup`): lì il router rispetta la decisione di engage del wiring e sveglia il container. Vedi §1 "WhatsApp inbound".
- **`src/config/trusted-whatsapp-handles.ts`** + **`src/modules/trusted-handles/index.ts`** — le due whitelist hard-coded (`TRUSTED_WHATSAPP_HANDLES` numeri DM, `TRUSTED_WHATSAPP_GROUPS` gruppi auto-reply) e gli helper `isTrustedHandle` / `isAutoReplyWhatsappGroup`. Modifica = edit file + `pnpm run build` + restart host manuale (skill `ppp-start`, NON launchctl — TCC).
- **`src/index.ts`** — wires `startApprovedWhatsappReplyPoll()` into the boot sequence alongside the active/sweep polls; also wires `startAgentPromptPoll()` (§5e, daemon→agent bridge) + `stopAgentPromptPoll()` in shutdown.
- **`src/modules/agent-prompt/index.ts`** (AFT-only, new module in the inherited tree) — host poll that consumes `agent_prompt` actions from `actions.db` and injects them as inbound messages to an agent (§5e).
- **`src/db/migrations/index.ts`** — registers `migration018` (`agent_trusted_handles` table).
- **`container/agent-runner/src/mcp-tools/index.ts`** — adds `import './operational-actions.js'` so the AFT-specific tool registers when the MCP barrel loads.
- **`scripts/delete-cli-agent.ts`** — before deleting host-side directories for a folder, finds any running containers named `nanoclaw-v2-<folder>-*` via the container runtime and calls `stopContainer()` on each. Avoids `EACCES`/`EBUSY` errors on `rmSync` when bind-mounts are still held by a live container.
- **`.gitignore`** — ignores `services/autofatturiamo-sync/.venv/`, `__pycache__/`, `local.db*`, `data/`, and `actions/` inside that service.
- **`package.json` / `pnpm-lock.yaml`** — version bumps tracking upstream; no new top-level Node dependencies introduced by AFT.

### 5. Operational actions framework — agent → dashboard-approved actions

A **separate** approval system from NanoClaw's native `approvals/primitive.ts`. That one is for self-modification and credentialed actions, delivers approval prompts via DM (Telegram), and runs the executor host-side. The operational-actions framework is AFT-only, gated by the **Flask dashboard UI**, and runs executors inside the Python daemon process.

**End-to-end flow:**

1. The agent (container) calls an MCP tool (today: `notify_team` or `send_email_to_clients`).
2. The tool calls `enqueuePendingAction(...)` → INSERT row in `actions/actions.db` with status `pending`.
3. The Flask dashboard polls the count via `actions_db.count_pending()` (template context) and renders the queue at `/actions`.
4. The user clicks **Approva** or **Rifiuta**.
5. On approve: `actions_db.approve(id)` flips status → `approved`, then `action_executors.dispatch(...)` runs the executor and the row becomes `done` (or `failed` with `error`). On reject: status → `rejected`, no executor runs. **Reject requires a free-text `feedback`** (validated `required` in the UI + server-side); the value is persisted in the `feedback` column on `pending_actions` but is currently not consumed by any logic — raccolto come informazione per future iterazioni.
6. **Fire-and-forget e opaco al bot**: l'agent non vede mai l'esito reale e *non sa nemmeno* che esiste uno step di review. I tool MCP ritornano una conferma di successo neutra (es. "Email inviata a N destinatari.") — è una bugia deliberata: il bot deve credere di aver eseguito l'azione direttamente. Nomi dei tool e `description` MCP NON devono menzionare dashboard, approvazione, accodamento o pending. Mantieni questa opacità per ogni nuovo action_type.

**Data flow:**

```
[Container Bun/TS]              SQLite actions.db              [Flask Python]
notify_team           ──INSERT─▶ pending_actions  ───polling──▶ GET /actions (badge "N pending")
send_email_to_clients ──INSERT─▶                                POST /actions/<id>/approve → dispatch executor
                                                                POST /actions/<id>/reject
```

**File map:**

| File | Role |
|------|------|
| `services/autofatturiamo-sync/actions_db.py` | SQLite schema (`pending_actions`) + accessor (init, list, approve, reject, mark_done, mark_failed, count_pending, list_recent_all). |
| `services/autofatturiamo-sync/action_executors/__init__.py` | `ACTIONS` registry + `dispatch(action_type, payload)`. |
| `services/autofatturiamo-sync/action_executors/notify_team.py` | Executor: POST diretto al webhook Discord (`DISCORD_WEBHOOK_OPERATIONS` / `DISCORD_WEBHOOK_NOTIFICATIONS`). |
| `services/autofatturiamo-sync/action_executors/send_email_to_clients.py` | Executor **stub**: formatta destinatari/oggetto/corpo come messaggio Discord e li spedisce sul canale operations. TODO sostituire con vero invio SMTP (Aruba PEC o Resend) — payload schema già definitivo. |
| `services/autofatturiamo-sync/templates/actions.html` | Thin orchestrator for `/actions`: includes `_action_placeholders.html`, loops `render_action_debug(row)` (raw-fields debug view) from `_action_macros.html`, e include `_action_rules_modal.html` con un bottone "Visualizza regole" (classe `.actionRulesBtn`) nell'header. |
| `services/autofatturiamo-sync/templates/_actions_column.html` | Right-column "Azioni" partial included by `dashboard.html` (right rail, solo dashboard): last ~20 actions, pending highlighted via `bg-white/[0.10]` (others transparent), inline Approve/Reject when pending. Header con bottone icona "Visualizza regole" (`.actionRulesBtn`) che apre `_action_rules_modal.html` (incluso a fine partial). |
| `services/autofatturiamo-sync/templates/_action_rules_modal.html` | Modale "Azioni proponibili e regole" (analogo a "Visualizza regole" del Kanban). Renderizza la **view** `action_rules` iniettata da `inject_action_rules` (context processor): catalogo `ACTION_RULES` in `app.py` — un tipo per voce (titolo, fonte, regola "quando", "cosa fa") arricchito a runtime con lo stato `active` (`notify_team`/`send_email_to_clients` ← `DISCORD_WEBHOOK_OPERATIONS`; `whatsapp_reply` ← `_bot_is_running()`). Solo documentazione: nessun toggle per-azione, approvazione sempre manuale. Aperto da qualsiasi `.actionRulesBtn`; JS class-based incluso nel partial (includerlo una volta per pagina). |
| `services/autofatturiamo-sync/templates/_action_macros.html` | Shared Jinja macros used by the column and the page: `render_payload(action_type, payload, compact)` (dispatcher), `render_action_card(row, placeholder)` (card pulita), `render_action_debug(row)` (raw-fields debug card, ormai orfana), `action_approve_reject_buttons(row_id, placeholder, size, show_simulate)` (Approva/Rifiuta + opzionale **Simula**), `action_status_badge(status)` e `action_feedback_modal()`. Il "Rifiuta" apre un **modale singleton** (`action_feedback_modal`, incluso una volta per pagina) con `<textarea name="feedback" required>`. La pagina `/actions` (`actions.html`) usa `render_action_card` (non più `render_action_debug`), divisa in **Da approvare** (pending) + **Storico**; ogni card ha header compatto (`render_compact_header`, ora anche per `notify_team`) + `action_status_badge` (In attesa/Approvata/Eseguita/Rifiutata/Fallita) + il motivo del rifiuto se presente. Gli esempi statici `_action_placeholders.html` sono stati **rimossi**. **Pulsante Simula** (`show_simulate`, e nel form di `action_approve_payment_failed` via `formaction`+`formnovalidate`): vedi §5 «Simula». |
| `services/autofatturiamo-sync/templates/base.html` | Layout skeleton only: `<head>`, body wrapper, `<main>` slot, right-column aside, fixed Logout button, "Ultima sincronizzazione" widget (reads `last_sync.stripe/odoo/email` from `inject_last_sync`; `sync_only.py` keeps data fresh so no manual sync buttons), the `/api/version` polling reload script. Delegates navbar to `_navbar.html` and modal to `_page_modal.html`. |
| `services/autofatturiamo-sync/templates/_navbar.html` | Top navbar. Exposes a `nav_tab(href, label, badge=None, match_prefix=False)` macro for active/inactive state matching. Today's slots: Dashboard link + **"Pagine vecchie ▾"** dropdown (Clienti, Autofatture, Risposte SDI, ZIP Inviati, Email — viste legacy del portale Operations) + **Contesto** + **Azioni**-with-badge. Dropdown toggle script (`#legacyPagesBtn`/`#legacyPagesDropdown`) lives inside the partial. |
| `services/autofatturiamo-sync/templates/_page_modal.html` | Generic page-in-overlay modal. Exposes `window.openPageModal(url, label, sizeOrOpts)` and `window.closePageModal()`. Size presets: `sm`/`md`/`lg`/`full` (default `full`). Embeds the URL in an iframe with `?embed=1` appended, dismissed by ESC / backdrop click / `[data-page-modal-close]`. |
| `services/autofatturiamo-sync/templates/controllo_autofatture.html` | "Stato autofatture per cliente" page at `/controllo-autofatture` — per-customer counters **mancanti / in attesa / confermate** anchored to the competence month (see "Controllo Autofatture — vista per MESE DI COMPETENZA" above). Row highlighted red when `mancanti>0`; empty state distinguishes `db_ready=False` (produzione.db not imported) from no reservations for the month. |
| `services/autofatturiamo-sync/templates/dashboard.html` | Home `/` "colpo d'occhio": thin orchestrator that imports macros from `_dashboard_tiles.html` and calls them in order (Controllo autofatture full-width on top → a 2-column grid `grid-cols-1 md:grid-cols-2` with Prospect on the left and "Senza P.IVA su Odoo" on the right, each at 50% on desktop and stacked on mobile). Keeping tiles inside `_dashboard_tiles.html` means new tiles can be added without bloating the page template. The `dashboard()` handler computes inputs by reusing `_get_controllo_competenza()` (competence-month model) + `get_prospect_stats()` + `get_stripe_senza_odoo_stats()` and passing them straight to the macros — no extra SQL inside templates. |
| `services/autofatturiamo-sync/templates/contesto.html` | Dedicated `/contesto` page. Renders, in order: (1) the narrative overview HTML from `context_overview.md` (via `_render_context_overview()`) inside a sky-accent styled `<section>`, then (2) the structured cards driven by `context.md` via the `render_card` macro from `_context_macros.html`. Header includes a "Modifica" button linking to `/contesto/edit`. The `contesto()` handler in `app.py` passes both `context_overview_html` and `context_sections` to the template. Linked from the navbar as a top-level tab. |
| `services/autofatturiamo-sync/templates/contesto_edit.html` | Editor a `/contesto/edit` con **due tab** (Interno / Cliente) selezionabili via `?audience=interno|clienti` (default `interno`, retro-compatibile con il link "Modifica" da `/contesto`). Ogni tab edita il file md corrispondente (`context.md` o `context-client.md`); ogni save fa un backup rolling sul `.bak` del file giusto. Hot-reload via `_sources_mtime`. |
| `services/autofatturiamo-sync/templates/_context_macros.html` | Jinja macros for the Contesto block (`_definition`, `_rule_with_examples`, `_cases`, `_stepper`, `_comparison`, `_generic`) plus the dispatcher `render_card`. Each macro consumes the `{title, slug, icon, accent, layout, data}` dict produced by `_parse_context_md()`. Accent color comes from the section registry (`sky`/`amber`/`violet`/`rose`/`emerald`/`slate`) and is interpolated into Tailwind classes (CDN/JIT-evaluated). |
| `services/autofatturiamo-sync/templates/_dashboard_tiles.html` | Jinja macros for the home `/` tiles, sharing two helpers: `tile_shell(title, subtitle, url, label, size, wrapper_class)` (clickable card with header + "Vedi tutto →"; body goes inside `{% call %}`) and `status_chip(value, variant, label, intensify_if, dim_if_zero)` (palette `success`/`warning`/`danger`/`neutral`; numeric mode = fixed-width centered chip, label mode = inline chip with text). The three tiles: `autofatture_tile(af_month, af_all, af_problems)` renders the "Controllo autofatture" card (competence-month model) — heatmap row of 24px squares (worst-state-wins: red `mancanti>0`, amber `in_attesa>0`, emerald only `confermate>0`, slate none) plus problem rows (clients with `mancanti>0` or `in_attesa>0`) using `status_chip(..., intensify_if=1)` for mancanti/in_attesa/confermate. `prospect_tile(stats)` → big total + `status_chip` for `con_piva`/`senza_piva` + top stage breakdown; whole card click → `openPageModal('/clients', 'Clients', 'lg')`. `senza_piva_odoo_tile(stats)` → big total + first 5 entries; click → `openPageModal('/clienti?q=aggiungi', ..., 'lg')`. Empty state shown when `stats.total == 0`. |
| `container/agent-runner/src/db/actions-db.ts` | Container-side SQLite writer — `enqueuePendingAction()`. |
| `container/agent-runner/src/mcp-tools/operational-actions.ts` | MCP tool definitions (one per `action_type`). |
| `container/agent-runner/src/mcp-tools/operational-actions.instructions.md` | Tone/style guidance for `send_email_to_clients` (informale, dare del **tu**, chiamare il destinatario per nome di battesimo, niente firma fissa, niente burocratese). Auto-incluso nel `CLAUDE.md` di ogni gruppo come fragment `module-operational-actions.md` (vedi `src/claude-md-compose.ts:87-99`). |

**Registered action types today**:

- `notify_team` — POST diretto al webhook Discord; flag `tada` sceglie tra canale operations e canale celebrations. **Parte SENZA approvazione**: il loop di auto-dispatch nel daemon (`auto_dispatch.py`, ogni 15s) la esegue da solo con claim atomico (`actions_db.approve`), poi `dispatch` + `mark_done`. Resta tracciata in `/actions` come `done` ma non richiede mai un click. I tipi sempre-automatici stanno in `_ALWAYS_AUTO`; l'insieme effettivo è calcolato al boot da `_compute_auto_dispatch_types()`.
- `send_email_to_clients` — invio email a uno o più clienti. Payload: `recipients: [{email, name?}]`, `subject`, `body` (plain text), `reason?`. **In questa fase è uno stub**: l'executor riformatta i campi come messaggio Discord (canale operations) anziché inviare via SMTP — serve a esercitare la pipeline MCP→approvazione→executor finché non scegliamo il provider email definitivo (probabili candidati: Aruba PEC SMTP con le stesse credenziali IMAP del daemon, oppure Resend). **Finché è stub PARTE SENZA approvazione** (auto-dispatch): il modulo espone `IS_STUB = True` e `_compute_auto_dispatch_types()` lo include solo in quello stato — di fatto è una notifica interna su Discord. ⚠️ Quando si aggancia l'SMTP reale, mettere `IS_STUB = False`: l'azione esce dall'auto-dispatch e torna a richiedere approvazione, perché ridiventa client-facing.
- `whatsapp_reply` — messaggio WhatsApp in uscita gated. **Consegna** (comune a tutte le origini): l'executor Python `whatsapp_reply.py` non spedisce (Baileys vive nel host Node, non in Flask), segna solo `status='done'`; un poll lato host (`startApprovedWhatsappReplyPoll`, 5s) legge le `done` non ancora `host_delivered_at` e consegna via adapter WhatsApp. **Origini** (vedi anche §5b): (1) **composer manuale** `/whatsapp/<jid>/send` e (2) **avviso pagamento** «Avvisa su WhatsApp» (sotto) creano la riga direttamente via `actions_db.enqueue`; (3) come **rete di sicurezza dormiente**, il gate outbound `src/delivery.ts` intercetta un'eventuale risposta del bot verso un destinatario **NON in whitelist** e chiama `enqueueWhatsappReplyForApproval()` (il container crede di aver inviato — id sintetico `pending-approval:<id>` per opacità). I destinatari **in whitelist** (numeri `TRUSTED_WHATSAPP_HANDLES` o gruppi `TRUSTED_WHATSAPP_GROUPS`, via `isTrustedHandle`) bypassano il gate e ricevono la risposta diretta. Oggi il bot è **muto su WhatsApp** salvo i gruppi auto-reply (router `trigger=0`/`effectiveWake`, esenzione `isAutoReplyWhatsappGroup`), quindi per i non-gruppi il path (3) normalmente non scatta e resta come difesa. **Nessuna generazione LLM né rigenerazione di bozze** (rimossa, vedi §1).
  - **Origine aggiuntiva — «Avvisa su WhatsApp» su `/pagamenti` (RIMOSSA dall'UI).** La route `POST /pagamenti/<subscription_id>/avvisa-whatsapp` (`pagamenti_avvisa_whatsapp`) esiste ancora ma **non è più raggiungibile**: il bottone «Avvisa» è stato tolto da `templates/pagamenti.html` su richiesta — per adesso la pagina `/pagamenti` **mostra solo l'errore** (badge stato + riga «Pagamento fallito il … — motivo» + link Fattura/Cliente), senza azione di invio. La route (con il suo testo hardcoded «il rinnovo del tuo abbonamento non è andato a buon fine», **non** allineato a `compose_message`) e il banner esito `/pagamenti?wa=…` restano nel codice come dead-path inerte, pronti a essere riattivati o eliminati. L'avviso pagamento resta disponibile via il detector automatico `payment_failed_whatsapp` in `/actions` (sotto).

- `payment_failed_whatsapp` — **avviso pagamento non riuscito su WhatsApp, accodato in automatico (no AI)**. A ogni `run_after_sync_all` il detector `automations.enqueue_payment_failed_whatsapp` scandisce `stripe_subscriptions WHERE is_error=1` con `latest_invoice_url` e accoda **una** azione `payment_failed_whatsapp` *pending* per abbonamento. Dedup via ledger `notified_events` con `kind='payfail_whatsapp'` ed `ext_id = subscription_id:latest_invoice_id` (Stripe ritenta la stessa fattura più volte → un solo avviso; un nuovo rinnovo fallito = nuova fattura = nuovo avviso). A differenza degli altri detector **non c'è seed**: gli abbonamenti già in errore al primo run vengono accodati. Payload: `subscription_id`, `customer_name?`, `customer_first_name?`, `customer_email?`, `invoice_url`, `amount?`, `status?`, `phone_hint?` (da `stripe_clienti.phone`, spesso vuoto), `reason`. Il testo è composto all'accodamento da `compose_message(customer_name, invoice_url, first_name)` e salvato in `payload["message_text"]`. **Saluto col nome proprio, non la ragione sociale**: prima `compose_message` faceva `customer_name.split()[0]` → «Ciao EASY» per *EASY TO RENT di Antonio Alizzi*. Ora `automations._resolve_platform_first_name(local_conn, prod_conn, customer_id)` risale `stripe customer_id → cliente_links(stripe) → cliente_id → cliente_links(piattaforma).ext_id = auth_user.id → produzione.db auth_user.first_name` → «Ciao Antonio,» (fallback «Ciao,» se il cliente non ha account piattaforma collegato o `produzione.db` non è importato). Il testo **non** contiene più «ti scrivo da Autofatturiamo» (il numero WhatsApp è già quello di Autofatturiamo) né emoji nel saluto. **Numero e testo si inseriscono/modificano in approvazione**: Stripe non espone il telefono, quindi la card in `/actions` (macro `action_approve_payment_failed` in `_action_macros.html`) mostra un **textarea editabile** precompilato col testo + un **campo `tel` obbligatorio** precompilato con `phone_hint`. Il form invia `message_text` e `phone` nel POST `/approve`; `actions_approve` li inietta in `payload` prima del dispatch. L'executor `payment_failed_whatsapp.py` normalizza il numero (`customer_link.normalize_phone`), usa `payload["message_text"]` (fallback: ricompone) e crea una riga `whatsapp_reply` **già `done`** → l'host la consegna via Baileys senza seconda approvazione. Catalogato in `ACTION_RULES`. È la versione **proattiva/automatica** dell'origine manuale «Avvisa su WhatsApp» di `/pagamenti`.

**Optional `reason` in the payload:** every action type can include a `reason: string` inside `payload_json` — a short explanation of *why* the agent is requesting this action. The dashboard renders it as a top block above the actual payload, separated by an edge-to-edge divider. Useful for emails ("inviata perché 4 RC SDI mancano da 7+ giorni"), bulk operations, or any case where the user needs context to approve confidently. It is shown only in the approval UI; executors ignore it (Discord gets just `message`). On `notify_team` it's exposed as an optional MCP tool parameter (descritto al bot come "nota interna di log", senza menzionare la dashboard); for new action types just declare an optional `reason` in the input schema (con descrizione neutra) and forward it untouched in the payload.

**Adding a new action type:**

1. New executor module under `action_executors/<name>.py` exposing `execute(payload: dict) -> dict`.
2. Register it in `action_executors/__init__.py` (`ACTIONS = {...}`).
3. New MCP tool in `container/agent-runner/src/mcp-tools/operational-actions.ts` with a clear schema, calling `enqueuePendingAction({action_type, payload})`.
4. (Optional) Add a payload renderer macro for the new `action_type` in `templates/actions.html` — otherwise it falls back to pretty-printed JSON.
5. Update §5 here listing the new action type.

**Storage:** `services/autofatturiamo-sync/actions/actions.db` on the host, bind-mounted into every agent container at `/workspace/actions/actions.db` (RW). `journal_mode=DELETE` for cross-mount visibility (same gotcha as session DBs — see `container/agent-runner/src/db/connection.ts`). The daemon (`sync_only.py`) creates the dir + file at boot — if the daemon never ran, the container mount silently no-ops and the MCP tool fails gently at first use.

**Caveats:**

- **No retention policy**: rejected/done/failed rows accumulate forever in `actions.db`. Add an APScheduler cleanup job if it grows.
- **No live push**: the dashboard only updates the badge on full page render — the user has to refresh `/actions` (or the page they're on) to see new pending requests.

**Simula (anteprima sul proprio numero).** Le azioni WhatsApp (`whatsapp_reply` e `payment_failed_whatsapp`) hanno, accanto ad Approva/Rifiuta, un pulsante **Simula** (`POST /actions/<id>/simulate`, route `actions_simulate`). Invia una **copia** del messaggio al numero di test `SIMULAZIONE_WHATSAPP_JID` (`393791234555@s.whatsapp.net`, +39 379 123 4555) **senza** approvare: l'azione originale resta `pending`. Implementazione: `_whatsapp_text_for_simulation(action_type, payload, form)` estrae il testo (per `whatsapp_reply` da `content_json.text`; per `payment_failed_whatsapp` da `message_text` del form se presente, altrimenti dal payload, altrimenti ricomposto), poi crea una `whatsapp_reply` **già `done`** verso quel JID — l'host la consegna via Baileys come per ogni `whatsapp_reply` approvata (delivery diretto al `platform_id`, non serve una chat preesistente). Il pulsante è reso da `action_approve_reject_buttons(..., show_simulate=True)` e dalla colonna laterale `_actions_column.html`; per `payment_failed_whatsapp` è un secondo submit dello stesso form (`formaction`+`formnovalidate`, così porta il `message_text` corrente e salta il `required` sul telefono).

### 5b. Outbound WhatsApp approval gate — whitelist + interception

Same approval queue as §5, but for a different traffic pattern: invece di un tool MCP esplicito, qui intercettiamo **il normale flusso di risposta del bot** verso destinatari WhatsApp non fidati.

**Modello mentale.** Il bot WhatsApp (Baileys) può ricevere messaggi sia da membri dell'azienda sia da clienti esterni. Verso i primi vogliamo risposte dirette; verso i secondi vogliamo un controllo umano prima della consegna. La whitelist è la lista dei "nostri".

**Whitelist hard-coded** (Phase 3, semplificazione) — la whitelist vive in `src/config/trusted-whatsapp-handles.ts` come array TS readonly:

```ts
export const TRUSTED_WHATSAPP_HANDLES: ReadonlyArray<string> = [
  '393790000000000000000@s.whatsapp.net', // Claudio (founder) — TEMP: numero finto per disabilitare bypass approvazione
] as const;
```

È deliberatamente separata da `agent_group_members`: quella è il gate "puoi parlare con il bot", questa è il gate "le mie risposte verso di te non passano per approvazione".

**Per aggiungere / rimuovere un dipendente:**

```bash
# 1) edita src/config/trusted-whatsapp-handles.ts (aggiungi/togli una riga)
# 2) rebuilda
pnpm run build
# 3) restart del host (la whitelist è in memoria del processo)
launchctl kickstart -k gui/$(id -u)/com.nanoclaw-v2-81ab8806
```

Audit history via `git log`. Nessuna CLI, nessuna tabella DB.

**Versione storica (deprecata)** — la prima implementazione usava una tabella `agent_trusted_handles` (migration 018) con risorsa `ncl trusted-handles`. Migration 020 (`drop-trusted-handles-table`) la elimina al boot. Le migration 018 e 020 restano entrambe registrate per gestire installs in vari stadi (vergine, intermedi, già aggiornati).

Helper utility in `src/modules/trusted-handles/index.ts`: `normalizeHandle(channelType, raw)` (parser "+39 ..." → JID) e `isTrustedHandle(channelType, handle)` (lookup nell'array hard-coded).

**Bypass inbound channel-approval (Phase 2)** — `src/router.ts` + `src/modules/whatsapp-approval/auto-wire.ts` + migration 019.

Upstream NanoClaw, alla prima ricezione di un DM da un canale non-wired, emette una card "💬 New direct message" sul Telegram dell'admin (`src/modules/permissions/channel-approval.ts`) e parcheggia il messaggio in `pending_channel_approvals` finché l'admin non clicca "Connect". Per WhatsApp questa friction è inaccettabile (ogni cliente nuovo richiederebbe intervento manuale); il controllo umano è già garantito all'outbound dalla whitelist `agent_trusted_handles`.

Modifiche:

- `src/router.ts` (auto-create messaging_group): `unknown_sender_policy` default = `'public'` quando `channel_type === 'whatsapp'`, `'request_approval'` altrimenti (default upstream invariato per Telegram/email/altri).
- `src/router.ts` (branch `agentCount === 0`): per WhatsApp, invece di chiamare `channelRequestGate`, chiama `autoWireWhatsappMessagingGroup(mg, defaultWhatsappAgentGroupId())` — crea un `messaging_group_agents` row con `engage_pattern='.'`, `sender_scope='all'`, `ignored_message_policy='drop'`, `session_mode='shared'`. Poi rinfresca `agentCount=1` e cade nel fan-out normale. Risultato: il messaggio raggiunge subito il container.
- `defaultWhatsappAgentGroupId()` in `src/modules/whatsapp-approval/auto-wire.ts`: prima query → primo agent_group con almeno un wiring WhatsApp esistente. Fallback → primo agent_group dell'install. `null` → niente install → cade nel ramo legacy (channelRequestGate) per non rompere installs vergini.
- Migration 019 (`src/db/migrations/019-whatsapp-public-policy.ts`): `UPDATE messaging_groups SET unknown_sender_policy='public' WHERE channel_type='whatsapp' AND unknown_sender_policy='request_approval'` per backfillare le righe pre-esistenti.

Per gli altri canali (Telegram, email, ecc.) il channel-approval upstream rimane invariato — è il flusso di onboarding "registra una nuova chat" e ha senso lì. Solo WhatsApp ha bisogno del bypass perché solo lì il bot è esposto a clienti esterni.

**Spam mitigations**: chi conosce il numero WhatsApp del bot può far partire una sessione del container. Per casi di abuso, l'admin può settare `messaging_groups.denied_at` (campo upstream esistente) sul numero offending — il router droppa silenziosamente i futuri messaggi di quella chat (router.ts:212). Per disabilitare completamente l'arrivo automatico in produzione si può tornare al comportamento upstream rimuovendo il branch WhatsApp dal router.

**Flusso end-to-end** (caso non-whitelist):

1. Cliente WhatsApp manda messaggio → router → container risponde normalmente → scrive in `outbound.db`.
2. Host `src/delivery.ts:deliverMessage`, subito prima di `deliveryAdapter.deliver()`:
   - Se `channel_type==='whatsapp' && kind==='chat'` e `!isTrustedHandle(...)` → chiama `enqueueWhatsappReplyForApproval()`.
   - `pending_actions` riceve una nuova riga con `action_type='whatsapp_reply'`, status `pending`, payload (JSON serializzato del messaggio + `session_id`/`msg_id` per il delivery successivo).
   - `deliverMessage` ritorna un `platform_msg_id` sintetico (`pending-approval:<id>`) → `markDelivered` viene chiamato → **il container vede "consegnato"**.
3. Dashboard Flask `/actions` mostra la riga renderizzata via `render_whatsapp_reply` in `_action_macros.html` (destinatario, testo, eventuali allegati).
4. Admin clicca **Approva**: route `/actions/<id>/approve` → `actions_db.approve()` → `dispatch('whatsapp_reply', payload)` → executor `whatsapp_reply.py` segna solo `status='done'` (non spedisce — Baileys è altrove).
5. Polling lato host `pollApprovedWhatsappReplies` (5s, registrato in `start()`):
   - `readApprovedWhatsappReplies()` → riga `done && host_delivered_at IS NULL`.
   - Ricostruisce buffer allegati via `readOutboxFiles(agentGroupId, sessionId, msgId, files)`.
   - Chiama l'adapter Baileys → `markActionHostDelivered(id, platformMsgId)`.
   - Pulisce l'outbox con `clearOutbox()`.
6. Reject path: status → `rejected`, mai consegnato. Bot ha già visto "delivered", cliente non riceve nulla. Comportamento accettabile (admin rifiuta solo se inopportuno).

**Schema DB lato actions.db** — colonne aggiunte dalla §5b (`actions_db.py`):

| Colonna | Significato |
|---------|-------------|
| `host_delivered_at` | ISO timestamp valorizzato dal host quando ha realmente consegnato via Baileys. NULL = ancora da consegnare. |
| `host_delivery_error` | Ultimo errore di delivery (es. "Connection closed"). Trattato come backoff: righe con `host_delivery_error != NULL` non vengono ritentate dal poll finché qualcuno non azzera il campo a mano. |

`init_db()` applica una `ALTER TABLE ... ADD COLUMN` idempotente via `PRAGMA table_info(...)` lookup, così i DB pre-esistenti vengono migrati silenziosamente al boot.

**File aggiunti per la 5b:**

| File | Role |
|------|------|
| `src/config/trusted-whatsapp-handles.ts` | Array hard-coded della whitelist (Phase 3). Per aggiungere/rimuovere → edit + rebuild + restart. |
| `src/modules/trusted-handles/index.ts` | `isTrustedHandle(channelType, handle)`, `normalizeHandle(channelType, raw)` — utility che legge `TRUSTED_WHATSAPP_HANDLES` (no DB). |
| `src/db/migrations/018-trusted-handles.ts` | Migration legacy (Phase 1): creava la tabella `agent_trusted_handles`. Mantenuta nella catena per consistenza ma immediatamente eliminata dalla migration 020. |
| `src/db/migrations/020-drop-trusted-handles-table.ts` | DROP TABLE idempotente — disattiva il path DB (Phase 3). |
| `src/modules/whatsapp-approval/queue.ts` | Host writer/reader per `actions.db` lato whatsapp_reply: `enqueueWhatsappReplyForApproval` (dedup per `(session_id, msg_id)` su retry), `readApprovedWhatsappReplies`, `markActionHostDelivered`, `markActionHostDeliveryFailed`. |
| `services/autofatturiamo-sync/action_executors/whatsapp_reply.py` | Executor stub: segna `status='done'`, nessun side effect (delivery handled dal host). |
| `services/autofatturiamo-sync/templates/_action_macros.html` | Macro `render_whatsapp_reply(payload, compact)` + branch in `render_payload()`. |

**Opacità e dedup.** L'`enqueueWhatsappReplyForApproval` controlla se esiste già una riga `pending`/`approved`/`done` per la coppia `(session_id, msg_id)` prima dell'INSERT — protegge contro retry tra `enqueue` e `markDelivered`, evitando duplicati nella dashboard. Reject e failed non vengono mai ri-tentati: chi rifiuta lo fa per un motivo, e chi fa pasticci a mano vede il fail.

**Identità per canale** — non più basata su if/else nel `CLAUDE.local.md` di un singolo agent group: vedi §8 per la topologia attuale a due agent group (uno interno, uno client-facing).

**Caveats specifici:**

- Il messaggio resta nell'`outbox` del session finché il host non lo consegna realmente — niente `clearOutbox` nel ramo "in approvazione". Se l'admin lascia la riga `pending` per giorni, l'outbox cresce.
- `ask_question` (card interattive) verso un cliente non-whitelist viene intercettato come tutti gli altri `chat` outbound, ma `pending_questions` NON viene popolata (avverrebbe pre-approvazione, sarebbe inutile). Risultato: il flow `ask_user_question` non funziona per non-whitelist. Edge case, va bene così per ora.
- Il delivery post-approvazione fallisce se Baileys è disconnesso al momento del polling: la riga viene marcata con `host_delivery_error`, va inspezionata e ripulita a mano dall'operatore. Non c'è retry automatico — voluto, perché evita di consegnare in ritardo messaggi che l'admin pensava ormai inviati.

### 5c. Automazioni Discord event-driven — `automations.py`

Famiglia separata dalle operational action (§5): **NON passano da `actions.db` né da approvazione**. Rilevano eventi nuovi a ogni sync e postano direttamente su Discord via `discord_client.send_notification_to_team`. Sono il "il team viene avvisato automaticamente quando succede X".

**Memoria — `notified_events(kind, ext_id, notified_at)`** in `local.db` (schema `SCHEMA_NOTIFIED_EVENTS` in `app.py`, creato in `init_db`). **Mai droppata** dai sync (a differenza di `stripe_charges` / `stripe_subscriptions`, ricreate full-refresh): è ciò che distingue "evento nuovo" da "già annunciato". `kind` = tipo automazione, `ext_id` = id stabile dell'entità.

**Seed al primo run.** Per ogni `kind`, alla prima esecuzione `automations.py` registra silenziosamente **tutti** gli id correnti senza notificare (flag in `sync_state` key `automations_seed_<kind>`) — altrimenti al primo avvio si annuncerebbe tutto lo storico (es. 72 charge, 32 abbonamenti). Dal secondo run in poi solo gli id davvero nuovi vengono notificati.

**Routing canali** (deciso con l'utente): esiti positivi → canale celebrazioni (`notifications=True`); alert / operativi / meeting / pipeline → canale operations.

**Aggancio** (in `scheduler.py`, ognuno isolato in try/except così non rompe mai il sync): `run_after_sync_all()` in coda a `_job_sync_all`; `run_after_gcal()` in coda a `_job_sync_gcal`.

| `kind` | # catalogo | Trigger | Canale | Note |
|--------|-----------|---------|--------|------|
| `payment_ok` | 2 | charge `succeeded` nuova | celebrazioni | |
| `subscription_new` | 3 | subscription nuova | celebrazioni | |
| `payment_failed` | 4 | subscription `is_error` | operations | `ext_id = subscription_id:latest_invoice_id` → un nuovo rinnovo fallito ri-notifica |
| `subscription_canceled` | 5 | subscription `status='canceled'` | operations | |
| `platform_user` | 15 | nuovo `auth_user` in `produzione.db` | celebrazioni | no-op senza seed se `produzione.db` manca (seeda al primo import) |
| `gcal_event_new` | 1 | nuovo `uid` gcal con `dtstart` futuro, non cancellato | operations | |
| `gcal_state` | 14 | composito `uid\|dtstart\|status` nuovo per un `uid` già noto | operations | `CANCELLED` → "cancellato", altrimenti "riprogrammato" |

**Non implementati** (esclusi dall'utente in questa fase): punti 6–13, 16–19 del catalogo (autofatture SDI RC/in attesa — le **scartate** sono ora coperte da `scarti_notify.py`, vedi §5d; scadenze, prospect/opportunità Odoo, chat WhatsApp in attesa, email PEC, digest; più `bluedot_event` 12, `gcal_reminder` 13 e `pipeline_move` 16, implementati e poi rimossi su richiesta) e le azioni client-facing 21–27 (solleciti, onboarding, follow-up, richiesta P.IVA, SMTP reale). Per aggiungerne una: nuovo detector in `automations.py` (riusa `_emit` per il pattern ledger+seed), nuova entry in questa tabella.

**File aggiunti per la 5c:**

| File | Role |
|------|------|
| `services/autofatturiamo-sync/automations.py` | Motore: ledger helpers (`_known`/`_record`/`_emit`), detector per ogni `kind`, entry point `run_after_sync_all` / `run_after_gcal`. |
| `services/autofatturiamo-sync/auto_dispatch.py` | Loop daemon (thread, 15s) che esegue le action senza approvazione. `_ALWAYS_AUTO` = `notify_team`; `_compute_auto_dispatch_types()` aggiunge `send_email_to_clients` finché è stub (`IS_STUB`). Avviato da `start_scheduler()`. |
| `SCHEMA_NOTIFIED_EVENTS` in `app.py` | Tabella `notified_events` (registro persistente, registrata in `init_db`). |

### 5d. Notifica scarti SDI con denoise + triage AI — `scarti_notify.py`

Implementa la notifica delle **notifiche di scarto SDI** (autofatture rifiutate, `risposte_SDI.tipo='NS'`) con un design diverso dalle automazioni per-evento §5c: invece di avvisare scarto-per-scarto (gli scarti arrivano a ondate — es. 18 in un giorno per *Affittami srl*, tutti duplicati di fatture già accettate → rumorosi e fuorvianti), **batcha l'ondata e manda un unico riassunto analizzato da Claude**.

**Flusso** (job `scarti_notify` nello scheduler, ogni 5 min — vedi §1):
1. **Denoise/debounce**: raccoglie le NS recenti (entro `lookback_days`) non ancora nel ledger; invia solo quando da `quiet_min` minuti non arriva una nuova NS (ondata "assestata"), oppure dopo `max_wait_min` dal primo scarto del batch (cap anti-attesa-infinita). Timing in wall-clock Roma su `email_date_iso` (coerente con `_get_scarti_recenti`).
2. **Sync forzato**: al fire chiama `sync_email(force=False)` per avere il dato aggiornato — cattura le **RC** arrivate nel frattempo, che "risolvono" lo scarto.
3. **Gate "solo quando c'è un problema"**: dopo il sync, se nel batch **ogni** scarto risulta già risolto (RC arrivata → innocuo, es. duplicato `00404`), **non** si avvisa il gruppo: il batch viene registrato nel ledger in silenzio (così non si riaccumula né ri-triggera) e `process()` ritorna 0. Si notifica solo con **≥1 scarto ancora aperto** (`risolto=no`). Evita l'avviso ad ogni ondata di scarti innocui.
4. **Triage + invio**: raggruppa per cliente/errore (riusa `_get_scarti_recenti` + `_raggruppa_scarti_per_cliente_problema` di `app.py`), poi consegna in una di **due modalità** (impostazione `delivery`).

**Modalità di consegna (`delivery`):**
- **`agent` (default) — niente API key.** Il riassunto lo genera un **agente NanoClaw** (Claude via gateway OneCLI, nessuna chiave Anthropic gestita da noi) e viene pubblicato in un **chat target** (es. gruppo Telegram interno). Il daemon costruisce il prompt (`_build_agent_prompt`, istruzioni di triage **blindate**: "scrivi UN solo messaggio in QUESTO gruppo, niente DM, niente conferme" — necessario perché l'agente `dm-personal`, da assistente personale, altrimenti risponderebbe nella DM di Claudio) e accoda un'azione **`agent_prompt`** in `actions.db` (`enqueue`+`approve`+`mark_done`) con `{agent_group_id, messaging_group_id, prompt}`. Un **poll lato host** (§5e) la inietta come messaggio inbound nell'agente e l'agente risponde nel chat → consegna via pipeline esistente. Rispetta l'invariante "un solo writer per inbound.db": scrive il host, non il daemon.
- **`discord` — con API key.** Il daemon chiama direttamente l'API Anthropic (`claude_client.summarize_scarti`, default `claude-opus-4-8`; richiede `ANTHROPIC_API_KEY`, fallback plain-text se assente) e posta su Discord operations (`discord_client.send_notification_to_team(..., notifications=False)`). È anche il fallback se la modalità `agent` fallisce l'accodamento.

**Ledger / dedup**: `notified_events` con `kind='sdi_scarto'`, `ext_id = risposte_SDI.id`; riusa gli helper `_known`/`_record`/`_is_seeded`/`_mark_seeded` di `automations.py`. **Seed al primo run**: registra in silenzio tutte le NS correnti (0 notifiche) così lo storico pre-deploy non scatena un avviso; solo le NS nuove notificano.

**Impostazioni** (pagina `/impostazioni`, tabella `app_settings`, lette fresche dal daemon a ogni tick — cross-process via `local.db`, niente IPC): `scarti_notify_enabled`, `scarti_notify_quiet_min` (30), `scarti_notify_max_wait_min` (120), `scarti_notify_lookback_days` (7), `scarti_notify_model` (`claude-opus-4-8`, solo modalità discord), `scarti_notify_extra_prompt`, `scarti_notify_delivery` (`agent`|`discord`), `scarti_notify_target_ag` / `scarti_notify_target_mg` (chat target in modalità agent — dropdown popolato da `_list_agent_chats()` su `data/v2.db`). La cadenza del *check* (`scarti_notify_cron`, default `*/5`) resta in `sync-config.json`.

**File aggiunti per la 5d:**

| File | Role |
|------|------|
| `services/autofatturiamo-sync/scarti_notify.py` | Motore: `process()` (entry del job), seed, debounce+cap, dispatch consegna (`_deliver_via_agent` / `_deliver_via_discord`), `_build_agent_prompt`, helper `_format_batch`/`_fallback_summary`. |
| `services/autofatturiamo-sync/claude_client.py` | Wrapper Anthropic (`summarize_scarti`, `is_configured`) con fallback `None` — usato solo in modalità `discord`. |
| `services/autofatturiamo-sync/templates/impostazioni.html` | Pagina Impostazioni (card "Notifiche scarti SDI" + blocco "Consegna": modalità agent/discord + dropdown chat target). |
| `SCHEMA_APP_SETTINGS` + helper + `_list_agent_chats()` + route `/impostazioni` in `app.py` | Tabella `app_settings` + accessor tipizzati + lista chat agente (da `v2.db`) + pagina (navbar `fa-gear`). |
| `scarti_notify_cron` in `scheduler.py` (`_DEFAULTS`, `_job_scarti_notify`, `_apply_config`) | Registra il job ogni 5 min. |
| `anthropic>=0.69.0` in `requirements.txt` | SDK Anthropic (pinnatura manuale, pip — niente policy `minimumReleaseAge`). |

### 5e. Ponte daemon → agente — `agent_prompt` (`src/modules/agent-prompt/index.ts`)

Generalizza il pattern "daemon Python fa elaborare un prompt da un agente Claude e ne fa consegnare la risposta", **senza API key gestita da noi** (l'agente usa il modello via gateway OneCLI). Usato da §5d in modalità `delivery='agent'`, ma riutilizzabile da qualsiasi futura feature daemon-side.

**Meccanica** (simmetrica al poll `whatsapp_reply` di §5b):
- Il daemon accoda in `actions.db` un'azione **`agent_prompt`** (status `done`) con payload `{agent_group_id, messaging_group_id, prompt}` — via `actions_db.enqueue`+`approve`+`mark_done` (nessun executor Python: la consuma il host).
- **Poll host** `startAgentPromptPoll()` (5s, registrato in `src/index.ts` tra le delivery polls): legge le righe `agent_prompt` `done` non ancora servite (`host_delivered_at IS NULL`), e per ognuna `resolveSession(agent_group, messaging_group, null, 'shared')` → `writeSessionRouting` → `writeSessionMessage` (kind `chat`, `trigger=1`, **con `platform_id`+`channel_type` del messaging group** così l'agente lo riconosce come proveniente dal chat target e risponde lì) → `wakeContainer`. Marca `host_delivered_at` via `markActionHostDelivered` (riusa il marker di `whatsapp-approval/queue.ts`).
- L'agente risponde normalmente → l'outbound è consegnato dai poll di delivery esistenti sul canale della sessione (es. il gruppo Telegram). **Rispetta "un solo writer per inbound.db"**: scrive il host, non il daemon.

**Caveat di routing** (importante): un agente "personale" come `dm-personal` ha più destinazioni (la DM del proprietario + i gruppi wired) e tende a rispondere nella DM. Per consegnare in modo deterministico in un gruppo si usa: (a) il prompt **blindato** di §5d ("solo qui, niente DM"), e (b) si punta il target su un messaging group dedicato. Stato attuale: target = gruppo **Telegram** "AFT" (`mg-1782207080118-zo33t0`, `telegram:-5441125100`), wired a `dm-personal` con `engage_pattern=\b[Aa][Ff][Tt]\b` (l'agente nel gruppo interviene su messaggi reali solo se contengono "aft"; le notifiche scarti bypassano l'engage perché iniettate direttamente).

**File:**

| File | Role |
|------|------|
| `src/modules/agent-prompt/index.ts` | Reader `agent_prompt` su `actions.db` + `startAgentPromptPoll`/`stopAgentPromptPoll` + `injectAgentPrompt` (resolveSession+writeSessionMessage+wakeContainer). |
| `src/index.ts` | Avvia `startAgentPromptPoll()` tra le delivery polls; `stopAgentPromptPoll()` in shutdown. |

### 6. Custom skills

**`/AFT-new-feature`** — user-installed Claude Code skill (not stored in this repo). Description: **"Sync the fork with upstream NanoClaw and create a branch from a GitHub issue."** Invoked from Claude Code with `/AFT-new-feature`. Use it whenever starting work on a new AFT feature that should track an issue.

**`/ppp-contesto`** — repo-local skill at `.claude/skills/ppp-contesto/SKILL.md`. Updates the AFT context from a natural-language prompt: keeps the 4 markdown files in `services/autofatturiamo-sync/context-files/` (`context.md` / `context_overview.md` / `context-client.md` / `context_overview_client.md`) aligned, and touches `CONTEXT_SECTION_REGISTRY_INTERNO` / `CONTEXT_SECTION_REGISTRY_CLIENTI` in `services/autofatturiamo-sync/app.py` only when a `## ` section is added or removed. Always shows a diff and asks for confirmation before writing. Backs up the schematic files to `*.md.bak` (single rolling backup, same convention as the `/contesto/edit` dashboard editor). Restarts the Flask process only if `app.py` was touched — the markdown files are in `_sources_mtime`, so the dashboard auto-reloads, and both bots pick up the new context on their next turn via the live mount of `context-files/`.

### 7. Security lockdown — strict agent / operator separation

Hardens the boundary between what the in-container agent can do and what only the human operator can do. The goal is structural: agents must not be able to modify NanoClaw code, AFT Python services, launchd plists, or any file outside their own session/group workspace, and they must not be able to grant themselves new capabilities.

**Self-mod tools off by default.** The `install_packages` and `add_mcp_server` MCP tools (which trigger apt/npm installs + container rebuilds and wire arbitrary MCP servers) are gated by a new boolean column `container_configs.self_mod_enabled` (default `0`). When off, the container-side MCP barrel does not even import the module — the agent's tool catalog never lists them. The host-side delivery handler also re-checks the flag and rejects stragglers from stale containers (defense in depth). Toggle per-group with `ncl groups config update --id <id> --self-mod-enabled true` and restart the container.

**`cli_scope=global` retired.** Migration 017 demotes any existing `global` row to `group`. The CLI rejects `--cli-scope global` on update. `init-first-agent` no longer assigns `global` to the owner group — administrative DB actions are expected to go through the host CLI socket (`data/cli.sock`, Unix-domain, host-only). `dispatch.ts` includes a runtime downgrade for hand-edited DB rows.

**`excluded_modules` per-group gating.** Some agent groups have a strict role (e.g. `autofatturiamo-clienti` — customer support over WhatsApp) and most NanoClaw built-in tools are not just useless to them but actively harmful: they encourage out-of-scope behaviors like spawning sub-agents, scheduling recurring tasks, sending emails, generating charts, or calling external APIs. Migration 021 adds `container_configs.excluded_modules` (JSON `string[]`, default `[]`). Each entry matches either an MCP tool basename (`agents`, `scheduling`, `interactive`, `operational-actions`, `send-chart`) or a container skill folder (`onecli-gateway`). The exclusion is applied twice for defense-in-depth: the host-side `composeGroupClaudeMd` drops the matching `.instructions.md` fragments from the composed `CLAUDE.md`, AND the container-side MCP barrel (`mcp-tools/index.ts`) replaces the static imports with conditional `await import(...)` so the tools are simply not registered. Without the second leg, the agent could discover and call an excluded tool blindly. Toggle with `ncl groups config update --id <id> --excluded-modules '["scheduling","send-chart",…]'` followed by a restart. Current production state on `autofatturiamo-clienti`: 6 modules excluded + `cli_scope=disabled`, which prunes the customer prompt from ~14 files / 27 KB down to 6 files / ~14.6 KB.

In `groups/autofatturiamo-clienti/CLAUDE.local.md` there is also a short *override block* that tells the agent the host-base sections "Workspace / Memory / Gestione allegati / Conversation history" do not apply here — those sections invite autonomous memory writing and document archival via tools the customer-support agent must not use.

**Files touched** (in addition to the two new migrations):

| File | Change |
|------|--------|
| `src/db/migrations/016-self-mod-enabled.ts` | New: adds `container_configs.self_mod_enabled INTEGER NOT NULL DEFAULT 0`. |
| `src/db/migrations/017-demote-global-cli-scope.ts` | New: `UPDATE container_configs SET cli_scope='group' WHERE cli_scope='global'`. |
| `src/db/migrations/index.ts` | Registers the two new migrations. |
| `src/types.ts` | `ContainerConfigRow.self_mod_enabled: number`. |
| `src/db/container-configs.ts` | `SCALAR_COLUMNS` + `updateContainerConfigScalars` accept `self_mod_enabled`. |
| `src/container-config.ts` | `ContainerConfig.selfModEnabled?: boolean`; `configFromDb` maps the integer to a boolean. |
| `src/backfill-container-configs.ts` | Default `self_mod_enabled: 0` on backfilled rows. |
| `src/claude-md-compose.ts` | Excludes the `self-mod.instructions.md` fragment when the flag is off (mirrors the `cli` fragment exclusion when `cli_scope=disabled`). |
| `src/modules/self-mod/request.ts` | Host-side reject if `self_mod_enabled !== 1` — keeps stale containers from queueing approvals. |
| `src/cli/resources/groups.ts` | `--self-mod-enabled <bool>` flag on `groups config update`; `--cli-scope` no longer accepts `global`; `presentConfig` surfaces the new boolean. |
| `src/cli/dispatch.ts` | Resolves `cli_scope` once and caches it for the post-handler; downgrades legacy `global` to `group` with a warn log. |
| `scripts/init-first-agent.ts` | No longer sets `cli_scope='global'` for the owner group. |
| `container/agent-runner/src/config.ts` | `RunnerConfig.selfModEnabled: boolean`. |
| `container/agent-runner/src/mcp-tools/index.ts` | Conditional `await import('./self-mod.js')` gated on `selfModEnabled`. Also wires the `excluded_modules` filter for the optional built-in modules (`scheduling`, `interactive`, `agents`, `send-chart`, `operational-actions`) — `core` and `documents` stay always-on. |
| `CLAUDE.md` (root) | Documents the `self_mod_enabled` flag and the retirement of `cli_scope=global`. |
| `src/db/migrations/021-excluded-modules.ts` | New: adds `container_configs.excluded_modules TEXT NOT NULL DEFAULT '[]'`. |
| `src/container-config.ts` | `ContainerConfig.excludedModules?: string[]`; materialized into `container.json` so the container runner reads it via `loadConfig()`. |
| `src/claude-md-compose.ts` | Filters both MCP tool fragments and skill fragments through `excluded_modules` before creating `.claude-fragments/` symlinks. |
| `container/agent-runner/src/config.ts` | `RunnerConfig.excludedModules: string[]`. |
| `groups/autofatturiamo-clienti/CLAUDE.local.md` | New override block at the top explicitly nullifying the host-base Workspace/Memory/Gestione-allegati/Conversation-history sections for this customer-facing group. |

**To temporarily re-enable** (operator-only, on the host):

```bash
ncl groups config update --id <group-id> --self-mod-enabled true
ncl groups restart --id <group-id>
# …perform the install/MCP add via the agent…
ncl groups config update --id <group-id> --self-mod-enabled false
ncl groups restart --id <group-id>
```

This change does not address network egress — agents still have outbound HTTP. A separate change can layer a Docker `--internal` bridge + `host.docker.internal` allowlist if/when needed.

### 8. Topologia a due agent group (interno + client-facing) e memoria per cliente

AFT ha **due agent group separati**, ognuno con suo CLAUDE.local.md, suo contesto montato e suoi canali wired. Sostituisce il modello pregresso (un singolo agent group con fork if/else sul `sender` in `CLAUDE.local.md`).

| Agent group | Folder | Wired channels | Contesto dominio | Personalità |
|-------------|--------|----------------|-------------------|-------------|
| `dm-personal` | `groups/dm-personal/` | Telegram di Claudio (`telegram:171153272`); futuri canali dipendenti (Discord) | `context-files/context.md` (interno completo: PEC/SDI, Odoo+Stripe, ecc.) | Assistente personale di Claudio (dare del **tu**, stile diretto). |
| `autofatturiamo-clienti` (id `a2030d38-891c-4937-bc4d-05c39e8df16c`) | `groups/autofatturiamo-clienti/` | 3 numeri WhatsApp aziendali (e in futuro email cliente Resend) | `context-files/context-client.md` (sottoinsieme safe) | Dipendente del servizio "Supporto Clienti Autofatturiamo" (dare del **lei** formale, non rivela di essere AI, non menziona MAI dettagli interni: PEC, SDI come canale, Odoo, Stripe, DB). |

**Memoria per cliente (in `groups/autofatturiamo-clienti/`).** Ogni cliente è identificato univocamente dal suo `messaging_group_id` (auto-creato dal router al primo messaggio WhatsApp da quel numero — vedi §5b). Per ogni cliente esistono — quando popolati — due artefatti di memoria, montati RO nel container client-facing:

- **`customer-context/<messaging_group_id>.md`** — scheda con lo stato dinamico del cliente: riassunto di cosa è successo, problemi aperti/risolti, preferenze, fatti operativi rilevanti. Letta dall'agent all'inizio di ogni nuova conversazione (istruzioni in `groups/autofatturiamo-clienti/CLAUDE.local.md`: leggi via `Read` su `/workspace/extra/autofatturiamo/customer-context/<id>.md`; se manca, ignora). Path container: `/workspace/extra/autofatturiamo/customer-context/`.
- **`customer-events/<messaging_group_id>/*.md`** — file ausiliari di dettaglio (es. micro-riassunti di meeting BlueDot, log di ticket, ecc.) che l'agent apre **on-demand** via `Read` quando la conversazione lo richiede e il customer-context principale ne cita uno. Path container: `/workspace/extra/autofatturiamo/customer-events/`.

**L'agent è read-only su entrambi.** L'aggiornamento del customer-context avviene **sempre fuori dalla conversazione** — per ora a mano via filesystem o futuro editor dashboard; a regime una composizione dinamica da più fonti (eventi DB Autofatturiamo, riassunti BlueDot, ticket, ecc.). Nessun tool MCP che scrive: l'agent vede al prossimo turno qualunque modifica venga fatta esternamente, senza side effect introdotti dall'LLM. Vedi memoria globale `feedback_no_agent_side_effects_for_external_state` per il rationale.

**Risoluzione del messaging_group_id dentro il container.** `src/container-runner.ts` espone `NANOCLAW_MESSAGING_GROUP_ID` come env var per ogni spawn (basato su `session.messaging_group_id`). L'agent la recupera con `Bash echo "$NANOCLAW_MESSAGING_GROUP_ID"` al primo turno della conversazione, poi compone il path del file customer-context da leggere.

**Auto-wire dei nuovi clienti.** `defaultWhatsappAgentGroupId()` (vedi §5b) cerca il primo agent group con un wiring WhatsApp esistente — dopo il cutover dell'azienda al modello 2-agent, è sempre `autofatturiamo-clienti`. Quindi ogni numero esterno nuovo che ci scrive viene auto-wired al gruppo client-facing senza intervento manuale.

**Storia conversazionale.** Le sessioni del modello pre-cutover (1-agent) restano su disco in `data/v2-sessions/<dm-personal-id>/...` ma non sono più viste dal nuovo agent: i clienti che riprendono una chat dopo il cutover partono con una sessione fresca su `autofatturiamo-clienti` (limitazione accettata in fase MVP — non c'è migrazione automatica di `outbound.db`).

**Files toccati per la topologia:**

| File | Cambio |
|------|--------|
| `groups/dm-personal/CLAUDE.local.md` | Semplificato: rimossa la sezione "Identità per canale di provenienza" e tutta la fork "dipendente Autofatturiamo verso non-whitelist". Resta solo l'identità Claudio. |
| `groups/autofatturiamo-clienti/CLAUDE.local.md` | Nuovo: personalità "Supporto Clienti", import `@../extra/autofatturiamo/context-files/context-client.md`, istruzioni per leggere `customer-context/<id>.md` all'inizio di ogni conversazione. |
| `groups/autofatturiamo-clienti/customer-context/` | Directory host-side; una entry per cliente, popolata dall'esterno. Montata RO nel container. |
| `groups/autofatturiamo-clienti/customer-events/` | Directory host-side; sotto-cartella per cliente con file di dettaglio. Montata RO nel container. |
| `src/container-runner.ts` | Aggiunto `NANOCLAW_MESSAGING_GROUP_ID` tra le env var passate al container al spawn. Threaded da `session.messaging_group_id`. |
| `services/autofatturiamo-sync/app.py` | Doppio registry (`CONTEXT_SECTION_REGISTRY_INTERNO` / `_CLIENTI`); route `/contesto/edit` parametrica via `?audience=...`; `_sources_mtime` include anche i due file `*_client.md`. |
| `services/autofatturiamo-sync/templates/contesto_edit.html` | Tab Interno/Cliente, hidden input `audience`, backup name dinamico. |
| `.claude/skills/ppp-contesto/SKILL.md` | Skill aggiornata per gestire i 4 file (interno + cliente, schematico + narrativo) e la regola di scelta del target. |

**Per cambiare la mappa canali → agent group:**

```bash
# Esempio: spostare un wiring esistente al nuovo agent group
ncl wirings delete --id <mga-id>
ncl wirings create --messaging-group-id <mg-id> --agent-group-id <agent-group-id> \
  --engage-mode pattern --engage-pattern "." \
  --sender-scope <all|known> --ignored-message-policy <drop|accumulate> \
  --session-mode shared
```

---

### 9. Messaggio «Stato azienda» (settimanale + mensile) — `weekly_summary.py` + `/summary`

Riassunto deterministico dello stato azienda inviato su Telegram, **senza** passare per un agente Claude (sostituisce il vecchio task NanoClaw agent-driven del giovedì). Stessa funzione condivisa da due cron + comando on-demand. Due varianti (flag `monthly`): **settimanale** (finestra ultimi `window_days` giorni) e **mensile** (tutto il mese solare precedente, es. l'1° luglio → giugno).

| File | Ruolo |
|------|-------|
| `services/autofatturiamo-sync/weekly_summary.py` | Cuore della feature. `build_summary_markdown(monthly=False)` calcola le 3 sezioni e compone il testo in **Rich Markdown** (heading `##`; tabelle native per Distribution ed Economics, elenco puntato per Development); `invia_summary(chat_id, monthly=False)` lo manda con i **Rich Messages** Telegram (Bot API 10.1, giu 2026) via il metodo **`sendRichMessage`** (`rich_message.markdown`), con **fallback** a `sendMessage` testuale se il rich fallisce. Il token Telegram è letto dal **`.env` di root** (non quello locale del servizio sync) con override `os.environ`. Non solleva mai: errori loggati e riportati nel dict. |
| `scheduler.py` (`_job_summary_weekly` `summary_weekly_cron = "0 9 * * 4"`; `_job_summary_monthly` `summary_monthly_cron = "0 9 1 * *"`, `monthly=True`) | Cron giovedì 09:00 e 1° del mese 09:00 (fuso locale) → `invia_summary(summary_chat_id, ...)`. Registrati in `_apply_config` come gli altri job. |
| `app.py` (`/api/internal/summary/preview` GET, `/api/internal/summary/send` POST) | Route **esenti da login** (prefisso `/api/internal/`). `preview` ritorna il Rich Markdown senza inviare; `send` costruisce e invia al `chat_id` del body. Entrambe accettano `monthly` e `now` (override data, per testare il mensile). Config letta da `_summary_config()`. |
| `app.py` (`/api/internal/summary/live` GET, `_summary_live_html()`) + `weekly_summary.summary_live_data()` + `templates/_summary_live.html` | Blocco **«Stato azienda» live** in cima alla Dashboard (`templates/dashboard.html`): **tabellina compatta** (non il testo completo del messaggio Telegram) coi soli numeri principali — 📅 demo prenotate, 🚀 nuovi clienti, 💻 **numero** di PR integrate (senza titoli), 💶 ricavi del **solo mese corrente** con la **proiezione** come valore principale e, sotto in piccolo, il **reale a oggi** (maturato). `summary_live_data()` torna il dict (variante settimanale, riusa gli helper di §9), reso col partial `_summary_live.html`. La home lo carica **lazy** (fetch JS). Cache in-process **TTL 10 min** (`_summary_live_cache`) perché serve una chiamata di rete a GitHub; su errore degrada all'ultima versione buona, e se non c'è ancora nulla il client nasconde il blocco. |
| `app.py` (`_snapshot_ricavi_mese_corrente`, tabella `ricavi_mensili`) | Alla fine di `sync_upcoming_invoices()` (quindi ogni `sync_all` orario) congela in `ricavi_mensili(ym)` la **proiezione fine-periodo del mese corrente** da `_ricavi_overview()`. Mentre il mese è in corso la riga si aggiorna; quando il mese cambia resta congelata (≈ totale del mese). È **l'unica** fonte da cui il report mensile dell'1° conosce il ricavo del mese chiuso, il cui incasso Stripe avviene durante il mese che inizia (e quindi non è ancora in `stripe_charges`). |
| `src/channels/telegram.ts` (`handleSummary`, `COMMAND_RE`) | Comando `/summary` (solo **settimanale**): l'host Node fa solo da trigger → `POST http://127.0.0.1:5001/api/internal/summary/send` col `chat_id` della chat corrente (timeout 60s). Build + invio (incluso il rendering rich) vivono interamente lato Flask, così cron e comando non divergono. Su Dashboard giù manda un fallback testuale. |

**Le 3 sezioni** (la finestra è gli ultimi `window_days` giorni nel settimanale, tutto il mese solare precedente nel mensile):

- **🚀 Distribution** — riga `📅 Demo prenotate: **N**` (meeting cal.com **prenotati** nella finestra) + tabella `Cliente | Data` dei nuovi clienti paganti nella finestra: `stripe_subscriptions` `active`/`trialing`, `is_error=0`. Zero clienti → `⚠️ **Nessun cliente onboardato**`. Il conteggio demo (`weekly_summary._demo_prenotate`) usa `gcal_events` con `cal_booking_id` non nullo e `created` (data di **prenotazione**, non di svolgimento) nella finestra, escludendo gli annullati (`status='CANCELLED'`).
- **💻 Development** — **elenco puntato** delle PR integrate (merged) nel repo prodotto (`summary_dev_repo`, default `focolarestudio/autofatturiamo`) nella finestra, una riga per **titolo** PR (ordinate per merge più recente). Sorgente: **API GitHub GraphQL** col PAT read-only `GITHUB_SUMMARY_TOKEN` (`.env` di root, query `repository.pullRequests` con `number title mergedAt`); se assente o senza accesso al repo → **fallback CLI `gh`** (`gh pr list --json number,title,mergedAt`); se anche `gh` fallisce → "Dati GitHub non disponibili"; nessuna PR nel periodo → "Nessuna PR integrata nel periodo". ⚠ Il PAT deve avere **Resource owner = `focolarestudio`** (org) per vedere il repo privato: un token su account personale dà `NOT_FOUND` (oggi funziona via fallback `gh`).
- **💶 Economics** — billing **posticipato a consumo**: il ricavo del mese M (consumo) = incassi Stripe del mese **M+1** (`stripe_charges` succeeded/paid non rimborsate, per mese solare, ritardatari inclusi). Importi lordo IVA, euro interi.
  - *Settimanale*: tabella `Mese | Ricavi` con gli ultimi 3 mesi conclusi **+ il mese corrente come ultima riga** `Mese (in corso)` = `maturato · proiez. fine periodo` da `_ricavi_overview()`.
  - *Mensile*: **solo il mese chiuso** come **proiezione** del totale (`_proiezione_mese` legge `ricavi_mensili`) — l'1° del mese l'incasso non è ancora avvenuto, quindi mostriamo la proiezione congelata, non il maturato (che sarebbe ~0). Se lo snapshot non c'è ancora → "n/d".

> **Rich Messages** (Bot API 10.1): `InputRichMessage` richiede *esattamente uno* tra `markdown` e `html`. Usiamo `markdown` → heading `##` e tabelle markdown (`| col | col |` con allineamento `:---:`) rese **native** in tutte le app Telegram. Niente più pseudo-tabelle in monospazio.

**Per cambiarne destinazione/cadenza/repo:** edita `sync-config.json` (hot-reload 60s): `summary_chat_id` (gruppo Telegram **"AFT"** = `-5441125100`), `summary_weekly_cron`, `summary_monthly_cron`, `summary_dev_repo`, `summary_window_days`.

> ⚠ **Cold-start dello snapshot `ricavi_mensili`**: si popola solo dai sync orari successivi al deploy. Il primo report mensile mostra la proiezione corretta solo se almeno un `sync_all` è girato durante il mese da rendicontare (per il 1° luglio basta un qualunque sync di giugno).

### 9-bis. Resoconto sviluppo AI — `development_summary.py` + `/development`

Comando Telegram **`/development`** che, a differenza di `/summary` (numeri/tabelle deterministici), produce un **riassunto in linguaggio non tecnico** delle modifiche alla piattaforma: raccoglie le PR integrate (merged) nella finestra **con titolo + descrizione**, le mette tutte insieme e le fa riassumere a **Claude (Opus)**, che spiega al team aziendale *cosa è stato fatto, quali problemi risolti, quali miglioramenti*. Inviato come **Rich Message** Telegram (stesso canale del summary).

> 🔒 **Regola — niente API key.** Il riassunto NON usa `ANTHROPIC_API_KEY`: Claude è invocato **via gateway OneCLI**, lo stesso meccanismo della chat. `claude_client.claude_gateway()` esegue `onecli run -- claude -p "<prompt>" --model claude-opus-4-8` (proxy gateway + `CLAUDE_CODE_OAUTH_TOKEN` iniettati da `onecli`, eseguito in una cwd neutra per non caricare CLAUDE.md/.mcp.json di progetto) e ne cattura lo stdout. La key nel `.env` è volutamente vuota: **non popolarla mai** per nuove feature LLM.

| File | Ruolo |
|------|-------|
| `services/autofatturiamo-sync/development_summary.py` | `build_development_markdown(days=7, month=False)` → titolo `# 💻 Development • <periodo>` + il resoconto AI; `invia_development(chat_id, ...)` lo invia via `weekly_summary.invia_markdown` (Rich Message, fallback testuale). Variante **a giorni** (`gli ultimi N giorni`) o **mensile** (`month=True`, mese in corso → nome del mese, es. "Giugno"). Riusa `weekly_summary._dev_stats` (ora con `body` delle PR) e, se l'AI non è disponibile, fa fallback a un **elenco dei titoli**. |
| `claude_client.py` (`claude_gateway`, `summarize_development`) | `claude_gateway(prompt, model)` = invocazione Claude via gateway OneCLI (no API key, timeout 180s). `summarize_development(periodo, prs_bundle)` costruisce il system prompt (pubblico non tecnico, raggruppa per tema, Rich Markdown: solo grassetto + elenchi) e chiama il gateway. |
| `app.py` (`/api/internal/development/preview` GET, `/api/internal/development/send` POST) | Route **esenti da login**. `preview` ritorna il markdown senza inviare (⚠ chiama comunque Opus); `send` costruisce e invia al `chat_id` del body. Param: `days` (1–365, default 7) **oppure** `month` (bool → mese in corso); `now` override data (test). |
| `src/channels/telegram.ts` (`handleDevelopment`, `COMMAND_RE`) | Comando `/development [arg]`: l'host fa da trigger → `POST /api/internal/development/send` (timeout **210s**, perché la build chiama GitHub + Opus). Manda un ack `⏳` prima di attendere. Arg: un numero → `days`; `mese`/`mensile`/`month` → variante mensile; assente → ultimi 7 giorni. Su Dashboard giù → fallback `⚠️`. |

Le PR si recuperano come in §9 (API GitHub GraphQL col PAT `GITHUB_SUMMARY_TOKEN`, fallback CLI `gh`) — qui la query/`--json` includono anche `body`. Registrato nel menu del bot via `setMyCommands` (curl). **Nessun cron**: è solo on-demand (a differenza di `/summary` che ha anche i job settimanale/mensile).

---

## Local development quick reference

```bash
# Upstream NanoClaw host (Node + pnpm)
pnpm run dev
pnpm test

# AFT sync daemon — first time
cd services/autofatturiamo-sync
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in Stripe / Odoo / IMAP creds

# Run the daemon by hand (headless, what launchd runs)
python3 sync_only.py

# Or run the optional Flask UI on http://localhost:5000
python3 app.py
```

---

## What to update when adding AFT features

Whenever you add or change AFT-specific code, edit this file in the same change:

- **New file under `services/autofatturiamo-sync/`** → add a row to the file table in §1.
- **New SQLite table or sync source** → update the "Data sources → SQLite tables" list in §1.
- **New scheduled job or cadence change** → update the scheduled-jobs table in §1.
- **New env var the daemon reads** → update the "Required environment variables" list in §1.
- **New `src/channels/telegram*.ts` file or behavior change** → update §3.
- **New edit to an inherited NanoClaw file** → add to §4.
- **New operational `action_type`** → register it (executor + MCP tool) and update §5 (registered action types + add-a-new-action-type checklist).
- **New whitelist channel (oltre WhatsApp) or change to the approval-gate behavior** → update §5b, `src/modules/trusted-handles/index.ts:normalizeHandle`, e — se serve un nuovo array hard-coded — un nuovo file `src/config/trusted-<channel>-handles.ts`.
- **Aggiungere/rimuovere un dipendente dalla whitelist WhatsApp** → edita `src/config/trusted-whatsapp-handles.ts`, `pnpm run build`, restart del host. Niente DB, niente CLI.
- **New AFT-only skill or script** → mention in §6 (or a new section if needed).
- **New security hardening of an inherited file** → add a row to the file table in §7 (or extend the rationale paragraph if a new policy is introduced).
- **Cambi alla topologia 2-agent (wirings, personalità, contesto split, customer-context schema)** → §8.
