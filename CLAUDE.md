# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project purpose

WhatsApp + Facebook Messenger bot for **two travel agencies** sharing the same codebase:
- **Puebla Travel Trips** — domestic routes (Mexico)
- **LibertYa** — international routes

The bot informs clients about packages and routes them to a human advisor when they want to book. No PDF generation, no formal quotes, no booking automation.

## Commands

```bash
# Start dev server
uvicorn app.main:app --reload

# Expose for webhook testing
ngrok http 8000

# Trigger follow-up job manually (requires WHATSAPP_VERIFY_TOKEN)
curl -X POST "http://localhost:8000/cron/recordatorio?token=<WHATSAPP_VERIFY_TOKEN>"

# View dashboard
curl "http://localhost:8000/dashboard?token=<WHATSAPP_VERIFY_TOKEN>"

# Broadcast to all active sessions (WhatsApp only)
curl -X POST "http://localhost:8000/admin/broadcast?token=<WHATSAPP_VERIFY_TOKEN>" \
     -H "Content-Type: application/json" -d '{"mensaje": "..."}'
```

Deployment uses a `Procfile` (`web: uvicorn app.main:app --host 0.0.0.0 --port $PORT`). There are no automated tests.

## Architecture

```
WhatsApp / Messenger → Meta Webhook → POST /webhook
                                           │
                              ┌────────────┴────────────┐
                    WhatsApp payload            Messenger payload
                   _procesar_payload_whatsapp   _procesar_payload_messenger
                              └────────────┬────────────┘
                                           │
                                 _procesar_mensaje(canal=...)
                                   ├── blocked? → ignore
                                   ├── asesor_activo? → silence bot (human handoff)
                                   ├── clasificar_intencion()  [gpt-4o-mini, temp=0]
                                   │     └── returns: saludo | despedida | no_interesado
                                   │                 menu_1..4 | continuar
                                   ├── state machine transition
                                   └── generar_respuesta_ia()  [gpt-4o-mini, function calling]

APScheduler (every 5 min) → procesar_seguimientos()
                              ├── 2h inactivity   → follow-up with 3-button interactive
                              ├── 24h inactivity  → close-warning with 2h deadline
                              ├── 2h after warning → auto-close session
                              ├── 24h after derivation → ask if advisor attended
                              └── 24h asesor inactive → reactivate bot
```

### Session state machine

State is encoded as a `{"role": "meta", "estado": "<state>", "viajes_interes": [...]}` entry prepended to the `historial` JSONB array (alongside normal OpenAI `user`/`assistant` messages). `get_estado()` reads it; `set_estado()` replaces it.

States:
| State | Meaning |
|---|---|
| `menu` | Initial / greeted, waiting for topic choice |
| `chat_nacional` | Client interested in domestic trips (Puebla Travel Trips) |
| `chat_internacional` | Client interested in international trips (LibertYa) |
| `chat_cliente` | Existing client — destination type not yet identified |
| `chat_cliente_nacional` | Existing client with a national reservation |
| `chat_cliente_internacional` | Existing client with an international reservation |
| `chat_grupo` | Group booking inquiry |
| `cerrada` | Session closed (no follow-ups sent) |

Only states in `_ESTADOS_CON_IA` (`chat_nacional`, `chat_internacional`, `chat_cliente`, `chat_cliente_nacional`, `chat_cliente_internacional`, `chat_grupo`) send further messages through OpenAI and trigger interactive booking buttons after each reply.

When a closed session receives a new message, `sesion_cerrada`, `seguimiento_1h`, and `seguimiento_3d` are reset before processing.

### `viajes_interes` tracking

The meta entry stores up to 3 specific trips a client expressed interest in (format: `"Destino (salida DD MMM, $precio)"`). These are passed to `_mensaje_derivar()` to pre-fill the WhatsApp deep link text when the client clicks a booking button.

### Intent classifier

`clasificar_intencion(texto, estado)` calls `gpt-4o-mini` with `temperature=0` to classify into one of 8 intents. When already in a `chat_*` state, almost everything should classify as `continuar` (rule enforced in the system prompt). Numeric inputs `"1"`–`"4"` are shortcut-matched directly in `menu` state without an API call. On API failure, falls back to `continuar`.

### Two-advisor routing

`_numero_asesor(estado)` uses two sets: `_ESTADOS_NACIONALES` (`chat_nacional`, `chat_cliente_nacional`) → `NUMERO_ASESOR_NACIONAL` (Puebla Travel Trips); everything else → `NUMERO_ASESOR_INTERNACIONAL` (LibertYa). Note: `chat_cliente` defaults to the international advisor until the sub-state is resolved.

`_mensaje_derivar()` builds a WhatsApp deep link (`wa.me/<numero>?text=...`) pre-filled with the client's destinations of interest, or their last question if they're an existing client.

### Interactive buttons

