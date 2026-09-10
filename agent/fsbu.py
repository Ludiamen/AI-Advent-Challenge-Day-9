"""Правила ФСБУ 14/2022, по которым агент выносит окончательное решение.

Зачем это отдельно от языковой модели. Модель хорошо читает документ и
вытаскивает из него факты: что за объект, сколько стоит, на какой срок,
облако это или коробка. Но арифметика и сопоставление с лимитом — не то, что
стоит доверять генерации: там нужен воспроизводимый ответ, а не правдоподобный.
Поэтому модель отвечает только на пять вопросов «да/нет» и даёт числа, а
категорию, счёт и амортизацию считает код в этом модуле — детерминированно и
под тестами.

Публичное API:
  classify(item, cost_limit) -> Item   — проставляет категорию, счёт, амортизацию
  CATEGORIES                           — категории и счета учёта
  DEFAULT_COST_LIMIT                   — лимит стоимости по умолчанию
"""

from __future__ import annotations

from agent.schemas import CRITERIA_FIELDS, Item

# Категория -> счёт учёта и пояснение (п. 4, 7 ФСБУ 14/2022).
NMA = "НМА"
LOW_VALUE = "малоценный_НМА"
PERIOD_EXPENSE = "расходы_периода"
SAAS = "SaaS_подписка"

CATEGORIES: dict[str, dict[str, str]] = {
    NMA: {
        "account": "04",
        "hint": "Все пять критериев выполнены, стоимость выше лимита",
    },
    LOW_VALUE: {
        "account": "забаланс",
        "hint": "Все пять критериев выполнены, но стоимость не превышает лимит",
    },
    PERIOD_EXPENSE: {
        "account": "26/44",
        "hint": "Не выполнен хотя бы один критерий признания",
    },
    SAAS: {
        "account": "26/44",
        "hint": "Облачный сервис: контроль отсутствует, срок не превышает 12 месяцев",
    },
}

# Лимит стоимости организация устанавливает сама в учётной политике (п. 7).
# 100 000 руб. — типичное значение, синхронизированное с налоговым учётом.
DEFAULT_COST_LIMIT = 100_000.0


def all_criteria_met(item: Item) -> bool:
    """Признание НМА требует ОДНОВРЕМЕННОГО выполнения всех пяти критериев."""
    return all(item.criteria.get(name, False) for name in CRITERIA_FIELDS)


def choose_category(item: Item, cost_limit: float) -> str:
    """Определяет категорию учёта по критериям и стоимости.

    Порядок важен: сначала проверяются критерии признания, и только для
    признанного объекта смотрят на стоимость. Объект, не прошедший критерии,
    в НМА не попадёт ни при какой сумме.
    """
    if all_criteria_met(item):
        return NMA if item.cost > cost_limit else LOW_VALUE

    # Не признан НМА. Отдельно выделяем облачную подписку — но именно её, а не
    # всё подряд без контроля: обучение персонала или НИОКР тоже не дают
    # контроля над объектом, однако облаком не являются. Поэтому признак
    # облачности агент берёт из документа (модель отвечает is_cloud_service),
    # а не выводит из отсутствия контроля.
    no_control = not item.criteria.get("economic_benefit_and_control", False)
    if item.is_cloud_service and no_control:
        return SAAS
    return PERIOD_EXPENSE


def amortization(item: Item) -> tuple[int | None, float, str]:
    """Считает срок полезного использования и ежемесячную амортизацию.

    Возвращает (СПИ в месяцах, сумма в месяц, пояснение). Амортизируются только
    объекты, признанные НМА: малоценные списываются сразу, расходы периода
    амортизировать нечего. Ликвидационная стоимость ИТ-активов принимается
    равной нулю (п. 30-42 ФСБУ 14/2022).
    """
    if item.category != NMA:
        return None, 0.0, "Амортизация не начисляется: объект не признан НМА"

    if item.term_type == "indefinite":
        return None, 0.0, (
            "Срок полезного использования не определён: амортизация не начисляется, "
            "требуется ежегодная проверка на обесценение"
        )

    spi = item.term_months
    if not spi or spi <= 0:
        return None, 0.0, (
            "Срок полезного использования не удалось определить по документу — "
            "установите его в учётной политике"
        )

    monthly = round(item.cost / spi, 2)
    return spi, monthly, f"Линейный способ: {item.cost:,.2f} / {spi} мес.".replace(",", " ")


def classify(item: Item, cost_limit: float = DEFAULT_COST_LIMIT) -> Item:
    """Проставляет объекту категорию, счёт, признак лимита и амортизацию.

    Изменяет и возвращает тот же объект. Всё, что здесь вычислено, перекрывает
    значения, пришедшие от языковой модели: она могла и ошибиться.
    """
    item.cost_limit = cost_limit
    item.above_limit = item.cost > cost_limit
    item.all_criteria_met = all_criteria_met(item)
    item.category = choose_category(item, cost_limit)
    item.account = CATEGORIES[item.category]["account"]
    item.spi_months, item.monthly_amortization, item.amortization_note = amortization(item)

    if not item.recommendation:
        item.recommendation = default_recommendation(item)
    return item


def default_recommendation(item: Item) -> str:
    """Короткая рекомендация бухгалтеру, если модель её не сформулировала."""
    if item.category == NMA:
        if item.spi_months:
            return (
                f"Принять к учёту на счёт 04, амортизировать линейным способом "
                f"{item.spi_months} мес."
            )
        return "Принять к учёту на счёт 04; срок полезного использования установить приказом"
    if item.category == LOW_VALUE:
        return (
            f"Списать в расходы периода: стоимость не превышает лимит "
            f"{item.cost_limit:,.0f} руб. Учитывать за балансом для контроля".replace(",", " ")
        )
    if item.category == SAAS:
        return "Признать расходами периода равномерно в течение срока подписки (счёт 26/44)"
    return "Признать расходами периода в момент возникновения (счёт 26/44)"
