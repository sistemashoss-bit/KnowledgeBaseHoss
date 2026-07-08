from __future__ import annotations

import os
import tempfile

from openai import OpenAI
from opensearchpy.helpers import bulk

from app.config import settings
from app import search as search_module


# ── Clients ───────────────────────────────────────────────────────────────────

def _llm_client() -> OpenAI:
    return OpenAI(base_url="https://openrouter.ai/api/v1", api_key=settings.openrouter_api_key)


def _voyage_client():
    import voyageai
    return voyageai.Client(api_key=settings.voyage_api_key)


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
            if text and not text.startswith("%PDF") and len(text) > 30:
                return text
        finally:
            os.unlink(tmp_path)
    except Exception:
        pass

    # 2. pypdf fallback for PDFs
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

    # 3. Plain-text files only
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


# ── Embeddings (Voyage AI) ────────────────────────────────────────────────────

def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """Batch-embed via Voyage AI. Returns None on failure."""
    if not texts:
        return []
    try:
        vo = _voyage_client()
        result = vo.embed(texts, model=settings.voyage_embedding_model, input_type="document")
        return result.embeddings
    except Exception:
        return None


def embed_text(text: str) -> list[float] | None:
    try:
        vo = _voyage_client()
        result = vo.embed([text], model=settings.voyage_embedding_model, input_type="query")
        return result.embeddings[0]
    except Exception:
        return None


# ── Reranking (Voyage AI) ─────────────────────────────────────────────────────

def rerank_chunks(question: str, chunks: list[dict], top_k: int = 6) -> list[dict]:
    """Rerank chunk dicts by relevance to question. Falls back to original order."""
    if not chunks:
        return chunks
    try:
        vo = _voyage_client()
        docs = [c["content"] for c in chunks]
        result = vo.rerank(question, docs, model=settings.voyage_rerank_model, top_k=top_k)
        return [chunks[r.index] for r in result.results]
    except Exception:
        return chunks[:top_k]


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

    chunks = chunk_text(text)
    if not chunks:
        return

    # Batch-generate embeddings for all chunks in one API call
    embeddings = embed_texts(chunks)

    actions = []
    for i, chunk in enumerate(chunks):
        source: dict = {
            "document_id": doc_id,
            "document_title": title,
            "department_id": department_id or "",
            "department_name": department_name or "",
            "status": status,
            "chunk_index": i,
            "content": chunk,
        }
        if embeddings is not None:
            source["embedding"] = embeddings[i]

        actions.append({
            "_index": search_module.CHUNKS_INDEX,
            "_id": f"{doc_id}_{i}",
            "_source": source,
        })

    if actions:
        bulk(client, actions)


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


# ── Document search (BM25 — listing/filtering, not Q&A) ──────────────────────

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


# ── Q&A: hybrid search (BM25 + kNN) with RRF merge ───────────────────────────

_RRF_K = 60  # standard RRF constant


def _rrf_merge(
    bm25_hits: list[dict],
    knn_hits: list[dict],
    top_k: int = 6,
) -> list[dict]:
    """Reciprocal Rank Fusion of two ranked hit lists."""
    scores: dict[str, float] = {}
    sources: dict[str, dict] = {}

    for rank, hit in enumerate(bm25_hits):
        cid = hit["_id"]
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (_RRF_K + rank + 1)
        sources[cid] = hit["_source"]

    for rank, hit in enumerate(knn_hits):
        cid = hit["_id"]
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (_RRF_K + rank + 1)
        sources[cid] = hit["_source"]

    top_ids = sorted(scores, key=lambda x: scores[x], reverse=True)[:top_k]
    return [sources[cid] for cid in top_ids]


def answer_question(
    question: str,
    access_filter: dict,
    *,
    role: str = "anon",
    dept_id: str | None = None,
) -> dict:
    from app import valkey_client as vk

    cached = vk.get_cached_rag(question, role, dept_id)
    if cached:
        return {"answer": cached, "sources": []}

    client = search_module.get_client()

    # ── BM25 search ──────────────────────────────────────────────────────────
    try:
        bm25_res = client.search(
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
                "size": 10,
            },
        )
        bm25_hits = bm25_res["hits"]["hits"]
    except Exception:
        bm25_hits = []

    # ── kNN search (skipped gracefully if embeddings unavailable) ─────────────
    knn_hits: list[dict] = []
    query_embedding = embed_text(question)
    if query_embedding is not None:
        try:
            knn_res = client.search(
                index=search_module.CHUNKS_INDEX,
                body={
                    "query": {
                        "bool": {
                            "must": {
                                "knn": {
                                    "embedding": {
                                        "vector": query_embedding,
                                        "k": 10,
                                    }
                                }
                            },
                            "filter": [access_filter],
                        }
                    },
                    "size": 10,
                },
            )
            knn_hits = knn_res["hits"]["hits"]
        except Exception:
            knn_hits = []

    # ── Merge with RRF → Voyage Rerank ───────────────────────────────────────
    rrf_chunks = _rrf_merge(bm25_hits, knn_hits, top_k=20)
    top_chunks = rerank_chunks(question, rrf_chunks, top_k=6)

    if not top_chunks:
        return {
            "answer": "No encontré documentos relevantes para tu pregunta.",
            "sources": [],
        }

    context_parts: list[str] = []
    seen: dict[str, dict] = {}
    for src in top_chunks:
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

    llm = _llm_client()
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
