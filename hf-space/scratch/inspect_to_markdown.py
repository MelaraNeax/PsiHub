import inspect
import pymupdf4llm

sig = inspect.signature(pymupdf4llm.to_markdown)
print(sig)
for param in sig.parameters.values():
    print(f"Name: {param.name}, Default: {param.default}, Annotation: {param.annotation}")
