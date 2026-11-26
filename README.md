# VIRALCLIP AI — MVP (v0.1.0)

Convierte vídeos largos y directos (**contenido propio/autorizado**) en clips
verticales **9:16, 1080×1920, MP4 H.264 + AAC**, con transcripción real
(faster-whisper), detección de momentos, puntuación viral 0–100 explicada,
subtítulos cinéticos por palabra, detección de facecam/gameplay con
composición vertical inteligente, clasificación del tipo de directo
(gameplay/podcast/reality/conversación) con criterios de momentos propios
por categoría, títulos por reglas y biblioteca con caducidad a 7 días.

> **Alcance honesto (ver `../docs/AUDITORIA.md` y `../docs/CONECTORES.md`):**
> ninguna API oficial entrega el archivo de vídeo de contenido ajeno, y los
> webhooks de Twitch/Kick son solo avisos (sin audio/vídeo). Por eso el MVP
> ingiere por **subida directa** del creador y valida el estado en vivo solo
> cuando hay credenciales; sin ellas devuelve `requires_credentials` y **nunca
> inventa** si un canal está en directo.

## Instalación

```bash
cd app
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # incluye faster-whisper, streamlink
cp .env.example .env                        # sin secretos en el repo
```

Requisitos del sistema: `ffmpeg` (8.x verificado) y Python 3.12.
Nota: `av` está fijado a `13.1.0` porque faster-whisper 1.2.1 usa
`av.open(metadata_errors=...)` y las versiones nuevas de `av` lo rompen.

## Arranque

```bash
.venv/bin/uvicorn api.main:app --host 0.0.0.0 --port 8000
# Scheduler de expiración (proceso aparte, 1 vez/hora):
.venv/bin/python -m worker.scheduler
# O con Docker (api + worker; Postgres opcional, ver docker-compose.yml):
docker compose up --build
```

## Endpoints

| Método | Ruta | Qué hace |
|---|---|---|
| GET | `/health` | Estado, backend de storage, modelo Whisper |
| POST | `/api/streams/validate` | `{url}` → plataforma, validez, estado en vivo real o `requires_credentials` |
| POST | `/api/streams` | Crea una live session (máx. 3 simultáneos, 1 activo por defecto; resto en cola `waiting`) |
| POST | `/api/streams/{id}/start` · `/stop` | Activa / detiene un directo (promociona al siguiente en cola) |
| GET | `/api/streams` | Lista de sesiones |
| POST | `/api/videos/{id}/classify` | Clasificación automática del tipo de directo (no sobrescribe una corrección manual) |
| PATCH | `/api/videos/{id}/category` | Corrección manual de la categoría (sin reintroducir la URL; tiene prioridad) |
| GET | `/api/videos/{id}` | Vídeo + estado de clasificación (categoría efectiva, confianza, fuente) |
| POST | `/api/videos/upload` | Subida directa (multipart); deduplica por SHA-256 |
| POST | `/api/videos/{id}/process` | Ejecuta el pipeline real (transcribe → detecta → puntúa → titula → renderiza) |
| GET | `/api/jobs` · `/api/jobs/{id}` | Trabajos y sus momentos |
| GET | `/api/moments?min_score=` | Momentos ordenados por puntuación, con desglose |
| GET | `/api/clips` · `/api/library` | Biblioteca; filtros: `q`, `platform`, `min_score`, `favorites_only`, `min_duration`, `max_duration`, `date_from`, `date_to`; incluye `expires_at` y `seconds_remaining` |
| POST | `/api/clips/{id}/favorite` | Alterna favorito |
| DELETE | `/api/clips/{id}` | Borra archivo + miniatura y libera espacio |
| GET | `/api/clips/{id}/download` | Descarga el MP4 (410 si caducó) |
| GET | `/api/clips/{id}/thumbnail` | Sirve la miniatura JPG del clip (404 si no existe; 410 si el clip caducó o no tiene miniatura) |
| GET | `/api/storage/quota` | Espacio usado/disponible, límite configurable, retención |

