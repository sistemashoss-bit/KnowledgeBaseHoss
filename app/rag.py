from __future__ import annotations

import os
import tempfile

from openai import OpenAI

from app.config import settings
from app import search as search_module


# ── Text extraction ───────────────────────────────────────────────────────────

_TEXT_EXTS = {"txt", "md", "csv", "json", "xml", "html", "htm", "rst"}


def extract_text(content: bytes, filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    # 1. MarkItDown (handles docx, xlsx, pptx, pdf with embedded text, etc.)
    try:
        from markitdown import MarkItDown

        suffix = f".{ext}" if ext else ".bin"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        try:
            result = MarkItDown().convert(tmp_path)
            text = (result.text_content or "").strip()
            # Reject if result looks like raw binary (common PDF header pattern)
            if text and not text.startswith("%PDF") and len(text) > 30:
                return text
        finally:
            os.unlink(tmp_path)
    except Exception:
        pass

    # 2. pypdf fallback for PDFs (catches scanned/unusual PDFs MarkItDown misses)
    if ext == "pdf":
        try:
            import io as _io
            import pypdf
            reader = pypdf.PdfReader(_io.BytesIO(content))
            pages = [p.extract_text() or "" for p in reader.pages]
            text = "\n".join(pages).strip()
            if text:
                return text
        except Exception:
            pass

    # 3. Plain-text files only — never decode binary formats
    if ext in _TEXT_EXTS:
        try:
            return content.decode("utf-8", errors="ignore").strip()
        except Exception:
            pass

    return ""


def chunk_text(text: str, size: int = 500, overlap: int = 50) -> list[str]:
    words = text.split()
    if not words:
        return []
    chunks, i = [], 0
    while i < len(words):
        chunks.append(" ".join(words[i : i + size]))
        i += size - overlap
    return chunks


# ── OpenSearch indexing ───────────────────────────────────────────────────────

def index_document(
    doc_id: str,
    title: str,
    description: str,
    department_id: str,
    department_name: str,
    status: str,
    content_type: str,
    uploaded_by: str,
    text: str,
) -> None:
    client = search_module.get_client()

    client.index(
        index=search_module.DOCUMENTS_INDEX,
        id=doc_id,
        body={
            "id": doc_id,
            "title": title,
            "description": description or "",
            "department_id": department_id or "",
            "department_name": department_name or "",
            "status": status,
            "content_type": content_type or "",
            "uploaded_by": uploaded_by or "",
        },
    )

    for i, chunk in enumerate(chunk_text(text)):
        client.index(
            index=search_module.CHUNKS_INDEX,
            id=f"{doc_id}_{i}",
            body={
                "document_id": doc_id,
                "document_title": title,
                "department_id": department_id or "",
                "department_name": department_name or "",
                "status": status,
                "chunk_index": i,
                "content": chunk,
            },
        )


def delete_document_from_index(doc_id: str) -> None:
    client = search_module.get_client()
    try:
        client.delete(index=search_module.DOCUMENTS_INDEX, id=doc_id)
    except Exception:
        pass
    try:
        client.delete_by_query(
            index=search_module.CHUNKS_INDEX,
            body={"query": {"term": {"document_id": doc_id}}},
        )
    except Exception:
        pass


# ── Search ────────────────────────────────────────────────────────────────────

def search_documents(
    query: str,
    access_filter: dict,
    department_id: str | None = None,
    size: int = 24,
) -> list[dict]:
    client = search_module.get_client()

    must: list[dict] = (
        [{"multi_match": {"query": query, "fields": ["title^3", "description^2"]}}]
        if query
        else [{"match_all": {}}]
    )
    filters = [access_filter]
    if department_id:
        filters.append({"term": {"department_id": department_id}})

    try:
        res = client.search(
            index=search_module.DOCUMENTS_INDEX,
            body={"query": {"bool": {"must": must, "filter": filters}}, "size": size},
        )
        return [hit["_source"] for hit in res["hits"]["hits"]]
    except Exception:
        return []


# ── Q&A via OpenRouter ────────────────────────────────────────────────────────

def answer_question(question: str, access_filter: dict, *, role: str = "anon", dept_id: str | None = None) -> dict:
    client = search_module.get_client()

    try:
        res = client.search(
            index=search_module.CHUNKS_INDEX,
            body={
                "query": {
                    "bool": {
                        "must": {
                            "multi_match": {
                                "query": question,
                                "fields": ["content^2", "document_title^3"],
                            }
                        },
                        "filter": [access_filter],
                    }
                },
                "size": 6,
            },
        )
    except Exception:
        return {"answer": "Error al buscar documentos.", "sources": []}

    hits = res["hits"]["hits"]
    if not hits:
        return {
            "answer": "No encontré documentos relevantes para tu pregunta.",
            "sources": [],
        }

    context_parts: list[str] = []
    seen: dict[str, dict] = {}
    for hit in hits:
        src = hit["_source"]
        doc_id = src["document_id"]
        if doc_id not in seen:
            seen[doc_id] = {
                "title": src["document_title"],
                "department": src.get("department_name", ""),
            }
        context_parts.append(f"[{src['document_title']}]\n{src['content']}")

    context = "\n\n---\n\n".join(context_parts)
    prompt = (
        "Eres un asistente de base de conocimiento interna.\n"
        "Responde la pregunta ÚNICAMENTE con información de los documentos proporcionados.\n"
        "Si la respuesta no está en los documentos, indícalo claramente.\n"
        "Responde en el mismo idioma que la pregunta.\n\n"
        f"Pregunta: {question}\n\nDocumentos:\n{context}\n\nRespuesta:"
    )

    from app import valkey_client as vk
    cached = vk.get_cached_rag(question, role, dept_id)
    if cached:
        return {"answer": cached, "sources": [{"id": k, **v} for k, v in seen.items()]}

    llm = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=settings.openrouter_api_key,
    )
    completion = llm.chat.completions.create(
        model=settings.openrouter_model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1000,
    )
    answer = completion.choices[0].message.content
    vk.cache_rag(question, role, dept_id, answer)

    return {
        "answer": answer,
        "sources": [{"id": k, **v} for k, v in seen.items()],
    }
