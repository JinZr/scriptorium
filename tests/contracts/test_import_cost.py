import subprocess
import sys


def test_retrieval_entrypoints_do_not_import_pdf_rendering_or_asyncio():
    # Each task command is a fresh process, so module import time is paid on every retrieval call.
    probe = (
        "import sys\n"
        "from scriptorium import cli, service\n"
        "cli.build_parser()\n"
        "print(sorted(name for name in ('pymupdf', 'fitz', 'asyncio') if name in sys.modules))\n"
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "[]"
