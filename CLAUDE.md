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

State is encoded as a `{"role": "meta", "estado": "<state>"}` entry prepended to the `historial` JSONB array (alongside normal OpenAI `user`/`assistant` messages). `get_estado()` reads it; `set_estado()` replaces it.

States:
| State | Meaning |
|---|---|
| `menu` | Initial / greeted, waiting for topic choice |
| `chat_nacional` | Client interested in domestic trips (Puebla Travel Trips) |
| `chat_internacional` | Client interested in international trips (LibertYa) |
| `chat_cliente` | Existing client with questions / payments / groups |
| `cerrada` | Session closed (no follow-ups sent) |

Only states in `_ESTADOS_CON_IA` (`chat_nacional`, `chat_internacional`, `chat_cliente`) send further messages through OpenAI and trigger interactive booking buttons after each reply.

### Intent classifier

`clasificar_intencion(texto, estado)` calls `gpt-4o-mini` with `temperature=0` to classify into one of 8 intents. When already in a `chat_*` state, almost everything should classify as `continuar` (rule enforced in the system prompt). On API failure, falls back to `continuar`.

### Two-advisor routing

Each session state maps to an advisor number:
- `chat_nacional` → `NUMERO_ASESOR_NACIONAL` (Puebla Travel Trips)
- everything else → `NUMERO_ASESOR_INTERNACIONAL` (LibertYa)

Buttons `btn_reservar` / `btn_asesor` send a WhatsApp deep link to the appropriate advisor.

## Data layer

**`data/viajes.json`** — the actual source of truth. Each entry has:
```json
{
  "tipo": "nacional" | "internacional",
  "destino": "...", "fecha_salida": "...", "salidas": "...",
  "no_dias": "...", "precio": "...", "transporte": "...",
  "incluye": ["..."], "reserva_con": "..."
}
```

**`data/paquetes.py`** — reads `viajes.json` on every call (no caching, so edits to the JSON are reflected without restart). Exports:
- `get_resumen_nacionales()` / `get_top10_internacionales()` — short lists for the initial menu response
- `get_contexto_paquetes()` — full detail injected as context into every OpenAI call
- `METODOS_PAGO`, `REQUISITOS` — static strings

## Database

Two tables (see [db/schema.sql](db/schema.sql)):
- **`sesiones_ia`** — one row per phone; `historial` JSONB stores full conversation; `seguimiento_1h` / `seguimiento_3d` track follow-up timestamps
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
- Reservations are never automated — `btn_reservar` sends a link to a human advisor
- Sessions auto-close 2h after the 24h warning if no reply is received
