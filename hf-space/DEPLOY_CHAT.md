# Deploy del Chat IA — PsiHub

El chat **no necesita un servidor ni una cuenta nuevos**: usa el mismo
servicio de Cloud Run que ya atiende la traducción de PDF y la búsqueda
bibliográfica, y las mismas API keys de DeepSeek que ya tenés configuradas.

El motor de búsqueda es **Europe PMC** (abstract, tipo de estudio, acceso
abierto verificado y XML completo del PMC). **OpenAlex** queda como respaldo
automático si Europe PMC no responde; no hay que configurar nada para eso.

- Servicio: `mi-servidor-api` (región `southamerica-east1`)
- URL: `https://mi-servidor-api-42204102300.southamerica-east1.run.app`
- Endpoint nuevo: `POST /api/chat` (streaming SSE)
- Endpoint de exportación: `POST /api/chat/pdf` (respuesta en Markdown → PDF,
  sin llamadas al modelo, costo cero)

---

## 1. API key de DeepSeek

El chat usa, en este orden:

1. `DEEPSEEK_API_CHAT` — key dedicada al chat (acepta varias separadas por coma).
2. `DEEPSEEK_API_KEY_1` … `DEEPSEEK_API_KEY_6` — las mismas de traducción.

Si ya traducís PDFs desde PsiHub, **ya tenés key** y no hay que hacer nada.
Si querés una key exclusiva para el chat (recomendado para no compartir el
límite de cuota con la traducción):

1. Entrá a <https://platform.deepseek.com/> → **API Keys** → **Create new key**.
2. Copiala (empieza con `sk-`; se muestra una sola vez).
3. Cargala en Cloud Run como `DEEPSEEK_API_CHAT` (paso 3).

> El archivo `hf-space/.env` es solo para desarrollo local. Está en
> `.gitignore` y en `.gcloudignore`: nunca se sube al repositorio ni a Cloud Run.

### Cambiar de motor: DeepSeek ⇄ Claude

Una sola variable decide quién responde. Toda la app (chat, traducción PMC,
resúmenes) cambia junta:

| `LLM_PROVIDER` | Motor | Claves |
| --- | --- | --- |
| `0` (u omitida) | DeepSeek | `DEEPSEEK_API_CHAT`, `DEEPSEEK_API_KEY_1…6` |
| `1` | Claude | `CLAUDE_CHAT_API`, `CLAUDE_TRAD_API_1`, `CLAUDE_TRAD_API_2` |

Opcionales en modo Claude:

- `CLAUDE_MODEL` — por defecto `claude-haiku-5-5`.
- `LLM_PRICE_INPUT` / `LLM_PRICE_OUTPUT` / `LLM_PRICE_CACHE_HIT` — USD por
  millón de tokens para el ledger de costos (por defecto los de Haiku 5.5).

Las claves `Trad` se reparten en círculo entre los lotes de un mismo artículo,
así que dos claves alcanzan para paralelizar la traducción. La visión de PDFs
escaneados sigue en DeepSeek: el modelo de Claude configurado aquí es de texto.

Cloud Run (ejemplo):

```bash
gcloud run services update mi-servidor-api --region southamerica-east1 \
  --update-env-vars LLM_PROVIDER=1,CLAUDE_MODEL=claude-haiku-5-5,CLAUDE_CHAT_API=sk-ant-...,CLAUDE_TRAD_API_1=sk-ant-...,CLAUDE_TRAD_API_2=sk-ant-...
```

`/health` muestra `llm_provider` y `trad_keys` para confirmar que quedó activo.
Cada motor guarda su propia caché de traducción, así que volver a `0` sigue
sirviendo los documentos ya traducidos con DeepSeek.

---

## 2. Probar localmente (opcional)

```bash
cd hf-space
pip install -r requirements.txt
uvicorn app:app --reload --port 8080
```

Abrí `http://localhost:8080/health`: debe devolver `"chat_model"` y
`"chat_keys"` con un número mayor que 0.

