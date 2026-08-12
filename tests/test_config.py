"""Configuração: precedência, portabilidade e invalidação de cache."""

from __future__ import annotations

from pathlib import Path

import pytest

from scriptor import config as config_module
from scriptor.config import CONFIG_FILENAME, Settings
from scriptor.errors import ConfigError


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / CONFIG_FILENAME
    path.write_text(body, encoding="utf-8")
    return path


def test_caminhos_relativos_ancoram_no_arquivo_de_configuracao(tmp_path: Path) -> None:
    """A portabilidade do workspace depende disto: nada de caminho absoluto."""
    path = _write(tmp_path, '[scriptor]\ninput_dir = "docs/entrada"\n')
    settings = config_module.load(path)
    assert settings.input_dir == tmp_path / "docs" / "entrada"
    assert settings.workspace == tmp_path.resolve()


def test_padroes_espelham_a_nomenclatura_do_kit_antigo(tmp_path: Path) -> None:
    settings = config_module.load(_write(tmp_path, "[scriptor]\n"))
    assert settings.input_dir.name == "Entrada"
    assert settings.output_dir.name == "Saida"
    assert settings.archive_dir.name == "Processados"


def test_chave_desconhecida_e_erro_e_nao_silencio(tmp_path: Path) -> None:
    """Erro de digitação em configuração é a pior classe de falha: silenciosa."""
    path = _write(tmp_path, '[scriptor]\nlanguagem = "por"\n')
    with pytest.raises(ConfigError, match="desconhecida"):
        config_module.load(path)


def test_toml_invalido_aponta_o_arquivo(tmp_path: Path) -> None:
    path = _write(tmp_path, "[scriptor]\nlanguages = [por]\n")
    with pytest.raises(ConfigError, match="TOML"):
        config_module.load(path)


def test_variavel_de_ambiente_sobrescreve_o_arquivo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path, '[scriptor]\nlanguages = ["por"]\n')
    monkeypatch.setenv("SCRIPTOR_LANGUAGES", "por+eng")
    assert config_module.load(path).languages == ("por", "eng")


def test_flag_sobrescreve_a_variavel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write(tmp_path, "[scriptor]\n")
    monkeypatch.setenv("SCRIPTOR_OPTIMIZE", "3")
    settings = config_module.load(path).with_overrides(optimize=1)
    assert settings.optimize == 1


def test_entrada_igual_a_saida_e_rejeitado(tmp_path: Path) -> None:
    """Reprocessar a própria saída degrada o acervo a cada execução."""
    path = _write(tmp_path, '[scriptor]\ninput_dir = "X"\noutput_dir = "X"\n')
    with pytest.raises(ConfigError, match="mesma pasta"):
        config_module.load(path).validate()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("pdfa_part", "pdfa-9"),
        ("optimize", 7),
        ("on_success", "incinerar"),
        ("on_signed", "talvez"),
        ("languages", ()),
    ],
)
def test_valores_invalidos_falham_na_validacao(tmp_path: Path, field: str, value: object) -> None:
    settings = config_module.load(_write(tmp_path, "[scriptor]\n"))
    with pytest.raises(ConfigError):
        settings.with_overrides(**{field: value}).validate()


def test_receita_ignora_o_que_nao_muda_o_conteudo(settings: Settings, tmp_path: Path) -> None:
    """Mover a pasta de saída não pode invalidar o trabalho já feito."""
    base = settings.recipe_hash("t=1")
    assert settings.with_overrides(output_dir=tmp_path / "outra").recipe_hash("t=1") == base
    assert settings.with_overrides(concurrency=7).recipe_hash("t=1") == base
    assert settings.with_overrides(on_success="delete").recipe_hash("t=1") == base


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("languages", ("eng",)),
        ("pdfa_part", "pdfa-1"),
        ("optimize", 0),
        ("text_threshold", 5),
        ("deskew", True),
    ],
)
def test_receita_muda_quando_o_conteudo_muda(settings: Settings, field: str, value: object) -> None:
    base = settings.recipe_hash("t=1")
    assert settings.with_overrides(**{field: value}).recipe_hash("t=1") != base


def test_atualizar_o_tesseract_invalida_a_receita(settings: Settings) -> None:
    """Versão nova reconhece diferente; o resultado anterior não é mais o mesmo."""
    assert settings.recipe_hash("tesseract=5.3.0") != settings.recipe_hash("tesseract=5.5.0")


def test_pastas_gerenciadas_ficam_fora_da_varredura(settings: Settings) -> None:
    managed = {p.name for p in settings.managed_dirs()}
    assert {"Saida", "Processados", "Falhas", "_scriptor"} <= managed
