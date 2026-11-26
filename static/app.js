/* VIRALCLIP AI — dashboard. Vanilla JS, sin build, sin CDN. */
"use strict";

const $ = (s) => document.querySelector(s);
const SUBCATEGORIES = {
  gameplay_streamer: ["gameplay_solo", "gameplay_facecam"],
  podcast_entrevista: ["podcast_estudio"],
  reality_casa: ["reality_multi_cam"],
  conversacion_reaccion: ["conversacion_solo", "narracion_solo"],
  otros: ["otros"],
};
let currentVideoId = null;
let analysisStart = null;
let analysisTimerId = null;
let libraryCache = []; // para la cuenta atrás en vivo (seconds_remaining)

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  const text = await r.text();
  let data = null;
  try { data = JSON.parse(text); } catch { data = { raw: text }; }
  if (!r.ok) {
    const msg = (data && data.detail) || (data && data.raw) || `HTTP ${r.status}`;
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return data;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fmtSecs(s) {
  s = Math.max(0, Math.round(Number(s) || 0));
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  if (d > 0) return `${d}d ${String(h).padStart(2, "0")}h ${String(m).padStart(2, "0")}m`;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
}
function fmtBytes(n) {
  n = Number(n) || 0;
  if (n >= 1024 ** 3) return (n / 1024 ** 3).toFixed(2) + " GB";
  if (n >= 1024 ** 2) return (n / 1024 ** 2).toFixed(1) + " MB";
  if (n >= 1024) return (n / 1024).toFixed(1) + " KB";
  return n + " B";
}

/* ---------- salud ---------- */
async function loadHealth() {
  const el = $("#health");
  try {
    const h = await api("/health");
    el.textContent = `API: ${h.status} · ${esc(h.app)} ${esc(h.version)} · backend ${esc(h.storage_backend)} · whisper ${esc(h.whisper_model)}`;
    el.dataset.state = "ok";
    $("#config-line").textContent =
      `backend de almacenamiento: ${h.storage_backend}; modelo Whisper: ${h.whisper_model}; ` +
      `retención de clips: 7 días; cuota de almacenamiento: 10 GB (config.py: storage_quota_gb=10.0, clip_retention_days=7).`;
    $("#conn-state").textContent = "conectado";
  } catch (e) {
    el.textContent = "API: no disponible (" + e.message + ")";
    el.dataset.state = "error";
    $("#conn-state").textContent = "sin conexión";
  }
}

/* ---------- analizar directo ---------- */
function startTimer() {
  analysisStart = Date.now();
  clearInterval(analysisTimerId);
  analysisTimerId = setInterval(() => {
    $("#analysis-timer").textContent = fmtSecs((Date.now() - analysisStart) / 1000);
  }, 1000);
}
function stopTimer() { clearInterval(analysisTimerId); analysisTimerId = null; }

$("#stream-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const url = $("#stream-url").value.trim();
  const box = $("#validate-result");
  box.textContent = "Validando…";
  try {
    const v = await api("/api/streams/validate", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    $("#live-state").textContent = `${v.live_status} — ${v.detail || ""}`;
    if (!v.valid) { box.textContent = "URL no válida: " + (v.detail || ""); return; }
    box.textContent = `Válida · plataforma: ${v.platform} · canal/vídeo: ${v.identifier} · live_status: ${v.live_status} — ${v.detail || ""} · Creando sesión…`;
    const s = await api("/api/streams", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    box.textContent = `Sesión #${s.id} creada: ${s.platform}/${s.channel} · estado: ${s.status} · live_status: ${s.live_status || "—"} · ${s.detail || ""}`;
    startTimer();
    await loadStreams();
  } catch (e) { box.textContent = "Error: " + e.message; }
});

async function loadStreams() {
  const list = $("#streams-list");
  try {
    const streams = await api("/api/streams");
    if (!streams.length) { list.innerHTML = "<li>Sin sesiones todavía.</li>"; return; }
    list.innerHTML = streams.map((s) => `
      <li>
        <strong>#${s.id} ${esc(s.platform)}/${esc(s.channel)}</strong> — estado: <strong>${esc(s.status)}</strong><br>
        <span class="hint">${esc(s.url)}${s.detail ? " · " + esc(s.detail) : ""}</span><br>
        <button type="button" data-start="${s.id}">Iniciar</button>
        <button type="button" data-stop="${s.id}">Detener análisis</button>
      </li>`).join("");
    list.querySelectorAll("[data-start]").forEach((b) => b.addEventListener("click", async () => {
      try { await api(`/api/streams/${b.dataset.start}/start`, { method: "POST" }); startTimer(); await loadStreams(); }
      catch (e) { alert("Error al iniciar: " + e.message); }
    }));
    list.querySelectorAll("[data-stop]").forEach((b) => b.addEventListener("click", async () => {
      try { await api(`/api/streams/${b.dataset.stop}/stop`, { method: "POST" }); stopTimer(); await loadStreams(); }
      catch (e) { alert("Error al detener: " + e.message); }
    }));
  } catch (e) { list.innerHTML = "<li>Error al cargar sesiones: " + esc(e.message) + "</li>"; }
}
$("#btn-refresh-streams").addEventListener("click", loadStreams);

/* ---------- subida directa (URL prefirmada) + reserva clásica ---------- */
// Flujo nuevo: el navegador hashea el archivo (SHA-256) y lee su
// duración, pide una URL prefirmada (POST /api/videos/upload/presign),
// sube el archivo DIRECTO a S3 con PUT (barra de progreso real, sin
// pasar por el proceso web) y registra el vídeo
// (POST /api/videos/upload/complete). Si la subida directa no está
// disponible (backend local, 501) o el PUT falla (red/CORS del
// bucket), se usa la subida clásica multipart como reserva.
async function sha256Hex(file) {
  const buf = await file.arrayBuffer();
  const hash = await crypto.subtle.digest("SHA-256", buf);
  return [...new Uint8Array(hash)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function fileDurationSeconds(file) {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(file);
    const v = document.createElement("video");
    v.preload = "metadata";
    v.onloadedmetadata = () => { URL.revokeObjectURL(url); resolve(v.duration || 0); };
    v.onerror = () => { URL.revokeObjectURL(url); resolve(0); };
    v.src = url;
  });
}

function putWithProgress(url, file, contentType, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", url);
    xhr.setRequestHeader("Content-Type", contentType);
    xhr.upload.onprogress = (ev) => { if (ev.lengthComputable) onProgress(ev.loaded / ev.total); };
    xhr.onload = () => (xhr.status >= 200 && xhr.status < 300
      ? resolve()
      : reject(new Error(`PUT directo: HTTP ${xhr.status}`)));
    xhr.onerror = () => reject(new Error("PUT directo: error de red (puede ser CORS del bucket; se intentará la subida clásica)"));
    xhr.send(file);
  });
}

