"""
Parseur API Client

Uploads invoice documents to Parseur for OCR processing.
API Documentation: https://developer.parseur.com/upload-emails-and-documents-guide
"""

import logging
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

import requests

from api_config import PARSEUR_API_KEY, PARSEUR_BASE_URL, get_route, is_parseur_configured

logger = logging.getLogger(__name__)


@dataclass
class UploadResult:
    """Result of uploading a document to Parseur."""
    success: bool
    supplier: str
    mailbox_id: str
    filename: str
    message: str
    response_data: Optional[dict] = None


class ParseurClient:
    """Client for interacting with Parseur API."""

    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize Parseur client.

        Args:
            api_key: Parseur API key. If not provided, uses PARSEUR_API_KEY env var.
        """
        self.api_key = api_key or PARSEUR_API_KEY
        self.base_url = PARSEUR_BASE_URL

        if not self.api_key:
            logger.warning("Parseur API key not configured. Set PARSEUR_API_KEY environment variable.")

    def is_configured(self) -> bool:
        """Check if API key is configured."""
        return bool(self.api_key)

    def upload_document(
        self,
        file_path: Path,
        mailbox_id: str,
        custom_params: Optional[dict] = None
    ) -> UploadResult:
        """
        Upload a document to Parseur for processing.

        Args:
            file_path: Path to the PDF or image file
            mailbox_id: Parseur mailbox ID to send to
            custom_params: Optional key-value pairs to include in parsed results

        Returns:
            UploadResult with success status and details
        """
        if not self.api_key:
            return UploadResult(
                success=False,
                supplier='',
                mailbox_id=mailbox_id,
                filename=file_path.name,
                message="Parseur API key not configured"
            )

        url = f"{self.base_url}/parser/{mailbox_id}/upload"

        headers = {
            "Authorization": self.api_key
        }

        try:
            with open(file_path, 'rb') as f:
                files = {'file': (file_path.name, f, 'application/pdf')}

                # Add custom parameters as form fields if provided
                data = custom_params or {}

                response = requests.post(
                    url,
                    headers=headers,
                    files=files,
                    data=data,
                    timeout=60
                )

            if response.status_code in (200, 201, 202):
                logger.info(f"Uploaded {file_path.name} to mailbox {mailbox_id}")
                return UploadResult(
                    success=True,
                    supplier='',
                    mailbox_id=mailbox_id,
                    filename=file_path.name,
                    message="Document uploaded successfully",
                    response_data=response.json() if response.text else None
                )
            else:
                error_msg = f"HTTP {response.status_code}: {response.text}"
                logger.error(f"Failed to upload {file_path.name}: {error_msg}")
                return UploadResult(
                    success=False,
                    supplier='',
                    mailbox_id=mailbox_id,
                    filename=file_path.name,
                    message=error_msg
                )

        except requests.exceptions.Timeout:
            return UploadResult(
                success=False,
                supplier='',
                mailbox_id=mailbox_id,
                filename=file_path.name,
                message="Request timed out"
            )
        except requests.exceptions.RequestException as e:
            return UploadResult(
                success=False,
                supplier='',
                mailbox_id=mailbox_id,
                filename=file_path.name,
                message=f"Request failed: {str(e)}"
            )
        except Exception as e:
            return UploadResult(
                success=False,
                supplier='',
                mailbox_id=mailbox_id,
                filename=file_path.name,
                message=f"Unexpected error: {str(e)}"
            )

    def upload_for_supplier(
        self,
        file_path: Path,
        supplier: str,
        custom_params: Optional[dict] = None
    ) -> UploadResult:
        """
        Upload a document using the configured route for a supplier.

        Args:
            file_path: Path to the PDF file
            supplier: Supplier name (must have a route configured)
            custom_params: Optional key-value pairs to include in parsed results

        Returns:
            UploadResult with success status and details
        """
        route = get_route(supplier)

        if not route:
            return UploadResult(
                success=False,
                supplier=supplier,
                mailbox_id='',
                filename=file_path.name,
                message=f"No API route configured for supplier: {supplier}"
            )

        if not route.enabled:
            return UploadResult(
                success=False,
                supplier=supplier,
                mailbox_id=route.mailbox_id or '',
                filename=file_path.name,
                message=f"API route for {supplier} is disabled (provider: {route.provider})"
            )

        if route.provider != 'parseur':
            return UploadResult(
                success=False,
                supplier=supplier,
                mailbox_id='',
                filename=file_path.name,
                message=f"Provider '{route.provider}' not implemented yet"
            )

        if not route.mailbox_id:
            return UploadResult(
                success=False,
                supplier=supplier,
                mailbox_id='',
                filename=file_path.name,
                message=f"No mailbox ID configured for supplier: {supplier}"
            )

        # Add supplier info to custom params
        params = custom_params or {}
        params['supplier'] = supplier

        result = self.upload_document(file_path, route.mailbox_id, params)
        result.supplier = supplier

        return result


# Convenience function
def upload_invoice(file_path: Path, supplier: str) -> UploadResult:
    """
    Upload an invoice to the appropriate OCR API based on supplier.

    Args:
        file_path: Path to the invoice PDF
        supplier: Classified supplier name

    Returns:
        UploadResult with success status and details
    """
    client = ParseurClient()
    return client.upload_for_supplier(file_path, supplier)
