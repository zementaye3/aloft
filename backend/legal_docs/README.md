# Legal Documents

This directory contains the legal documents for the Aloft application.

## Available Documents

- `privacy_policy.md` - Privacy Policy
- `terms_of_service.md` - Terms of Service  
- `cookie_policy.md` - Cookie Policy

## How to Edit

1. **Edit the Markdown files** directly in this directory
2. **Update metadata** in `app/routers/legal.py` if you add new documents:
   ```python
   "new_document": {
       "title": "New Document Title",
       "version": "1.0.0",
       "last_updated": "2024-01-15",
       "filename": "new_document.md"
   }
   ```
3. **Restart the application** to see changes

## Adding New Documents

1. Create a new Markdown file in this directory
2. Add metadata to `_LEGAL_DOCUMENTS` in `app/routers/legal.py`
3. The document will be automatically available via the API

## API Endpoints

- `GET /v1/legal/` - List all documents
- `GET /v1/legal/{document_type}` - Get specific document

## Important Notes

- Always update the `last_updated` date when making changes
- Increment the `version` number for significant changes
- Ensure legal review before deploying to production
- Consider GDPR/CCPA compliance for your target markets
