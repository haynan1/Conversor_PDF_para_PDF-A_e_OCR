"""Renderização no terminal.

Todo lote bem-sucedido passa por estas funções depois que o trabalho já foi
feito. Uma exceção aqui destrói a experiência exatamente no momento em que não
há mais nada a corrigir — a conversão terminou, mas o operador vê um traceback.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scriptor import ui
from scriptor.analysis import DocumentProfile
from scriptor.conformance import ConformanceReport
from scriptor.console import console
from scriptor.runner import AttemptLog, Job, Outcome
from scriptor.toolchain import Tool, Toolchain

TODOS_OS_STATUS = ("ok", "cached", "skipped", "rejected", "failed")


@pytest.fixture
def cadeia(tmp_path: Path) -> Toolchain:
    binario = tmp_path / "falso.exe"
    binario.write_bytes(b"")
    return Toolchain(
        tesseract=Tool("Tesseract", binario, "tesseract v5.5.0.20241111", "registro"),
        ghostscript=Tool("Ghostscript", binario, "10.05.0", "PATH"),
        languages=frozenset({"por", "eng"}),
        tessdata_dir=tmp_path,
        tessdata_origin="sistema",
        notes=["tessdata do sistema é somente leitura; espelho em …"],
    )


def _outcome(status: str, nome: str = "contrato-2019.pdf") -> Outcome:
    profile = DocumentProfile(
        path=Path(nome),
        sha256="a" * 64,
        size_bytes=84_000,
        pages=18,
        page_text_chars=[0] * 18,
        page_images=[1] * 18,
    )
    return Outcome(
        job=Job(source=Path(nome), relative=Path(nome)),
        status=status,
        detail="nenhuma das 18 páginas tem camada de texto",
        profile=profile,
        mode="redo" if status == "ok" else None,
        attempts=[AttemptLog("redo", "…", 0, 4.2)],
        conformance=ConformanceReport(True, "interno", declared="2") if status == "ok" else None,
        duration=4.2,
        output_bytes=96_000 if status == "ok" else None,
        log_path=Path("_scriptor/logs/contrato.log") if status == "failed" else None,
    )


@pytest.mark.parametrize("status", TODOS_OS_STATUS)
def test_toda_linha_de_resultado_renderiza(status: str) -> None:
    vista = ui.RunView(total=5, enabled=False)
    with console.capture() as capturado:
        console.print(vista._line(_outcome(status)))
    assert "contrato-2019.pdf" in capturado.get()


def test_nome_longo_e_encurtado_sem_colidir_com_a_coluna_seguinte() -> None:
    nome = "2024/janeiro/processo-administrativo-numero-muito-longo.pdf"
    vista = ui.RunView(total=1, enabled=False)
    with console.capture() as capturado:
        console.print(vista._line(_outcome("ok", nome)))
    saida = capturado.get()
    assert "18 pág" in saida
    # O nome é elidido, e sobra separação antes da coluna de modo.
    assert "redo" in saida


def test_cabecalho_mostra_receita_e_motor(cadeia: Toolchain, tmp_path: Path) -> None:
    with console.capture() as capturado:
        ui.header(
            input_dir=tmp_path / "Entrada",
            output_dir=tmp_path / "Saida",
            languages=("por", "eng"),
            profile="pdfa-2",
            toolchain=cadeia,
            total=12,
            total_bytes=88_000_000,
            workers=8,
            jobs=4,
            dry_run=False,
        )
    saida = capturado.get()
    assert "por+eng" in saida
    assert "PDF/A-2" in saida
    assert "8×4" in saida
    assert "5.5.0.20241111" in saida
    # Observações da cadeia de ferramentas precisam chegar ao operador.
    assert "somente leitura" in saida


def test_cabecalho_marca_a_simulacao(cadeia: Toolchain, tmp_path: Path) -> None:
    with console.capture() as capturado:
        ui.header(
            input_dir=tmp_path,
            output_dir=tmp_path / "s",
            languages=("por",),
            profile="pdfa-2",
            toolchain=cadeia,
            total=1,
            total_bytes=1,
            workers=1,
            jobs=1,
            dry_run=True,
        )
    assert "simulação" in capturado.get()


def test_resumo_contabiliza_cada_desfecho() -> None:
    outcomes = [_outcome(status, f"doc-{status}.pdf") for status in TODOS_OS_STATUS]
    with console.capture() as capturado:
        ui.summary(
            outcomes,
            elapsed=42.0,
            report_path=Path("_scriptor/relatorios/2026-08-12.json"),
            verify_enabled=True,
        )
    saida = capturado.get()
    assert "5 documentos" in saida
    assert "convertido" in saida and "falhou" in saida
    assert "conforme" in saida
    assert "pendências" in saida
    assert "relatorios" in saida


def test_resumo_de_lote_vazio_nao_estoura() -> None:
    with console.capture() as capturado:
        ui.summary([], elapsed=0.0, report_path=None, verify_enabled=True)
    assert "0 documentos" in capturado.get()


def test_resumo_sem_verificacao_omite_a_conformidade() -> None:
    with console.capture() as capturado:
        ui.summary([_outcome("ok")], elapsed=1.0, report_path=None, verify_enabled=False)
    assert "conforme" not in capturado.get()


def test_progresso_avanca_e_estima_o_restante() -> None:
    vista = ui.RunView(total=10, enabled=False)
    vista.done = 4
    with console.capture() as capturado:
        console.print(vista._progress(vista._started + 20))
    saida = capturado.get()
    assert "4/10" in saida
    assert "restam" in saida


def test_rodape_vivo_lista_o_que_esta_em_execucao() -> None:
    vista = ui.RunView(total=3, enabled=False)
    vista.begin(Job(source=Path("a.pdf"), relative=Path("a.pdf")))
    with console.capture() as capturado:
        console.print(vista._render())
    assert "a.pdf" in capturado.get()


def test_observacoes_do_documento_aparecem_na_linha() -> None:
    outcome = _outcome("ok")
    outcome.notes = ["não foi possível arquivar o original: acesso negado"]
    vista = ui.RunView(total=1, enabled=False)
    with console.capture() as capturado:
        console.print(vista._line(outcome))
    assert "acesso negado" in capturado.get()
