---
title: PsiHub Reader
emoji: 📖
colorFrom: green
colorTo: blue
sdk: gradio
app_file: app.py
pinned: false
---

# PsiHub Reader API

Servicio de traducción de PDFs académicos para la aplicación PsiHub.

Convierte papers en PDF (Open Access) a Markdown estructurado traducido al español, 
preservando tablas, encabezados, figuras y la fidelidad de los datos originales.

También expone `POST /api/chat`, el chat de investigación con streaming (OpenAlex +
DeepSeek). Ver [DEPLOY_CHAT.md](DEPLOY_CHAT.md) para configurarlo y desplegarlo.
