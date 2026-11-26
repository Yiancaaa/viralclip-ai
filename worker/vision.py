"""Detección visual real con OpenCV (open source, 0 $): rostros, facecam,
área de gameplay y HUD/overlays, y decisión de composición vertical 9:16.

Sin modelos de pago ni servicios externos. El detector principal es el
Haar cascade ``haarcascade_frontalface_alt2`` que trae OpenCV (verificado
en esta VM con opencv-python-headless 4.11: el cascade ``default`` da
muchos falsos positivos sobre el gameplay — personajes del juego,
texturas de carretera/cielo — mientras que ``alt2`` solo detecta el
rostro real del streamer de forma estable).

Hallazgo importante medido en el segmento real (ver
``docs/PRUEBA_EDICION_V2_facecam.md``): los pesos ``levelWeights`` del
Haar NO distinguen el rostro real de los falsos positivos (FP ~55.0-55.4,
rostro real ~56.3-57.4). Lo que separa la facecam es la ESTABILIDAD EN
EL TIEMPO: el rostro de la facecam aparece en el 96 % de los fotogramas
en el mismo sitio (desviación típica ~3 px), mientras que los falsos
positivos del gameplay aparecen un solo fotograma en posiciones
aleatorias. Por eso ``detect_facecam`` agrupa por proximidad espacial y
exige cobertura temporal, y su confianza depende de cobertura x
compacidad, no del peso del cascade.

RAM: se procesan fotogramas muestreados (1 fps por defecto, ~960 px de
ancho), nunca el vídeo completo en memoria.
"""
from __future__ import annotations

import math
import statistics
from pathlib import Path

import cv2
import numpy as np

# ------------------------------------------------------------------ cascades
# alt2: medido con menos falsos positivos que default sobre gameplay real.
_CASCADE_NAME = "haarcascade_frontalface_alt2.xml"
_FALLBACK_CASCADE = "haarcascade_frontalface_default.xml"

_cascade = None


def _load_cascade() -> "cv2.CascadeClassifier":
    global _cascade
    if _cascade is None:
        path = Path(cv2.data.haarcascades) / _CASCADE_NAME
        if not path.exists():
            path = Path(cv2.data.haarcascades) / _FALLBACK_CASCADE
        _cascade = cv2.CascadeClassifier(str(path))
        if _cascade.empty():
            raise RuntimeError(f"no se pudo cargar el Haar cascade {path}")
    return _cascade


def _weight_to_conf(weight: float) -> float:
    """Peso del cascade -> confianza 0..1 (sigmoide centrada en 50).

    Solo mide cuánto "parece un rostro" el parche según Haar; NO separa
    falsos positivos del rostro real (ver docstring del módulo).
    """
    return 1.0 / (1.0 + math.exp(-(weight - 50.0) / 2.0))


# ------------------------------------------------------------------ muestreo
def sample_frames(video: str, fps: float = 1.0, max_seconds: float | None = None,
                  max_width: int = 960, limit: int | None = None) -> list[tuple[float, np.ndarray]]:
    """Fotogramas muestreados a `fps` (lectura secuencial, sin cargar el
    vídeo entero). Devuelve [(t_segundos, frame_BGR)].

    `max_seconds` / `limit` acotan el trabajo en vídeos largos (el .ts de
    prueba informa una duración errónea de ~4,7 h por sus timestamps).
    """
    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        raise RuntimeError(f"no se puede abrir el vídeo: {video}")
    try:
        src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        step = max(1, int(round(src_fps / fps)))
        out: list[tuple[float, np.ndarray]] = []
        idx = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if idx % step == 0:
                t = idx / src_fps
                if max_seconds is not None and t > max_seconds:
                    break
                if max_width and frame.shape[1] > max_width:
                    scale = max_width / frame.shape[1]
                    frame = cv2.resize(frame, (max_width, int(frame.shape[0] * scale)),
                                       interpolation=cv2.INTER_AREA)
                out.append((t, frame))
                if limit is not None and len(out) >= limit:
                    break
            idx += 1
        return out
    finally:
        cap.release()


