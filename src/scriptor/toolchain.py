"""Descoberta e provisionamento das dependências externas.

O kit original acompanhava dois documentos de troubleshooting — "Resolver problema
do Path no Tesseract OCR" e "Instalar o Idioma Português no Tesseract" — porque o
script em lote assumia que ``tesseract`` e ``gswin64c`` estariam no ``PATH`` e que
``por.traineddata`` já estaria dentro de ``Program Files``. Nenhuma das duas coisas
é verdade numa instalação limpa do Windows.

Este módulo elimina as duas apostilas:

* localiza os binários no ``PATH``, no registro do Windows e nos diretórios de
  instalação usuais, nessa ordem;
* provisiona idiomas ausentes a partir dos ``.traineddata`` que acompanham o kit,
  sem exigir privilégio de administrador — se ``Program Files`` for somente
  leitura, monta um espelho de ``tessdata`` no perfil do usuário;
* devolve um overlay de ambiente que é injetado em todo subprocesso.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from .errors import LanguageError, ToolchainError

IS_WINDOWS = sys.platform == "win32"

_VERSION_RE = re.compile(r"(\d+(?:\.\d+){1,3})")

# Executáveis candidatos, em ordem de preferência.
_TESSERACT_BINARIES = ("tesseract",)
_GHOSTSCRIPT_BINARIES = ("gswin64c", "gswin32c", "gs") if IS_WINDOWS else ("gs",)


@dataclass(frozen=True, slots=True)
class Tool:
    """Uma dependência externa resolvida."""

    name: str
    path: Path
    version: str
    origin: str  # config | PATH | registro | instalação padrão

    @property
    def version_tuple(self) -> tuple[int, ...]:
        match = _VERSION_RE.search(self.version)
        if not match:
            return ()
        return tuple(int(p) for p in match.group(1).split("."))


@dataclass(slots=True)
class Toolchain:
    """Conjunto completo de dependências resolvidas, pronto para execução."""

    tesseract: Tool
    ghostscript: Tool
    languages: frozenset[str]
    tessdata_dir: Path
    tessdata_origin: str
    verapdf: Tool | None = None
    notes: list[str] = field(default_factory=list)

    def env(self) -> dict[str, str]:
        """Ambiente para os subprocessos: PATH ampliado e TESSDATA_PREFIX fixado.

        Retorna uma cópia — nunca mutamos ``os.environ`` do processo pai.
        """
        env = dict(os.environ)
        extra = [str(self.tesseract.path.parent), str(self.ghostscript.path.parent)]
        current = env.get("PATH", "")
        prefix = os.pathsep.join(p for p in extra if p and p not in current)
        env["PATH"] = f"{prefix}{os.pathsep}{current}" if prefix else current
        env["TESSDATA_PREFIX"] = str(self.tessdata_dir)
        # Tesseract paraleliza internamente; a concorrência é nossa, não dele.
        env.setdefault("OMP_THREAD_LIMIT", "1")
        return env

    def fingerprint(self) -> str:
        """Identidade das ferramentas — entra na receita para invalidar o cache."""
        parts = [f"tesseract={self.tesseract.version}", f"gs={self.ghostscript.version}"]
        return " ".join(parts)


# --------------------------------------------------------------------------- #
# Sondagem de binários
# --------------------------------------------------------------------------- #


def _run_version(exe: Path) -> str | None:
    """Executa ``--version`` e devolve a primeira linha útil, ou None."""
    try:
        proc = subprocess.run(  # noqa: S603 - caminho resolvido, argv explícito
            [str(exe), "--version"],
            capture_output=True,
            timeout=20,
            check=False,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    raw = (proc.stdout or proc.stderr).decode("utf-8", "replace")
    for line in raw.splitlines():
        line = line.strip()
        if line:
            return line
    return None


_NO_WINDOW = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0


def _registry_paths(subkeys: Iterable[str]) -> Iterator[Path]:
    """Diretórios de instalação declarados no registro do Windows."""
    if not IS_WINDOWS:
        return
    import winreg

    hives = (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER)
    views = (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY)
    for hive in hives:
        for view in views:
            for subkey in subkeys:
                try:
                    with winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ | view) as key:
                        yield from _registry_key_paths(key, winreg)
                except OSError:
                    continue


def _registry_key_paths(key, winreg) -> Iterator[Path]:
    """Valores da chave, mais um nível de subchaves (Ghostscript versiona assim)."""
    for value_name in ("", "Path", "InstallDir", "InstallLocation", "GS_DLL"):
        try:
            value, _ = winreg.QueryValueEx(key, value_name)
        except OSError:
            continue
        if isinstance(value, str) and value.strip():
            candidate = Path(value.strip().strip('"'))
            yield candidate.parent if candidate.suffix else candidate

    index = 0
    while True:
        try:
            child_name = winreg.EnumKey(key, index)
        except OSError:
            return
        index += 1
        try:
            with winreg.OpenKey(key, child_name) as child:
                yield from _registry_key_paths(child, winreg)
        except OSError:
            continue


def _well_known_dirs(patterns: Iterable[str]) -> Iterator[Path]:
    """Globs sobre os diretórios de instalação típicos."""
    roots: list[Path] = []
    if IS_WINDOWS:
        for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "LOCALAPPDATA"):
            value = os.environ.get(var)
            if value:
                roots.append(Path(value))
                roots.append(Path(value) / "Programs")
    else:
        roots += [Path("/usr/bin"), Path("/usr/local/bin"), Path("/opt/homebrew/bin")]

    seen: set[Path] = set()
    for root in roots:
        if root in seen or not root.is_dir():
            continue
        seen.add(root)
        for pattern in patterns:
            try:
                yield from root.glob(pattern)
            except OSError:
                continue


def _resolve_tool(
    name: str,
    binaries: Iterable[str],
    *,
    override: Path | None,
    registry_keys: Iterable[str],
    glob_patterns: Iterable[str],
    remedy: str,
    required: bool = True,
) -> Tool | None:
    """Escada de descoberta: config → PATH → registro → instalação padrão."""
    binaries = tuple(binaries)

    def _try(path: Path, origin: str) -> Tool | None:
        if not path.is_file():
            return None
        version = _run_version(path)
        if version is None:
            return None
        return Tool(name=name, path=path.resolve(), version=version, origin=origin)

    if override is not None:
        candidate = Path(override).expanduser()
        if candidate.is_dir():
            for binary in binaries:
                found = _try(candidate / _exe(binary), "config")
                if found:
                    return found
        found = _try(candidate, "config")
        if found:
            return found
        raise ToolchainError(
            f"{name}: caminho configurado não é executável — {override}",
            remedy=f"Corrija a chave em scriptor.toml ou remova-a para autodetectar. {remedy}",
        )

    for binary in binaries:
        which = shutil.which(binary)
        if which:
            found = _try(Path(which), "PATH")
            if found:
                return found

    searched: set[Path] = set()
    for directory in _registry_paths(registry_keys):
        for base in (directory, directory / "bin"):
            if base in searched:
                continue
            searched.add(base)
            for binary in binaries:
                found = _try(base / _exe(binary), "registro")
                if found:
                    return found

    for candidate in _well_known_dirs(glob_patterns):
        found = _try(candidate, "instalação padrão")
        if found:
            return found

    if not required:
        return None
    raise ToolchainError(f"{name} não encontrado", remedy=remedy)


def _exe(stem: str) -> str:
    return f"{stem}.exe" if IS_WINDOWS else stem


def find_tesseract(override: Path | None = None) -> Tool:
    return _resolve_tool(
        "Tesseract",
        _TESSERACT_BINARIES,
        override=override,
        registry_keys=(
            r"SOFTWARE\Tesseract-OCR",
            r"SOFTWARE\WOW6432Node\Tesseract-OCR",
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Tesseract-OCR",
        ),
        glob_patterns=("Tesseract-OCR/tesseract.exe", "Tesseract*/tesseract.exe"),
        remedy=(
            r"Instale o Tesseract com instaladores\tesseract-ocr-w64-setup-*.exe, ou aponte "
            r'`tesseract = "C:/caminho/tesseract.exe"` no scriptor.toml. '
            "Não é necessário mexer no PATH do Windows."
        ),
    )  # type: ignore[return-value]


def find_ghostscript(override: Path | None = None) -> Tool:
    return _resolve_tool(
        "Ghostscript",
        _GHOSTSCRIPT_BINARIES,
        override=override,
        registry_keys=(
            r"SOFTWARE\GPL Ghostscript",
            r"SOFTWARE\Artifex\GPL Ghostscript",
            r"SOFTWARE\WOW6432Node\GPL Ghostscript",
        ),
        glob_patterns=("gs/gs*/bin/gswin64c.exe", "gs/gs*/bin/gswin32c.exe"),
        remedy=(
            r"Instale o Ghostscript com instaladores\gs*w64.exe. Ele é quem produz o "
            "PDF/A — sem ele só é possível gerar PDF comum."
        ),
    )  # type: ignore[return-value]


def find_verapdf(override: Path | None = None) -> Tool | None:
    """Validador PDF/A independente. Opcional, mas é o padrão-ouro."""
    return _resolve_tool(
        "veraPDF",
        ("verapdf",),
        override=override,
        registry_keys=(),
        glob_patterns=("verapdf/verapdf.bat", "veraPDF/verapdf.bat"),
        remedy="",
        required=False,
    )


# --------------------------------------------------------------------------- #
# Idiomas do Tesseract
# --------------------------------------------------------------------------- #


def system_tessdata(tesseract: Tool) -> Path:
    """Diretório ``tessdata`` da instalação, respeitando TESSDATA_PREFIX."""
    env_prefix = os.environ.get("TESSDATA_PREFIX")
    if env_prefix:
        prefix = Path(env_prefix)
        # Tesseract 4 apontava para o pai; Tesseract 5, para o próprio tessdata.
        if (prefix / "tessdata").is_dir():
            return prefix / "tessdata"
        if prefix.is_dir():
            return prefix
    return tesseract.path.parent / "tessdata"


def installed_languages(tessdata: Path) -> frozenset[str]:
    """Idiomas disponíveis, lidos do disco.

    Ler o diretório é mais confiável que ``tesseract --list-langs``: não depende
    de qual ``TESSDATA_PREFIX`` o binário herdou do ambiente do usuário.
    """
    if not tessdata.is_dir():
        return frozenset()
    return frozenset(p.stem for p in tessdata.glob("*.traineddata"))


def _traineddata_search_roots(extra: Iterable[Path]) -> Iterator[Path]:
    yield from (Path(p) for p in extra)
    yield Path.cwd()
    # A raiz do kit: o pacote vive em <kit>/scriptor/src/scriptor.
    package_root = Path(__file__).resolve()
    yield from package_root.parents[:5]


def locate_traineddata(lang: str, extra_dirs: Iterable[Path] = ()) -> Path | None:
    """Procura ``<lang>.traineddata`` no kit, sem descer em árvores profundas."""
    filename = f"{lang}.traineddata"
    seen: set[Path] = set()
    for root in _traineddata_search_roots(extra_dirs):
        if not root.is_dir() or root in seen:
            continue
        seen.add(root)
        for candidate in (root / filename, root / "tessdata" / filename):
            if candidate.is_file():
                return candidate
    return None


def _user_tessdata() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "scriptor" / "tessdata"


def _mirror(source: Path, target: Path) -> None:
    """Espelha um arquivo por hardlink; copia se o link não for possível."""
    if target.exists():
        return
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def provision_languages(
    tesseract: Tool,
    wanted: Iterable[str],
    *,
    extra_dirs: Iterable[Path] = (),
) -> tuple[Path, str, frozenset[str], list[str]]:
    """Garante que todos os idiomas pedidos estejam visíveis ao Tesseract.

    Estratégia, em ordem de preferência:

    1. já está instalado — nada a fazer;
    2. o ``.traineddata`` acompanha o kit e ``tessdata`` do sistema é gravável —
       instala lá, beneficiando qualquer outra ferramenta da máquina;
    3. ``tessdata`` é somente leitura (o caso comum, ``Program Files`` sem
       elevação) — monta um espelho no perfil do usuário, com hardlinks para não
       duplicar os 15 MB de cada idioma, e aponta ``TESSDATA_PREFIX`` para ele.

    Retorna ``(tessdata_dir, origem, idiomas_disponíveis, notas)``.
    """
    wanted = [lang for lang in dict.fromkeys(wanted) if lang]
    system_dir = system_tessdata(tesseract)
    available = installed_languages(system_dir)
    notes: list[str] = []

    missing = [lang for lang in wanted if lang not in available]
    if not missing:
        return system_dir, "sistema", available, notes

    sources: dict[str, Path] = {}
    for lang in missing:
        found = locate_traineddata(lang, extra_dirs)
        if found is None:
            raise LanguageError(
                f"Idioma '{lang}' indisponível e nenhum {lang}.traineddata encontrado no kit",
                remedy=(
                    f"Baixe {lang}.traineddata de github.com/tesseract-ocr/tessdata "
                    f"e coloque em {system_dir} ou na raiz do kit."
                ),
            )
        sources[lang] = found

    # 2. Instalação direta, se houver permissão de escrita.
    if _is_writable(system_dir):
        for lang, source in sources.items():
            shutil.copy2(source, system_dir / f"{lang}.traineddata")
            notes.append(f"idioma '{lang}' instalado em {system_dir}")
        return system_dir, "sistema", installed_languages(system_dir), notes

    # 3. Espelho no perfil do usuário.
    mirror_dir = _user_tessdata()
    mirror_dir.mkdir(parents=True, exist_ok=True)
    for entry in system_dir.glob("*.traineddata"):
        _mirror(entry, mirror_dir / entry.name)
    for subdir in ("configs", "tessconfigs"):
        source_sub = system_dir / subdir
        if source_sub.is_dir():
            shutil.copytree(source_sub, mirror_dir / subdir, dirs_exist_ok=True)
    for lang, source in sources.items():
        target = mirror_dir / f"{lang}.traineddata"
        if not target.exists():
            _mirror(source, target)

    notes.append(
        f"tessdata do sistema é somente leitura; espelho do usuário em {mirror_dir} "
        f"(idiomas adicionados: {', '.join(sorted(sources))})"
    )
    return mirror_dir, "espelho do usuário", installed_languages(mirror_dir), notes


def _is_writable(directory: Path) -> bool:
    if not directory.is_dir():
        return False
    probe = directory / ".scriptor-write-probe"
    try:
        probe.touch()
        probe.unlink()
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------- #
# Resolução completa
# --------------------------------------------------------------------------- #


def resolve(
    *,
    languages: Iterable[str],
    tesseract_path: Path | None = None,
    ghostscript_path: Path | None = None,
    verapdf_path: Path | None = None,
    tessdata_dirs: Iterable[Path] = (),
) -> Toolchain:
    """Resolve toda a cadeia de ferramentas ou falha com um remédio concreto."""
    tesseract = find_tesseract(tesseract_path)
    ghostscript = find_ghostscript(ghostscript_path)
    tessdata, origin, available, notes = provision_languages(
        tesseract, languages, extra_dirs=tessdata_dirs
    )
    verapdf = find_verapdf(verapdf_path)

    if tesseract.version_tuple and tesseract.version_tuple[0] < 4:
        notes.append(
            f"Tesseract {tesseract.version} é anterior à engine LSTM; "
            "a qualidade do OCR será sensivelmente pior que na 5.x"
        )

    return Toolchain(
        tesseract=tesseract,
        ghostscript=ghostscript,
        languages=available,
        tessdata_dir=tessdata,
        tessdata_origin=origin,
        verapdf=verapdf,
        notes=notes,
    )
