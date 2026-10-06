"""Bounded PDF extraction subprocess. Never executes document actions or links."""
import io
import json
import resource
import sys


def main():
    # Callers may raise the bounds (the problem importer does); defaults suit literature capture.
    limits = {"max_bytes": 24 * 1024**2, "memory_mb": 768, "cpu_seconds": 20, "max_pages": 100, "max_chars": 600_000}
    if len(sys.argv) > 1:
        limits.update({key: int(value) for key, value in json.loads(sys.argv[1]).items() if key in limits})
    # Limit even decompression/parser allocations, not only extracted text.
    memory = limits["memory_mb"] * 1024**2
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CPU, (limits["cpu_seconds"], limits["cpu_seconds"]))
    from pypdf import PdfReader
    raw = sys.stdin.buffer.read(limits["max_bytes"] + 1)
    if len(raw) > limits["max_bytes"]:
        raise ValueError("PDF exceeds extraction input limit")
    reader = PdfReader(io.BytesIO(raw), strict=False)
    if reader.is_encrypted:
        raise ValueError("Encrypted PDF cannot be extracted")
    pages, remaining = [], limits["max_chars"]
    for number, page in enumerate(reader.pages):
        if number >= limits["max_pages"] or remaining <= 0:
            break
        text = page.extract_text() or ""
        pages.append({"page": number + 1, "text": text[:remaining]})
        remaining -= len(text)
    print(json.dumps({"pages": pages, "page_count": len(reader.pages),
                      "truncated": remaining < 0 or len(pages) < len(reader.pages)}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # No document text, URLs or internal paths in error output.
        print(type(exc).__name__, file=sys.stderr)
        sys.exit(1)
