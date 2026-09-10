import json
import re
from pathlib import Path

cache_dir = Path("hf-space/cache")
for f in cache_dir.glob("*.json"):
    data = json.loads(f.read_text(encoding="utf-8"))
    md = data.get("markdown", "")
    urls = re.findall(r'href="([^"]+)"', md)
    title = ""
    for line in md.split("\n"):
        line = line.strip()
        if line and not line.startswith("<!--"):
            title = line[:100]
            break
    print(f"{f.name} -> {title} | url: {urls[0] if urls else 'none'}")