---

## 3. Desplegar a Cloud Run

Desde la carpeta `hf-space/`:

```bash
cd hf-space

gcloud run deploy mi-servidor-api \
  --source . \
  --region southamerica-east1 \
  --allow-unauthenticated \
  --min-instances=1 \
  --cpu-boost \
  --update-env-vars DEEPSEEK_API_CHAT=sk-tu-key-del-chat
```

> `--min-instances=1` evita el arranque en frío de ~15 s, que era la causa de
> que OpenAlex fallara solo en el primer inicio de la app. Cuesta unos centavos
> al mes y ahorra la espera en cada apertura. `--cpu-boost` acelera el arranque
> cuando la instancia igual tiene que despertar.

Si preferís mantener las keys fuera del historial de la shell, usá un
archivo de variables:

```bash
gcloud run deploy mi-servidor-api \
  --source . \
  --region southamerica-east1 \
  --allow-unauthenticated \
  --env-vars-file env.yaml
```

`env.yaml` (no lo commitees):

```yaml
DEEPSEEK_API_CHAT: "sk-..."
DEEPSEEK_API_KEY_1: "sk-..."
OPENALEX_API_KEY: "..."
```

Las variables que ya estaban configuradas se conservan; solo se agrega o
actualiza la que pasás en `--update-env-vars`.

### Variables opcionales del chat

| Variable | Default | Para qué sirve |
|---|---|---|
| `CHAT_MODEL` | `deepseek-flash` | Modelo de chat |
| `CHAT_MAX_SOURCES` | `10` | Papers enviados al modelo por respuesta |
| `CHAT_ABSTRACT_CHARS` | `460` | Recorte de cada abstract (conserva inicio, resultados y cierre) |
| `CHAT_MAX_OUTPUT_TOKENS` | `900` | Largo máximo de la respuesta de investigación |
| `CHAT_CONTINUATION_TOKENS` | `500` | Continuación que se pide si la respuesta llega al tope anterior |
| `CHAT_DEEP_MAX_SOURCES` | `22` | Fuentes en la extensión profunda (plan pago) |
| `CHAT_DEEP_ABSTRACT_CHARS` | `560` | Recorte de abstract en modo profundo |
| `CHAT_DEEP_MAX_OUTPUT_TOKENS` | `2200` | Tope de salida del modo profundo |
| `CHAT_DEEP_MIN_RELEVANCE` | `0.18` | Umbral de relevancia más estricto con 20+ fuentes |
| `CHAT_DIRECT_TOKENS` | `800` | Tope de las respuestas de charla y acompañamiento |
| `CHAT_HISTORY_MESSAGES` | `3` | Turnos de historial que se reenvían |
| `CHAT_HISTORY_CHARS` | `450` | Recorte de cada turno del historial |
| `CHAT_SEARCH_CACHE_TTL` | `1800` | Segundos de caché de búsquedas del motor |
| `CHAT_SEARCH_RESULTS` | `20` | Papers que se piden por cada consulta |
| `CHAT_ONECLICK_CANDIDATES` | `12` | PDFs que se verifican cuando el filtro es "1 clic" |
| `CHAT_MIN_RELEVANCE` | `0.14` | Umbral de relevancia para descartar papers |
| `CHAT_SEARCH_CONCURRENCY` | `4` | Consultas simultáneas contra el motor |
| `CHAT_PLANNER_TOKENS` | `550` | Tope de tokens del router/planificador |
| `CHAT_ENRICH` | `1` | Completa abstract/año/DOI con Crossref, Europe PMC y Semantic Scholar |
| `CHAT_ENRICH_CANDIDATES` | `18` | Papers candidatos que se intentan enriquecer por búsqueda |
| `CHAT_SNOWBALL_SEEDS` | `3` | Trabajos semilla cuyas referencias se expanden (`0` desactiva) |
| `CHAT_SNOWBALL_IDS` | `80` | Máximo de referencias recuperadas por búsqueda |
| `EUROPEPMC_API_KEY` | vacío | **No hace falta**: Europe PMC no emite claves ni pide registro. Si algún día se pone un proxy propio delante, la variable se envía como `apiKey`; con la API pública se ignora |
| `EUROPEPMC_BASE` | `https://www.ebi.ac.uk/europepmc/webservices/rest` | Endpoint de Europe PMC |
| `EUROPEPMC_TIMEOUT` | `20` | Segundos de espera por consulta |
| `EUROPEPMC_CACHE_TTL` | `900` | Caché de resultados del motor |
| `CHAT_FULLTEXT` | `1` | Lee el texto completo (XML del PMC) de los papers de acceso abierto |
| `CHAT_FULLTEXT_QUICK` | `4` | Papers completos leídos en la investigación rápida |
| `CHAT_FULLTEXT_DEEP` | `8` | Papers completos leídos en la investigación profunda |
| `CHAT_FULLTEXT_CHARS` | `1200` | Pasajes clave que se envían al modelo por paper completo |
| `CHAT_ABSTRACT_FILL` | `24` | Resúmenes que se completan desde el XML del PMC |

