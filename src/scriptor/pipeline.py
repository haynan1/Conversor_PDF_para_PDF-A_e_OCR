"""Orquestração do lote: descoberta, paralelismo, relatório.

O script original processava um arquivo por vez num laço ``for`` do ``cmd``. Numa
máquina de 32 núcleos isso deixa 31 ociosos enquanto o Tesseract trabalha em um.
Mas simplesmente lançar 32 conversões também é errado: o próprio OCRmyPDF
paraleliza páginas internamente, e as duas camadas competem pela mesma CPU.

O planejamento aqui é bidimensional — quantos documentos ao mesmo tempo e quantos
núcleos por documento — de modo que o produto fique próximo do total disponível.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import __version__
from .analysis import INPUT_SUFFIXES
from .config import Settings
from .ledger import Ledger
from .runner import Job, Outcome, Runner
from .toolchain import Toolchain

#: Acima disto, dar mais núcleos a um único documento rende cada vez menos: a
#: paralelização do OCRmyPDF é por página, e a montagem final é serial.
MAX_JOBS_PER_FILE = 8

#: Teto de documentos simultâneos. Cada um carrega Ghostscript e Tesseract na
#: memória; passar disso troca CPU por pressão de E/S e de RAM.
MAX_WORKERS = 12

_IGNORED_PREFIXES = (".", "~$")


@dataclass(slots=True)
class BatchReport:
    started_at: datetime
    elapsed: float
    outcomes: list[Outcome]
    workers: int
    jobs_per_file: int
    recipe_hash: str
    report_path: Path | None = None
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def failed(self) -> int:
        return self.counts.get("failed", 0) + self.counts.get("rejected", 0)

    @property
    def exit_code(self) -> int:
        """0 se tudo passou, 1 se houve falha, 2 se nada havia para fazer."""
        if not self.outcomes:
            return 2
        return 1 if self.failed else 0


# --------------------------------------------------------------------------- #
# Descoberta
# --------------------------------------------------------------------------- #


def discover(settings: Settings) -> list[Job]:
    """Localiza as entradas, ignorando as pastas que o próprio Scriptor gerencia.

    A ordenação é por tamanho decrescente: começar pelos documentos longos reduz
    o tempo total do lote, porque os curtos preenchem as lacunas no fim em vez de
    deixar um arquivo de 400 páginas rodando sozinho depois que tudo acabou.
    """
    root = settings.input_dir
    if not root.is_dir():
        return []

    managed = {path.resolve() for path in settings.managed_dirs()}
    jobs: list[tuple[int, Job]] = []

    for path in _walk(root, recursive=settings.recursive, managed=managed):
        if path.suffix.lower() not in INPUT_SUFFIXES:
            continue
        if path.name.startswith(_IGNORED_PREFIXES):
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size == 0:
            continue
        jobs.append((size, Job(source=path, relative=path.relative_to(root))))

    jobs.sort(key=lambda item: item[0], reverse=True)
    return [job for _, job in jobs]


def _walk(root: Path, *, recursive: bool, managed: set[Path]) -> Iterator[Path]:
    for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        try:
            if entry.is_dir():
                if not recursive or entry.resolve() in managed:
                    continue
                yield from _walk(entry, recursive=recursive, managed=managed)
            elif entry.is_file():
                yield entry
        except OSError:
            continue


# --------------------------------------------------------------------------- #
# Planejamento de paralelismo
# --------------------------------------------------------------------------- #


def plan_concurrency(settings: Settings, document_count: int) -> tuple[int, int]:
    """Devolve ``(documentos_simultâneos, núcleos_por_documento)``."""
    cores = os.cpu_count() or 4

    if settings.concurrency > 0:
        workers = settings.concurrency
    else:
        workers = max(1, min(document_count, cores // 4 or 1, MAX_WORKERS))

    workers = max(1, min(workers, document_count or 1))

    if settings.jobs_per_file > 0:
        jobs = settings.jobs_per_file
    else:
        jobs = max(1, min(cores // workers, MAX_JOBS_PER_FILE))

    return workers, jobs


# --------------------------------------------------------------------------- #
# Execução
# --------------------------------------------------------------------------- #


def run(
    settings: Settings,
    toolchain: Toolchain,
    *,
    jobs: list[Job] | None = None,
    ledger: Ledger | None = None,
    on_start=None,
    on_finish=None,
    on_runner=None,
    write_report: bool = True,
) -> BatchReport:
    """Processa o lote. ``on_start``/``on_finish`` alimentam a interface."""
    jobs = discover(settings) if jobs is None else jobs
    started_at = datetime.now().astimezone()
    clock = time.perf_counter()

    workers, jobs_per_file = plan_concurrency(settings, len(jobs))
    effective = settings.with_overrides(jobs_per_file=jobs_per_file)

    if not jobs:
        return BatchReport(started_at, 0.0, [], workers, jobs_per_file, "")

    run_id = 0
    if ledger is not None:
        run_id = ledger.start_run(
            recipe_hash=effective.recipe_hash(toolchain.fingerprint()),
            toolchain=toolchain.fingerprint(),
            workspace=effective.workspace,
            settings=effective.to_dict(),
        )

    runner = Runner(effective, toolchain, ledger, run_id=run_id)
    if on_runner:
        # Dá ao chamador uma alça para cancelar o lote em andamento.
        on_runner(runner)
    outcomes: list[Outcome] = []

    def _work(job: Job) -> Outcome:
        if on_start:
            on_start(job)
        return runner.process(job)

    try:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="scriptor") as pool:
            futures: dict[Future[Outcome], Job] = {pool.submit(_work, job): job for job in jobs}
            try:
                for future in _as_completed(futures):
                    outcome = future.result()
                    outcomes.append(outcome)
                    if on_finish:
                        on_finish(outcome)
            except KeyboardInterrupt:
                runner.cancel()
                for future in futures:
                    future.cancel()
                raise
    finally:
        elapsed = time.perf_counter() - clock

    counts: dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome.status] = counts.get(outcome.status, 0) + 1

    report = BatchReport(
        started_at=started_at,
        elapsed=elapsed,
        outcomes=outcomes,
        workers=workers,
        jobs_per_file=jobs_per_file,
        recipe_hash=runner.recipe_hash,
        counts=counts,
    )

    if write_report and not settings.dry_run:
        report.report_path = _write_report(effective, toolchain, report)
    if ledger is not None:
        ledger.finish_run(run_id, {"counts": counts, "elapsed_s": round(elapsed, 2)})

    return report


def _as_completed(futures: dict[Future[Outcome], Job]) -> Iterator[Future[Outcome]]:
    from concurrent.futures import as_completed

    yield from as_completed(futures)


# --------------------------------------------------------------------------- #
# Relatório
# --------------------------------------------------------------------------- #


def _write_report(settings: Settings, toolchain: Toolchain, report: BatchReport) -> Path:
    settings.report_dir.mkdir(parents=True, exist_ok=True)
    stamp = report.started_at.strftime("%Y-%m-%dT%H-%M-%S")
    path = settings.report_dir / f"{stamp}.json"

    payload = {
        "scriptor": __version__,
        "iniciado_em": report.started_at.isoformat(timespec="seconds"),
        "duracao_s": round(report.elapsed, 2),
        "receita": settings.recipe(toolchain.fingerprint()),
        "receita_hash": report.recipe_hash,
        "ferramentas": {
            "tesseract": toolchain.tesseract.version,
            "tesseract_path": str(toolchain.tesseract.path),
            "ghostscript": toolchain.ghostscript.version,
            "ghostscript_path": str(toolchain.ghostscript.path),
            "tessdata": str(toolchain.tessdata_dir),
            "idiomas": sorted(toolchain.languages),
            "verapdf": toolchain.verapdf.version if toolchain.verapdf else None,
        },
        "paralelismo": {
            "documentos": report.workers,
            "nucleos_por_documento": report.jobs_per_file,
        },
        "totais": report.counts,
        "documentos": [_document_entry(outcome, settings) for outcome in report.outcomes],
    }

    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _document_entry(outcome: Outcome, settings: Settings) -> dict:
    profile = outcome.profile
    entry: dict = {
        "origem": str(outcome.job.source),
        "status": outcome.status,
        "detalhe": outcome.detail,
        "duracao_s": round(outcome.duration, 2),
    }
    if profile:
        entry |= {
            "sha256": profile.sha256,
            "bytes": profile.size_bytes,
            "paginas": profile.pages,
            "natureza": profile.nature(settings.text_threshold),
            "assinado": profile.signed,
            "pdfa_origem": profile.pdfa_part,
        }
    if outcome.output_path:
        entry |= {
            "saida": str(outcome.output_path),
            "saida_sha256": outcome.output_sha256,
            "saida_bytes": outcome.output_bytes,
        }
    if outcome.mode:
        entry["estrategia"] = outcome.mode
    if outcome.attempts:
        entry["tentativas"] = [
            {"modo": a.label, "saida": a.exit_code, "duracao_s": round(a.duration, 2)}
            for a in outcome.attempts
        ]
    if outcome.conformance:
        entry["conformidade"] = {
            "valido": outcome.conformance.ok,
            "validador": outcome.conformance.validator,
            "problemas": outcome.conformance.problems,
        }
    if outcome.notes:
        entry["observacoes"] = outcome.notes
    return entry


# --------------------------------------------------------------------------- #
# Vigilância de pasta
# --------------------------------------------------------------------------- #


def stable_jobs(settings: Settings, *, quiet_seconds: float = 2.0) -> list[Job]:
    """Entradas cujo tamanho parou de crescer.

    Scanner de rede escreve o arquivo em partes; processar no instante em que ele
    aparece produz um PDF truncado. Em vez de reagir a eventos do sistema de
    arquivos — que disparam na criação, não na conclusão — medimos duas vezes e
    só aceitamos o que não mudou.
    """
    first = {job.source: _size(job.source) for job in discover(settings)}
    if not first:
        return []
    time.sleep(quiet_seconds)
    stable: list[Job] = []
    for job in discover(settings):
        before = first.get(job.source)
        if before is not None and before == _size(job.source) and before > 0:
            stable.append(job)
    return stable


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return -1