## Pipeline (`worker/pipeline.py`)

- `capture_segment(url, duration)` — HLS con streamlink + ffmpeg (contenido propio/autorizado).
- `transcribe(audio)` — faster-whisper real, `word_timestamps=True`. Por defecto
  modelo `small` en CPU (desarrollo); `large-v3-turbo` documentado como opción
  con más RAM/GPU. En esta VM: 20 s de audio → ~27 s de transcripción.
- `detect_moments(segments, audio_energy?)` — ventanas 30–90 s cortadas solo en
  límites de palabra y preferentemente en pausas; nunca corta una palabra.
- `score_moment(segment)` — 0–100 determinista con desglose por 8 criterios
  (gancho 20%, emoción 15%, entretenimiento 15%, originalidad 10%, curiosidad
  15%, comentarios 10%, comprensión 10%, calidad 5%). Acepta `category`:
  cada categoría usa sus propios pesos (`CATEGORY_WEIGHTS`) y bonus
  (ver `worker/classify.py`).
- `generate_titles(text)` — ≥5 títulos por plantillas (curiosidad/clickbait/
  humor/polémica/directo) anclados al transcript real; no inventa hechos.
- `render_clip(source, start, end, subtitles)` — 1080×1920 H.264 + AAC, subtítulos
  ASS quemados. Parámetros opcionales: `style="static"|"dynamic"` (subtítulos
  cinéticos por palabra, ver `worker/kinetic_subs.py`), `layout`/`facecam`
  (composición gameplay + facecam, ver `worker/vision.py`), `category`/`focus_x`
  (recorte desplazado hacia el rostro que habla en conversación, ver
  `worker/classify.py`). Por defecto: estático, recorte centrado, `gameplay_only`.
  **Layout + dinámico unificados (2026-11-26):** el render dinámico respeta
  el layout — en `facecam_top`/`facecam_bottom` el zoom cinético se aplica
  solo sobre la rama de gameplay (1080×1440) y la facecam queda fija; en
  `talking_head`, recorte de rostro + cinético; en `multi_window`, dos
  recortes apilados + cinético. Con facecam abajo los subtítulos cinéticos
  suben a posición `raised` (MarginV 720) para no taparla. Sin facecam
  válida el layout cae a `gameplay_only` dinámico (fallback documentado
  en `_effective_layout`, nunca un error silencioso distinto del render).
  El recorte de silencios también compone: el trim va sobre el origen y
  la composición se aplica tras el concat. `focus_x` ya se aplica en el
  dinámico `gameplay_only` (conversación/podcast/reality), como en el
  estático. `process_job` (`worker/jobs.py`) conecta la detección: en
  `gameplay_streamer` muestrea hasta 30 fotogramas (1 fps, primeros 30 s),
  ejecuta `detect_facecam` + `decide_layout` por momento (reacción →
  `facecam_top`, si no `facecam_bottom`) y renderiza dinámico con
  `words` reales; sin OpenCV o sin facecam cae a `gameplay_only`
  dinámico sin romper el job; fuera de gameplay no se muestrea.
- `worker/kinetic_subs.py` — motor de subtítulos cinéticos: eventos
  acumulativos palabra a palabra con timestamps reales de faster-whisper,
  efectos por bloque (impacto rosa, sorpresa con zoom breve, highlight
  amarillo, tensión naranja progresiva, normal), bloques ≤ 6 palabras,
  recorte de silencios > 1,5 s sin cortar palabras. Posición `bottom` o
  `raised` (para vídeos con tercio inferior ocupado).
- `worker/vision.py` — detección de facecam y gameplay (OpenCV Haar):
  `detect_facecam` (posición, bbox, confianza por estabilidad temporal,
  no por peso del cascade), `detect_static_regions` (HUD/overlay),
  `detect_gameplay_area`, `decide_layout` (`facecam_top`/`facecam_bottom`/
  `gameplay_only`/`talking_head`/`multi_window`). La facecam se estima
  desde el rostro (calibrado con un solo streamer: revalidar con
  contenido propio).
