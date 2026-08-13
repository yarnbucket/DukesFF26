#!/usr/bin/env python3
"""Build the hardened FF26 production HTML from index.dev.html.

Security/build rules:
- readable development file remains authoritative
- development-only browser exports are removed
- production mode is enabled
- JS/CSS comments are stripped
- JS/CSS whitespace is conservatively minified
- no source maps are produced or referenced

This build intentionally does NOT claim minification is secrecy.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from pygments import lex
from pygments.lexers import JavascriptLexer, CssLexer
from pygments.token import Comment

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "index.dev.html"
PROD = ROOT / "index.html"

DEV_BLOCK = re.compile(
    r"/\*\s*@DEV_EXPORTS_START\s*\*/[\s\S]*?/\*\s*@DEV_EXPORTS_END\s*\*/",
    re.I,
)

def minify_tokenized(source: str, lexer) -> str:
    out = []
    pending = ""
    for token_type, value in lex(source, lexer):
        if token_type in Comment:
            if "\n" in value or "\r" in value:
                pending = "\n"
            elif not pending:
                pending = " "
            continue
        if value.isspace():
            if "\n" in value or "\r" in value:
                pending = "\n"
            elif not pending:
                pending = " "
            continue
        if pending:
            out.append(pending)
            pending = ""
        out.append(value)
    return "".join(out).strip()

def minify_html_embedded(html: str) -> str:
    # Strip real HTML comments outside of script/style content after temporarily
    # replacing embedded blocks with sentinels.
    blocks = []
    pattern = re.compile(r"<(script|style)(\b[^>]*)>([\s\S]*?)</\1>", re.I)

    def stash(match):
        kind = match.group(1).lower()
        attrs = match.group(2)
        body = match.group(3)
        if kind == "script" and "src=" not in attrs.lower():
            body = minify_tokenized(body, JavascriptLexer())
        elif kind == "style":
            body = minify_tokenized(body, CssLexer())
        index = len(blocks)
        blocks.append(f"<{kind}{attrs}>{body}</{kind}>")
        return f"@@FF26_BLOCK_{index}@@"

    shell = pattern.sub(stash, html)
    shell = re.sub(r"<!--[\s\S]*?-->", "", shell)
    shell = re.sub(r"[ \t]+\n", "\n", shell)
    shell = re.sub(r"\n{3,}", "\n\n", shell)

    for i, block in enumerate(blocks):
        shell = shell.replace(f"@@FF26_BLOCK_{i}@@", block)
    return shell.strip() + "\n"

def build():
    html = DEV.read_text(encoding="utf-8")
    html = DEV_BLOCK.sub("", html)
    html = html.replace(
        "const FF26_BUILD_MODE='development';",
        "const FF26_BUILD_MODE='production';",
        1,
    )

    # Source maps are forbidden in production.
    if "sourceMappingURL" in html:
        raise RuntimeError("Development source contains a sourceMappingURL reference.")

    prod = minify_html_embedded(html)

    forbidden = [
        "window.FantasyDraftGrader",
        "window.DukesFF26UnifiedLogic",
        "window.DUKES_FF26_LEAGUE",
        "window.dukesLeagueAwareDraftScore",
        "window.DukesFF26DraftEngine",
        "window.ff26WrIntelligenceFromApp",
        "window.latestDraftGrade",
        "window.dukesBestFitRanking",
        "window.DUKES_BEST_FIT_CONSENSUS_WINDOW",
        "window.DUKES_ROUND_ONE_ADP_GUARDRAIL",
        "window.reloadFF26Status",
        "window.getFF26StatusDiagnostics",
        "sourceMappingURL",
    ]
    leaked = [item for item in forbidden if item in prod]
    if leaked:
        raise RuntimeError(f"Production exposure check failed: {leaked}")

    PROD.write_text(prod, encoding="utf-8")
    print(f"Built {PROD.name} from {DEV.name}")
    print(f"Development bytes: {DEV.stat().st_size:,}")
    print(f"Production bytes:  {PROD.stat().st_size:,}")

if __name__ == "__main__":
    try:
        build()
    except Exception as exc:
        print(f"FF26 production build failed: {exc}", file=sys.stderr)
        raise
