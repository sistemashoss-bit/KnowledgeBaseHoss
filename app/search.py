from opensearchpy import OpenSearch
from app.config import settings

_client: OpenSearch | None = None

DOCUMENTS_INDEX = "kb_documents"
CHUNKS_INDEX = "kb_chunks"


def get_client() -> OpenSearch:
    global _client
    if _client is None:
        _client = OpenSearch(settings.opensearch_url, use_ssl=True, verify_certs=False)
    return _client


def ensure_indices() -> None:
    client = get_client()

    if not client.indices.exists(index=DOCUMENTS_INDEX):
        client.indices.create(
            index=DOCUMENTS_INDEX,
            body={
                "mappings": {
                    "properties": {
                        "id": {"type": "keyword"},
                        "title": {"type": "text"},
                        "description": {"type": "text"},
                        "department_id": {"type": "keyword"},
                        "department_name": {"type": "keyword"},
                        "status": {"type": "keyword"},
                        "content_type": {"type": "keyword"},
                        "uploaded_by": {"type": "keyword"},
                        "created_at": {"type": "date"},
                    }
                }
            },
        )

    if not client.indices.exists(index=CHUNKS_INDEX):
        client.indices.create(
            index=CHUNKS_INDEX,
            body={
                "mappings": {
                    "properties": {
                        "document_id": {"type": "keyword"},
                        "document_title": {"type": "text"},
                        "department_id": {"type": "keyword"},
                        "department_name": {"type": "keyword"},
                        "status": {"type": "keyword"},
                        "chunk_index": {"type": "integer"},
                        "content": {"type": "text"},
                    }
                }
            },
        )
