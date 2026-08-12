"""Verificação de conformidade PDF/A."""

from __future__ import annotations

from pathlib import Path

import pikepdf

from scriptor.conformance import ConformanceReport, verify


def test_pdf_comum_nao_passa_como_pdfa(samples: dict[str, Path]) -> None:
    """Um PDF válido não é um PDF/A. O kit antigo tratava os dois como a mesma coisa."""
    report = verify(samples["nativo"], expected="pdfa-2")
    assert not report.ok
    assert any("pdfaid" in problem for problem in report.problems)
    assert any("OutputIntent" in problem for problem in report.problems)


def test_perfil_pdf_exige_apenas_legibilidade(samples: dict[str, Path]) -> None:
    assert verify(samples["nativo"], expected="pdf").ok


def test_arquivo_corrompido_reprova(samples: dict[str, Path]) -> None:
    report = verify(samples["corrompido"], expected="pdfa-2")
    assert not report.ok
    assert "ilegível" in report.detail()


def test_arquivo_criptografado_reprova(samples: dict[str, Path]) -> None:
    report = verify(samples["protegido"], expected="pdfa-2")
    assert not report.ok


def test_reprovacao_interna_dispensa_o_verapdf(samples: dict[str, Path], monkeypatch) -> None:
    """Chamar o validador externo sobre algo já reprovado só custa tempo."""
    from scriptor import conformance

    chamado = False

    def _nunca(*args, **kwargs):  # pragma: no cover - deve permanecer não chamado
        nonlocal chamado
        chamado = True
        return None

    monkeypatch.setattr(conformance, "_verify_verapdf", _nunca)
    fake_tool = object()
    verify(samples["nativo"], expected="pdfa-2", verapdf=fake_tool)  # type: ignore[arg-type]
    assert not chamado


def test_validador_falha_fechado_quando_a_fonte_e_ilegivel(
    samples: dict[str, Path], monkeypatch
) -> None:
    """Regressão: exceção ao inspecionar fonte não pode virar aprovação.

    A versão anterior envolvia a inspeção num `suppress(Exception)`. Qualquer
    falha pulava o registro da fonte — e o arquivo passava como conforme
    justamente nos casos mais estranhos, que são os que importam.
    """
    from scriptor import conformance

    def explode(_font):
        raise RuntimeError("estrutura de fonte inesperada")

    monkeypatch.setattr(conformance, "_font_is_embedded", explode)

    with pikepdf.open(samples["nativo"]) as pdf:
        problemas = conformance._fonts_not_embedded(pdf)

    assert problemas, "fonte ilegível foi tratada como embutida"
    assert any("ilegível" in item for item in problemas)


def test_fontes_embutidas_nao_geram_problema(samples: dict[str, Path]) -> None:
    """Contraprova: sem exceção, uma fonte de fato embutida não é reportada."""
    from scriptor import conformance

    monkeypatch_free = conformance._font_is_embedded
    assert callable(monkeypatch_free)
    with pikepdf.open(samples["nativo"]) as pdf:
        # O reportlab referencia Helvetica sem embutir: deve ser reportada,
        # e pelo nome — não como erro de leitura.
        problemas = conformance._fonts_not_embedded(pdf)
    assert not any("ilegível" in item for item in problemas)


# ---------------------------------------------------------------- veraPDF --

_VERAPDF_REPROVA = b"""{"report": {"jobs": [{"validationResult": {
  "compliant": false,
  "details": {"ruleSummaries": [
    {"clause": "6.1.7", "description": "Fonte nao embutida", "failedChecks": 3},
    {"clause": "6.2.2", "description": "Espaco de cor sem OutputIntent", "failedChecks": 1}
  ]}
}}]}}"""

_VERAPDF_APROVA = b'{"report": {"jobs": [{"validationResult": {"compliant": true}}]}}'


class _Processo:
    def __init__(self, stdout: bytes, returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = b""
        self.returncode = returncode


def _tool(tmp_path: Path):
    from scriptor.toolchain import Tool

    binario = tmp_path / "verapdf.bat"
    binario.write_text("", encoding="utf-8")
    return Tool("veraPDF", binario, "1.26", "teste")


def test_verapdf_reprovando_traz_as_clausulas(tmp_path: Path, monkeypatch) -> None:
    from scriptor import conformance

    monkeypatch.setattr(
        conformance.subprocess, "run", lambda *a, **k: _Processo(_VERAPDF_REPROVA, 1)
    )
    relatorio = conformance._verify_verapdf(
        tmp_path / "x.pdf", expected="pdfa-2", tool=_tool(tmp_path), timeout=10
    )

    assert relatorio is not None
    assert not relatorio.ok
    assert relatorio.validator == "veraPDF"
    assert any("6.1.7" in p for p in relatorio.problems)


def test_verapdf_aprovando(tmp_path: Path, monkeypatch) -> None:
    from scriptor import conformance

    monkeypatch.setattr(conformance.subprocess, "run", lambda *a, **k: _Processo(_VERAPDF_APROVA))
    relatorio = conformance._verify_verapdf(
        tmp_path / "x.pdf", expected="pdfa-1", tool=_tool(tmp_path), timeout=10
    )

    assert relatorio is not None and relatorio.ok
    assert relatorio.declared == "1"


def test_verapdf_com_saida_ilegivel_cai_no_codigo_de_saida(tmp_path: Path, monkeypatch) -> None:
    from scriptor import conformance

    monkeypatch.setattr(conformance.subprocess, "run", lambda *a, **k: _Processo(b"nao e json", 1))
    relatorio = conformance._verify_verapdf(
        tmp_path / "x.pdf", expected="pdfa-2", tool=_tool(tmp_path), timeout=10
    )
    assert relatorio is not None and not relatorio.ok


def test_verapdf_que_nao_executa_devolve_none(tmp_path: Path, monkeypatch) -> None:
    """Validador quebrado não pode virar reprovação — nem aprovação."""
    from scriptor import conformance

    def explode(*_a, **_k):
        raise OSError("não pôde iniciar")

    monkeypatch.setattr(conformance.subprocess, "run", explode)
    assert (
        conformance._verify_verapdf(
            tmp_path / "x.pdf", expected="pdfa-2", tool=_tool(tmp_path), timeout=10
        )
        is None
    )


def test_verapdf_indisponivel_cai_na_checagem_interna(
    samples: dict[str, Path], tmp_path: Path, monkeypatch
) -> None:
    from scriptor import conformance

    monkeypatch.setattr(conformance, "_verify_verapdf", lambda *a, **k: None)
    monkeypatch.setattr(
        conformance, "_verify_internal", lambda *a, **k: ConformanceReport(True, "interno")
    )

    relatorio = verify(samples["nativo"], expected="pdfa-2", verapdf=_tool(tmp_path))
    assert relatorio.ok
    assert "indisponível" in relatorio.validator
