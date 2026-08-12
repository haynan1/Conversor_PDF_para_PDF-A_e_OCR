"""Execução real do pipeline, com OCR de verdade.

Estes testes chamam Tesseract e Ghostscript. São lentos por natureza e ficam sob
a marca ``slow`` — mas são os únicos que provam que o conjunto funciona: análise,
estratégia, conversão, verificação, arquivamento e ledger, na ordem certa.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pikepdf
import pytest

from scriptor.config import Settings
from scriptor.ledger import Ledger
from scriptor.pipeline import run
from scriptor.toolchain import Toolchain

pytestmark = pytest.mark.slow


def _text_chars(path: Path) -> list[int]:
    """Caracteres de texto por página do arquivo produzido."""
    totals: list[int] = []
    with pikepdf.open(path) as pdf:
        for page in pdf.pages:
            count = 0
            for instruction in pikepdf.parse_content_stream(page):
                if str(instruction.operator) not in {"Tj", "TJ", "'", '"'}:
                    continue
                for operand in instruction.operands:
                    if isinstance(operand, pikepdf.String):
                        count += len(bytes(operand))
                    elif isinstance(operand, pikepdf.Array):
                        count += sum(
                            len(bytes(item)) for item in operand if isinstance(item, pikepdf.String)
                        )
            totals.append(count)
    return totals


@pytest.fixture(scope="module")
def executado(
    tmp_path_factory: pytest.TempPathFactory,
    samples: dict[str, Path],
    toolchain: Toolchain,
) -> tuple[Settings, object]:
    """Uma execução completa, compartilhada pelas asserções do módulo."""
    from scriptor import config as config_module

    root = tmp_path_factory.mktemp("e2e")
    (root / "scriptor.toml").write_text('[scriptor]\nlanguages = ["por"]\n', encoding="utf-8")
    settings = config_module.load(root / "scriptor.toml")
    settings.ensure_dirs()

    for key in ("nativo", "escaneado", "carimbado", "misto", "assinado", "corrompido"):
        shutil.copy2(samples[key], settings.input_dir / samples[key].name)
    nested = settings.input_dir / "2024"
    nested.mkdir()
    shutil.copy2(samples["imagem"], nested / samples["imagem"].name)

    with Ledger(settings.ledger_path) as ledger:
        report = run(settings, toolchain, ledger=ledger)
    return settings, report


def _by_name(report) -> dict[str, object]:
    return {outcome.job.source.name: outcome for outcome in report.outcomes}


def test_cada_natureza_recebe_o_desfecho_certo(executado) -> None:
    _, report = executado
    outcomes = _by_name(report)

    assert outcomes["nativo.pdf"].status == "ok"
    assert outcomes["escaneado.pdf"].status == "ok"
    assert outcomes["carimbado.pdf"].status == "ok"
    assert outcomes["misto.pdf"].status == "ok"
    assert outcomes["digitalizacao.png"].status == "ok"
    assert outcomes["assinado.pdf"].status == "skipped"
    assert outcomes["corrompido.pdf"].status == "rejected"


def test_pagina_carimbada_e_realmente_reconhecida(executado) -> None:
    """O defeito central do kit original, verificado no arquivo produzido.

    Com ``--skip-text``, a página 1 sairia com os poucos caracteres do carimbo.
    A conversão só está correta se ela contiver o texto inteiro da digitalização.
    """
    settings, _ = executado
    paginas = _text_chars(settings.output_dir / "carimbado.pdf")
    assert paginas[0] > 200, "a página carimbada não foi OCRizada"


def test_digitalizacao_pura_ganha_camada_de_texto(executado) -> None:
    settings, _ = executado
    assert all(count > 200 for count in _text_chars(settings.output_dir / "escaneado.pdf"))


def test_saida_declara_conformidade_pdfa(executado) -> None:
    _, report = executado
    convertidos = [o for o in report.outcomes if o.status == "ok"]
    assert convertidos
    for outcome in convertidos:
        assert outcome.conformance is not None
        assert outcome.conformance.ok, f"{outcome.job.name}: {outcome.conformance.detail()}"


def test_hierarquia_de_pastas_e_preservada(executado) -> None:
    settings, _ = executado
    assert (settings.output_dir / "2024" / "digitalizacao.pdf").is_file()


def test_original_convertido_vai_para_processados(executado) -> None:
    settings, _ = executado
    assert (settings.archive_dir / "nativo.pdf").is_file()
    assert not (settings.input_dir / "nativo.pdf").exists()


def test_original_recusado_vai_para_falhas(executado) -> None:
    settings, _ = executado
    assert (settings.failed_dir / "corrompido.pdf").is_file()


def test_documento_assinado_permanece_intocado(executado) -> None:
    """Pular significa não mover, não converter, não arriscar."""
    settings, _ = executado
    assert (settings.input_dir / "assinado.pdf").is_file()
    assert not (settings.output_dir / "assinado.pdf").exists()


def test_nenhum_arquivo_temporario_sobra(executado) -> None:
    settings, _ = executado
    restos = list(settings.output_dir.rglob(".*scriptor-*"))
    assert restos == []


def test_relatorio_json_registra_a_receita_completa(executado) -> None:
    _, report = executado
    assert report.report_path is not None
    payload = json.loads(report.report_path.read_text(encoding="utf-8"))
    assert payload["ferramentas"]["tesseract"]
    assert payload["receita"]["languages"] == ["por"]
    assert len(payload["documentos"]) == len(report.outcomes)


def test_log_por_documento_guarda_o_comando_executado(executado) -> None:
    settings, _ = executado
    log = settings.log_dir / "carimbado.pdf.log"
    conteudo = log.read_text(encoding="utf-8")
    assert "--redo-ocr" in conteudo
    assert "conformidade" in conteudo


def test_segunda_execucao_nao_reprocessa_nada(executado, toolchain: Toolchain) -> None:
    """Idempotência: o ledger reconhece origem e receita idênticas."""
    settings, _ = executado
    # O que sobrou na entrada foi apenas o documento assinado; devolvemos os
    # originais arquivados — na mesma posição relativa, porque um documento
    # movido de pasta tem outro destino e deve mesmo ser regerado.
    for arquivo in settings.archive_dir.rglob("*"):
        if not arquivo.is_file():
            continue
        destino = settings.input_dir / arquivo.relative_to(settings.archive_dir)
        destino.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(arquivo, destino)

    with Ledger(settings.ledger_path) as ledger:
        segunda = run(settings, toolchain, ledger=ledger)

    reconvertidos = [o for o in segunda.outcomes if o.status == "ok"]
    assert reconvertidos == [], "documentos idênticos foram reprocessados"
    assert any(o.status == "cached" for o in segunda.outcomes)


def test_receita_diferente_reprocessa(executado, toolchain: Toolchain) -> None:
    settings, _ = executado
    shutil.copy2(settings.archive_dir / "nativo.pdf", settings.input_dir / "nativo.pdf")

    alterado = settings.with_overrides(pdfa_part="pdfa-1")
    with Ledger(settings.ledger_path) as ledger:
        report = run(alterado, toolchain, ledger=ledger)

    nativo = _by_name(report).get("nativo.pdf")
    assert nativo is not None
    assert nativo.status == "ok"


def test_simulacao_nao_escreve_nada(
    settings: Settings, samples: dict[str, Path], toolchain: Toolchain
) -> None:
    shutil.copy2(samples["escaneado"], settings.input_dir / "escaneado.pdf")
    report = run(settings.with_overrides(dry_run=True), toolchain)

    assert all(o.status == "skipped" for o in report.outcomes)
    assert list(settings.output_dir.iterdir()) == []
    assert (settings.input_dir / "escaneado.pdf").is_file()


def test_documento_em_cache_sai_da_entrada(executado, toolchain: Toolchain) -> None:
    """Regressão: o que já está convertido não pode ficar preso na entrada.

    A versão anterior devolvia 'cached' sem aplicar a política de originais.
    O arquivo era redescoberto e reidentificado como cache a cada execução, e a
    fila nunca esvaziava — na interface, um documento que o operador não
    conseguiria remover a não ser à mão.
    """
    settings, _ = executado
    origem = settings.input_dir / "nativo.pdf"
    shutil.copy2(settings.archive_dir / "nativo.pdf", origem)

    with Ledger(settings.ledger_path) as ledger:
        report = run(settings, toolchain, ledger=ledger)

    resultado = _by_name(report)["nativo.pdf"]
    assert resultado.status == "cached"
    assert not origem.exists(), "documento em cache continuou na pasta de entrada"
