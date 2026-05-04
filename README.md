# BotCotizar - Alquiladora Crystal

Bot de cotizacion por WhatsApp para renta de equipo de eventos. El proyecto recibe mensajes desde la API de WhatsApp Business (Meta), extrae datos del cliente con OpenAI, valida productos contra inventario, arma la cotizacion en PostgreSQL, genera una nota de servicio en PDF y la envia de regreso por WhatsApp.

## Que hace el proyecto

Flujo principal:

1. Meta envia un webhook a `GET /webhook` para verificar el endpoint.
2. Meta envia mensajes entrantes a `POST /webhook`.
3. La app detecta si el mensaje es:
   - un saludo,
   - una consulta de inventario,
   - o una respuesta para avanzar una cotizacion.
4. OpenAI extrae datos del mensaje actual:
   - nombre,
   - equipo,
   - fechas,
   - direccion,
   - tipo de entrega,
   - factura,
   - colores de manteleria / silla Tiffany.
5. La app guarda el contexto conversacional en `sesiones_ia` hasta completar los campos faltantes.
6. Cuando la cotizacion esta completa:
   - crea o actualiza el cliente,
   - crea la cotizacion y sus detalles,
   - calcula flete e IVA si aplica,
   - genera el PDF,
   - y lo envia al cliente por WhatsApp.

Tambien incluye logica para:

- responder preguntas tipo "que sillas manejan?",
- sugerir alternativas cuando un producto no existe en inventario,
- preguntar por modelo cuando el cliente dice solo "sillas" o "mesas",
- preguntar por color de manteles o color de silla Tiffany cuando hace falta.

## Stack

- FastAPI
- Uvicorn
- SQLAlchemy 2
- PostgreSQL
- OpenAI API
- WhatsApp Business Cloud API (Meta)
- openpyxl + LibreOffice para generar PDF desde Excel
- fpdf2 como fallback cuando no hay plantilla Excel o LibreOffice

## Estructura del proyecto

```text
app/
  main.py         # Entrypoint FastAPI y endpoints webhook/health
  config.py       # Variables de entorno
  database.py     # Engine y sesiones SQLAlchemy
  models.py       # Modelos ORM
  schemas.py      # Esquemas Pydantic
  services.py     # Logica de negocio, OpenAI, PDF y WhatsApp
data/
  catalogo.py     # Catalogo fuente de verdad del inventario
db/
  schema.sql      # Esquema PostgreSQL
scripts/
  seed_catalogo.py         # Sincroniza catalogo -> inventario
  setup_excel_template.py  # Convierte Nota.xls -> Nota.xlsx
assets/
  logo.png
  Nota.xls
  Nota.xlsx
pdf/
  # Salida de PDFs/XLSX generados
```

## Requisitos

- Python 3.13.5
- PostgreSQL
- Credenciales de OpenAI
- Credenciales de WhatsApp Business Cloud API
- LibreOffice instalado si quieres generar la nota desde la plantilla Excel

## Instalacion

Desde la raiz del proyecto:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Variables de entorno

El proyecto usa `.env` y espera estas variables:

```env
DATABASE_URL=postgresql://usuario:password@host:5432/base_de_datos
OPENAI_API_KEY=sk-...
WHATSAPP_TOKEN=EA...
WHATSAPP_VERIFY_TOKEN=tu_verify_token
WHATSAPP_PHONE_NUMBER_ID=123456789012345
NEGOCIO_ID=uuid-del-registro-en-negocios

PDF_DIR=pdf
LOGO_PATH=assets/logo.png
EXCEL_TEMPLATE_PATH=assets/Nota.xlsx
LIBREOFFICE_PATH=/Applications/LibreOffice.app/Contents/MacOS/soffice
```

Notas:

- `DATABASE_URL`: conexion a PostgreSQL.
- `OPENAI_API_KEY`: llave para extraer datos del mensaje.
- `WHATSAPP_TOKEN`: token de acceso de Meta para enviar mensajes y subir media.
- `WHATSAPP_VERIFY_TOKEN`: debe coincidir con el token configurado en Meta Webhooks.
- `WHATSAPP_PHONE_NUMBER_ID`: ID del numero de WhatsApp Business.
- `NEGOCIO_ID`: UUID del registro en la tabla `negocios` que representa a este negocio.
- `LIBREOFFICE_PATH`: solo se usa si quieres generar el PDF desde la plantilla Excel. Si no existe, el sistema cae automaticamente a `fpdf2`.

Importante:

- No subas `.env` al repositorio.
- Si un `.env` con credenciales reales ya se compartio fuera de un entorno seguro, conviene rotar las llaves de OpenAI, Meta y la conexion a base de datos.

## Configuracion de base de datos

### 1. Crear el esquema

Ejecuta el SQL:

```bash
psql -d TU_BASE -f db/schema.sql
```

### 2. Crear el negocio

El codigo espera que exista un registro en `negocios` y que su UUID este en `NEGOCIO_ID`.

Ejemplo:

```sql
INSERT INTO negocios (
  nombre,
  api_key_meta,
  verify_token,
  activo
) VALUES (
  'Alquiladora Crystal',
  'REEMPLAZAR_TOKEN_META',
  'REEMPLAZAR_VERIFY_TOKEN',
  true
)
RETURNING id;
```

