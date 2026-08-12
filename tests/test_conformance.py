"""Verificação de conformidade PDF/A."""

from __future__ import annotations

from pathlib import Path

from scriptor.conformance import verify


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
