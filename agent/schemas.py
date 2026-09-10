"""Структуры данных, которыми агент описывает результат разбора документа.

Модели здесь намеренно простые (dataclass, без внешних зависимостей): они нужны,
чтобы у результата была одна фиксированная форма — и когда его собрал агент, и
когда его прислал пользователь обратно для выгрузки в Excel.

Ключевая мысль: ответ языковой модели — это сырьё, а не результат. Он приходит
свободным JSON, может недосчитаться полей и переврать типы, поэтому
`Item.from_llm()` разбирает его защищённо, а окончательную категорию, счёт и
амортизацию считает код (см. fsbu.py), а не модель.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

# Названия пяти критериев признания НМА (п. 4 ФСБУ 14/2022) в том порядке,
# в каком их проверяет и выводит агент.
CRITERIA_FIELDS = (
    "no_physical_form",
    "for_business_use",
    "term_over_12m",
    "economic_benefit_and_control",
    "identifiable",
)

CRITERIA_LABELS = {
    "no_physical_form": "Отсутствие материально-вещественной формы",
    "for_business_use": "Использование в деятельности организации",
    "term_over_12m": "Срок использования более 12 месяцев",
    "economic_benefit_and_control": "Экономические выгоды, права и контроль",
    "identifiable": "Идентифицируемость (отделимость)",
}


def _as_float(value: Any, default: float = 0.0) -> float:
    """Число из чего угодно: LLM присылает и 850000, и \"850 000\", и \"850000.00\"."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        cleaned = (
            value.replace(" ", "")
            .replace(" ", "")
            .replace("₽", "")
            .replace(",", ".")
        )
        try:
            return float(cleaned)
        except ValueError:
            return default
    return default


def _as_int(value: Any, default: int | None = None) -> int | None:
    number = _as_float(value, default if default is not None else 0.0)
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    return int(number)


def _as_bool(value: Any) -> bool:
    """Критерий считается выполненным только при явном «да»."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "да", "yes", "1", "выполнен", "выполняется")
    return bool(value)


@dataclass
class Item:
    """Один объект учёта из документа с результатом классификации."""

    position: str
    description: str = ""
    cost: float = 0.0
    currency: str = "RUB"
    term_months: int | None = None
    term_type: str = "unknown"          # fixed | indefinite | unknown
    is_cloud_service: bool = False      # облачный сервис (SaaS), а не переданный экземпляр
    criteria: dict[str, bool] = field(default_factory=dict)

    # Поля ниже заполняет код (fsbu.classify), а не языковая модель.
    all_criteria_met: bool = False
    category: str = ""
    account: str = ""
    cost_limit: float = 0.0
    above_limit: bool = False
    spi_months: int | None = None
    monthly_amortization: float = 0.0
    amortization_note: str = ""

    recommendation: str = ""
    risk_notes: str = ""

    @classmethod
    def from_llm(cls, raw: dict[str, Any]) -> "Item":
        """Собирает объект из сырого фрагмента ответа модели, ничего не требуя.

        Любое поле может отсутствовать или прийти строкой — здесь это не ошибка,
        а норма: пропущенное значение заменяется безопасным умолчанием.
        """
        raw_criteria = raw.get("criteria_check") or raw.get("criteria") or {}
        criteria = {name: _as_bool(raw_criteria.get(name)) for name in CRITERIA_FIELDS}

        return cls(
            position=str(raw.get("position") or raw.get("name") or "Без названия").strip(),
            description=str(raw.get("description") or "").strip(),
            cost=_as_float(raw.get("cost")),
            currency=str(raw.get("currency") or "RUB").strip() or "RUB",
            term_months=_as_int(raw.get("term_months")),
            term_type=str(raw.get("term_type") or "unknown").strip().lower(),
            is_cloud_service=_as_bool(raw.get("is_cloud_service")),
            criteria=criteria,
            recommendation=str(raw.get("recommendation") or "").strip(),
            risk_notes=str(raw.get("risk_notes") or "").strip(),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AnalysisResult:
    """Полный результат разбора одного документа."""

    source_file: str = ""
    document_type: str = "иной"
    analysis_date: str = field(default_factory=lambda: date.today().isoformat())
    items: list[Item] = field(default_factory=list)
    cost_limit: float = 0.0

    # Служебные сведения о самом вызове — нужны интерфейсам и логам.
    model: str = ""
    elapsed: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    raw_answer: str = ""        # сырой ответ модели, если JSON разобрать не удалось
    parse_error: str = ""       # человекочитаемое пояснение, почему не удалось

    @property
    def ok(self) -> bool:
        """Разбор удался: есть хотя бы один объект и нет ошибки разбора."""
        return bool(self.items) and not self.parse_error

    @property
    def total_cost(self) -> float:
        return sum(item.cost for item in self.items)

    def summary(self) -> dict[str, Any]:
        """Сводка по категориям: сумма и количество по каждой."""
        by_category: dict[str, dict[str, float]] = {}
        for item in self.items:
            row = by_category.setdefault(item.category, {"count": 0, "amount": 0.0})
            row["count"] += 1
            row["amount"] += item.cost
        return {
            "total_items": len(self.items),
            "total_cost": self.total_cost,
            "by_category": by_category,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_info": {
                "source_file": self.source_file,
                "document_type": self.document_type,
                "analysis_date": self.analysis_date,
                "cost_limit": self.cost_limit,
            },
            "items": [item.to_dict() for item in self.items],
            "summary": self.summary(),
            "meta": {
                "model": self.model,
                "elapsed": round(self.elapsed, 2),
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "parse_error": self.parse_error,
            },
        }
