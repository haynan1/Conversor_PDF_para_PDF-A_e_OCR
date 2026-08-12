"""Geração de documentos de teste que se parecem com os reais.

Um PDF "escaneado" sintético só vale se o Tesseract conseguir de fato ler o
texto. Por isso o caminho é indireto: gera-se um PDF com texto vetorial de
verdade, rasteriza-se com o Ghostscript e reembute-se o bitmap resultante. O
produto final é indistinguível de uma digitalização — e traz texto que o OCR
consegue recuperar, o que permite verificar o resultado, não só o exit code.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pikepdf
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

PAGE_WIDTH, PAGE_HEIGHT = A4

LOREM = [
    "RELATORIO DE PRESTACAO DE CONTAS",
    "",
    "O presente documento consolida os lancamentos do periodo e",
    "acompanha a documentacao fiscal correspondente, na forma",
    "prevista pelo regulamento interno da entidade.",
    "",
    "Total apurado no exercicio: R$ 148.320,00",
    "Responsavel tecnico: Departamento de Controladoria",
]


def make_text_pdf(path: Path, *, pages: int = 1, title: str = "Documento") -> Path:
    """PDF nativo digital: texto vetorial, fontes embutidas pelo reportlab."""
    pdf = canvas.Canvas(str(path), pagesize=A4)
    pdf.setTitle(title)
    for page in range(pages):
        pdf.setFont("Helvetica-Bold", 16)
        pdf.drawString(25 * mm, PAGE_HEIGHT - 30 * mm, f"{title} — pagina {page + 1}")
        pdf.setFont("Helvetica", 12)
        y = PAGE_HEIGHT - 45 * mm
        for line in LOREM:
            pdf.drawString(25 * mm, y, line)
            y -= 7 * mm
        pdf.showPage()
    pdf.save()
    return path


def make_blank_pdf(path: Path, *, pages: int = 1) -> Path:
    """Páginas com um retângulo e nenhum texto — simula folha em branco escaneada."""
    pdf = canvas.Canvas(str(path), pagesize=A4)
    for _ in range(pages):
        pdf.rect(20 * mm, 20 * mm, 60 * mm, 40 * mm, stroke=1, fill=0)
        pdf.showPage()
    pdf.save()
    return path


def rasterize(source: Path, target_dir: Path, *, ghostscript: str, dpi: int = 200) -> list[Path]:
    """Converte um PDF em PNGs, uma imagem por página."""
    target_dir.mkdir(parents=True, exist_ok=True)
    pattern = target_dir / "pagina-%02d.png"
    subprocess.run(
        [
            ghostscript,
            "-dQUIET",
            "-dNOPAUSE",
            "-dBATCH",
            "-sDEVICE=png16m",
            f"-r{dpi}",
            f"-sOutputFile={pattern}",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    return sorted(target_dir.glob("pagina-*.png"))


def make_scanned_pdf(
    path: Path,
    images: list[Path],
    *,
    stamp: str | None = None,
) -> Path:
    """PDF de digitalização: cada página é um bitmap ocupando a folha inteira.

    ``stamp`` acrescenta um texto curto sobre a primeira página. É exatamente o
    caso que o ``--skip-text`` do script original tratava mal: alguns caracteres
    de texto bastavam para a página inteira ser excluída do OCR.
    """
    pdf = canvas.Canvas(str(path), pagesize=A4)
    for index, image in enumerate(images):
        pdf.drawImage(str(image), 0, 0, width=PAGE_WIDTH, height=PAGE_HEIGHT)
        if stamp and index == 0:
            pdf.setFont("Helvetica", 8)
            pdf.drawString(15 * mm, 10 * mm, stamp)
        pdf.showPage()
    pdf.save()
    return path


def make_mixed_pdf(path: Path, scanned_images: list[Path], *, text_pages: int = 1) -> Path:
    """Capa digitalizada seguida de páginas nativas — o lote real típico."""
    pdf = canvas.Canvas(str(path), pagesize=A4)
    for image in scanned_images:
        pdf.drawImage(str(image), 0, 0, width=PAGE_WIDTH, height=PAGE_HEIGHT)
        pdf.showPage()
    for page in range(text_pages):
        pdf.setFont("Helvetica", 12)
        y = PAGE_HEIGHT - 40 * mm
        for line in LOREM:
            pdf.drawString(25 * mm, y, f"{line}")
            y -= 7 * mm
        pdf.drawString(25 * mm, y - 10 * mm, f"Anexo digital {page + 1}")
        pdf.showPage()
    pdf.save()
    return path


def mark_as_signed(path: Path) -> Path:
    """Marca o PDF como assinado, no nível estrutural que o Scriptor inspeciona.

    Uma assinatura criptográfica real exigiria certificado; o que importa para o
    teste é o ``/SigFlags`` que sinaliza a presença dela.
    """
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.Root[pikepdf.Name.AcroForm] = pdf.make_indirect(
            pikepdf.Dictionary(
                SigFlags=3,
                Fields=pikepdf.Array(
                    [
                        pdf.make_indirect(
                            pikepdf.Dictionary(
                                FT=pikepdf.Name.Sig,
                                T=pikepdf.String("Assinatura1"),
                                V=pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Sig)),
                            )
                        )
                    ]
                ),
            )
        )
        pdf.save()
    return path


def make_encrypted_pdf(path: Path, source: Path, *, password: str = "segredo") -> Path:  # noqa: S107
    with pikepdf.open(source) as pdf:
        pdf.save(path, encryption=pikepdf.Encryption(user=password, owner=password))
    return path


def make_restricted_pdf(path: Path, source: Path) -> Path:
    """Sem senha de usuário, apenas com permissões restritas — abre normalmente."""
    with pikepdf.open(source) as pdf:
        pdf.save(
            path,
            encryption=pikepdf.Encryption(
                user="", owner="dono", allow=pikepdf.Permissions(extract=False)
            ),
        )
    return path


def make_damaged_pdf(path: Path) -> Path:
    path.write_bytes(b"%PDF-1.7\nisto nao e um PDF valido\n%%EOF\n")
    return path


def make_image(path: Path, source_image: Path) -> Path:
    """Copia um PNG rasterizado como entrada bitmap direta."""
    path.write_bytes(source_image.read_bytes())
    return path


if __name__ == "__main__":  # geração manual para inspeção
    from scriptor.toolchain import find_ghostscript

    out = Path(sys.argv[1] if len(sys.argv) > 1 else "amostras").resolve()
    out.mkdir(parents=True, exist_ok=True)
    gs = str(find_ghostscript().path)

    text = make_text_pdf(out / "nativo.pdf", pages=2, title="Contrato")
    images = rasterize(text, out / "_png", ghostscript=gs)
    make_scanned_pdf(out / "escaneado.pdf", images)
    make_scanned_pdf(out / "escaneado-com-carimbo.pdf", images, stamp="Fls. 12")
    make_mixed_pdf(out / "misto.pdf", images[:1])
    mark_as_signed(make_text_pdf(out / "assinado.pdf", title="Ata"))
    print(f"amostras em {out}")
