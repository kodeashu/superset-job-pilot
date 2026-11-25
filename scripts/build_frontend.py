"""Build and validate the static Netlify frontend from ``web_server.HTML_TEMPLATE``.

Usage:
    SJP_BUILD_ONLY=1 python scripts/build_frontend.py
    SJP_BUILD_ONLY=1 python scripts/build_frontend.py --check
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("SJP_BUILD_ONLY", "1")

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import security  # noqa: E402
import web_server  # noqa: E402

DIST_DIR = ROOT_DIR / "netlify_dist"
INDEX_PATH = DIST_DIR / "index.html"
HEADERS_PATH = DIST_DIR / "_headers"
REDIRECTS_PATH = DIST_DIR / "_redirects"

SCRIPT_RE = re.compile(r"<script(?:\s[^>]*)?>(.*?)</script>", re.S | re.I)


def render_index_html() -> str:
    """Return the normalized static HTML document built from HTML_TEMPLATE."""
    return web_server.HTML_TEMPLATE.rstrip("\n") + "\n"


def render_netlify_headers() -> str:
    """Build Netlify ``_headers`` matching Flask security headers and CSP."""
    lines = [
        "/*",
        f"  Content-Security-Policy: {security.CSP_POLICY}",
        "  X-Content-Type-Options: nosniff",
        "  X-Frame-Options: DENY",
        "  Referrer-Policy: strict-origin-when-cross-origin",
        "  Permissions-Policy: camera=(), geolocation=(), microphone=()",
        "  Cross-Origin-Opener-Policy: same-origin-allow-popups",
        "  Strict-Transport-Security: max-age=31536000; includeSubDomains",
    ]
    return "\n".join(lines) + "\n"


def render_netlify_redirects() -> str:
    """Build Netlify ``_redirects`` proxying ``/api/*`` to Render."""
    return (
        "/api/*  https://superset-job-pilot.onrender.com/api/:splat  200!\n"
        "/*  /index.html  200\n"
    )


def validate_inline_scripts(html: str) -> int:
    """Validate every non-empty inline ``<script>`` block with ``node --check``."""
    node_bin = shutil.which("node")
    if not node_bin:
        raise RuntimeError("node binary not found on PATH; cannot syntax-check inline scripts")

    checked = 0
    for idx, body in enumerate(SCRIPT_RE.findall(html)):
        if not body.strip():
            continue
        with tempfile.NamedTemporaryFile("w", suffix=f"_{idx}.js", encoding="utf-8", delete=False) as tmp:
            tmp.write(body)
            tmp_path = tmp.name
        try:
            subprocess.run([node_bin, "--check", tmp_path], check=True)
            checked += 1
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    return checked


def check_outputs(expected: dict[Path, str]) -> int:
    """Verify generated artifacts already match on disk."""
    mismatches = []
    for path, content in expected.items():
        if not path.exists():
            mismatches.append(f"Missing artifact: {path.relative_to(ROOT_DIR)}")
            continue
        actual = path.read_text(encoding="utf-8")
        if actual != content:
            mismatches.append(
                f"Stale artifact: {path.relative_to(ROOT_DIR)} "
                f"(expected {len(content)} chars, found {len(actual)} chars). "
                "Run: SJP_BUILD_ONLY=1 python scripts/build_frontend.py"
            )
    if mismatches:
        for msg in mismatches:
            print(msg, file=sys.stderr)
        return 1
    print(f"Frontend artifacts up to date ({len(expected)} files verified).")
    return 0


def write_outputs(expected: dict[Path, str]) -> None:
    """Write generated frontend artifacts to disk."""
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    for path, content in expected.items():
        path.write_text(content, encoding="utf-8")
        print(f"Wrote {path.relative_to(ROOT_DIR)} ({len(content.encode('utf-8'))} bytes)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build or check netlify_dist frontend artifacts.")
    parser.add_argument("--check", action="store_true", help="Fail if netlify_dist artifacts are stale or missing.")
    args = parser.parse_args(argv)

    html = render_index_html()
    checked = validate_inline_scripts(html)
    print(f"Validated {checked} inline <script> block(s) with node --check.")

    expected = {
        INDEX_PATH: html,
        HEADERS_PATH: render_netlify_headers(),
        REDIRECTS_PATH: render_netlify_redirects(),
    }
    if args.check:
        return check_outputs(expected)
    write_outputs(expected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