async function legacyUpload(file, title) {
  const fd = new FormData();
  fd.append("file", file);
  fd.append("title", title);
  const r = await fetch("/api/videos/upload", { method: "POST", body: fd });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
  return data;
}

async function finishUpload(data, box) {
  $("#upload-progress").hidden = true;
  currentVideoId = data.id;
  const dup = data.duplicate
    ? ` · ⚠ duplicado de vídeo #${data.duplicate_of} (mismo SHA-256, no se guardó otra copia)` : "";
  box.textContent = `Vídeo #${data.id} · título: ${data.title || ""} · ${fmtBytes(data.file_size_bytes ?? data.size_bytes)}${dup}`;
  $("#video-panel").hidden = false;
  $("#video-id-label").textContent = "#" + data.id;
  $("#video-info").textContent = `file_path: ${data.file_path || "—"}`;
  await loadVideo(data.id);
  await loadVideos();
}

$("#upload-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const file = $("#upload-file").files[0];
  if (!file) return;
  const box = $("#upload-result");
  const bar = $("#upload-progress");
  const title = $("#upload-title").value || file.name;
  bar.hidden = false;
  bar.value = 0;
  try {
    box.textContent = "Calculando SHA-256 y duración…";
    let sha;
    try {
      sha = await sha256Hex(file);
    } catch (e) {
      box.textContent = "No se pudo hashear el archivo en el navegador; usando la subida clásica…";
      return finishUpload(await legacyUpload(file, title), box);
    }
    const duration = await fileDurationSeconds(file);
    box.textContent = "Pidiendo URL de subida directa…";
    const r = await fetch("/api/videos/upload/presign", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        filename: file.name, size_bytes: file.size, sha256: sha, title,
        duration_seconds: duration, content_type: file.type || "video/mp4",
      }),
    });
    const pre = await r.json().catch(() => ({}));
    if (!r.ok) {
      if (r.status === 501) {
        box.textContent = "Subida directa no disponible en este backend; usando la subida clásica…";
        return finishUpload(await legacyUpload(file, title), box);
      }
      throw new Error(pre.detail || `HTTP ${r.status}`);
    }
    if (pre.duplicate) {
      bar.hidden = true;
      box.textContent = `⚠ Ese vídeo ya existe (#${pre.video_id} · ${pre.title || ""}); no hizo falta subirlo.`;
      currentVideoId = pre.video_id;
      $("#video-panel").hidden = false;
      $("#video-id-label").textContent = "#" + pre.video_id;
      await loadVideo(pre.video_id);
      await loadVideos();
      return;
    }
    box.textContent = "Subiendo directo a S3… (el archivo no pasa por el servidor)";
    await putWithProgress(pre.upload_url, file, pre.content_type, (p) => {
      bar.value = Math.round(p * 100);
      box.textContent = `Subiendo directo a S3… ${Math.round(p * 100)}% (${fmtBytes(file.size * p)} de ${fmtBytes(file.size)})`;
    });
    box.textContent = "Subida completada. Registrando el vídeo…";
    const done = await api("/api/videos/upload/complete", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        key: pre.key, sha256: sha, filename: file.name, title,
        size_bytes: file.size, duration_seconds: duration,
      }),
    });
    bar.value = 100;
    return finishUpload(done, box);
  } catch (e) {
    // Si el PUT directo falló (red/CORS), un intento con la subida clásica.
    if (/PUT directo/.test(e.message)) {
      try {
        box.textContent = "La subida directa falló; intentando la subida clásica…";
        bar.value = 0;
        return finishUpload(await legacyUpload(file, title), box);
      } catch (e2) { bar.hidden = true; box.textContent = "Error al subir: " + e2.message; return; }
    }
    bar.hidden = true;
    box.textContent = "Error al subir: " + e.message;
  }
});

