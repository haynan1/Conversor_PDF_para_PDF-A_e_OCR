"""Escada de recuperação, limite de tempo e atomicidade.

Estes são os caminhos que só aparecem quando algo dá errado — e por isso mesmo
nunca são exercitados pelo lote que funciona. Em vez de provocar falhas reais no
Tesseract, os códigos de saída são roteirizados: o que se testa aqui é a decisão
do Scriptor diante de cada código, não o comportamento do OCRmyPDF.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from scriptor import runner as runner_module
from scriptor.config import Settings
from scriptor.conformance import ConformanceReport
from scriptor.ledger import Ledger
from scriptor.runner import Job, Runner
from scriptor.strategy import (
    EXIT_INVALID_OUTPUT_PDF,
    EXIT_MISSING_DEPENDENCY,
    EXIT_OK,
    EXIT_OTHER,
    EXIT_PDFA_FAILED,
    EXIT_TIMEOUT,
)
from scriptor.toolchain import Tool, Toolchain

APROVADO = ConformanceReport(True, "interno", declared="2")
REPROVADO = ConformanceReport(False, "interno", problems=["sem OutputIntent"])


@pytest.fixture
def toolchain_falso(tmp_path: Path) -> Toolchain:
    """Cadeia de ferramentas sintética: nenhum binário é invocado nestes testes."""
    tessdata = tmp_path / "tessdata"
    tessdata.mkdir()
    (tessdata / "por.traineddata").write_bytes(b"")
    binario = tmp_path / "falso.exe"
    binario.write_bytes(b"")
    return Toolchain(
        tesseract=Tool("Tesseract", binario, "tesseract v5.5.0", "teste"),
        ghostscript=Tool("Ghostscript", binario, "10.05.0", "teste"),
        languages=frozenset({"por"}),
        tessdata_dir=tessdata,
        tessdata_origin="teste",
    )


class RunnerRoteirizado(Runner):
    """Runner cujas execuções devolvem códigos de saída pré-definidos."""

    def __init__(self, *args, codes: list[int], modelo: Path, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.codes = list(codes)
        self.modelo = modelo
        self.comandos: list[list[str]] = []

    def _execute(self, command: list[str]) -> tuple[int, str]:
        self.comandos.append(command)
        code = self.codes.pop(0) if self.codes else EXIT_OTHER
        if code == EXIT_OK:
            # O OCRmyPDF teria escrito o destino temporário; imitamos isso para
            # que a publicação atômica tenha o que renomear.
            shutil.copy2(self.modelo, Path(command[-1]))
        return code, f"[simulado] código {code}\n"

    @property
    def modos(self) -> list[str]:
        """Sequência de modos tentados, na ordem."""
        flags = {"--skip-text": "skip", "--redo-ocr": "redo", "--force-ocr": "force"}
        return [flags[c] for cmd in self.comandos for c in cmd if c in flags]


def _job(settings: Settings, origem: Path) -> Job:
    destino = settings.input_dir / origem.name
    shutil.copy2(origem, destino)
    return Job(source=destino, relative=Path(origem.name))


def _runner(settings, toolchain, samples, codes, monkeypatch, conformance=APROVADO):
    monkeypatch.setattr(runner_module, "verify", lambda *a, **k: conformance)
    return RunnerRoteirizado(settings, toolchain, None, codes=codes, modelo=samples["nativo"])


# ------------------------------------------------------------------ escada --


def test_sucesso_na_primeira_tentativa_nao_escala(
    settings: Settings, toolchain_falso, samples, monkeypatch
) -> None:
    runner = _runner(settings, toolchain_falso, samples, [EXIT_OK], monkeypatch)
    resultado = runner.process(_job(settings, samples["misto"]))

    assert resultado.status == "ok"
    assert runner.modos == ["redo"]
    assert len(resultado.attempts) == 1


def test_falha_recuperavel_sobe_um_degrau(
    settings: Settings, toolchain_falso, samples, monkeypatch
) -> None:
    """Documento misto: redo falha, skip resolve. force não deve ser alcançado."""
    runner = _runner(settings, toolchain_falso, samples, [EXIT_OTHER, EXIT_OK], monkeypatch)
    resultado = runner.process(_job(settings, samples["misto"]))

    assert resultado.status == "ok"
    assert runner.modos == ["redo", "skip"]
    assert resultado.mode == "skip"


def test_force_e_o_ultimo_recurso(
    settings: Settings, toolchain_falso, samples, monkeypatch
) -> None:
    runner = _runner(
        settings, toolchain_falso, samples, [EXIT_OTHER, EXIT_OTHER, EXIT_OK], monkeypatch
    )
    resultado = runner.process(_job(settings, samples["misto"]))

    assert runner.modos == ["redo", "skip", "force"]
    assert resultado.status == "ok"


def test_falha_de_empacotamento_repete_o_mesmo_modo_degradado(
    settings: Settings, toolchain_falso, samples, monkeypatch
) -> None:
    """Exit 10 é do Ghostscript, não do OCR: insistir no mesmo modo é o certo."""
    runner = _runner(settings, toolchain_falso, samples, [EXIT_PDFA_FAILED, EXIT_OK], monkeypatch)
    resultado = runner.process(_job(settings, samples["misto"]))

    assert runner.modos == ["redo", "redo"]
    assert resultado.mode == "redo+degradado"
    assert "--continue-on-soft-render-error" in runner.comandos[1]
    assert runner.comandos[1][-3:-2] == ["0"] or "0" in runner.comandos[1]


def test_degradacao_acontece_uma_vez_por_degrau(
    settings: Settings, toolchain_falso, samples, monkeypatch
) -> None:
    """Sem isto, exit 10 repetido geraria degradações infinitas."""
    runner = _runner(
        settings,
        toolchain_falso,
        samples,
        [EXIT_PDFA_FAILED, EXIT_PDFA_FAILED, EXIT_PDFA_FAILED, EXIT_PDFA_FAILED],
        monkeypatch,
    )
    resultado = runner.process(_job(settings, samples["misto"]))

    assert resultado.status == "failed"
    # três degraus × no máximo uma degradação cada
    assert len(runner.comandos) <= 6
    assert runner.modos.count("redo") == 2


def test_erro_fatal_aborta_sem_escalar(
    settings: Settings, toolchain_falso, samples, monkeypatch
) -> None:
    """Dependência ausente não melhora tentando outro modo de OCR."""
    runner = _runner(settings, toolchain_falso, samples, [EXIT_MISSING_DEPENDENCY], monkeypatch)
    resultado = runner.process(_job(settings, samples["misto"]))

    assert resultado.status == "failed"
    assert len(runner.comandos) == 1
    assert "dependência" in resultado.detail


def test_tempo_limite_e_fatal(settings: Settings, toolchain_falso, samples, monkeypatch) -> None:
    runner = _runner(settings, toolchain_falso, samples, [EXIT_TIMEOUT], monkeypatch)
    resultado = runner.process(_job(settings, samples["misto"]))

    assert resultado.status == "failed"
    assert len(runner.comandos) == 1
    assert "limite" in resultado.detail


# ------------------------------------------------------------ conformidade --


def test_saida_nao_conforme_alimenta_a_escada(
    settings: Settings, toolchain_falso, samples, monkeypatch
) -> None:
    """Reprovação na verificação não pode virar entrega.

    O OCRmyPDF devolve 0, mas o arquivo não é PDF/A válido. Tratar isso como
    sucesso seria exatamente o que o kit antigo fazia — prometer conformidade
    sem conferir.
    """
    # Códigos suficientes para toda a escada: 3 degraus × até uma degradação.
    runner = _runner(
        settings, toolchain_falso, samples, [EXIT_OK] * 8, monkeypatch, conformance=REPROVADO
    )
    resultado = runner.process(_job(settings, samples["misto"]))

    assert resultado.status == "failed"
    assert len(runner.comandos) > 1, "reprovação não escalou para a próxima estratégia"
    # O OCRmyPDF devolveu 0 em todas; quem reprovou foi a verificação.
    assert all(a.exit_code == EXIT_INVALID_OUTPUT_PDF for a in resultado.attempts)
    assert resultado.conformance is not None and not resultado.conformance.ok
    assert not (settings.output_dir / "misto.pdf").exists()
    assert list(settings.output_dir.rglob("*")) == []


# ------------------------------------------------------------ atomicidade --


def test_falha_nao_deixa_temporario_na_saida(
    settings: Settings, toolchain_falso, samples, monkeypatch
) -> None:
    runner = _runner(settings, toolchain_falso, samples, [EXIT_OTHER] * 5, monkeypatch)
    runner.process(_job(settings, samples["misto"]))

    assert list(settings.output_dir.rglob("*")) == []


def test_falha_nao_publica_saida_parcial(
    settings: Settings, toolchain_falso, samples, monkeypatch
) -> None:
    """O destino final só existe se a conversão inteira deu certo."""
    runner = _runner(settings, toolchain_falso, samples, [EXIT_OTHER] * 5, monkeypatch)
    runner.process(_job(settings, samples["misto"]))

    assert not (settings.output_dir / "misto.pdf").exists()
    assert (settings.failed_dir / "misto.pdf").is_file()


def test_original_so_e_arquivado_apos_a_verificacao(
    settings: Settings, toolchain_falso, samples, monkeypatch
) -> None:
    job = _job(settings, samples["misto"])
    runner = _runner(settings, toolchain_falso, samples, [EXIT_OK], monkeypatch)
    runner.process(job)

    assert not job.source.exists()
    assert (settings.archive_dir / "misto.pdf").is_file()
    assert (settings.output_dir / "misto.pdf").is_file()


def test_politica_keep_preserva_o_original_onde_esta(
    settings: Settings, toolchain_falso, samples, monkeypatch
) -> None:
    guardado = settings.with_overrides(on_success="keep")
    job = _job(guardado, samples["misto"])
    runner = _runner(guardado, toolchain_falso, samples, [EXIT_OK], monkeypatch)
    runner.process(job)

    assert job.source.is_file()
    assert not (guardado.archive_dir / "misto.pdf").exists()


# ------------------------------------------------------------ cancelamento --


def test_cancelamento_impede_novas_tentativas(
    settings: Settings, toolchain_falso, samples, monkeypatch
) -> None:
    runner = _runner(settings, toolchain_falso, samples, [EXIT_OK], monkeypatch)
    runner.cancel()
    resultado = runner.process(_job(settings, samples["misto"]))

    assert resultado.status == "failed"
    assert runner.comandos == []
    assert "interrompido" in resultado.detail


# ------------------------------------------------------------------ ledger --


def test_falha_e_registrada_no_ledger_com_o_motivo(
    settings: Settings, toolchain_falso, samples, monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runner_module, "verify", lambda *a, **k: APROVADO)
    with Ledger(tmp_path / "l.sqlite3") as ledger:
        runner = RunnerRoteirizado(
            settings,
            toolchain_falso,
            ledger,
            codes=[EXIT_MISSING_DEPENDENCY],
            modelo=samples["nativo"],
        )
        runner.process(_job(settings, samples["misto"]))
        registros = ledger.recent(5)

    assert registros[0].status == "failed"
    assert registros[0].detail


def test_log_do_documento_registra_cada_tentativa(
    settings: Settings, toolchain_falso, samples, monkeypatch
) -> None:
    runner = _runner(settings, toolchain_falso, samples, [EXIT_OTHER, EXIT_OK], monkeypatch)
    resultado = runner.process(_job(settings, samples["misto"]))

    conteudo = resultado.log_path.read_text(encoding="utf-8")
    assert conteudo.count("$ [") == 2
    assert "--redo-ocr" in conteudo
    assert "--skip-text" in conteudo


# --------------------------------------------------------------- exceções --


def test_erro_interno_nao_derruba_o_documento(
    settings: Settings, toolchain_falso, samples, monkeypatch
) -> None:
    """Um bug do Scriptor vira falha daquele documento, não do lote inteiro."""
    monkeypatch.setattr(
        runner_module, "analyze", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bug"))
    )
    runner = _runner(settings, toolchain_falso, samples, [EXIT_OK], monkeypatch)
    resultado = runner.process(_job(settings, samples["misto"]))

    assert resultado.status == "failed"
    assert "RuntimeError" in resultado.detail
