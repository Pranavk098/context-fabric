"""Render a block of terminal text as an SVG terminal window (standard library only).

    python scripts/render_terminal.py INPUT.txt OUTPUT.svg "title" [max_columns]

Used for the README example images so they come from real command output, not mock-ups.
"""

from __future__ import annotations

import sys
from pathlib import Path
from xml.sax.saxutils import escape

CHAR_W = 8.4
LINE_H = 20
PAD = 22
TITLE_H = 40


def render(text: str, title: str, max_cols: int = 110) -> str:
    lines = [ln.rstrip("\n")[:max_cols] for ln in text.splitlines()]
    cols = max((len(ln) for ln in lines), default=20)
    width = int(PAD * 2 + cols * CHAR_W)
    height = TITLE_H + PAD + len(lines) * LINE_H + PAD // 2
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" '
        f'height="{height}" role="img" aria-label="{escape(title)}">',
        f"<title>{escape(title)}</title>",
        f'<rect width="{width}" height="{height}" rx="12" fill="#0b1020"/>',
        f'<rect width="{width}" height="{TITLE_H}" rx="12" fill="#161f3a"/>',
        f'<rect y="{TITLE_H - 12}" width="{width}" height="12" fill="#161f3a"/>',
        '<circle cx="22" cy="20" r="6" fill="#fb7185"/>',
        '<circle cx="42" cy="20" r="6" fill="#fbbf24"/>',
        '<circle cx="62" cy="20" r="6" fill="#5eead4"/>',
        f'<text x="84" y="25" font-family="Segoe UI, system-ui, sans-serif" font-size="13" '
        f'fill="#94a3b8">{escape(title)}</text>',
        '<g font-family="Cascadia Mono, SF Mono, Consolas, Menlo, monospace" font-size="13.5" '
        'xml:space="preserve" style="white-space:pre">',
    ]
    for i, ln in enumerate(lines):
        y = TITLE_H + PAD + i * LINE_H
        if i == 0 and ln.startswith("$"):
            color = "#5eead4"
        elif ln.rstrip().endswith(":") or ln.startswith(("Claude Code", "First-turn", "Itemized")):
            color = "#f8fafc"
        elif "[unused]" in ln or "never" in ln.lower():
            color = "#fbbf24"
        else:
            color = "#cbd5e1"
        out.append(
            f'<text x="{PAD}" y="{y}" fill="{color}" xml:space="preserve" '
            f'style="white-space:pre">{escape(ln) or " "}</text>'
        )
    out += ["</g>", "</svg>"]
    return "\n".join(out) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) < 4:
        print(__doc__)
        return 2
    text = Path(argv[1]).read_text(encoding="utf-8")
    cols = int(argv[4]) if len(argv) > 4 else 110
    Path(argv[2]).write_text(render(text, argv[3], cols), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
