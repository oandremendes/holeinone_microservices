"""
API Configuration for Invoice OCR Routing

Defines which API provider and mailbox to use for each supplier.
"""

import os
import json
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

# Load config from file
CONFIG_FILE = Path(__file__).parent / 'config.json'

def _load_config() -> dict:
    """Load configuration from config.json file."""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}

_config = _load_config()

# API Keys - load from config file, fallback to environment variable
PARSEUR_API_KEY = _config.get('parseur', {}).get('api_key', '') or os.environ.get('PARSEUR_API_KEY', '')
DOCUPIPE_API_KEY = _config.get('docupipe', {}).get('api_key', '') or os.environ.get('DOCUPIPE_API_KEY', '')

# API endpoints
PARSEUR_BASE_URL = "https://api.parseur.com"
DOCUPIPE_BASE_URL = "https://app.docupipe.ai"


@dataclass
class APIRoute:
    """Configuration for routing a supplier's invoices to an OCR API."""
    provider: str  # 'parseur', 'docupipe', etc.
    mailbox_id: Optional[str] = None  # For Parseur
    workflow_id: Optional[str] = None  # For Docupipe
    enabled: bool = True


# Supplier to API routing table
# Original invoice suppliers -> Parseur
SUPPLIER_ROUTES = {
    'soares': APIRoute(provider='parseur', mailbox_id='111948'),
    'justdrinks': APIRoute(provider='parseur', mailbox_id='112442'),
    'novadis': APIRoute(provider='parseur', mailbox_id='111943'),
    'garcias': APIRoute(provider='parseur', mailbox_id='112445'),
    'teofilo': APIRoute(provider='parseur', mailbox_id='112431'),
    'jmv': APIRoute(provider='parseur', mailbox_id='112446'),
    'absolutlyvintage': APIRoute(provider='parseur', mailbox_id='112448'),
    # All receipts and other documents -> Docupipe
    'magniberia': APIRoute(provider='docupipe'),
    'teofilo_gd': APIRoute(provider='docupipe'),  # Teófilo return guides
    'teofilo_nc': APIRoute(provider='docupipe'),  # Teófilo credit notes
    # Supermarkets
    'intermarche': APIRoute(provider='docupipe'),
    'continente': APIRoute(provider='docupipe', workflow_id='4Vy92EQH'),
    'overseas': APIRoute(provider='docupipe'),
    'makro': APIRoute(provider='docupipe', workflow_id='jtVquUzt'),
    'pingodoce': APIRoute(provider='docupipe', workflow_id='PRtYtwC7'),
    'lidl': APIRoute(provider='docupipe', workflow_id='YxiR0kCy'),
    'inframoura': APIRoute(provider='docupipe', workflow_id='y2c7v2bS'),
    # Gas stations
    'moeve': APIRoute(provider='docupipe'),
    'galp': APIRoute(provider='docupipe'),
    'cepsa': APIRoute(provider='docupipe'),
    'makro_gas': APIRoute(provider='docupipe'),
    'bp': APIRoute(provider='docupipe'),
    # Retail stores
    'action': APIRoute(provider='docupipe'),
    'worten': APIRoute(provider='docupipe'),
    'wells': APIRoute(provider='docupipe'),
    'ikea': APIRoute(provider='docupipe'),
    'leroy': APIRoute(provider='docupipe'),
    'staples': APIRoute(provider='docupipe'),
    'note': APIRoute(provider='docupipe'),
    'partyland': APIRoute(provider='docupipe'),
    'kiabi': APIRoute(provider='docupipe'),
    # Hardware/supplies
    'constamarina': APIRoute(provider='docupipe'),
    'constantino': APIRoute(provider='docupipe'),
    'papelnet': APIRoute(provider='docupipe'),
    'gildadasilva': APIRoute(provider='docupipe'),
    'robalo': APIRoute(provider='docupipe', workflow_id='kjigWqyQ'),
    # Fast food
    'burgerking': APIRoute(provider='docupipe'),
    'mcdonalds': APIRoute(provider='docupipe'),
    'pizzahut': APIRoute(provider='docupipe'),
    'dominos': APIRoute(provider='docupipe'),
    # Restaurants
    'mourapao': APIRoute(provider='docupipe'),
    'matchpoint': APIRoute(provider='docupipe'),
    'osakasushi': APIRoute(provider='docupipe'),
    'tribulum': APIRoute(provider='docupipe'),
    'zorba': APIRoute(provider='docupipe'),
    'sinfonia': APIRoute(provider='docupipe'),
    'eurolatina': APIRoute(provider='docupipe'),
    'apaisagem': APIRoute(provider='docupipe'),
    'anticapizzeria': APIRoute(provider='docupipe'),
    'italianrepublic': APIRoute(provider='docupipe'),
    'reichurrasco': APIRoute(provider='docupipe'),
    'solarfarelo': APIRoute(provider='docupipe'),
    'botanico': APIRoute(provider='docupipe'),
    'adegamonte': APIRoute(provider='docupipe'),
    'afamilia': APIRoute(provider='docupipe'),
    'artisan': APIRoute(provider='docupipe'),
    'bagga': APIRoute(provider='docupipe'),
    'maxidrive': APIRoute(provider='docupipe'),
    'padoca': APIRoute(provider='docupipe'),
    # Shopping/other
    'seminoshopping': APIRoute(provider='docupipe'),
    'orientalshopping': APIRoute(provider='docupipe'),
    'shoppingloule': APIRoute(provider='docupipe'),
    'a4tabacaria': APIRoute(provider='docupipe'),
    # Tolls/parking
    'brisa': APIRoute(provider='docupipe'),
    'brisatoll': APIRoute(provider='docupipe'),
    'alparques': APIRoute(provider='docupipe'),
}


def get_route(supplier: str) -> Optional[APIRoute]:
    """Get the API route configuration for a supplier."""
    return SUPPLIER_ROUTES.get(supplier.lower())


def is_parseur_configured() -> bool:
    """Check if Parseur API key is configured."""
    return bool(PARSEUR_API_KEY)


def is_docupipe_configured() -> bool:
    """Check if Docupipe API key is configured."""
    return bool(DOCUPIPE_API_KEY)
