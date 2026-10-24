# Deploy del Chat IA — PsiHub

El chat **no necesita un servidor ni una cuenta nuevos**: usa el mismo
servicio de Cloud Run que ya atiende la traducción de PDF y el proxy de
OpenAlex, y las mismas API keys de DeepSeek que ya tenés configuradas.

- Servicio: `mi-servidor-api` (región `southamerica-east1`)
- URL: `https://mi-servidor-api-42204102300.southamerica-east1.run.app`
- Endpoint nuevo: `POST /api/chat` (streaming SSE)

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
  --update-env-vars DEEPSEEK_API_CHAT=sk-tu-key-del-chat
```

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
| `CHAT_MAX_SOURCES` | `8` | Papers enviados al modelo por respuesta |
| `CHAT_ABSTRACT_CHARS` | `900` | Recorte de cada abstract (tokens) |
| `CHAT_MAX_OUTPUT_TOKENS` | `1000` | Largo máximo de la respuesta |
| `CHAT_HISTORY_MESSAGES` | `6` | Turnos de historial que se reenvían |
| `CHAT_SEARCH_CACHE_TTL` | `1800` | Segundos de caché de búsquedas OpenAlex |

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

Vas a ver eventos `data: {"type":"sources"...}`, luego varios
`data: {"type":"delta"...}` y al final `data: {"type":"done"...}`.

---

## 5. Costos en Google Cloud Logging

Cada respuesta genera una línea con el prefijo **`[cost-chat]`**:

```
[cost-chat] chat-1761...a · 8 fuentes · 12450 tokens (cache 8700) · $0.000412 USD · 4.21s
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
| `No pude buscar en OpenAlex` | Cuota de OpenAlex agotada; configurá `OPENALEX_API_KEY` |
| El chat responde pero no cita papers | Pregunta demasiado específica; probá términos más amplios o `access: all` |
| `chat_keys: 0` en `/health` | La variable no llegó al servicio; revisá el deploy |
