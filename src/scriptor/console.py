"""Superfície visual do Scriptor.

Dark-first, tipografia editorial, cor usada como sinal — nunca como decoração.

O console do Windows ainda entrega cp1252 por padrão, e uma linha divisória
U+2500 basta para derrubar o processo com ``UnicodeEncodeError``. Duas defesas,
nesta ordem: negociar UTF-8 na inicialização e, se não for possível, escolher
glifos ASCII equivalentes. A saída degrada; nunca quebra.
"""

from __future__ import annotations

import contextlib
import sys

from rich.console import Console
from rich.theme import Theme

THEME = Theme(
    {
        # Estrutura
        "s.brand": "bold #ece7dd",
        "s.rule": "#37342f",
        "s.label": "#736c62",
        "s.value": "#d8d3c9",
        "s.dim": "#8b857b",
        "s.path": "#a9b7c6",
        # Sinal
        "s.accent": "#c9a227",
        "s.ok": "#84a95c",
        "s.warn": "#d79b4a",
        "s.err": "#cf6060",
        "s.skip": "#6a8398",
        "s.busy": "#c9a227",
        # Estados de documento
        "s.mode.skip": "#6a8398",
        "s.mode.redo": "#7fa3b8",
        "s.mode.force": "#d79b4a",
        "s.mode.passthrough": "#736c62",
    }
)


def _negotiate_utf8() -> None:
    """Tenta colocar o terminal e os fluxos de saída em UTF-8."""
    if sys.platform == "win32":
        # Falha quando não há console anexado (execução por pythonw, serviço).
        with contextlib.suppress(Exception):
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)  # type: ignore[attr-defined]
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8", errors="replace")


_negotiate_utf8()

console = Console(theme=THEME, highlight=False, soft_wrap=False)
err_console = Console(theme=THEME, highlight=False, stderr=True)


def _encodable(sample: str) -> bool:
    """O terminal aceita estes caracteres — e consegue desenhá-los?

    Duas perguntas distintas. Codificar em UTF-8 sempre funciona depois do
    ``reconfigure``; o console legado do Windows, porém, usa fonte raster sem
    caracteres de desenho, e o resultado seria uma fileira de quadrados. Aí
    ASCII é a escolha correta, não a inferior.
    """
    if getattr(console, "legacy_windows", False):
        return False
    encoding = getattr(console.file, "encoding", None) or "utf-8"
    try:
        sample.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


UNICODE_OK = _encodable("─▰▱✓✗…·")

RULE_CHAR = "─" if UNICODE_OK else "-"
BAR_FULL = "▰" if UNICODE_OK else "#"
BAR_EMPTY = "▱" if UNICODE_OK else "."
DOT = "·" if UNICODE_OK else "-"
ELLIPSIS = "…" if UNICODE_OK else "~"
SPINNER = "dots" if UNICODE_OK else "line"

_GLYPHS = (
    {"ok": "✓", "err": "✗", "warn": "!", "skip": "–", "busy": "·"}
    if UNICODE_OK
    else {"ok": "+", "err": "x", "warn": "!", "skip": "-", "busy": "."}
)


def glyph(kind: str) -> str:
    """Marcador de estado de largura fixa."""
    return _GLYPHS.get(kind, "?")


def divider(width: int = 68, *, target: Console | None = None) -> None:
    (target or console).print(f"  [s.rule]{RULE_CHAR * width}[/]")


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def fmt_duration(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def fmt_delta(before: int, after: int) -> str:
    """Variação de tamanho, assinada. Crescimento não é falha — PDF/A embute fontes."""
    if before <= 0:
        return "—" if UNICODE_OK else "-"
    pct = (after - before) / before * 100
    return f"{pct:+.0f}%"


def elide(text: str, width: int) -> str:
    """Encurta pelo meio: o começo identifica, o fim desambigua."""
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    head = (width - 1) // 2
    tail = width - 1 - head
    return f"{text[:head]}{ELLIPSIS}{text[-tail:]}"
