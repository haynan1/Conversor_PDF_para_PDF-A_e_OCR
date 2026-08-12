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


class DocumentError(ScriptorError):
    """O documento de entrada não pode ser processado como está."""


class EncryptedDocumentError(DocumentError):
    remedy = "Forneça a senha do documento ou remova a proteção antes de reprocessar."


class SignedDocumentError(DocumentError):
    remedy = (
        "OCR invalida assinaturas digitais. Use --on-signed invalidate "
        "apenas se a perda da assinatura for aceitável."
    )


class ConversionError(ScriptorError):
    """Todas as tentativas da escada de fallback falharam."""


class ConformanceError(ScriptorError):
    """A saída não atende ao perfil PDF/A exigido."""
