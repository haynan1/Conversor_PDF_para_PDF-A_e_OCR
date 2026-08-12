"""Interface local: contrato HTTP e postura de segurança.

Um servidor que grava arquivos no disco da máquina merece o mesmo rigor de um
serviço exposto, mesmo escutando só em 127.0.0.1.
"""

from __future__ import annotations

import base64
import json
import threading
import tomllib
from http.client import HTTPConnection

import pytest

from scriptor.config import Settings
from scriptor.web.server import launch, sanitize_filename


@pytest.fixture
def servidor(settings: Settings):
    server, url = launch(settings, open_browser=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    token = url.rsplit("t=", 1)[1]
    yield host, port, token
    server.shutdown()
    server.server_close()


def _request(host, port, method, path, *, token=None, body=None, headers=None):
    conn = HTTPConnection(host, port, timeout=10)
    all_headers = dict(headers or {})
    if token:
        all_headers["X-Scriptor-Token"] = token
    conn.request(method, path, body=body, headers=all_headers)
    response = conn.getresponse()
    payload = response.read()
    conn.close()
    return response.status, payload


# ----------------------------------------------------------------- segurança --


def test_api_sem_token_e_recusada(servidor) -> None:
    host, port, _ = servidor
    status, _ = _request(host, port, "GET", "/api/estado")
    assert status == 401


def test_api_com_token_errado_e_recusada(servidor) -> None:
    host, port, _ = servidor
    status, _ = _request(host, port, "GET", "/api/estado", token="x" * 32)
    assert status == 401


def test_pagina_sem_credencial_e_recusada(servidor) -> None:
    host, port, _ = servidor
    status, _ = _request(host, port, "GET", "/")
    assert status == 403


def test_host_externo_e_recusado(servidor) -> None:
    """Defesa contra DNS rebinding: o cabeçalho Host precisa ser local."""
    host, port, token = servidor
    status, _ = _request(
        host, port, "GET", "/api/estado", token=token, headers={"Host": "attacker.example"}
    )
    assert status == 403


def test_pagina_entrega_a_interface_com_o_token_injetado(servidor) -> None:
    host, port, token = servidor
    status, body = _request(host, port, "GET", f"/?t={token}")
    assert status == 200
    texto = body.decode("utf-8")
    assert token in texto
    assert "__TOKEN__" not in texto


def test_respostas_trazem_cabecalhos_de_defesa(servidor) -> None:
    host, port, token = servidor
    conn = HTTPConnection(host, port, timeout=10)
    conn.request("GET", "/api/estado", headers={"X-Scriptor-Token": token})
    response = conn.getresponse()
    response.read()
    assert response.getheader("X-Content-Type-Options") == "nosniff"
    assert "default-src 'none'" in (response.getheader("Content-Security-Policy") or "")
    assert response.getheader("Cache-Control") == "no-store"
    conn.close()


def test_escuta_apenas_em_loopback(servidor) -> None:
    host, _, _ = servidor
    assert host == "127.0.0.1"


# ------------------------------------------------------------------- nomes --


@pytest.mark.parametrize(
    "malicioso",
    [
        "../../../../Windows/System32/evil.pdf",
        r"..\..\evil.pdf",
        "C:\\Windows\\notas.pdf",
        "/etc/passwd.pdf",
    ],
)
def test_travessia_de_caminho_e_neutralizada(malicioso: str) -> None:
    limpo = sanitize_filename(malicioso)
    assert "/" not in limpo
    assert "\\" not in limpo
    assert ".." not in limpo


@pytest.mark.parametrize("recusado", ["script.exe", "planilha.xlsx", "nota.pdf.exe", ""])
def test_formatos_fora_da_lista_sao_recusados(recusado: str) -> None:
    with pytest.raises(ValueError):
        sanitize_filename(recusado)


def test_acentos_sao_preservados() -> None:
    assert sanitize_filename("Relatório Anual — 2024.pdf") == "Relatório Anual — 2024.pdf"


# -------------------------------------------------------------------- API --


def test_estado_descreve_fila_motor_e_config(servidor) -> None:
    host, port, token = servidor
    status, body = _request(host, port, "GET", "/api/estado", token=token)
    assert status == 200
    payload = json.loads(body)
    assert {"fila", "motor", "config", "pastas", "formatos"} <= set(payload)


def test_envio_grava_na_entrada(servidor, settings: Settings, samples) -> None:
    host, port, token = servidor
    conteudo = samples["nativo"].read_bytes()
    status, body = _request(
        host,
        port,
        "POST",
        "/api/arquivos",
        token=token,
        body=conteudo,
        headers={"X-Scriptor-Nome": base64.b64encode(b"contrato.pdf").decode()},
    )
    assert status == 200
    assert json.loads(body)["arquivo"] == "contrato.pdf"
    assert (settings.input_dir / "contrato.pdf").read_bytes() == conteudo


def test_envio_nao_sobrescreve_homonimo(servidor, settings: Settings, samples) -> None:
    host, port, token = servidor
    nome = base64.b64encode(b"doc.pdf").decode()
    for _ in range(2):
        _request(
            host,
            port,
            "POST",
            "/api/arquivos",
            token=token,
            body=samples["nativo"].read_bytes(),
            headers={"X-Scriptor-Nome": nome},
        )
    assert (settings.input_dir / "doc.pdf").is_file()
    assert (settings.input_dir / "doc (1).pdf").is_file()


def test_abrir_so_aceita_pastas_conhecidas(servidor) -> None:
    host, port, token = servidor
    status, _ = _request(
        host,
        port,
        "POST",
        "/api/abrir",
        token=token,
        body=json.dumps({"alvo": "C:/Windows"}),
        headers={"Content-Type": "application/json"},
    )
    assert status == 400


def test_preferencia_desconhecida_e_recusada(servidor) -> None:
    host, port, token = servidor
    status, _ = _request(
        host,
        port,
        "POST",
        "/api/config",
        token=token,
        body=json.dumps({"rm_rf": True}),
        headers={"Content-Type": "application/json"},
    )
    assert status == 400


def test_preferencia_invalida_e_recusada(servidor) -> None:
    host, port, token = servidor
    status, body = _request(
        host,
        port,
        "POST",
        "/api/config",
        token=token,
        body=json.dumps({"pdfa_part": "pdfa-42"}),
        headers={"Content-Type": "application/json"},
    )
    assert status == 400
    assert json.loads(body)["remedio"]


def test_preferencia_valida_persiste_no_toml(servidor, settings: Settings) -> None:
    host, port, token = servidor
    status, _ = _request(
        host,
        port,
        "POST",
        "/api/config",
        token=token,
        body=json.dumps({"pdfa_part": "pdfa-1", "sidecar_text": True}),
        headers={"Content-Type": "application/json"},
    )
    assert status == 200
    caminho = settings.workspace / "scriptor.toml"
    gravado = caminho.read_text(encoding="utf-8")

    relido = tomllib.loads(gravado)["scriptor"]
    assert relido["pdfa_part"] == "pdfa-1"
    assert relido["sidecar_text"] is True

    # Os comentários do template precisam sobreviver à gravação pela interface:
    # um arquivo que perde os comentários ao ser salvo por um programa deixa de
    # ser editável à mão.
    assert "# --- Saída" in gravado
    assert "on_signed" in gravado and "perda jurídica" in gravado


def test_converter_sem_fila_nao_inicia(servidor) -> None:
    host, port, token = servidor
    status, body = _request(host, port, "POST", "/api/converter", token=token)
    assert status == 200
    assert json.loads(body)["iniciado"] is False


def test_rota_inexistente_devolve_404(servidor) -> None:
    host, port, token = servidor
    status, _ = _request(host, port, "GET", "/api/inexistente", token=token)
    assert status == 404


# ------------------------------------------------------- integração completa --


@pytest.mark.slow
def test_conversao_completa_pela_interface(servidor, settings: Settings, samples) -> None:
    """Envio, conversão e progresso, exercitando o mesmo caminho do operador."""
    host, port, token = servidor

    for chave, nome in (("escaneado", "escaneado.pdf"), ("nativo", "nativo.pdf")):
        _request(
            host,
            port,
            "POST",
            "/api/arquivos",
            token=token,
            body=samples[chave].read_bytes(),
            headers={"X-Scriptor-Nome": base64.b64encode(nome.encode()).decode()},
        )

    eventos: list[dict] = []
    parado = threading.Event()

    def escutar() -> None:
        conn = HTTPConnection(host, port, timeout=180)
        conn.request("GET", "/api/eventos", headers={"X-Scriptor-Token": token})
        response = conn.getresponse()
        buffer = b""
        while not parado.is_set():
            chunk = response.read(1)
            if not chunk:
                break
            buffer += chunk
            if buffer.endswith(b"\n\n"):
                linha = buffer.decode("utf-8", "replace").strip()
                buffer = b""
                if linha.startswith("data: "):
                    evento = json.loads(linha[6:])
                    eventos.append(evento)
                    if evento.get("tipo") == "lote":
                        parado.set()
        conn.close()

    ouvinte = threading.Thread(target=escutar, daemon=True)
    ouvinte.start()

    _, body = _request(host, port, "POST", "/api/converter", token=token)
    assert json.loads(body)["iniciado"] is True

    assert parado.wait(timeout=180), "o lote não terminou no tempo esperado"
    ouvinte.join(timeout=5)

    tipos = [evento["tipo"] for evento in eventos]
    assert tipos[0] == "inicio"
    assert tipos[-1] == "lote"

    concluidos = [e for e in eventos if e["tipo"] == "fim"]
    assert {e["nome"] for e in concluidos} == {"escaneado.pdf", "nativo.pdf"}
    assert all(e["status"] == "ok" and e["conforme"] for e in concluidos)

    assert (settings.output_dir / "escaneado.pdf").is_file()
    assert (settings.archive_dir / "nativo.pdf").is_file()


# ------------------------------------------------------------------ tema --


def test_interface_define_os_tres_estados_de_tema(servidor) -> None:
    """Escuro é o projeto; claro é escolha explícita; sistema é o padrão."""
    host, port, token = servidor
    _, body = _request(host, port, "GET", f"/?t={token}")
    html = body.decode("utf-8")

    assert 'data-theme="light"' in html
    assert ":root:not([data-theme])" in html, "escolha do operador não venceria o sistema"
    assert "prefers-color-scheme: light" in html
    assert 'localStorage.getItem("scriptor-tema")' in html


def test_tema_e_aplicado_antes_da_primeira_pintura(servidor) -> None:
    """Sem isto, quem escolheu claro vê um lampejo escuro a cada carregamento."""
    host, port, token = servidor
    _, body = _request(host, port, "GET", f"/?t={token}")
    html = body.decode("utf-8")

    inicio_script = html.index("scriptor-tema")
    inicio_body = html.index("<body")
    assert inicio_script < inicio_body


def test_paleta_clara_nao_diverge_entre_escolha_e_sistema(servidor) -> None:
    """Duas definições da mesma paleta é a origem clássica de tema inconsistente."""
    import re

    host, port, token = servidor
    _, body = _request(host, port, "GET", f"/?t={token}")
    html = body.decode("utf-8")

    def tokens(padrao: str) -> dict[str, str]:
        bloco = re.search(padrao, html, re.S).group(1)
        return dict(re.findall(r"(--[a-z-]+):\s*([^;]+);", bloco))

    explicito = tokens(r':root\[data-theme="light"\] \{(.*?)\}')
    do_sistema = tokens(r":root:not\(\[data-theme\]\) \{(.*?)\}")
    assert explicito == do_sistema
    assert explicito["--bg"].strip() != "#100f0d"


# ------------------------------------------------------------- histórico --


def _semear_ledger(settings: Settings, quantidade: int = 3) -> None:
    from scriptor.ledger import Ledger

    with Ledger(settings.ledger_path) as ledger:
        execucao = ledger.start_run(
            recipe_hash="r1", toolchain="t=1", workspace=settings.workspace, settings={}
        )
        for indice in range(quantidade):
            ledger.record(
                execucao,
                source_path=f"doc-{indice}.pdf",
                source_sha256=f"sha{indice}",
                source_bytes=100,
                output_bytes=120,
                recipe_hash="r1",
                status="ok" if indice % 2 == 0 else "failed",
                mode="redo",
                pages=3,
                conformance="PDF/A-2",
                detail="motivo",
            )


def test_historico_devolve_registros_e_totais(servidor, settings: Settings) -> None:
    _semear_ledger(settings, 4)
    host, port, token = servidor
    status, body = _request(host, port, "GET", "/api/historico", token=token)
    payload = json.loads(body)

    assert status == 200
    assert len(payload["registros"]) == 4
    assert payload["totais"] == {"ok": 2, "failed": 2}
    primeiro = payload["registros"][0]
    assert {"quando", "arquivo", "status", "modo", "paginas", "conformidade"} <= set(primeiro)


def test_historico_filtra_por_status(servidor, settings: Settings) -> None:
    _semear_ledger(settings, 4)
    host, port, token = servidor
    _, body = _request(host, port, "GET", "/api/historico?status=failed", token=token)
    registros = json.loads(body)["registros"]

    assert registros
    assert all(r["status"] == "failed" for r in registros)


def test_historico_recusa_status_desconhecido(servidor, settings: Settings) -> None:
    _semear_ledger(settings)
    host, port, token = servidor
    status, _ = _request(host, port, "GET", "/api/historico?status=tudo", token=token)
    assert status == 400


def test_historico_limita_o_resultado(servidor, settings: Settings) -> None:
    _semear_ledger(settings, 10)
    host, port, token = servidor
    _, body = _request(host, port, "GET", "/api/historico?limite=3", token=token)
    assert len(json.loads(body)["registros"]) == 3


def test_historico_teto_protege_contra_limite_absurdo(servidor, settings: Settings) -> None:
    """Consulta sem teto é como o ledger vira um problema de memória."""
    _semear_ledger(settings, 5)
    host, port, token = servidor
    status, body = _request(host, port, "GET", "/api/historico?limite=999999", token=token)
    assert status == 200
    assert len(json.loads(body)["registros"]) == 5


def test_historico_limite_invalido_e_recusado(servidor) -> None:
    host, port, token = servidor
    status, _ = _request(host, port, "GET", "/api/historico?limite=abc", token=token)
    assert status == 400


def test_historico_sem_ledger_devolve_vazio(servidor) -> None:
    host, port, token = servidor
    status, body = _request(host, port, "GET", "/api/historico", token=token)
    assert status == 200
    assert json.loads(body) == {"registros": [], "totais": {}}


def test_historico_exige_credencial(servidor) -> None:
    host, port, _ = servidor
    status, _ = _request(host, port, "GET", "/api/historico")
    assert status == 401
