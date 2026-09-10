#!/usr/bin/env python3
"""Генератор примеров документов для проверки агента.

Файлы в samples/ созданы этим скриптом. Он нужен, чтобы примеры можно было
пересоздать, а не хранить как непрозрачные двоичные вложения, и чтобы тесты
имели предсказуемое содержимое.

Запуск:
    python make_samples.py
"""

import os

from docx import Document
from openpyxl import Workbook

SAMPLES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples")

# Позиции подобраны так, чтобы попасть во все четыре категории ФСБУ 14/2022:
# крупная бессрочная лицензия, дешёвая лицензия, годовая облачная подписка
# и обучение персонала, которое НМА не является в принципе (п. 8).
CONTRACT_ROWS = [
    ("1", "Неисключительная лицензия на СУБД «Постгрес Про», серверная установка",
     "850 000", "36 мес."),
    ("2", "Лицензия на антивирус для 30 рабочих мест, установка на серверы заказчика",
     "45 000", "24 мес."),
    ("3", "Подписка на облачную CRM (SaaS), доступ через сеть Интернет",
     "120 000", "12 мес."),
    ("4", "Обучение сотрудников работе с СУБД, очный курс", "60 000", "единовременно"),
]

BUDGET_ROWS = [
    ("Разработка модуля интеграции с 1С (исключительные права)", 1_400_000, 60, "Договор подряда"),
    ("Лицензия на систему электронного документооборота", 300_000, 36, "Лицензионный договор"),
    ("Продление домена и хостинга", 18_000, 12, "Счёт-оферта"),
    ("Аренда серверных мощностей в облаке", 240_000, 12, "Договор оказания услуг"),
]


def make_contract(path: str) -> str:
    """Договор в формате Word: текст плюс таблица позиций."""
    document = Document()
    document.add_heading("Договор № ИТ-2026/14 на передачу прав и оказание услуг", level=1)
    document.add_paragraph(
        "г. Москва. ООО «Ромашка» (Лицензиат) и ООО «Софтлайн-Интегратор» "
        "(Лицензиар) заключили настоящий договор о нижеследующем."
    )
    document.add_paragraph(
        "1.1. Лицензиар предоставляет Лицензиату права использования программ для ЭВМ "
        "и оказывает сопутствующие услуги согласно спецификации (таблица 1)."
    )
    document.add_paragraph(
        "1.2. Программное обеспечение по позициям 1 и 2 устанавливается на серверы "
        "Лицензиата. Лицензиат вправе ограничить доступ третьих лиц к экземплярам ПО."
    )
    document.add_paragraph(
        "1.3. По позиции 3 доступ предоставляется через сеть Интернет на серверах "
        "Лицензиара; экземпляр программы Лицензиату не передаётся."
    )
    document.add_paragraph("Таблица 1. Спецификация")

    table = document.add_table(rows=1, cols=4)
    table.style = "Table Grid"
    for cell, title in zip(table.rows[0].cells, ("№", "Наименование", "Стоимость, руб.", "Срок")):
        cell.text = title
    for row in CONTRACT_ROWS:
        cells = table.add_row().cells
        for cell, value in zip(cells, row):
            cell.text = value

    document.add_paragraph(
        "2.1. Общая стоимость по договору составляет 1 075 000 (один миллион "
        "семьдесят пять тысяч) рублей, НДС не облагается."
    )
    document.save(path)
    return path


def make_budget(path: str) -> str:
    """Смета ИТ-расходов в формате Excel."""
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Смета"
    worksheet.append(["Статья расходов", "Сумма, руб.", "Срок использования, мес.", "Основание"])
    for row in BUDGET_ROWS:
        worksheet.append(list(row))
    for column, width in zip("ABCD", (52, 16, 26, 26)):
        worksheet.column_dimensions[column].width = width
    workbook.save(path)
    return path


def main() -> int:
    """Создаёт примеры документов в каталоге samples/."""
    os.makedirs(SAMPLES_DIR, exist_ok=True)
    created = [
        make_contract(os.path.join(SAMPLES_DIR, "договор-лицензии.docx")),
        make_budget(os.path.join(SAMPLES_DIR, "смета-ит-расходов.xlsx")),
    ]
    for path in created:
        print(f"создан: {os.path.relpath(path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
