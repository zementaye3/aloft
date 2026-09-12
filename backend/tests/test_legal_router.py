"""
Tests for legal documents router.

Covers:
- Retrieving individual legal documents
- Listing all available documents
- Error handling for non-existent documents
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def test_client() -> TestClient:
    """Legal document endpoints are public, no auth required."""
    return TestClient(app)


def test_get_privacy_policy(test_client):
    """Test retrieving the privacy policy document."""
    response = test_client.get("/v1/legal/privacy_policy")

    assert response.status_code == 200
    body = response.json()
    assert body["document_type"] == "privacy_policy"
    assert body["title"] == "Privacy Policy"
    assert "Privacy Policy" in body["content"]
    assert body["version"] == "1.0.0"
    assert body["last_updated"] == "2024-01-15"


def test_get_terms_of_service(test_client):
    """Test retrieving the terms of service document."""
    response = test_client.get("/v1/legal/terms_of_service")

    assert response.status_code == 200
    body = response.json()
    assert body["document_type"] == "terms_of_service"
    assert body["title"] == "Terms of Service"
    assert "Terms of Service" in body["content"]


def test_get_cookie_policy(test_client):
    """Test retrieving the cookie policy document."""
    response = test_client.get("/v1/legal/cookie_policy")

    assert response.status_code == 200
    body = response.json()
    assert body["document_type"] == "cookie_policy"
    assert body["title"] == "Cookie Policy"
    assert "Cookie Policy" in body["content"]


def test_list_legal_documents(test_client):
    """Test listing all available legal documents."""
    response = test_client.get("/v1/legal/")

    assert response.status_code == 200
    body = response.json()
    assert "documents" in body
    assert len(body["documents"]) == 3

    document_types = {doc["type"] for doc in body["documents"]}
    assert "privacy_policy" in document_types
    assert "terms_of_service" in document_types
    assert "cookie_policy" in document_types


def test_get_nonexistent_document_returns_404(test_client):
    """Test that requesting a non-existent document returns 404."""
    response = test_client.get("/v1/legal/nonexistent_document")

    assert response.status_code == 404
    body = response.json()
    assert "detail" in body
    assert "not found" in body["detail"].lower()


def test_document_case_insensitive(test_client):
    """Test that document type lookup is case-insensitive."""
    response = test_client.get("/v1/legal/PRIVACY_POLICY")

    assert response.status_code == 200
    body = response.json()
    assert body["document_type"] == "PRIVACY_POLICY"
    assert body["title"] == "Privacy Policy"
