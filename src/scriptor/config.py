"""Configuração: arquivo TOML, variáveis de ambiente e flags de linha de comando.

Regra que governa o módulo inteiro: **nenhum caminho absoluto embutido no código**.
O kit original fixava ``C:\\PDF_Automacao\\Entrada`` dentro do ``.bat``, o que
tornava o projeto intransferível. Aqui tudo é relativo a um *workspace* — a pasta
que contém o ``scriptor.toml``, ou o diretório atual quando não há arquivo.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from . import STRATEGY_VERSION
from .errors import ConfigError

CONFIG_FILENAME = "scriptor.toml"
STATE_DIRNAME = "_scriptor"

PDFA_PROFILES = ("pdfa-1", "pdfa-2", "pdfa-3", "pdf")
ON_SUCCESS = ("keep", "archive", "delete")
ON_FAILURE = ("keep", "quarantine")
ON_SIGNED = ("skip", "invalidate")

_ENV_PREFIX = "SCRIPTOR_"


@dataclass(slots=True)
class Settings:
    """Configuração efetiva de uma execução."""

    workspace: Path
    input_dir: Path
    output_dir: Path
    archive_dir: Path
    failed_dir: Path

    # OCR
    languages: tuple[str, ...] = ("por",)
    text_threshold: int = 40
    """Caracteres por página a partir dos quais a página conta como 'já tem texto'.

    O ``--skip-text`` do kit original tratava *qualquer* texto como texto: uma
    página escaneada com um carimbo de 3 caracteres era pulada inteira. O limiar
    separa camada de texto real de resíduo."""

    deskew: bool = False
    rotate_pages: bool = False
    clean: bool = False
    sidecar_text: bool = False
    image_dpi: int = 300
    """Resolução assumida para entradas bitmap sem DPI declarado."""

    # Saída
    pdfa_part: str = "pdfa-2"
    optimize: int = 1
    """0 desliga; 1 é lossless. 2 e 3 recomprimem imagem com perda — inaceitável
    para acervo, disponíveis apenas por escolha explícita."""

    # Execução
    recursive: bool = True
    concurrency: int = 0  # 0 = automático
    jobs_per_file: int = 0  # 0 = automático
    timeout_seconds: int = 1200
    tesseract_timeout: int = 300

    # Política
    on_success: str = "archive"
    on_failure: str = "quarantine"
    on_signed: str = "skip"
    skip_if_pdfa: bool = False
    verify: bool = True
    force: bool = False
    dry_run: bool = False

    # Ferramentas
    tesseract: Path | None = None
    ghostscript: Path | None = None
    verapdf: Path | None = None
    tessdata_dirs: tuple[Path, ...] = ()

    source: Path | None = None
    _explicit: frozenset[str] = field(default_factory=frozenset, repr=False)

    # ---------------------------------------------------------------- paths --

    @property
    def state_dir(self) -> Path:
        return self.workspace / STATE_DIRNAME

    @property
    def ledger_path(self) -> Path:
        return self.state_dir / "ledger.sqlite3"

    @property
    def log_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def report_dir(self) -> Path:
        return self.state_dir / "relatorios"

    def managed_dirs(self) -> tuple[Path, ...]:
        """Diretórios que a varredura de entrada nunca deve reconsumir."""
        return (self.output_dir, self.archive_dir, self.failed_dir, self.state_dir)

    def ensure_dirs(self) -> None:
        for directory in (self.input_dir, self.output_dir, self.state_dir, self.log_dir):
            directory.mkdir(parents=True, exist_ok=True)
        if self.on_success == "archive":
            self.archive_dir.mkdir(parents=True, exist_ok=True)
        if self.on_failure == "quarantine":
            self.failed_dir.mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------------------- receita --

    def recipe(self, toolchain_fingerprint: str = "") -> dict[str, Any]:
        """Tudo que altera o *conteúdo* da saída — e nada mais.

        Caminhos, concorrência e política de arquivamento ficam de fora de
        propósito: mover a pasta de saída não deve invalidar o trabalho já feito.
        """
        return {
            "strategy": STRATEGY_VERSION,
            "languages": list(self.languages),
            "text_threshold": self.text_threshold,
            "pdfa_part": self.pdfa_part,
            "optimize": self.optimize,
            "deskew": self.deskew,
            "rotate_pages": self.rotate_pages,
            "clean": self.clean,
            "sidecar_text": self.sidecar_text,
            "tools": toolchain_fingerprint,
        }

    def recipe_hash(self, toolchain_fingerprint: str = "") -> str:
        payload = json.dumps(self.recipe(toolchain_fingerprint), sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("_explicit", None)
        return {k: (str(v) if isinstance(v, Path) else v) for k, v in data.items()}

    # ------------------------------------------------------------ validação --

    def validate(self) -> None:
        if self.pdfa_part not in PDFA_PROFILES:
            raise ConfigError(
                f"perfil de saída inválido: {self.pdfa_part!r}",
                remedy=f"Use um de: {', '.join(PDFA_PROFILES)}.",
            )
        if self.on_success not in ON_SUCCESS:
            raise ConfigError(
                f"on_success inválido: {self.on_success!r}",
                remedy=f"Use um de: {', '.join(ON_SUCCESS)}.",
            )
        if self.on_failure not in ON_FAILURE:
            raise ConfigError(
                f"on_failure inválido: {self.on_failure!r}",
                remedy=f"Use um de: {', '.join(ON_FAILURE)}.",
            )
        if self.on_signed not in ON_SIGNED:
            raise ConfigError(
                f"on_signed inválido: {self.on_signed!r}",
                remedy=f"Use um de: {', '.join(ON_SIGNED)}.",
            )
        if not 0 <= self.optimize <= 3:
            raise ConfigError(
                f"optimize fora da faixa: {self.optimize}",
                remedy="Use 0 (nenhuma), 1 (sem perda, padrão), 2 ou 3 (com perda).",
            )
        if not self.languages:
            raise ConfigError(
                "nenhum idioma de OCR definido",
                remedy='Defina languages = ["por"] no scriptor.toml.',
            )
        if self.text_threshold < 0:
            raise ConfigError("text_threshold não pode ser negativo", remedy="Use 0 ou mais.")
        if self.timeout_seconds <= 0:
            raise ConfigError("timeout_seconds deve ser positivo", remedy="Use 600, por exemplo.")

        if self.input_dir.resolve() == self.output_dir.resolve():
            raise ConfigError(
                "entrada e saída apontam para a mesma pasta",
                remedy="Separe as pastas: reprocessar a própria saída degrada o acervo.",
            )

    # -------------------------------------------------------------- fábrica --

    def with_overrides(self, **overrides: Any) -> Settings:
        clean = {k: v for k, v in overrides.items() if v is not None}
        explicit = self._explicit | frozenset(clean)
        return replace(self, _explicit=explicit, **clean)

    def was_set(self, name: str) -> bool:
        return name in self._explicit


# --------------------------------------------------------------------------- #
# Carregamento
# --------------------------------------------------------------------------- #


def find_config(start: Path | None = None) -> Path | None:
    """Procura ``scriptor.toml`` no diretório atual, nos pais e no perfil."""
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        candidate = directory / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
    appdata = os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME")
    if appdata:
        candidate = Path(appdata) / "scriptor" / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
    return None


def _coerce_path(value: Any, base: Path) -> Path:
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else (base / path)


_KNOWN_KEYS = {
    "input_dir",
    "output_dir",
    "archive_dir",
    "failed_dir",
    "languages",
    "text_threshold",
    "deskew",
    "rotate_pages",
    "clean",
    "sidecar_text",
    "image_dpi",
    "pdfa_part",
    "optimize",
    "recursive",
    "concurrency",
    "jobs_per_file",
    "timeout_seconds",
    "tesseract_timeout",
    "on_success",
    "on_failure",
    "on_signed",
    "skip_if_pdfa",
    "verify",
    "tesseract",
    "ghostscript",
    "verapdf",
    "tessdata_dirs",
}

_PATH_KEYS = {"input_dir", "output_dir", "archive_dir", "failed_dir"}
_TOOL_KEYS = {"tesseract", "ghostscript", "verapdf"}


def load(
    config_path: Path | None = None,
    *,
    workspace: Path | None = None,
) -> Settings:
    """Monta as configurações a partir do TOML e do ambiente.

    Precedência: flags de CLI (aplicadas depois, por ``with_overrides``) >
    variáveis ``SCRIPTOR_*`` > ``scriptor.toml`` > padrões.
    """
    path = config_path or find_config(workspace)
    if config_path is not None and not config_path.is_file():
        raise ConfigError(
            f"arquivo de configuração não encontrado: {config_path}",
            remedy="Rode `scriptor init` para criar um scriptor.toml comentado.",
        )

    raw: dict[str, Any] = {}
    if path is not None:
        try:
            with path.open("rb") as handle:
                document = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(
                f"{path}: TOML inválido — {exc}",
                remedy="Revise a sintaxe; strings de caminho precisam de aspas.",
            ) from exc
        raw = document.get("scriptor", document)

    root = workspace or (path.parent if path else Path.cwd())
    root = root.resolve()

    unknown = set(raw) - _KNOWN_KEYS
    if unknown:
        raise ConfigError(
            f"{path}: chave(s) desconhecida(s): {', '.join(sorted(unknown))}",
            remedy="Remova ou corrija; erro de digitação em configuração é falha silenciosa.",
        )

    raw.update(_env_overrides())

    values: dict[str, Any] = {}
    for key, value in raw.items():
        if key in _PATH_KEYS:
            values[key] = _coerce_path(value, root)
        elif key in _TOOL_KEYS:
            values[key] = Path(str(value)).expanduser()
        elif key == "tessdata_dirs":
            values[key] = tuple(_coerce_path(v, root) for v in value)
        elif key == "languages":
            values[key] = tuple(str(v) for v in value)
        else:
            values[key] = value

    settings = Settings(
        workspace=root,
        input_dir=values.pop("input_dir", root / "Entrada"),
        output_dir=values.pop("output_dir", root / "Saida"),
        archive_dir=values.pop("archive_dir", root / "Processados"),
        failed_dir=values.pop("failed_dir", root / "Falhas"),
        source=path,
        _explicit=frozenset(raw),
        **values,
    )
    return settings


def _env_overrides() -> dict[str, Any]:
    """``SCRIPTOR_LANGUAGES=por+eng``, ``SCRIPTOR_CONCURRENCY=4``…"""
    overrides: dict[str, Any] = {}
    booleans = {
        "deskew",
        "rotate_pages",
        "clean",
        "sidecar_text",
        "recursive",
        "skip_if_pdfa",
        "verify",
    }
    integers = {
        "text_threshold",
        "optimize",
        "concurrency",
        "jobs_per_file",
        "timeout_seconds",
        "tesseract_timeout",
        "image_dpi",
    }
    for name, value in os.environ.items():
        if not name.startswith(_ENV_PREFIX):
            continue
        key = name[len(_ENV_PREFIX) :].lower()
        if key not in _KNOWN_KEYS:
            continue
        if key == "languages":
            overrides[key] = tuple(part for part in value.replace("+", ",").split(",") if part)
        elif key == "tessdata_dirs":
            overrides[key] = tuple(part for part in value.split(os.pathsep) if part)
        elif key in booleans:
            overrides[key] = value.strip().lower() in {"1", "true", "yes", "on", "sim"}
        elif key in integers:
            try:
                overrides[key] = int(value)
            except ValueError as exc:
                raise ConfigError(
                    f"{name}={value!r} não é um inteiro",
                    remedy="Corrija a variável de ambiente ou remova-a.",
                ) from exc
        else:
            overrides[key] = value
    return overrides


_ASSIGN_RE = re.compile(
    r"^(?P<indent>\s*)(?P<key>[a-z_]+)(?P<pad>\s*)=(?P<space>\s*)"
    r"(?P<value>\[[^\]]*\]|\"[^\"]*\"|[^#\s]+)(?P<trail>.*)$"
)

_SERIALIZABLE = (
    "input_dir",
    "output_dir",
    "archive_dir",
    "failed_dir",
    "recursive",
    "languages",
    "text_threshold",
    "deskew",
    "rotate_pages",
    "clean",
    "sidecar_text",
    "image_dpi",
    "pdfa_part",
    "optimize",
    "verify",
    "concurrency",
    "jobs_per_file",
    "timeout_seconds",
    "on_success",
    "on_failure",
    "on_signed",
    "skip_if_pdfa",
)


def _toml_value(value: Any, *, workspace: Path) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Path):
        try:
            relative = value.resolve().relative_to(workspace)
            text = relative.as_posix() or "."
        except ValueError:
            text = value.as_posix()
        return f'"{text}"'
    if isinstance(value, (tuple, list)):
        inner = ", ".join(f'"{item}"' for item in value)
        return f"[{inner}]"
    return f'"{value}"'


def dump(settings: Settings) -> str:
    """Reescreve o ``scriptor.toml`` preservando comentários e ordem.

    A interface gráfica grava por aqui. Um arquivo de configuração que perde os
    comentários ao ser salvo por um programa deixa de ser editável à mão — e
    passa a exigir a interface para sempre.
    """
    values = {key: getattr(settings, key) for key in _SERIALIZABLE}
    lines: list[str] = []

    for line in TEMPLATE.splitlines():
        match = _ASSIGN_RE.match(line)
        if match is None or match.group("key") not in values:
            lines.append(line)
            continue
        key = match.group("key")
        rendered = _toml_value(values.pop(key), workspace=settings.workspace)
        lines.append(
            f"{match.group('indent')}{key}{match.group('pad')}="
            f"{match.group('space')}{rendered}{match.group('trail')}"
        )

    # Chaves que o template não menciona (ou que foram acrescentadas depois).
    leftovers = [key for key in values if settings.was_set(key)]
    if leftovers:
        lines.append("")
        for key in leftovers:
            lines.append(f"{key} = {_toml_value(values[key], workspace=settings.workspace)}")

    return "\n".join(lines) + "\n"


def save(settings: Settings, path: Path | None = None) -> Path:
    """Grava a configuração de forma atômica."""
    target = path or (settings.source or settings.workspace / CONFIG_FILENAME)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(dump(settings), encoding="utf-8")
    os.replace(temporary, target)
    return target


TEMPLATE = """\
# scriptor.toml — configuração do pipeline de OCR e PDF/A.
#
# Todos os caminhos relativos são resolvidos a partir da pasta deste arquivo.
# Mover a pasta inteira para outra máquina não quebra nada.

