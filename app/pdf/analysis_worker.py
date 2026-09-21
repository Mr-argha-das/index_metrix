"""Isolated PDF parser process; never executes embedded PDF scripts.

Linux deployments enforce a 512 MiB address-space cap and 30s CPU cap.
The parent also enforces a 45s wall deadline and kills cancelled workers.
"""
import json
import sys
from dataclasses import asdict


def main():
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    from .analyzer import analyze_pdf
    import pymupdf
    pymupdf.TOOLS.mupdf_display_errors(False)
    pymupdf.TOOLS.mupdf_display_warnings(False)
    data = sys.stdin.buffer.read(200 * 1024 * 1024 + 1)
    if len(data) > 200 * 1024 * 1024:
        raise ValueError("PDF parser input cap exceeded")
    result = analyze_pdf(data)
    if result.error:
        result.error = result.error[:500]
    sys.stdout.write(json.dumps(asdict(result)))


if __name__ == "__main__":
    main()