async function loadVideos() {
  const list = $("#videos-list");
  try {
    const videos = await api("/api/videos");
    if (!videos.length) { list.innerHTML = "<li>Sin vídeos subidos todavía.</li>"; return; }
    list.innerHTML = videos.map((v) => `
      <li>
        <strong>#${v.id} ${esc(v.title || "")}</strong> — plataforma: ${esc(v.platform)} · duración: ${fmtSecs(v.duration_seconds || 0)} · ${fmtBytes(v.file_size_bytes || 0)}<br>
        <span class="hint">estado de proceso: <strong>${esc(v.process_status)}</strong> · clips: ${v.clips_count} · categoría: ${esc(v.effective_category || "otros")} · creado: ${esc(v.created_at || "—")}</span><br>
        <button type="button" data-select-video="${v.id}">Seleccionar</button>
        <button type="button" data-process-video="${v.id}" ${(v.process_status === "processing" || v.process_status === "queued") ? "disabled" : ""}>Procesar</button>
      </li>`).join("");
    list.querySelectorAll("[data-select-video]").forEach((b) => b.addEventListener("click", async () => {
      currentVideoId = Number(b.dataset.selectVideo);
      $("#video-panel").hidden = false;
      $("#video-id-label").textContent = "#" + currentVideoId;
      await loadVideo(currentVideoId);
    }));
    list.querySelectorAll("[data-process-video]").forEach((b) => b.addEventListener("click", async () => {
      const vid = Number(b.dataset.processVideo);
      const box = $("#process-result");
      currentVideoId = vid;
      $("#video-panel").hidden = false;
      $("#video-id-label").textContent = "#" + vid;
      box.textContent = "Procesando en segundo plano…";
      try {
        const r = await api(`/api/videos/${vid}/process`, { method: "POST" });
        const j = await pollJob(r.job_id, box, "Procesando");
        box.textContent = `Job #${j.id} · estado: ${j.status} · momentos: ${(j.moments || []).length} · clips listos en Mi biblioteca.`;
        await loadLibrary(); await loadJobs(); await loadVideos();
      } catch (e) { box.textContent = "Error al procesar: " + e.message; }
    }));
  } catch (e) { list.innerHTML = "<li>Error al cargar vídeos: " + esc(e.message) + "</li>"; }
}
$("#btn-refresh-videos").addEventListener("click", loadVideos);

