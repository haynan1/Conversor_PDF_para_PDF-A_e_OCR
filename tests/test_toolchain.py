"""Descoberta de binários e provisionamento de idioma.

Duas apostilas acompanhavam o kit original — "Resolver problema do Path no
Tesseract OCR" e "Instalar o Idioma Português no Tesseract". Este módulo é o que
as substitui, e por isso precisa funcionar exatamente nos cenários que elas
descreviam: binário fora do PATH e ``Program Files`` sem permissão de escrita.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from scriptor import toolchain as tc
from scriptor.errors import LanguageError, ToolchainError
from scriptor.toolchain import Tool, Toolchain

# Um executável de verdade que responde a --version, sem depender do Tesseract.
INTERPRETADOR = Path(sys.executable)


# ------------------------------------------------------------- descoberta --


def test_caminho_configurado_tem_precedencia() -> None:
    achado = tc._resolve_tool(
        "Falso",
        (INTERPRETADOR.stem,),
        override=INTERPRETADOR,
        registry_keys=(),
        glob_patterns=(),
        remedy="",
    )
    assert achado is not None
    assert achado.path == INTERPRETADOR.resolve()
    assert achado.origin == "config"


def test_diretorio_configurado_tambem_e_aceito() -> None:
    """Apontar a pasta de instalação é o que o operador tende a fazer."""
    achado = tc._resolve_tool(
        "Falso",
        (INTERPRETADOR.stem,),
        override=INTERPRETADOR.parent,
        registry_keys=(),
        glob_patterns=(),
        remedy="",
    )
    assert achado is not None
    assert achado.path == INTERPRETADOR.resolve()


def test_caminho_configurado_invalido_falha_com_remedio(tmp_path: Path) -> None:
    with pytest.raises(ToolchainError) as erro:
        tc._resolve_tool(
            "Falso",
            ("nada",),
            override=tmp_path / "inexistente.exe",
            registry_keys=(),
            glob_patterns=(),
            remedy="Instale com instaladores/…",
        )
    assert "instaladores" in erro.value.remedy


def test_ferramenta_ausente_e_opcional_devolve_none() -> None:
    assert (
        tc._resolve_tool(
            "veraPDF",
            ("binario-que-nao-existe-em-lugar-nenhum",),
            override=None,
            registry_keys=(),
            glob_patterns=(),
            remedy="",
            required=False,
        )
        is None
    )


def test_ferramenta_obrigatoria_ausente_levanta_com_remedio() -> None:
    with pytest.raises(ToolchainError) as erro:
        tc._resolve_tool(
            "Tesseract",
            ("binario-que-nao-existe-em-lugar-nenhum",),
            override=None,
            registry_keys=(),
            glob_patterns=(),
            remedy="Instale o Tesseract",
        )
    assert erro.value.remedy


def test_versao_e_convertida_em_tupla_comparavel() -> None:
    assert Tool("t", Path("x"), "tesseract v5.5.0.20241111", "teste").version_tuple[:2] == (5, 5)
    assert Tool("g", Path("x"), "10.05.0", "teste").version_tuple == (10, 5, 0)
    assert Tool("?", Path("x"), "sem número", "teste").version_tuple == ()


# ------------------------------------------------------------- ambiente --


def _cadeia(tmp_path: Path) -> Toolchain:
    tessdata = tmp_path / "tessdata"
    tessdata.mkdir(exist_ok=True)
    return Toolchain(
        tesseract=Tool("Tesseract", tmp_path / "bin" / "tesseract.exe", "5.5.0", "registro"),
        ghostscript=Tool("Ghostscript", tmp_path / "gs" / "gswin64c.exe", "10.05.0", "registro"),
        languages=frozenset({"por"}),
        tessdata_dir=tessdata,
        tessdata_origin="sistema",
    )


def test_ambiente_amplia_o_path_sem_tocar_no_processo(tmp_path: Path) -> None:
    """A descoberta não pode depender de alterar o PATH do Windows do usuário."""
    original = dict(os.environ)
    ambiente = _cadeia(tmp_path).env()

    assert str(tmp_path / "bin") in ambiente["PATH"]
    assert str(tmp_path / "gs") in ambiente["PATH"]
    assert os.environ == original, "os.environ do processo foi mutado"


def test_ambiente_fixa_o_tessdata_prefix(tmp_path: Path) -> None:
    ambiente = _cadeia(tmp_path).env()
    assert ambiente["TESSDATA_PREFIX"] == str(tmp_path / "tessdata")


def test_ambiente_limita_as_threads_do_tesseract(tmp_path: Path) -> None:
    """A concorrência é do Scriptor; o Tesseract não deve abrir a sua por cima."""
    assert _cadeia(tmp_path).env()["OMP_THREAD_LIMIT"] == "1"


def test_impressao_digital_muda_com_a_versao(tmp_path: Path) -> None:
    cadeia = _cadeia(tmp_path)
    antes = cadeia.fingerprint()
    cadeia.tesseract = Tool("Tesseract", Path("x"), "5.3.0", "teste")
    assert cadeia.fingerprint() != antes


# ------------------------------------------------------------- idiomas --


@pytest.fixture
def sistema(tmp_path: Path) -> tuple[Tool, Path]:
    """Instalação simulada do Tesseract, com inglês e os configs."""
    raiz = tmp_path / "Tesseract-OCR"
    tessdata = raiz / "tessdata"
    (tessdata / "configs").mkdir(parents=True)
    (tessdata / "eng.traineddata").write_bytes(b"eng")
    (tessdata / "configs" / "pdf").write_text("renderer pdf\n", encoding="utf-8")
    binario = raiz / "tesseract.exe"
    binario.write_bytes(b"")
    return Tool("Tesseract", binario, "5.5.0", "teste"), tessdata


def test_idioma_ja_instalado_nao_gera_trabalho(sistema, monkeypatch: pytest.MonkeyPatch) -> None:
    tesseract, tessdata = sistema
    monkeypatch.delenv("TESSDATA_PREFIX", raising=False)

    destino, origem, disponiveis, notas = tc.provision_languages(tesseract, ["eng"])

    assert destino == tessdata
    assert origem == "sistema"
    assert "eng" in disponiveis
    assert notas == []


def test_idioma_do_kit_e_instalado_no_sistema_quando_ha_permissao(
    sistema, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tesseract, tessdata = sistema
    monkeypatch.delenv("TESSDATA_PREFIX", raising=False)
    kit = tmp_path / "idiomas"
    kit.mkdir()
    (kit / "por.traineddata").write_bytes(b"por")

    destino, origem, disponiveis, notas = tc.provision_languages(
        tesseract, ["por"], extra_dirs=[kit]
    )

    assert origem == "sistema"
    assert destino == tessdata
    assert (tessdata / "por.traineddata").read_bytes() == b"por"
    assert "por" in disponiveis
    assert notas


def test_tessdata_somente_leitura_gera_espelho_no_perfil(
    sistema, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O caso comum: Program Files sem elevação.

    A alternativa seria exigir que o operador rodasse como administrador —
    exatamente a fricção que as apostilas do kit antigo documentavam.
    """
    tesseract, tessdata = sistema
    monkeypatch.delenv("TESSDATA_PREFIX", raising=False)
    kit = tmp_path / "idiomas"
    kit.mkdir()
    (kit / "por.traineddata").write_bytes(b"por")

    espelho = tmp_path / "perfil" / "tessdata"
    monkeypatch.setattr(tc, "_is_writable", lambda _p: False)
    monkeypatch.setattr(tc, "_user_tessdata", lambda: espelho)

    destino, origem, disponiveis, notas = tc.provision_languages(
        tesseract, ["por"], extra_dirs=[kit]
    )

    assert destino == espelho
    assert origem == "espelho do usuário"
    # O idioma novo e os que já existiam precisam estar no espelho: apontar
    # TESSDATA_PREFIX para lá esconde o diretório do sistema.
    assert (espelho / "por.traineddata").read_bytes() == b"por"
    assert (espelho / "eng.traineddata").read_bytes() == b"eng"
    # Os configs também: o OCRmyPDF invoca o Tesseract com o renderer "pdf".
    assert (espelho / "configs" / "pdf").is_file()
    assert disponiveis >= {"por", "eng"}
    assert any("somente leitura" in nota for nota in notas)
    # O tessdata do sistema permanece intocado.
    assert not (tessdata / "por.traineddata").exists()


