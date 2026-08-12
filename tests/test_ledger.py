"""Livro-razão: idempotência e trilha de auditoria."""

from __future__ import annotations

from pathlib import Path

from scriptor.ledger import Ledger


def _run(ledger: Ledger, recipe: str = "r1") -> int:
    return ledger.start_run(recipe_hash=recipe, toolchain="t=1", workspace=Path("."), settings={})


def test_consulta_encontra_a_conversao_da_mesma_receita(tmp_path: Path) -> None:
    with Ledger(tmp_path / "l.sqlite3") as ledger:
        run = _run(ledger)
        ledger.record(
            run,
            source_path="a.pdf",
            source_sha256="abc",
            source_bytes=10,
            recipe_hash="r1",
            status="ok",
        )
        assert ledger.lookup("abc", "r1") is not None
        assert ledger.lookup("abc", "r2") is None
        assert ledger.lookup("outro", "r1") is None


def test_falha_nao_conta_como_conversao_feita(tmp_path: Path) -> None:
    with Ledger(tmp_path / "l.sqlite3") as ledger:
        run = _run(ledger)
        ledger.record(
            run,
            source_path="a.pdf",
            source_sha256="abc",
            source_bytes=10,
            recipe_hash="r1",
            status="failed",
        )
        assert ledger.lookup("abc", "r1") is None


def test_historico_e_acumulativo_e_nunca_sobrescrito(tmp_path: Path) -> None:
    """Auditoria exige que a tentativa fracassada de ontem continue registrada."""
    with Ledger(tmp_path / "l.sqlite3") as ledger:
        run = _run(ledger)
        for status in ("failed", "ok"):
            ledger.record(
                run,
                source_path="a.pdf",
                source_sha256="abc",
                source_bytes=10,
                recipe_hash="r1",
                status=status,
                mode=status,
            )
        assert len(ledger.recent(10)) == 2
        assert ledger.totals() == {"failed": 1, "ok": 1}
        assert ledger.lookup("abc", "r1").mode == "ok"


def test_execucao_registra_totais_no_fechamento(tmp_path: Path) -> None:
    with Ledger(tmp_path / "l.sqlite3") as ledger:
        run = _run(ledger)
        ledger.finish_run(run, {"counts": {"ok": 3}})
        registro = ledger.runs(1)[0]
        assert registro["finished_at"]
        assert "ok" in registro["totals"]


def test_o_banco_sobrevive_a_reabertura(tmp_path: Path) -> None:
    path = tmp_path / "l.sqlite3"
    with Ledger(path) as ledger:
        ledger.record(
            _run(ledger),
            source_path="a.pdf",
            source_sha256="abc",
            source_bytes=1,
            recipe_hash="r1",
            status="ok",
        )
    with Ledger(path) as reopened:
        assert reopened.lookup("abc", "r1") is not None


def test_escrita_concorrente_nao_perde_linhas(tmp_path: Path) -> None:
    """O pipeline grava a partir de várias threads de trabalho."""
    from concurrent.futures import ThreadPoolExecutor

    with Ledger(tmp_path / "l.sqlite3") as ledger:
        run = _run(ledger)

        def write(index: int) -> None:
            ledger.record(
                run,
                source_path=f"{index}.pdf",
                source_sha256=f"sha{index}",
                source_bytes=index,
                recipe_hash="r1",
                status="ok",
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, range(64)))

        assert ledger.totals()["ok"] == 64
