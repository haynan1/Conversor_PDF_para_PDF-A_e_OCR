"""Apresentação de uma execução no terminal.

Duas superfícies distintas, e a distinção importa:

* **o que já aconteceu** é impresso e rola para cima — é registro, permanece
  legível depois que o comando termina e sobrevive ao redirecionamento de saída;
* **o que está acontecendo agora** é redesenhado no rodapé — é estado, e estado
  obsoleto não deve deixar rastro.

Cor só aparece onde carrega informação: verde é conversão verificada, âmbar é
decisão que o operador precisa conhecer, vermelho exige ação. Nada é colorido
para parecer bonito.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Group, RenderableType
from rich.live import Live
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from .console import (
    BAR_EMPTY,
    BAR_FULL,
    DOT,
    SPINNER,
    console,
    divider,
    elide,
    fmt_bytes,
    fmt_delta,
    fmt_duration,
    glyph,
)
from .runner import Job, Outcome
from .toolchain import Toolchain

_STATUS_STYLE = {
    "ok": ("ok", "ok"),
    "cached": ("skip", "skip"),
    "skipped": ("skip", "skip"),
    "rejected": ("warn", "warn"),
    "failed": ("err", "err"),
}

_STATUS_WORD = {
    "ok": "convertido",
    "cached": "em cache",
    "skipped": "pulado",
    "rejected": "recusado",
    "failed": "falhou",
}

_NAME_WIDTH = 34


def header(
    *,
    input_dir: Path,
    output_dir: Path,
    languages: Iterable[str],
    profile: str,
    toolchain: Toolchain,
    total: int,
    total_bytes: int,
    workers: int,
    jobs: int,
    dry_run: bool = False,
) -> None:
    """Cabeçalho: tudo que determina o resultado, visível antes de começar."""
    console.print()
    title = Text("  SCRIPTOR", style="s.brand")
    title.append("  OCR · PDF/A", style="s.dim")
    if dry_run:
        title.append("   simulação", style="s.accent")
    console.print(title)
    divider()

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="s.label", justify="left", width=9)
    grid.add_column(style="s.value", overflow="fold")

    grid.add_row("entrada", f"[s.path]{_shorten(input_dir)}[/]")
    grid.add_row("saída", f"[s.path]{_shorten(output_dir)}[/]")
    grid.add_row(
        "lote",
        f"{total} documento{'s' if total != 1 else ''}  [s.dim]·[/]  {fmt_bytes(total_bytes)}",
    )
    grid.add_row(
        "receita",
        f"{'+'.join(languages)}  [s.dim]·[/]  {profile.upper().replace('PDFA-', 'PDF/A-')}"
        f"  [s.dim]·[/]  {workers}×{jobs} paralelo",
    )
    grid.add_row(
        "motor",
        f"Tesseract {_version(toolchain.tesseract.version)}"
        f"  [s.dim]·[/]  Ghostscript {_version(toolchain.ghostscript.version)}"
        + ("  [s.dim]·[/]  veraPDF" if toolchain.verapdf else ""),
    )

    console.print(grid)
    for note in toolchain.notes:
        console.print(f"  [s.warn]{glyph('warn')}[/] [s.dim]{note}[/]")
    divider()


@dataclass
class _Active:
    job: Job
    started: float = field(default_factory=time.monotonic)


class RunView:
    """Rodapé vivo com o que está em execução, mais o histórico impresso acima."""

    def __init__(self, total: int, *, enabled: bool = True) -> None:
        self.total = total
        self.done = 0
        self.enabled = enabled and console.is_terminal
        self._active: dict[Path, _Active] = {}
        self._spinner = Spinner(SPINNER, style="s.busy")
        self._started = time.monotonic()
        self._live: Live | None = None

    # ------------------------------------------------------------ contexto --

    def __enter__(self) -> RunView:
        if self.enabled:
            self._live = Live(
                self._render(),
                console=console,
                refresh_per_second=8,
                transient=True,
            )
            self._live.__enter__()
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._live is not None:
            self._live.__exit__(*exc_info)
            self._live = None

    # -------------------------------------------------------------- eventos --

    def begin(self, job: Job) -> None:
        self._active[job.source] = _Active(job)
        self._refresh()

    def finish(self, outcome: Outcome) -> None:
        self._active.pop(outcome.job.source, None)
        self.done += 1
        console.print(self._line(outcome))
        self._refresh()

    # ------------------------------------------------------------ desenho --

    def _refresh(self) -> None:
        if self._live is not None:
            self._live.update(self._render())

    def _render(self) -> RenderableType:
        rows: list[RenderableType] = []
        now = time.monotonic()

        table = Table.grid(padding=(0, 1))
        table.add_column(width=2)
        table.add_column(width=_NAME_WIDTH, overflow="ellipsis")
        table.add_column(style="s.dim")
        for entry in list(self._active.values())[:8]:
            table.add_row(
                self._spinner,
                Text(elide(entry.job.name, _NAME_WIDTH), style="s.value"),
                Text(fmt_duration(now - entry.started), style="s.dim"),
            )
        if self._active:
            rows.append(table)

        rows.append(self._progress(now))
        return Group(*rows)

    def _progress(self, now: float) -> RenderableType:
        width = 24
        ratio = self.done / self.total if self.total else 1.0
        filled = int(ratio * width)
        bar = Text("  ")
        bar.append(BAR_FULL * filled, style="s.accent")
        bar.append(BAR_EMPTY * (width - filled), style="s.rule")
        elapsed = now - self._started
        bar.append(f"  {self.done}/{self.total}", style="s.value")
        bar.append(f"  {fmt_duration(elapsed)}", style="s.dim")
        if self.done and self.done < self.total:
            remaining = elapsed / self.done * (self.total - self.done)
            bar.append(f"  restam ~{fmt_duration(remaining)}", style="s.dim")
        return bar

    def _line(self, outcome: Outcome) -> Text:
        mark_style, text_style = _STATUS_STYLE.get(outcome.status, ("dim", "dim"))
        mark = {
            "ok": "ok",
            "cached": "skip",
            "skipped": "skip",
            "rejected": "warn",
            "failed": "err",
        }[outcome.status]

        line = Text("  ")
        line.append(f"{glyph(mark)} ", style=f"s.{mark_style}")
        line.append(f"{elide(outcome.job.name, _NAME_WIDTH):<{_NAME_WIDTH + 2}}", style="s.value")

        if outcome.status == "ok":
            profile = outcome.profile
            line.append(f"{(outcome.mode or ''):<14}", style=f"s.mode.{_mode_key(outcome.mode)}")
            line.append(f"{outcome.pages:>4} pág  ", style="s.dim")
            line.append(f"{fmt_duration(outcome.duration):>7}  ", style="s.dim")
            if profile and outcome.output_bytes:
                line.append(
                    f"{fmt_delta(profile.size_bytes, outcome.output_bytes):>5}", style="s.dim"
                )
            if outcome.conformance and not outcome.conformance.ok:
                line.append("  não conforme", style="s.warn")
        else:
            line.append(f"{_STATUS_WORD[outcome.status]} {DOT} ", style=f"s.{text_style}")
            line.append(outcome.detail, style="s.dim")

        for note in outcome.notes:
            line.append(f"\n      {glyph('warn')} {note}", style="s.warn")
        return line


def _mode_key(mode: str | None) -> str:
    base = (mode or "").split("+", 1)[0]
    return base if base in {"skip", "redo", "force"} else "passthrough"


def summary(
    outcomes: list[Outcome],
    *,
    elapsed: float,
    report_path: Path | None,
    verify_enabled: bool,
) -> None:
    """Fecho da execução: números, falhas com o log ao lado, caminho do relatório."""
    counts: dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome.status] = counts.get(outcome.status, 0) + 1

    source_bytes = sum(o.profile.size_bytes for o in outcomes if o.profile)
    output_bytes = sum(o.output_bytes or 0 for o in outcomes if o.status == "ok")

    divider()

    line = Text("  ")
    line.append(f"{len(outcomes)} documentos", style="s.value")
    line.append(f"  ·  {fmt_duration(elapsed)}", style="s.dim")
    console.print(line)

    order = ("ok", "cached", "skipped", "rejected", "failed")
    tally = Text("  ")
    for status in order:
        if not counts.get(status):
            continue
        style = f"s.{_STATUS_STYLE[status][1]}"
        tally.append(f"{counts[status]} {_STATUS_WORD[status]}", style=style)
        tally.append("   ", style="s.dim")
    if tally.plain.strip():
        console.print(tally)

    converted = [o for o in outcomes if o.status == "ok"]
    if converted:
        console.print()
        console.print(
            f"  [s.label]volume [/][s.value]{fmt_bytes(source_bytes)}[/]"
            f"[s.dim] → [/][s.value]{fmt_bytes(output_bytes)}[/]"
            f"[s.dim]  ({fmt_delta(source_bytes, output_bytes)})[/]"
        )
        if verify_enabled:
            conforming = sum(1 for o in converted if o.conformance and o.conformance.ok)
            validators = {o.conformance.validator for o in converted if o.conformance}
            style = "s.ok" if conforming == len(converted) else "s.warn"
            console.print(
                f"  [s.label]conforme[/] [{style}]{conforming}/{len(converted)}[/]"
                f"[s.dim]  verificado por {', '.join(sorted(validators)) or '—'}[/]"
            )

    problems = [o for o in outcomes if o.status in {"failed", "rejected"}]
    if problems:
        console.print()
        console.print("  [s.err]pendências[/]")
        for outcome in problems:
            console.print(f"    [s.value]{outcome.job.name}[/] [s.dim]— {outcome.detail}[/]")
            if outcome.log_path:
                console.print(f"      [s.dim]log: {_shorten(outcome.log_path)}[/]")

    if report_path:
        console.print()
        console.print(f"  [s.label]relatório[/] [s.path]{_shorten(report_path)}[/]")
    console.print()


def _shorten(path: Path, keep: int = 3) -> str:
    parts = Path(path).parts
    if len(parts) <= keep + 1:
        return str(path)
    return "…" + "\\".join(parts[-keep:]) if "\\" in str(path) else "…/" + "/".join(parts[-keep:])


def _version(raw: str) -> str:
    for token in raw.replace("v", " ").split():
        if token[:1].isdigit():
            return token
    return raw