### Cómo decide y cómo busca

**Paso 0 — router (una sola llamada corta al modelo).** Antes de buscar, el
mensaje se clasifica en uno de tres modos:

| Modo | Cuándo | Qué hace |
|---|---|---|
| `chat` | Saludos, charla, preguntas sobre la app, aclaraciones | Responde directo, sin OpenAlex ni fuentes (~$0.00008) |
| `support` | Malestar personal actual, angustia, ideación suicida | Respuesta breve de acompañamiento + bloque fijo de líneas de ayuda (el servidor agrega los números, el modelo no los inventa) |
| `research` | Todo lo demás (y ante la duda, siempre) | Búsqueda bibliográfica completa |

La detección de riesgo es deliberadamente conservadora: solo se activa cuando
el mensaje es personal y actual. Una pregunta académica sobre suicidio
("¿qué factores de riesgo predicen la ideación suicida?") se investiga normal.

**Paso 1 — descomposición.** El mismo router devuelve entre 1 y 3 conceptos con
su vocabulario propio en inglés (y los conceptos vecinos con los que no deben
confundirse), 1 consulta de puente entre conceptos, 1 consulta de revisiones o
meta-análisis, palabras clave, 3-5 ejes que la respuesta debe cubrir (`outline`)
y el alcance (`psy`, o `broad` si la pregunta cruza disciplinas ajenas a
psicología/medicina).

**Paso 2 — recuperación.** Cada consulta se lanza dos veces en paralelo contra
Europe PMC (título+resumen y buscador general) y la primera consulta de cada
concepto suma una pasada ordenada por citas. A los papers de acceso abierto del
PMC se les completa el resumen desde el XML cuando falta. Los conceptos teóricos se buscan
sin filtro de campo para que entre la fenomenología. Después se expande por
referencias: se toman hasta 3 trabajos semilla (uno por concepto, más revisiones)
y se recuperan sus referencias, que es donde suelen estar los clásicos
fundacionales (~200-250 papers únicos por pregunta).

**Paso 3 — ranking.** Cada paper se puntúa por coincidencia de términos en
título, resumen y tópicos, trato directo de los constructos, citas, citas por
año, vigencia, diseño y penalización de revistas ajenas al dominio. Se reservan
cupos por concepto, por papers que unen conceptos y por revisiones, para que un
constructo abundante no desplace a otro minoritario. Se envían al modelo los
`CHAT_MAX_SOURCES` mejores, con autor, año, diseño, muestra y citas.

**Paso 4 — redacción.** El texto se transmite en streaming y las fuentes viajan
al final. La respuesta se arma en capas para que se lea rápido sin perder
matiz:

