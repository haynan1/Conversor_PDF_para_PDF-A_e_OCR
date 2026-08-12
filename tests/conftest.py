from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from scriptor import config as config_module
from scriptor.config import Settings
from scriptor.errors import ToolchainError
from scriptor.toolchain import Toolchain, find_ghostscript
from scriptor.toolchain import resolve as resolve_toolchain

from . import fixtures as fx


@pytest.fixture(scope="session")
def ghostscript_path() -> str:
    try:
        return str(find_ghostscript().path)
    except ToolchainError:  # pragma: no cover - ambiente sem Ghostscript
        pytest.skip("Ghostscript indisponível")


@pytest.fixture(scope="session")
def samples(tmp_path_factory: pytest.TempPathFactory, ghostscript_path: str) -> dict[str, Path]:
    """Conjunto de documentos que cobre as naturezas encontradas num lote real."""
    root = tmp_path_factory.mktemp("amostras")

    native = fx.make_text_pdf(root / "nativo.pdf", pages=2, title="Contrato")
    images = fx.rasterize(native, root / "_png", ghostscript=ghostscript_path)

    catalog = {
        "nativo": native,
        "escaneado": fx.make_scanned_pdf(root / "escaneado.pdf", images),
        "carimbado": fx.make_scanned_pdf(root / "carimbado.pdf", images, stamp="Fls. 12"),
        "misto": fx.make_mixed_pdf(root / "misto.pdf", images[:1]),
        "assinado": fx.mark_as_signed(fx.make_text_pdf(root / "assinado.pdf", title="Ata")),
        "protegido": fx.make_encrypted_pdf(root / "protegido.pdf", native),
        "restrito": fx.make_restricted_pdf(root / "restrito.pdf", native),
        "corrompido": fx.make_damaged_pdf(root / "corrompido.pdf"),
        "imagem": fx.make_image(root / "digitalizacao.png", images[0]),
        "branco": fx.make_blank_pdf(root / "branco.pdf"),
    }
    return catalog


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / config_module.CONFIG_FILENAME).write_text(
        '[scriptor]\nlanguages = ["por"]\n', encoding="utf-8"
    )
    return tmp_path


@pytest.fixture
def settings(workspace: Path) -> Settings:
    loaded = config_module.load(workspace / config_module.CONFIG_FILENAME)
    loaded.ensure_dirs()
    return loaded


@pytest.fixture(scope="session")
def toolchain() -> Toolchain:
    try:
        return resolve_toolchain(languages=("por",))
    except ToolchainError as exc:  # pragma: no cover
        pytest.skip(f"cadeia de ferramentas indisponível: {exc}")


@pytest.fixture
def populated(settings: Settings, samples: dict[str, Path]) -> Settings:
    """Workspace com a pasta de entrada preenchida, inclusive numa subpasta."""
    for key in ("nativo", "escaneado", "carimbado", "misto", "assinado", "corrompido"):
        shutil.copy2(samples[key], settings.input_dir / samples[key].name)
    nested = settings.input_dir / "2024" / "janeiro"
    nested.mkdir(parents=True)
    shutil.copy2(samples["imagem"], nested / samples["imagem"].name)
    return settings
