# Prueba: subida directa de archivos a S3 con URL prefirmada

Fecha: 2026-11-26. Autor: agente de subida de archivos.

## Problema verificado (en vivo, 2026-11-26)

El endpoint clásico `POST /api/videos/upload` lee el archivo ENTERO en
RAM (`await file.read()`) y la petición pasa por el proxy de Render.
La subida de un vídeo del usuario (descargado de YouTube Studio)
llevaba **1 hora y nunca llegó al servidor**: no existía ningún vídeo
nuevo (`GET /api/videos/2` → 404) y el almacenamiento usado era
0 bytes. Además `GET /api/videos` **no existía** (404), así que el
panel ni siquiera podía listar los vídeos subidos ni su estado.

## Qué cambió

### 1. `viralclip/storage.py` — URL prefirmada en la abstracción

- Nuevo método abstracto `Storage.presigned_put_url(key, content_type,
  expires_in)`.
- `S3Storage` lo implementa con boto3 `generate_presigned_url(
  "put_object", ...)` (SigV4). El navegador sube con PUT y el MISMO
  `Content-Type` que se firma; el proceso web no ve ni un byte.
- `LocalStorage` lanza `NotImplementedError`: la subida directa solo
  existe con S3. La API traduce eso a HTTP 501 y el panel usa la
  subida clásica como reserva.

### 2. `worker/upload.py` (nuevo) — lógica pura de subida directa

- `build_upload_key(sha256, suffix)` → `originals/{sha[:12]}{ext}`,
  igual que el endpoint clásico.
- `validate_upload_request(...)` → valida extensión (mp4/mov/mkv/webm/
  avi/m4v), tamaño > 0 y ≤ MAX_UPLOAD_GB (2 GB), SHA-256 hexadecimal
  de 64 caracteres y duración 0–7200 s. `UploadTooLargeError`
  (subclase de ValueError) para el caso de exceso de tamaño (HTTP 413).
- `presign_direct_upload(storage, key, content_type)` → URL prefirmada
  válida `PRESIGNED_URL_EXPIRES_SECONDS` = 6 h (para subidas lentas de
  archivos grandes; SigV4 permite hasta 7 días).
- `register_uploaded_video(db, storage, ...)` → comprueba que la clave
  es exactamente la derivada del SHA-256 declarado (nada de claves
  arbitrarias), que el objeto existe en almacenamiento y que su tamaño
  coincide con el declarado (detecta subidas truncadas). Deduplica por
  SHA-256 como el endpoint clásico. Devuelve `(video, is_duplicate)`.

### 3. `api/main.py` — solo endpoints de subida + `GET /api/videos`

- `POST /api/videos/upload/presign` — valida (tamaño, SHA, extensión,
  duración), responde `duplicate` si el SHA ya existe (no hace falta
  subir nada), comprueba la cuota (misma regla que
  `GET /api/storage/quota`) y devuelve `{upload_url, key, expires_in,
  content_type, max_upload_gb}`. 400 validación, 413 cuota o exceso de
  tamaño, 501 backend sin URLs prefirmadas.
- `POST /api/videos/upload/complete` — registra el vídeo con
  `register_uploaded_video`. 400 clave/tamaño inválidos, 404 objeto
  ausente. Devuelve el dict del vídeo + `duplicate`.
- `GET /api/videos` — lista de vídeos, más recientes primero, con
  `process_status` (estado del último job de proceso, o
  `"sin_procesar"`), `latest_process_job_id` y `clips_count`.
- `_video_dict` ahora incluye también `file_size_bytes` y `created_at`
  (aditivo, no rompe `GET /api/videos/{id}`).
- El endpoint clásico `POST /api/videos/upload` **no se tocó**: queda
  como reserva para archivos pequeños y para backends sin S3.

### 4. `static/index.html` y `static/app.js` — panel

