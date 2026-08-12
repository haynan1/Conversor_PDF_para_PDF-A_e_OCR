"""A perfilagem é a base de toda decisão: se ela erra, o resto erra junto."""

from __future__ import annotations

from pathlib import Path

from scriptor.analysis import analyze, sha256_file

THRESHOLD = 40


def test_nativo_tem_texto_em_todas_as_paginas(samples: dict[str, Path]) -> None:
    profile = analyze(samples["nativo"])
    assert profile.pages == 2
    assert profile.pages_with_text(THRESHOLD) == 2
    assert profile.nature(THRESHOLD) == "nativo"
    assert not profile.signed
    assert profile.pdfa_part is None


def test_escaneado_nao_tem_camada_de_texto(samples: dict[str, Path]) -> None:
    profile = analyze(samples["escaneado"])
    assert profile.pages_with_text(THRESHOLD) == 0
    assert profile.has_images()
    assert profile.nature(THRESHOLD) == "digitalizado"


def test_carimbo_conta_como_residuo_e_nao_como_texto(samples: dict[str, Path]) -> None:
    """O caso que o --skip-text do kit original tratava mal.

    Sete caracteres de carimbo não fazem da página um documento com texto — mas
    bastam para o OCRmyPDF pular a página inteira se a decisão for por presença.
    """
    profile = analyze(samples["carimbado"])
    assert profile.pages_with_text(THRESHOLD) == 0
    assert profile.pages_with_residue(THRESHOLD) == 1
    assert profile.nature(THRESHOLD) == "digitalizado"


def test_misto_e_reconhecido_como_misto(samples: dict[str, Path]) -> None:
    profile = analyze(samples["misto"])
    assert profile.nature(THRESHOLD) == "misto"
    assert 0 < profile.text_ratio(THRESHOLD) < 1


def test_assinatura_digital_e_detectada(samples: dict[str, Path]) -> None:
    assert analyze(samples["assinado"]).signed is True


def test_pdf_com_senha_de_usuario_nao_abre(samples: dict[str, Path]) -> None:
    profile = analyze(samples["protegido"])
    assert profile.encrypted is True
    assert profile.pages == 0


def test_pdf_apenas_com_permissoes_restritas_abre(samples: dict[str, Path]) -> None:
    """Restrição de permissão não impede a leitura — e não deve bloquear o OCR."""
    profile = analyze(samples["restrito"])
    assert profile.encrypted is False
    assert profile.restricted is True
    assert profile.pages == 2


def test_pdf_corrompido_vira_campo_no_perfil_e_nao_excecao(samples: dict[str, Path]) -> None:
    profile = analyze(samples["corrompido"])
    assert profile.damaged is True
    assert profile.error
    assert str(samples["corrompido"]) not in profile.error


def test_imagem_e_perfilada_sem_pikepdf(samples: dict[str, Path]) -> None:
    profile = analyze(samples["imagem"])
    assert profile.is_image is True
    assert profile.pages == 1
    assert profile.nature(THRESHOLD) == "digitalizado"


def test_pagina_em_branco_nao_conta_como_texto(samples: dict[str, Path]) -> None:
    profile = analyze(samples["branco"])
    assert profile.pages_with_text(THRESHOLD) == 0


def test_sha256_e_estavel_e_confere_com_o_perfil(samples: dict[str, Path]) -> None:
    path = samples["nativo"]
    assert analyze(path).sha256 == sha256_file(path)


def test_arquivo_inexistente_nao_levanta(tmp_path: Path) -> None:
    profile = analyze(tmp_path / "ausente.pdf")
    assert profile.damaged is True