1. una o dos frases de respuesta directa con la salvedad principal;
2. **Hallazgos clave** — 3 a 5 viñetas, cada una con su cita y su nivel de
   respaldo (meta-análisis, ensayo pequeño, correlacional, teoría);
3. **Dónde la evidencia no coincide** — discrepancias, nulos o ausencias;
4. **Qué falta y qué buscar** — qué parte de la pregunta no tiene respaldo y
   qué tipo de estudio la resolvería.

Se permite una tabla Markdown compacta (máx. 4 columnas × 6 filas) cuando se
comparan 3 o más elementos. Extensión objetivo: 280-380 palabras (230 si la
pregunta es simple). El servidor añade un bloque de cobertura, las consultas
realizadas y las distinciones conceptuales, para que el modelo declare las
brechas en vez de rellenarlas, y registra en el log `[chat-citas]` si quedaron
citas fuera de rango o fuentes sin usar. Si el modelo agota `CHAT_MAX_OUTPUT_TOKENS`,
se pide una única continuación acotada en lugar de entregar el texto cortado.

### Opciones del chat (barra de escritura)

| Grupo | Opciones | Efecto |
|---|---|---|
| Extensión | Botón **Extensión** con barra de 3 puntos: **Poco / Mediano / Largo** (`size`) | Topes relativos al plan: en `basic` van de ~4 a 9 fuentes; en `pro` cada nivel pide más fuentes y palabras. El backend lo resuelve en `SIZE_PROFILES` |
| Pensar más | Interruptor (`think`) | Activa el razonamiento del modelo y la profundidad de búsqueda (más lento, más matices). Antes era el botón "Profunda" |
| Tipo de paper | Todos / Revisiones / Estudios | Agrega `PUB_TYPE` a la consulta de Europe PMC y refina la selección |
| Acceso | Todos / Gratis / 1 clic / Pago | Filtra por `is_oa` y verifica el PDF real |
| Periodo | Desde / Hasta | `PUB_YEAR:[a TO b]` |
| Cuartil | Q1-Q4 | Filtro estimado por la posición de la revista en PsiHub. **No es el SJR oficial**: Europe PMC no publica cuartiles y Scimago bloquea el acceso automático. Se etiqueta como estimado en la interfaz |
| Tipo de estudio | Meta-análisis / Revisión / ECA / Observacional / Cualitativo | Consulta por diseño + filtro de respaldo sobre las fuentes elegidas |

Los filtros viajan en `filters` y se combinan con `access`. En la interfaz el
menú muestra primero las cuatro categorías (con el valor elegido) y cada una
se abre en su propio subpanel, para no desplegar un panel larguísimo. Si un
filtro deja menos de 4 fuentes, el backend devuelve las fuentes sin filtrar en
vez de una respuesta vacía (mejor una base algo más amplia que una alucinación).

### Adjuntos (`POST /api/chat/attach`)

El composer acepta hasta **4 adjuntos** por consulta:

- **PDF** — se extrae el texto en memoria con PyMuPDF (máx. 25 MB, 80 páginas,
  80 000 caracteres). Si el PDF es escaneado (menos de 120 caracteres) se pide
  subir capturas como imágenes.
- **Imagen** (PNG/JPG/WebP) — se transcribe con DeepSeek Vision y se registra
  el costo como `kind=attachment`, `detail=image`.
- **Paper guardado** — el frontend envía título, autores, revista, DOI y
  abstract; no hay llamada extra al modelo.

El texto se reenvía dentro de `attachments` al pedir la respuesta. El backend
lo inyecta como contexto con topes (`CHAT_ATTACHMENT_MAX_CHARS` = 9 000 por
archivo, `CHAT_ATTACHMENT_TOTAL_CHARS` = 26 000 en total) y lo marca como
material del usuario, separado de las fuentes citables.

## Endpoints auxiliares (costo cero o mínimo)

