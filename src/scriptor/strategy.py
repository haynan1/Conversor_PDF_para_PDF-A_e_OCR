"""Escolha de estratégia por documento e escada de recuperação.

Duas decisões vivem aqui.

**Qual modo de OCR aplicar.** ``--skip-text`` pula qualquer página que contenha
qualquer texto; ``--force-ocr`` rasteriza tudo, destruindo texto vetorial;
``--redo-ocr`` preserva texto real e reprocessa só as regiões de imagem. Nenhum
dos três é correto para todo documento — a escolha vem do perfil medido em
:mod:`scriptor.analysis`.

**O que fazer quando falha.** O ``.bat`` original ignorava o código de saída, de
modo que uma conversão falha produzia a mesma mensagem que uma bem-sucedida.
Aqui cada código de saída do OCRmyPDF é classificado em: fatal (abortar),
degradável (repetir o mesmo modo com opções mais conservadoras) ou escalável
(subir um degrau na escada de modos).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from .analysis import DocumentProfile
from .config import Settings

# Códigos de saída do OCRmyPDF (ocrmypdf.ExitCode), replicados para que o módulo
# permaneça testável sem importar a biblioteca inteira.
EXIT_OK = 0
EXIT_BAD_ARGS = 1
EXIT_INPUT_FILE = 2
EXIT_MISSING_DEPENDENCY = 3
EXIT_INVALID_OUTPUT_PDF = 4
EXIT_FILE_ACCESS = 5
EXIT_ALREADY_DONE_OCR = 6
EXIT_CHILD_PROCESS = 7
EXIT_ENCRYPTED_PDF = 8
EXIT_INVALID_CONFIG = 9
EXIT_PDFA_FAILED = 10
EXIT_OTHER = 15
EXIT_CTRL_C = 130
EXIT_TIMEOUT = -1  # nosso, não do OCRmyPDF

#: Não adianta tentar de novo: o problema é do ambiente ou da entrada.
FATAL_CODES = frozenset(
    {
        EXIT_BAD_ARGS,
        EXIT_INPUT_FILE,
        EXIT_MISSING_DEPENDENCY,
        EXIT_FILE_ACCESS,
        EXIT_ENCRYPTED_PDF,
        EXIT_INVALID_CONFIG,
        EXIT_CTRL_C,
        EXIT_TIMEOUT,
    }
)

#: A conversão de OCR foi bem, o empacotamento PDF/A é que não. Vale repetir o
#: mesmo modo sem otimização e tolerando erro leve de renderização.
DEGRADABLE_CODES = frozenset({EXIT_INVALID_OUTPUT_PDF, EXIT_PDFA_FAILED})

MODE_FLAG = {
    "skip": "--skip-text",
    "redo": "--redo-ocr",
    "force": "--force-ocr",
}


@dataclass(frozen=True, slots=True)
class Attempt:
    """Uma invocação concreta do OCRmyPDF."""

    mode: str
    rationale: str
    extra: tuple[str, ...] = ()
    degraded: bool = False

    @property
    def label(self) -> str:
        return f"{self.mode}+degradado" if self.degraded else self.mode

    def degrade(self) -> Attempt:
        """Mesma estratégia de OCR, empacotamento mais tolerante."""
        return Attempt(
            mode=self.mode,
            rationale="repetição sem otimização, tolerando erro leve de renderização",
            extra=(*self.extra, "--optimize", "0", "--continue-on-soft-render-error"),
            degraded=True,
        )


@dataclass(frozen=True, slots=True)
class Plan:
    """O que fazer com um documento."""

    decision: str  # convert | skip | reject
    reason: str
    nature: str = "indeterminado"
    attempts: tuple[Attempt, ...] = field(default_factory=tuple)

    @property
    def actionable(self) -> bool:
        return self.decision == "convert"


def plan(profile: DocumentProfile, settings: Settings) -> Plan:
    """Decide o destino do documento a partir do perfil medido."""
    nature = profile.nature(settings.text_threshold)

    if profile.encrypted:
        return Plan(
            "reject",
            "protegido por senha de usuário",
            nature,
        )
    if profile.damaged:
        return Plan("reject", profile.error or "PDF ilegível", nature)
    if profile.pages == 0:
        return Plan("reject", "documento sem páginas", nature)

    if profile.signed and settings.on_signed == "skip":
        return Plan(
            "skip",
            "assinatura digital presente — converter a invalidaria",
            nature,
        )

    if settings.skip_if_pdfa and profile.pdfa_part:
        wanted = settings.pdfa_part.removeprefix("pdfa-")
        if profile.pdfa_part.startswith(wanted):
            return Plan("skip", f"já é PDF/A-{profile.pdfa_part}", nature)

    return Plan(
        "convert",
        _reason_for(nature, profile, settings),
        nature,
        _ladder(
            nature,
            is_image=profile.is_image,
            residue=profile.pages_with_residue(settings.text_threshold),
        ),
    )


def _reason_for(nature: str, profile: DocumentProfile, settings: Settings) -> str:
    if profile.is_image:
        return f"imagem {profile.path.suffix.lstrip('.').upper()} — OCR e empacotamento em PDF/A"
    with_text = profile.pages_with_text(settings.text_threshold)
    scanned = len(profile.page_text_chars)
    if nature == "digitalizado":
        return f"nenhuma das {scanned} páginas tem camada de texto"
    if nature.startswith("nativo"):
        return f"texto vetorial em {with_text}/{scanned} páginas — só empacotamento PDF/A"
    if nature == "misto":
        return f"{scanned - with_text} de {scanned} páginas sem texto"
    return "natureza indeterminada"


def _ladder(nature: str, *, is_image: bool = False, residue: int = 0) -> tuple[Attempt, ...]:
    if is_image:
        return (Attempt("skip", "entrada bitmap: OCR direto, sem texto a preservar"),)
    return _pdf_ladder(nature, residue=residue)


def _pdf_ladder(nature: str, *, residue: int = 0) -> tuple[Attempt, ...]:
    """Ordem de tentativas, da mais preservadora para a mais destrutiva.

    ``force`` fica sempre por último: ele rasteriza a página inteira e é a única
    opção que pode *piorar* um documento. Só se chega nele quando as anteriores
    falharam de fato.
    """
    if nature == "digitalizado":
        if residue:
            # Páginas com carimbo ou número de folha seriam puladas por
            # --skip-text, que decide por presença de texto, não por quantidade.
            # --redo-ocr enxerga a página como imagem com um fragmento de texto e
            # reconhece o resto.
            return (
                Attempt(
                    "redo",
                    f"{residue} página(s) com texto residual que --skip-text descartaria",
                ),
                Attempt("skip", "redo-ocr rejeitou o documento"),
                Attempt("force", "último recurso: rasterizar e reconhecer tudo"),
            )
        return (
            Attempt("skip", "OCR em todas as páginas; nenhuma tem texto a preservar"),
            Attempt("force", "camada de texto residual bloqueou o OCR; rasterizar"),
        )
    if nature.startswith("nativo"):
        return (
            Attempt("skip", "texto já existe; converter para PDF/A sem OCR"),
            Attempt("redo", "empacotamento falhou; reconstruir a camada de texto"),
        )
    if nature == "misto":
        return (
            Attempt("redo", "preserva o texto existente e OCRiza apenas as imagens"),
            Attempt("skip", "redo-ocr rejeitou o documento; OCR só nas páginas vazias"),
            Attempt("force", "último recurso: rasterizar e reconhecer tudo"),
        )
    return (
        Attempt("redo", "perfil indeterminado; caminho preservador primeiro"),
        Attempt("skip", "redo-ocr rejeitou o documento"),
        Attempt("force", "último recurso: rasterizar e reconhecer tudo"),
    )


def classify_exit(code: int) -> str:
    """``fatal`` | ``degrade`` | ``escalate`` | ``ok``."""
    if code == EXIT_OK:
        return "ok"
    if code in FATAL_CODES:
        return "fatal"
    if code in DEGRADABLE_CODES:
        return "degrade"
    return "escalate"


def explain_exit(code: int) -> str:
    return {
        EXIT_OK: "sucesso",
        EXIT_BAD_ARGS: "argumentos inválidos (erro do Scriptor, reporte)",
        EXIT_INPUT_FILE: "arquivo de entrada inválido ou ilegível",
        EXIT_MISSING_DEPENDENCY: "dependência externa ausente",
        EXIT_INVALID_OUTPUT_PDF: "o PDF gerado não passou na verificação",
        EXIT_FILE_ACCESS: "acesso negado a arquivo ou pasta",
        EXIT_ALREADY_DONE_OCR: "o documento já contém OCR",
        EXIT_CHILD_PROCESS: "falha em processo filho (Tesseract ou Ghostscript)",
        EXIT_ENCRYPTED_PDF: "PDF criptografado",
        EXIT_INVALID_CONFIG: "configuração inválida",
        EXIT_PDFA_FAILED: "conversão para PDF/A falhou no Ghostscript",
        EXIT_OTHER: "erro não classificado",
        EXIT_CTRL_C: "interrompido pelo operador",
        EXIT_TIMEOUT: "tempo limite excedido; processo encerrado",
    }.get(code, f"código de saída {code}")


def build_command(
    attempt: Attempt,
    *,
    source: Path,
    destination: Path,
    settings: Settings,
    profile: DocumentProfile,
    jobs: int,
    sidecar: Path | None = None,
) -> list[str]:
    """Monta o argv do OCRmyPDF.

    Executado como ``python -m ocrmypdf`` no mesmo interpretador: dispensa
    procurar o console script no PATH e garante que a versão usada é a mesma que
    o Scriptor inspecionou.
    """
    command: list[str] = [sys.executable, "-m", "ocrmypdf"]

    command += [MODE_FLAG[attempt.mode]]
    command += ["--output-type", settings.pdfa_part]
    command += ["-l", "+".join(settings.languages)]
    command += ["--jobs", str(max(1, jobs))]
    command += ["--optimize", str(settings.optimize)]
    command += ["--tesseract-timeout", str(settings.tesseract_timeout)]

    # --jbig2-lossy é deliberadamente omitido: a compressão JBIG2 com perda
    # substitui glifos visualmente parecidos por um único símbolo — o defeito que
    # trocou dígitos em digitalizações da Xerox. Inaceitável em acervo.

    if settings.deskew:
        command.append("--deskew")
    if settings.rotate_pages:
        command.append("--rotate-pages")
    if settings.clean:
        command.append("--clean")
    if profile.is_image:
        # Muitos scanners gravam TIFF sem resolução declarada; sem isto o
        # OCRmyPDF recusa a entrada em vez de adivinhar.
        command += ["--image-dpi", str(settings.image_dpi)]
    if profile.signed and settings.on_signed == "invalidate":
        command.append("--invalidate-digital-signatures")
    if sidecar is not None:
        command += ["--sidecar", str(sidecar)]

    command += list(attempt.extra)
    command += [str(source), str(destination)]
    return command
