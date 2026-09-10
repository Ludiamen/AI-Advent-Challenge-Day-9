"""Каталог моделей, между которыми можно переключать агента.

Все провайдеры OpenAI-совместимы, поэтому модель описывается тремя вещами:
адресом API, идентификатором модели и переменной окружения с ключом. Агент
берёт отсюда всё нужное, а интерфейсы показывают пользователю список.

Состав проверен живыми запросами: у Groq моделей Llama больше нет, у z.ai
бесплатно работает только glm-4.5-flash, платные модели без баланса отвечают
ошибкой. Здесь оставлено то, что действительно отвечает.

Размер контекстного окна взят из ответа эндпоинта /models самого провайдера, а
не из статей в интернете. Именно он ограничивает длину разговора: когда история
перестаёт помещаться, провайдер отвечает ошибкой, а не обрезает лишнее сам.
Модель allam-2-7b с окном 4096 токенов держится в каталоге как испытательный
стенд — на ней переполнение наступает за считанные реплики.

История диалога от модели не зависит: она хранится как обычные пары
«роль — текст», поэтому модель можно сменить посреди разговора, и агент
продолжит с тем же контекстом.

Публичное API:
  MODELS              — все модели (ключ -> описание)
  DEFAULT_MODEL       — ключ модели по умолчанию
  get(key)            — описание модели по ключу
  available()         — ключи моделей, для которых задан ключ API
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Model:
    """Одна модель: где живёт, как называется и чем интересна."""

    key: str
    api_id: str
    provider: str
    base_url: str
    env_var: str
    label: str
    free: bool
    context_window: int          # сколько токенов помещается в запрос вместе с ответом
    price_in: float = 0.0        # долларов за миллион входных токенов
    price_out: float = 0.0       # долларов за миллион выходных
    # Токенизатор у каждой модели свой, и один и тот же русский текст обходится
    # им по-разному. Коэффициент — во сколько раз фактический расход отличается
    # от базовой оценки agent/tokens.py; измерен одним текстом на всех моделях.
    token_factor: float = 1.0
    # Провайдер добавляет к запросу собственную обёртку (служебный системный
    # блок, разделители ролей). У gpt-oss на Groq это целых 72 токена.
    request_overhead: int = 8
    # Модели семейства gpt-oss принимают reasoning_effort: на механических
    # задачах вроде сжатия истории «low» убирает почти всё скрытое рассуждение.
    # Без него сжиматель тратит сотни токенов на размышления и порой возвращает
    # пустой ответ, упершись в предел длины.
    supports_effort: bool = False
    note: str = ""

    @property
    def api_key(self) -> str:
        return os.getenv(self.env_var, "").strip()

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)

    @property
    def price_text(self) -> str:
        if self.free:
            return "бесплатно"
        return f"${self.price_in:g} / ${self.price_out:g} за 1M"


_GROQ = "https://api.groq.com/openai/v1"
_ZAI = "https://api.z.ai/api/paas/v4"
_DEEPSEEK = "https://api.deepseek.com"

MODELS: dict[str, Model] = {
    "groq-120b": Model(
        key="groq-120b", api_id="openai/gpt-oss-120b", provider="Groq",
        base_url=_GROQ, env_var="GROQ_API_KEY", label="GPT-OSS 120B",
        free=True, context_window=131_072,
        token_factor=0.83, request_overhead=72,
        supports_effort=True,
        note="Самая сильная из бесплатных, отвечает за 3-4 секунды. По умолчанию.",
    ),
    "groq-20b": Model(
        key="groq-20b", api_id="openai/gpt-oss-20b", provider="Groq",
        base_url=_GROQ, env_var="GROQ_API_KEY", label="GPT-OSS 20B",
        free=True, context_window=131_072,
        token_factor=0.83, request_overhead=72,
        supports_effort=True,
        note="Вдвое быстрее старшей и тоже бесплатна; на простых вопросах разницы почти нет.",
    ),
    "groq-qwen27b": Model(
        key="groq-qwen27b", api_id="qwen/qwen3.6-27b", provider="Groq",
        base_url=_GROQ, env_var="GROQ_API_KEY", label="Qwen 3.6 27B",
        free=True, context_window=131_072,
        token_factor=0.74, request_overhead=11,
        note="Другое семейство моделей — полезно для сравнения формулировок.",
    ),
    "groq-allam7b": Model(
        key="groq-allam7b", api_id="allam-2-7b", provider="Groq",
        base_url=_GROQ, env_var="GROQ_API_KEY", label="Allam 2 7B",
        free=True, context_window=4_096,
        token_factor=2.54, request_overhead=8,
        note=(
            "Контекст всего 4096 токенов — на ней переполнение видно за пару реплик. "
            "Для содержательных ответов слаба, зато незаменима как испытательный стенд."
        ),
    ),
    "glm-flash": Model(
        key="glm-flash", api_id="glm-4.5-flash", provider="z.ai",
        base_url=_ZAI, env_var="ZAI_API_KEY", label="GLM 4.5 Flash",
        free=True, context_window=131_072,
        token_factor=0.89, request_overhead=6,
        note="Бесплатная модель z.ai. Отвечает заметно медленнее остальных.",
    ),
    "ds-flash": Model(
        key="ds-flash", api_id="deepseek-v4-flash", provider="DeepSeek",
        base_url=_DEEPSEEK, env_var="DEEPSEEK_API_KEY", label="DeepSeek V4 Flash",
        free=False, context_window=131_072, price_in=0.44, price_out=1.32,
        token_factor=0.92, request_overhead=5,
        note="Платная. Хороша на длинных документах и юридических формулировках.",
    ),
    "ds-pro": Model(
        key="ds-pro", api_id="deepseek-v4-pro", provider="DeepSeek",
        base_url=_DEEPSEEK, env_var="DEEPSEEK_API_KEY", label="DeepSeek V4 Pro",
        free=False, context_window=131_072, price_in=1.32, price_out=3.96,
        token_factor=0.92, request_overhead=5,
        note="Платная и самая медленная, зато самая внимательная к деталям.",
    ),
}

DEFAULT_MODEL = os.getenv("LLM_MODEL_KEY", "groq-120b")


def get(key: str) -> Model:
    """Описание модели по ключу; понятная ошибка, если ключ неизвестен."""
    try:
        return MODELS[key]
    except KeyError:
        raise KeyError(
            f"Неизвестная модель «{key}». Доступны: {', '.join(MODELS)}."
        ) from None


def available() -> list[str]:
    """Модели, для которых в окружении есть ключ соответствующего провайдера."""
    return [key for key, model in MODELS.items() if model.has_key]


def describe() -> list[dict]:
    """Список моделей для интерфейсов: без ключей, только то, что можно показать."""
    return [
        {
            "key": model.key,
            "label": model.label,
            "provider": model.provider,
            "api_id": model.api_id,
            "free": model.free,
            "context_window": model.context_window,
            "price_in": model.price_in,
            "price_out": model.price_out,
            "price_text": model.price_text,
            "token_factor": model.token_factor,
            "note": model.note,
            "available": model.has_key,
        }
        for model in MODELS.values()
    ]