- La subida usa el flujo prefirmado con **barra de progreso real**
  (`<progress id="upload-progress">` + `XMLHttpRequest.upload.
  onprogress`): el navegador calcula el SHA-256 (`crypto.subtle`) y
  la duración (elemento `<video>` con `preload="metadata"`), pide la
  URL prefirmada, hace el PUT directo a S3 y llama a `complete`.
- Reservas automáticas: si el backend no soporta presign (501), si el
  hash en el navegador falla, o si el PUT directo falla (red/CORS del
  bucket), se usa la subida clásica multipart. El usuario ve qué
  camino se usó.
- Nueva sección **«Mis vídeos subidos»** (`GET /api/videos`) con
  estado de proceso, nº de clips y botones «Seleccionar» y
  «Procesar» por vídeo; se actualiza al arrancar y tras cada subida.

## Qué se verificó (local, con fakes)

`pytest tests/test_upload_direct.py` → **14 passed**.

- presign OK: clave `originals/{sha[:12]}.mp4`, URL prefirmada con
  la clave, `expires_in` 6 h, `content_type` firmado y registrado.
- presign rechaza: tamaño > 2 GB (413), SHA inválido (400),
  extensión no admitida (400).
- presign con SHA ya existente → `duplicate`, sin llamar a presign.
- presign con backend sin soporte → 501.
- complete registra el vídeo (plataforma `upload`, `file_path`,
  tamaño, duración, SHA) y lo deja en BD.
- complete rechaza: objeto ausente en storage (404), tamaño distinto
  al declarado (400, sin crear vídeo), clave arbitraria no derivada
  del SHA (400).
- complete dos veces con el mismo SHA → segundo `duplicate: true`,
  mismo id, una sola fila en BD.
- `GET /api/videos`: más recientes primero, `process_status`
  `sin_procesar` sin jobs y `done` con un job de proceso terminado,
  `clips_count` 0.
- Lógica pura: `build_upload_key` y `validate_upload_request`
  (errores y caso válido).
- `node --check static/app.js` → sintaxis JS OK.
- `import api.main` → OK.

## Qué quedó SIN verificar (necesita el despliegue en vivo)

- **Un PUT prefirmado real a Supabase S3**: necesita credenciales
  vivas y el bucket real; en local solo se probó con un fake. Al
  desplegar, comprobar que el PUT directo desde el navegador llega al
  bucket y que `complete` registra el vídeo.
- **CORS del bucket de Supabase**: el PUT directo es cross-origin
  (origen `https://viralclip-ai-58t0.onrender.com` → endpoint S3 de
  Supabase). Si el bucket no permite ese origen, el navegador
  bloqueará el PUT y el panel caerá a la subida clásica (con mensaje).
  Si eso pasa, hay que permitir el origen en la configuración CORS
  del bucket (sin coste, sin cuentas nuevas).
- **Subida real de un archivo grande** (p. ej. el vídeo de Studio del
  usuario): no se probó end-to-end; el flujo local es con fakes.
- **La duración** la calcula el navegador; el servidor la acepta tal
  cual (0–7200 s). El proceso posterior no depende de ella.

## Limitaciones honestas

- El SHA-256 lo calcula el navegador y el servidor confía en él para
  la deduplicación y para derivar la clave. El servidor NO vuelve a
  hashear el archivo (eso exigiría descargarlo entero, justo lo que se
  evita). La integridad se comprueba por tamaño (`head_object`) y el
  proceso posterior fallaría de forma visible si el archivo estuviera
  corrupto.
- El cálculo de cuota en `presign` usa la misma regla que
  `GET /api/storage/quota` (en S3 solo cuenta clips no caducados, no
  originales): si el bucket está cerca del límite por originales, la
  comprobación puede ser optimista.
- `crypto.subtle.digest` con `file.arrayBuffer()` carga el archivo en
  memoria del navegador para hashearlo; en archivos de ~2 GB puede
  fallar en equipos con poca RAM, en cuyo caso el panel usa la subida
  clásica.