Toma el `id` que regrese PostgreSQL y colocarlo en `NEGOCIO_ID`.

### 3. Cargar inventario inicial

El inventario se alimenta desde `data/catalogo.py`.

```bash
python scripts/seed_catalogo.py
```

Si editas precios, stock o productos en `data/catalogo.py`, vuelve a correr ese script.

## Configuracion de APIs

### OpenAI

La app inicializa un cliente OpenAI y usa `gpt-4o-mini` con function calling para extraer datos estructurados desde mensajes libres de WhatsApp.

No hay endpoint extra para OpenAI; basta con que `OPENAI_API_KEY` sea valida.

### Meta / WhatsApp Business Cloud API

Debes configurar en Meta Developers:

1. Un numero de WhatsApp Business.
2. Un `WHATSAPP_TOKEN` valido.
3. El `WHATSAPP_PHONE_NUMBER_ID`.
4. Un webhook publico HTTPS apuntando a:

```text
https://tu-dominio.com/webhook
```

5. El verify token debe ser exactamente el mismo que `WHATSAPP_VERIFY_TOKEN`.

La app usa:

- `GET /webhook` para verificacion inicial.
- `POST /webhook` para mensajes entrantes.

Para pruebas locales normalmente necesitas un tunel publico HTTPS, por ejemplo con ngrok o Cloudflare Tunnel, para exponer tu `localhost`.

## Plantilla Excel y PDFs

La generacion del documento funciona asi:

- Si existen `assets/Nota.xlsx` y `LIBREOFFICE_PATH`, usa la plantilla Excel y la convierte a PDF.
- Si falta la plantilla o LibreOffice, usa el generador alterno con `fpdf2`.

Si necesitas regenerar `assets/Nota.xlsx` a partir de `assets/Nota.xls`:

```bash
python scripts/setup_excel_template.py
```

## Como ejecutarlo con uvicorn

Desde la raiz del proyecto:

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Alternativa equivalente:

```bash
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Luego tendras disponible:

- API local: `http://127.0.0.1:8000`
- Swagger UI: `http://127.0.0.1:8000/docs`
- Health check: `http://127.0.0.1:8000/health`

## Orden recomendado de arranque

1. Configura `.env`.
2. Crea esquema en PostgreSQL.
3. Inserta el registro en `negocios`.
4. Ejecuta `python scripts/seed_catalogo.py`.
5. Arranca la app con:

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

6. Configura el webhook de Meta hacia `/webhook`.

## Endpoints principales

- `GET /webhook`
  - Verifica el webhook de Meta con `hub.mode`, `hub.verify_token` y `hub.challenge`.
- `POST /webhook`
  - Recibe mensajes entrantes de WhatsApp.
  - Siempre responde `200` para evitar reintentos de Meta.
- `GET /health`
  - Health check simple.

## Comandos utiles

Instalar dependencias:

```bash
pip install -r requirements.txt
```

Sembrar catalogo:

```bash
python scripts/seed_catalogo.py
```

Convertir plantilla Excel:

```bash
python scripts/setup_excel_template.py
```

Arrancar la API:

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## Que podriamos mejorar o implementar a futuro

1. Seguridad del webhook
   Validar la firma de Meta (`X-Hub-Signature-256`) para no confiar solo en el verify token.

2. Manejo de secretos
   Agregar un `.env.example`, sacar credenciales reales del repo y rotarlas si ya estuvieron expuestas.

3. Migraciones
   Incorporar Alembic para versionar cambios de schema en vez de depender solo de `db/schema.sql`.

4. Pruebas automatizadas
   Agregar tests unitarios para:
   - extraccion de datos,
   - calculo de flete,
   - preguntas faltantes,
   - match de inventario,
   - y generacion de cotizaciones.

5. Idempotencia y resiliencia
   Evitar procesar dos veces el mismo mensaje de Meta y agregar reintentos controlados para OpenAI / WhatsApp.

6. Procesamiento asincrono
   Mover generacion de PDF y envio de media a una cola (Celery, RQ, Dramatiq o similar) para no bloquear el webhook.

7. Observabilidad
   Mejorar logs estructurados, metricas, trazas y alertas de errores.

8. Estado comercial de la cotizacion
   Exponer flujo para aceptar/rechazar cotizaciones y conectar la funcion `aceptar_cotizacion` del schema para descontar stock formalmente.

9. Panel administrativo
   Crear una interfaz para:
   - editar inventario,
   - revisar conversaciones,
   - consultar cotizaciones,
   - reenviar PDFs,
   - y cambiar precios sin tocar codigo.

10. Soporte a mas tipos de mensajes
   Procesar ubicaciones, audios, imagenes o documentos entrantes, no solo texto.

11. Reglas de inventario mas avanzadas
   Reservas por fecha, control de disponibilidad por evento y prevencion de sobreventa.

12. Multi-tenant real
   Hoy ya hay base multi-tenant en schema/modelos, pero se puede endurecer la separacion por negocio y automatizar el alta de nuevos tenants.
