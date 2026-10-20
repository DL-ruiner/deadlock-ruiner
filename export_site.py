"""Build site/ for publishing: page without its own document skeleton plus data and assets."""
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).parent
WEB, OUT = ROOT / "web", ROOT / "site"


def main():
    shutil.rmtree(OUT, ignore_errors=True)
    shutil.copytree(WEB, OUT, ignore=shutil.ignore_patterns("index.html"))
    page = (WEB / "index.html").read_text(encoding="utf-8")
    # the host wraps the page in its own doctype/html/head/body
    page = re.sub(r"<!doctype html>\s*|</?html[^>]*>\s*|</?head>\s*|</?body>\s*|<meta charset=\"utf-8\">\s*|<meta name=\"viewport\"[^>]*>\s*",
                  "", page, flags=re.I)
    (OUT / "index.html").write_text(page, encoding="utf-8")
    files = sorted(p.relative_to(OUT).as_posix() for p in OUT.rglob("*") if p.is_file() and p.name != "index.html")
    print(len(files), "files")
    (ROOT / "site_files.txt").write_text("\n".join(files), encoding="utf-8")


if __name__ == "__main__":
    main()
