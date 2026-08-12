"""Hierarquia de erros do Scriptor.

Cada erro carrega uma ``remedy``: a próxima ação concreta do operador.
Uma mensagem de erro sem remédio é um beco sem saída.
"""

from __future__ import annotations


class ScriptorError(Exception):
    """Erro base. Sempre carrega um remédio acionável."""

    remedy: str = ""

    def __init__(self, message: str, *, remedy: str = "") -> None:
        super().__init__(message)
        self.message = message
        if remedy:
            self.remedy = remedy

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


class ConfigError(ScriptorError):
    """Configuração ausente, inválida ou contraditória."""


class ToolchainError(ScriptorError):
    """Dependência externa ausente ou inutilizável (Tesseract, Ghostscript…)."""


class LanguageError(ToolchainError):
    """Idioma de OCR solicitado sem ``traineddata`` correspondente."""


# Não há exceções para falhas de documento, de propósito.
#
# Documento protegido, corrompido, assinado ou que esgotou a escada de
# tentativas é resultado esperado de um lote real, não condição excepcional. Um
# lote de mil arquivos termina normalmente com alguns nesse estado, e o operador
# precisa do relatório inteiro. Esses casos viram um `Outcome` com status e
# motivo — nunca uma exceção que interromperia o restante do trabalho.
