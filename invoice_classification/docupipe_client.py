"""
Docupipe API Client

Uploads receipt documents to Docupipe for OCR processing.
API Documentation: https://docs.docupipe.ai/reference
"""

import base64
import logging
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

import requests

from api_config import DOCUPIPE_API_KEY, DOCUPIPE_BASE_URL, get_route

logger = logging.getLogger(__name__)


@dataclass
class UploadResult:
    """Result of uploading a document to Docupipe."""
    success: bool
    supplier: str
    filename: str
    message: str
    document_id: Optional[str] = None
    job_id: Optional[str] = None
    response_data: Optional[dict] = None


class DocupipeClient:
    """Client for interacting with Docupipe API."""

    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize Docupipe client.

        Args:
            api_key: Docupipe API key. If not provided, uses config file or env var.
        """
        self.api_key = api_key or DOCUPIPE_API_KEY
        self.base_url = DOCUPIPE_BASE_URL

        if not self.api_key:
            logger.warning("Docupipe API key not configured. Set in config.json or DOCUPIPE_API_KEY env var.")

    def is_configured(self) -> bool:
        """Check if API key is configured."""
        return bool(self.api_key)

    def upload_document(
        self,
        file_path: Path,
        workflow_id: Optional[str] = None,
    ) -> UploadResult:
        """
        Upload a document to Docupipe for processing.

        Args:
            file_path: Path to the PDF or image file
            workflow_id: Optional Docupipe workflow ID

        Returns:
            UploadResult with success status and details
        """
        if not self.api_key:
            return UploadResult(
                success=False,
                supplier='',
                filename=file_path.name,
                message="Docupipe API key not configured"
            )

        url = f"{self.base_url}/document"

        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "X-API-Key": self.api_key
        }

        try:
            # Read and base64 encode the file
            with open(file_path, 'rb') as f:
                file_contents = base64.b64encode(f.read()).decode('utf-8')

            # Build payload
            payload = {
                "document": {
                    "file": {
                        "contents": file_contents,
                        "filename": file_path.name
                    }
                }
            }

            # Add workflow if specified
            if workflow_id:
                payload["workflowId"] = workflow_id

            response = requests.post(
                url,
                json=payload,
                headers=headers,
                timeout=120  # Longer timeout for base64 uploads
            )

            if response.status_code in (200, 201, 202):
                data = response.json() if response.text else {}
                logger.info(f"Uploaded {file_path.name} to Docupipe (doc_id: {data.get('documentId')})")
                return UploadResult(
                    success=True,
                    supplier='',
                    filename=file_path.name,
                    message="Document uploaded successfully",
                    document_id=data.get('documentId'),
                    job_id=data.get('jobId'),
                    response_data=data
                )
            else:
                error_msg = f"HTTP {response.status_code}: {response.text}"
                logger.error(f"Failed to upload {file_path.name}: {error_msg}")
                return UploadResult(
                    success=False,
                    supplier='',
                    filename=file_path.name,
                    message=error_msg
                )

        except requests.exceptions.Timeout:
            return UploadResult(
                success=False,
                supplier='',
                filename=file_path.name,
                message="Request timed out"
            )
        except requests.exceptions.RequestException as e:
            return UploadResult(
                success=False,
                supplier='',
                filename=file_path.name,
                message=f"Request failed: {str(e)}"
            )
        except Exception as e:
            return UploadResult(
                success=False,
                supplier='',
                filename=file_path.name,
                message=f"Unexpected error: {str(e)}"
            )

    def upload_for_supplier(
        self,
        file_path: Path,
        supplier: str,
    ) -> UploadResult:
        """
        Upload a document using the configured route for a supplier.

        Args:
            file_path: Path to the PDF file
            supplier: Supplier name (must have a docupipe route configured)

        Returns:
            UploadResult with success status and details
        """
        route = get_route(supplier)

        if not route:
            return UploadResult(
                success=False,
                supplier=supplier,
                filename=file_path.name,
                message=f"No API route configured for supplier: {supplier}"
            )

        if not route.enabled:
            return UploadResult(
                success=False,
                supplier=supplier,
                filename=file_path.name,
                message=f"API route for {supplier} is disabled"
            )

        if route.provider != 'docupipe':
            return UploadResult(
                success=False,
                supplier=supplier,
                filename=file_path.name,
                message=f"Supplier {supplier} uses provider '{route.provider}', not docupipe"
            )

        result = self.upload_document(file_path, workflow_id=route.workflow_id)
        result.supplier = supplier

        return result

    def get_job_status(self, job_id: str) -> Optional[dict]:
        """
        Check the processing status of a job.

        Args:
            job_id: Job ID returned from upload

        Returns:
            Job status dict or None if failed
        """
        if not self.api_key:
            return None

        url = f"{self.base_url}/job/{job_id}"
        headers = {
            "accept": "application/json",
            "X-API-Key": self.api_key
        }

        try:
            response = requests.get(url, headers=headers, timeout=30)
            if response.status_code == 200:
                return response.json()
        except Exception as e:
            logger.error(f"Failed to get job status: {e}")

        return None


# Convenience function
def upload_receipt(file_path: Path, supplier: str) -> UploadResult:
    """
    Upload a receipt to Docupipe for OCR processing.

    Args:
        file_path: Path to the receipt PDF
        supplier: Classified supplier name

    Returns:
        UploadResult with success status and details
    """
    client = DocupipeClient()
    return client.upload_for_supplier(file_path, supplier)
