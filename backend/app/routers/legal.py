"""
Legal documents router - serves privacy policy, terms of service, etc.

This provides a backend API for legal documents that can be consumed by
frontend applications. Documents are stored as Markdown files in the
legal_docs directory for easy editing and version control.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/v1/legal", tags=["legal"])


class LegalDocumentResponse(BaseModel):
    """Response model for legal document endpoints."""

    document_type: str
    title: str
    content: str
    last_updated: str
    version: str


# Path to legal documents directory
_LEGAL_DOCS_DIR = Path(__file__).parent.parent.parent / "legal_docs"

# Legal document metadata
# In production, this could be stored in a database or configuration file
_LEGAL_DOCUMENTS = {
    "privacy_policy": {
        "title": "Privacy Policy",
        "version": "1.0.0",
        "last_updated": "2024-01-15",
        "filename": "privacy_policy.md",
    },
    "terms_of_service": {
        "title": "Terms of Service",
        "version": "1.0.0",
        "last_updated": "2024-01-15",
        "filename": "terms_of_service.md",
    },
    "cookie_policy": {
        "title": "Cookie Policy",
        "version": "1.0.0",
        "last_updated": "2024-01-15",
        "filename": "cookie_policy.md",
    },
}


def _read_document_content(filename: str) -> str:
    """Read the content of a legal document from the legal_docs directory."""
    file_path = _LEGAL_DOCS_DIR / filename

    if not file_path.exists():
        raise FileNotFoundError(f"Legal document file not found: {file_path}")

    return file_path.read_text(encoding="utf-8")


@router.get("/{document_type}", response_model=LegalDocumentResponse)
async def get_legal_document(document_type: str) -> LegalDocumentResponse:
    """Retrieve a legal document by type.

    Available document types:
    - privacy_policy
    - terms_of_service
    - cookie_policy

    Returns 404 if the document type is not found.
    """
    document_metadata = _LEGAL_DOCUMENTS.get(document_type.lower())

    if not document_metadata:
        available_types = ", ".join(_LEGAL_DOCUMENTS.keys())
        raise HTTPException(
            status_code=404,
            detail=f"Document type '{document_type}' not found. Available types: {available_types}",
        )

    try:
        content = _read_document_content(document_metadata["filename"])
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=500, detail=f"Legal document file not found: {str(exc)}"
        ) from exc

    return LegalDocumentResponse(
        document_type=document_type,
        title=document_metadata["title"],
        content=content,
        last_updated=document_metadata["last_updated"],
        version=document_metadata["version"],
    )


@router.get("/", response_model=dict)
async def list_legal_documents() -> dict:
    """List all available legal documents with metadata."""
    return {
        "documents": [
            {
                "type": doc_type,
                "title": doc["title"],
                "version": doc["version"],
                "last_updated": doc["last_updated"],
            }
            for doc_type, doc in _LEGAL_DOCUMENTS.items()
        ]
    }