async function loadVideo(id) {
  try {
    const v = await api(`/api/videos/${id}`);
    $("#video-info").textContent =
      `Título: ${v.title} · plataforma: ${v.platform} · duración: ${v.duration_seconds || 0} s · ` +
      `categoría efectiva: ${v.effective_category || "otros"} (origen: ${v.effective_category_source || "default"}) · ` +
      `confianza: ${v.classification_confidence} · manual: ${v.manual_override ? "sí" : "no"}`;
    if (v.category) { $("#manual-category").value = v.category; syncSubcategories(); if (v.subcategory) $("#manual-subcategory").value = v.subcategory; }
  } catch (e) { $("#video-info").textContent = "No se pudo leer el vídeo: " + e.message; }
}

$("#btn-classify").addEventListener("click", async () => {
  if (!currentVideoId) return;
  const box = $("#classify-result");
  box.textContent = "Clasificando (muestras de audio/vídeo, whisper tiny)…";
  try {
    const r = await api(`/api/videos/${currentVideoId}/classify`, { method: "POST" });
    const need = r.needs_user_choice
      ? " ⚠ Confianza baja (< 0,6): elige la categoría con los botones de abajo."
      : "";
    box.textContent = `Categoría: ${r.category} (${r.subcategory}) · confianza: ${r.confidence}${need} · guardada: ${r.saved ? "sí" : "no (se conserva la manual)"} · layout: ${r.layout ? r.layout.layout : "—"}`;
    await loadVideo(currentVideoId);
  } catch (e) { box.textContent = "Error al clasificar: " + e.message; }
});

function syncSubcategories() {
  const cat = $("#manual-category").value;
  const sel = $("#manual-subcategory");
  sel.innerHTML = (SUBCATEGORIES[cat] || ["otros"]).map((s) => `<option value="${s}">${s}</option>`).join("");
}
$("#manual-category").addEventListener("change", syncSubcategories);
syncSubcategories();

async function setManualCategory(category, subcategory) {
  if (!currentVideoId) { alert("Primero sube un vídeo."); return; }
  const box = $("#manual-result");
  box.textContent = "Guardando corrección manual…";
  try {
    const r = await api(`/api/videos/${currentVideoId}/category`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ category, subcategory }),
    });
    box.textContent = `Categoría manual guardada: ${r.effective_category} (${r.subcategory}) · botón del dashboard: ${r.dashboard_button || "—"} · tiene prioridad sobre la automática.`;
    await loadVideo(currentVideoId);
  } catch (e) { box.textContent = "Error: " + e.message; }
}
$("#btn-manual-category").addEventListener("click", () =>
  setManualCategory($("#manual-category").value, $("#manual-subcategory").value));
document.querySelectorAll(".cat-btn").forEach((b) => b.addEventListener("click", async () => {
  const cat = b.dataset.category;
  if (cat === "gameplay_streamer") { await setManualCategory("gameplay_streamer", "gameplay_solo"); return; }
  // Botón 2 agrupa podcast/reality/conversación: se usa la categoría elegida en el desplegable
  // (por defecto podcast_entrevista) o la que el usuario haya seleccionado.
  const chosen = $("#manual-category").value;
  const valid = ["podcast_entrevista", "reality_casa", "conversacion_reaccion"].includes(chosen)
    ? chosen : "podcast_entrevista";
  await setManualCategory(valid, SUBCATEGORIES[valid][0]);
}));

/* ---------- sondeo de trabajos en segundo plano ---------- */
// Los endpoints /process y /from-url responden 202 al instante y el
// trabajo sigue en un hilo del servidor (Render corta las peticiones
// largas con 502). El panel sondea GET /api/jobs/{id} hasta done/error.
async function pollJob(jobId, box, label) {
  for (let i = 0; i < 480; i++) {
    const j = await api(`/api/jobs/${jobId}`);
    if (j.status === "done") return j;
    if (j.status === "error") throw new Error(j.error || "El trabajo terminó con error");
    if (box) box.textContent = `${label} · estado: ${j.status}… (puede tardar varios minutos, no cierres la página)`;
    await new Promise((r) => setTimeout(r, 5000));
  }
  throw new Error("Tiempo de espera agotado sondeando el trabajo");
}

