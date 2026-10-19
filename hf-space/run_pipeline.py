"""CLI de desarrollo: PDF -> JSON estructurado + Markdown + PDF traducido.

Uso:
    python run_pipeline.py ruta/al/articulo.pdf --out salida/
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from exporters import document_to_markdown, document_to_pdf
from vision_extract import extract_document

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')


def api_keys() -> list[str]:
    keys = [os.getenv(f'DEEPSEEK_API_KEY_{i}', '').strip() for i in range(1, 7)]
    legacy = os.getenv('DEEPSEEK_API_KEY', '').strip()
    if not any(keys) and legacy:
        keys = [legacy]
    return [k for k in keys if k]


async def main() -> None:
    parser = argparse.ArgumentParser(description='Procesa un PDF con vision y exporta JSON/MD/PDF.')
    parser.add_argument('pdf')
    parser.add_argument('--out', default='salida')
    parser.add_argument('--model', default=os.getenv('DEEPSEEK_MODEL', 'deepseek-v4-flash'))
    args = parser.parse_args()

    pdf_path = Path(args.pdf).resolve()
    pdf_bytes = pdf_path.read_bytes()
    doc_id = hashlib.sha256(pdf_bytes).hexdigest()
    out_dir = Path(args.out).resolve()
    images_dir = out_dir / 'images'
    out_dir.mkdir(parents=True, exist_ok=True)

    document = await extract_document(
        pdf_bytes,
        api_keys(),
        model=args.model,
        images_dir=images_dir,
        doc_id=doc_id,
        progress=lambda done, total: print(f'  progreso {done}/{total}', flush=True),
    )

    json_path = out_dir / f'{doc_id[:12]}.json'
    md_path = out_dir / f'{doc_id[:12]}.md'
    pdf_out = out_dir / f'{doc_id[:12]}.pdf'
    json_path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding='utf-8')
    md_path.write_text(document_to_markdown(document), encoding='utf-8')
    document_to_pdf(document, pdf_out, images_dir)

    print(json.dumps(document.get('stats', {}), ensure_ascii=False, indent=2))
    print('JSON:', json_path)
    print('MD:  ', md_path)
    print('PDF: ', pdf_out)


if __name__ == '__main__':
    asyncio.run(main())
