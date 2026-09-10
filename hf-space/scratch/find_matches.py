import hashlib
from pathlib import Path

cache_dir = Path("hf-space/cache")
cached_hashes = {f.stem for f in cache_dir.glob("*.json")}

def check_string(s):
    if not s: return
    for suf in ["_gemini", ""]:
        val = f"{s}{suf}".encode()
        h = hashlib.sha256(val).hexdigest()[:24]
        if h in cached_hashes:
            print(f"MATCH: '{s}' (suffix='{suf}') -> {h}.json")

# Let's test DOIs, OpenAlex IDs, URLs in the cache files themselves
for f in cache_dir.glob("*.json"):
    text = f.read_text(encoding="utf-8")
    import re
    dois = re.findall(r'10\.\d{4,9}/[-._;()/:A-Z0-9]+', text, re.IGNORECASE)
    for d in dois:
        check_string(d)
        check_string(f"https://doi.org/{d}")
        check_string(f"doi:{d}")