[scriptor]

# --- Pastas -----------------------------------------------------------------
input_dir   = "Entrada"      # documentos a processar
output_dir  = "Saida"        # PDF/A gerados
archive_dir = "Processados"  # originais, após conversão bem-sucedida
failed_dir  = "Falhas"       # originais que não puderam ser convertidos
recursive   = true           # varre subpastas e preserva a hierarquia na saída

# --- OCR --------------------------------------------------------------------
languages = ["por"]          # ex.: ["por", "eng"] para documentos bilíngues

# Caracteres por página a partir dos quais a página é considerada "já tem texto".
# Abaixo disso, presume-se resíduo (carimbo, número de página, marca d'água) e a
# página é enviada ao OCR. Era exatamente aqui que o script .bat perdia conteúdo.
text_threshold = 40

deskew        = false   # corrige inclinação da digitalização (altera a imagem)
rotate_pages  = false   # corrige orientação a partir do texto detectado
clean         = false   # remove ruído antes do OCR (requer unpaper)
sidecar_text  = false   # grava um .txt ao lado do PDF, para indexação

# --- Saída ------------------------------------------------------------------
pdfa_part = "pdfa-2"    # pdfa-1 | pdfa-2 | pdfa-3 | pdf (sem conformidade)

# 0 = nenhuma, 1 = sem perda (padrão).
# 2 e 3 recomprimem imagens com perda: mais leve, porém inadequado para acervo.
optimize = 1