| Endpoint | Qué hace |
|---|---|
| `POST /api/chat/attach` | Extrae texto de un PDF o transcribe una imagen para el chat. Procesa en memoria y no guarda el archivo |
| `POST /api/enrich` | Completa abstract, año, DOI, autores y revista faltantes usando Crossref, Europe PMC y Semantic Scholar. Sin costo de modelo (solo APIs gratuitas + caché de 7 días) |
| `POST /api/summarize` | `mode: "basic"` resume abstract y metadatos; `mode: "advanced"` + `pdf_url` descarga el PDF y resume el texto completo (~$0.001-0.003) |
| `POST /api/summarize-pdf` | Resumen avanzado con un PDF que sube el usuario (papers de pago). Campos `file` y `payload` (JSON) |
| `GET /api/work-meta` | Afiliaciones, financiamiento, licencia, volumen, ISSN y autor de correspondencia desde el XML JATS del PMC (`pmc:PMC…`, `doi` o `id`) |
| `POST /api/export` | Genera RIS o BibTeX de una referencia (`format: "ris" \| "bibtex"`); el frontend lo descarga y se importa en Zotero, Mendeley o EndNote |
| `GET /api/auth/me` | Valida un JWT de Supabase (`Authorization: Bearer …`) y devuelve `{id, email, role}`. Sin Supabase configurado responde 401 y la app sigue en modo local |

### Login con Google (Supabase, opcional)

El backend ya valida tokens (`hf-space/auth_supabase.py`) y el esquema
`hf-space/supabase_schema.sql` define `profiles`, `folders`, `saved_papers`,
`chats`, `summaries`, `translations` y `usage_events` con RLS por usuario.

Para activarlo:

1. Creá un proyecto gratuito en <https://supabase.com>. Anotá el **Project
   URL** (`https://<ref>.supabase.co`) y la **anon public key** (Settings → API
   Keys). La anon key es pública por diseño; nunca pongas la `service_role`
   en el frontend.
2. En <https://console.cloud.google.com> → APIs & Services → Credentials creá
   un **OAuth client ID** de tipo *Web application* con:
   - *Authorized JavaScript origins*: la URL del frontend (por ejemplo
     `https://melaraneax.github.io`).
   - *Authorized redirect URIs*: `https://<ref>.supabase.co/auth/v1/callback`.
   Copiá Client ID y Client Secret en Supabase → Authentication → Providers →
   **Google** y activalo.
3. Ejecutá `supabase_schema.sql` en el SQL Editor (crea `profiles`, `folders`,
   `saved_papers`, `chats`, `summaries`, `translations`, `usage_events` con RLS).
4. En Supabase → Authentication → URL Configuration poné el **Site URL** del
   frontend y agregá en *Redirect URLs* cada origen desde el que se abre la app:
   `https://melaraneax.github.io/PsiHub/`, `http://localhost:8080/` y
   `https://localhost` (Capacitor). Si falta un origen, Supabase redirige al
   Site URL y la sesión no vuelve a la app.
5. En Cloud Run configurá `SUPABASE_URL` y una de estas dos:
   `SUPABASE_JWKS_URL` (recomendado;
   `https://<ref>.supabase.co/auth/v1/.well-known/jwks.json`, funciona con las
   claves asimétricas nuevas y requiere `PyJWT[crypto]`, ya incluido en
   `requirements.txt`) o `SUPABASE_JWT_SECRET` (proyectos con secreto HS256).
6. En `index.html`, completá las metas `psyhub-supabase-url` y
   `psyhub-supabase-anon` (o definí `window.PSYHUB_SUPABASE = {url, anonKey}`
   antes de `app.js`). Después corré `npm run build` para que `www/` las tenga.
   El botón "Iniciar sesión con Google" pasa a usar OAuth real; sin esos datos
   sigue el formulario local.
7. Verificá con `/health`: `supabase_auth` debe pasar a `true`.

