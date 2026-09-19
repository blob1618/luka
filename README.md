<p align="center">
  <img src="public/logo-luka-texto.png" alt="LUKA" width="400">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white" alt="Python 3.11">
  <img src="https://img.shields.io/badge/FastAPI-async-009688?logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/Meta-WhatsApp%20Business%20API-25D366?logo=whatsapp&logoColor=white" alt="WhatsApp Business API">
  <img src="https://img.shields.io/badge/Gemini%20|%20Mistral-LLM-FF6F00?logo=google&logoColor=white" alt="LLM">
  <img src="https://img.shields.io/badge/PostgreSQL-Supabase-4171A3?logo=postgresql&logoColor=white" alt="PostgreSQL / Supabase">
  <img src="https://img.shields.io/badge/Redis-Cache-DC382D?logo=redis&logoColor=white" alt="Redis">
  <img src="https://img.shields.io/badge/Docker-Deploy-2496ED?logo=docker&logoColor=white" alt="Docker">
  <img src="https://img.shields.io/badge/Render-Hosting-46E3B7?logo=render&logoColor=white" alt="Render">
</p>

<p align="center">
  <img src="public/hero-readme.jpg" alt="LUKA - Asistente Financiero por WhatsApp" width="100%">
</p>

<h3 align="center">Registra tus finanzas en lenguaje natural directamente en WhatsApp, sin esfuerzo.</h3>

---

## Qué es LUKA

LUKA es un asistente financiero inteligente que opera por WhatsApp. Usa inteligencia artificial para interpretar tus mensajes en lenguaje natural y registrar automáticamente tus ingresos y egresos. Sin apps complicadas, sin formularios, sin fricción: solo escribe como hablas y LUKA se encarga del resto.

## Para qué se usa

LUKA convierte la gestión financiera diaria en algo simple y natural. En lugar de abrir una app, buscar categorías y llenar campos, solo escribís un mensaje como *"Gasté $500 en almuerzo"* o *"Cobré mi sueldo"* y LUKA lo registra al instante.

Para las personas que nunca pudieron mantener sus finanzas organizadas, LUKA elimina la barrera de entrada: no necesitás disciplina de contabilidad ni aprender a usar una herramienta nueva. Solo necesitás WhatsApp, que ya tenés en tu teléfono.

Con el tiempo, LUKA te ayuda a ver patrones, entender en qué gastás y tomar mejores decisiones con tu dinero — todo desde el chat que ya usás todos los días.

## Características principales

- **Registro por texto** — Escribí en lenguaje natural y LUKA interpreta el monto, la categoría y el tipo de movimiento.
- **Categorización inteligente** — LUKA identifica automáticamente si es un ingreso o un egreso y lo clasifica según el concepto.
- **Sin duplicados** — Si Meta reenvía un mensaje, LUKA lo detecta y evita registros repetidos.
- **Respuesta inmediata** — Confirmación al instante después de cada registro.
- **Multi-movimiento** — Registrá uno o varios movimientos por mensaje.
- **Recordatorios proactivos** — Si un día no registrás gastos, LUKA te pregunta antes de que te olvides. Se apaga con un mensaje.
- **Dashboard web** — Visualizá tus finanzas en un panel interactivo ([Frontend](https://github.com/sandralilianaacosta-ui/luka_frontend)).

## Configuración inicial

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python -m uvicorn app.main:app --reload
```

La API levanta en `http://127.0.0.1:8000`. Verificar con:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/
```

Respuesta esperada: `{ "message": "Luka API is running" }`

Para macOS/Linux: `python3 -m venv .venv`, `source .venv/bin/activate`, `cp .env.example .env`.

## Entorno de testing

La carpeta `testing/` contiene una app Streamlit que simula el flujo de WhatsApp contra el backend local, sin depender de la API real de Meta. Solo se levanta con Docker o Podman.

Ver [testing/README.md](testing/README.md) para la guía completa.

## Links del proyecto

| Recurso | URL |
| ------- | --- |
| Backend (GitHub) | https://github.com/blob1618/luka |
| Frontend (Dashboard) | https://github.com/blob1618/luka_frontend |
| Deploy en Render | https://luka-f2nb.onrender.com |
| DeepWiki - Overview | https://deepwiki.com/blob1618/luka/1-luka-overview |
| DeepWiki - Arquitectura | https://deepwiki.com/blob1618/luka/2-core-architecture |
| Guía de base de datos | `SUPABASE_SETUP.md` |
| Notas de base de datos | `docs/database.md` |
| Guía de deploy en Render | `RENDER_DEPLOYMENT.md` |
| Guía para desarrolladores | `docs/developer-guide.md` |
| Arquitectura MVP | `docs/architecture.md` |
