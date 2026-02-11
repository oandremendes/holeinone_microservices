#!/usr/bin/env python3
"""
Invoice Classification System
Classifies scanned invoices by supplier to route to appropriate OCR APIs.
"""

import os
import re
import shutil
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime
import logging

# Image processing
import pdf2image
import cv2
import numpy as np
from PIL import Image

# OCR
import pytesseract

# For template matching / feature extraction
from skimage.metrics import structural_similarity as ssim

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class SupplierProfile:
    """Profile for a known supplier."""
    name: str
    display_name: str
    nif: str  # Portuguese tax ID
    keywords: list[str]  # Unique text patterns to look for
    logo_template: Optional[np.ndarray] = None  # Reference logo image
    header_region: tuple[int, int, int, int] = (0, 0, 800, 300)  # x, y, w, h for logo area


@dataclass
class ClassificationResult:
    """Result of invoice classification."""
    supplier: str
    confidence: float
    method: str  # 'template', 'ocr', 'hybrid'
    details: dict
    invoice_date: Optional[str] = None  # YYYYMMDD format
    ocr_text: str = ""  # Store OCR text for date extraction


class InvoiceClassifier:
    """
    Classifies invoices by supplier using multiple methods:
    1. Template matching (logo/header comparison)
    2. OCR text analysis (NIF, company names, keywords)
    3. Layout analysis (document structure)
    """

    # Known suppliers with their identifying characteristics
    SUPPLIERS = {
        'teofilo': SupplierProfile(
            name='teofilo',
            display_name='Estabelecimentos Teófilo Fontainhas Neto',
            nif='500099871',
            keywords=['teofilo', 'fontainhas', 'messines', '8375-127', 'teofilo.pt'],
            header_region=(0, 0, 400, 150),  # Logo is top-left
        ),
        'soares': SupplierProfile(
            name='soares',
            display_name='Garrafeira Soares',
            nif='501496912',
            keywords=['soares', 'garrafeira', 'wine.*spirits', '40.*anos', 'garrafeirasoares.pt'],
            header_region=(0, 0, 300, 150),
        ),
        'garcias': SupplierProfile(
            name='garcias',
            display_name='Garcias S.A.',
            nif='501141243',
            keywords=['garcias', 'wines.*spirits', 'algoz', '8365-085', 'garcias.pt'],
            header_region=(0, 0, 300, 150),
        ),
        'jmv': SupplierProfile(
            name='jmv',
            display_name='Jose Maria Vieira S.A.',
            nif='503858471',
            keywords=['jose.*maria.*vieira', 'rio.*tinto', '4439-909'],
            header_region=(0, 0, 400, 100),
        ),
        'justdrinks': SupplierProfile(
            name='justdrinks',
            display_name='Justdrinks Lda',
            nif='508976464',
            keywords=['justdrinks', 'quatro.*estradas', '8100-287', 'justdrinks.pt'],
            header_region=(0, 0, 300, 200),
        ),
        'novadis': SupplierProfile(
            name='novadis',
            display_name='Novadis Unipessoal Lda',
            nif='504350900',
            keywords=['novadis', 'alfarrobeira', 'vila.*franca.*xira', 'centralcervejas'],
            header_region=(0, 0, 400, 100),
        ),
        'absolutlyvintage': SupplierProfile(
            name='absolutlyvintage',
            display_name='Absolutly Vintage Unipessoal Lda',
            nif='516001906',
            keywords=['absolutly.*vintage', 'alcantarilha', '8365-028', 'rogel'],
            header_region=(0, 0, 300, 200),
        ),
        'magniberia': SupplierProfile(
            name='magniberia',
            display_name='Magnibéria Ltd. (VENKO Solutions)',
            nif='515102334',
            keywords=['magniberia', 'venko', 'tavira', '8800-318', 'magniberia.pt'],
            header_region=(0, 0, 300, 150),
        ),
        # Teófilo return guides (Guia de Devolução) - same company, different document type
        'teofilo_gd': SupplierProfile(
            name='teofilo_gd',
            display_name='Teófilo - Guia de Devolução',
            nif='500099871',  # Same NIF as teofilo
            keywords=['guia.*devolu', 'produto.*reclamado', 'produto.*devolvido'],
            header_region=(0, 0, 400, 150),
        ),
        'teofilo_nc': SupplierProfile(
            name='teofilo_nc',
            display_name='Teófilo - Nota de Crédito',
            nif='500099871',  # Same NIF as teofilo
            keywords=['nota.*cr[ée]dito', 'c\\s*caau'],
            header_region=(0, 0, 400, 150),
        ),
        # === RECEIPTS (Docupipe) ===
        'intermarche': SupplierProfile(
            name='intermarche',
            display_name='Intermarché / Sodiquarteira',
            nif='508162378',
            keywords=['intermarche', 'sodiquarteira', 'vilamoura', 'supermercados'],
            header_region=(0, 0, 400, 150),
        ),
        'continente': SupplierProfile(
            name='continente',
            display_name='Continente Hipermercados',
            nif='502011475',
            keywords=['continente', 'hipermercados', 'sonae'],
            header_region=(0, 0, 400, 150),
        ),
        'moeve': SupplierProfile(
            name='moeve',
            display_name='Moeve (Galp Tolls)',
            nif='500223840',
            keywords=['moeve', 'operacoes.*retalho'],
            header_region=(0, 0, 400, 150),
        ),
        'galp': SupplierProfile(
            name='galp',
            display_name='Galp Energia',
            nif='500697370',
            keywords=['galp', 'petróleos', 'energia'],
            header_region=(0, 0, 400, 150),
        ),
        'cepsa': SupplierProfile(
            name='cepsa',
            display_name='Cepsa / Vilacomb',
            nif='510748430',
            keywords=['cepsa', 'vilacomb', 'combustiveis'],
            header_region=(0, 0, 400, 150),
        ),
        'makro_gas': SupplierProfile(
            name='makro_gas',
            display_name='Makro Gas (Carbusol)',
            nif='505337053',
            keywords=['carbusol', 'makro.*fetha'],
            header_region=(0, 0, 400, 150),
        ),
        'action': SupplierProfile(
            name='action',
            display_name='Action Store',
            nif='517247739',
            keywords=['action', 'storeops.*portugal'],
            header_region=(0, 0, 400, 150),
        ),
        'burgerking': SupplierProfile(
            name='burgerking',
            display_name='Burger King',
            nif='504661264',
            keywords=['burger.*king', 'whopper'],
            header_region=(0, 0, 400, 150),
        ),
        'mourapao': SupplierProfile(
            name='mourapao',
            display_name='Mourapão / Sailor Corner',
            nif='518468020',
            keywords=['mourapao', 'sailor.*corner', 'grupo.*mourapao'],
            header_region=(0, 0, 400, 150),
        ),
        'worten': SupplierProfile(
            name='worten',
            display_name='Worten',
            nif='503630330',
            keywords=['worten', 'equipamentos.*lar'],
            header_region=(0, 0, 400, 150),
        ),
        'wells': SupplierProfile(
            name='wells',
            display_name='Wells Pharmacy',
            nif='508037514',
            keywords=['wells', 'pharmacontinente'],
            header_region=(0, 0, 400, 150),
        ),
        'matchpoint': SupplierProfile(
            name='matchpoint',
            display_name='Pizzaria MatchPoint',
            nif='516585800',
            keywords=['matchpoint', 'premier.*sports'],
            header_region=(0, 0, 400, 150),
        ),
        'overseas': SupplierProfile(
            name='overseas',
            display_name='Overseas Supermercados',
            nif='509943888',
            keywords=['overseas', 'tavagueira'],
            header_region=(0, 0, 400, 150),
        ),
        'makro': SupplierProfile(
            name='makro',
            display_name='Makro Cash & Carry',
            nif='502030712',
            keywords=['makro', 'cash.*carry'],
            header_region=(0, 0, 400, 150),
        ),
        'pingodoce': SupplierProfile(
            name='pingodoce',
            display_name='Pingo Doce',
            nif='500829093',
            keywords=['pingo.*doce', 'distribuição.*alimentar'],
            header_region=(0, 0, 400, 150),
        ),
        'lidl': SupplierProfile(
            name='lidl',
            display_name='Lidl',
            nif='503340855',
            keywords=['lidl', 'www\\.lidl\\.pt'],
            header_region=(0, 0, 400, 150),
        ),
        'inframoura': SupplierProfile(
            name='inframoura',
            display_name='Inframoura',
            nif='504915266',
            keywords=['inframoura', 'águas.*algarve', 'saneamento'],
            header_region=(0, 0, 400, 150),
        ),
        'constamarina': SupplierProfile(
            name='constamarina',
            display_name='Constamarina',
            nif='504147480',
            keywords=['constamarina', 'drogaria.*nauticos'],
            header_region=(0, 0, 400, 150),
        ),
        'constantino': SupplierProfile(
            name='constantino',
            display_name='Drogaria Constantino',
            nif='500072205',
            keywords=['constantino', 'rocha.*amador'],
            header_region=(0, 0, 400, 150),
        ),
        'papelnet': SupplierProfile(
            name='papelnet',
            display_name='Papelnet',
            nif='504064282',
            keywords=['papelnet', 'papelaria'],
            header_region=(0, 0, 400, 150),
        ),
        'osakasushi': SupplierProfile(
            name='osakasushi',
            display_name='Osaka Sushi',
            nif='518794482',
            keywords=['osaka', 'meridiano.*suculento'],
            header_region=(0, 0, 400, 150),
        ),
        'tribulum': SupplierProfile(
            name='tribulum',
            display_name='Tribulum Restaurant',
            nif='515892327',
            keywords=['tribulum', 'all.*over.*mountain'],
            header_region=(0, 0, 400, 150),
        ),
        'zorba': SupplierProfile(
            name='zorba',
            display_name='Zorba The Greek',
            nif='518564410',
            keywords=['zorba', 'meadows.*heaven'],
            header_region=(0, 0, 400, 150),
        ),
        'sinfonia': SupplierProfile(
            name='sinfonia',
            display_name='Sinfonia d\'Iguarias',
            nif='518636766',
            keywords=['sinfonia', 'iguarias'],
            header_region=(0, 0, 400, 150),
        ),
        'eurolatina': SupplierProfile(
            name='eurolatina',
            display_name='Eurolatina Bakery',
            nif='502781106',
            keywords=['eurolatina', 'diniz.*nota.*loureiro'],
            header_region=(0, 0, 400, 150),
        ),
        'brisa': SupplierProfile(
            name='brisa',
            display_name='Brisa Service Areas',
            nif='514166096',
            keywords=['brisa', 'areas.*servico'],
            header_region=(0, 0, 400, 150),
        ),
        'ikea': SupplierProfile(
            name='ikea',
            display_name='IKEA Portugal',
            nif='505416654',
            keywords=['ikea', 'moveis.*decoracao'],
            header_region=(0, 0, 400, 150),
        ),
        'leroy': SupplierProfile(
            name='leroy',
            display_name='Leroy Merlin',
            nif='506848556',
            keywords=['leroy.*merlin', 'bricolage'],
            header_region=(0, 0, 400, 150),
        ),
        'staples': SupplierProfile(
            name='staples',
            display_name='Staples Portugal',
            nif='503789372',
            keywords=['staples', 'equipamento.*escritorio'],
            header_region=(0, 0, 400, 150),
        ),
        'note': SupplierProfile(
            name='note',
            display_name='Note Papelaria',
            nif='517309505',
            keywords=['note', 'mundo.*note', 'livraria.*papelaria'],
            header_region=(0, 0, 400, 150),
        ),
        'partyland': SupplierProfile(
            name='partyland',
            display_name='Partyland',
            nif='509199429',
            keywords=['partyland', 'solucoes.*alegres'],
            header_region=(0, 0, 400, 150),
        ),
        'alparques': SupplierProfile(
            name='alparques',
            display_name='Alparques Estacionamento',
            nif='514916494',
            keywords=['alparques', 'parque.*estac'],
            header_region=(0, 0, 400, 150),
        ),
        'pizzahut': SupplierProfile(
            name='pizzahut',
            display_name='Pizza Hut',
            nif='502604735',
            keywords=['pizza.*hut', 'iberusa'],
            header_region=(0, 0, 400, 150),
        ),
        'mcdonalds': SupplierProfile(
            name='mcdonalds',
            display_name='McDonald\'s',
            nif='504416014',
            keywords=['mcdonald', 'magic.*empreend'],
            header_region=(0, 0, 400, 150),
        ),
        'dominos': SupplierProfile(
            name='dominos',
            display_name='Domino\'s Pizza',
            nif='513146051',
            keywords=['domino', 'daufood'],
            header_region=(0, 0, 400, 150),
        ),
        'apaisagem': SupplierProfile(
            name='apaisagem',
            display_name='Restaurante A Paisagem',
            nif='510577199',
            keywords=['paisagem', 'wine.*glass', 'churrasqueira'],
            header_region=(0, 0, 400, 150),
        ),
        'a4tabacaria': SupplierProfile(
            name='a4tabacaria',
            display_name='A4 Tabacarias',
            nif='502749423',
            keywords=['a4.*tabacaria', 'tabacarias.*lda'],
            header_region=(0, 0, 400, 150),
        ),
        'anticapizzeria': SupplierProfile(
            name='anticapizzeria',
            display_name='Antica Pizzeria',
            nif='517973634',
            keywords=['antica.*pizzeria', 'centralholding'],
            header_region=(0, 0, 400, 150),
        ),
        'italianrepublic': SupplierProfile(
            name='italianrepublic',
            display_name='Italian Republic Restaurant',
            nif='503254435',
            keywords=['italian.*republic', 'estrela.*guia'],
            header_region=(0, 0, 400, 150),
        ),
        'reichurrasco': SupplierProfile(
            name='reichurrasco',
            display_name='Rei do Churrasco',
            nif='515553565',
            keywords=['rei.*churrasco', 'titulo.*amistoso'],
            header_region=(0, 0, 400, 150),
        ),
        'solarfarelo': SupplierProfile(
            name='solarfarelo',
            display_name='Solar do Farelo',
            nif='504055224',
            keywords=['solar.*farelo', 'dois.*dias.*hotelaria'],
            header_region=(0, 0, 400, 150),
        ),
        'botanico': SupplierProfile(
            name='botanico',
            display_name='Botanico Restaurant',
            nif='516823961',
            keywords=['botanico', 'quinta.*lago'],
            header_region=(0, 0, 400, 150),
        ),
        'adegamonte': SupplierProfile(
            name='adegamonte',
            display_name='Adega do Monte Velho',
            nif='',  # No reliable NIF - use keyword match only
            keywords=['adega.*monte', 'natureza.*prato'],
            header_region=(0, 0, 400, 150),
        ),
        'afamilia': SupplierProfile(
            name='afamilia',
            display_name='A Família Pizzaria',
            nif='508179047',
            keywords=['familia', 'pizzaria.*artesanal.*brasileira'],
            header_region=(0, 0, 400, 150),
        ),
        'artisan': SupplierProfile(
            name='artisan',
            display_name='Artisan Restaurant',
            nif='515652946',
            keywords=['artisan', 'luxury.*ingredient', 'old.*village'],
            header_region=(0, 0, 400, 150),
        ),
        'bagga': SupplierProfile(
            name='bagga',
            display_name='Bagga',
            nif='508879990',
            keywords=['bagga', 'pronto.*gostar', 'bb.*food'],
            header_region=(0, 0, 400, 150),
        ),
        'maxidrive': SupplierProfile(
            name='maxidrive',
            display_name='Maxidrive Pizzaria',
            nif='515865672',
            keywords=['maxidrive', 'galaxialaranjada'],
            header_region=(0, 0, 400, 150),
        ),
        'padoca': SupplierProfile(
            name='padoca',
            display_name='Padoca',
            nif='000000000',  # Needs keyword match
            keywords=['padoca', 'costa.*leitas', 'las.*arcos'],
            header_region=(0, 0, 400, 150),
        ),
        'gildadasilva': SupplierProfile(
            name='gildadasilva',
            display_name='Gilda da Silva',
            nif='219329976',
            keywords=['gilda.*silva', 'multiservicos.*solbelo'],
            header_region=(0, 0, 400, 150),
        ),
        'robalo': SupplierProfile(
            name='robalo',
            display_name='Robalo S.A.',
            nif='500654573',
            keywords=['robalo', 'utilidades.*dom.sticas', 'hoteleiras', 'robalo-sa\\.com'],
            header_region=(0, 0, 400, 150),
        ),
        'seminoshopping': SupplierProfile(
            name='seminoshopping',
            display_name='Semino Shopping',
            nif='247388858',
            keywords=['semino.*shopping', 'chen.*shuang'],
            header_region=(0, 0, 400, 150),
        ),
        'orientalshopping': SupplierProfile(
            name='orientalshopping',
            display_name='Oriental Shopping',
            nif='514703873',
            keywords=['oriental.*shopping', 'orientalefeito'],
            header_region=(0, 0, 400, 150),
        ),
        'shoppingloule': SupplierProfile(
            name='shoppingloule',
            display_name='Shopping Loulé',
            nif='509713955',
            keywords=['shopping.*loule', 'leia.*creia'],
            header_region=(0, 0, 400, 150),
        ),
        # BP has multiple franchises with different NIFs - use keyword matching
        'bp': SupplierProfile(
            name='bp',
            display_name='BP Gas Station',
            nif='507161058',  # One of several BP NIFs
            keywords=['\\bbp\\b', 'bp.*quarteira', 'bp.*vilamoura'],
            header_region=(0, 0, 400, 150),
        ),
        'kiabi': SupplierProfile(
            name='kiabi',
            display_name='Kiabi',
            nif='000000000',  # Needs keyword match
            keywords=['kiabi', 'fidelidade'],
            header_region=(0, 0, 400, 150),
        ),
        'brisatoll': SupplierProfile(
            name='brisatoll',
            display_name='Brisa Tolls (BCR)',
            nif='502790624',
            keywords=['brisa.*concessao', 'bcr', 'portagem'],
            header_region=(0, 0, 400, 150),
        ),
    }

    def __init__(self, templates_dir: Optional[Path] = None):
        """
        Initialize classifier.

        Args:
            templates_dir: Directory containing reference template images for each supplier
        """
        self.templates_dir = templates_dir
        self.templates: dict[str, np.ndarray] = {}

        if templates_dir and templates_dir.exists():
            self._load_templates()

    def _load_templates(self):
        """Load reference template images for each supplier."""
        for supplier_name in self.SUPPLIERS:
            template_path = self.templates_dir / f"{supplier_name}_template.png"
            if template_path.exists():
                template = cv2.imread(str(template_path), cv2.IMREAD_GRAYSCALE)
                if template is not None:
                    self.templates[supplier_name] = template
                    logger.info(f"Loaded template for {supplier_name}")

    def pdf_to_image(self, pdf_path: Path, dpi: int = 200) -> np.ndarray:
        """
        Convert first page of PDF to image.

        Args:
            pdf_path: Path to PDF file
            dpi: Resolution for conversion

        Returns:
            Image as numpy array (BGR format for OpenCV)
        """
        images = pdf2image.convert_from_path(pdf_path, dpi=dpi, first_page=1, last_page=1)
        if not images:
            raise ValueError(f"Could not convert PDF: {pdf_path}")

        # Convert PIL Image to OpenCV format
        pil_image = images[0]
        cv_image = cv2.cvtColor(np.array(pil_image), cv2.COLOR_RGB2BGR)
        return cv_image

    def extract_text_ocr(self, image: np.ndarray) -> str:
        """
        Extract text from image using OCR.

        Args:
            image: Image as numpy array

        Returns:
            Extracted text
        """
        # Convert to grayscale if needed
        if len(image.shape) == 3:
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        else:
            gray = image

        # Apply some preprocessing for better OCR
        # Slight blur to reduce noise
        gray = cv2.GaussianBlur(gray, (1, 1), 0)

        # Use Portuguese language for better accuracy
        try:
            text = pytesseract.image_to_string(gray, lang='por')
        except pytesseract.TesseractError:
            # Fallback to default if Portuguese not available
            text = pytesseract.image_to_string(gray)

        return text.lower()

    def extract_header_text(self, image: np.ndarray) -> str:
        """
        Extract text from the header region of the invoice.

        This is useful for invoices where the date is in a table header
        that full-page OCR doesn't capture well.

        Args:
            image: Image as numpy array

        Returns:
            Extracted text from header region
        """
        h, w = image.shape[:2]

        # Extract top 40% of page, right half (where dates typically are)
        header = image[0:int(h*0.4), int(w*0.5):]

        # Convert to grayscale if needed
        if len(header.shape) == 3:
            gray = cv2.cvtColor(header, cv2.COLOR_BGR2GRAY)
        else:
            gray = header

        # Use Portuguese language
        try:
            text = pytesseract.image_to_string(gray, lang='por', config='--psm 6')
        except pytesseract.TesseractError:
            text = pytesseract.image_to_string(gray, config='--psm 6')

        return text.lower()

    def classify_by_nif(self, text: str) -> Optional[ClassificationResult]:
        """
        Classify by finding supplier NIF (tax ID) in text.

        Args:
            text: OCR extracted text

        Returns:
            Classification result if NIF found, None otherwise
        """
        # Special handling for suppliers with same NIF but different document types
        # Check for specific document type keywords first
        SPECIAL_CASES = {
            '500099871': [  # Teófilo NIF
                ('teofilo_gd', ['guia.*devolu', 'produto.*reclamado', 'produto.*devolvido']),
                ('teofilo_nc', ['nota.*cr[ée]dito', 'c\\s*caau']),
                ('teofilo', []),  # Default fallback for this NIF
            ],
        }

        for name, profile in self.SUPPLIERS.items():
            # Skip special case suppliers - they'll be handled separately
            if name in ['teofilo_gd', 'teofilo_nc']:
                continue

            # Skip suppliers without a valid NIF (need keyword matching instead)
            if not profile.nif or len(profile.nif) < 9:
                continue

            # Look for NIF with various formats: 501496912, 501 496 912, PT501496912
            nif_patterns = [
                profile.nif,  # Plain
                ' '.join([profile.nif[i:i+3] for i in range(0, len(profile.nif), 3)]),  # Spaced
                f"pt{profile.nif}",  # With PT prefix
                f"pt {profile.nif}",
            ]

            for pattern in nif_patterns:
                if pattern.lower() in text:
                    # Check if this NIF has special cases
                    if profile.nif in SPECIAL_CASES:
                        for special_name, keywords in SPECIAL_CASES[profile.nif]:
                            if keywords:  # Has specific keywords to match
                                if any(re.search(kw, text, re.IGNORECASE) for kw in keywords):
                                    return ClassificationResult(
                                        supplier=special_name,
                                        confidence=0.95,
                                        method='nif',
                                        details={'matched_nif': profile.nif, 'special_type': special_name}
                                    )
                            else:  # Default case (no keywords = fallback)
                                return ClassificationResult(
                                    supplier=special_name,
                                    confidence=0.95,
                                    method='nif',
                                    details={'matched_nif': profile.nif}
                                )
                    else:
                        return ClassificationResult(
                            supplier=name,
                            confidence=0.95,  # NIF match is very reliable
                            method='nif',
                            details={'matched_nif': profile.nif}
                        )

        return None

    def classify_by_keywords(self, text: str) -> Optional[ClassificationResult]:
        """
        Classify by finding supplier-specific keywords in text.

        Args:
            text: OCR extracted text

        Returns:
            Classification result with confidence based on keyword matches
        """
        best_match = None
        best_score = 0
        best_matches = []

        for name, profile in self.SUPPLIERS.items():
            matches = []
            for keyword in profile.keywords:
                if re.search(keyword, text, re.IGNORECASE):
                    matches.append(keyword)

            # Score based on number of matches
            if matches:
                score = len(matches) / len(profile.keywords)
                if score > best_score:
                    best_score = score
                    best_match = name
                    best_matches = matches

        if best_match and best_score >= 0.3:  # At least 30% of keywords matched
            return ClassificationResult(
                supplier=best_match,
                confidence=min(0.9, 0.5 + best_score * 0.4),  # Scale confidence
                method='keywords',
                details={'matched_keywords': best_matches, 'score': best_score}
            )

        return None

    def extract_invoice_date(self, text: str) -> Optional[str]:
        """
        Extract invoice date from OCR text.

        Looks for common date patterns in Portuguese invoices:
        - YYYY-MM-DD, DD-MM-YYYY, DD/MM/YYYY, YYYY/MM/DD
        - Data Emissão: DD/MM/YYYY, etc.

        Prioritizes issue date (emissão) over due date (vencimento).

        Args:
            text: OCR extracted text

        Returns:
            Date in YYYYMMDD format, or None if not found
        """
        # Portuguese month abbreviations
        pt_months = r'(jan|fev|mar|abr|mai|jun|jul|ago|set|out|nov|dez)'

        # Common date patterns (order matters - more specific patterns first)
        date_patterns = [
            # Portuguese month format: 30 - set - 2025 or 30 - set 2025 or 30-set-25
            (rf'(\d{{1,2}})\s*[-–]\s*{pt_months}\s*[-–]?\s*(\d{{2,4}})', 'pt_month'),
            # ISO format with dashes: 2025-02-10
            (r'(\d{4})-(\d{1,2})-(\d{1,2})', 'ymd'),
            # ISO format with slashes: 2025/02/10
            (r'(\d{4})/(\d{1,2})/(\d{1,2})', 'ymd'),
            # European/US format with slashes: 10/02/2025 or 2/16/2025
            (r'(\d{1,2})/(\d{1,2})/(\d{4})', 'ambiguous'),
            # European format with dashes: 10-02-2025
            (r'(\d{1,2})-(\d{1,2})-(\d{4})', 'dmy'),
            # European format with dots: 10.02.2025
            (r'(\d{1,2})\.(\d{1,2})\.(\d{4})', 'dmy'),
        ]

        # Priority keywords for invoice ISSUE date (highest priority first)
        priority_keywords = [
            r'data\s*(?:de\s*)?emiss[ãa]o\s*[:\s]*',
            r'data\s*(?:do\s*)?documento\s*[:\s]*',
            r'data\s*(?:da\s*)?fact?ura\s*[:\s]*',
            r'emitido\s*(?:em|a)?\s*[:\s]*',
        ]

        # Keywords to AVOID (these are due dates, not issue dates)
        avoid_keywords = [
            r'vencimento',
            r'pagamento',
            r'prazo',
        ]

        text_lower = text.lower()

        # First pass: Look for dates near priority keywords (issue date)
        for keyword in priority_keywords:
            keyword_match = re.search(keyword, text_lower)
            if keyword_match:
                # Look for date pattern after the keyword
                search_area = text_lower[keyword_match.end():keyword_match.end()+30]

                for pattern, date_format in date_patterns:
                    match = re.search(pattern, search_area)
                    if match:
                        date = self._normalize_date(match, date_format)
                        if date:
                            return date

        # Second pass: Find all dates and pick the earliest one that's not a due date
        # This is a heuristic: invoice date is usually earlier than due date
        all_dates = []

        for pattern, date_format in date_patterns:
            for match in re.finditer(pattern, text_lower):
                # Check if this date is near an "avoid" keyword
                start_pos = max(0, match.start() - 50)
                context = text_lower[start_pos:match.start()]

                is_due_date = any(re.search(kw, context) for kw in avoid_keywords)

                if not is_due_date:
                    date = self._normalize_date(match, date_format)
                    if date:
                        all_dates.append(date)

        # Return the earliest date found (usually the issue date)
        if all_dates:
            return min(all_dates)

        # Last fallback: any date at all
        for pattern, date_format in date_patterns:
            match = re.search(pattern, text_lower)
            if match:
                return self._normalize_date(match, date_format)

        return None

    def _normalize_date(self, match: re.Match, date_format: str) -> Optional[str]:
        """
        Convert matched date to YYYYMMDD format.

        Args:
            match: Regex match object with date groups
            date_format: 'ymd' for YYYY-MM-DD, 'dmy' for DD-MM-YYYY, 'ambiguous' for auto-detect,
                        'pt_month' for DD-MMM-YYYY with Portuguese month names
        """
        # Portuguese month abbreviations mapping
        pt_month_map = {
            'jan': 1, 'fev': 2, 'mar': 3, 'abr': 4, 'mai': 5, 'jun': 6,
            'jul': 7, 'ago': 8, 'set': 9, 'out': 10, 'nov': 11, 'dez': 12
        }

        groups = match.groups()

        try:
            if date_format == 'pt_month':
                # Portuguese month format: DD - MMM - YYYY (e.g., 30 - set - 2025)
                day = int(groups[0])
                month_abbr = groups[1].lower()
                year = int(groups[2])
                month = pt_month_map.get(month_abbr)
                if not month:
                    return None
                # Handle 2-digit year
                if year < 100:
                    year = 2000 + year
            elif date_format == 'ymd':
                # ISO format: YYYY-MM-DD or YYYY/MM/DD
                year, month, day = int(groups[0]), int(groups[1]), int(groups[2])
            elif date_format == 'dmy':
                # European format: DD-MM-YYYY or DD/MM/YYYY
                day, month, year = int(groups[0]), int(groups[1]), int(groups[2])
            elif date_format == 'ambiguous':
                # Could be DD/MM/YYYY (European) or MM/DD/YYYY (US)
                # Detect based on which value can be a valid month
                first, second, year = int(groups[0]), int(groups[1]), int(groups[2])

                if first > 12 and second <= 12:
                    # First > 12, must be day (European: DD/MM/YYYY)
                    day, month = first, second
                elif second > 12 and first <= 12:
                    # Second > 12, must be day (US: MM/DD/YYYY)
                    month, day = first, second
                else:
                    # Both could be month or day - assume European (DD/MM/YYYY) for Portugal
                    day, month = first, second
            else:
                return None

            # Basic validation
            if not (2020 <= year <= 2030 and 1 <= month <= 12 and 1 <= day <= 31):
                return None

            # Additional validation: check day is valid for month
            days_in_month = [0, 31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
            if day > days_in_month[month]:
                return None

            return f"{year:04d}{month:02d}{day:02d}"
        except (ValueError, IndexError):
            return None

    def classify_by_template(self, image: np.ndarray) -> Optional[ClassificationResult]:
        """
        Classify by comparing header region against templates.

        Args:
            image: Full document image

        Returns:
            Classification result if good template match found
        """
        if not self.templates:
            return None

        # Convert to grayscale
        if len(image.shape) == 3:
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        else:
            gray = image

        best_match = None
        best_score = 0

        for name, template in self.templates.items():
            profile = self.SUPPLIERS[name]
            x, y, w, h = profile.header_region

            # Extract header region from image
            header = gray[y:y+h, x:x+w]

            # Resize template to match header region if needed
            if template.shape != header.shape:
                template_resized = cv2.resize(template, (header.shape[1], header.shape[0]))
            else:
                template_resized = template

            # Calculate structural similarity
            try:
                score, _ = ssim(header, template_resized, full=True)
                if score > best_score:
                    best_score = score
                    best_match = name
            except Exception as e:
                logger.warning(f"Template matching failed for {name}: {e}")
                continue

        if best_match and best_score >= 0.4:  # Threshold for template match
            return ClassificationResult(
                supplier=best_match,
                confidence=best_score,
                method='template',
                details={'similarity_score': best_score}
            )

        return None

    def classify(self, pdf_path: Path) -> ClassificationResult:
        """
        Classify an invoice PDF.

        Uses multiple methods in order of reliability:
        1. NIF matching (most reliable)
        2. Template matching
        3. Keyword matching

        Args:
            pdf_path: Path to invoice PDF

        Returns:
            Classification result
        """
        logger.info(f"Classifying: {pdf_path.name}")

        # Convert PDF to image
        try:
            image = self.pdf_to_image(pdf_path)
        except Exception as e:
            logger.error(f"Failed to convert PDF: {e}")
            return ClassificationResult(
                supplier='unknown',
                confidence=0.0,
                method='error',
                details={'error': str(e)}
            )

        # Extract text with OCR
        text = self.extract_text_ocr(image)

        # Try classification methods in order of reliability

        # Extract invoice date
        invoice_date = self.extract_invoice_date(text)

        # If no date found, try extracting from header region
        # (some invoices have dates in table headers that full-page OCR misses)
        if not invoice_date:
            header_text = self.extract_header_text(image)
            invoice_date = self.extract_invoice_date(header_text)

        # 1. NIF matching - most reliable
        result = self.classify_by_nif(text)
        if result and result.confidence >= 0.9:
            result.invoice_date = invoice_date
            result.ocr_text = text
            logger.info(f"Classified by NIF: {result.supplier} ({result.confidence:.2f}), date: {invoice_date}")
            return result

        # 2. Template matching
        template_result = self.classify_by_template(image)

        # 3. Keyword matching
        keyword_result = self.classify_by_keywords(text)

        # Combine results - prefer template if both available and agree
        if template_result and keyword_result:
            if template_result.supplier == keyword_result.supplier:
                # Both methods agree - high confidence
                return ClassificationResult(
                    supplier=template_result.supplier,
                    confidence=max(template_result.confidence, keyword_result.confidence) + 0.1,
                    method='hybrid',
                    details={
                        'template': template_result.details,
                        'keywords': keyword_result.details
                    },
                    invoice_date=invoice_date,
                    ocr_text=text
                )
            else:
                # Disagreement - use the one with higher confidence
                if template_result.confidence > keyword_result.confidence:
                    template_result.invoice_date = invoice_date
                    template_result.ocr_text = text
                    return template_result
                else:
                    keyword_result.invoice_date = invoice_date
                    keyword_result.ocr_text = text
                    return keyword_result

        # Return whichever worked
        if template_result:
            template_result.invoice_date = invoice_date
            template_result.ocr_text = text
            return template_result
        if keyword_result:
            keyword_result.invoice_date = invoice_date
            keyword_result.ocr_text = text
            return keyword_result

        # Fallback - unknown
        return ClassificationResult(
            supplier='unknown',
            confidence=0.0,
            method='none',
            details={'text_sample': text[:500]},
            invoice_date=invoice_date,
            ocr_text=text
        )

    def classify_batch(self, folder: Path) -> dict[str, ClassificationResult]:
        """
        Classify all PDFs in a folder.

        Args:
            folder: Path to folder containing PDFs

        Returns:
            Dict mapping filename to classification result
        """
        results = {}
        pdf_files = list(folder.glob('*.pdf')) + list(folder.glob('*.PDF'))

        for pdf_path in pdf_files:
            results[pdf_path.name] = self.classify(pdf_path)

        return results


def upload_to_api(file_path: Path, supplier: str) -> dict:
    """
    Upload a classified invoice to the appropriate OCR API.

    Routes to Parseur or Docupipe based on supplier configuration.

    Args:
        file_path: Path to the invoice PDF
        supplier: Classified supplier name

    Returns:
        Dict with upload result
    """
    try:
        from api_config import get_route

        route = get_route(supplier)
        if not route:
            return {
                'success': False,
                'supplier': supplier,
                'provider': None,
                'message': f'No API route configured for supplier: {supplier}'
            }

        if route.provider == 'parseur':
            from parseur_client import upload_invoice
            result = upload_invoice(file_path, supplier)
            return {
                'success': result.success,
                'supplier': result.supplier,
                'provider': 'parseur',
                'mailbox_id': result.mailbox_id,
                'message': result.message
            }
        elif route.provider == 'docupipe':
            from docupipe_client import upload_receipt
            result = upload_receipt(file_path, supplier)
            return {
                'success': result.success,
                'supplier': result.supplier,
                'provider': 'docupipe',
                'document_id': result.document_id,
                'job_id': result.job_id,
                'message': result.message
            }
        else:
            return {
                'success': False,
                'supplier': supplier,
                'provider': route.provider,
                'message': f"Provider '{route.provider}' not implemented"
            }

    except ImportError as e:
        return {
            'success': False,
            'supplier': supplier,
            'message': f'Import error: {e}'
        }
    except Exception as e:
        return {
            'success': False,
            'supplier': supplier,
            'message': str(e)
        }


def _has_integration(supplier: str) -> bool:
    """Check if a supplier has an API integration (mailbox_id or workflow_id)."""
    try:
        from api_config import get_route
        route = get_route(supplier)
        if route and route.enabled:
            return bool(route.mailbox_id or route.workflow_id)
    except ImportError:
        pass
    return False


def process_and_move(
    classifier: InvoiceClassifier,
    source_dir: Path,
    matched_dir: Path,
    review_dir: Path,
    integrated_dir: Path,
    dry_run: bool = False,
    upload: bool = False
) -> dict:
    """
    Process all PDFs in source directory, rename and move them.

    Integrated invoices: YYYYMMDD_Supplier.pdf -> INTEGRATED/ (has API workflow/mailbox)
    Matched invoices: YYYYMMDD_Supplier.pdf -> MATCHED/ (no API workflow/mailbox)
    Unknown invoices: original name -> REVIEW/

    Args:
        classifier: InvoiceClassifier instance
        source_dir: Directory containing PDFs to process
        matched_dir: Directory for matched/classified invoices without integration
        review_dir: Directory for unknown invoices needing review
        integrated_dir: Directory for matched invoices with API workflow/mailbox
        dry_run: If True, only show what would happen without moving files
        upload: If True, upload classified invoices to OCR API

    Returns:
        Dict with processing statistics
    """
    # Ensure output directories exist
    matched_dir.mkdir(exist_ok=True)
    review_dir.mkdir(exist_ok=True)
    integrated_dir.mkdir(exist_ok=True)

    stats = {
        'total': 0,
        'integrated': 0,
        'matched': 0,
        'review': 0,
        'errors': 0,
        'uploaded': 0,
        'upload_failed': 0,
        'files': []
    }

    current_year = datetime.now().year

    pdf_files = list(source_dir.glob('*.pdf')) + list(source_dir.glob('*.PDF'))

    for pdf_path in pdf_files:
        stats['total'] += 1

        try:
            # Classify the invoice
            result = classifier.classify(pdf_path)

            if result.supplier != 'unknown':
                # Build new filename: YYYYMMDD_Supplier.pdf
                if result.invoice_date:
                    date_part = result.invoice_date
                else:
                    date_part = f"{current_year}XXXX"

                # Capitalize supplier name properly
                supplier_name = result.supplier.capitalize()

                # Choose destination: INTEGRATED (has workflow/mailbox) or MATCHED
                has_api = _has_integration(result.supplier)
                target_dir = integrated_dir if has_api else matched_dir

                # Handle duplicate filenames by adding a counter
                new_filename = f"{date_part}_{supplier_name}.pdf"
                dest_path = target_dir / new_filename

                counter = 1
                while dest_path.exists():
                    new_filename = f"{date_part}_{supplier_name}_{counter}.pdf"
                    dest_path = target_dir / new_filename
                    counter += 1

                if has_api:
                    stats['integrated'] += 1
                    action = 'INTEGRATED'
                else:
                    stats['matched'] += 1
                    action = 'MATCHED'
            else:
                # Unknown - move to review with original name
                new_filename = pdf_path.name
                dest_path = review_dir / new_filename

                # Handle duplicates
                counter = 1
                while dest_path.exists():
                    stem = pdf_path.stem
                    new_filename = f"{stem}_{counter}.pdf"
                    dest_path = review_dir / new_filename
                    counter += 1

                stats['review'] += 1
                action = 'REVIEW'

            # Log the action
            file_info = {
                'original': pdf_path.name,
                'new_name': new_filename,
                'supplier': result.supplier,
                'confidence': result.confidence,
                'date': result.invoice_date,
                'action': action,
                'dest': str(dest_path)
            }
            stats['files'].append(file_info)

            if dry_run:
                logger.info(f"[DRY RUN] {pdf_path.name} -> {action}/{new_filename}")
                if upload and action == 'INTEGRATED':
                    logger.info(f"[DRY RUN] Would upload to API for supplier: {result.supplier}")
            else:
                shutil.move(str(pdf_path), str(dest_path))
                logger.info(f"{pdf_path.name} -> {action}/{new_filename}")

                # Upload to OCR API if enabled and has integration
                if upload and action == 'INTEGRATED':
                    upload_result = upload_to_api(dest_path, result.supplier)
                    file_info['upload'] = upload_result
                    if upload_result['success']:
                        stats['uploaded'] += 1
                        provider = upload_result.get('provider', '?')
                        if provider == 'parseur':
                            logger.info(f"  -> Uploaded to Parseur mailbox {upload_result.get('mailbox_id', '?')}")
                        elif provider == 'docupipe':
                            logger.info(f"  -> Uploaded to Docupipe (doc_id: {upload_result.get('document_id', '?')})")
                        else:
                            logger.info(f"  -> Uploaded to {provider}")
                    else:
                        stats['upload_failed'] += 1
                        logger.warning(f"  -> Upload failed: {upload_result['message']}")

        except Exception as e:
            stats['errors'] += 1
            logger.error(f"Error processing {pdf_path.name}: {e}")
            stats['files'].append({
                'original': pdf_path.name,
                'error': str(e),
                'action': 'ERROR'
            })

    return stats


def generate_templates(invoices_dir: Path, output_dir: Path):
    """
    Generate reference templates from sample invoices.

    Extracts header regions from sample invoices to create templates.
    """
    output_dir.mkdir(exist_ok=True)
    classifier = InvoiceClassifier()

    # Group invoices by supplier (based on filename)
    supplier_samples: dict[str, list[Path]] = {name: [] for name in classifier.SUPPLIERS}

    for pdf_path in invoices_dir.glob('*.pdf'):
        filename_lower = pdf_path.name.lower()
        for supplier_name in classifier.SUPPLIERS:
            if supplier_name in filename_lower:
                supplier_samples[supplier_name].append(pdf_path)
                break

    # Generate template for each supplier
    for supplier_name, samples in supplier_samples.items():
        if not samples:
            logger.warning(f"No samples found for {supplier_name}")
            continue

        # Use first sample as template
        sample_path = samples[0]
        logger.info(f"Generating template for {supplier_name} from {sample_path.name}")

        try:
            image = classifier.pdf_to_image(sample_path)
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

            # Extract header region
            profile = classifier.SUPPLIERS[supplier_name]
            x, y, w, h = profile.header_region
            header = gray[y:y+h, x:x+w]

            # Save template
            template_path = output_dir / f"{supplier_name}_template.png"
            cv2.imwrite(str(template_path), header)
            logger.info(f"Saved template: {template_path}")

            # Also save full first page for reference
            full_path = output_dir / f"{supplier_name}_full.png"
            cv2.imwrite(str(full_path), image)

        except Exception as e:
            logger.error(f"Failed to generate template for {supplier_name}: {e}")


if __name__ == '__main__':
    import sys

    base_dir = Path(__file__).parent
    invoices_dir = base_dir / 'invoices_example'
    templates_dir = base_dir / 'templates'
    integrated_dir = base_dir / 'INTEGRATED'
    matched_dir = base_dir / 'MATCHED'
    review_dir = base_dir / 'REVIEW'

    def print_usage():
        print("""
Invoice Classification System

Usage:
    python classifier.py [folder]                      # Classify and show results (no file changes)
    python classifier.py process [folder]              # Process, rename and move files
    python classifier.py process [folder] --dry-run    # Show what would happen without moving
    python classifier.py process [folder] --upload     # Process and upload to OCR APIs
    python classifier.py process [folder] --output-dir [dir]  # Custom output directory
    python classifier.py generate-templates            # Generate reference templates

Options:
    --dry-run        Show what would happen without making changes
    --upload         Upload classified invoices to configured OCR APIs
                     (Parseur for invoices, Docupipe for receipts)
    --output-dir     Directory for INTEGRATED/, MATCHED/ and REVIEW/ folders (default: source folder)

Configuration:
    Edit config.json with API keys (see config.example.json)

Default folder: invoices_example/
        """)

    # Parse arguments
    args = sys.argv[1:]
    command = None
    source_folder = None
    output_dir = None
    dry_run = '--dry-run' in args
    upload = '--upload' in args

    # Parse --output-dir option
    for i, arg in enumerate(args):
        if arg == '--output-dir' and i + 1 < len(args):
            output_dir = Path(args[i + 1])
            break

    # Remove flags and their values from args
    clean_args = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg == '--output-dir':
            skip_next = True
            continue
        if not arg.startswith('--'):
            clean_args.append(arg)
    args = clean_args

    if args:
        if args[0] in ('process', 'generate-templates', '-h', '--help'):
            command = args[0]
            if len(args) > 1:
                source_folder = Path(args[1])
        else:
            # First arg is a folder
            source_folder = Path(args[0])

    # Use custom source folder if provided
    if source_folder:
        invoices_dir = source_folder

    # Set output directories (INTEGRATED/MATCHED/REVIEW)
    # Priority: --output-dir > source folder > script directory
    if output_dir:
        integrated_dir = output_dir / 'INTEGRATED'
        matched_dir = output_dir / 'MATCHED'
        review_dir = output_dir / 'REVIEW'
    elif source_folder:
        integrated_dir = source_folder / 'INTEGRATED'
        matched_dir = source_folder / 'MATCHED'
        review_dir = source_folder / 'REVIEW'

    if command:
        if command == 'generate-templates':
            generate_templates(invoices_dir, templates_dir)

        elif command == 'process':
            # Check API config if upload requested
            if upload and not dry_run:
                from api_config import is_parseur_configured, is_docupipe_configured
                missing = []
                if not is_parseur_configured():
                    missing.append("Parseur (PARSEUR_API_KEY)")
                if not is_docupipe_configured():
                    missing.append("Docupipe (DOCUPIPE_API_KEY)")
                if missing:
                    print(f"WARNING: API keys not configured: {', '.join(missing)}")
                    print("Upload will fail for suppliers using unconfigured APIs.")
                    print("Configure in config.json (see config.example.json)")

            classifier = InvoiceClassifier(templates_dir=templates_dir)

            mode_str = ""
            if dry_run:
                mode_str = " (DRY RUN)"
            if upload:
                mode_str += " + UPLOAD"

            print("\n" + "="*70)
            print(f"PROCESSING INVOICES from {invoices_dir}{mode_str}")
            print("="*70)

            stats = process_and_move(
                classifier=classifier,
                source_dir=invoices_dir,
                matched_dir=matched_dir,
                review_dir=review_dir,
                integrated_dir=integrated_dir,
                dry_run=dry_run,
                upload=upload
            )

            print("\n" + "="*70)
            print("PROCESSING SUMMARY")
            print("="*70)
            print(f"Total processed: {stats['total']}")
            print(f"Integrated (-> INTEGRATED/): {stats['integrated']}")
            print(f"Matched (-> MATCHED/): {stats['matched']}")
            print(f"For review (-> REVIEW/): {stats['review']}")
            print(f"Errors: {stats['errors']}")

            if upload:
                print(f"Uploaded to API: {stats['uploaded']}")
                if stats['upload_failed'] > 0:
                    print(f"Upload failed: {stats['upload_failed']}")

            if dry_run:
                print("\n[DRY RUN] No files were moved. Run without --dry-run to process.")

        elif command == '-h':
            print_usage()

        else:
            print(f"Unknown command: {command}")
            print_usage()

    else:
        # No command - just classify and show results
        # Default: Classify and show results without moving
        classifier = InvoiceClassifier(templates_dir=templates_dir)

        print("\n" + "="*70)
        print("INVOICE CLASSIFICATION RESULTS")
        print("="*70)

        results = classifier.classify_batch(invoices_dir)

        # Group by supplier for summary
        by_supplier: dict[str, list[tuple[str, ClassificationResult]]] = {}
        for filename, result in sorted(results.items()):
            if result.supplier not in by_supplier:
                by_supplier[result.supplier] = []
            by_supplier[result.supplier].append((filename, result))

        for supplier, items in sorted(by_supplier.items()):
            print(f"\n{supplier.upper()} ({len(items)} invoices)")
            print("-" * 50)
            for filename, result in items[:5]:  # Show first 5
                date_str = result.invoice_date or "no date"
                print(f"  {filename}: {result.confidence:.2f} ({result.method}) [{date_str}]")
            if len(items) > 5:
                print(f"  ... and {len(items) - 5} more")

        # Summary
        print("\n" + "="*70)
        print("SUMMARY")
        print("="*70)
        total = len(results)
        classified = sum(1 for r in results.values() if r.supplier != 'unknown')
        with_date = sum(1 for r in results.values() if r.invoice_date)
        print(f"Total invoices: {total}")
        print(f"Classified: {classified} ({classified/total*100:.1f}%)")
        print(f"With date extracted: {with_date} ({with_date/total*100:.1f}%)")
        print(f"Unknown: {total - classified}")
        print(f"\nRun 'python classifier.py process' to rename and move files.")
