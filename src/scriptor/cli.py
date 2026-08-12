"""Interface de linha de comando."""

from __future__ import annotations

import contextlib
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Optional

import typer

from . import __version__
from .analysis import INPUT_SUFFIXES, analyze
from .config import CONFIG_FILENAME, TEMPLATE, Settings
from .config import load as load_settings
from .console import console, divider, err_console, fmt_bytes, fmt_duration, glyph
from .errors import ScriptorError
from .ledger import Ledger
from .toolchain import Toolchain
from .toolchain import resolve as resolve_toolchain

app = typer.Typer(
    name="scriptor",
    help="OCR e conversão PDF/A com verificação e trilha de auditoria.",
    add_completion=False,
    no_args_is_help=False,
    rich_markup_mode=None,
    context_settings={"help_option_names": ["-h", "--help"]},
)

# --------------------------------------------------------------------------- #
# Opções compartilhadas
# --------------------------------------------------------------------------- #

ConfigOpt = Annotated[
    Optional[Path], typer.Option("--config", "-c", help="Caminho do scriptor.toml.")
]
InputOpt = Annotated[Optional[Path], typer.Option("--entrada", "-i", help="Pasta de entrada.")]
OutputOpt = Annotated[Optional[Path], typer.Option("--saida", "-o", help="Pasta de saída.")]
LangOpt = Annotated[
    Optional[str], typer.Option("--idioma", "-l", help="Idiomas do OCR, ex.: por ou por+eng.")
]
ProfileOpt = Annotated[
    Optional[str], typer.Option("--perfil", "-p", help="pdfa-1, pdfa-2, pdfa-3 ou pdf.")
]


def _settings(
    config: Path | None,
    *,
    input_dir: Path | None = None,
    output_dir: Path | None = None,
    languages: str | None = None,
    pdfa_part: str | None = None,
    **overrides,
) -> Settings:
    settings = load_settings(config)
    parsed_langs = None
    if languages:
        parsed_langs = tuple(p for p in languages.replace("+", ",").split(",") if p)
    settings = settings.with_overrides(
        input_dir=input_dir.resolve() if input_dir else None,
        output_dir=output_dir.resolve() if output_dir else None,
        languages=parsed_langs,
        pdfa_part=pdfa_part,
        **overrides,
    )
    settings.validate()
    return settings


def _toolchain(settings: Settings) -> Toolchain:
    return resolve_toolchain(
        languages=settings.languages,
        tesseract_path=settings.tesseract,
        ghostscript_path=settings.ghostscript,
        verapdf_path=settings.verapdf,
        tessdata_dirs=settings.tessdata_dirs,
    )


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #


@app.command("run")
def run_command(
    config: ConfigOpt = None,
    input_dir: InputOpt = None,
    output_dir: OutputOpt = None,
    languages: LangOpt = None,
    pdfa_part: ProfileOpt = None,
    optimize: Annotated[
        Optional[int], typer.Option("--otimizar", min=0, max=3, help="0-3; 1 é sem perda.")
    ] = None,
    concurrency: Annotated[
        Optional[int], typer.Option("--paralelo", min=1, help="Documentos simultâneos.")
    ] = None,
    jobs_per_file: Annotated[
        Optional[int], typer.Option("--nucleos", min=1, help="Núcleos por documento.")
    ] = None,
    on_signed: Annotated[
        Optional[str], typer.Option("--assinados", help="skip ou invalidate.")
    ] = None,
    on_success: Annotated[
        Optional[str], typer.Option("--originais", help="keep, archive ou delete.")
    ] = None,
    recursive: Annotated[Optional[bool], typer.Option("--recursivo/--sem-recursao")] = None,
    sidecar: Annotated[
        Optional[bool], typer.Option("--texto/--sem-texto", help="Gera .txt ao lado do PDF.")
    ] = None,
    verify: Annotated[Optional[bool], typer.Option("--verificar/--sem-verificar")] = None,
    force: Annotated[
        bool, typer.Option("--forcar", help="Reprocessa mesmo o que já está no ledger.")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--simular", help="Mostra o plano sem converter nada.")
    ] = False,
) -> None:
    """Converte a pasta de entrada em PDF/A pesquisável."""
    settings = _settings(
        config,
        input_dir=input_dir,
        output_dir=output_dir,
        languages=languages,
        pdfa_part=pdfa_part,
        optimize=optimize,
        concurrency=concurrency,
        jobs_per_file=jobs_per_file,
        on_signed=on_signed,
        on_success=on_success,
        recursive=recursive,
        sidecar_text=sidecar,
        verify=verify,
        force=force or None,
        dry_run=dry_run or None,
    )
    raise typer.Exit(_execute(settings))


