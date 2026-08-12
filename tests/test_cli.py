"""Superfície de comandos.

Um erro de fiação do Typer não aparece em nenhum teste de unidade — aparece na
primeira vez que alguém digita o comando. Aqui todo comando é invocado de
verdade, incluindo os caminhos de erro e os códigos de saída, que são contrato
para quem automatiza o Scriptor no Agendador de Tarefas.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from scriptor import __version__
from scriptor.cli import app, main
from scriptor.config import Settings

cli = CliRunner()


def _invoke(*args: str, workspace: Path | None = None):
    argumentos = list(args)
    if workspace is not None:
        argumentos += ["--config", str(workspace / "scriptor.toml")]
    return cli.invoke(app, argumentos)


# ------------------------------------------------------------------ básicos --


def test_versao() -> None:
    resultado = _invoke("--version")
    assert resultado.exit_code == 0
    assert __version__ in resultado.output


def test_ajuda_lista_todos_os_comandos() -> None:
    resultado = _invoke("--help")
    assert resultado.exit_code == 0
    for comando in ("run", "abrir", "doctor", "watch", "verificar", "historico", "limpar", "init"):
        assert comando in resultado.output


def test_init_cria_configuracao_e_pastas(tmp_path: Path) -> None:
    destino = tmp_path / "novo"
    resultado = cli.invoke(app, ["init", str(destino)])

    assert resultado.exit_code == 0
    assert (destino / "scriptor.toml").is_file()
    for pasta in ("Entrada", "Saida", "Processados"):
        assert (destino / pasta).is_dir()


def test_init_e_idempotente(tmp_path: Path) -> None:
    cli.invoke(app, ["init", str(tmp_path)])
    marca = (tmp_path / "scriptor.toml").read_text(encoding="utf-8")
    resultado = cli.invoke(app, ["init", str(tmp_path)])

    assert resultado.exit_code == 0
    assert (tmp_path / "scriptor.toml").read_text(encoding="utf-8") == marca


# ---------------------------------------------------------------- doctor --


def test_doctor_reporta_o_ambiente(workspace: Path) -> None:
    resultado = _invoke("doctor", workspace=workspace)
    # 0 quando pronto, 1 quando falta algo — nunca estouro.
    assert resultado.exit_code in (0, 1)
    assert "Tesseract" in resultado.output
    assert "Ghostscript" in resultado.output


def test_doctor_aponta_o_remedio_quando_falta_ferramenta(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scriptor import cli as cli_module
    from scriptor.errors import ToolchainError

    def sem_tesseract(*_args, **_kwargs):
        raise ToolchainError("Tesseract não encontrado", remedy="Instale com instaladores/…")

    monkeypatch.setattr(cli_module, "resolve_toolchain", sem_tesseract)
    resultado = _invoke("doctor", workspace=workspace)

    assert resultado.exit_code == 1
    assert "Tesseract não encontrado" in resultado.output
    assert "instaladores" in resultado.output


# ------------------------------------------------------------ inspecionar --


def test_inspecionar_mostra_perfil_e_estrategia(workspace: Path, samples) -> None:
    resultado = _invoke("inspecionar", str(samples["carimbado"]), workspace=workspace)

    assert resultado.exit_code == 0
    assert "digitalizado" in resultado.output
    assert "convert" in resultado.output
    assert "redo" in resultado.output


def test_inspecionar_documento_assinado_explica_o_pulo(workspace: Path, samples) -> None:
    resultado = _invoke("inspecionar", str(samples["assinado"]), workspace=workspace)
    assert "skip" in resultado.output
    assert "assinatura" in resultado.output


# -------------------------------------------------------------- verificar --


def test_verificar_reprova_pdf_comum(workspace: Path, samples) -> None:
    resultado = _invoke("verificar", str(samples["nativo"]), workspace=workspace)
    assert resultado.exit_code == 1
    assert "0/1" in resultado.output


def test_verificar_pasta_sem_pdf_sai_com_2(workspace: Path, tmp_path: Path) -> None:
    vazia = tmp_path / "vazia"
    vazia.mkdir()
    resultado = _invoke("verificar", str(vazia), workspace=workspace)
    assert resultado.exit_code == 2


# --------------------------------------------------------------------- run --


def test_run_sem_documentos_nao_falha(workspace: Path) -> None:
    resultado = _invoke("run", workspace=workspace)
    assert resultado.exit_code == 0
    assert "nenhum documento" in resultado.output


def test_run_simulado_nao_escreve_nada(settings: Settings, samples) -> None:
    shutil.copy2(samples["escaneado"], settings.input_dir / "escaneado.pdf")
    resultado = _invoke("run", "--simular", workspace=settings.workspace)

    assert resultado.exit_code == 0
    assert "simulação" in resultado.output
    assert list(settings.output_dir.iterdir()) == []
    assert (settings.input_dir / "escaneado.pdf").is_file()


def test_perfil_invalido_e_recusado_com_remedio(workspace: Path) -> None:
    resultado = _invoke("run", "--perfil", "pdfa-42", workspace=workspace)
    assert resultado.exit_code != 0


# --------------------------------------------------------------- histórico --


def test_historico_sem_execucoes_sai_com_2(workspace: Path) -> None:
    resultado = _invoke("historico", workspace=workspace)
    assert resultado.exit_code == 2


def test_historico_lista_o_que_o_ledger_registrou(settings: Settings) -> None:
    from scriptor.ledger import Ledger

    with Ledger(settings.ledger_path) as ledger:
        execucao = ledger.start_run(
            recipe_hash="r1", toolchain="t", workspace=settings.workspace, settings={}
        )
        ledger.record(
            execucao,
            source_path="contrato.pdf",
            source_sha256="abc",
            source_bytes=1,
            recipe_hash="r1",
            status="ok",
            mode="redo",
            conformance="PDF/A-2",
        )

    resultado = _invoke("historico", workspace=settings.workspace)
    assert resultado.exit_code == 0
    assert "contrato.pdf" in resultado.output
    assert "redo" in resultado.output


# ------------------------------------------------------------------ limpar --


def test_limpar_lista_sem_remover_por_padrao(settings: Settings, samples) -> None:
    """A ausência de --confirmar tem de ser inofensiva. Era o defeito do lixo.bat."""
    settings.archive_dir.mkdir(parents=True, exist_ok=True)
    alvo = settings.archive_dir / "antigo.pdf"
    shutil.copy2(samples["nativo"], alvo)

    resultado = _invoke("limpar", "--mais-velho-que", "0", workspace=settings.workspace)

    assert resultado.exit_code == 0
    assert "simulação" in resultado.output
    assert alvo.is_file(), "arquivo removido sem confirmação"


def test_limpar_remove_com_confirmacao(settings: Settings, samples) -> None:
    settings.archive_dir.mkdir(parents=True, exist_ok=True)
    alvo = settings.archive_dir / "antigo.pdf"
    shutil.copy2(samples["nativo"], alvo)

    resultado = _invoke(
        "limpar", "--mais-velho-que", "0", "--confirmar", workspace=settings.workspace
    )

    assert resultado.exit_code == 0
    assert not alvo.exists()


def test_limpar_avisa_antes_de_apagar_a_saida(settings: Settings, samples) -> None:
    """O aviso precisa aparecer na simulação, quando ainda dá para desistir."""
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(samples["nativo"], settings.output_dir / "resultado.pdf")

    resultado = _invoke(
        "limpar", "--o-que", "saida", "--mais-velho-que", "0", workspace=settings.workspace
    )

    assert "atenção" in resultado.output
    assert (settings.output_dir / "resultado.pdf").is_file()


def test_limpar_recusa_alvo_desconhecido(workspace: Path) -> None:
    resultado = _invoke("limpar", "--o-que", "system32", workspace=workspace)
    assert resultado.exit_code == 2


def test_limpar_respeita_o_filtro_de_idade(settings: Settings, samples) -> None:
    settings.archive_dir.mkdir(parents=True, exist_ok=True)
    recente = settings.archive_dir / "recente.pdf"
    shutil.copy2(samples["nativo"], recente)

    resultado = _invoke(
        "limpar", "--mais-velho-que", "365", "--confirmar", workspace=settings.workspace
    )

    assert resultado.exit_code == 0
    assert recente.is_file()


# ------------------------------------------------------ ponto de entrada --


def test_erro_de_configuracao_sai_com_3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`main` é o que o Agendador de Tarefas executa; o código 3 é contrato."""
    ruim = tmp_path / "scriptor.toml"
    ruim.write_text('[scriptor]\nchave_inexistente = "x"\n', encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["scriptor", "run", "--config", str(ruim)])

    with pytest.raises(SystemExit) as saida:
        main()
    assert saida.value.code == 3


