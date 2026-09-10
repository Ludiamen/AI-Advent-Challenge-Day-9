"""DocumentAnalysisAgent — агент с памятью, разбирающий ИТ-расходы по ФСБУ 14/2022.

Агент — самостоятельная сущность, а не обёртка над вызовом API. Наружу он
отдаёт небольшой набор публичных методов, и этого хватает обоим интерфейсам:

    ask(question)                     — вопрос с учётом прошлого разговора
    analyze(file_path)                — разбор документа
    build_pivot_report(items, path)   — сводная таблица в Excel
    history() / clear_history()       — чтение и очистка истории
    switch_model() / switch_session() — смена модели и темы разговора на лету

Что добавилось в этот день — память. Каждая реплика сразу ложится в SQLite, а
при следующем запуске поднимается обратно, поэтому разговор продолжается с того
же места, даже если процесс между репликами останавливали. Разбор документа
тоже попадает в историю — сжатой сводкой, а не полным текстом договора: этого
достаточно, чтобы потом спросить «а почему третья позиция не НМА?».

Скрытым остаётся всё прежнее: адрес провайдера, ключ, формат HTTP-запроса,
системные промпты, чтение PDF/DOCX/XLSX, разбор ответа. Ни `cli.py`, ни `web.py`
не знают, какая модель отвечает и как выглядит запрос к ней.

Обязанности с языковой моделью разделены: она читает документ и отвечает на пять
вопросов «да/нет», а категорию учёта, счёт и амортизацию считает код
(`agent/fsbu.py`) — там нужен воспроизводимый результат, а не правдоподобный.
"""

from __future__ import annotations

import logging
import os
import re
import json
import time
from typing import Any

import httpx
from dotenv import load_dotenv

from agent import (
    catalog, compression, doc_readers, fsbu, memory as memory_module,
    prompts, report, tokens,
)
from agent.doc_readers import DocumentError
from agent.memory import ConversationMemory, MemoryError
from agent.schemas import AnalysisResult, Item

load_dotenv()

log = logging.getLogger("agent")

DEFAULT_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.2"))
DEFAULT_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "4096"))
DEFAULT_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "120"))
DEFAULT_COST_LIMIT = float(os.getenv("COST_LIMIT", fsbu.DEFAULT_COST_LIMIT))
MAX_FILE_SIZE_MB = float(os.getenv("MAX_FILE_SIZE_MB", "10"))
MAX_TEXT_LENGTH = int(os.getenv("MAX_TEXT_LENGTH", doc_readers.MAX_TEXT_LENGTH))
MAX_HISTORY_MESSAGES = int(os.getenv("MAX_HISTORY_MESSAGES", memory_module.DEFAULT_MAX_MESSAGES))
MAX_HISTORY_CHARS = int(os.getenv("MAX_HISTORY_CHARS", memory_module.DEFAULT_MAX_CHARS))

# Что делать, когда разговор перестаёт помещаться в контекст модели.
TRIM = "обрезать"    # выбросить самые давние реплики и продолжить
REFUSE = "отказать"   # остановиться и сказать, что именно не помещается
SEND = "отправить"    # отправить как есть и показать, чем ответит провайдер
OVERFLOW_POLICIES = (TRIM, REFUSE, SEND)
DEFAULT_OVERFLOW = os.getenv("ON_OVERFLOW", TRIM)

# Бесплатные тарифы ограничивают не только число запросов в минуту, но и число
# токенов в минуту. Чем длиннее разговор, тем больше токенов уносит каждая
# реплика — и тем скорее упираешься в потолок. Ждать тут осмысленно: провайдер
# сам подсказывает, через сколько повторить.
RATE_LIMIT_RETRIES = 4
RATE_LIMIT_PAUSE = 8.0

# Сжатие истории (см. agent/compression.py).
COMPRESS = os.getenv("COMPRESS_HISTORY", "1") not in ("0", "нет", "off")
SUMMARIZER_MODEL = os.getenv("SUMMARIZER_MODEL", compression.DEFAULT_SUMMARIZER)
KEEP_LAST = int(os.getenv("KEEP_LAST_MESSAGES", compression.DEFAULT_KEEP_LAST))
COMPRESS_EVERY = int(os.getenv("COMPRESS_EVERY", compression.DEFAULT_COMPRESS_EVERY))
COMPRESS_THRESHOLD = float(os.getenv("COMPRESS_THRESHOLD", compression.DEFAULT_THRESHOLD))
SUMMARY_TOKENS = int(os.getenv("SUMMARY_TOKENS", compression.DEFAULT_SUMMARY_TOKENS))


class AgentError(RuntimeError):
    """Единственный тип ошибки, который агент выпускает наружу.

    Интерфейсам незачем разбираться, что именно случилось — сеть, ключ, формат
    файла, хранилище или ответ модели. Им нужно понятное сообщение.
    """