# ------------------------------------------------------------------ rostros
def detect_faces(frame: np.ndarray, min_size: int = 30) -> list[tuple[int, int, int, int, float]]:
    """Rostros en un fotograma BGR -> [(x, y, w, h, confianza)] en píxeles.

    Ecualiza el histograma (ayuda con gameplay oscuro / contraluces) y
    usa ``detectMultiScale3`` para obtener el peso de cada detección.
    """
    if frame is None or frame.size == 0:
        return []
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    gray = cv2.equalizeHist(gray)
    cascade = _load_cascade()
    faces, _reject, weights = cascade.detectMultiScale3(
        gray, scaleFactor=1.08, minNeighbors=4, minSize=(min_size, min_size),
        outputRejectLevels=True)
    out = []
    for (x, y, w, h), weight in zip(faces, weights):
        out.append((int(x), int(y), int(w), int(h),
                    round(_weight_to_conf(float(weight)), 3)))
    return out


def _rel_bbox(bbox: tuple[float, float, float, float], w: int, h: int) -> tuple[float, float, float, float]:
    x, y, bw, bh = bbox
    return (x / w, y / h, bw / w, bh / h)


def _center(bbox: tuple[float, float, float, float]) -> tuple[float, float]:
    x, y, w, h = bbox
    return (x + w / 2.0, y + h / 2.0)


def _position_of(center: tuple[float, float]) -> str:
    cx, cy = center
    vertical = "top" if cy < 0.5 else "bottom"
    horizontal = "left" if cx < 0.5 else "right"
    if 0.35 < cx < 0.65 and 0.35 < cy < 0.65:
        return "center"
    return f"{vertical}-{horizontal}"


# ------------------------------------------------------------------ clustering
def _cluster_faces(detections: list[tuple[float, float, float, float]],
                   tol: float = 0.08) -> list[dict]:
    """Agrupa detecciones (centros relativos) por proximidad espacial.

    Cada cluster: centro medio, nº de fotogramas distintos con una
    detección en él, bboxes y desviación típica del centro. Un rostro
    real de facecam forma un cluster denso y compacto; los falsos
    positivos del gameplay quedan en clusters de 1 fotograma.
    """
    clusters: list[dict] = []
    for bbox in detections:
        cx, cy = _center(bbox)
        placed = False
        for cl in clusters:
            ccx, ccy = cl["center"]
            if abs(cx - ccx) <= tol and abs(cy - ccy) <= tol:
                cl["bboxes"].append(bbox)
                n = len(cl["bboxes"])
                cl["center"] = ((ccx * (n - 1) + cx) / n, (ccy * (n - 1) + cy) / n)
                cl["count"] += 1
                placed = True
                break
        if not placed:
            clusters.append({"center": (cx, cy), "bboxes": [bbox], "count": 1})
    for cl in clusters:
        centers = [_center(b) for b in cl["bboxes"]]
        cl["std"] = (statistics.pstdev([c[0] for c in centers]) if len(centers) > 1 else 0.0,
                     statistics.pstdev([c[1] for c in centers]) if len(centers) > 1 else 0.0)
        xs = [b[0] for b in cl["bboxes"]]
        ys = [b[1] for b in cl["bboxes"]]
        ws = [b[2] for b in cl["bboxes"]]
        hs = [b[3] for b in cl["bboxes"]]
        cl["median_bbox"] = (statistics.median(xs), statistics.median(ys),
                             statistics.median(ws), statistics.median(hs))
    clusters.sort(key=lambda c: c["count"], reverse=True)
    return clusters


def smooth_bbox_series(bboxes: list[tuple[float, float, float, float]],
                       window: int = 5) -> list[tuple[float, float, float, float]]:
    """Suavizado temporal: media móvil de la bbox (ventana `window`).

    Mantiene la región identificada aunque el rostro se mueva o cambie
    ligeramente de tamaño entre fotogramas; evita saltos bruscos del
    recorte de la facecam.
    """
    if window <= 1 or not bboxes:
        return list(bboxes)
    out = []
    for i in range(len(bboxes)):
        lo = max(0, i - window + 1)
        chunk = bboxes[lo:i + 1]
        out.append(tuple(sum(b[k] for b in chunk) / len(chunk) for k in range(4)))
    return out