def _execute(settings: Settings) -> int:
    from . import pipeline, ui

    toolchain = _toolchain(settings)
    settings.ensure_dirs()

    jobs = pipeline.discover(settings)
    workers, jobs_per_file = pipeline.plan_concurrency(settings, len(jobs))
    total_bytes = sum(_safe_size(job.source) for job in jobs)

    ui.header(
        input_dir=settings.input_dir,
        output_dir=settings.output_dir,
        languages=settings.languages,
        profile=settings.pdfa_part,
        toolchain=toolchain,
        total=len(jobs),
        total_bytes=total_bytes,
        workers=workers,
        jobs=jobs_per_file,
        dry_run=settings.dry_run,
    )

    if not jobs:
        console.print(
            f"  [s.dim]nenhum documento em {settings.input_dir}. "
            f"Formatos aceitos: {', '.join(sorted(INPUT_SUFFIXES))}[/]\n"
        )
        return 0

    with Ledger(settings.ledger_path) as ledger, ui.RunView(len(jobs)) as view:
        try:
            report = pipeline.run(
                settings,
                toolchain,
                jobs=jobs,
                ledger=ledger,
                on_start=view.begin,
                on_finish=view.finish,
            )
        except KeyboardInterrupt:
            console.print("\n  [s.warn]interrompido — nenhum arquivo foi deixado pela metade[/]\n")
            return 130

    ui.summary(
        report.outcomes,
        elapsed=report.elapsed,
        report_path=report.report_path,
        verify_enabled=settings.verify,
    )
    return 1 if report.failed else 0


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


# --------------------------------------------------------------------------- #
# abrir — interface para quem não usa terminal
# --------------------------------------------------------------------------- #


