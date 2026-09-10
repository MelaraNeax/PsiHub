import pymupdf4llm
import tempfile
import os
import urllib.request

def main():
    # URL de un PDF open access
    url = "https://arxiv.org/pdf/1706.03762.pdf" # Attention is all you need (tiene imágenes)
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    pdf_bytes = urllib.request.urlopen(req).read()
    
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = tmp.name

    try:
        # Default
        md_text = pymupdf4llm.to_markdown(tmp_path)
        print("DEFAULT REFERENCES:")
        for line in md_text.splitlines():
            if "![" in line or "image" in line.lower() or ".png" in line:
                print("  ", line)
                
        # Con write_images=True
        img_dir = "test_images"
        os.makedirs(img_dir, exist_ok=True)
        md_text_images = pymupdf4llm.to_markdown(tmp_path, write_images=True, image_path=img_dir)
        print("\nWITH WRITE_IMAGES REFERENCES:")
        for line in md_text_images.splitlines():
            if "![" in line or "image" in line.lower() or ".png" in line:
                print("  ", line)
    finally:
        os.unlink(tmp_path)

if __name__ == "__main__":
    main()