# ------------------------------------------------------------------ facecam
# La caja de la facecam es mayor que el rostro que contiene: en el
# segmento real medido, la caja (~365x367 px sobre 1920x1080) es ~3,4x
# el lado del rostro (~140 px) y su centro está ~0,5 alturas de rostro
# por encima del centro del rostro (logo arriba, nombre abajo). Ambas
# constantes están calibradas con ese material y deben revalidarse con
# contenido propio.
FACECAM_EXPAND = 3.4
FACECAM_CENTER_LIFT_FACE_H = 0.5


def _face_to_facecam_box(face_rel: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """Estima la caja de la facecam a partir de la caja del rostro.

    El rostro es ~cuadrado en píxeles, así que expandir su caja por el
    mismo factor en coords relativas (x e y) da una caja ~cuadrada en
    píxeles, como la facecam real medida.
    """
    fx, fy, fw, fh = face_rel
    fcx, fcy = fx + fw / 2.0, fy + fh / 2.0
    new_w = fw * FACECAM_EXPAND
    new_h = fh * FACECAM_EXPAND
    cy = fcy - FACECAM_CENTER_LIFT_FACE_H * fh
    x = min(max(fcx - new_w / 2.0, 0.0), 1.0 - new_w)
    y = min(max(cy - new_h / 2.0, 0.0), 1.0 - new_h)
    return (round(x, 3), round(y, 3), round(new_w, 3), round(new_h, 3))


def _as_frames(frames) -> list[np.ndarray]:
    """Acepta [frame] o [(t, frame)] y devuelve [frame]."""
    out = []
    for item in frames:
        if isinstance(item, tuple):
            out.append(item[1])
        else:
            out.append(item)
    return out


def detect_facecam(frames, min_coverage: float = 0.5,
                   cluster_tol: float = 0.08) -> dict:
    """Detecta la facecam a partir de fotogramas muestreados.

    La facecam es una región PEQUEÑA y ESTABLE que contiene un rostro en
    la mayoría de los fotogramas. Devuelve::

        {present, bbox, position, confidence, stable, face_bbox,
         coverage, n_frames, faces, median_face_rel_area, large_face}

    - ``bbox``: caja estimada de la facecam, coords relativas 0..1.
    - ``position``: top-left | top-right | bottom-left | bottom-right |
      center | other.
    - ``faces``: clusters estables (para multi-ventana), cada uno con
      ``bbox`` (rostro, relativo), ``coverage`` y ``center``.
    - ``large_face``: el rostro mediano cubre >15 % del frame (streamer
      hablando a cámara: no hay facecam pequeña, el rostro ES el plano).
    """
    imgs = _as_frames(frames)
    n = len(imgs)
    if n == 0:
        return {"present": False, "bbox": None, "position": "other",
                "confidence": 0.0, "stable": False, "face_bbox": None,
                "coverage": 0.0, "n_frames": 0, "faces": [],
                "median_face_rel_area": 0.0, "large_face": False}
    all_dets: list[tuple[float, float, float, float]] = []
    frames_with_face = 0
    for img in imgs:
        h, w = img.shape[:2]
        dets = detect_faces(img)
        if dets:
            frames_with_face += 1
        for (x, y, bw, bh, _conf) in dets:
            all_dets.append(_rel_bbox((x, y, bw, bh), w, h))
    clusters = _cluster_faces(all_dets, tol=cluster_tol)
    stable_faces = []
    for cl in clusters:
        coverage = cl["count"] / n
        if coverage >= 0.3 and cl["count"] >= 3:
            stable_faces.append({
                "bbox": tuple(round(v, 3) for v in cl["median_bbox"]),
                "coverage": round(coverage, 3),
                "center": tuple(round(v, 3) for v in cl["center"]),
                "std": tuple(round(v, 4) for v in cl["std"]),
            })
    median_area = 0.0
    if all_dets:
        median_area = statistics.median([b[2] * b[3] for b in all_dets])
    large_face = median_area > 0.15
    result = {"present": False, "bbox": None, "position": "other",
              "confidence": 0.0, "stable": False, "face_bbox": None,
              "coverage": 0.0, "n_frames": n, "faces": stable_faces,
              "median_face_rel_area": round(median_area, 4),
              "large_face": large_face}
    if not stable_faces:
        return result
    main = stable_faces[0]
    face_bbox = main["bbox"]
    face_area = face_bbox[2] * face_bbox[3]
    coverage = main["coverage"]
    # Compacidad del cluster: desviación típica del centro (relativa).
    std = math.hypot(*main["std"])
    tightness = max(0.0, 1.0 - min(1.0, std / 0.05))
    confidence = round(min(1.0, coverage * (0.35 + 0.65 * tightness)), 3)
    stable = bool(coverage >= 0.6 and std < 0.02)
    # La facecam es pequeña: si el rostro ya es grande, no es facecam.
    present = bool(coverage >= min_coverage and face_area <= 0.20)
    if not present:
        result.update({"coverage": coverage, "face_bbox": face_bbox,
                       "confidence": confidence, "stable": stable})
        return result
    # Suavizado temporal: la mediana del cluster ya es robusta frente a
    # detecciones que saltan un fotograma; smooth_bbox_series queda para
    # series por fotograma (p. ej. seguimiento en vivo).
    facecam_box = _face_to_facecam_box(face_bbox)
    cx, cy = _center(facecam_box)
    position = _position_of((cx, cy))
    result.update({"present": True, "bbox": facecam_box, "position": position,
                   "confidence": confidence, "stable": stable,
                   "face_bbox": face_bbox, "coverage": coverage})
    return result


# ------------------------------------------------------------------ HUD / estático
def detect_static_regions(frames, diff_thresh: int = 25, static_frac: float = 0.8,
                          min_area_rel: float = 0.002) -> list[dict]:
    """Regiones ESTÁTICAS entre fotogramas (diferencia de frames).

    Lo que no cambia en varios segundos es overlay/HUD (minimapa, barras,
    marcos de facecam, textos fijos); lo que cambia es gameplay. Devuelve
    [{bbox (relativa), static_fraction}] ordenadas por área descendente.
    Trabaja a 320 px de ancho para no disparar la RAM/CPU.
    """
    imgs = _as_frames(frames)
    if len(imgs) < 3:
        return []
    small = []
    for img in imgs:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        if gray.shape[1] > 320:
            scale = 320 / gray.shape[1]
            gray = cv2.resize(gray, (320, int(gray.shape[0] * scale)),
                              interpolation=cv2.INTER_AREA)
        small.append(gray)
    h, w = small[0].shape[:2]
    static_count = np.zeros((h, w), dtype=np.float32)
    pairs = 0
    for a, b in zip(small, small[1:]):
        diff = cv2.absdiff(a, b)
        static_count += (diff < diff_thresh).astype(np.float32)
        pairs += 1
    static_mask = (static_count / max(pairs, 1) >= static_frac).astype(np.uint8) * 255
    static_mask = cv2.morphologyEx(
        static_mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    contours, _ = cv2.findContours(static_mask, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for cnt in contours:
        x, y, bw, bh = cv2.boundingRect(cnt)
        area_rel = (bw * bh) / (w * h)
        if area_rel < min_area_rel:
            continue
        frac = float(static_count[y:y + bh, x:x + bw].mean() / max(pairs, 1)) if pairs else 0.0
        out.append({"bbox": (round(x / w, 3), round(y / h, 3),
                             round(bw / w, 3), round(bh / h, 3)),
                    "static_fraction": round(min(1.0, frac), 3),
                    "area_rel": round(area_rel, 4)})
    out.sort(key=lambda r: r["area_rel"], reverse=True)
    return out


def detect_gameplay_area(frame: np.ndarray, facecam_bbox: tuple[float, float, float, float] | None = None,
                         static_regions: list[dict] | None = None) -> dict:
    """Región principal de gameplay: el mayor rectángulo del frame que no
    se solapa con la facecam (y, si se pasan, sin las regiones estáticas
    de HUD). Devuelve {bbox, facecam_bbox, static_regions,
    area_fraction, excluded}.

    ``bbox`` es relativa 0..1. Si no hay facecam, el gameplay es el
    frame completo. Nota: para el render vertical no se usa este rectángulo
    tal cual (el pane de gameplay usa crop centrado con cover, como v1);
    esta función describe dónde está el juego frente a overlays.
    """
    h, w = frame.shape[:2]
    _ = (h, w)
    static_regions = static_regions or []
    if not facecam_bbox:
        return {"bbox": (0.0, 0.0, 1.0, 1.0), "facecam_bbox": None,
                "static_regions": static_regions, "area_fraction": 1.0,
                "excluded": [r["bbox"] for r in static_regions]}
    fx, fy, fw, fh = facecam_bbox
    x1, y1, x2, y2 = fx + fw, fy + fh, fx, fy  # bordes de la facecam
    # Candidatos: franja derecha, franja izquierda, franja superior,
    # franja inferior (rectángulos que no tocan la facecam).
    candidates = [
        (x1, 0.0, 1.0 - x1, 1.0),   # derecha
        (0.0, 0.0, fx, 1.0),        # izquierda
        (0.0, 0.0, 1.0, fy),        # arriba
        (0.0, y1, 1.0, 1.0 - y1),   # abajo
    ]
    candidates = [c for c in candidates if c[2] > 0.05 and c[3] > 0.05]
    best = max(candidates, key=lambda c: c[2] * c[3])
    best = tuple(round(v, 3) for v in best)
    return {"bbox": best, "facecam_bbox": facecam_bbox,
            "static_regions": static_regions,
            "area_fraction": round(best[2] * best[3], 3),
            "excluded": [facecam_bbox] + [r["bbox"] for r in static_regions]}


# ------------------------------------------------------------------ layout
# Zona reservada a subtítulos en el lienzo 1080x1920 (ASS MarginV 180).
SUBTITLE_ZONE_H = 200
# Altura del pane de facecam en el lienzo vertical (px).
FACECAM_PANE_H = 480
GAMEPLAY_PANE_H = 1920 - FACECAM_PANE_H

LAYOUTS = ("facecam_top", "facecam_bottom", "gameplay_only",
           "talking_head", "multi_window")


def decide_layout(facecam: dict, gameplay: dict | None = None,
                  moment_context: dict | None = None) -> str:
    """Elige la composición vertical según lo detectado y el momento.

    - Varios rostros estables -> ``multi_window`` (o foco en quien habla
      si fuera detectable; sin diarización de audio no lo es: se usa
      multi-ventana).
    - Rostro grande sin facecam pequeña -> ``talking_head`` (streamer a
      cámara: el rostro es casi todo el plano).
    - Sin facecam -> ``gameplay_only`` (comportamiento v1).
    - Con facecam: momento de reacción (energía/emoción alta) ->
      ``facecam_top`` (la facecam se ve primero y nunca la tapan los
      subtítulos, que quedan abajo sobre el gameplay); acción del juego
      -> ``facecam_bottom`` (el gameplay manda arriba y los subtítulos se
      desplazan encima del pane de facecam).
    """
    ctx = moment_context or {}
    faces = (facecam or {}).get("faces", []) or []
    if len(faces) >= 2:
        return "multi_window"
    facecam = facecam or {}
    if facecam.get("large_face") and not facecam.get("present"):
        return "talking_head"
    if not facecam.get("present"):
        return "gameplay_only"
    energy = float(ctx.get("energy", 0.0) or 0.0)
    reaction = bool(ctx.get("reaction")) or energy > 0.7 or float(ctx.get("emotion_score", 0.0) or 0.0) > 0.7
    face_area = 0.0
    if facecam.get("face_bbox"):
        fb = facecam["face_bbox"]
        face_area = fb[2] * fb[3]
    # Rostro inusualmente grande dentro de la facecam = reacción visible.
    if face_area > 0.02:
        reaction = True
    if reaction:
        return "facecam_top"
    return "facecam_bottom"
