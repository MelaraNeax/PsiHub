import fitz
import pymupdf4llm
import re
import sys

def remove_headers_footers(doc: fitz.Document):
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