Six button IDs are used across the app:
- `btn_reservar` — triggers `_mensaje_derivar()` with advisor deep link; sets `asesor_activo = True`
- `btn_terminar` / `btn_no_interes` — close the session and send farewell
- `btn_seguir` — resets to `menu` state and shows `_MENU_OPCIONES` (only in 2h follow-up)
- `btn_atendido` — client confirms advisor helped; closes session (post-derivation follow-up only)
- `btn_no_atendido` — client says advisor didn't respond; re-sends derivation link

In Messenger, interactive buttons are sent as `quick_replies` instead of WhatsApp `interactive.button`.

### Human handoff (Messenger only)

When a client clicks `btn_reservar`, `asesor_activo` is set to `True` and `asesor_desde` is stamped. On Messenger, the bot silences itself for 24h to let the human advisor reply directly in the same thread. The advisor can send `/bot` from the page to manually reactivate the bot early. After 24h with no advisor activity, `procesar_seguimientos()` auto-reactivates the bot.

## Data layer

**`data/viajes.json`** — the actual source of truth. Each entry has:
```json
{
  "tipo": "nacional" | "internacional",
  "destino": "...", "salidas": "...",
  "no_dias": "...", "precio": "...", "transporte": "...",
  "incluye": ["..."], "reserva_con": "..."
}
```
Optional fields: `fechas` (list of departure dates, replaces `fecha_salida`), `estado` (geographic region), `horario_salida`, `horario_regreso` (for day excursions), `notas`, `lugares` (cities visited, for international).

**`data/paquetes.py`** — reads `viajes.json` on every call (no caching, so edits to the JSON are reflected without restart). Key exports:
- `get_resumen_nacionales()` / `get_resumen_internacionales()` — bullet-list of destinations for menu option responses
- `get_top10_internacionales()` — deduplicated top-10 international list (not currently used in the main flow)
- `get_contexto_paquetes()` — full formatted detail for all trips, injected as context into every OpenAI call
- `METODOS_PAGO`, `REQUISITOS`, `UBICACION` — static strings

Menu options 1 and 2 (`get_respuesta_opcion`) call `get_resumen_nacionales()`/`get_resumen_internacionales()`. Options 3 and 4 are served from `_RESPUESTAS_FIJAS` dict in `services.py`.

## Database

Two tables (see [db/schema.sql](db/schema.sql)):
- **`sesiones_ia`** — one row per phone/PSID; `historial` JSONB stores full conversation; `canal` stores `'whatsapp'` or `'messenger'`; `seguimiento_1h` tracks when the 2h follow-up was sent; `seguimiento_3d` tracks when the 24h follow-up was sent (column names are misleading relative to their semantic purpose); `asesor_activo` / `asesor_desde` track human handoff state
- **`leads`** — one row per phone; `estatus` progresses through: `nuevo` → `informado` → `derivado_nacional` | `derivado_internacional` | `no_interesado`

The `canal`, `asesor_activo`, `asesor_desde`, `derivado_at`, and `seguimiento_derivado` columns were added via migrations after the initial schema. Run [db/migration_01_derivado.sql](db/migration_01_derivado.sql) and the ALTER TABLE statements in `schema.sql` comments on new environments.

## Variables de entorno

```
DATABASE_URL=postgresql://usuario@localhost:5432/puebltrips
OPENAI_API_KEY=sk-...
WHATSAPP_TOKEN=EAAxxxxxxx
WHATSAPP_VERIFY_TOKEN=mi_token
WHATSAPP_PHONE_NUMBER_ID=123456
MESSENGER_PAGE_TOKEN=EAAxxxxxxx   # Page Access Token for Facebook page
FACEBOOK_APP_ID=123456            # Meta app ID (optional)
BLOCKED_PHONES=521XXXXXXXXXX,521YYYYYYYYYY   # comma-separated
```

## Code conventions

- **Español** for domain variables (`sesion`, `viaje`, `paquete`, `seguimiento`)
- **Inglés** for technical terms (`handler`, `payload`, `parser`, `scheduler`)
- Endpoints always return HTTP 200 to Meta (errors are logged, never re-raised to the caller)
- Only text and interactive button/quick-reply messages are processed; all other types are silently ignored
- Timezone: `America/Mexico_City` (enforced via `ZoneInfo`)
- `historial` is capped at `_MAX_HISTORIAL = 20` messages before sending to OpenAI
- Both channels share the same session and lead tables using phone number / PSID as the key

## Key business rules

- Phones in `BLOCKED_PHONES` are ignored before any DB write
- The bot never fabricates prices or destinations — OpenAI only has what's in `viajes.json`
- Reservations are never automated — booking buttons send a pre-filled WhatsApp deep link to a human advisor
- Sessions auto-close 2h after the 24h warning if no reply is received
- On Messenger, the bot silences itself for 24h once a client is connected to a human advisor
