"""Scriptor — pipeline de OCR e conversão PDF/A com grau arquivístico."""

from __future__ import annotations

__version__ = "1.0.0"

#: Versão da lógica de decisão (estratégia + escada de fallback).
#: Entra na impressão digital da receita: ao mudar, invalida o cache do ledger
#: e os documentos são reprocessados com o novo comportamento.
STRATEGY_VERSION = 1

__all__ = ["STRATEGY_VERSION", "__version__"]