$("#btn-process").addEventListener("click", async () => {
  if (!currentVideoId) return;
  const box = $("#process-result");
  box.textContent = "Procesando en segundo plano…";
  try {
    const r = await api(`/api/videos/${currentVideoId}/process`, { method: "POST" });
    box.textContent = `Job #${r.job_id} en cola…`;
    const j = await pollJob(r.job_id, box, "Procesando");
    box.textContent = `Job #${j.id} · estado: ${j.status} · momentos: ${(j.moments || []).length} · clips listos en Mi biblioteca.`;
    await loadLibrary(); await loadJobs();
  } catch (e) { box.textContent = "Error al procesar: " + e.message; }
});

/* ---------- descargar por enlace (YouTube) ---------- */
$("#fromurl-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const url = $("#fromurl-url").value.trim();
  const box = $("#fromurl-result");
  box.textContent = "Descargando el vídeo… (puede tardar varios minutos según su tamaño)";
  try {
    const r = await api("/api/videos/from-url", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, auto_process: true }),
    });
    if (r.status === "duplicate") {
      box.textContent = `Ese vídeo ya existe (#${r.video_id}). Sus clips están en Mi biblioteca.`;
      currentVideoId = r.video_id;
      await loadLibrary(); await loadJobs();
      return;
    }
    const dl = await pollJob(r.job_id, box, "Descargando");
    const videoId = dl.video_id;
    if (!videoId) throw new Error("La descarga terminó sin vídeo (revisa la cola).");
    box.textContent = `Vídeo #${videoId} descargado. Procesando…`;
    currentVideoId = videoId;
    $("#video-panel").hidden = false;
    $("#video-id-label").textContent = "#" + videoId;
    await loadVideo(videoId);
    // El job de proceso se crea al terminar la descarga; puede tardar
    // unos segundos en aparecer en /api/jobs.
    let pj = null;
    for (let i = 0; i < 5 && !pj; i++) {
      const jobs = await api("/api/jobs");
      pj = jobs.find((j) => j.video_id === videoId && j.kind === "process");
      if (!pj) await new Promise((r) => setTimeout(r, 2000));
    }
    if (pj) {
      const done = await pollJob(pj.id, box, "Procesando");
      box.textContent = `Listo: vídeo #${videoId} procesado · momentos: ${(done.moments || []).length} · clips en Mi biblioteca.`;
    } else {
      box.textContent = `Vídeo #${videoId} descargado. Pulsa «Procesar» en «Subir vídeo propio» si no se procesó solo.`;
    }
    await loadLibrary(); await loadJobs();
  } catch (e) { box.textContent = "Error: " + e.message; }
});

/* ---------- cookies de YouTube ---------- */
async function loadCookiesStatus() {
  const el = $("#cookies-status");
  try {
    const s = await api("/api/cookies/youtube/status");
    el.textContent = s.configured
      ? `Cookies: configuradas · subidas: ${s.uploaded_at || "—"} · cookies de YouTube: ${s.youtube_cookies ?? "—"}`
      : "Cookies: no configuradas (sin ellas, YouTube bloquea la descarga).";
  } catch (e) { el.textContent = "Cookies: no se pudo comprobar (" + e.message + ")"; }
}
$("#cookies-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const file = $("#cookies-file").files[0];
  if (!file) return;
  const box = $("#cookies-result");
  box.textContent = "Subiendo cookies…";
  const fd = new FormData();
  fd.append("file", file);
  try {
    const r = await fetch("/api/cookies/youtube", { method: "POST", body: fd });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
    box.textContent = `Cookies guardadas · ${data.youtube_cookies} cookies de YouTube · subidas: ${data.uploaded_at}`;
    await loadCookiesStatus();
  } catch (e) { box.textContent = "Error al subir cookies: " + e.message; }
});
$("#btn-cookies-delete").addEventListener("click", async () => {
  if (!confirm("¿Eliminar las cookies de YouTube guardadas? Las descargas por enlace dejarán de funcionar hasta subir unas nuevas.")) return;
  try {
    await api("/api/cookies/youtube", { method: "DELETE" });
    $("#cookies-result").textContent = "Cookies eliminadas.";
    await loadCookiesStatus();
  } catch (e) { $("#cookies-result").textContent = "Error: " + e.message; }
});

