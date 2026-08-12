"""Estratégia por documento e escada de recuperação."""

from __future__ import annotations

from pathlib import Path

import pytest

from scriptor.analysis import analyze
from scriptor.config import Settings
from scriptor.strategy import (
    EXIT_ENCRYPTED_PDF,
    EXIT_MISSING_DEPENDENCY,
    EXIT_OK,
    EXIT_OTHER,
    EXIT_PDFA_FAILED,
    EXIT_TIMEOUT,
    Attempt,
    build_command,
    classify_exit,
    plan,
)


def _modes(decision) -> list[str]:
    return [attempt.mode for attempt in decision.attempts]


def test_digitalizado_puro_comeca_por_skip(samples: dict[str, Path], settings: Settings) -> None:
    decision = plan(analyze(samples["escaneado"]), settings)
    assert decision.decision == "convert"
    assert _modes(decision)[0] == "skip"


def test_carimbo_muda_a_estrategia_para_redo(samples: dict[str, Path], settings: Settings) -> None:
    """Regressão do defeito central do kit original.

    Uma página com carimbo seria devolvida intacta por --skip-text. A escada
    precisa começar por --redo-ocr para que o texto da digitalização seja lido.
    """
    decision = plan(analyze(samples["carimbado"]), settings)
    assert _modes(decision)[0] == "redo"
    assert "residual" in decision.attempts[0].rationale


def test_misto_preserva_o_texto_existente(samples: dict[str, Path], settings: Settings) -> None:
    decision = plan(analyze(samples["misto"]), settings)
    assert _modes(decision)[0] == "redo"


def test_nativo_apenas_empacota(samples: dict[str, Path], settings: Settings) -> None:
    decision = plan(analyze(samples["nativo"]), settings)
    assert _modes(decision)[0] == "skip"
    assert "PDF/A" in decision.reason


def test_force_e_sempre_o_ultimo_degrau(samples: dict[str, Path], settings: Settings) -> None:
    """force rasteriza a página; é a única opção que pode piorar o documento."""
    for key in ("escaneado", "carimbado", "misto"):
        modes = _modes(plan(analyze(samples[key]), settings))
        if "force" in modes:
            assert modes[-1] == "force"


def test_assinado_e_pulado_por_padrao(samples: dict[str, Path], settings: Settings) -> None:
    decision = plan(analyze(samples["assinado"]), settings)
    assert decision.decision == "skip"
    assert "assinatura" in decision.reason


def test_assinado_e_convertido_quando_o_operador_assume(
    samples: dict[str, Path], settings: Settings
) -> None:
    decision = plan(analyze(samples["assinado"]), settings.with_overrides(on_signed="invalidate"))
    assert decision.decision == "convert"


def test_protegido_e_recusado(samples: dict[str, Path], settings: Settings) -> None:
    decision = plan(analyze(samples["protegido"]), settings)
    assert decision.decision == "reject"


def test_corrompido_e_recusado(samples: dict[str, Path], settings: Settings) -> None:
    assert plan(analyze(samples["corrompido"]), settings).decision == "reject"


def test_imagem_tem_escada_de_um_degrau(samples: dict[str, Path], settings: Settings) -> None:
    decision = plan(analyze(samples["imagem"]), settings)
    assert _modes(decision) == ["skip"]


def test_limiar_de_texto_altera_a_classificacao(
    samples: dict[str, Path], settings: Settings
) -> None:
    """Um limiar absurdamente alto faz até o nativo parecer digitalizado."""
    profile = analyze(samples["nativo"])
    assert plan(profile, settings).nature == "nativo"
    assert plan(profile, settings.with_overrides(text_threshold=100_000)).nature == "digitalizado"


@pytest.mark.parametrize(
    ("code", "verdict"),
    [
        (EXIT_OK, "ok"),
        (EXIT_MISSING_DEPENDENCY, "fatal"),
        (EXIT_ENCRYPTED_PDF, "fatal"),
        (EXIT_TIMEOUT, "fatal"),
        (EXIT_PDFA_FAILED, "degrade"),
        (EXIT_OTHER, "escalate"),
    ],
)
def test_classificacao_de_codigos_de_saida(code: int, verdict: str) -> None:
    assert classify_exit(code) == verdict


def test_degradacao_desliga_a_otimizacao() -> None:
    degraded = Attempt("redo", "x").degrade()
    assert degraded.degraded
    assert "--optimize" in degraded.extra
    assert "0" in degraded.extra
    assert "--continue-on-soft-render-error" in degraded.extra


def test_comando_traz_idioma_perfil_e_modo(
    samples: dict[str, Path], settings: Settings, tmp_path: Path
) -> None:
    profile = analyze(samples["escaneado"])
    command = build_command(
        Attempt("redo", "x"),
        source=samples["escaneado"],
        destination=tmp_path / "saida.pdf",
        settings=settings.with_overrides(languages=("por", "eng")),
        profile=profile,
        jobs=3,
    )
    assert "--redo-ocr" in command
    assert command[command.index("-l") + 1] == "por+eng"
    assert command[command.index("--output-type") + 1] == "pdfa-2"
    assert command[command.index("--jobs") + 1] == "3"
    # Nunca: JBIG2 com perda troca glifos parecidos entre si.
    assert "--jbig2-lossy" not in command


def test_comando_de_imagem_declara_dpi(
    samples: dict[str, Path], settings: Settings, tmp_path: Path
) -> None:
    command = build_command(
        Attempt("skip", "x"),
        source=samples["imagem"],
        destination=tmp_path / "saida.pdf",
        settings=settings,
        profile=analyze(samples["imagem"]),
        jobs=1,
    )
    assert "--image-dpi" in command


def test_documento_assinado_so_recebe_a_flag_com_autorizacao(
    samples: dict[str, Path], settings: Settings, tmp_path: Path
) -> None:
    profile = analyze(samples["assinado"])
    padrao = build_command(
        Attempt("skip", "x"),
        source=samples["assinado"],
        destination=tmp_path / "a.pdf",
        settings=settings,
        profile=profile,
        jobs=1,
    )
    assert "--invalidate-digital-signatures" not in padrao

    autorizado = build_command(
        Attempt("skip", "x"),
        source=samples["assinado"],
        destination=tmp_path / "a.pdf",
        settings=settings.with_overrides(on_signed="invalidate"),
        profile=profile,
        jobs=1,
    )
    assert "--invalidate-digital-signatures" in autorizado