- `worker/classify.py` — clasificación del tipo de directo por muestras
  (1 fps, máx. 60 fotogramas; audio: primeros 60 s con faster-whisper
  tiny): categorías `gameplay_streamer`, `podcast_entrevista`,
  `reality_casa`, `conversacion_reaccion`, `otros`, con 7 subcategorías
  internas (un reality ≠ un podcast). Confianza por margen; < 0,6 pide
  elegir al usuario (`needs_user_choice`). La corrección manual tiene
  prioridad (`manual_override`).
- `worker/expiry.py` — borra clips/miniaturas con `expires_at` vencida
  (7 días desde `created_at`), libera espacio, conserva metadatos básicos.
- `worker/live.py` — gestor de directos: máx. 3, 1 activo por defecto, cola.

## Pruebas

```bash
.venv/bin/python -m pytest tests/ -v
```

**Resultado real (2026-11-26, esta VM): 84 passed, 0 failed** (65 antes de
la unificación layout+dinámico; +12 nuevos en
`tests/test_render_dynamic_layout.py`: dinámico + facecam_bottom/top/
talking_head/multi_window, vídeo real de La Casa, recorte de silencios
con facecam, fallbacks documentados, geometría de subtítulos raised y
`process_job` con layout decidido / sin visión / fuera de gameplay;
+4 en `tests/test_subtitles.py`: corte de línea en pausa > 0,8 s
—regla portada de `e2e/work/make_ass.py`—, tope de 6 palabras y
fallback por segmento; +3 en `tests/test_api_live.py`: endpoint de
miniaturas `GET /api/clips/{id}/thumbnail` — 200 con `image/jpeg`,
404 si falta el archivo, 410 si caducó o no hay miniatura).
Cobertura: validación de URLs (Twitch/Kick/YouTube, válidas e inválidas, mocks
de la API de Kick), expiración a 7 días, puntuación con el ground truth real
de la narración de Nora (`../docs/ground_truth.txt`, 195 palabras),
transcripción real con faster-whisper tiny de los primeros 10 s de
`narracion_full.mp3` (detecta "casa", con timestamps de palabra), render
vertical verificado con ffprobe (1080×1920, h264, aac), almacenamiento local
(guardar/leer/borrar, anti-traversal), API + gestor de directos,
subtítulos cinéticos (`tests/test_kinetic_subs.py`, 12 tests),
detección de facecam/gameplay (`tests/test_vision.py`, 17 tests, incluye
detección real sobre 3 fotogramas del segmento TheGrefg) y clasificación
del tipo de directo (`tests/test_classify.py`, 9 tests).

**Prueba de edición V2 (2026-11-26):** ver
`../docs/PRUEBA_EDICION_V2.md` (informes parciales:
`PRUEBA_EDICION_V2_subtitulos.md`, `PRUEBA_EDICION_V2_facecam.md`,
`PRUEBA_EDICION_V2_clasificacion.md`). Subtítulos dinámicos probados
con La Casa (v1 estática vs v2 dinámica, 0–38 s, fotogramas en
`../e2e/frames_v2/`); facecam detectada en el segmento real de TheGrefg
(bottom-left, confianza 0,91) con clip vertical gameplay + facecam
(`../e2e/clips/thegrefg_v2_facecam.mp4`); clasificación probada con
gameplay de Yianca/Roblox (0,95) y La Casa (0,76). Podcast y reality
están implementados pero **no probados** con material real (falta
material); la facecam tampoco está demostrada con contenido propio
(falta una grabación de Juan con gameplay + facecam).

