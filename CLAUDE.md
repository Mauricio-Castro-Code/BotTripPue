# CLAUDE.md — PueblTrips WhatsApp Bot

## Propósito del proyecto

Bot de WhatsApp para **PUEBLA TRAVEL TRIPS**, agencia de viajes en Puebla. El bot atiende clientes en WhatsApp y realiza las siguientes funciones:

- Responder preguntas frecuentes: destinos, precios, qué incluye cada paquete, formas de pago
- Filtrar y calificar leads (detectar si hay interés real)
- Derivar al agente humano cuando el cliente quiere reservar o necesita atención personalizada

**No hay generación de PDF, cotizaciones formales ni flujo de confirmación.** El bot es un asistente informativo conversacional.

## Base del código

El proyecto está **adaptado de BotCotizar** (bot de cotización para Alquiladora Crystal). Gran parte de la lógica original (PDF, cotizaciones, inventario de equipo, flujo de autorización) debe eliminarse y reemplazarse por un flujo conversacional simple.

Archivos con referencias al negocio anterior que hay que migrar:
- [app/main.py](app/main.py) — título de la app, mensajes de bienvenida, flujo
- [app/services.py](app/services.py) — lógica de negocio y prompts a OpenAI
- [app/schemas.py](app/schemas.py) — eliminar `DatosRenta`; el bot no acumula datos en un schema estructurado
- [app/models.py](app/models.py) — eliminar modelos de cotizaciones/inventario que no aplican
- [data/catalogo.py](data/catalogo.py) — reemplazar catálogo de muebles con info de paquetes de viaje
- [db/schema.sql](db/schema.sql) — simplificar; solo necesitamos sesiones y registro de leads

## Stack tecnológico

| Componente | Tecnología |
|---|---|
| Framework API | FastAPI + Uvicorn |
| BD | PostgreSQL + SQLAlchemy 2.x |
| IA | OpenAI (GPT-4o) — respuestas conversacionales |
| Mensajería | Meta WhatsApp Business API (Cloud API v20) |
| Validación | Pydantic v2 + pydantic-settings |

Dependencias a eliminar: `fpdf2`, `openpyxl` (no hay PDF ni Excel).

## Arquitectura

```
WhatsApp → Meta Webhook → POST /webhook (FastAPI)
                              │
                    _procesar_payload()
                              │
                    _procesar_mensaje_texto()
                      ├── es_saludo() → bienvenida + menú de destinos
                      ├── OpenAI (contexto conversacional) → respuesta informativa
                      └── detectar_interes_reserva() → derivar a agente humano
```

### Flujo de conversación

1. Cliente manda mensaje → bot saluda y presenta los destinos disponibles
2. Cliente pregunta sobre precios, fechas, qué incluye, formas de pago → OpenAI responde usando el catálogo de paquetes como contexto
3. Si el cliente muestra intención de reservar → bot indica que un asesor lo contactará y cierra la sesión
4. Cualquier cosa fuera del scope de viajes → responder amablemente y redirigir

### Gestión de sesión

- Una sesión activa por `telefono_cliente`
- El historial de mensajes se acumula en `sesiones_ia.contexto_actual` (JSONB) para dar contexto a OpenAI
- Teléfonos en `BLOCKED_PHONES` → bot ignora completamente

## Estructura de archivos

```
PueblTrips/
├── app/
│   ├── main.py        # Endpoints FastAPI + orquestación del flujo
│   ├── services.py    # Lógica: OpenAI, WhatsApp API, sesiones
│   ├── models.py      # Modelos SQLAlchemy (sesiones, leads)
│   ├── schemas.py     # Pydantic: payload de WhatsApp
│   ├── config.py      # Settings desde .env (pydantic-settings)
│   └── database.py    # Engine SQLAlchemy + get_db()
├── data/
│   └── paquetes.py    # FUENTE DE VERDAD: info de destinos y paquetes
│                        # Se inyecta como contexto al prompt de OpenAI
├── db/
│   └── schema.sql     # DDL PostgreSQL
├── assets/
│   └── logo.png
├── .env.example
└── requirements.txt
```

## Variables de entorno (.env)

```
DATABASE_URL=postgresql://usuario@localhost:5432/puebltrips
OPENAI_API_KEY=sk-...
WHATSAPP_TOKEN=EAAxxxxxxx
WHATSAPP_VERIFY_TOKEN=mi_token
WHATSAPP_PHONE_NUMBER_ID=123456
BLOCKED_PHONES=521XXXXXXXXXX
```

## Convenciones de código

- **Sin comentarios obvios** — solo cuando el "por qué" no es evidente
- **Español** para variables de dominio (sesion, viaje, paquete, negocio)
- **Inglés** para términos técnicos (handler, payload, parser)
- Los endpoints siempre devuelven HTTP 200 a Meta (para evitar reintentos), los errores se loguean
- El bot **solo procesa mensajes de texto**; adjuntos/audio/stickers se ignoran

## Reglas de negocio

- Los teléfonos en `BLOCKED_PHONES` nunca reciben respuesta
- Si el cliente quiere reservar → responder que un asesor lo contactará pronto; no automatizar reservas
- El bot nunca inventa precios ni destinos — solo usa lo que está en `data/paquetes.py`
- Timezone: `America/Mexico_City`

## Comandos útiles

```bash
# Desarrollo
uvicorn app.main:app --reload

# Exponer para pruebas de webhook
ngrok http 8000
```

## Estado actual

Migración completada. El bot está listo para conectar con Supabase y Meta.

Pendiente antes de producción:
- [ ] Llenar [data/paquetes.py](data/paquetes.py) con los paquetes y precios reales
- [ ] Correr [db/schema.sql](db/schema.sql) en Supabase SQL Editor
- [ ] Completar `.env` con los tokens de Meta y Supabase
- [ ] Configurar el webhook en Meta Developers apuntando a la URL pública del servidor
- [ ] Borrar archivos obsoletos: `data/catalogo.py`, `scripts/`, `app/__init__.py` si está vacío