def test_configuracao_ausente_sai_com_3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys, "argv", ["scriptor", "run", "--config", str(tmp_path / "nao-existe.toml")]
    )
    with pytest.raises(SystemExit) as saida:
        main()
    assert saida.value.code == 3


# ----------------------------------------------------------------- empacotar --


@pytest.fixture
def kit_falso(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Uma pasta de projeto com tudo que um kit real tem — inclusive o que vaza."""
    raiz = tmp_path / "kit"
    (raiz / "src" / "scriptor").mkdir(parents=True)
    (raiz / "instaladores").mkdir()
    (raiz / "idiomas").mkdir()
    (raiz / "tests").mkdir()
    (raiz / ".git").mkdir()
    (raiz / "Documentos" / "Saida").mkdir(parents=True)
    (raiz / "Documentos" / "_scriptor").mkdir(parents=True)
    (raiz / "src" / "scriptor" / "__pycache__").mkdir()

    (raiz / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (raiz / "Scriptor.cmd").write_text("@echo off\n", encoding="utf-8")
    (raiz / "README.md").write_text("# x\n", encoding="utf-8")
    (raiz / "LICENSE").write_text("MIT\n", encoding="utf-8")
    (raiz / "src" / "scriptor" / "cli.py").write_text("x = 1\n", encoding="utf-8")
    (raiz / "src" / "scriptor" / "__pycache__" / "cli.pyc").write_bytes(b"\x00")
    (raiz / "instaladores" / "python-3.13.3-amd64.exe").write_bytes(b"instalador")
    (raiz / "idiomas" / "por.traineddata").write_bytes(b"idioma")
    (raiz / "tests" / "test_x.py").write_text("def test(): pass\n", encoding="utf-8")
    (raiz / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    # O que jamais pode sair da máquina:
    (raiz / "Documentos" / "Saida" / "contrato-confidencial.pdf").write_bytes(b"%PDF-1.7\n")
    (raiz / "Documentos" / "_scriptor" / "ledger.sqlite3").write_bytes(b"SQLite")

    monkeypatch.chdir(raiz)
    return raiz


def _conteudo(zip_path: Path) -> set[str]:
    import zipfile

    with zipfile.ZipFile(zip_path) as pacote:
        return set(pacote.namelist())


def test_pacote_nao_leva_documentos_nem_trilha_de_auditoria(kit_falso: Path) -> None:
    """O risco concreto: um kit é encaminhado por e-mail sem ninguém reler.

    A pasta de trabalho tem os PDFs já processados e o ledger com nomes,
    hashes e datas de tudo que passou pela máquina.
    """
    destino = kit_falso / "saida.zip"
    resultado = cli.invoke(app, ["empacotar", str(destino)])

    assert resultado.exit_code == 0
    nomes = _conteudo(destino)
    assert not any(n.startswith("Documentos/") for n in nomes)
    assert not any("ledger" in n for n in nomes)
    assert not any("confidencial" in n for n in nomes)


def test_pacote_nao_leva_git_testes_nem_caches(kit_falso: Path) -> None:
    destino = kit_falso / "saida.zip"
    cli.invoke(app, ["empacotar", str(destino)])
    nomes = _conteudo(destino)

    assert not any(n.startswith(".git/") for n in nomes)
    assert not any(n.startswith("tests/") for n in nomes)
    assert not any("__pycache__" in n for n in nomes)
    assert not any(n.endswith(".pyc") for n in nomes)


def test_pacote_leva_o_que_o_operador_precisa(kit_falso: Path) -> None:
    destino = kit_falso / "saida.zip"
    cli.invoke(app, ["empacotar", str(destino)])
    nomes = _conteudo(destino)

    assert "Scriptor.cmd" in nomes
    assert "pyproject.toml" in nomes
    assert "README.md" in nomes
    assert "src/scriptor/cli.py" in nomes
    assert "instaladores/python-3.13.3-amd64.exe" in nomes
    assert "idiomas/por.traineddata" in nomes


def test_pacote_avisa_quando_faltam_instaladores(kit_falso: Path) -> None:
    """Sem instaladores, quem receber vai depender de internet — precisa saber."""
    import shutil

    shutil.rmtree(kit_falso / "instaladores")
    shutil.rmtree(kit_falso / "idiomas")
    resultado = cli.invoke(app, ["empacotar", str(kit_falso / "saida.zip")])

    assert resultado.exit_code == 0
    assert "internet" in resultado.output


def test_pacote_nao_sobrescreve_sem_permissao(kit_falso: Path) -> None:
    destino = kit_falso / "saida.zip"
    destino.write_bytes(b"conteudo anterior")

    resultado = cli.invoke(app, ["empacotar", str(destino)])
    assert resultado.exit_code == 3
    assert destino.read_bytes() == b"conteudo anterior"

    assert cli.invoke(app, ["empacotar", str(destino), "--forcar"]).exit_code == 0
    assert destino.read_bytes() != b"conteudo anterior"


def test_pacote_nao_deixa_parcial_no_destino(kit_falso: Path) -> None:
    destino = kit_falso / "saida.zip"
    cli.invoke(app, ["empacotar", str(destino)])
    assert not list(kit_falso.glob("*.parcial"))


def test_empacotar_fora_de_um_projeto_falha_com_remedio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["scriptor", "empacotar", str(tmp_path / "x.zip")])
    with pytest.raises(SystemExit) as saida:
        main()
    assert saida.value.code == 3
