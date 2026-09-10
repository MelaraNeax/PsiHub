# Plan de Implementación: Limpieza y Referencias al PDF Original

## 1. Filtrado de Imágenes "Basura" (Opción 1)
En `extract_markdown_and_images`, filtraremos las imágenes extraídas por `pymupdf4llm` basándonos en su tamaño en disco (ej. < 10 KB) o resolucion (leyendo los primeros bytes si es necesario, pero el tamaño en disco suele ser suficiente para descartar íconos/flechas sueltas de gráficos vectoriales).
- Si el archivo mide menos de 10KB, lo borramos y no lo incluimos en `images_b64`. 
- Luego actualizaremos `replace_image_refs_with_base64` para que reemplace cualquier referencia de imagen que **no** esté en el diccionario por un string vacío (`""`). Esto eliminará del Markdown los fragmentos de flechas e íconos no deseados.

## 2. Limpieza Algorítmica Previa (Regex)
Crearemos una función `preprocess_raw_markdown(md_text: str)` que se ejecute **antes** de enviar el texto a Gemini:
- **Números de línea marginales:** Eliminaremos líneas que contengan únicamente números sueltos, que suelen ser artefactos de los márgenes (`re.sub(r'(?m)^\s*\d+\s*$\n?', '', text)`).
- **Palabras cortadas por saltos de línea (Hyphenation):** `re.sub(r'(\w+)-\n(\w+)', r'\1\2', text)` para unir palabras como "tradu-\ncción".
- **Saltos de línea dobles innecesarios:** Normalizaremos espacios en blanco excesivos.

## 3. Paginación y Enlaces al PDF Original (El "Modo Reader")
Para que el LLM sepa en qué página del PDF se encuentra cada elemento y pueda generar enlaces `[Ver ... en PDF - pág. X](#page=X)`:
- Modificaremos la extracción de `pymupdf4llm` para usar `page_chunks=True`. Esto devuelve una lista donde cada elemento es una página del PDF.
- Ensamblaremos el Markdown inyectando una marca invisible antes de cada página: `<!-- PAGE:1 -->`, `<!-- PAGE:2 -->`, etc.
- **Actualización del Prompt de Gemini:** Le indicaremos a Gemini que utilice esos marcadores para generar los enlaces:
  - *"GRÁFICOS/FIGURAS: Debajo del título o leyenda de cada gráfico, inserta el texto `[Ver gráfico en PDF original - pág. X](#page=X)` usando el número de la última etiqueta `PAGE` vista."*
  - *"BIBLIOGRAFÍA/REFERENCIAS/DEDICATORIAS: Si llegas a la sección de Referencias o Bibliografía, NO LA TRADUZCAS. Reemplaza toda esa sección con `[Ver bibliografía en PDF original - pág. X](#page=X)` y finaliza tu respuesta allí."*

## Preguntas Abiertas
- ¿El umbral de 10 KB te parece bien para eliminar fragmentos de imagen (flechas, iconos) o preferirías que ignoremos todas las imágenes directamente y solo dejemos los enlaces de "Ver gráfico en PDF"?
- ¿Estás de acuerdo en proceder con este plan?
