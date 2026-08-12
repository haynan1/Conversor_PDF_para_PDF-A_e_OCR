"""Perfilagem do documento antes de decidir o que fazer com ele.

O kit original aplicava a mesma flag a todo arquivo. Um lote real de digitalização
mistura pelo menos quatro naturezas de documento, e cada uma exige tratamento
diferente:

* digitalização pura — nenhuma camada de texto, precisa de OCR completo;
* nativo digital — texto vetorial íntegro, OCR só degradaria;
* misto — capa escaneada anexada a um relatório digital;
* já OCRizado — camada de texto invisível, de qualidade desconhecida.

Distinguir os quatro casos custa dezenas de milissegundos por arquivo e é a
diferença entre um acervo pesquisável e um acervo com buracos silenciosos.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import pikepdf
from pikepdf import Pdf

#: Operadores que efetivamente desenham texto na página.
_TEXT_OPERATORS = {"Tj", "TJ", "'", '"'}

#: Teto de páginas inspecionadas. Acima disso, amostramos uniformemente: a
#: proporção de páginas com texto converge muito antes de 300 amostras, e um
#: documento de 5.000 páginas não deve pagar a varredura inteira.
MAX_PAGES_SCANNED = 300

_PDFA_PART_RE = re.compile(rb"pdfaid[:/]part['\"\s>]*[=>]?\s*['\"]?(\d)")
_PDFA_CONF_RE = re.compile(rb"pdfaid[:/]conformance['\"\s>]*[=>]?\s*['\"]?([ABU])", re.IGNORECASE)


@dataclass(slots=True)
class DocumentProfile:
    """Tudo que se sabe sobre o documento sem ainda tê-lo modificado."""

    path: Path
    sha256: str
    size_bytes: int
    pages: int = 0
    encrypted: bool = False
    """Protegido por senha de usuário — não conseguimos sequer abrir."""
    restricted: bool = False
    """Abre sem senha, mas tem restrições de permissão. Removível com segurança."""
    signed: bool = False
    pdfa_part: str | None = None
    damaged: bool = False
    error: str | None = None
    page_text_chars: list[int] = field(default_factory=list)
    page_images: list[int] = field(default_factory=list)
    sampled: bool = False
    is_image: bool = False
    """Entrada bitmap (TIFF, JPEG…) em vez de PDF. Sempre precisa de OCR."""

    # ------------------------------------------------------------ derivados --

    def pages_with_text(self, threshold: int) -> int:
        return sum(1 for count in self.page_text_chars if count >= threshold)

    def pages_with_residue(self, threshold: int) -> int:
        """Páginas com algum texto, porém abaixo do limiar.

        São as páginas que fazem ``--skip-text`` perder conteúdo: um carimbo, um
        número de folha ou uma marca d'água bastam para o OCRmyPDF considerar a
        página "já processada" e devolvê-la intacta, sem uma linha reconhecida.
        """
        return sum(1 for count in self.page_text_chars if 0 < count < threshold)

    def text_ratio(self, threshold: int) -> float:
        if not self.page_text_chars:
            return 0.0
        return self.pages_with_text(threshold) / len(self.page_text_chars)

    def has_images(self) -> bool:
        return any(self.page_images)

    def nature(self, threshold: int) -> str:
        """Classificação legível, usada na decisão de estratégia e no relatório."""
        if not self.page_text_chars:
            return "indeterminado"
        ratio = self.text_ratio(threshold)
        if ratio == 0:
            return "digitalizado"
        if ratio >= 0.995:
            return "nativo" if not self.has_images() else "nativo-com-imagens"
        return "misto"

    def summary(self, threshold: int) -> str:
        return (
            f"{self.pages} pág · {self.nature(threshold)} · "
            f"{self.pages_with_text(threshold)} com texto"
        )


def sha256_file(path: Path, *, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _count_text_chars(operands) -> int:
    """Aproxima a quantidade de caracteres desenhados por uma instrução de texto.

    Fontes CID usam dois bytes por glifo, então isto superestima em documentos
    CJK — irrelevante: o número só é comparado a um limiar de resíduo.
    """
    total = 0
    for operand in operands:
        if isinstance(operand, pikepdf.String):
            total += len(bytes(operand))
        elif isinstance(operand, pikepdf.Array):
            for item in operand:
                if isinstance(item, pikepdf.String):
                    total += len(bytes(item))
    return total


def _scan_page(page: pikepdf.Page, *, depth: int = 0) -> tuple[int, int]:
    """Devolve ``(caracteres_de_texto, imagens)`` da página.

    Desce um nível em Form XObjects: alguns digitalizadores encapsulam todo o
    conteúdo num formulário, e ignorá-los faria o documento parecer vazio.
    """
    text_chars = 0
    images = 0

    try:
        instructions = pikepdf.parse_content_stream(page)
    except (pikepdf.PdfError, ValueError, TypeError):
        return 0, 0

    for instruction in instructions:
        operator = str(instruction.operator)
        if operator in _TEXT_OPERATORS:
            text_chars += _count_text_chars(instruction.operands)
        elif operator == "INLINE IMAGE":
            images += 1

    try:
        resources = page.obj.get("/Resources")
        xobjects = resources.get("/XObject") if resources is not None else None
    except (pikepdf.PdfError, AttributeError):
        xobjects = None

    if xobjects is not None:
        for value in xobjects.values():
            try:
                subtype = str(value.get("/Subtype", ""))
            except (pikepdf.PdfError, AttributeError):
                continue
            if subtype == "/Image":
                images += 1
            elif subtype == "/Form" and depth == 0:
                nested_text, nested_images = _scan_page(pikepdf.Page(value), depth=1)
                text_chars += nested_text
                images += nested_images

    return text_chars, images


def _detect_signature(pdf: Pdf) -> bool:
    """Assinatura digital presente? Conservador: na dúvida, responde sim."""
    try:
        root = pdf.Root
    except pikepdf.PdfError:
        return False

    acroform = root.get("/AcroForm")
    if acroform is not None:
        try:
            if int(acroform.get("/SigFlags", 0)) & 1:
                return True
        except (TypeError, ValueError, pikepdf.PdfError):
            pass
        fields = acroform.get("/Fields")
        if fields is not None:
            for item in fields:
                try:
                    if str(item.get("/FT", "")) == "/Sig" and item.get("/V") is not None:
                        return True
                except (pikepdf.PdfError, AttributeError):
                    continue

    perms = root.get("/Perms")
    if perms is not None and len(perms.keys()) > 0:
        return True

    for page in pdf.pages:
        annots = page.obj.get("/Annots")
        if annots is None:
            continue
        for annot in annots:
            try:
                if str(annot.get("/FT", "")) == "/Sig":
                    return True
            except (pikepdf.PdfError, AttributeError):
                continue
    return False


def _detect_pdfa(pdf: Pdf) -> str | None:
    """Lê ``pdfaid:part`` do XMP direto do stream, sem tocar nos metadados.

    ``Pdf.open_metadata()`` reescreve o XMP ao sair do contexto; para uma sonda
    somente-leitura isso é efeito colateral indesejado.
    """
    try:
        metadata = pdf.Root.get("/Metadata")
        if metadata is None:
            return None
        raw = bytes(metadata.read_bytes())
    except (pikepdf.PdfError, AttributeError, TypeError):
        return None

    part = _PDFA_PART_RE.search(raw)
    if not part:
        return None
    conformance = _PDFA_CONF_RE.search(raw)
    suffix = conformance.group(1).decode().upper() if conformance else ""
    return f"{part.group(1).decode()}{suffix.lower()}"


def _clean_error(exc: Exception, path: Path) -> str:
    """Remove o caminho absoluto que o pikepdf prefixa às mensagens.

    O nome do arquivo já aparece na linha do relatório; repeti-lo por extenso
    empurra a causa real para fora da tela.
    """
    message = str(exc)
    for prefix in (f"{path}: ", f"{path.name}: "):
        if message.startswith(prefix):
            return message[len(prefix) :]
    return message


def _sample_indices(total: int, cap: int) -> tuple[list[int], bool]:
    if total <= cap:
        return list(range(total)), False
    step = total / cap
    return [min(total - 1, int(i * step)) for i in range(cap)], True


#: Formatos bitmap aceitos como entrada. O OCRmyPDF os embrulha em PDF antes de
#: reconhecer — capacidade que o script em lote original simplesmente não tinha,
#: apesar de o TIFF multipágina ser a saída padrão de scanner departamental.
IMAGE_SUFFIXES = frozenset({".tif", ".tiff", ".png", ".jpg", ".jpeg", ".bmp", ".webp"})
PDF_SUFFIXES = frozenset({".pdf"})
INPUT_SUFFIXES = IMAGE_SUFFIXES | PDF_SUFFIXES


def analyze(path: Path, *, max_pages: int = MAX_PAGES_SCANNED) -> DocumentProfile:
    """Perfila o documento. Nunca levanta exceção: falha vira campo no perfil."""
    path = Path(path)
    try:
        size = path.stat().st_size
        digest = sha256_file(path)
    except OSError as exc:
        return DocumentProfile(
            path=path, sha256="", size_bytes=0, damaged=True, error=f"leitura falhou: {exc}"
        )

    if path.suffix.lower() in IMAGE_SUFFIXES:
        # Bitmap não tem estrutura a inspecionar: por definição não há camada de
        # texto, e a contagem de páginas só se conhece após a conversão.
        return DocumentProfile(
            path=path,
            sha256=digest,
            size_bytes=size,
            pages=1,
            is_image=True,
            page_text_chars=[0],
            page_images=[1],
        )

    profile = DocumentProfile(path=path, sha256=digest, size_bytes=size)

    try:
        pdf = Pdf.open(path)
    except pikepdf.PasswordError:
        profile.encrypted = True
        profile.error = "protegido por senha"
        return profile
    except (pikepdf.PdfError, OSError, RuntimeError) as exc:
        profile.damaged = True
        profile.error = f"PDF ilegível: {_clean_error(exc, path)}"
        return profile

    with pdf:
        profile.restricted = bool(pdf.is_encrypted)
        try:
            profile.pages = len(pdf.pages)
        except pikepdf.PdfError as exc:
            profile.damaged = True
            profile.error = f"árvore de páginas corrompida: {exc}"
            return profile

        profile.signed = _detect_signature(pdf)
        profile.pdfa_part = _detect_pdfa(pdf)

        indices, sampled = _sample_indices(profile.pages, max_pages)
        profile.sampled = sampled
        for index in indices:
            try:
                text_chars, images = _scan_page(pdf.pages[index])
            except (pikepdf.PdfError, IndexError, RuntimeError):
                text_chars, images = 0, 0
            profile.page_text_chars.append(text_chars)
            profile.page_images.append(images)

    return profile
