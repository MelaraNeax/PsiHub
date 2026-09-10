import pymupdf4llm
import tempfile
import urllib.request
import os

def main():
    url = "https://arxiv.org/pdf/1706.03762.pdf" # Attention is all you need
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    # I'll just create a dummy pdf instead of downloading to avoid SSL error
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 50), "Hello page 1")
    page2 = doc.new_page()
    page2.insert_text((50, 50), "Hello page 2")
    
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        doc.save(tmp.name)
        tmp_path = tmp.name

    try:
        chunks = pymupdf4llm.to_markdown(tmp_path, page_chunks=True)
        print("CHUNKS INFO:")
        for c in chunks:
            print(c.keys())
            print(c.get("metadata", {}))
    finally:
        os.unlink(tmp_path)

if __name__ == "__main__":
    main()