verify = true           # confere a conformidade PDF/A do arquivo gerado

# --- Execução ---------------------------------------------------------------
concurrency      = 0     # 0 = automático a partir do número de núcleos
jobs_per_file    = 0     # 0 = automático
timeout_seconds  = 1200  # limite por documento; acima disso, o processo é morto

# --- Política ---------------------------------------------------------------
on_success = "archive"   # keep | archive | delete
on_failure = "quarantine"# keep | quarantine
on_signed  = "skip"      # skip | invalidate
#
# on_signed = "skip" é deliberado: o OCR reescreve o PDF e invalida assinaturas
# digitais. Documento assinado convertido em silêncio é perda jurídica, não ganho
# de arquivamento. Mude para "invalidate" apenas com essa consequência aceita.

skip_if_pdfa = false     # pula arquivos que já declaram conformidade PDF/A

# --- Ferramentas ------------------------------------------------------------
# Detectadas automaticamente (PATH, registro do Windows, Program Files).
# Descomente apenas para forçar um caminho específico.
# tesseract   = "C:/Program Files/Tesseract-OCR/tesseract.exe"
# ghostscript = "C:/Program Files/gs/gs10.05.0/bin/gswin64c.exe"
# verapdf     = "C:/verapdf/verapdf.bat"
"""
