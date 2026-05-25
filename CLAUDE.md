# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project purpose

WhatsApp bot for **two travel agencies** sharing the same codebase:
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
```

Deployment uses a `Procfile` (`web: uvicorn app.main:app --host 0.0.0.0 --port $PORT`). There are no automated tests.

## Architecture

```
WhatsApp → Meta Webhook → POST /webhook
                              │
                    _procesar_payload()   ← handles text + interactive button replies
                              │
                    _procesar_mensaje()
                      ├── blocked? → ignore
                      ├── clasificar_intencion()  [gpt-4o-mini, temp=0]
                      │     └── returns: saludo | despedida | no_interesado
                      │                 menu_1..4 | continuar
                      ├── state machine transition
                      └── generar_respuesta_ia()  [gpt-4o-mini, function calling]

APScheduler (every 5 min) → procesar_seguimientos()
                              ├── 8h inactivity  → follow-up with 3-button interactive
                              ├── 24h inactivity → close-warning with 2h deadline
                              └── 2h after warning → auto-close session
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

Four button IDs are used across the app:
- `btn_reservar` — triggers `_mensaje_derivar()` with advisor deep link
- `btn_terminar` / `btn_no_interes` — close the session and send farewell
- `btn_seguir` — resets to `menu` state and shows `_MENU_OPCIONES` (only in 8h follow-up)

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
- **`sesiones_ia`** — one row per phone; `historial` JSONB stores full conversation; `seguimiento_1h` tracks when the 8h follow-up was sent; `seguimiento_3d` tracks when the 24h follow-up was sent (column names are misleading relative to their semantic purpose)
- **`leads`** — one row per phone; `estatus` progresses through: `nuevo` → `informado` → `derivado_nacional` | `derivado_internacional` | `no_interesado`

## Variables de entorno

```
DATABASE_URL=postgresql://usuario@localhost:5432/puebltrips
OPENAI_API_KEY=sk-...
WHATSAPP_TOKEN=EAAxxxxxxx
WHATSAPP_VERIFY_TOKEN=mi_token
WHATSAPP_PHONE_NUMBER_ID=123456
BLOCKED_PHONES=521XXXXXXXXXX,521YYYYYYYYYY   # comma-separated
```

## Code conventions

- **Español** for domain variables (`sesion`, `viaje`, `paquete`, `seguimiento`)
- **Inglés** for technical terms (`handler`, `payload`, `parser`, `scheduler`)
- Endpoints always return HTTP 200 to Meta (errors are logged, never re-raised to the caller)
- Only text and interactive button messages are processed; all other types are silently ignored
- Timezone: `America/Mexico_City` (enforced via `ZoneInfo`)
- `historial` is capped at `_MAX_HISTORIAL = 20` messages before sending to OpenAI

## Key business rules

- Phones in `BLOCKED_PHONES` are ignored before any DB write
- The bot never fabricates prices or destinations — OpenAI only has what's in `viajes.json`
- Reservations are never automated — booking buttons send a pre-filled WhatsApp deep link to a human advisor
- Sessions auto-close 2h after the 24h warning if no reply is received
