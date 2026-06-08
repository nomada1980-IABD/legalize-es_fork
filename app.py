"""
Consultor Legislativo España – backend FastAPI
Lee los documentos del repositorio legalize-es y expone endpoints de
búsqueda y consulta con IA (Claude).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import List, Optional

import yaml
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

REPO_PATH = Path(__file__).parent
INDEX_CACHE = Path("/tmp/legalize_index.json")
MAX_RESULTS = 30
PREVIEW_CHARS = 400

REGIONS: dict[str, str] = {
    "es": "España (Estatal)",
    "es-an": "Andalucía",
    "es-ar": "Aragón",
    "es-as": "Asturias",
    "es-cb": "Cantabria",
    "es-cl": "Castilla y León",
    "es-cm": "Castilla-La Mancha",
    "es-cn": "Canarias",
    "es-ct": "Cataluña",
    "es-ex": "Extremadura",
    "es-ga": "Galicia",
    "es-ib": "Islas Baleares",
    "es-mc": "Murcia",
    "es-md": "Madrid",
    "es-nc": "Navarra",
    "es-pv": "País Vasco",
    "es-ri": "La Rioja",
    "es-vc": "Valencia",
}

RANK_LABELS: dict[str, str] = {
    "ley": "Ley",
    "ley_organica": "Ley Orgánica",
    "real_decreto_ley": "Real Decreto-ley",
    "real_decreto": "Real Decreto",
    "decreto": "Decreto",
    "orden": "Orden",
    "resolucion": "Resolución",
    "instruccion": "Instrucción",
    "circular": "Circular",
    "convenio": "Convenio",
    "acuerdo": "Acuerdo",
    "anuncio": "Anuncio",
    "correccion_errores": "Corrección de errores",
}

STOP_WORDS = {
    "de", "la", "el", "en", "y", "a", "que", "los", "las", "se", "del",
    "al", "por", "con", "un", "una", "para", "es", "su", "sus", "o", "e",
    "no", "si", "más", "sobre", "esta", "este", "ello", "como", "entre",
    "lo", "le", "les", "todo", "todos", "todas", "ha", "han", "hay", "ser",
    "fue", "son", "cual", "cuales",
}

# ---------------------------------------------------------------------------
# Estado global del índice
# ---------------------------------------------------------------------------

_index: List[dict] = []
_index_ready = threading.Event()
_index_error: str | None = None
_index_total = 0
_index_loaded = 0


# ---------------------------------------------------------------------------
# Parseo de documentos
# ---------------------------------------------------------------------------

def _parse_doc(filepath: Path) -> dict | None:
    try:
        text = filepath.read_text(errors="ignore")
    except OSError:
        return None

    if not text.startswith("---"):
        return None

    end = text.find("---", 3)
    if end == -1:
        return None

    yaml_text = text[3:end].strip()
    content = text[end + 3:].strip()

    try:
        meta = yaml.safe_load(yaml_text)
    except yaml.YAMLError:
        return None

    if not isinstance(meta, dict):
        return None

    # Strip markdown from content preview
    clean = re.sub(r"#+\s*", "", content)
    clean = re.sub(r"\*+", "", clean)
    clean = re.sub(r"\n{2,}", "\n", clean).strip()

    meta["_preview"] = clean[:PREVIEW_CHARS]
    meta["_region"] = filepath.parent.name
    meta["_filepath"] = str(filepath)
    meta["_filename"] = filepath.name

    # Normalise subjects to list
    subjects = meta.get("subjects")
    if isinstance(subjects, str):
        meta["subjects"] = [s.strip() for s in subjects.split(",")]
    elif not isinstance(subjects, list):
        meta["subjects"] = []

    return meta


def _build_index() -> None:
    global _index, _index_error, _index_total, _index_loaded

    # Try cache first (only valid if repo hasn't changed)
    if INDEX_CACHE.exists():
        try:
            data = json.loads(INDEX_CACHE.read_text())
            if isinstance(data, list) and len(data) > 100:
                _index = data
                _index_ready.set()
                return
        except Exception:
            pass

    docs = []
    dirs = sorted(
        [d for d in REPO_PATH.iterdir() if d.is_dir() and d.name.startswith("es")],
        key=lambda d: (0 if d.name == "es" else 1, d.name),
    )

    all_files: List[Path] = []
    for d in dirs:
        all_files.extend(sorted(d.glob("*.md")))

    _index_total = len(all_files)

    for i, fp in enumerate(all_files):
        doc = _parse_doc(fp)
        if doc:
            docs.append(doc)
        _index_loaded = i + 1

    _index = docs

    try:
        INDEX_CACHE.write_text(json.dumps(docs, ensure_ascii=False))
    except Exception:
        pass

    _index_ready.set()


threading.Thread(target=_build_index, daemon=True, name="index-builder").start()


# ---------------------------------------------------------------------------
# Motor de búsqueda
# ---------------------------------------------------------------------------

def _tokenise(text: str) -> List[str]:
    tokens = re.findall(r"[a-záéíóúüñ]+", text.lower())
    return [t for t in tokens if t not in STOP_WORDS and len(t) > 2]


def _score_doc(doc: dict, terms: List[str]) -> int:
    title = (doc.get("title") or "").lower()
    subjects = " ".join(doc.get("subjects") or []).lower()
    preview = (doc.get("_preview") or "").lower()
    dept = (doc.get("department") or "").lower()
    rank = (doc.get("rank") or "").lower()
    alerts = (doc.get("alerts") or "").lower()

    score = 0
    for term in terms:
        if term in title:
            score += 12
        if term in subjects:
            score += 8
        if term in dept:
            score += 5
        if term in alerts:
            score += 4
        if term in rank:
            score += 3
        if term in preview:
            score += 2
    return score


def search(
    query: str,
    region: str | None = None,
    rank_filter: str | None = None,
    status_filter: str | None = None,
    limit: int = MAX_RESULTS,
) -> List[dict]:
    terms = _tokenise(query)
    if not terms:
        return []

    results: List[tuple[int, dict]] = []

    for doc in _index:
        if region and doc.get("_region") != region:
            continue
        if rank_filter and doc.get("rank") != rank_filter:
            continue
        if status_filter and doc.get("status") != status_filter:
            continue

        score = _score_doc(doc, terms)
        if score > 0:
            results.append((score, doc))

    results.sort(key=lambda x: (x[0], x[1].get("publication_date") or ""), reverse=True)
    return [doc for _, doc in results[:limit]]


# ---------------------------------------------------------------------------
# Consulta con Claude
# ---------------------------------------------------------------------------

async def ask_claude(question: str, docs: List[dict]) -> str | None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)

        context_parts: List[str] = []
        for i, doc in enumerate(docs[:6], 1):
            subjects = ", ".join(doc.get("subjects") or [])
            context_parts.append(
                f"[{i}] {doc.get('title', 'Sin título')}\n"
                f"    ID: {doc.get('identifier', '')}\n"
                f"    Tipo: {RANK_LABELS.get(doc.get('rank', ''), doc.get('rank', ''))}\n"
                f"    Fecha: {doc.get('publication_date', '')}\n"
                f"    Estado: {'Vigente' if doc.get('status') == 'in_force' else doc.get('status', '')}\n"
                f"    Departamento: {doc.get('department', '')}\n"
                f"    Materias: {subjects}\n"
                f"    Fuente: {doc.get('source', '')}\n\n"
                f"    {doc.get('_preview', '')}\n"
            )

        context = "\n---\n".join(context_parts)

        system_prompt = (
            "Eres un asistente jurídico especializado en legislación española. "
            "Tu misión es ayudar a ciudadanos, empresas y profesionales a entender "
            "la normativa vigente de forma clara y práctica. "
            "Responde siempre en español. Cita los identificadores BOE cuando los menciones. "
            "Si la información no está en los documentos proporcionados, indícalo. "
            "Sé conciso pero completo. Usa listas cuando sea útil."
        )

        user_message = (
            f"Pregunta: {question}\n\n"
            f"Documentos legislativos relevantes encontrados en la base de datos:\n\n"
            f"{context}\n\n"
            "Por favor responde la pregunta basándote en estos documentos. "
            "Indica al final las referencias BOE utilizadas."
        )

        message = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=1500,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )

        return message.content[0].text

    except Exception as exc:
        return f"⚠️ Error al consultar la IA: {exc}"


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Consultor Legislativo España", version="1.0.0")

app.mount("/static", StaticFiles(directory=str(REPO_PATH / "static")), name="static")


class QueryRequest(BaseModel):
    question: str
    region: Optional[str] = None
    rank: Optional[str] = None
    status: Optional[str] = None


@app.get("/", response_class=FileResponse)
def root():
    return FileResponse(str(REPO_PATH / "static" / "index.html"))


@app.get("/api/status")
def api_status():
    ready = _index_ready.is_set()
    return {
        "ready": ready,
        "total_docs": len(_index) if ready else _index_loaded,
        "total_files": _index_total,
        "has_ai": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "regions": REGIONS,
        "ranks": RANK_LABELS,
    }


@app.get("/api/search")
def api_search(
    q: str = Query(..., min_length=2),
    region: Optional[str] = None,
    rank: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = Query(default=20, le=50),
):
    if not _index_ready.is_set():
        raise HTTPException(status_code=503, detail="Índice en construcción, espera unos segundos.")

    results = search(q, region=region, rank_filter=rank, status_filter=status, limit=limit)
    return {
        "query": q,
        "total": len(results),
        "results": [_serialise(d) for d in results],
    }


@app.post("/api/ask")
async def api_ask(req: QueryRequest):
    if not _index_ready.is_set():
        raise HTTPException(status_code=503, detail="Índice en construcción, espera unos segundos.")

    results = search(
        req.question,
        region=req.region,
        rank_filter=req.rank,
        status_filter=req.status,
        limit=10,
    )

    ai_answer = await ask_claude(req.question, results)

    return {
        "question": req.question,
        "ai_answer": ai_answer,
        "sources": [_serialise(d) for d in results[:8]],
    }


@app.get("/api/recent")
def api_recent(region: Optional[str] = None, limit: int = 15):
    if not _index_ready.is_set():
        raise HTTPException(status_code=503, detail="Índice en construcción.")

    docs = [
        d for d in _index
        if (not region or d.get("_region") == region)
        and d.get("publication_date")
    ]
    docs.sort(key=lambda d: d.get("publication_date") or "", reverse=True)
    return {"results": [_serialise(d) for d in docs[:limit]]}


@app.get("/api/doc/{identifier}")
def api_doc(identifier: str):
    if not _index_ready.is_set():
        raise HTTPException(status_code=503, detail="Índice en construcción.")

    for doc in _index:
        if doc.get("identifier") == identifier or doc.get("_filename") == f"{identifier}.md":
            fp = Path(doc["_filepath"])
            try:
                full_text = fp.read_text(errors="ignore")
                end = full_text.find("---", 3)
                content = full_text[end + 3:].strip() if end != -1 else full_text
                return {**_serialise(doc), "full_content": content[:8000]}
            except OSError:
                return _serialise(doc)

    raise HTTPException(status_code=404, detail="Documento no encontrado.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _serialise(doc: dict) -> dict:
    """Return a JSON-safe subset of a document record."""
    return {
        "identifier": doc.get("identifier", ""),
        "title": doc.get("title", "Sin título"),
        "rank": doc.get("rank", ""),
        "rank_label": RANK_LABELS.get(doc.get("rank", ""), doc.get("rank", "")),
        "publication_date": doc.get("publication_date", ""),
        "last_updated": doc.get("last_updated", ""),
        "status": doc.get("status", ""),
        "department": doc.get("department", ""),
        "subjects": doc.get("subjects") or [],
        "source": doc.get("source", ""),
        "pdf_url": doc.get("pdf_url") or doc.get("url_pdf", ""),
        "scope": doc.get("scope", ""),
        "region": REGIONS.get(doc.get("_region", ""), doc.get("_region", "")),
        "region_code": doc.get("_region", ""),
        "preview": doc.get("_preview", ""),
        "official_number": doc.get("official_number", ""),
        "alerts": doc.get("alerts", ""),
    }


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