Los tokens vuelven en el hash, `src/services/auth.js` los guarda y limpia la
URL, y `GET /api/auth/me` los valida. Las tablas todavía no se sincronizan
desde el frontend: el login queda operativo y el backend y el esquema están
listos para hacerlo sin migrar datos locales.

---

## 4. Verificar el deploy

```bash
curl https://mi-servidor-api-42204102300.southamerica-east1.run.app/health
```

Debe incluir:

```json
{
  "status": "ok",
  "chat_model": "deepseek-flash",
  "chat_keys": 1
}
```

Y una prueba de streaming:

```bash
curl -N -X POST https://mi-servidor-api-42204102300.southamerica-east1.run.app/api/chat \
  -H "Content-Type: application/json" \
  -d '{"question":"¿Qué dice la evidencia sobre la TCC para la ansiedad?","access":"all"}'
```

Vas a ver eventos `data: {"type":"status"...}`, luego varios
`data: {"type":"delta"...}`, después `data: {"type":"sources"...}` y al final
`data: {"type":"done"...}`.

### Exportar una respuesta a PDF

El botón de descarga del chat llama a este endpoint (no usa el modelo, así que
no suma costo):

```bash
curl -X POST https://mi-servidor-api-42204102300.southamerica-east1.run.app/api/chat/pdf \
  -H "Content-Type: application/json" \
  -d '{"question":"¿TCC o fármacos?","answer":"## Respuesta breve\n\nTexto…","sources":[{"title":"Cuijpers et al.","year":2020,"journal":"JAMA Psychiatry","doi":"10.1001/..."}]}' \
  -o respuesta.pdf
```

El PDF se arma con `markdown_to_pdf()` de `exporters.py`: mismos tipos y
plantilla que el lector, respetando encabezados, listas, citas, tablas GFM y
la lista de fuentes al final. El frontend también ofrece Markdown, copiar y
compartir (Web Share API con fallback al portapapeles).

---

## 5. Costos en Google Cloud Logging

Cada respuesta genera una línea con el prefijo **`[cost-chat]`**:

```
[cost-chat] chat-1761...a | modo research | 16 fuentes | 12450 tokens (cache 8700) | $0.000412 USD | 4.21s
```

Para verla en la consola:

1. **Cloud Run** → `mi-servidor-api` → **Registros**.
2. Filtrá por `[cost-chat]`.
3. O en **Logging** → **Consulta**:

```
resource.type="cloud_run_revision"
resource.labels.service_name="mi-servidor-api"
textPayload:"[cost-chat]"
```

El mismo registro se guarda en el ledger de costos y se puede ver en:

```
https://mi-servidor-api-42204102300.southamerica-east1.run.app/costos
```

Ahí aparecen las columnas **Tipo** (`traducción` / `chat`), tokens, caché y
costo por respuesta.

---

## 6. Publicar el frontend

```bash
npm run build      # regenera www/ (incluye style-chat.css)
```

Después subí los archivos a tu hosting habitual (GitHub Pages) o sincronizá
Android:

```bash
npm run sync
```

---

## 7. Problemas frecuentes

| Síntoma | Causa / solución |
|---|---|
| `503 No hay API key de DeepSeek configurada` | Falta `DEEPSEEK_API_CHAT` o `DEEPSEEK_API_KEY_1` en Cloud Run |
| La respuesta llega de golpe, sin streaming | Un proxy intermedio está buffereando; Cloud Run ya envía `X-Accel-Buffering: no`. Evitá proxys propios |
| `El buscador alcanzó su límite de consultas` | Europe PMC limita por IP a ~10 consultas/segundo y responde 429. Se reintenta solo con espera creciente; si persiste, baja `CHAT_SEARCH_CONCURRENCY` o subí `--min-instances` (las instancias comparten IP de salida). No existe API key que lo aumente |
| El chat responde pero no cita papers | Pregunta demasiado específica; probá términos más amplios o `access: all` |
| `chat_keys: 0` en `/health` | La variable no llegó al servicio; revisá el deploy |