def test_espelho_nao_duplica_o_que_ja_copiou(
    sistema, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tesseract, _ = sistema
    monkeypatch.delenv("TESSDATA_PREFIX", raising=False)
    kit = tmp_path / "idiomas"
    kit.mkdir()
    (kit / "por.traineddata").write_bytes(b"por")
    espelho = tmp_path / "perfil" / "tessdata"
    monkeypatch.setattr(tc, "_is_writable", lambda _p: False)
    monkeypatch.setattr(tc, "_user_tessdata", lambda: espelho)

    tc.provision_languages(tesseract, ["por"], extra_dirs=[kit])
    tc.provision_languages(tesseract, ["por"], extra_dirs=[kit])  # segunda vez

    assert (espelho / "por.traineddata").read_bytes() == b"por"


def test_idioma_inexistente_falha_apontando_onde_baixar(
    sistema, monkeypatch: pytest.MonkeyPatch
) -> None:
    tesseract, _ = sistema
    monkeypatch.delenv("TESSDATA_PREFIX", raising=False)

    with pytest.raises(LanguageError) as erro:
        tc.provision_languages(tesseract, ["klingon"], extra_dirs=[])

    assert "klingon" in erro.value.message
    assert "tessdata" in erro.value.remedy


def test_tessdata_prefix_do_ambiente_e_respeitado(sistema, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tesseract 5 aponta para o próprio tessdata; a 4 apontava para o pai."""
    tesseract, tessdata = sistema
    monkeypatch.setenv("TESSDATA_PREFIX", str(tessdata))
    assert tc.system_tessdata(tesseract) == tessdata

    monkeypatch.setenv("TESSDATA_PREFIX", str(tessdata.parent))
    assert tc.system_tessdata(tesseract) == tessdata


def test_idiomas_sao_lidos_do_disco(sistema) -> None:
    _, tessdata = sistema
    assert tc.installed_languages(tessdata) == {"eng"}
    assert tc.installed_languages(tessdata / "inexistente") == frozenset()


# ------------------------------------------------- orientação por origem --


def test_remedio_aponta_o_instalador_quando_e_um_kit(monkeypatch: pytest.MonkeyPatch) -> None:
    """No kit, o instalador está a um clique — é para lá que se manda o operador."""
    monkeypatch.setattr(tc, "bundled_installers", lambda: Path(r"C:\Kit\instaladores"))
    with pytest.raises(ToolchainError) as erro:
        tc.find_tesseract(Path("caminho-inexistente"))
    assert "instaladores" in erro.value.remedy


def test_remedio_usa_winget_quando_e_um_clone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Num clone do repositório não existe pasta de instaladores.

    Apontar para ela seria mandar o operador procurar algo que não está lá.
    """
    monkeypatch.setattr(tc, "bundled_installers", lambda: None)
    with pytest.raises(ToolchainError) as erro:
        tc.find_tesseract(Path("caminho-inexistente"))
    assert "winget" in erro.value.remedy
    assert "instaladores" not in erro.value.remedy


def test_remedio_do_ghostscript_tambem_se_adapta(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tc, "bundled_installers", lambda: None)
    with pytest.raises(ToolchainError) as erro:
        tc.find_ghostscript(Path("caminho-inexistente"))
    assert "winget" in erro.value.remedy


def test_remedio_de_idioma_se_adapta(sistema, monkeypatch: pytest.MonkeyPatch) -> None:
    tesseract, _ = sistema
    monkeypatch.delenv("TESSDATA_PREFIX", raising=False)
    monkeypatch.setattr(tc, "bundled_installers", lambda: None)

    with pytest.raises(LanguageError) as erro:
        tc.provision_languages(tesseract, ["por"], extra_dirs=[])
    assert "tessdata" in erro.value.remedy
    assert "kit" not in erro.value.remedy.lower()


def test_instaladores_do_kit_sao_detectados_por_presenca_de_exe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pasta vazia não conta: o que importa é haver instalador dentro dela."""
    modulo = tmp_path / "src" / "scriptor" / "toolchain.py"
    modulo.parent.mkdir(parents=True)
    modulo.write_text("", encoding="utf-8")
    monkeypatch.setattr(tc, "__file__", str(modulo))

    (tmp_path / "instaladores").mkdir()
    assert tc.bundled_installers() is None

    (tmp_path / "instaladores" / "python-3.13.3-amd64.exe").write_bytes(b"")
    assert tc.bundled_installers() == tmp_path / "instaladores"


def test_remedio_nomeia_o_instalador_que_existe_de_fato(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Instalador rebaixado duas vezes ganha sufixo do navegador.

    Mandar procurar `tesseract-ocr-w64-setup-5.5.0.exe` quando o arquivo no
    disco é `tesseract-ocr-w64-setup-5.5.0 (1).exe` trava exatamente quem o kit
    tenta atender.
    """
    pasta = tmp_path / "instaladores"
    pasta.mkdir()
    (pasta / "tesseract-ocr-w64-setup-5.5.0 (1).exe").write_bytes(b"")
    monkeypatch.setattr(tc, "bundled_installers", lambda: pasta)

    with pytest.raises(ToolchainError) as erro:
        tc.find_tesseract(Path("caminho-inexistente"))
    assert "tesseract-ocr-w64-setup-5.5.0 (1).exe" in erro.value.remedy