**Prueba end-to-end real (2026-11-26):** subidos los primeros 20 s del vídeo
final de *La casa que solo existe de noche* por `POST /api/videos/upload` y
procesados por `POST /api/videos/1/process` → job `done`, 1 momento
(0.88–19.98 s, puntuación 41.2, transcripción correcta: "Si hay una casa en tu
calle… Nora, diecinueve años…"), 5 títulos, clip `data/storage/clips/1.mp4`
verificado con ffprobe: **h264 1080×1920 + aac**, en biblioteca con
`expires_at` 7 días después. La transcripción de 20 s con modelo `small` en
CPU tardó ~27 s.

**Prueba end-to-end real con el vídeo completo (2026-11-26, integración
E2E):** el vídeo final entero de *La casa* (106,7 s, 9 MB) por la API:
`POST /api/videos/upload` → vídeo 3 (SHA-256 verificado, deduplicación
comprobada: segunda subida devuelve `duplicate_of: 3`);
`POST /api/videos/3/classify` → `conversacion_reaccion` /
`narracion_solo`, confianza 0,76 (14,8 s);
`POST /api/videos/3/process` → job 3 `done` en **82,6 s** totales,
1 momento (0.88–90.01 s, puntuación 67.8), 5 títulos, estilo
`dynamic`, layout `gameplay_only` (sin facecam: no es gameplay).
Transcripción medida aparte sobre el mismo audio: **39,1 s** con
`small` en CPU (14 segmentos, 201 palabras); el resto (~43 s) es
render dinámico + puntuación + miniatura. Clip
`data/storage/clips/3.mp4` verificado con ffprobe: **h264 1080×1920 +
aac, 84,08 s** (89,13 s del momento menos ~5 s de silencios
recortados), en biblioteca con `expires_at` 2026-12-03 (7 días) y
miniatura `data/storage/thumbs/3.jpg` servida por
`GET /api/clips/2/thumbnail` → **200 `image/jpeg`, 17 193 bytes**.
Subtítulos dinámicos confirmados: `data/work/clip_3.ass` tiene 192
eventos `Kinetic` palabra a palabra, y 3 fotogramas del clip muestran
el resaltado de la palabra actual. **Observación:** el vídeo final de
*La casa* ya lleva sus propios subtítulos quemados en el tercio
inferior, y los del pipeline se superponen a ellos en varios
fotogramas (ver PENDIENTE).

**Integración de los scripts E2E (2026-11-26):** los scripts de
`../e2e/work/` quedaron consolidados sin duplicar lógica —
`transcribe.py` y `score_moment.py` están *superseded* por
`worker/pipeline.py::transcribe` y `::score_moment` (la fórmula de
`score_moment.py` era ad hoc para un momento concreto de TheGrefg;
sus ideas de densidad/claridad quedan como señales candidatas
futuras, no portadas para no cambiar las puntuaciones validadas);
de `make_ass.py` se portó el corte de línea en pausa > 0,8 s a
`pipeline.subtitles_from_segments` (el escritor ASS ya existía como
`build_ass`); `e2e_transcribe.py` y `e2e_render.py` ya usaban el
pipeline y quedan como drivers finos documentados. Cada script lleva
una nota de cabecera con su disposición. No hay dos implementaciones
de lo mismo.

**Incidencias encontradas y corregidas durante las pruebas:**
- `detect_moments` solo guardaba la primera/última palabra de cada bloque
  (texto truncado) → corregido a acumular todas las palabras.
- `time_remaining` devolvía `expired=None` en objetos no persistidos → `bool()`.
- `av 19.x` es incompatible con faster-whisper 1.2.1 → fijado `av==13.1.0`.
- La primera instalación de dependencias en segundo plano fue cancelada por el
  runtime (SIGTERM) sin instalar nada → reinstalado por lotes en primer plano.

## Despliegue (camino 0 $)

Guía completa paso a paso para Juan en `../docs/DESPLIEGUE.md` (dos caminos:
**A** Oracle Always Free + R2 — 24/7 real, pide tarjeta solo para
verificación; **B** Render + Supabase — sin tarjeta, pero duerme a los 15 min
y con 512 MB de RAM el procesamiento puede fallar). Hugging Face Spaces **no**
es alternativa: crear un Space Docker exige PRO (9 $/mes) desde 2026.

```bash
cp .env.production.example .env   # rellenar valores reales
./deploy.sh                        # camino A: instala Docker, levanta api + worker + Postgres
```

Cambios de código imprescindibles para el despliegue (2026-11-26):
`psycopg2-binary` en `requirements.txt` (driver Postgres) y
`S3_ADDRESSING_STYLE` en `viralclip/config.py` + `viralclip/storage.py`
(`path` para Supabase Storage, `auto` para R2). Sin ellos, Postgres y el
storage S3 de Supabase no funcionan. La app no se modificó en nada más.

## PENDIENTE (no implementado, documentado explícitamente)

- **Títulos con LLM open source (Qwen3):** `generate_titles` es por reglas;
  el LLM queda como fase posterior.
- **Facecam en PiP sobre el gameplay** (clasificación): requiere seguir
  la región de la facecam fotograma a fotograma (hoy la bbox es fija por
  vídeo: la mediana del cluster estable; si el streamer mueve la cámara
  o cambia de escena, el pane queda desactualizado).
- **Energía de audio para gritos:** el impacto de los subtítulos
  cinéticos se detecta hoy solo por texto; conectar `audio_energy`.
- **Posicionamiento automático de subtítulos** según la zona de acción
  (hoy manual: `bottom`/`raised`).
- **Validación con material propio:** facecam con gameplay propio de
  Juan (falta su grabación), podcast con varias personas y reality
  multi-cámara (categorías implementadas pero no probadas).
- **Subtítulos sobre vídeos ya terminados:** el vídeo final de *La casa*
  ya lleva sus propios subtítulos quemados en el tercio inferior; al
  re-cliparlo, los subtítulos cinéticos del pipeline se superponen a los
  del origen y en algunos fotogramas se solapan y se vuelven ilegibles
  (verificado 2026-11-26 en el clip de la E2E con el vídeo completo).
  El caso de uso correcto es metraje en bruto / directo sin texto
  quemado; detectar texto quemado en el origen queda como mejora
  futura (no implementada).
- **Worker encolado asíncrono (Redis/RQ):** `POST /process` ejecuta el pipeline
  de forma síncrona; la cola persistente con reintentos es fase posterior.
- **Frontend / dashboard:** solo API REST (sin UI).
- **Autenticación de usuarios y tokens OAuth cifrados:** no hay auth; las
  credenciales de plataforma van por variables de entorno.
- **Publicación automática** (YouTube/TikTok/Meta) y **detección por chat en
  vivo**: no implementadas; ver `../docs/CONECTORES.md` para los límites.
- **Captura HLS en vivo durante el directo:** `capture_segment` existe pero no
  hay worker de larga duración que la mantenga; el flujo v1 es VOD/subida tras
  el directo.
- **Detección de momentos por densidad de chat:** no implementada (requiere
  webhooks/suscripciones por canal con OAuth del creador).
- **Miniaturas WebP / sprites, descarga en lote (ZIP), aprendizaje
  (aprobados/rechazados):** no implementados.

## Estructura

```
app/
  viralclip/  config.py db.py models.py storage.py connectors.py
  api/main.py            # FastAPI
  worker/     pipeline.py jobs.py expiry.py live.py scheduler.py
              kinetic_subs.py vision.py classify.py
  tests/      test_urls.py test_scoring.py test_expiry_storage.py
              test_render.py test_transcribe.py test_api_live.py
              test_kinetic_subs.py test_vision.py test_classify.py
  requirements.txt .env.example Dockerfile docker-compose.yml
```

## Costes

Presupuesto 0 $: nada de pago por defecto. Storage local en desarrollo;
R2/S3 por variables de entorno (`STORAGE_BACKEND=s3`). Ver
`../docs/PRESUPUESTO.md` para el modelo de costes (Hetzner CX23 ~6,49 $/mes
recomendado; GPU Modal T4 por trabajo dentro del crédito de 30 $/mes).
