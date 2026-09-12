import fitz
import pymupdf4llm
import re
import sys

def normalize_editorial_text(text: str) -> str:
    """
    Normaliza un bloque para detectar encabezados/pies repetidos
    aunque contengan números de página u otras variaciones menores.
    """
    text = re.sub(r"\s+", " ", text.strip())

    # Números aislados o números al final del encabezado/pie
    text = re.sub(r"\b\d+\b", "#", text)

    # Normalizar guiones/separadores
    text = re.sub(r"\s*[-–—|]\s*", " - ", text)

    return text.lower().strip()


def is_page_number(text: str) -> bool:
    """
    Detecta números de página y variantes habituales.
    """
    s = re.sub(r"\s+", " ", text.strip())

    patterns = [
        r"^\d+$",                              # 128
        r"^[-–—]?\s*\d+\s*[-–—]?$",            # - 128 -
        r"^page\s+\d+$",                       # Page 128
        r"^p\.?\s*\d+$",                       # p. 128 / p 128
        r"^\d+\s+(?:of|de)\s+\d+$",            # 128 of 300
        r"^page\s+\d+\s+(?:of|de)\s+\d+$",     # Page 128 of 300
        r"^\d+\s*/\s*\d+$",                    # 128 / 300
    ]

    return any(re.fullmatch(p, s, re.IGNORECASE) for p in patterns)


def remove_headers_footers(doc: fitz.Document):
    """
    Elimina encabezados, pies de página y números de página
    pertenecientes al diseño editorial del PDF.

    Características:
    - Detecta números de página variables.
    - Detecta headers/footers repetidos aunque contengan números.
    - Solo elimina elementos situados realmente en el margen.
    - No elimina texto del cuerpo aunque coincida con un header/footer.
    - Conserva título, autores y afiliaciones de la primera página.
    """

    if doc.page_count < 2:
        return

    # ---------------------------------------------------------
    # CONFIGURACIÓN
    # ---------------------------------------------------------

    HEADER_MARGIN = 0.06
    FOOTER_MARGIN = 0.06

    # Un header/footer debe aparecer al menos en este porcentaje
    # de las páginas para considerarse editorial.
    REPEAT_RATIO = 0.30

    threshold = max(
        2,
        int(doc.page_count * REPEAT_RATIO)
    )

    header_candidates = {}
    footer_candidates = {}

    # ---------------------------------------------------------
    # 1. RECOPILAR CANDIDATOS
    # ---------------------------------------------------------

    for page in doc:
        rect = page.rect

        for block in page.get_text("blocks"):
            block_rect = fitz.Rect(block[:4])
            text = block[4].strip()

            if not text:
                continue

            # Ignorar números de página aislados.
            if is_page_number(text):
                continue

            normalized = normalize_editorial_text(text)

            # HEADER
            if block_rect.y1 <= rect.height * HEADER_MARGIN:
                header_candidates[normalized] = (
                    header_candidates.get(normalized, 0) + 1
                )

            # FOOTER
            elif block_rect.y0 >= rect.height * (1 - FOOTER_MARGIN):
                footer_candidates[normalized] = (
                    footer_candidates.get(normalized, 0) + 1
                )

    # ---------------------------------------------------------
    # 2. DETERMINAR QUÉ ES REALMENTE REPETITIVO
    # ---------------------------------------------------------

    repeated_headers = {
        text
        for text, count in header_candidates.items()
        if count >= threshold
    }

    repeated_footers = {
        text
        for text, count in footer_candidates.items()
        if count >= threshold
    }

    print(
        f"  🧹 Headers repetidos detectados: {len(repeated_headers)} | "
        f"Footers repetidos: {len(repeated_footers)}",
        flush=True
    )

    # ---------------------------------------------------------
    # 3. ELIMINAR
    # ---------------------------------------------------------

    removed_count = 0

    for page_num, page in enumerate(doc, start=1):
        rect = page.rect

        for block in page.get_text("blocks"):
            block_rect = fitz.Rect(block[:4])
            text = block[4].strip()

            if not text:
                continue

            normalized = normalize_editorial_text(text)

            should_remove = False
            reason = ""

            # -------------------------------------------------
            # A. NÚMERO DE PÁGINA
            # -------------------------------------------------

            if (
                block_rect.y0 >=
                rect.height * (1 - FOOTER_MARGIN)
                and is_page_number(text)
            ):
                should_remove = True
                reason = "page number"

            # -------------------------------------------------
            # B. HEADER REPETIDO
            # IMPORTANTE: solo dentro del header
            # -------------------------------------------------

            elif (
                block_rect.y1 <= rect.height * HEADER_MARGIN
                and normalized in repeated_headers
            ):
                should_remove = True
                reason = "repeated header"

            # -------------------------------------------------
            # C. FOOTER REPETIDO
            # IMPORTANTE: solo dentro del footer
            # -------------------------------------------------

            elif (
                block_rect.y0 >=
                rect.height * (1 - FOOTER_MARGIN)
                and normalized in repeated_footers
            ):
                should_remove = True
                reason = "repeated footer"

            if should_remove:
                page.add_redact_annot(
                    block_rect,
                    fill=(1, 1, 1)
                )

                removed_count += 1

                print(
                    f"    🗑 Página {page_num}: "
                    f"{reason}: {text[:100]!r}",
                    flush=True
                )

        page.apply_redactions()

    print(
        f"  🧹 Elementos editoriales eliminados: {removed_count}",
        flush=True
    )
    if doc.page_count < 3:
        return
    
    header_texts = {}
    footer_texts = {}
    
    for page in doc:
        rect = page.rect
        for b in page.get_text("blocks"):
            b_rect = fitz.Rect(b[:4])
            text = b[4].strip()
            if not text or len(text) < 4: continue 
            
            # Use smaller top/bottom margins, e.g., 10%
            if b_rect.y1 < rect.height * 0.10:
                header_texts[text] = header_texts.get(text, 0) + 1
            elif b_rect.y0 > rect.height * 0.90:
                footer_texts[text] = footer_texts.get(text, 0) + 1

    threshold = max(2, int(doc.page_count * 0.4))
    bad_texts = {k for k, v in header_texts.items() if v >= threshold} | {k for k, v in footer_texts.items() if v >= threshold}
    
    if bad_texts:
        print(f"Removing headers/footers: {bad_texts}")
        for page in doc:
            for b in page.get_text("blocks"):
                if b[4].strip() in bad_texts:
                    page.add_redact_annot(fitz.Rect(b[:4]), fill=(1, 1, 1))
            page.apply_redactions()

if __name__ == "__main__":
    pass
