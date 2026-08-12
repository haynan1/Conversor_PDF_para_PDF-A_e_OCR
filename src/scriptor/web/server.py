"""Servidor local que serve a interface do operador.

Por que um servidor e um navegador, em vez de uma janela nativa: a barra de
qualidade visual deste projeto não é alcançável com Tkinter, e PySide6
acrescentaria mais de cem megabytes a um kit que já carrega três instaladores.
O navegador já está na máquina e entrega tipografia, animação e layout de
verdade — sem uma linha de dependência nova.

Servidor local com acesso ao sistema de arquivos é superfície de ataque, e o
tratamento aqui é o mesmo que se daria a um serviço exposto:

* escuta apenas em ``127.0.0.1``, nunca em ``0.0.0.0``;
* toda chamada de API exige o cabeçalho ``X-Scriptor-Token`` — cabeçalho
  personalizado obriga preflight CORS, que jamais respondemos, de modo que
  nenhuma página de terceiros consegue acionar a API;
* o cabeçalho ``Host`` é validado, o que fecha o ataque de DNS rebinding;
* o cliente nunca informa caminho: escolhe entre pastas nomeadas do workspace;
* nomes de arquivo enviados são saneados antes de tocar o disco.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue
from typing import Any
from urllib.parse import parse_qs, urlparse

from .. import __version__
from ..analysis import INPUT_SUFFIXES
from ..config import Settings
from ..config import save as save_settings
from ..errors import ScriptorError
from ..ledger import Ledger
from ..toolchain import Toolchain
from ..toolchain import resolve as resolve_toolchain

#: Um lote de digitalização em alta resolução chega perto disto por arquivo.
MAX_UPLOAD_BYTES = 512 * 1024 * 1024

#: Hosts aceitos no cabeçalho ``Host``. Qualquer outro é rebinding.
ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

#: Pastas que o cliente pode pedir para abrir, por nome. Nunca por caminho.
OPENABLE = ("entrada", "saida", "processados", "falhas", "relatorios")

_INTERFACE = Path(__file__).with_name("interface.html")

_UNSAFE_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')

_EVENT_HISTORY = 800


# --------------------------------------------------------------------------- #
# Difusão de eventos
# --------------------------------------------------------------------------- #


class EventBus:
    """Distribui o progresso para todas as abas abertas.

    O histórico é retido para que uma aba aberta no meio de um lote longo receba
    tudo que já aconteceu, em vez de uma tela vazia até o próximo documento.
    """

    def __init__(self) -> None:
        self._subscribers: set[Queue] = set()
        self._history: deque[dict[str, Any]] = deque(maxlen=_EVENT_HISTORY)
        self._lock = threading.Lock()

    def publish(self, event: dict[str, Any]) -> None:
        event.setdefault("t", time.time())
        with self._lock:
            self._history.append(event)
            subscribers = list(self._subscribers)
        for queue in subscribers:
            # Aba lenta ou já fechada não pode segurar o lote.
            with contextlib.suppress(Exception):
                queue.put_nowait(event)

    def reset(self) -> None:
        with self._lock:
            self._history.clear()

    def subscribe(self) -> Queue:
        queue: Queue = Queue(maxsize=2048)
        with self._lock:
            for event in self._history:
                queue.put_nowait(event)
            self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: Queue) -> None:
        with self._lock:
            self._subscribers.discard(queue)


# --------------------------------------------------------------------------- #
# Estado da aplicação
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class _RunState:
    running: bool = False
    total: int = 0
    done: int = 0
    started: float = 0.0


class Studio:
    """Estado compartilhado entre as requisições."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.token = secrets.token_urlsafe(24)
        self.bus = EventBus()
        self.run = _RunState()
        self._lock = threading.Lock()
        self._runner = None
        self.toolchain: Toolchain | None = None
        self.toolchain_problem: dict[str, str] | None = None
        self.refresh_toolchain()

    # ------------------------------------------------------------ ambiente --

    def refresh_toolchain(self) -> None:
        try:
            self.toolchain = resolve_toolchain(
                languages=self.settings.languages,
                tesseract_path=self.settings.tesseract,
                ghostscript_path=self.settings.ghostscript,
                verapdf_path=self.settings.verapdf,
                tessdata_dirs=self.settings.tessdata_dirs,
            )
            self.toolchain_problem = None
        except ScriptorError as exc:
            self.toolchain = None
            self.toolchain_problem = {"mensagem": exc.message, "remedio": exc.remedy}

    # -------------------------------------------------------------- estado --

    def snapshot(self) -> dict[str, Any]:
        from .. import pipeline

        settings = self.settings
        jobs = pipeline.discover(settings)
        workers, cores = pipeline.plan_concurrency(settings, len(jobs))

        return {
            "versao": __version__,
            "workspace": str(settings.workspace),
            "pastas": {
                "entrada": str(settings.input_dir),
                "saida": str(settings.output_dir),
                "processados": str(settings.archive_dir),
                "falhas": str(settings.failed_dir),
                "relatorios": str(settings.report_dir),
            },
            "fila": {
                "total": len(jobs),
                "bytes": sum(_size(job.source) for job in jobs),
                "nomes": [job.relative.as_posix() for job in jobs[:200]],
            },
            "config": {
                "languages": list(settings.languages),
                "pdfa_part": settings.pdfa_part,
                "optimize": settings.optimize,
                "on_success": settings.on_success,
                "on_signed": settings.on_signed,
                "sidecar_text": settings.sidecar_text,
                "deskew": settings.deskew,
                "rotate_pages": settings.rotate_pages,
                "verify": settings.verify,
                "recursive": settings.recursive,
                "text_threshold": settings.text_threshold,
            },
            "paralelismo": {"documentos": workers, "nucleos": cores},
            "motor": self._engine(),
            "execucao": {
                "rodando": self.run.running,
                "total": self.run.total,
                "concluidos": self.run.done,
            },
            "formatos": sorted(INPUT_SUFFIXES),
        }

    def _engine(self) -> dict[str, Any]:
        if self.toolchain is None:
            return {"pronto": False, "problema": self.toolchain_problem}
        chain = self.toolchain
        return {
            "pronto": True,
            "tesseract": {
                "versao": _short_version(chain.tesseract.version),
                "caminho": str(chain.tesseract.path),
                "origem": chain.tesseract.origin,
            },
            "ghostscript": {
                "versao": _short_version(chain.ghostscript.version),
                "caminho": str(chain.ghostscript.path),
                "origem": chain.ghostscript.origin,
            },
            "verapdf": chain.verapdf.version if chain.verapdf else None,
            "idiomas": sorted(chain.languages),
            "tessdata": {"caminho": str(chain.tessdata_dir), "origem": chain.tessdata_origin},
            "observacoes": list(chain.notes),
        }

    # ------------------------------------------------------------ execução --

    def start(self) -> dict[str, Any]:
        from .. import pipeline

        with self._lock:
            if self.run.running:
                return {"iniciado": False, "motivo": "já existe um lote em andamento"}
            if self.toolchain is None:
                return {"iniciado": False, "motivo": "ambiente incompleto"}
            jobs = pipeline.discover(self.settings)
            if not jobs:
                return {"iniciado": False, "motivo": "nenhum documento na entrada"}
            self.run = _RunState(running=True, total=len(jobs), started=time.time())

        self.bus.reset()
        self.bus.publish(
            {
                "tipo": "inicio",
                "total": len(jobs),
                "documentos": [job.relative.as_posix() for job in jobs],
            }
        )
        threading.Thread(target=self._execute, args=(jobs,), daemon=True).start()
        return {"iniciado": True, "total": len(jobs)}

    def _execute(self, jobs) -> None:
        from .. import pipeline

        try:
            self.settings.ensure_dirs()
            with Ledger(self.settings.ledger_path) as ledger:
                report = pipeline.run(
                    self.settings,
                    self.toolchain,
                    jobs=jobs,
                    ledger=ledger,
                    on_start=self._on_start,
                    on_finish=self._on_finish,
                    on_runner=self._bind_runner,
                )
            self.bus.publish(
                {
                    "tipo": "lote",
                    "totais": report.counts,
                    "duracao": round(report.elapsed, 1),
                    "relatorio": str(report.report_path) if report.report_path else None,
                }
            )
        except ScriptorError as exc:
            self.bus.publish({"tipo": "erro", "mensagem": exc.message, "remedio": exc.remedy})
        except Exception as exc:
            self.bus.publish({"tipo": "erro", "mensagem": f"{type(exc).__name__}: {exc}"})
        finally:
            with self._lock:
                self.run.running = False
                self._runner = None

    def _bind_runner(self, runner) -> None:
        with self._lock:
            self._runner = runner

    def _on_start(self, job) -> None:
        self.bus.publish({"tipo": "comeco", "nome": job.relative.as_posix()})

    def _on_finish(self, outcome) -> None:
        with self._lock:
            self.run.done += 1
            done = self.run.done
        conformance = outcome.conformance
        self.bus.publish(
            {
                "tipo": "fim",
                "nome": outcome.job.relative.as_posix(),
                "status": outcome.status,
                "detalhe": outcome.detail,
                "paginas": outcome.pages,
                "modo": outcome.mode,
                "duracao": round(outcome.duration, 1),
                "bytes_origem": outcome.profile.size_bytes if outcome.profile else 0,
                "bytes_saida": outcome.output_bytes or 0,
                "conforme": bool(conformance and conformance.ok),
                "conformidade": conformance.detail() if conformance else "",
                "concluidos": done,
            }
        )

    def cancel(self) -> bool:
        with self._lock:
            runner = self._runner
        if runner is None:
            return False
        runner.cancel()
        self.bus.publish({"tipo": "cancelado"})
        return True

    # ---------------------------------------------------------- preferências --

    def apply_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Valida e persiste as preferências vindas da interface."""
        allowed = {
            "languages": lambda v: tuple(str(x) for x in v),
            "pdfa_part": str,
            "optimize": int,
            "on_success": str,
            "on_signed": str,
            "sidecar_text": bool,
            "deskew": bool,
            "rotate_pages": bool,
            "verify": bool,
            "recursive": bool,
            "text_threshold": int,
        }
        overrides: dict[str, Any] = {}
        for key, value in patch.items():
            if key not in allowed:
                raise ScriptorError(f"preferência desconhecida: {key}", remedy="")
            overrides[key] = allowed[key](value)

        candidate = self.settings.with_overrides(**overrides)
        candidate.validate()  # levanta ConfigError com remédio, se inválido

        language_changed = candidate.languages != self.settings.languages
        self.settings = candidate
        save_settings(candidate)
        if language_changed:
            self.refresh_toolchain()
        return self.snapshot()

    # -------------------------------------------------------------- arquivos --

    def store_upload(self, raw_name: str, payload: bytes) -> str:
        name = sanitize_filename(raw_name)
        self.settings.input_dir.mkdir(parents=True, exist_ok=True)
        target = _unique(self.settings.input_dir / name)
        temporary = target.with_name(f".{target.name}.parcial")
        temporary.write_bytes(payload)
        os.replace(temporary, target)
        return target.name

    def clear_queue(self) -> int:
        """Esvazia a fila de entrada. Só remove o que o próprio operador enviou."""
        from .. import pipeline

        removed = 0
        for job in pipeline.discover(self.settings):
            try:
                job.source.unlink()
                removed += 1
            except OSError:
                pass
        return removed

    def open_folder(self, key: str) -> bool:
        folders = {
            "entrada": self.settings.input_dir,
            "saida": self.settings.output_dir,
            "processados": self.settings.archive_dir,
            "falhas": self.settings.failed_dir,
            "relatorios": self.settings.report_dir,
        }
        target = folders.get(key)
        if target is None:
            return False
        target.mkdir(parents=True, exist_ok=True)
        _reveal(target)
        return True

    def history(self, limit: int = 40) -> list[dict[str, Any]]:
        if not self.settings.ledger_path.exists():
            return []
        with Ledger(self.settings.ledger_path) as ledger:
            return [
                {
                    "quando": record.created_at,
                    "arquivo": Path(record.source_path).name,
                    "status": record.status,
                    "modo": record.mode,
                    "paginas": record.pages,
                    "conformidade": record.conformance,
                    "detalhe": record.detail,
                }
                for record in ledger.recent(limit)
            ]


# --------------------------------------------------------------------------- #
# Utilitários
# --------------------------------------------------------------------------- #


def sanitize_filename(raw: str) -> str:
    """Reduz um nome enviado pelo cliente a algo seguro para o disco."""
    name = Path(raw.replace("\\", "/")).name
    name = _UNSAFE_CHARS.sub("", name).strip().strip(".")
    if not name:
        raise ValueError("nome de arquivo vazio")
    stem, _, suffix = name.rpartition(".")
    if f".{suffix.lower()}" not in INPUT_SUFFIXES:
        raise ValueError(f"formato não aceito: .{suffix}")
    if len(name) > 180:
        name = f"{stem[:170]}.{suffix}"
    return name


def _unique(target: Path) -> Path:
    if not target.exists():
        return target
    for index in range(1, 1000):
        candidate = target.with_name(f"{target.stem} ({index}){target.suffix}")
        if not candidate.exists():
            return candidate
    return target.with_name(f"{target.stem} ({secrets.token_hex(4)}){target.suffix}")


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _short_version(raw: str) -> str:
    for token in raw.replace("v", " ").split():
        if token[:1].isdigit():
            return token
    return raw


def _reveal(path: Path) -> None:
    """Abre a pasta no gerenciador de arquivos do sistema."""
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606 - caminho interno, não vem do cliente
    elif sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)  # noqa: S603, S607
    else:
        subprocess.run(["xdg-open", str(path)], check=False)  # noqa: S603, S607


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"Scriptor/{__version__}"
    sys_version = ""

    studio: Studio  # injetado pela fábrica

    # ------------------------------------------------------------ segurança --

    def _host_is_local(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ALLOWED_HOSTS

    def _authorized(self) -> bool:
        provided = self.headers.get("X-Scriptor-Token", "")
        return secrets.compare_digest(provided, self.studio.token)

    # -------------------------------------------------------------- respostas --

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
            "img-src data:; connect-src 'self'; form-action 'none'; base-uri 'none'",
        )
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _fail(self, status: HTTPStatus, message: str, remedy: str = "") -> None:
        self._json({"erro": message, "remedio": remedy}, status)

    # ---------------------------------------------------------------- rotas --

    def do_GET(self) -> None:
        if not self._host_is_local():
            self._fail(HTTPStatus.FORBIDDEN, "host não autorizado")
            return

        parsed = urlparse(self.path)
        route = parsed.path

        if route in {"/", "/index.html"}:
            token = (parse_qs(parsed.query).get("t") or [""])[0]
            if not secrets.compare_digest(token, self.studio.token):
                self._fail(
                    HTTPStatus.FORBIDDEN,
                    "endereço sem credencial",
                    "Feche esta aba e abra o Scriptor pelo atalho.",
                )
                return
            self._serve_interface()
            return

        if not self._authorized():
            self._fail(HTTPStatus.UNAUTHORIZED, "credencial ausente")
            return

        if route == "/api/estado":
            self._json(self.studio.snapshot())
        elif route == "/api/historico":
            self._json({"registros": self.studio.history()})
        elif route == "/api/eventos":
            self._stream_events()
        else:
            self._fail(HTTPStatus.NOT_FOUND, "rota inexistente")

    def do_POST(self) -> None:
        if not self._host_is_local():
            self._fail(HTTPStatus.FORBIDDEN, "host não autorizado")
            return
        if not self._authorized():
            self._fail(HTTPStatus.UNAUTHORIZED, "credencial ausente")
            return

        route = urlparse(self.path).path
        try:
            if route == "/api/arquivos":
                self._receive_file()
            elif route == "/api/converter":
                self._json(self.studio.start())
            elif route == "/api/cancelar":
                self._json({"cancelado": self.studio.cancel()})
            elif route == "/api/config":
                self._json(self.studio.apply_settings(self._read_json()))
            elif route == "/api/abrir":
                key = str(self._read_json().get("alvo", ""))
                if key not in OPENABLE:
                    self._fail(HTTPStatus.BAD_REQUEST, "pasta desconhecida")
                    return
                self._json({"aberto": self.studio.open_folder(key)})
            elif route == "/api/limpar-fila":
                self._json({"removidos": self.studio.clear_queue()})
            elif route == "/api/reverificar":
                self.studio.refresh_toolchain()
                self._json(self.studio.snapshot())
            else:
                self._fail(HTTPStatus.NOT_FOUND, "rota inexistente")
        except ScriptorError as exc:
            self._fail(HTTPStatus.BAD_REQUEST, exc.message, exc.remedy)
        except ValueError as exc:
            self._fail(HTTPStatus.BAD_REQUEST, str(exc))

    # ------------------------------------------------------------ auxiliares --

    def _serve_interface(self) -> None:
        try:
            html = _INTERFACE.read_text(encoding="utf-8")
        except OSError:
            self._fail(HTTPStatus.INTERNAL_SERVER_ERROR, "interface não encontrada")
            return
        html = html.replace("__TOKEN__", self.studio.token)
        self._send(HTTPStatus.OK, html.encode("utf-8"), "text/html; charset=utf-8")

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > 1_000_000:
            raise ValueError("corpo grande demais")
        payload = self.rfile.read(length)
        try:
            data = json.loads(payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("JSON inválido") from exc
        if not isinstance(data, dict):
            raise ValueError("esperado um objeto JSON")
        return data

    def _receive_file(self) -> None:
        encoded = self.headers.get("X-Scriptor-Nome", "")
        try:
            raw_name = base64.b64decode(encoded).decode("utf-8")
        except Exception as exc:
            raise ValueError("nome de arquivo ilegível") from exc

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ValueError("arquivo vazio")
        if length > MAX_UPLOAD_BYTES:
            raise ValueError(f"arquivo acima do limite de {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")

        payload = _read_exactly(self.rfile, length)
        stored = self.studio.store_upload(raw_name, payload)
        self._json({"arquivo": stored})

    def _stream_events(self) -> None:
        """Server-Sent Events: uma conexão longa por aba aberta."""
        queue = self.studio.bus.subscribe()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True

        try:
            while True:
                try:
                    event = queue.get(timeout=15)
                    payload = json.dumps(event, ensure_ascii=False)
                    self.wfile.write(f"data: {payload}\n\n".encode())
                except Empty:
                    # Comentário SSE: mantém a conexão viva sem poluir o cliente.
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.studio.bus.unsubscribe(queue)

    def log_message(self, *args: Any) -> None:
        return


def _read_exactly(stream, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining > 0:
        chunk = stream.read(min(remaining, 1 << 20))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    if remaining:
        raise ValueError("envio interrompido")
    return b"".join(chunks)


# --------------------------------------------------------------------------- #
# Ciclo de vida
# --------------------------------------------------------------------------- #


def _free_port(preferred: int = 8731) -> int:
    for candidate in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", candidate))
                return probe.getsockname()[1]
            except OSError:
                continue
    raise ScriptorError(
        "nenhuma porta local disponível",
        remedy="Feche outras instâncias do Scriptor e tente novamente.",
    )


def launch(
    settings: Settings,
    *,
    port: int | None = None,
    open_browser: bool = True,
) -> tuple[ThreadingHTTPServer, str]:
    """Sobe o servidor e devolve ``(servidor, url)``."""
    settings.ensure_dirs()
    studio = Studio(settings)

    handler = type("BoundHandler", (Handler,), {"studio": studio})
    server = ThreadingHTTPServer(("127.0.0.1", port or _free_port()), handler)
    server.daemon_threads = True

    url = f"http://127.0.0.1:{server.server_address[1]}/?t={studio.token}"
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    return server, url
