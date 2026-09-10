"""Пакет агента: наружу отдаются только сама сущность и её тип ошибки.

Интерфейсы (cli.py, web.py) импортируют отсюда и больше ничего про устройство
агента не знают — ни про LLM API, ни про промпты, ни про чтение документов.
"""

from agent.agent import AgentError, DocumentAnalysisAgent
from agent.memory import ConversationMemory

__all__ = ["DocumentAnalysisAgent", "AgentError", "ConversationMemory"]
