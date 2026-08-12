"""Verificação de conformidade PDF/A da saída.

O script original produzia arquivos rotulados como PDF/A e nunca conferia se de
fato eram. Arquivo de acervo não verificado é uma promessa, não uma garantia.

Duas camadas, complementares:

* **verificação interna** (sempre disponível, via pikepdf) — confere as
  propriedades estruturais que reprovam a maioria dos arquivos na prática:
  identificação XMP, OutputIntent, ausência de criptografia e embutimento de
  todas as fontes;
* **veraPDF** (opcional) — o validador de referência do PDF Association. Quando
  está instalado, a palavra final é dele.

A camada interna não substitui o veraPDF: ela reprova o que é claramente
inválido, sem afirmar que o restante é perfeito.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pikepdf

from .analysis import _PDFA_PART_RE
from .toolchain import Tool

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

_FONT_FILE_KEYS = ("/FontFile", "/FontFile2", "/FontFile3")

#: Perfil do Scriptor → flavour do veraPDF.
_VERAPDF_FLAVOUR = {"pdfa-1": "1b", "pdfa-2": "2b", "pdfa-3": "3b"}


@dataclass(slots=True)
class ConformanceReport:
    ok: bool
    validator: str
    declared: str | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        if self.ok:
            return f"PDF/A-{self.declared}" if self.declared else "válido"
        return "reprovado"

    def detail(self) -> str:
        return "; ".join(self.problems) if self.problems else ""


def verify(
    path: Path,
    *,
    expected: str,
    verapdf: Tool | None = None,
    timeout: int = 180,
) -> ConformanceReport:
    """Verifica ``path`` contra o perfil ``expected`` (``pdfa-1|pdfa-2|pdfa-3|pdf``)."""
    if expected == "pdf":
        return _verify_readable(path)

    internal = _verify_internal(path, expected=expected)
    if verapdf is None or not internal.ok:
        # Se a checagem estrutural já reprovou, chamar o veraPDF só custa tempo:
        # o arquivo não vai passar de qualquer forma.
        return internal

    external = _verify_verapdf(path, expected=expected, tool=verapdf, timeout=timeout)
    if external is None:
        internal.validator = "interno (veraPDF indisponível)"
        return internal
    external.declared = external.declared or internal.declared
    return external


def _verify_readable(path: Path) -> ConformanceReport:
    try:
        with pikepdf.open(path) as pdf:
            _ = len(pdf.pages)
    except Exception as exc:
        return ConformanceReport(False, "interno", problems=[f"PDF ilegível: {exc}"])
    return ConformanceReport(True, "interno")


def _verify_internal(path: Path, *, expected: str) -> ConformanceReport:
    wanted_part = expected.removeprefix("pdfa-")
    problems: list[str] = []
    declared: str | None = None

    try:
        pdf = pikepdf.open(path)
    except Exception as exc:
        return ConformanceReport(False, "interno", problems=[f"PDF ilegível: {exc}"])

    with pdf:
        if pdf.is_encrypted:
            problems.append("arquivo criptografado (proibido em PDF/A)")

        declared = _declared_part(pdf)
        if declared is None:
            problems.append("sem identificação PDF/A no XMP (pdfaid:part)")
        elif not declared.startswith(wanted_part):
            problems.append(f"declara PDF/A-{declared}, esperado PDF/A-{wanted_part}")

        if not _has_output_intent(pdf):
            problems.append("sem OutputIntent com perfil de cor")

        missing_fonts = _fonts_not_embedded(pdf)
        if missing_fonts:
            shown = ", ".join(sorted(missing_fonts)[:5])
            suffix = "…" if len(missing_fonts) > 5 else ""
            problems.append(f"fonte(s) não embutida(s): {shown}{suffix}")

    return ConformanceReport(not problems, "interno", declared=declared, problems=problems)


def _declared_part(pdf: pikepdf.Pdf) -> str | None:
    try:
        metadata = pdf.Root.get("/Metadata")
        if metadata is None:
            return None
        raw = bytes(metadata.read_bytes())
    except Exception:
        return None
    match = _PDFA_PART_RE.search(raw)
    return match.group(1).decode() if match else None


def _has_output_intent(pdf: pikepdf.Pdf) -> bool:
    try:
        intents = pdf.Root.get("/OutputIntents")
        if intents is None or len(intents) == 0:
            return False
        for intent in intents:
            if intent.get("/DestOutputProfile") is not None:
                return True
    except Exception:
        return False
    return False


def _fonts_not_embedded(pdf: pikepdf.Pdf) -> set[str]:
    """Nomes de fontes sem programa de fonte embutido.

    PDF/A exige que toda fonte usada esteja embutida — inclusive as 14 padrão,
    que em PDF comum podem ser referenciadas por nome.
    """
    missing: set[str] = set()
    for page in pdf.pages:
        fonts = None
        with contextlib.suppress(Exception):
            resources = page.obj.get("/Resources")
            fonts = resources.get("/Font") if resources is not None else None
        if fonts is None:
            continue
        for font in fonts.values():
            # Fonte ilegível não é prova de conformidade: se não dá para
            # confirmar que está embutida, ela não conta como embutida.
            with contextlib.suppress(Exception):
                if not _font_is_embedded(font):
                    missing.add(str(font.get("/BaseFont", "?")).lstrip("/"))
    return missing


def _font_is_embedded(font) -> bool:
    subtype = str(font.get("/Subtype", ""))
    if subtype == "/Type3":
        # Glifos são content streams; não há programa de fonte a embutir.
        return True
    if subtype == "/Type0":
        descendants = font.get("/DescendantFonts")
        if descendants is None or len(descendants) == 0:
            return False
        return all(_descriptor_has_file(child.get("/FontDescriptor")) for child in descendants)
    return _descriptor_has_file(font.get("/FontDescriptor"))


def _descriptor_has_file(descriptor) -> bool:
    if descriptor is None:
        return False
    return any(descriptor.get(key) is not None for key in _FONT_FILE_KEYS)


def _verify_verapdf(
    path: Path, *, expected: str, tool: Tool, timeout: int
) -> ConformanceReport | None:
    """Roda o veraPDF. Devolve ``None`` se o validador não pôde ser executado."""
    flavour = _VERAPDF_FLAVOUR.get(expected, "2b")
    command = [str(tool.path), "--format", "json", "--flavour", flavour, str(path)]
    try:
        proc = subprocess.run(  # noqa: S603
            command,
            capture_output=True,
            timeout=timeout,
            check=False,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    stdout = proc.stdout.decode("utf-8", "replace")
    problems: list[str] = []
    compliant: bool | None = None

    try:
        payload = json.loads(stdout)
        jobs = payload.get("report", payload).get("jobs", [])
        for job in jobs:
            result = job.get("validationResult") or {}
            compliant = bool(result.get("compliant"))
            for rule in (result.get("details") or {}).get("ruleSummaries", []):
                clause = rule.get("clause", "?")
                description = rule.get("description", "").strip()
                failures = rule.get("failedChecks", 0)
                problems.append(f"{clause}: {description} ({failures}×)")
    except (json.JSONDecodeError, AttributeError, TypeError):
        compliant = proc.returncode == 0
        if not compliant:
            problems.append(stdout.strip()[:400] or "veraPDF reprovou o arquivo")

    if compliant is None:
        return None
    return ConformanceReport(
        compliant,
        "veraPDF",
        declared=flavour[0],
        problems=problems[:10],
    )