/* ---------- biblioteca ---------- */
function clipCard(c) {
  const remaining = c.seconds_remaining ?? 0;
  const soon = remaining > 0 && remaining < 86400;
  return `<article class="clip" data-clip-id="${c.id}" data-remaining="${remaining}">
    <div class="thumb-placeholder" aria-label="Miniatura del clip">
      <img src="/api/clips/${c.id}/thumbnail" alt="Miniatura del clip" loading="lazy" style="max-width:100%;display:block" onerror="this.outerHTML='<span>Sin miniatura<br>(no disponible)</span>'">
      <strong>${fmtSecs(c.duration_seconds)} · ★ ${c.score}</strong>
    </div>
    <h4>${esc(c.title || "Clip #" + c.id)}</h4>
    <p class="hint">Plataforma: ${esc(c.platform)} · Duración: ${fmtSecs(c.duration_seconds)} · Puntuación viral: ${c.score}</p>
    <p class="hint">Creado: ${esc(c.created_at || "—")}<br>Expira: ${esc(c.expires_at || "—")}<br>
    Tiempo restante: <strong class="countdown ${soon ? "soon" : ""}">${fmtSecs(remaining)}</strong>${c.expired ? " · CADUCADO" : ""}</p>
    <p class="hint">Tamaño: ${fmtBytes(c.file_size_bytes)}</p>
    <div class="row">
      <button type="button" data-play="${c.id}" ${c.expired ? "disabled" : ""}>▶ Reproducir</button>
      <a href="/api/clips/${c.id}/download" download>⬇ Descargar</a>
      <button type="button" data-fav="${c.id}">${c.favorite ? "★ Favorito" : "☆ Favorito"}</button>
      <button type="button" data-del="${c.id}">🗑 Eliminar</button>
    </div>
  </article>`;
}

async function loadLibrary() {
  const q = $("#filter-q").value.trim();
  const platform = $("#filter-platform").value;
  const minScore = $("#filter-min-score").value || 0;
  const fav = $("#filter-favorites").checked;
  const params = new URLSearchParams();
  if (q) params.set("q", q);
  if (platform) params.set("platform", platform);
  if (Number(minScore) > 0) params.set("min_score", minScore);
  if (fav) params.set("favorites_only", "true");
  const list = $("#library-list");
  try {
    const clips = await api("/api/library?" + params.toString());
    libraryCache = clips;
    $("#library-empty").hidden = clips.length > 0;
    list.innerHTML = clips.map(clipCard).join("");
    list.querySelectorAll("[data-play]").forEach((b) => b.addEventListener("click", () => {
      const clip = libraryCache.find((c) => c.id === Number(b.dataset.play));
      const player = $("#player");
      player.src = `/api/clips/${b.dataset.play}/download`;
      $("#player-wrap").hidden = false;
      $("#player-title").textContent = clip ? clip.title : "";
      player.play().catch(() => {});
    }));
    list.querySelectorAll("[data-fav]").forEach((b) => b.addEventListener("click", async () => {
      try { await api(`/api/clips/${b.dataset.fav}/favorite`, { method: "POST" }); await loadLibrary(); }
      catch (e) { alert("Error: " + e.message); }
    }));
    list.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", async () => {
      if (!confirm("¿Eliminar este clip? Se borra el archivo y se libera espacio.")) return;
      try { const r = await api(`/api/clips/${b.dataset.del}`, { method: "DELETE" }); alert(`Eliminado. Espacio liberado: ${fmtBytes(r.freed_bytes)}`); await loadLibrary(); await loadQuota(); }
      catch (e) { alert("Error: " + e.message); }
    }));
  } catch (e) { list.innerHTML = "<p>Error al cargar la biblioteca: " + esc(e.message) + "</p>"; }
}
$("#library-filters").addEventListener("submit", (ev) => { ev.preventDefault(); loadLibrary(); });

// Cuenta atrás en vivo: decrementa seconds_remaining cada segundo (sin inventar datos,
// parte del valor que devolvió la API; se resincroniza al recargar la biblioteca).
setInterval(() => {
  document.querySelectorAll(".clip[data-remaining]").forEach((el) => {
    let rem = Number(el.dataset.remaining);
    if (rem > 0) rem -= 1;
    el.dataset.remaining = rem;
    const cd = el.querySelector(".countdown");
    if (cd) cd.textContent = fmtSecs(rem);
  });
}, 1000);