@app.command("abrir")
def open_command(
    config: ConfigOpt = None,
    port: Annotated[Optional[int], typer.Option("--porta", min=1024, max=65535)] = None,
    no_browser: Annotated[
        bool, typer.Option("--sem-navegador", help="Não abre o navegador sozinho.")
    ] = False,
) -> None:
    """Abre a interface gráfica do Scriptor."""
    from .web import launch

    settings = _settings(config)
    server, url = launch(settings, port=port, open_browser=not no_browser)

    console.print()
    console.print("  [s.brand]SCRIPTOR[/]  [s.dim]interface[/]")
    divider()
    console.print(f"  [s.label]endereço  [/][s.path]{url}[/]")
    console.print(f"  [s.label]workspace [/][s.path]{settings.workspace}[/]")
    console.print("  [s.dim]feche esta janela para encerrar[/]\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        console.print("  [s.dim]encerrado[/]\n")


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #


@app.command("doctor")
def doctor_command(
    config: ConfigOpt = None,
    languages: LangOpt = None,
) -> None:
    """Diagnostica o ambiente e diz exatamente o que corrigir."""
    from rich.table import Table

    console.print()
    console.print("  [s.brand]SCRIPTOR[/]  [s.dim]diagnóstico[/]")
    divider()

    rows: list[tuple[str, str, str, str]] = []
    problems: list[str] = []

    rows.append(("ok", "Python", f"{sys.version.split()[0]}", str(Path(sys.executable).parent)))

    try:
        import ocrmypdf

        rows.append(("ok", "OCRmyPDF", ocrmypdf.__version__, ""))
    except ImportError:
        rows.append(("err", "OCRmyPDF", "ausente", ""))
        problems.append("Instale com: pip install ocrmypdf")

    try:
        settings = _settings(config, languages=languages)
    except ScriptorError as exc:
        _report_error(exc)
        raise typer.Exit(3) from exc

    origin = settings.source or "padrões internos (nenhum scriptor.toml encontrado)"
    rows.append(("ok", "Configuração", str(origin), ""))

    toolchain: Toolchain | None = None
    try:
        toolchain = _toolchain(settings)
    except ScriptorError as exc:
        rows.append(("err", "Ferramentas", str(exc), ""))
        problems.append(exc.remedy)

    if toolchain is not None:
        rows.append(
            (
                "ok",
                "Tesseract",
                toolchain.tesseract.version,
                f"{toolchain.tesseract.path}  ({toolchain.tesseract.origin})",
            )
        )
        rows.append(
            (
                "ok",
                "Ghostscript",
                toolchain.ghostscript.version,
                f"{toolchain.ghostscript.path}  ({toolchain.ghostscript.origin})",
            )
        )
        wanted = set(settings.languages)
        missing = wanted - toolchain.languages
        status = "err" if missing else "ok"
        detail = ", ".join(sorted(toolchain.languages)) or "nenhum"
        rows.append(
            (
                status,
                "Idiomas",
                ", ".join(sorted(wanted)),
                f"disponíveis: {detail}  ({toolchain.tessdata_origin})",
            )
        )
        if missing:
            problems.append(f"Idioma(s) ausente(s): {', '.join(sorted(missing))}")

        if toolchain.verapdf:
            rows.append(("ok", "veraPDF", toolchain.verapdf.version, str(toolchain.verapdf.path)))
        else:
            rows.append(
                (
                    "warn",
                    "veraPDF",
                    "não instalado",
                    "a verificação usa a checagem interna, mais superficial",
                )
            )

    for name, directory in (
        ("Entrada", settings.input_dir),
        ("Saída", settings.output_dir),
        ("Estado", settings.state_dir),
    ):
        exists = directory.exists()
        writable = _writable(directory if exists else directory.parent)
        status = "ok" if writable else "err"
        note = "" if exists else "será criada"
        detail = note if writable else "sem permissão de escrita"
        rows.append((status, name, str(directory), detail))
        if not writable:
            problems.append(f"Sem permissão de escrita em {directory}")

    table = Table.grid(padding=(0, 2))
    table.add_column(width=3)
    table.add_column(style="s.label", width=13)
    table.add_column(style="s.value", overflow="fold")
    table.add_column(style="s.dim", overflow="fold")
    for status, name, value, note in rows:
        table.add_row(f"[s.{status}]{glyph(status)}[/]", name, value, note)
    console.print(table)

    if toolchain and toolchain.notes:
        console.print()
        for note in toolchain.notes:
            console.print(f"  [s.dim]{note}[/]")

    divider()
    if problems:
        console.print("  [s.err]a corrigir[/]")
        for problem in problems:
            if problem:
                console.print(f"    [s.dim]· {problem}[/]")
        console.print()
        raise typer.Exit(1)
    console.print(f"  [s.ok]{glyph('ok')}[/] [s.value]ambiente pronto[/]\n")


def _writable(directory: Path) -> bool:
    probe = directory / ".scriptor-probe"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe.touch()
        probe.unlink()
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------- #
# watch
# --------------------------------------------------------------------------- #


@app.command("watch")
def watch_command(
    config: ConfigOpt = None,
    input_dir: InputOpt = None,
    output_dir: OutputOpt = None,
    languages: LangOpt = None,
    interval: Annotated[
        float, typer.Option("--intervalo", min=1.0, help="Segundos entre varreduras.")
    ] = 10.0,
) -> None:
    """Vigia a pasta de entrada e converte o que chegar."""
    import time

    from . import pipeline, ui

    settings = _settings(config, input_dir=input_dir, output_dir=output_dir, languages=languages)
    toolchain = _toolchain(settings)
    settings.ensure_dirs()

    console.print()
    console.print("  [s.brand]SCRIPTOR[/]  [s.dim]vigilância[/]")
    divider()
    console.print(f"  [s.label]entrada [/][s.path]{settings.input_dir}[/]")
    console.print(f"  [s.label]saída   [/][s.path]{settings.output_dir}[/]")
    console.print(f"  [s.dim]varredura a cada {interval:.0f}s · Ctrl+C para encerrar[/]\n")

    processed = 0
    try:
        with Ledger(settings.ledger_path) as ledger:
            while True:
                jobs = pipeline.stable_jobs(settings)
                if jobs:
                    with ui.RunView(len(jobs)) as view:
                        report = pipeline.run(
                            settings,
                            toolchain,
                            jobs=jobs,
                            ledger=ledger,
                            on_start=view.begin,
                            on_finish=view.finish,
                        )
                    processed += len(report.outcomes)
                    console.print(
                        f"  [s.dim]{datetime.now():%H:%M:%S} · lote de {len(jobs)} em "
                        f"{fmt_duration(report.elapsed)} · {processed} no total[/]\n"
                    )
                time.sleep(interval)
    except KeyboardInterrupt:
        console.print(f"\n  [s.dim]encerrado · {processed} documentos processados[/]\n")


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #


@app.command("verificar")
def verify_command(
    target: Annotated[Path, typer.Argument(help="Arquivo PDF ou pasta.")],
    config: ConfigOpt = None,
    pdfa_part: ProfileOpt = None,
) -> None:
    """Verifica a conformidade PDF/A de arquivos já existentes."""
    from rich.table import Table

    from .conformance import verify as verify_pdf

    settings = _settings(config, pdfa_part=pdfa_part)
    verapdf = None
    # A checagem interna não depende da cadeia de ferramentas: sem Tesseract
    # ainda é possível validar conformidade PDF/A de arquivos existentes.
    with contextlib.suppress(ScriptorError):
        verapdf = _toolchain(settings).verapdf

    targets = (
        sorted(p for p in target.rglob("*.pdf") if p.is_file()) if target.is_dir() else [target]
    )
    if not targets:
        console.print(f"\n  [s.dim]nenhum PDF em {target}[/]\n")
        raise typer.Exit(2)

    console.print()
    console.print(
        f"  [s.brand]SCRIPTOR[/]  [s.dim]verificação · "
        f"{settings.pdfa_part.upper().replace('PDFA-', 'PDF/A-')}[/]"
    )
    divider()

    table = Table.grid(padding=(0, 2))
    table.add_column(width=3)
    table.add_column(style="s.value", overflow="ellipsis", max_width=40)
    table.add_column(style="s.dim", overflow="fold")

    failures = 0
    for path in targets:
        report = verify_pdf(path, expected=settings.pdfa_part, verapdf=verapdf)
        status = "ok" if report.ok else "err"
        failures += 0 if report.ok else 1
        table.add_row(
            f"[s.{status}]{glyph(status)}[/]",
            path.name,
            report.label if report.ok else report.detail(),
        )
    console.print(table)
    divider()
    console.print(f"  [s.value]{len(targets) - failures}/{len(targets)}[/] [s.dim]conformes[/]\n")
    raise typer.Exit(1 if failures else 0)


# --------------------------------------------------------------------------- #
# histórico
# --------------------------------------------------------------------------- #


@app.command("historico")
def history_command(
    config: ConfigOpt = None,
    limit: Annotated[int, typer.Option("--limite", "-n", min=1)] = 20,
    status: Annotated[
        Optional[str], typer.Option("--status", help="ok, failed, skipped, rejected.")
    ] = None,
) -> None:
    """Mostra a trilha de auditoria registrada no ledger."""
    from rich.table import Table

    settings = _settings(config)
    if not settings.ledger_path.exists():
        console.print("\n  [s.dim]nenhuma execução registrada ainda[/]\n")
        raise typer.Exit(2)

    with Ledger(settings.ledger_path) as ledger:
        records = ledger.recent(limit, status=status)
        totals = ledger.totals()

    console.print()
    console.print("  [s.brand]SCRIPTOR[/]  [s.dim]histórico[/]")
    divider()

    table = Table.grid(padding=(0, 2))
    table.add_column(style="s.dim", width=16)
    table.add_column(width=3)
    table.add_column(style="s.value", overflow="ellipsis", max_width=32)
    table.add_column(style="s.dim", width=8)
    table.add_column(style="s.dim", overflow="fold")

    marks = {"ok": "ok", "failed": "err", "skipped": "skip", "rejected": "warn"}
    for record in records:
        mark = marks.get(record.status, "skip")
        table.add_row(
            record.created_at[:16].replace("T", " "),
            f"[s.{mark}]{glyph(mark)}[/]",
            Path(record.source_path).name,
            record.mode or "",
            record.conformance or record.detail or "",
        )
    console.print(table)
    divider()
    console.print(
        "  "
        + "   ".join(f"[s.value]{count}[/] [s.dim]{name}[/]" for name, count in totals.items())
        + "\n"
    )


# --------------------------------------------------------------------------- #
# limpar
# --------------------------------------------------------------------------- #


@app.command("limpar")
def clean_command(
    config: ConfigOpt = None,
    what: Annotated[
        str, typer.Option("--o-que", help="saida, processados, falhas, logs ou tudo.")
    ] = "processados",
    older_than: Annotated[
        int, typer.Option("--mais-velho-que", min=0, help="Dias. 0 remove tudo.")
    ] = 30,
    confirm: Annotated[
        bool, typer.Option("--confirmar", help="Sem esta flag, apenas lista o que seria removido.")
    ] = False,
) -> None:
    """Remove arquivos antigos das pastas gerenciadas.

    Substitui o `lixo.bat`, que apagava entrada e saída de uma vez, sem filtro,
    sem confirmação e sem possibilidade de recuperação. Aqui a listagem é o
    comportamento padrão; a remoção exige --confirmar.
    """
    settings = _settings(config)
    groups = {
        "saida": settings.output_dir,
        "processados": settings.archive_dir,
        "falhas": settings.failed_dir,
        "logs": settings.log_dir,
    }
    selected = list(groups) if what == "tudo" else [w.strip() for w in what.split(",")]
    unknown = [name for name in selected if name not in groups]
    if unknown:
        err_console.print(f"[s.err]alvo desconhecido: {', '.join(unknown)}[/]")
        raise typer.Exit(2)

    cutoff = datetime.now() - timedelta(days=older_than)
    victims: list[tuple[Path, int]] = []
    for name in selected:
        directory = groups[name]
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if not path.is_file():
                continue
            stat = path.stat()
            if older_than and datetime.fromtimestamp(stat.st_mtime) > cutoff:
                continue
            victims.append((path, stat.st_size))

    console.print()
    console.print("  [s.brand]SCRIPTOR[/]  [s.dim]limpeza[/]")
    divider()
    if not victims:
        console.print("  [s.dim]nada a remover[/]\n")
        raise typer.Exit(0)

    total = sum(size for _, size in victims)
    for path, size in victims[:20]:
        console.print(f"  [s.dim]{path}[/]  [s.dim]{fmt_bytes(size)}[/]")
    if len(victims) > 20:
        console.print(f"  [s.dim]… e mais {len(victims) - 20}[/]")

    divider()
    console.print(
        f"  [s.value]{len(victims)}[/] [s.dim]arquivos ·[/] [s.value]{fmt_bytes(total)}[/]"
    )

    # O aviso precisa vir antes da decisão, não depois: alertar sobre o que já
    # foi apagado não é aviso, é epitáfio.
    if "saida" in selected:
        console.print(
            "  [s.warn]atenção: a pasta de saída contém os PDF/A gerados, "
            "não cópias descartáveis.[/]"
        )

    if not confirm:
        console.print("  [s.dim]simulação. Adicione --confirmar para remover.[/]\n")
        raise typer.Exit(0)

    removed = 0
    for path, _ in victims:
        try:
            path.unlink()
            removed += 1
        except OSError as exc:
            err_console.print(f"  [s.err]{path}: {exc}[/]")
    console.print(f"  [s.ok]{glyph('ok')}[/] [s.value]{removed} removidos[/]\n")


# --------------------------------------------------------------------------- #
# init
# --------------------------------------------------------------------------- #


@app.command("init")
def init_command(
    directory: Annotated[Path, typer.Argument(help="Pasta do workspace.")] = Path("."),
) -> None:
    """Cria o scriptor.toml e a estrutura de pastas."""
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    config_path = directory / CONFIG_FILENAME

    console.print()
    console.print("  [s.brand]SCRIPTOR[/]  [s.dim]novo workspace[/]")
    divider()

    if config_path.exists():
        console.print(f"  [s.warn]{glyph('warn')}[/] [s.dim]{CONFIG_FILENAME} já existe[/]")
    else:
        config_path.write_text(TEMPLATE, encoding="utf-8")
        console.print(f"  [s.ok]{glyph('ok')}[/] [s.value]{config_path}[/]")

    settings = load_settings(config_path)
    settings.ensure_dirs()
    for directory_path in (settings.input_dir, settings.output_dir, settings.archive_dir):
        console.print(f"  [s.ok]{glyph('ok')}[/] [s.path]{directory_path}[/]")

    divider()
    console.print("  [s.dim]coloque os documentos em Entrada e rode:[/] [s.value]scriptor run[/]\n")


# --------------------------------------------------------------------------- #
# inspecionar
# --------------------------------------------------------------------------- #


@app.command("inspecionar")
def inspect_command(
    target: Annotated[Path, typer.Argument(help="Arquivo PDF.")],
    config: ConfigOpt = None,
) -> None:
    """Mostra o perfil de um documento e a estratégia que seria aplicada."""
    from rich.table import Table

    from .strategy import plan as make_plan

    settings = _settings(config)
    profile = analyze(target)
    decision = make_plan(profile, settings)

    console.print()
    console.print(f"  [s.brand]{target.name}[/]")
    divider()

    table = Table.grid(padding=(0, 2))
    table.add_column(style="s.label", width=14)
    table.add_column(style="s.value", overflow="fold")
    table.add_row("tamanho", fmt_bytes(profile.size_bytes))
    table.add_row("sha256", profile.sha256[:32] + "…")
    table.add_row("páginas", str(profile.pages) + (" (amostrado)" if profile.sampled else ""))
    table.add_row("natureza", profile.nature(settings.text_threshold))
    table.add_row(
        "com texto",
        f"{profile.pages_with_text(settings.text_threshold)}/{len(profile.page_text_chars)}"
        f"  (limiar {settings.text_threshold} caracteres/página)",
    )
    table.add_row("assinado", "sim" if profile.signed else "não")
    table.add_row("criptografado", "sim" if profile.encrypted or profile.restricted else "não")
    table.add_row("PDF/A", profile.pdfa_part or "não")
    console.print(table)

    divider()
    color = {"convert": "s.ok", "skip": "s.skip", "reject": "s.err"}[decision.decision]
    console.print(f"  [{color}]{decision.decision}[/]  [s.dim]{decision.reason}[/]")
    for index, attempt in enumerate(decision.attempts, 1):
        console.print(f"    [s.dim]{index}. {attempt.label} — {attempt.rationale}[/]")
    console.print()


# --------------------------------------------------------------------------- #
# entrada
# --------------------------------------------------------------------------- #


@app.callback(invoke_without_command=True)
def main_callback(
    ctx: typer.Context,
    version: Annotated[
        bool, typer.Option("--version", "-V", help="Mostra a versão e sai.")
    ] = False,
) -> None:
    if version:
        console.print(f"scriptor {__version__}")
        raise typer.Exit(0)
    if ctx.invoked_subcommand is None:
        # Sem subcomando, o verbo esperado é converter.
        ctx.invoke(run_command)


def _report_error(exc: ScriptorError) -> None:
    err_console.print(f"\n  [s.err]{glyph('err')}[/] [s.value]{exc.message}[/]")
    if exc.remedy:
        err_console.print(f"    [s.dim]{exc.remedy}[/]")
    err_console.print()


def main() -> None:
    try:
        app()
    except ScriptorError as exc:
        _report_error(exc)
        sys.exit(3)
    except KeyboardInterrupt:
        err_console.print("\n  [s.dim]interrompido[/]\n")
        sys.exit(130)


if __name__ == "__main__":
    main()
