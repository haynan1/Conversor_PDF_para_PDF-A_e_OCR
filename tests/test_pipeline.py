"""Descoberta de entradas e planejamento de paralelismo."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from scriptor.config import Settings
from scriptor.pipeline import MAX_JOBS_PER_FILE, discover, plan_concurrency, stable_jobs


def test_descoberta_preserva_a_hierarquia(populated: Settings) -> None:
    jobs = discover(populated)
    nomes = {job.relative.as_posix() for job in jobs}
    assert "2024/janeiro/digitalizacao.png" in nomes
    assert "nativo.pdf" in nomes


def test_saida_e_arquivo_nunca_sao_reconsumidos(
    populated: Settings, samples: dict[str, Path]
) -> None:
    """Sem isto, cada execução reprocessaria a própria saída da anterior."""
    populated.output_dir.mkdir(parents=True, exist_ok=True)
    populated.archive_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(samples["nativo"], populated.output_dir / "ja-convertido.pdf")
    shutil.copy2(samples["nativo"], populated.archive_dir / "ja-arquivado.pdf")

    nomes = {job.source.name for job in discover(populated)}
    assert "ja-convertido.pdf" not in nomes
    assert "ja-arquivado.pdf" not in nomes


def test_saida_dentro_da_entrada_tambem_e_excluida(
    settings: Settings, samples: dict[str, Path]
) -> None:
    nested = settings.with_overrides(output_dir=settings.input_dir / "Saida")
    nested.ensure_dirs()
    shutil.copy2(samples["nativo"], nested.input_dir / "origem.pdf")
    shutil.copy2(samples["nativo"], nested.output_dir / "resultado.pdf")
    assert {job.source.name for job in discover(nested)} == {"origem.pdf"}


def test_arquivos_temporarios_e_vazios_sao_ignorados(populated: Settings) -> None:
    (populated.input_dir / "~$rascunho.pdf").write_bytes(b"%PDF-1.7\n")
    (populated.input_dir / ".oculto.pdf").write_bytes(b"%PDF-1.7\n")
    (populated.input_dir / "vazio.pdf").write_bytes(b"")
    (populated.input_dir / "planilha.xlsx").write_bytes(b"nao e documento")

    nomes = {job.source.name for job in discover(populated)}
    assert not nomes & {"~$rascunho.pdf", ".oculto.pdf", "vazio.pdf", "planilha.xlsx"}


def test_lote_ordenado_do_maior_para_o_menor(populated: Settings) -> None:
    """Começar pelos longos encurta o tempo total do lote."""
    tamanhos = [job.source.stat().st_size for job in discover(populated)]
    assert tamanhos == sorted(tamanhos, reverse=True)


def test_sem_recursao_ignora_subpastas(populated: Settings) -> None:
    jobs = discover(populated.with_overrides(recursive=False))
    assert all(job.relative.parent == Path(".") for job in jobs)


def test_entrada_inexistente_devolve_lista_vazia(settings: Settings, tmp_path: Path) -> None:
    assert discover(settings.with_overrides(input_dir=tmp_path / "nao-existe")) == []


@pytest.mark.parametrize("documentos", [1, 3, 50])
def test_paralelismo_nao_estoura_os_nucleos(settings: Settings, documentos: int) -> None:
    workers, jobs = plan_concurrency(settings, documentos)
    import os

    cores = os.cpu_count() or 4
    assert 1 <= workers <= max(1, documentos)
    assert 1 <= jobs <= MAX_JOBS_PER_FILE
    assert workers * jobs <= max(cores, 4) * 2


def test_um_unico_documento_recebe_todos_os_nucleos_uteis(settings: Settings) -> None:
    workers, jobs = plan_concurrency(settings, 1)
    assert workers == 1
    assert jobs >= 1


def test_configuracao_explicita_prevalece(settings: Settings) -> None:
    workers, jobs = plan_concurrency(settings.with_overrides(concurrency=2, jobs_per_file=3), 20)
    assert (workers, jobs) == (2, 3)


def test_arquivo_ainda_sendo_gravado_e_adiado(
    settings: Settings, samples: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scanner de rede grava em partes; processar cedo produz PDF truncado."""
    from scriptor import pipeline

    shutil.copy2(samples["nativo"], settings.input_dir / "chegando.pdf")

    tamanhos = iter([100, 200])
    monkeypatch.setattr(pipeline, "_size", lambda _path: next(tamanhos, 200))
    assert stable_jobs(settings, quiet_seconds=0.01) == []


def test_arquivo_estavel_e_aceito(
    settings: Settings, samples: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from scriptor import pipeline

    shutil.copy2(samples["nativo"], settings.input_dir / "pronto.pdf")
    monkeypatch.setattr(pipeline, "_size", lambda _path: 4096)
    assert [job.source.name for job in stable_jobs(settings, quiet_seconds=0.01)] == ["pronto.pdf"]