class DocumentAnalysisAgent:
    """Агент: принимает запрос, помнит разговор, обращается к LLM, отдаёт результат."""

    def __init__(
        self,
        model_key: str = catalog.DEFAULT_MODEL,
        api_key: str | None = None,
        session: str = memory_module.DEFAULT_SESSION,
        memory: ConversationMemory | str | None = None,
        remember: bool = True,
        system_prompt: str = prompts.SYSTEM_ANALYST,
        chat_prompt: str = prompts.SYSTEM_CHAT,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        timeout: float = DEFAULT_TIMEOUT,
        cost_limit: float = DEFAULT_COST_LIMIT,
        max_text_length: int = MAX_TEXT_LENGTH,
        max_history_messages: int = MAX_HISTORY_MESSAGES,
        max_history_chars: int = MAX_HISTORY_CHARS,
        on_overflow: str = DEFAULT_OVERFLOW,
        context_limit: int = 0,
        compress: bool = COMPRESS,
        summarizer_model: str = SUMMARIZER_MODEL,
        keep_last: int = KEEP_LAST,
        compress_every: int = COMPRESS_EVERY,
        compress_threshold: float = COMPRESS_THRESHOLD,
        summary_tokens: int = SUMMARY_TOKENS,
        http_client: httpx.Client | None = None,
    ) -> None:
        """Создаёт агента.

        model_key  — ключ из agent/catalog.py; определяет и адрес, и ключ API;
        session    — имя темы разговора: у каждой своя история;
        memory     — готовое хранилище или путь к файлу базы; None — путь по умолчанию;
        remember   — False полностью отключает память (агент станет как в Day-6);
        api_key    — задаётся явно только в тестах, обычно берётся из окружения;
        on_overflow — что делать, если разговор не помещается в контекст:
                      «обрезать» — выбросить давние реплики и продолжить;
                      «отказать» — остановиться и объяснить, чего не хватает;
                      «отправить» — отправить как есть; провайдер ответит
                      ошибкой, и её будет видно. Режим для опытов, не для работы;
        context_limit — искусственно уменьшенное окно контекста: нужно, чтобы
                      показать переполнение, не тратя настоящие сотни тысяч
                      токенов. 0 означает «взять настоящее окно модели»;
        compress    — сжимать ли давние реплики в выжимку;
        summarizer_model — какая модель делает выжимку (обычно дешёвая);
        keep_last   — сколько последних сообщений остаются дословными;
        compress_every — сколько состарившихся сообщений запускают сжатие;
        compress_threshold — доля контекста, после которой сжатие идёт досрочно;
        http_client — тоже для тестов: клиент с подменённым транспортом.
        """
        if on_overflow not in OVERFLOW_POLICIES:
            raise AgentError(
                f"Неизвестное поведение при переполнении «{on_overflow}». "
                f"Допустимы: {', '.join(OVERFLOW_POLICIES)}."
            )
        self._model = catalog.get(model_key)
        self.api_key = api_key if api_key is not None else self._model.api_key
        self.session = session or memory_module.DEFAULT_SESSION

        self.system_prompt = system_prompt
        self.chat_prompt = chat_prompt
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.cost_limit = cost_limit
        self.max_text_length = max_text_length
        self.max_history_messages = max_history_messages
        self.max_history_chars = max_history_chars
        self.on_overflow = on_overflow
        self.context_limit = context_limit
        self.compress_enabled = compress
        self.keep_last = max(0, keep_last)
        self.compress_every = max(2, compress_every)
        self.compress_threshold = min(0.95, max(0.05, compress_threshold))
        self.summary_tokens = max(50, summary_tokens)
        try:
            self._summarizer = catalog.get(summarizer_model)
        except KeyError as exc:
            raise AgentError(str(exc)) from exc
        # Что произошло при последнем сжатии — интерфейсы это показывают.
        self.last_compression: dict[str, Any] = {}
        self._client = http_client
        # Оценка и факт по последнему запросу — интерфейсы показывают обе.
        self.last_budget: tokens.Budget | None = None
        self._explicit_key = api_key is not None

        self.memory = self._open_memory(memory, remember)
        self.last_usage: dict[str, Any] = {}

    @staticmethod
    def _open_memory(
        memory: ConversationMemory | str | None, remember: bool
    ) -> ConversationMemory | None:
        """Готовит хранилище истории или возвращает None, если память выключена."""
        if not remember:
            return None
        if isinstance(memory, ConversationMemory):
            return memory
        try:
            return ConversationMemory(memory or memory_module.DEFAULT_DB_PATH)
        except MemoryError as exc:
            raise AgentError(str(exc)) from exc

    # --- свойства ------------------------------------------------------------

    @property
    def model_key(self) -> str:
        return self._model.key

    @property
    def model(self) -> str:
        """Идентификатор модели у провайдера — его показывают в интерфейсах."""
        return self._model.api_id

    @property
    def base_url(self) -> str:
        return self._model.base_url

    @property
    def remembers(self) -> bool:
        return self.memory is not None

    @property
    def context_window(self) -> int:
        """Сколько токенов помещается в запрос вместе с ответом.

        Обычно это настоящее окно модели, но его можно искусственно уменьшить
        (context_limit) — так переполнение показывают, не тратя сотни тысяч
        токенов на заполнение настоящего окна.
        """
        реальное = self._model.context_window
        if self.context_limit and self.context_limit < реальное:
            return self.context_limit
        return реальное

    @property
    def reserve(self) -> int:
        """Сколько токенов контекста отложено под ответ модели.

        Это max_tokens, но не больше половины окна. Иначе на маленьких моделях
        получается тупик: у allam-2-7b окно 4096, и запрошенные по умолчанию
        4096 токенов ответа не оставляют под вопрос ни одного токена — запрос
        не отправить вообще. Половина окна — разумный потолок: ответ длиннее
        половины разговора нужен редко.
        """
        return max(1, min(self.max_tokens, self.context_window // 2))

    @property
    def ready(self) -> bool:
        """Готов ли агент работать: есть ли ключ API для выбранной модели."""
        return bool(self.api_key)

    def info(self) -> dict[str, Any]:
        """Сведения об агенте для интерфейсов — без ключа и без промптов."""
        data: dict[str, Any] = {
            "model_key": self._model.key,
            "model": self._model.api_id,
            "model_label": self._model.label,
            "provider": self._model.provider,
            "free": self._model.free,
            "session": self.session,
            "remembers": self.remembers,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "cost_limit": self.cost_limit,
            "max_text_length": self.max_text_length,
            "max_history_messages": self.max_history_messages,
            "supported_formats": list(doc_readers.SUPPORTED),
            "ready": self.ready,
            "models": catalog.describe(),
            "context_window": self.context_window,
            "reserve": self.reserve,
            "real_context_window": self._model.context_window,
            "price_in": self._model.price_in,
            "price_out": self._model.price_out,
            "price_text": self._model.price_text,
            "on_overflow": self.on_overflow,
            "compress": self.compress_enabled,
            "summarizer": self._summarizer.key,
            "keep_last": self.keep_last,
            "compress_every": self.compress_every,
            "compress_threshold": self.compress_threshold,
        }
        if self.memory is not None:
            data["stats"] = self.memory.stats(self.session)
            выжимка = self.memory.latest_summary(self.session)
            data["summary"] = выжимка["content"] if выжимка else ""
            data["summary_generation"] = выжимка["generation"] if выжимка else 0
            data["pending"] = len(self.memory.pending(self.session, self.keep_last))
        return data

    # --- публичный интерфейс: разговор ---------------------------------------

    def ask(self, question: str) -> str:
        """Отвечает на вопрос с учётом прошлых реплик и сохраняет обе.

        Ровно здесь и живёт «сохранение контекста»: перед запросом поднимается
        история сессии, после ответа пара «вопрос — ответ» уходит в базу.
        """
        question = (question or "").strip()
        if not question:
            raise AgentError("Пустой запрос: нечего отправлять.")

        self._maybe_compress()
        история = self._history_for_model()
        история = self._fit_history(история, question)

        messages = [{"role": "system", "content": self.chat_prompt}]
        messages.extend(история)
        messages.append({"role": "user", "content": question})

        log.info(
            "Вопрос (%d символов), из истории взято сообщений: %d, оценка запроса: %d токенов",
            len(question), len(история), tokens.estimate_messages(messages),
        )
        answer = self._call_llm(messages)
        self._remember(question, answer, kind=memory_module.KIND_CHAT)
        return answer

    def estimate(self, text: str) -> int:
        """Оценка числа токенов в тексте для ТЕКУЩЕЙ модели.

        Базовая оценка одинакова для всех, а вот токенизаторы разные: тот же
        русский текст обходится allam-2-7b примерно втрое дороже, чем gpt-oss.
        Поэтому базовая величина домножается на коэффициент модели из каталога.
        """
        return round(tokens.estimate(text) * self._model.token_factor)

    def _estimate_history(self, история: list[dict[str, str]]) -> int:
        """Оценка объёма истории с поправкой на токенизатор текущей модели."""
        if not история:
            return 0
        return sum(
            self.estimate(м.get("content", "")) + tokens.MESSAGE_OVERHEAD for м in история
        )

    def budget(self, question: str = "") -> tokens.Budget:
        """Оценивает, как разговор укладывается в контекст, ДО обращения к модели.

        Считает по частям: системный промпт, история и текущий вопрос. Точное
        число знает только провайдер, но оценка нужна раньше — чтобы решить,
        отправлять ли запрос вообще.
        """
        история = self._history_for_model()
        return tokens.Budget(
            limit=self.context_window,
            reserved=self.reserve,
            system=self.estimate(self.chat_prompt) + tokens.MESSAGE_OVERHEAD,
            history=self._estimate_history(история),
            question=self.estimate(question) + tokens.MESSAGE_OVERHEAD if question else 0,
            overhead=self._model.request_overhead,
        )

    def usage(self) -> dict[str, Any]:
        """Сколько токенов и денег стоила текущая сессия целиком."""
        if self.memory is None:
            return {"messages": 0, "tokens": 0, "cost": 0.0}
        сводка = self.memory.stats(self.session)
        return {
            "session": self.session,
            "messages": сводка["messages"],
            "questions": сводка["questions"],
            "documents": сводка["documents"],
            "prompt_tokens": сводка["prompt_tokens"],
            "completion_tokens": сводка["completion_tokens"],
            "tokens": сводка["tokens"],
            "cost": сводка["cost"],
        }

    def growth(self) -> list[dict[str, Any]]:
        """Как накапливались токены и деньги от обмена к обмену."""
        return self.memory.growth(self.session) if self.memory is not None else []

    def summary(self) -> dict[str, Any] | None:
        """Текущая выжимка сессии со сведениями о том, что в неё вошло."""
        if self.memory is None:
            return None
        return self.memory.latest_summary(self.session)

    def summaries(self) -> list[dict[str, Any]]:
        """Все поколения выжимок — по ним видно, как менялся конспект."""
        return self.memory.summaries(self.session) if self.memory is not None else []

    def compress(self, force: bool = False) -> dict[str, Any]:
        """Сворачивает состарившиеся реплики в выжимку.

        force=True сжимает всё, что накопилось, не дожидаясь порогов — этим
        пользуются команда «сжать» и пересборка конспекта.

        Возвращает сведения о том, что произошло: сколько сообщений свёрнуто,
        сколько токенов занимала эта часть истории и сколько заняла выжимка.
        """
        if self.memory is None or not self.compress_enabled:
            return {"compressed": 0, "reason": "сжатие выключено"}

        ожидающие = self.memory.pending(self.session, self.keep_last)
        if not ожидающие:
            return {"compressed": 0, "reason": "нечего сжимать"}
        if not force and len(ожидающие) < 2:
            return {"compressed": 0, "reason": "слишком мало сообщений"}

        предыдущая = self.memory.latest_summary(self.session)
        было_токенов = sum(
            self.estimate(м["content"]) + tokens.MESSAGE_OVERHEAD for м in ожидающие
        )

        запрос = compression.build_summary_prompt(
            ожидающие,
            предыдущая["content"] if предыдущая else "",
            self.summary_tokens,
        )
        log.info(
            "Сжатие: %d сообщений (~%d токенов) моделью %s",
            len(ожидающие), было_токенов, self._summarizer.api_id,
        )
        # Запас нужен двойной. Во-первых, выжимка, оборванная на полуслове,
        # молча теряет хвост — а хвост это самые свежие факты отрезка.
        # Во-вторых, у моделей семейства gpt-oss часть бюджета уходит на скрытое
        # рассуждение, и при тесном лимите ответ возвращается вовсе пустым.
        предел = max(self.summary_tokens * 6, 3000)
        текст = self._call_llm(
            [
                {"role": "system", "content": compression.SUMMARY_SYSTEM},
                {"role": "user", "content": запрос},
            ],
            model=self._summarizer,
            max_tokens=предел,
            low_effort=True,
        )

        расход = self.last_usage
        обрыв = расход.get("finish_reason") == "length"
        if обрыв:
            log.warning(
                "Выжимка оборвана по пределу длины (%d токенов) — часть сведений "
                "могла потеряться. Увеличьте SUMMARY_TOKENS.",
                предел,
            )
        стало_токенов = self.estimate(compression.render_summary(текст))
        self.memory.save_summary(
            self.session, текст,
            covers_until=ожидающие[-1]["id"],
            messages=len(ожидающие) + (предыдущая["messages"] if предыдущая else 0),
            model=self._summarizer.api_id,
            tokens=расход.get("total_tokens", 0),
            cost=расход.get("cost", 0.0),
        )

        self.last_compression = {
            "compressed": len(ожидающие),
            "tokens_before": было_токенов,
            "tokens_after": стало_токенов,
            "saved": было_токенов - стало_токенов,
            "ratio": (стало_токенов / было_токенов) if было_токенов else 1.0,
            "summarizer": self._summarizer.api_id,
            "spent_tokens": расход.get("total_tokens", 0),
            "spent_cost": расход.get("cost", 0.0),
            "generation": len(self.memory.summaries(self.session)),
            "truncated": обрыв,
        }
        log.info(
            "Сжато: %d сообщений, %d -> %d токенов (осталось %.0f%%)",
            len(ожидающие), было_токенов, стало_токенов,
            self.last_compression["ratio"] * 100,
        )
        return self.last_compression

    def rebuild_summary(self) -> dict[str, Any]:
        """Пересобирает выжимку с нуля из всей сырой истории.

        Накатанный конспект от поколения к поколению теряет детали. Сырые
        сообщения при этом никуда не деваются, поэтому конспект всегда можно
        собрать заново — за один проход и без эффекта испорченного телефона.
        """
        if self.memory is None or not self.compress_enabled:
            return {"compressed": 0, "reason": "сжатие выключено"}
        self.memory.drop_summaries(self.session)
        return self.compress(force=True)

    def history(self, limit: int | None = None) -> list[dict]:
        """Вся история текущей сессии; limit оставляет последние сообщения."""
        if self.memory is None:
            return []
        return self.memory.messages(self.session, limit)

    def clear_history(self) -> int:
        """Стирает историю текущей сессии и возвращает число удалённых сообщений."""
        if self.memory is None:
            return 0
        removed = self.memory.clear(self.session)
        log.info("История сессии «%s» очищена: удалено %d сообщений", self.session, removed)
        return removed

    def sessions(self) -> list[dict]:
        """Список всех сессий в хранилище со сводкой по каждой."""
        return self.memory.sessions() if self.memory is not None else []

    def switch_session(self, session: str) -> None:
        """Переключает тему разговора: у каждой сессии своя история."""
        session = (session or "").strip()
        if not session:
            raise AgentError("Имя сессии не может быть пустым.")
        self.session = session
        log.info("Сессия переключена на «%s»", session)

    def switch_model(self, model_key: str) -> None:
        """Меняет модель, сохраняя разговор.

        История хранится как обычные пары «роль — текст» и от провайдера не
        зависит, поэтому смена модели посреди разговора контекст не рвёт.
        """
        try:
            model = catalog.get(model_key)
        except KeyError as exc:
            raise AgentError(str(exc)) from exc

        self._model = model
        if not self._explicit_key:
            self.api_key = model.api_key
        log.info("Модель переключена на %s (%s)", model.api_id, model.provider)

    # --- публичный интерфейс: документы --------------------------------------

    def analyze(self, file_path: str, file_type: str = "") -> dict[str, Any]:
        """Разбирает документ и возвращает результат классификации словарём."""
        self._check_file(file_path)
        source_file = os.path.basename(file_path)

        # Определение типа и чтение — под одним перехватом: наружу из агента
        # должен выходить только AgentError, каким бы ни был внутренний сбой.
        try:
            kind = file_type or doc_readers.detect_kind(file_path)
            log.info("Получен файл %s, тип %s", source_file, kind)
            text = self._read_document(file_path, kind)
        except DocumentError as exc:
            raise AgentError(str(exc)) from exc
        log.info("Извлечено %d символов текста", len(text))

        return self._analyze_text(text, source_file)

    def analyze_text(self, text: str, source_file: str = "текст из формы") -> dict[str, Any]:
        """То же, что analyze, но текст документа передан напрямую, без файла."""
        text = (text or "").strip()
        if not text:
            raise AgentError("Пустой текст: нечего анализировать.")
        return self._analyze_text(doc_readers.truncate(text, self.max_text_length), source_file)

    def build_pivot_report(self, items: list[Any], output_path: str) -> str:
        """Собирает сводную таблицу Excel по результатам классификации."""
        if not items:
            raise AgentError("Нечего выгружать: список объектов пуст.")
        try:
            path = report.build_pivot_report(items, output_path, self.cost_limit)
        except (ValueError, OSError) as exc:
            raise AgentError(f"Не удалось собрать отчёт: {exc}") from exc
        log.info("Отчёт сохранён: %s", path)
        return path

    # --- приватное: разбор документа -----------------------------------------

    def _analyze_text(self, text: str, source_file: str) -> dict[str, Any]:
        """Общая часть analyze и analyze_text: запрос, разбор, запись в историю."""
        запрос = prompts.analyst_user_prompt(text, source_file)
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": запрос},
        ]
        self.last_budget = tokens.Budget(
            limit=self.context_window,
            reserved=self.reserve,
            system=self.estimate(self.system_prompt) + tokens.MESSAGE_OVERHEAD,
            history=0,
            question=self.estimate(запрос) + tokens.MESSAGE_OVERHEAD,
            overhead=self._model.request_overhead,
        )
        if self.last_budget.overflow and self.on_overflow != SEND:
            raise AgentError(
                f"Документ не помещается в контекст модели {self._model.api_id}: "
                f"{self.last_budget.describe()}. Возьмите модель с большим окном "
                f"или уменьшите MAX_TEXT_LENGTH."
            )
        answer = self._call_llm(messages)
        result = self._parse_analysis(answer, source_file)
        log.info("Классифицировано объектов: %d", len(result.items))

        # Разбор документа тоже становится частью разговора — сжатой сводкой,
        # а не полным текстом договора: она короткая, но её достаточно, чтобы
        # потом спросить «а почему третья позиция не НМА?».
        if result.items:
            self._remember(
                f"Разбери документ «{source_file}» по ФСБУ 14/2022.",
                self._document_digest(result),
                kind=memory_module.KIND_DOCUMENT,
            )
        return result.to_dict()

    @staticmethod
    def _document_digest(result: AnalysisResult) -> str:
        """Короткая сводка разбора для истории — компактнее исходного документа."""
        lines = [
            f"Разобран документ «{result.source_file}» ({result.document_type}). "
            f"Найдено объектов: {len(result.items)} на сумму "
            f"{result.total_cost:,.2f} ₽.".replace(",", " ")
        ]
        for number, item in enumerate(result.items, start=1):
            term = f"{item.term_months} мес." if item.term_months else item.term_type
            line = (
                f"{number}. {item.position} — {item.cost:,.2f} ₽, {term} → "
                f"{item.category} (счёт {item.account})".replace(",", " ")
            )
            if item.monthly_amortization:
                line += f", амортизация {item.monthly_amortization:,.2f} ₽/мес.".replace(",", " ")
            lines.append(line)
        lines.append(f"Лимит стоимости: {result.cost_limit:,.0f} ₽.".replace(",", " "))
        return "\n".join(lines)

    def _check_file(self, file_path: str) -> None:
        """Проверяет существование файла и его размер до всякого чтения."""
        if not os.path.exists(file_path):
            raise AgentError(f"Файл не найден: {file_path}")
        size_mb = os.path.getsize(file_path) / (1024 * 1024)
        if size_mb > MAX_FILE_SIZE_MB:
            raise AgentError(
                f"Файл слишком большой: {size_mb:.1f} МБ при пределе {MAX_FILE_SIZE_MB:.0f} МБ."
            )

    def _read_document(self, file_path: str, kind: str) -> str:
        """Направляет файл нужному читателю по типу."""
        readers = {".pdf": self._read_pdf, ".docx": self._read_docx, ".xlsx": self._read_xlsx}
        reader = readers.get(kind.lower())
        if reader is None:
            raise DocumentError(
                f"Неподдерживаемый формат «{kind}». "
                f"Допустимы: {', '.join(doc_readers.SUPPORTED)}."
            )
        return reader(file_path)

    def _read_pdf(self, file_path: str) -> str:
        return doc_readers.read_pdf(file_path, self.max_text_length)

    def _read_docx(self, file_path: str) -> str:
        return doc_readers.read_docx(file_path, self.max_text_length)

    def _read_xlsx(self, file_path: str) -> str:
        return doc_readers.read_xlsx(file_path, self.max_text_length)

    # --- приватное: память ---------------------------------------------------

    def _history_for_model(self) -> list[dict[str, str]]:
        """Что уходит в запрос вместо полной истории.

        Без сжатия — последние реплики как есть, как было в прежних днях.
        Со сжатием — выжимка давнего отрезка отдельным системным сообщением
        плюс несколько последних реплик дословно. Выжимка помечена явно, чтобы
        модель не приняла пересказ за чью-то реплику.
        """
        if self.memory is None:
            return []
        if not self.compress_enabled:
            return self.memory.context(
                self.session, self.max_history_messages, self.max_history_chars
            )

        сообщения: list[dict[str, str]] = []
        выжимка = self.memory.latest_summary(self.session)
        if выжимка:
            сообщения.append({
                "role": "system",
                "content": compression.render_summary(выжимка["content"]),
            })
        # Дословно уходит всё, что ещё не свёрнуто в выжимку, а не только
        # keep_last: keep_last решает, что не сжимать, а не что отправлять.
        сообщения.extend(
            self.memory.after_summary(self.session, self.max_history_messages)
        )
        return сообщения

    def _maybe_compress(self) -> None:
        """Решает, пора ли сжимать, и сжимает, если пора.

        Два условия, и срабатывает то, что наступит раньше. По числу сообщений —
        чтобы длинная цепочка коротких реплик не копилась бесконечно. По доле
        занятого контекста — потому что один разбор договора может занять больше
        места, чем десяток реплик, и счётчик сообщений этого не заметит.
        """
        if self.memory is None or not self.compress_enabled:
            return

        ожидающие = self.memory.pending(self.session, self.keep_last)
        if len(ожидающие) < 2:
            return

        причина = ""
        if len(ожидающие) >= self.compress_every:
            причина = f"накопилось {len(ожидающие)} состарившихся сообщений"
        else:
            занято = self._estimate_history(self._history_for_model())
            доступно = max(1, self.context_window - self.reserve)
            if занято / доступно >= self.compress_threshold:
                причина = (
                    f"история заняла {занято / доступно:.0%} контекста "
                    f"при пороге {self.compress_threshold:.0%}"
                )

        if причина:
            log.info("Запускается сжатие: %s", причина)
            try:
                self.compress(force=True)
            except AgentError as exc:
                # Сжатие — не главная работа агента: если оно не удалось,
                # разговор продолжается на полной истории, а не падает.
                # Но история при этом остаётся длинной, поэтому предупреждение
                # должно быть заметным: следующий запрос может не пройти по
                # минутному лимиту расхода у провайдера.
                self.last_compression = {"compressed": 0, "error": str(exc)}
                log.warning(
                    "Сжать историю не удалось (%s). Разговор продолжится на полной "
                    "истории — запрос будет крупнее обычного.", exc,
                )

    def _fit_history(self, история: list[dict[str, str]], question: str) -> list[dict[str, str]]:
        """Укладывает историю в контекст модели, выбрасывая самые давние реплики.

        Обрезаются именно старые сообщения: свежие важнее для продолжения
        разговора. Первым в контексте всегда должна оставаться реплика
        пользователя, иначе модель примет чужой ответ за начало беседы.

        Если разговор не помещается даже без истории, обрезать нечего: об этом
        сообщается отдельно, потому что причина другая — слишком большой вопрос
        или слишком маленький контекст модели.
        """
        основа = tokens.Budget(
            limit=self.context_window,
            reserved=self.reserve,
            system=self.estimate(self.chat_prompt) + tokens.MESSAGE_OVERHEAD,
            history=0,
            question=self.estimate(question) + tokens.MESSAGE_OVERHEAD,
            overhead=self._model.request_overhead,
        )

        if основа.overflow and self.on_overflow != SEND:
            self.last_budget = основа
            raise AgentError(
                f"Запрос не помещается в контекст модели {self._model.api_id} даже без "
                f"истории: нужно {tokens.format_tokens(основа.prompt)} токенов, доступно "
                f"{tokens.format_tokens(основа.available)} "
                f"(окно {tokens.format_tokens(основа.limit)} минус "
                f"{tokens.format_tokens(основа.reserved)} на ответ). "
                f"Сократите вопрос, уменьшите max_tokens или возьмите модель "
                f"с большим контекстом."
            )

        подрезанная = list(история)
        while True:
            бюджет = tokens.Budget(
                limit=основа.limit, reserved=основа.reserved, system=основа.system,
                history=self._estimate_history(подрезанная),
                question=основа.question, overhead=основа.overhead,
            )
            if not бюджет.overflow:
                self.last_budget = бюджет
                if len(подрезанная) < len(история):
                    log.warning(
                        "Контекст переполнен: из истории выброшено сообщений %d из %d",
                        len(история) - len(подрезанная), len(история),
                    )
                return подрезанная

            if self.on_overflow == SEND:
                # Намеренно не вмешиваемся: пусть ответит сам провайдер.
                self.last_budget = бюджет
                log.warning(
                    "Контекст переполнен на %d токенов, но запрос отправляется как есть",
                    -бюджет.free,
                )
                return подрезанная

            if self.on_overflow == REFUSE:
                self.last_budget = бюджет
                raise AgentError(
                    f"Разговор не помещается в контекст модели {self._model.api_id}: "
                    f"{бюджет.describe()}. Очистите историю, начните новую сессию или "
                    f"переключитесь на режим «{TRIM}»."
                )

            # Выбрасываем самую давнюю реплику и снова проверяем.
            подрезанная.pop(0)
            while подрезанная and подрезанная[0]["role"] != memory_module.ROLE_USER:
                подрезанная.pop(0)

    def _remember(self, question: str, answer: str, kind: str) -> None:
        """Сохраняет пару «вопрос — ответ». Сбой записи не рушит уже готовый ответ."""
        if self.memory is None:
            return
        расход = self.last_usage
        try:
            self.memory.append_pair(
                self.session, question, answer, self.model, kind,
                prompt_tokens=расход.get("prompt_tokens", 0),
                completion_tokens=расход.get("completion_tokens", 0),
                cost=расход.get("cost", 0.0),
            )
        except MemoryError as exc:
            log.warning("Не удалось сохранить историю: %s", exc)

    # --- приватное: работа с LLM ---------------------------------------------

    def _payload(
        self,
        messages: list[dict[str, str]],
        model: catalog.Model,
        max_tokens: int,
        low_effort: bool = False,
    ) -> dict[str, Any]:
        """Собирает тело запроса в OpenAI-совместимом формате."""
        payload: dict[str, Any] = {
            "model": model.api_id,
            "messages": messages,
            "temperature": self.temperature,
            # Просить у модели больше, чем поместится, бессмысленно: провайдер
            # ответит ошибкой ещё до генерации.
            "max_tokens": max_tokens,
            "stream": False,
        }
        # У DeepSeek V4 режим размышления включён по умолчанию, а в нём
        # temperature не действует. Разбор документа требует предсказуемости,
        # поэтому для DeepSeek режим отключается явно.
        if "api.deepseek.com" in model.base_url:
            payload["thinking"] = {"type": "disabled"}
        # Сжатие истории — работа механическая, глубоко рассуждать над ней не
        # нужно. Замер: с reasoning_effort=low тот же пересказ обошёлся в 38
        # выходных токенов вместо 857.
        if low_effort and model.supports_effort:
            payload["reasoning_effort"] = "low"
        return payload

    def _call_llm(
        self,
        messages: list[dict[str, str]],
        model: catalog.Model | None = None,
        max_tokens: int = 0,
        low_effort: bool = False,
    ) -> str:
        """Единственное место, где происходит HTTP-запрос к LLM.

        model — можно указать другую модель каталога: этим пользуется сжатие,
        которое ходит к отдельной дешёвой модели, не меняя основную.

        Делает одну повторную попытку: сетевые сбои и ответы 429/5xx у
        бесплатных тарифов случаются штатно и обычно проходят сами.
        """
        model = model or self._model
        ключ = self.api_key if model is self._model else model.api_key
        max_tokens = max_tokens or self.reserve

        if not ключ:
            raise AgentError(
                f"Не задан ключ API для модели «{model.key}»: нужна переменная "
                f"{model.env_var}. Скопируйте .env.example в .env и впишите ключ."
            )
        # Ключ уходит в HTTP-заголовок, а тот допускает только ASCII. Лишний
        # символ при копировании иначе обернётся невнятной ошибкой кодировки
        # из недр HTTP-клиента.
        if not ключ.isascii():
            raise AgentError(
                "Ключ API содержит недопустимые символы — вероятно, при копировании "
                "в него попал лишний знак. Проверьте значение в .env."
            )

        payload = self._payload(messages, model, max_tokens, low_effort)
        url = f"{model.base_url.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {ключ}", "Content-Type": "application/json"}

        client = self._client or httpx.Client(timeout=self.timeout)
        own_client = self._client is None
        started = time.monotonic()
        last_error = ""

        try:
            попыток_с_лимитом = 0
            attempt = 0
            while attempt < 2 + RATE_LIMIT_RETRIES:
                attempt += 1
                log.info("Запрос к LLM отправлен (%s, попытка %d)", model.api_id, attempt)
                try:
                    response = client.post(url, headers=headers, json=payload)
                except httpx.HTTPError as exc:
                    last_error = f"сетевая ошибка — {exc}"
                else:
                    if response.status_code in (401, 403):
                        raise AgentError(
                            f"Провайдер {model.provider} отклонил ключ "
                            f"({response.status_code}). Проверьте {model.env_var} в .env."
                        )
                    if response.is_success:
                        elapsed = time.monotonic() - started
                        text = self._extract_answer(response, elapsed, model)
                        log.info("Ответ получен за %.1f с, %d символов", elapsed, len(text))
                        return text

                    # 413 у Groq означает не «слишком длинный контекст», а
                    # «запрос больше, чем осталось в минутном лимите расхода».
                    # Лечится тем же ожиданием, что и 429.
                    if response.status_code in (429, 413):
                        попыток_с_лимитом += 1
                        if попыток_с_лимитом > RATE_LIMIT_RETRIES:
                            raise AgentError(
                                f"Провайдер {model.provider} ограничивает расход: "
                                f"{self._rate_limit_hint(response)} Длинный разговор тратит "
                                f"больше токенов на каждую реплику, поэтому в лимит упираешься "
                                f"быстрее — очистите историю или начните новую сессию."
                            )
                        пауза = self._retry_after(response, попыток_с_лимитом)
                        log.warning(
                            "Достигнут лимит расхода, ждём %.0f с: %s",
                            пауза, self._rate_limit_hint(response),
                        )
                        time.sleep(пауза)
                        continue

                    last_error = f"провайдер вернул {response.status_code}: {response.text[:300]}"

                if attempt >= 2 + попыток_с_лимитом:
                    break
                log.warning("Повтор запроса: %s", last_error)
                time.sleep(2)
        finally:
            if own_client:
                client.close()

        raise AgentError(f"Не удалось получить ответ от модели: {last_error}")

    @staticmethod
    def _retry_after(response: httpx.Response, попытка: int) -> float:
        """Сколько ждать перед повтором: по заголовку провайдера или с запасом."""
        заголовок = response.headers.get("retry-after", "")
        try:
            return max(1.0, min(60.0, float(заголовок)))
        except ValueError:
            return min(60.0, RATE_LIMIT_PAUSE * попытка)

    @staticmethod
    def _rate_limit_hint(response: httpx.Response) -> str:
        """Вытаскивает из ответа человекочитаемое пояснение про лимит."""
        try:
            сообщение = (response.json().get("error") or {}).get("message", "")
        except ValueError:
            сообщение = ""
        return (сообщение or response.text)[:200]

    def _extract_answer(
        self, response: httpx.Response, elapsed: float, model: catalog.Model | None = None
    ) -> str:
        """Достаёт текст ответа и расход токенов из тела ответа провайдера."""
        try:
            data = response.json()
            выбор = data["choices"][0]
            text = (выбор["message"].get("content") or "").strip()
            response_finish = выбор.get("finish_reason", "")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise AgentError(f"Неожиданный формат ответа провайдера: {response.text[:300]}") from exc

        if not text:
            raise AgentError("Модель вернула пустой ответ.")

        model = model or self._model
        usage = data.get("usage") or {}
        prompt_tokens = usage.get("prompt_tokens", 0) or 0
        completion_tokens = usage.get("completion_tokens", 0) or 0
        оценка = self.last_budget.prompt if self.last_budget else 0
        self.last_usage = {
            "model": data.get("model", model.api_id),
            "elapsed": elapsed,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "cost": tokens.cost(
                prompt_tokens, completion_tokens, model.price_in, model.price_out,
            ),
            # Насколько локальная оценка разошлась с фактом — это видно только
            # здесь, и по этой величине судят, можно ли доверять предсказанию.
            "finish_reason": response_finish,
            "estimated_prompt_tokens": оценка,
            "estimate_error": (
                (оценка - prompt_tokens) / prompt_tokens if prompt_tokens and оценка else 0.0
            ),
        }
        log.info(
            "Расход: вход %d, выход %d, стоимость %s",
            prompt_tokens, completion_tokens, tokens.format_cost(self.last_usage["cost"]),
        )
        return text

    # --- приватное: разбор ответа --------------------------------------------

    @staticmethod
    def _extract_json(answer: str) -> dict[str, Any] | None:
        """Достаёт JSON из ответа модели, даже если он завёрнут в ```json.

        Модели регулярно добавляют пояснения до и после и оборачивают ответ в
        markdown, хотя их просили этого не делать. Возвращает None, если JSON
        не нашёлся — тогда вызывающий код покажет сырой текст.
        """
        text = answer.strip()
        fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
        if fenced:
            text = fenced.group(1).strip()

        try:
            data = json.loads(text)
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            pass

        # Последняя попытка: взять самый внешний объект от первой { до последней }.
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                data = json.loads(text[start : end + 1])
                return data if isinstance(data, dict) else None
            except json.JSONDecodeError:
                return None
        return None

    def _parse_analysis(self, answer: str, source_file: str) -> AnalysisResult:
        """Превращает ответ модели в AnalysisResult и досчитывает классификацию."""
        usage = self.last_usage
        result = AnalysisResult(
            source_file=source_file,
            cost_limit=self.cost_limit,
            model=usage.get("model", self._model.api_id),
            elapsed=usage.get("elapsed", 0.0),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
        )

        data = self._extract_json(answer)
        if data is None:
            result.raw_answer = answer
            result.parse_error = (
                "Модель ответила не в формате JSON — показан её ответ как есть."
            )
            log.warning("Не удалось разобрать ответ модели как JSON")
            return result

        result.document_type = str(data.get("document_type") or "иной").strip() or "иной"
        raw_items = data.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            result.raw_answer = answer
            result.parse_error = "Модель не нашла в документе ни одного объекта учёта."
            return result

        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            result.items.append(fsbu.classify(Item.from_llm(raw), self.cost_limit))

        if not result.items:
            result.raw_answer = answer
            result.parse_error = "В ответе модели не оказалось пригодных для разбора объектов."
        return result
