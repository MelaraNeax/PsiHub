import os
import sys
import json
import asyncio
import time
from pathlib import Path

# Fix relative imports to import app
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app import process_pdf, log_section

async def run_benchmark(pdf_dir: str):
    dir_path = Path(pdf_dir)
    if not dir_path.exists() or not dir_path.is_dir():
        print(f"Directory not found: {dir_path}")
        return

    pdfs = list(dir_path.glob("*.pdf"))
    if not pdfs:
        print(f"No PDFs found in {dir_path}")
        return

    print(f"Found {len(pdfs)} PDFs for benchmarking.")
    results = []

    for pdf_file in pdfs:
        log_section(f"Benchmarking: {pdf_file.name}")
        pdf_bytes = pdf_file.read_bytes()
        t0 = time.time()
        
        try:
            res = await process_pdf(pdf_bytes)
            elapsed = time.time() - t0
            
            ocr = res.get("ocr_stats", {})
            trans = res.get("translation_stats", {})
            qr = res.get("quality_report", {})
            
            summary = {
                "file": pdf_file.name,
                "success": res.get("success"),
                "time_sec": round(elapsed, 2),
                "pages_total": ocr.get("pages_analyzed", 0),
                "pages_issues": ocr.get("pages_with_issues", 0),
                "mistral_pages": ocr.get("pages_sent_to_mistral", 0),
                "mistral_regions": ocr.get("regions_sent_to_mistral", 0),
                "mistral_cache_hits": ocr.get("cache_hits", 0),
                "mistral_cost_usd": round(ocr.get("estimated_cost_usd", 0), 4),
                "deepseek_chunks_total": trans.get("chunks_total", 0),
                "deepseek_chunks_failed": trans.get("chunks_failed", 0),
                "deepseek_cache_hits": trans.get("cache_hits", 0),
                "avg_quality_score": qr.get("average_score", 0),
                "doc_id": res.get("document_id")
            }
            results.append(summary)
            print(f"Done: {pdf_file.name} in {elapsed:.1f}s")
        except Exception as e:
            elapsed = time.time() - t0
            print(f"Failed: {pdf_file.name} in {elapsed:.1f}s - {e}")
            results.append({
                "file": pdf_file.name,
                "success": False,
                "time_sec": round(elapsed, 2),
                "error": str(e)
            })

    report_file = dir_path / "benchmark_report.json"
    report_file.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding='utf-8')
    print(f"\nBenchmark complete! Report saved to {report_file}")
    
    # Print summary table
    print("\n" + "="*80)
    print(f"{'File':<30} | {'Time':<6} | {'Pgs':<4} | {'Iss':<4} | {'M.Pgs':<5} | {'M.Reg':<5} | {'Cost$':<6} | {'TransOK':<7} | {'T.Cache':<7}")
    print("-" * 80)
    for r in results:
        if r.get("success"):
            print(f"{r['file'][:28]:<30} | {r['time_sec']:<6} | {r['pages_total']:<4} | {r['pages_issues']:<4} | {r['mistral_pages']:<5} | {r['mistral_regions']:<5} | {r['mistral_cost_usd']:<6} | {r['deepseek_chunks_total']-r['deepseek_chunks_failed']:<7} | {r['deepseek_cache_hits']:<7}")
        else:
            print(f"{r['file'][:28]:<30} | FAILED: {r.get('error')}")
    print("="*80)

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python benchmark.py <path_to_pdf_directory>")
    else:
        asyncio.run(run_benchmark(sys.argv[1]))