/* ---------- cola y momentos ---------- */
async function loadJobs() {
  try {
    const jobs = await api("/api/jobs");
    $("#jobs-list").innerHTML = jobs.length ? jobs.map((j) => `
      <li>Job #${j.id} · vídeo #${j.video_id} · ${esc(j.kind)} · estado: <strong>${esc(j.status)}</strong>
      ${j.error ? " · error: " + esc(j.error) : ""} · ${esc(j.created_at || "")}
      <button type="button" data-job="${j.id}">Ver momentos</button></li>`).join("") : "<li>Sin trabajos.</li>";
    $("#jobs-list").querySelectorAll("[data-job]").forEach((b) => b.addEventListener("click", async () => {
      try {
        const j = await api(`/api/jobs/${b.dataset.job}`);
        $("#moments-list").innerHTML = (j.moments || []).map((m) => `
          <li>Momento #${m.id} · ${fmtSecs(m.start)}–${fmtSecs(m.end)} · puntuación: ${m.score}<br>
          <span class="hint">${esc(m.text || "")}</span><br>
          <span class="hint">Títulos: ${esc((m.titles || []).join(" | "))}</span></li>`).join("") || "<li>Sin momentos en este trabajo.</li>";
      } catch (e) { alert("Error: " + e.message); }
    }));
  } catch (e) { $("#jobs-list").innerHTML = "<li>Error: " + esc(e.message) + "</li>"; }
  try {
    const min = $("#moments-min-score").value || 0;
    const moments = await api(`/api/moments?min_score=${min}`);
    if (!moments.length) { $("#moments-list").innerHTML = "<li>Sin momentos con esa puntuación mínima.</li>"; return; }
    $("#moments-list").innerHTML = moments.map((m) => `
      <li>Momento #${m.id} · job #${m.job_id} · vídeo #${m.video_id} · ${fmtSecs(m.start)}–${fmtSecs(m.end)} · puntuación: ${m.score}<br>
      <span class="hint">${esc(m.text || "")}</span><br>
      <span class="hint">Títulos: ${esc((m.titles || []).join(" | "))}</span></li>`).join("");
  } catch (e) { $("#moments-list").innerHTML = "<li>Error: " + esc(e.message) + "</li>"; }
}
$("#btn-refresh-jobs").addEventListener("click", loadJobs);

/* ---------- costes ---------- */
async function loadQuota() {
  const box = $("#quota-box");
  const alerts = $("#cost-alerts");
  try {
    const q = await api("/api/storage/quota");
    const pct = q.used_percent;
    box.innerHTML = `<strong>Almacenamiento (API, en vivo):</strong> ${fmtBytes(q.used_bytes)} usados de ${fmtBytes(q.limit_bytes)} (${q.limit_gb} GB) · ` +
      `<strong>${pct}% usado</strong> · disponible: ${fmtBytes(q.available_bytes)} · retención: ${q.retention_days} días · backend: ${esc(q.backend)}` +
      `<div class="bar"><div style="width:${Math.min(100, pct)}%"></div></div>`;
    alerts.innerHTML = "";
    if (pct >= 100) alerts.innerHTML = `<p class="alert danger">⛔ Cuota agotada (100%): nuevas subidas/trabajos en pausa hasta liberar espacio. El sistema no genera cargos.</p>`;
    else if (pct >= 80) alerts.innerHTML = `<p class="alert warn">⚠ Uso por encima del 80% (${pct}%): aviso + sugerencia de limpieza. Al superar 1 GB, las subidas se pausan hasta liberar espacio (el borrado a 7 días libera espacio).</p>`;
    else alerts.innerHTML = `<p class="alert ok">✓ Dentro del tramo gratuito de Supabase (1 GB). Gasto mensual real: 0 $.</p>`;
  } catch (e) { box.textContent = "No se pudo cargar la cuota: " + e.message; }
}

/* ---------- inicio ---------- */
loadHealth(); loadStreams(); loadLibrary(); loadJobs(); loadQuota(); loadCookiesStatus(); loadVideos();
setInterval(loadQuota, 60000);
setInterval(loadLibrary, 60000);
