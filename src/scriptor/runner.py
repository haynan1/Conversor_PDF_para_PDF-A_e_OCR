"""Conversão de um documento, do perfil ao arquivo verificado.

Decisões estruturais deste módulo:

**Isolamento por subprocesso.** O OCRmyPDF é importável como biblioteca, mas
chama Tesseract e Ghostscript — dois binários nativos que, diante de um PDF
malformado, podem travar ou abortar o processo. Num lote de milhares de
documentos, um único deles não pode derrubar o restante. Cada conversão roda em
subprocesso próprio, com limite de tempo e encerramento da árvore de processos.

**Saída atômica.** A conversão escreve num arquivo temporário e só então é
renomeada para o destino. Interromper o Scriptor no meio nunca deixa um PDF
truncado na pasta de saída — o modo de falha mais traiçoeiro num acervo, porque
o arquivo *parece* pronto.

**O original é intocável até a verificação passar.** Só depois de o PDF/A ser
gerado e validado é que o documento de origem é arquivado ou removido.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .analysis import DocumentProfile, analyze, sha256_file
from .config import Settings
from .conformance import ConformanceReport, verify
from .ledger import Ledger
from .strategy import (
    EXIT_INVALID_OUTPUT_PDF,
    EXIT_OK,
    EXIT_TIMEOUT,
    Attempt,
    Plan,
    build_command,
    classify_exit,
    explain_exit,
    plan,
)
from .toolchain import Toolchain

IS_WINDOWS = sys.platform == "win32"
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0

#: Caminho absoluto do taskkill. Invocar pelo nome deixaria a escolha do
#: executável a cargo do PATH — e o PATH é gravável pelo usuário.
_TASKKILL = str(Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "taskkill.exe")


@dataclass(frozen=True, slots=True)
class Job:
    """Um documento a processar."""

    source: Path
    relative: Path

    def destination(self, settings: Settings) -> Path:
        # A saída é sempre PDF, mesmo quando a entrada é TIFF ou JPEG.
        return settings.output_dir / self.relative.with_suffix(".pdf")

    @property
    def name(self) -> str:
        return self.relative.as_posix()


@dataclass(slots=True)
class AttemptLog:
    label: str
    rationale: str
    exit_code: int
    duration: float

    @property
    def ok(self) -> bool:
        return self.exit_code == EXIT_OK


@dataclass(slots=True)
class Outcome:
    """Resultado final de um documento."""

    job: Job
    status: str  # ok | cached | skipped | rejected | failed
    detail: str = ""
    profile: DocumentProfile | None = None
    document_plan: Plan | None = None
    mode: str | None = None
    attempts: list[AttemptLog] = field(default_factory=list)
    conformance: ConformanceReport | None = None
    duration: float = 0.0
    output_path: Path | None = None
    output_sha256: str | None = None
    output_bytes: int | None = None
    log_path: Path | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def produced_output(self) -> bool:
        return self.status in {"ok", "cached"}

    @property
    def pages(self) -> int:
        return self.profile.pages if self.profile else 0


class Cancelled(Exception):
    """A execução foi interrompida pelo operador."""


class Runner:
    """Converte documentos. Uma instância é compartilhada por todas as threads."""

    def __init__(
        self,
        settings: Settings,
        toolchain: Toolchain,
        ledger: Ledger | None = None,
        *,
        run_id: int = 0,
    ) -> None:
        self.settings = settings
        self.toolchain = toolchain
        self.ledger = ledger
        self.run_id = run_id
        self.recipe_hash = settings.recipe_hash(toolchain.fingerprint())
        self._env = toolchain.env()
        self._cancel = threading.Event()
        self._live: set[subprocess.Popen] = set()
        self._live_lock = threading.Lock()

    # ------------------------------------------------------------ controle --

    def cancel(self) -> None:
        """Interrompe o lote e mata os subprocessos em andamento."""
        self._cancel.set()
        with self._live_lock:
            processes = list(self._live)
        for process in processes:
            _kill_tree(process)

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    # ------------------------------------------------------------ execução --

    def process(self, job: Job) -> Outcome:
        started = time.perf_counter()
        try:
            outcome = self._process(job, started)
        except Cancelled:
            outcome = Outcome(
                job=job,
                status="failed",
                detail="interrompido pelo operador",
                duration=time.perf_counter() - started,
            )
        except Exception as exc:
            outcome = Outcome(
                job=job,
                status="failed",
                detail=f"erro interno: {type(exc).__name__}: {exc}",
                duration=time.perf_counter() - started,
            )
        self._persist(outcome)
        return outcome

    def _process(self, job: Job, started: float) -> Outcome:
        if self.cancelled:
            raise Cancelled

        profile = analyze(job.source)
        decision = plan(profile, self.settings)

        if decision.decision == "reject":
            self._relocate_failure(job)
            return Outcome(
                job=job,
                status="rejected",
                detail=decision.reason,
                profile=profile,
                document_plan=decision,
                duration=time.perf_counter() - started,
            )

        if decision.decision == "skip":
            return Outcome(
                job=job,
                status="skipped",
                detail=decision.reason,
                profile=profile,
                document_plan=decision,
                duration=time.perf_counter() - started,
            )

        destination = job.destination(self.settings)

        cached = self._cached(profile, destination)
        if cached is not None:
            return Outcome(
                job=job,
                status="cached",
                detail=cached,
                profile=profile,
                document_plan=decision,
                duration=time.perf_counter() - started,
                output_path=destination,
                output_bytes=destination.stat().st_size if destination.exists() else None,
            )

        if self.settings.dry_run:
            return Outcome(
                job=job,
                status="skipped",
                detail=f"simulação: {decision.attempts[0].mode} — {decision.reason}",
                profile=profile,
                document_plan=decision,
                mode=decision.attempts[0].mode,
                duration=time.perf_counter() - started,
            )

        return self._convert(job, profile, decision, destination, started)

    # ------------------------------------------------------------ conversão --

    def _convert(
        self,
        job: Job,
        profile: DocumentProfile,
        decision: Plan,
        destination: Path,
        started: float,
    ) -> Outcome:
        destination.parent.mkdir(parents=True, exist_ok=True)
        log_path = self._log_path(job)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        attempts: list[AttemptLog] = []
        notes: list[str] = []
        conformance: ConformanceReport | None = None
        jobs_per_file = max(1, self.settings.jobs_per_file or 1)

        with log_path.open("w", encoding="utf-8", errors="replace") as log:
            log.write(f"# scriptor {__version__}\n")
            log.write(f"# origem   : {job.source}\n")
            log.write(f"# destino  : {destination}\n")
            log.write(f"# perfil   : {profile.summary(self.settings.text_threshold)}\n")
            log.write(f"# decisão  : {decision.reason}\n")
            log.write(f"# receita  : {self.recipe_hash} · {self.toolchain.fingerprint()}\n\n")

            queue: list[Attempt] = list(decision.attempts)
            index = 0
            while index < len(queue):
                attempt = queue[index]
                index += 1
                if self.cancelled:
                    raise Cancelled

                temp_output = _temp_sibling(destination, "pdf")
                temp_sidecar = (
                    _temp_sibling(destination, "txt") if self.settings.sidecar_text else None
                )
                command = build_command(
                    attempt,
                    source=job.source,
                    destination=temp_output,
                    settings=self.settings,
                    profile=profile,
                    jobs=jobs_per_file,
                    sidecar=temp_sidecar,
                )

                log.write(f"$ [{attempt.label}] {_shell_repr(command)}\n")
                log.write(f"  motivo: {attempt.rationale}\n")

                attempt_started = time.perf_counter()
                exit_code, output = self._execute(command)
                elapsed = time.perf_counter() - attempt_started
                log.write(output)
                log.write(
                    f"\n→ saída {exit_code} ({explain_exit(exit_code)}) em {elapsed:.1f}s\n\n"
                )
                log.flush()

                attempts.append(AttemptLog(attempt.label, attempt.rationale, exit_code, elapsed))

                if exit_code == EXIT_OK and self.settings.verify:
                    conformance = verify(
                        temp_output,
                        expected=self.settings.pdfa_part,
                        verapdf=self.toolchain.verapdf,
                    )
                    log.write(f"→ conformidade [{conformance.validator}]: {conformance.label}\n")
                    if conformance.problems:
                        for problem in conformance.problems:
                            log.write(f"  · {problem}\n")
                    log.write("\n")
                    if not conformance.ok:
                        # Trata reprovação como falha de empacotamento: alimenta a
                        # mesma escada, em vez de entregar um arquivo não conforme.
                        exit_code = EXIT_INVALID_OUTPUT_PDF
                        attempts[-1].exit_code = exit_code

                if exit_code == EXIT_OK:
                    self._commit(temp_output, destination, temp_sidecar)
                    output_bytes = destination.stat().st_size
                    outcome = Outcome(
                        job=job,
                        status="ok",
                        detail=decision.reason,
                        profile=profile,
                        document_plan=decision,
                        mode=attempt.label,
                        attempts=attempts,
                        conformance=conformance,
                        duration=time.perf_counter() - started,
                        output_path=destination,
                        output_sha256=sha256_file(destination),
                        output_bytes=output_bytes,
                        log_path=log_path,
                        notes=notes,
                    )
                    self._relocate_success(job, notes)
                    return outcome

                _discard(temp_output, temp_sidecar)

                verdict = classify_exit(exit_code)
                if verdict == "fatal":
                    break
                if verdict == "degrade" and not attempt.degraded:
                    queue.insert(index, attempt.degrade())

        self._relocate_failure(job)
        last = attempts[-1] if attempts else None
        detail = explain_exit(last.exit_code) if last else "nenhuma tentativa executada"
        if conformance is not None and not conformance.ok:
            detail = f"{detail} — {conformance.detail()}"
        return Outcome(
            job=job,
            status="failed",
            detail=detail,
            profile=profile,
            document_plan=decision,
            mode=last.label if last else None,
            attempts=attempts,
            conformance=conformance,
            duration=time.perf_counter() - started,
            log_path=log_path,
            notes=notes,
        )

    def _execute(self, command: list[str]) -> tuple[int, str]:
        """Roda o comando com limite de tempo, matando a árvore de processos."""
        popen_kwargs: dict[str, object] = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "env": self._env,
            "creationflags": _NO_WINDOW,
        }
        if not IS_WINDOWS:
            # Grupo próprio para que o kill alcance Tesseract e Ghostscript.
            popen_kwargs["start_new_session"] = True

        try:
            process = subprocess.Popen(command, **popen_kwargs)  # noqa: S603
        except OSError as exc:
            return 3, f"falha ao iniciar o processo: {exc}\n"

        with self._live_lock:
            self._live.add(process)
        try:
            try:
                stdout, _ = process.communicate(timeout=self.settings.timeout_seconds)
                exit_code = process.returncode
            except subprocess.TimeoutExpired:
                _kill_tree(process)
                stdout, _ = process.communicate()
                return EXIT_TIMEOUT, _decode(stdout) + (
                    f"\n[scriptor] excedeu {self.settings.timeout_seconds}s; processo encerrado\n"
                )
        finally:
            with self._live_lock:
                self._live.discard(process)

        if self.cancelled:
            raise Cancelled
        return exit_code, _decode(stdout)

    # ---------------------------------------------------------- movimentação --

    def _commit(self, temp_output: Path, destination: Path, temp_sidecar: Path | None) -> None:
        """Publica a saída atomicamente."""
        os.replace(temp_output, destination)
        if temp_sidecar is not None and temp_sidecar.exists():
            os.replace(temp_sidecar, destination.with_suffix(".txt"))

    def _relocate_success(self, job: Job, notes: list[str]) -> None:
        policy = self.settings.on_success
        if policy == "keep":
            return
        if policy == "delete":
            try:
                job.source.unlink()
            except OSError as exc:
                notes.append(f"original não pôde ser removido: {exc}")
            return
        self._move(job.source, self.settings.archive_dir / job.relative, notes, "arquivar")

    def _relocate_failure(self, job: Job) -> None:
        if self.settings.on_failure != "quarantine" or self.settings.dry_run:
            return
        self._move(job.source, self.settings.failed_dir / job.relative, [], "isolar")

    @staticmethod
    def _move(source: Path, target: Path, notes: list[str], action: str) -> None:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, _unique(target))
        except OSError as exc:
            notes.append(f"não foi possível {action} o original: {exc}")

    # ------------------------------------------------------------- suporte --

    def _cached(self, profile: DocumentProfile, destination: Path) -> str | None:
        """Já convertido com esta mesma receita e a saída ainda existe?"""
        if self.settings.force or self.ledger is None:
            return None
        record = self.ledger.lookup(profile.sha256, self.recipe_hash)
        if record is None:
            return None
        if not destination.exists():
            return None
        if record.output_sha256:
            try:
                if sha256_file(destination) != record.output_sha256:
                    return None
            except OSError:
                return None
        return f"já convertido em {record.created_at[:10]} com a mesma receita"

    def _log_path(self, job: Job) -> Path:
        stem = job.relative.as_posix().replace("/", "__")
        return self.settings.log_dir / f"{stem}.log"

    def _persist(self, outcome: Outcome) -> None:
        if self.ledger is None or self.settings.dry_run:
            return
        profile = outcome.profile
        self.ledger.record(
            self.run_id,
            source_path=str(outcome.job.source),
            source_sha256=profile.sha256 if profile else "",
            source_bytes=profile.size_bytes if profile else 0,
            output_path=str(outcome.output_path) if outcome.output_path else None,
            output_sha256=outcome.output_sha256,
            output_bytes=outcome.output_bytes,
            recipe_hash=self.recipe_hash,
            status="ok" if outcome.status == "cached" else outcome.status,
            nature=profile.nature(self.settings.text_threshold) if profile else None,
            pages=profile.pages if profile else None,
            mode=outcome.mode,
            attempts=len(outcome.attempts),
            conformance=outcome.conformance.label if outcome.conformance else None,
            conformance_detail=outcome.conformance.detail() if outcome.conformance else None,
            duration_ms=int(outcome.duration * 1000),
            detail=outcome.detail,
        )


# --------------------------------------------------------------------------- #
# Utilitários de processo e arquivo
# --------------------------------------------------------------------------- #


def _kill_tree(process: subprocess.Popen) -> None:
    """Mata o processo e seus descendentes.

    ``Popen.kill()`` sozinho encerra apenas o interpretador do OCRmyPDF; o
    Tesseract e o Ghostscript que ele lançou continuariam rodando, segurando
    handles do arquivo e consumindo CPU.
    """
    if process.poll() is not None:
        return
    if IS_WINDOWS:
        subprocess.run(  # noqa: S603
            [_TASKKILL, "/T", "/F", "/PID", str(process.pid)],
            capture_output=True,
            check=False,
            creationflags=_NO_WINDOW,
        )
    else:
        with contextlib.suppress(OSError, ProcessLookupError):
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    with contextlib.suppress(OSError):
        process.kill()


def _decode(payload: bytes | None) -> str:
    if not payload:
        return ""
    return payload.decode("utf-8", "replace")


def _temp_sibling(destination: Path, suffix: str) -> Path:
    """Temporário no mesmo diretório do destino — condição para o rename atômico."""
    token = uuid.uuid4().hex[:8]
    return destination.parent / f".{destination.stem}.scriptor-{token}.{suffix}"


def _discard(*paths: Path | None) -> None:
    for path in paths:
        if path is None:
            continue
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)


def _unique(target: Path) -> Path:
    """Evita sobrescrever ao arquivar homônimos vindos de pastas diferentes."""
    if not target.exists():
        return target
    for index in range(1, 1000):
        candidate = target.with_name(f"{target.stem} ({index}){target.suffix}")
        if not candidate.exists():
            return candidate
    return target.with_name(f"{target.stem} ({uuid.uuid4().hex[:8]}){target.suffix}")


def _shell_repr(command: list[str]) -> str:
    return " ".join(f'"{part}"' if " " in part else part for part in command)
