# 🤖 Automatización Diaria de Historias para PsyHub

Esta carpeta contiene todo el sistema que busca papers científicos automáticamente, los analiza con IA y genera las historias diarias para la sección **Explorar** de PsyHub.

---

## 🧭 ¿Cómo funciona?

1. **GitHub Actions** despierta una computadora en la nube todos los días a las 05:00 UTC (o cuando tú toques el botón "Run workflow").
2. Consulta la API pública de **OpenAlex** y descarga los 5 papers más recientes y citados de 18 tópicos de psicología clínica, cognitiva y emocional.
3. Se conecta a **Groq (GPT-OSS 120B / 20B)** con tu API Key gratuita.
4. Groq selecciona el paper más llamativo y redacta la "placa" en formato *snackable* (gancho, hallazgo, conclusión y links al paper).
5. Se guarda en `data/stories.json` y se sube automáticamente a tu repositorio de GitHub.
6. La app PsyHub lee ese archivo y muestra las historias actualizadas a todos los usuarios.

---

## 🚀 Guía Paso a Paso (Para Principiantes)

### Paso 1: Crear tu cuenta en GitHub (si no tienes una)
1. Ve a [https://github.com](https://github.com) y regístrate con tu correo.
2. Crea un nuevo repositorio (haz clic en el botón verde **"New"**):
   - **Repository name**: `psihub` (o el nombre que prefieras).
   - **Visibility**: Elige **Public** (así tu app puede leer el archivo `stories.json` sin pagar nada ni configurar servidores).
   - Haz clic en **Create repository**.

---

### Paso 2: Subir tu proyecto a GitHub
Puedes hacerlo desde tu terminal (PowerShell) dentro de la carpeta del proyecto:

```powershell
# 1. Inicializar git si no lo hiciste
git init

# 2. Agregar todos los archivos
git add .

# 3. Crear el primer commit
git commit -m "feat: app con historias diarias y automatización"

# 4. Cambiar a rama principal
git branch -M main

# 5. Conectar con tu repositorio (reemplaza 'tu-usuario' por tu usuario real de GitHub)
git remote add origin https://github.com/tu-usuario/psihub.git

# 6. Subir todo
git push -u origin main
```

*(Si prefieres no usar la terminal, también puedes descargar la aplicación oficial **GitHub Desktop** desde [desktop.github.com](https://desktop.github.com), que es 100% visual y con botones).*

---

### Paso 3: Configurar tu API Key de Groq en GitHub
Para que GitHub Actions pueda usar tu cuenta gratuita de Groq sin exponer tu clave públicamente:

1. Entra a tu repositorio en **GitHub**.
2. Haz clic en la pestaña **Settings** (Configuración) arriba a la derecha.
3. En el menú de la izquierda, busca **Secrets and variables** y haz clic en **Actions**.
4. Haz clic en el botón verde **"New repository secret"**.
5. En **Name**, escribe exactamente: `GROQ_API_KEY`
6. En **Secret**, pega la clave que obtuviste en [console.groq.com](https://console.groq.com/keys).
7. Haz clic en **Add secret**. ¡Listo!

---

### Paso 4: Probar la Automatización con un Clic
¡No tienes que esperar a mañana a las 5 AM para probarlo!

1. En tu repositorio de GitHub, ve a la pestaña **Actions** arriba.
2. En la lista izquierda, verás: **"🧬 Curaduría Diaria de Historias (Groq + OpenAlex)"**.
3. Haz clic sobre ella y verás un botón a la derecha que dice **"Run workflow"**.
4. Presiona el botón verde **Run workflow**.
5. Verás cómo se inicia la tarea. Al cabo de ~20-30 segundos, quedará con un tilde verde `✓` y habrá actualizado `data/stories.json` con los papers del día.

---

### Paso 5: ¿Cómo lee la App los datos de GitHub?
En `app.js`, la app ya está programada para cargar primero los datos locales (`data/stories.json`), y si configuras tu usuario de GitHub en la variable:
```javascript
const GITHUB_STORIES_URL = 'https://raw.githubusercontent.com/TU_USUARIO/TU_REPO/main/data/stories.json';
```
La app descargará automáticamente las historias frescas del día cada vez que el usuario abra la sección Explorar con internet.

---

## 🛠️ Cómo probar el script en tu propia computadora (Opcional)

Si quieres ejecutar la curaduría directamente en tu máquina:

En **PowerShell**:
```powershell
$env:GROQ_API_KEY="gsk_tu_clave_aqui"
node automation/generate_stories.js
```
Verás en la consola en tiempo real cómo busca en OpenAlex y cómo Groq redacta cada una de las 6 historias.
