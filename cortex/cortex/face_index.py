"""On-device face recognition for the People-Recall journey (Zack 2026-06-01).

Two deterministic operations, NO LLM in the hot path:
  - `match(jpeg)`  → which enrolled person is this? (recall, latency-critical)
  - `enroll(jpeg)` → store a person's face embedding (one-time, at enroll time)

Design notes
------------
* **Biometric retrieval, not image→text.** This does face embedding + nearest
  neighbour over the wearer's own gallery; it never turns the photo into a text
  description fed to a model (that's the C-77 ban). The recall card renders a
  blurb that was written at ENROLL time — recall touches no LLM at all.
* **In-process, deterministic, CPU.** InsightFace (SCRFD detect + ArcFace 512-d)
  via onnxruntime (CoreML EP on Apple Silicon). The gallery is tiny (tens–
  hundreds), so matching is a single normalized matmul → argmax (sub-ms). The
  whole module is synchronous and CPU-bound; the async server calls it through
  `asyncio.to_thread(...)` so the event loop never blocks.
* **Lazy + graceful.** Heavy imports (insightface, cv2) load on first use, and
  numpy is guarded — if the deps aren't installed yet, `available()` returns
  False and the server shows a graceful card instead of crashing on import.
* **Source of truth is the per-person `.md`; `index.json` is a derived cache**
  (rebuildable). Each entry carries the precomputed embedding so startup is a
  plain JSON read — no re-embedding the gallery.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

try:
    import numpy as np
    _NUMPY_OK = True
except Exception:  # pragma: no cover - deps not installed yet
    np = None  # type: ignore
    _NUMPY_OK = False

# Tunables (env-overridable so Phase-0 calibration needs no code edit).
#   MODEL: buffalo_l (det_10g + w600k_r50, accurate) | buffalo_s (smaller/faster)
#   THRESHOLD: cosine sim over L2-normalized ArcFace embeddings; 0.40 is a
#     conservative start for "same person" — tune with real samples.
_MODEL = os.environ.get("CONSTELLATION_FACE_MODEL", "buffalo_l")
_THRESHOLD = float(os.environ.get("CONSTELLATION_FACE_THRESHOLD", "0.40"))
_DET_SIZE = int(os.environ.get("CONSTELLATION_FACE_DET_SIZE", "640"))


class FaceEngine:
    """Lazy singleton wrapper over InsightFace `FaceAnalysis`.

    `embed()` returns the L2-normalized 512-d embedding of the LARGEST face in
    the frame (the person you're facing), plus its bbox and the decoded image so
    callers can save a crop. Returns None when no face is found / deps missing.
    """

    def __init__(self, model: str = _MODEL, det_size: int = _DET_SIZE) -> None:
        self._model = model
        self._det_size = det_size
        self._app: Any = None  # insightface.app.FaceAnalysis

    @property
    def loaded(self) -> bool:
        return self._app is not None

    def load(self) -> None:
        """Load the model pack (downloads to ~/.insightface on first ever run).
        Blocking + heavy — call once, off the event loop (warmup thread)."""
        if self._app is not None:
            return
        t0 = time.monotonic()
        # CoreML first (Apple Silicon), CPU fallback. Lazy import so a missing
        # dep can't break `import cortex.server`.
        from insightface.app import FaceAnalysis  # type: ignore

        providers = ["CoreMLExecutionProvider", "CPUExecutionProvider"]
        app = FaceAnalysis(name=self._model, providers=providers)
        app.prepare(ctx_id=0, det_size=(self._det_size, self._det_size))
        self._app = app
        log.info(
            "face_engine.loaded",
            model=self._model, det_size=self._det_size,
            elapsed_ms=round((time.monotonic() - t0) * 1000),
        )

    def embed(self, jpeg_bytes: bytes) -> dict[str, Any] | None:
        """Decode JPEG → detect → pick largest face → normalized embedding.
        Returns {embedding(np.float32[512]), bbox(list[int]), det_score(float),
        bgr(np.ndarray)} or None (no face / decode failure)."""
        if not _NUMPY_OK:
            return None
        self.load()
        import cv2  # type: ignore

        buf = np.frombuffer(jpeg_bytes, dtype=np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)  # BGR
        if img is None:
            log.warning("face_engine.decode_failed", n_bytes=len(jpeg_bytes))
            return None
        t0 = time.monotonic()
        faces = self._app.get(img)
        if not faces:
            log.info("face_engine.no_face", elapsed_ms=round((time.monotonic() - t0) * 1000))
            return None
        # Largest bbox = the person in front of you.
        face = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
        emb = np.asarray(face.normed_embedding, dtype=np.float32)  # already L2-normed
        log.info(
            "face_engine.embedded",
            n_faces=len(faces), det_score=round(float(face.det_score), 3),
            elapsed_ms=round((time.monotonic() - t0) * 1000),
        )
        return {
            "embedding": emb,
            "bbox": [int(x) for x in face.bbox],
            "det_score": float(face.det_score),
            "bgr": img,
        }


class FaceIndex:
    """The matchable gallery: in-memory (N, 512) matrix + per-entry metadata,
    persisted to `<people_root>/_faces/index.json`. Cosine match = matrix @ emb
    (both L2-normed) → argmax. Sync/CPU — server wraps calls in to_thread."""

    def __init__(self, people_root: str | Path, *, threshold: float = _THRESHOLD,
                 engine: FaceEngine | None = None) -> None:
        self.people_root = Path(people_root).expanduser()
        self.faces_dir = self.people_root / "_faces"
        self.index_path = self.faces_dir / "index.json"
        self.threshold = threshold
        self.engine = engine or FaceEngine()
        self._entries: list[dict[str, Any]] = []
        self._matrix: Any = None  # np.ndarray (N, 512) or None
        self.load()

    # ── persistence ──────────────────────────────────────────────────────────
    def available(self) -> bool:
        """True iff face deps importable (numpy at least; insightface verified
        lazily on first embed). Server uses this to degrade gracefully."""
        return _NUMPY_OK

    def load(self) -> None:
        self._entries = []
        self._matrix = None
        if not self.index_path.exists():
            return
        try:
            raw = json.loads(self.index_path.read_text())
            entries = raw if isinstance(raw, list) else raw.get("entries", [])
        except Exception as e:
            log.warning("face_index.load_failed", error=str(e))
            return
        self._entries = [e for e in entries if e.get("embedding")]
        if _NUMPY_OK and self._entries:
            self._matrix = np.asarray(
                [e["embedding"] for e in self._entries], dtype=np.float32)
        log.info("face_index.loaded", n=len(self._entries), path=str(self.index_path))

    reload = load  # alias

    def _persist(self) -> None:
        self.faces_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._entries, ensure_ascii=False, indent=1))
        tmp.replace(self.index_path)

    # ── recall (hot path) ────────────────────────────────────────────────────
    def match(self, jpeg_bytes: bytes) -> tuple[dict[str, Any] | None, float]:
        """Recall: best gallery match for the face in `jpeg_bytes`.
        Returns (entry_without_embedding | None, score). None when no face,
        empty gallery, or best score < threshold. Deterministic, no LLM."""
        if not _NUMPY_OK or self._matrix is None or not self._entries:
            return None, 0.0
        got = self.engine.embed(jpeg_bytes)
        if got is None:
            return None, -1.0  # sentinel: a face was NOT found (vs no match)
        sims = self._matrix @ got["embedding"]  # (N,) cosine, both normalized
        idx = int(np.argmax(sims))
        score = float(sims[idx])
        if score < self.threshold:
            log.info("face_index.no_match", best=round(score, 3), threshold=self.threshold)
            return None, score
        entry = {k: v for k, v in self._entries[idx].items() if k != "embedding"}
        log.info("face_index.match", name=entry.get("name"), score=round(score, 3))
        return entry, score

    # ── enroll (one-time) ────────────────────────────────────────────────────
    def enroll(self, jpeg_bytes: bytes, *, slug: str, name: str, recall: str,
               profile_relpath: str, meta: dict[str, Any] | None = None,
               ) -> dict[str, Any] | None:
        """Compute + store a person's face embedding. Saves a crop to
        `_faces/<slug>.jpg`, appends an index entry (pointing at the profile
        `.md`), and reloads the in-memory matrix. Returns the entry, or None if
        no face was detected. Multiple entries per slug are allowed (re-enroll
        adds a sample; match takes the max over all)."""
        got = self.engine.embed(jpeg_bytes)
        if got is None:
            log.warning("face_index.enroll_no_face", slug=slug)
            return None
        self.faces_dir.mkdir(parents=True, exist_ok=True)
        face_rel = f"people/_faces/{slug}.jpg"
        try:
            import cv2  # type: ignore
            x1, y1, x2, y2 = got["bbox"]
            h, w = got["bgr"].shape[:2]
            mx, my = int((x2 - x1) * 0.3), int((y2 - y1) * 0.3)  # margin
            crop = got["bgr"][max(0, y1 - my):min(h, y2 + my),
                              max(0, x1 - mx):min(w, x2 + mx)]
            cv2.imwrite(str(self.people_root / "_faces" / f"{slug}.jpg"), crop)
        except Exception as e:
            log.warning("face_index.crop_failed", slug=slug, error=str(e))
        entry: dict[str, Any] = {
            "slug": slug,
            "name": name,
            "recall": recall,
            "profile": profile_relpath,
            "face": face_rel,
            "enrolled_at": datetime.now(timezone.utc).isoformat(),
            "embedding": [round(float(x), 6) for x in got["embedding"].tolist()],
            **(meta or {}),
        }
        self._entries.append(entry)
        self._persist()
        self.load()
        log.info("face_index.enroll", slug=slug, name=name, n_total=len(self._entries))
        return {k: v for k, v in entry.items() if k != "embedding"}

    def warm(self) -> None:
        """Load the model so the first match is fast. Call off the event loop."""
        try:
            self.engine.load()
        except Exception as e:
            log.warning("face_index.warm_failed", error=str(e))
