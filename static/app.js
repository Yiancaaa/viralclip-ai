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

/* ---------- subida + clasificación ---------- */
$("#upload-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const file = $("#upload-file").files[0];
  if (!file) return;
  const box = $("#upload-result");
  box.textContent = "Subiendo… (puede tardar; el archivo se hashea con SHA-256)";
  const fd = new FormData();
  fd.append("file", file);
  fd.append("title", $("#upload-title").value || file.name);
  try {
    const r = await fetch("/api/videos/upload", { method: "POST", body: fd });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
    currentVideoId = data.id;
    const dup = data.duplicate_of && data.duplicate_of !== undefined
      ? ` · ⚠ duplicado de vídeo #${data.duplicate_of} (mismo SHA-256, no se guardó otra copia)`
      : "";
    box.textContent = `Vídeo #${data.id} · título: ${data.title || ""} · ${fmtBytes(data.size_bytes)}${dup}`;
    $("#video-panel").hidden = false;
    $("#video-id-label").textContent = "#" + data.id;
    $("#video-info").textContent = `file_path: ${data.file_path || "—"}`;
    await loadVideo(data.id);
  } catch (e) { box.textContent = "Error al subir: " + e.message; }
});

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

$("#btn-process").addEventListener("click", async () => {
  if (!currentVideoId) return;
  const box = $("#process-result");
  box.textContent = "Procesando… (síncrono en el servidor: transcripción + momentos + render; puede tardar varios minutos en un fragmento largo). No cierres la página.";
  try {
    const r = await api(`/api/videos/${currentVideoId}/process`, { method: "POST" });
    box.textContent = `Job #${r.job_id} · estado: ${r.status} · momentos: ${r.moments} · clips: ${r.clips ? r.clips.length : 0} · categoría usada: ${r.category} (origen: ${r.category_source})`;
    await loadLibrary(); await loadJobs();
  } catch (e) { box.textContent = "Error al procesar: " + e.message; }
});

/* ---------- biblioteca ---------- */
function clipCard(c) {
  const remaining = c.seconds_remaining ?? 0;
  const soon = remaining > 0 && remaining < 86400;
  return `<article class="clip" data-clip-id="${c.id}" data-remaining="${remaining}">
    <div class="thumb-placeholder" aria-label="Sin miniatura disponible">
      <span>Sin miniatura<br>(pendiente endpoint)</span>
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
    else if (pct >= 80) alerts.innerHTML = `<p class="alert warn">⚠ Uso por encima del 80% (${pct}%): aviso + sugerencia de limpieza. Al superar 10 GB retenidos el coste dejaría de ser 0 $ (0,015 $/GB-mes).</p>`;
    else alerts.innerHTML = `<p class="alert ok">✓ Dentro del tramo gratuito de R2 (10 GB). Gasto mensual real: 0 $.</p>`;
  } catch (e) { box.textContent = "No se pudo cargar la cuota: " + e.message; }
}

/* ---------- inicio ---------- */
loadHealth(); loadStreams(); loadLibrary(); loadJobs(); loadQuota();
setInterval(loadQuota, 60000);
setInterval(loadLibrary, 60000);
