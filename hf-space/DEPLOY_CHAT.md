# Deploy del Chat IA — PsiHub

El chat **no necesita un servidor ni una cuenta nuevos**: usa el mismo
servicio de Cloud Run que ya atiende la traducción de PDF y el proxy de
OpenAlex, y las mismas API keys de DeepSeek que ya tenés configuradas.

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
| `CHAT_DIRECT_TOKENS` | `800` | Tope de las respuestas de charla y acompañamiento |
| `CHAT_HISTORY_MESSAGES` | `3` | Turnos de historial que se reenvían |
| `CHAT_HISTORY_CHARS` | `450` | Recorte de cada turno del historial |
| `CHAT_SEARCH_CACHE_TTL` | `1800` | Segundos de caché de búsquedas OpenAlex |
| `CHAT_SEARCH_RESULTS` | `20` | Papers que se piden por cada consulta |
| `CHAT_ONECLICK_CANDIDATES` | `12` | PDFs que se verifican cuando el filtro es "1 clic" |
| `CHAT_MIN_RELEVANCE` | `0.14` | Umbral de relevancia para descartar papers |
| `CHAT_SEARCH_CONCURRENCY` | `4` | Consultas simultáneas contra OpenAlex |
| `CHAT_PLANNER_TOKENS` | `550` | Tope de tokens del router/planificador |
| `CHAT_ENRICH` | `1` | Completa abstract/año/DOI con Crossref, Europe PMC y Semantic Scholar |
| `CHAT_ENRICH_CANDIDATES` | `18` | Papers candidatos que se intentan enriquecer por búsqueda |
| `CHAT_SNOWBALL_SEEDS` | `3` | Trabajos semilla cuyas referencias se expanden (`0` desactiva) |
| `CHAT_SNOWBALL_IDS` | `80` | Máximo de referencias recuperadas por búsqueda |

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
OpenAlex (título+resumen y buscador general) y la primera consulta de cada
concepto suma una pasada ordenada por citas. Los conceptos teóricos se buscan
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

## Endpoints auxiliares (costo cero o mínimo)

| Endpoint | Qué hace |
|---|---|
| `POST /api/enrich` | Completa abstract, año, DOI, autores y revista faltantes usando Crossref, Europe PMC y Semantic Scholar. Sin costo de modelo (solo APIs gratuitas + caché de 7 días) |
| `POST /api/summarize` | `mode: "basic"` resume abstract y metadatos; `mode: "advanced"` + `pdf_url` descarga el PDF y resume el texto completo (~$0.001-0.003) |
| `POST /api/summarize-pdf` | Resumen avanzado con un PDF que sube el usuario (papers de pago). Campos `file` y `payload` (JSON) |

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
| `OpenAlex alcanzó su límite de consultas por hoy` | Cuota diaria compartida por IP agotada; configurá `OPENALEX_API_KEY` (gratuita) para usar tu propio presupuesto |
| El chat responde pero no cita papers | Pregunta demasiado específica; probá términos más amplios o `access: all` |
| `chat_keys: 0` en `/health` | La variable no llegó al servicio; revisá el deploy |
