#!/usr/bin/env python3
"""CLI: разговор с агентом, разбор документов и работа с историей.

Быстрый старт:
    python cli.py                                  # диалог, история сохраняется
    python cli.py "Что такое НМА по ФСБУ 14/2022?" # один вопрос
    python cli.py --модель ds-pro --сессия ромашка # своя модель и своя тема
    python cli.py --история                        # показать сохранённый диалог
    python cli.py --файл samples/договор-лицензии.docx --отчёт out/свод.xlsx

Полная справка: python cli.py --help

Этот файл — интерфейс, и он намеренно ничего не знает про LLM: ни адреса, ни
ключа, ни формата запроса. Он умеет только вызывать публичные методы агента.
"""

import argparse
import logging
import sys

from agent import AgentError, DocumentAnalysisAgent
from agent import catalog, tokens

LINE = "-" * 78

_МОДЕЛИ = "\n".join(
    f"  {m['key']:<13} {m['provider']:<9} {m['label']:<18} "
    f"окно {tokens.format_tokens(m['context_window']):>7} "
    f"{'бесплатно' if m['free'] else m['price_text']}"
    for m in catalog.describe()
)

EPILOG = f"""\
модели (--модель):
{_МОДЕЛИ}

  Модель можно сменить и посреди разговора — история от неё не зависит.

токены:
  Перед каждым запросом агент оценивает, сколько токенов займёт системный
  промпт, история и сам вопрос, и сверяет это с контекстным окном модели.
  После ответа показывается точный расход из ответа API и стоимость по прайсу.
  Флаг --токены выводит эту раскладку, --рост — как накапливался расход по
  ходу разговора.

  Токенизаторы у моделей разные: один и тот же русский текст обходится
  allam-2-7b примерно втрое дороже, чем gpt-oss. Коэффициенты измерены и
  лежат в agent/catalog.py.

переполнение контекста:
  Когда разговор перестаёт помещаться в окно модели, агент либо выбрасывает
  самые давние реплики (--при-переполнении обрезать, по умолчанию), либо
  останавливается и объясняет, чего не хватает (--при-переполнении отказать),
  либо отправляет запрос как есть (--при-переполнении отправить) — тогда
  ошибку вернёт сам провайдер, и её видно целиком.
  Флаг --окно искусственно уменьшает контекст: так переполнение можно увидеть,
  не тратя сотни тысяч токенов на его заполнение.

сжатие истории:
  Последние сообщения уходят в запрос дословно, а всё, что старше, заменяется
  краткой выжимкой: несколько предложений вместо тысяч токенов переписки.
  Выжимку делает отдельная дешёвая модель (--сжиматель), хранится она рядом с
  историей и накатывается — новая собирается из прежней плюс состарившихся
  реплик. Сырые сообщения при этом не удаляются никогда, поэтому конспект
  всегда можно пересобрать заново (команда «пересобрать»).

  Сжатие запускается по тому из двух условий, что наступит раньше: накопилось
  --каждые состарившихся сообщений либо история заняла долю контекста больше
  --порог-сжатия. Флаг --без-сжатия выключает механизм целиком — так меряют,
  сколько он экономит.

память:
  История каждой сессии лежит в SQLite (по умолчанию history.db) и переживает
  перезапуск: агент продолжает разговор с того места, где остановились.
  Сессии независимы — «--сессия ромашка» и «--сессия черновик» не мешают друг другу.
  Разбор документа тоже попадает в историю сжатой сводкой, поэтому после него
  можно спрашивать «а почему третья позиция не НМА?».

примеры:
  # диалог: «?» — памятка, Ctrl+C — выход
  python cli.py

  # продолжить вчерашний разговор в своей теме
  python cli.py --сессия ромашка

  # тот же вопрос другой моделью, история сохраняется общая
  python cli.py --модель groq-20b "Чем малоценный НМА отличается от расходов периода?"

  # разбор договора, затем уточняющий вопрос по нему
  python cli.py --файл samples/договор-лицензии.docx
  python cli.py "Почему подписка на CRM не попала в НМА?"

  # разговор со сжатием и без него — для сравнения
  python cli.py --сессия со-сжатием "Вопрос"
  python cli.py --сессия без-сжатия --без-сжатия "Вопрос"

  # посмотреть выжимку и пересобрать её начисто
  python cli.py --выжимка
  python cli.py --пересобрать

  # раскладка токенов и стоимость запроса
  python cli.py --токены "Что такое НМА?"

  # как рос расход по ходу разговора
  python cli.py --рост

  # переполнение на модели с окном 4096 токенов
  python cli.py --модель groq-allam7b --при-переполнении отказать "Вопрос"

  # искусственно узкое окно — переполнение наступит сразу
  python cli.py --окно 600 --при-переполнении отказать "Вопрос"

  # посмотреть и очистить историю
  python cli.py --история
  python cli.py --сессии
  python cli.py --сброс
"""

INTERACTIVE_HELP = """\
Команды диалога:
  ?  /  справка       — показать эту памятку
  файл <путь>         — разобрать документ (.pdf, .docx, .xlsx)
  модель <ключ>       — сменить модель, не теряя разговор
  сессия <имя>        — переключиться на другую тему
  история             — показать сохранённые сообщения
  токены              — раскладка контекста и расход сессии
  выжимка             — показать текущий конспект давних реплик
  сжать               — свернуть состарившиеся реплики прямо сейчас
  пересобрать         — собрать конспект заново из всей сырой истории
  рост                — как накапливались токены и деньги
  сброс               — стереть историю текущей сессии
  Ctrl+C              — выход
Любая другая строка отправляется агенту как вопрос.
"""


def build_parser() -> argparse.ArgumentParser:
    """Собирает разбор аргументов командной строки со всей справкой."""
    parser = argparse.ArgumentParser(
        description="Агент-аналитик ИТ-расходов по ФСБУ 14/2022 с памятью диалога.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("вопрос", nargs="*", help="текст вопроса; без него — диалог")
    parser.add_argument("--модель", dest="model", default=catalog.DEFAULT_MODEL, metavar="КЛЮЧ",
                        help=f"модель из каталога (по умолчанию: {catalog.DEFAULT_MODEL})")
    parser.add_argument("--сессия", dest="session", default="основная", metavar="ИМЯ",
                        help="тема разговора со своей историей (по умолчанию: основная)")
    parser.add_argument("--база", dest="db", default="", metavar="ПУТЬ",
                        help="файл хранилища истории (по умолчанию: history.db)")
    parser.add_argument("--файл", dest="path", default="", metavar="ПУТЬ",
                        help="документ для разбора (.pdf, .docx, .xlsx)")
    parser.add_argument("--отчёт", dest="report", default="", metavar="ПУТЬ",
                        help="сохранить сводную таблицу Excel по результатам разбора")
    parser.add_argument("--лимит", dest="limit", type=float, default=None, metavar="РУБ",
                        help="лимит стоимости из учётной политики (по умолчанию 100000)")
    parser.add_argument("--без-сжатия", dest="no_compress", action="store_true",
                        help="не сжимать историю: отправлять переписку целиком")
    parser.add_argument("--сжиматель", dest="summarizer", default="", metavar="КЛЮЧ",
                        help="модель для выжимки (по умолчанию groq-20b)")
    parser.add_argument("--дословно", dest="keep_last", type=int, default=0, metavar="N",
                        help="сколько последних сообщений не сжимать (по умолчанию 6)")
    parser.add_argument("--каждые", dest="compress_every", type=int, default=0, metavar="N",
                        help="сколько состарившихся сообщений запускают сжатие (по умолчанию 10)")
    parser.add_argument("--порог-сжатия", dest="threshold", type=float, default=0.0,
                        metavar="ДОЛЯ",
                        help="доля контекста, после которой сжатие идёт досрочно (по умолчанию 0.5)")
    parser.add_argument("--выжимка", dest="show_summary", action="store_true",
                        help="показать текущую выжимку и выйти")
    parser.add_argument("--сжать", dest="do_compress", action="store_true",
                        help="свернуть состарившиеся реплики и выйти")
    parser.add_argument("--пересобрать", dest="rebuild", action="store_true",
                        help="собрать выжимку заново из всей сырой истории и выйти")
    parser.add_argument("--токены", dest="show_tokens", action="store_true",
                        help="показывать раскладку токенов до и после запроса")
    parser.add_argument("--рост", dest="growth", action="store_true",
                        help="показать, как рос расход токенов и денег по ходу диалога")
    parser.add_argument("--при-переполнении", dest="overflow", default="обрезать",
                        choices=("обрезать", "отказать", "отправить"), metavar="РЕЖИМ",
                        help="что делать при переполнении контекста: обрезать историю, "
                             "отказать с объяснением или отправить как есть "
                             "и показать ответ провайдера")
    parser.add_argument("--окно", dest="window", type=int, default=0, metavar="N",
                        help="искусственно уменьшить контекстное окно, чтобы увидеть переполнение")
    parser.add_argument("--история", action="store_true",
                        help="показать сохранённую историю сессии и выйти")
    parser.add_argument("--сессии", action="store_true",
                        help="показать список всех сессий и выйти")
    parser.add_argument("--сброс", action="store_true",
                        help="стереть историю сессии и выйти")
    parser.add_argument("--без-памяти", dest="no_memory", action="store_true",
                        help="не сохранять и не читать историю в этом запуске")
    parser.add_argument("--подробно", action="store_true",
                        help="показать проверку каждого из пяти критериев")
    parser.add_argument("--логи", action="store_true",
                        help="печатать журнал работы агента (уровень INFO)")
    return parser


# --- вывод -------------------------------------------------------------------


def print_history(agent: DocumentAnalysisAgent) -> int:
    """Печатает сохранённый диалог текущей сессии."""
    записи = agent.history()
    if not записи:
        print(f"История сессии «{agent.session}» пуста.")
        return 0

    print(f"История сессии «{agent.session}» — сообщений: {len(записи)}\n")
    for запись in записи:
        кто = "Вы" if запись["role"] == "user" else "Агент"
        когда = запись["created_at"][:16].replace("T", " ")
        пометка = " [документ]" if запись["kind"] == "document" else ""
        print(f"{LINE}\n{кто}{пометка} · {когда}")
        print(запись["content"])
    print(LINE)

    сводка = agent.info().get("stats", {})
    print(f"вопросов: {сводка.get('questions', 0)} · разборов документов: "
          f"{сводка.get('documents', 0)}")
    return 0


def print_budget(agent: DocumentAnalysisAgent, вопрос: str) -> None:
    """Раскладка контекста ДО отправки запроса."""
    б = agent.budget(вопрос)
    сведения = agent.info()
    print(LINE)
    print(f"Оценка запроса ({сведения['model_label']}, окно "
          f"{tokens.format_tokens(б.limit)} токенов):")
    print(f"  системный промпт   {tokens.format_tokens(б.system):>9}")
    print(f"  история диалога    {tokens.format_tokens(б.history):>9}")
    print(f"  текущий вопрос     {tokens.format_tokens(б.question):>9}")
    print(f"  обёртка провайдера {tokens.format_tokens(б.overhead):>9}")
    print(f"  ---")
    print(f"  итого запрос       {tokens.format_tokens(б.prompt):>9}")
    print(f"  резерв под ответ   {tokens.format_tokens(б.reserved):>9}")
    print(f"  {б.describe()}")
    print(LINE)


def print_usage(agent: DocumentAnalysisAgent) -> None:
    """Точный расход по последнему запросу и по сессии целиком."""
    факт = agent.last_usage
    if факт:
        ошибка = факт.get("estimate_error", 0.0)
        print(
            f"расход запроса: вход {tokens.format_tokens(факт['prompt_tokens'])}, "
            f"выход {tokens.format_tokens(факт['completion_tokens'])}, "
            f"итого {tokens.format_tokens(факт['total_tokens'])} · "
            f"{tokens.format_cost(факт['cost'])}"
            + (f" · оценка разошлась с фактом на {ошибка:+.0%}" if ошибка else "")
        )
    сессия = agent.usage()
    if сессия.get("tokens"):
        print(
            f"за сессию «{сессия['session']}»: "
            f"{tokens.format_tokens(сессия['tokens'])} токенов "
            f"({tokens.format_tokens(сессия['prompt_tokens'])} вход + "
            f"{tokens.format_tokens(сессия['completion_tokens'])} выход), "
            f"{tokens.format_cost(сессия['cost'])}"
        )


def print_growth(agent: DocumentAnalysisAgent) -> int:
    """Таблица роста: сколько стоил каждый обмен и сколько накопилось."""
    шаги = agent.growth()
    if not шаги:
        print(f"В сессии «{agent.session}» ещё не было обменов с моделью.")
        return 0

    print(f"Рост расхода в сессии «{agent.session}»\n")
    print(f"{'обмен':>5} {'вход':>8} {'выход':>8} {'за обмен':>9} "
          f"{'накоплено':>10} {'стоимость':>12}")
    print(LINE)
    for ш in шаги:
        за_обмен = ш["prompt_tokens"] + ш["completion_tokens"]
        пометка = " 📄" if ш["kind"] == "document" else ""
        print(f"{ш['number']:>5} {tokens.format_tokens(ш['prompt_tokens']):>8} "
              f"{tokens.format_tokens(ш['completion_tokens']):>8} "
              f"{tokens.format_tokens(за_обмен):>9} "
              f"{tokens.format_tokens(ш['total_tokens']):>10} "
              f"{tokens.format_cost(ш['total_cost']):>12}{пометка}")
    print(LINE)
    первый = шаги[0]["prompt_tokens"]
    последний = шаги[-1]["prompt_tokens"]
    if первый:
        print(f"Запрос вырос с {tokens.format_tokens(первый)} до "
              f"{tokens.format_tokens(последний)} токенов "
              f"(в {последний / первый:.1f} раза): каждая реплика тянет за собой "
              f"всю прошлую переписку.")
    return 0


def print_summary(agent: DocumentAnalysisAgent) -> int:
    """Показывает текущую выжимку и то, что в неё вошло."""
    сведения = agent.info()
    if not сведения.get("compress"):
        print("Сжатие выключено в этом запуске (--без-сжатия).")
        return 0

    выжимка = agent.summary()
    if выжимка is None:
        ждут = сведения.get("pending", 0)
        print(f"Выжимки пока нет: сжатие ещё не запускалось.\n"
              f"Состарившихся сообщений ждёт сжатия: {ждут} "
              f"(порог — {сведения['compress_every']}).")
        return 0

    print(f"Выжимка сессии «{agent.session}», поколение {выжимка['generation']}")
    print(f"Свёрнуто сообщений: {выжимка['messages']} · "
          f"сделана моделью {выжимка['model']} · "
          f"занимает ~{tokens.format_tokens(agent.estimate(выжимка['content']))} токенов")
    print(LINE)
    print(выжимка["content"])
    print(LINE)
    поколений = len(agent.summaries())
    if поколений > 1:
        print(f"Поколений выжимки: {поколений}. Конспект накатывается, поэтому "
              f"детали могут размываться — «пересобрать» соберёт его заново "
              f"из сырой истории.")
    print(f"Дословно в запрос уходит последних сообщений: {сведения.get('pending', 0)} "
          f"+ {сведения['keep_last']} свежих")
    return 0


def print_compression(итог: dict) -> None:
    """Печатает, что дало сжатие."""
    if not итог.get("compressed"):
        причина = итог.get("reason") or итог.get("error") or "нечего сжимать"
        print(f"Сжатие не выполнено: {причина}")
        return
    print(
        f"Свёрнуто сообщений: {итог['compressed']} · "
        f"{tokens.format_tokens(итог['tokens_before'])} → "
        f"{tokens.format_tokens(итог['tokens_after'])} токенов "
        f"(осталось {итог['ratio']:.0%}, сэкономлено "
        f"{tokens.format_tokens(итог['saved'])})"
    )
    print(
        f"Само сжатие обошлось в {tokens.format_tokens(итог['spent_tokens'])} токенов "
        f"модели {итог['summarizer']} ({tokens.format_cost(итог['spent_cost'])})"
    )
    if итог.get("truncated"):
        print("⚠ Выжимка оборвана по пределу длины — часть сведений могла потеряться.")


def print_sessions(agent: DocumentAnalysisAgent) -> int:
    """Печатает список сессий в хранилище."""
    сессии = agent.sessions()
    if not сессии:
        print("Сохранённых сессий пока нет.")
        return 0
    print(f"{'сессия':<18} {'сообщений':>10} {'документов':>11} {'токенов':>10} "
          f"{'стоимость':>12}  обновлена")
    print(LINE)
    for строка in сессии:
        когда = (строка["updated_at"] or "")[:16].replace("T", " ")
        print(f"{строка['session']:<18} {строка['messages']:>10} "
              f"{строка['documents']:>11} "
              f"{tokens.format_tokens(строка['tokens'] or 0):>10} "
              f"{tokens.format_cost(строка['cost'] or 0):>12}  {когда}")
    return 0


def print_items(result: dict, verbose: bool) -> None:
    """Печатает разбор документа: объекты, их категории и сводку."""
    info = result["document_info"]
    print(f"\nДокумент: {info['source_file']} · тип: {info['document_type']}")
    print(f"Лимит стоимости: {info['cost_limit']:,.0f} ₽".replace(",", " "))

    if result["meta"].get("parse_error"):
        print(f"\n⚠ {result['meta']['parse_error']}")

    for number, item in enumerate(result["items"], start=1):
        print(LINE)
        print(f"{number}. {item['position']}")
        if item["description"]:
            print(f"   {item['description']}")
        term = f"{item['term_months']} мес." if item["term_months"] else item["term_type"]
        print(
            f"   стоимость: {item['cost']:,.2f} {item['currency']} · срок: {term}"
            .replace(",", " ")
        )
        print(f"   категория: {item['category']} · счёт: {item['account']}")
        if item["monthly_amortization"]:
            print(
                f"   амортизация: {item['monthly_amortization']:,.2f} ₽/мес. "
                f"({item['spi_months']} мес.)".replace(",", " ")
            )
        elif item["amortization_note"]:
            print(f"   амортизация: {item['amortization_note']}")

        if verbose:
            print("   критерии признания:")
            for name, value in item["criteria"].items():
                print(f"     {'✓' if value else '✗'} {name}")
        if item["recommendation"]:
            print(f"   рекомендация: {item['recommendation']}")
        if item["risk_notes"]:
            print(f"   риски: {item['risk_notes']}")

    summary = result["summary"]
    print(LINE)
    print(f"Итого объектов: {summary['total_items']} на сумму "
          f"{summary['total_cost']:,.2f} ₽".replace(",", " "))
    for category, row in summary["by_category"].items():
        print(f"  {category:<18} {row['count']:>2} шт. "
              f"{row['amount']:>14,.2f} ₽".replace(",", " "))

    meta = result["meta"]
    print(f"\nмодель={meta['model']}  время={meta['elapsed']} с  "
          f"токены: вход={meta['prompt_tokens']} выход={meta['completion_tokens']}")


def analyze_and_report(agent: DocumentAnalysisAgent, path: str, report: str, verbose: bool) -> int:
    """Разбирает документ и при необходимости выгружает отчёт."""
    try:
        result = agent.analyze(path)
    except AgentError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1

    if result["meta"].get("parse_error") and not result["items"]:
        print(f"⚠ {result['meta']['parse_error']}")
        return 1

    print_items(result, verbose)
    print_usage(agent)
    if agent.remembers:
        print("Разбор добавлен в историю — по нему можно задавать вопросы.")

    if report:
        try:
            saved = agent.build_pivot_report(result["items"], report)
        except AgentError as exc:
            print(f"Ошибка выгрузки: {exc}", file=sys.stderr)
            return 1
        print(f"\nСводная таблица сохранена: {saved}")
    return 0


def interactive(agent: DocumentAnalysisAgent, verbose: bool, verbose_tokens: bool = False) -> int:
    """Диалог с командами управления памятью и моделью."""
    info = agent.info()
    сводка = info.get("stats", {})
    print(f"Агент-аналитик ФСБУ 14/2022. Модель: {info['model_label']} ({info['model_key']}).")
    print(f"Сессия: «{agent.session}»", end="")
    if сводка.get("messages"):
        print(f", в памяти сообщений: {сводка['messages']} — продолжаем разговор.")
    else:
        print(" — новая, история пуста.")
    if info.get("compress"):
        выжимка = info.get("summary")
        print(f"Сжатие включено: дословно последние {info['keep_last']}, "
              f"остальное — выжимкой от {info['summarizer']}"
              + (f" (поколение {info['summary_generation']})" if выжимка else ""))
    else:
        print("Сжатие выключено: история уходит в запрос целиком.")
    print("«?» — справка, Ctrl+C — выход.\n")

    while True:
        try:
            line = input("Вы: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nПока! История сохранена.")
            return 0

        if not line:
            continue
        низкая = line.lower()

        if низкая in ("?", "справка", "help"):
            print(INTERACTIVE_HELP)
        elif низкая == "история":
            print_history(agent)
        elif низкая == "сессии":
            print_sessions(agent)
        elif низкая == "токены":
            print_budget(agent, "")
            print_usage(agent)
        elif низкая == "рост":
            print_growth(agent)
        elif низкая == "выжимка":
            print_summary(agent)
        elif низкая in ("сжать", "пересобрать"):
            try:
                print_compression(
                    agent.rebuild_summary() if низкая == "пересобрать"
                    else agent.compress(force=True)
                )
            except AgentError as exc:
                print(f"Ошибка: {exc}", file=sys.stderr)
        elif низкая == "сброс":
            print(f"Удалено сообщений: {agent.clear_history()}")
        elif низкая.startswith("файл "):
            analyze_and_report(agent, line[5:].strip(), "", verbose)
        elif низкая.startswith("модель "):
            try:
                agent.switch_model(line[7:].strip())
                print(f"Модель переключена на {agent.info()['model_label']}. Разговор сохранён.")
            except AgentError as exc:
                print(f"Ошибка: {exc}", file=sys.stderr)
        elif низкая.startswith("сессия "):
            try:
                agent.switch_session(line[7:].strip())
                сводка = agent.info().get("stats", {})
                print(f"Сессия «{agent.session}», сообщений в памяти: "
                      f"{сводка.get('messages', 0)}")
            except AgentError as exc:
                print(f"Ошибка: {exc}", file=sys.stderr)
        else:
            if verbose_tokens:
                print_budget(agent, line)
            try:
                print(f"Агент: {agent.ask(line)}\n")
            except AgentError as exc:
                print(f"Ошибка: {exc}\n", file=sys.stderr)
                continue
            if agent.last_compression.get("compressed"):
                print_compression(agent.last_compression)
                agent.last_compression = {}
            if verbose_tokens:
                print_usage(agent)
                print()


def main() -> int:
    """Точка входа: разбирает аргументы, создаёт агента и выполняет команду."""
    args = build_parser().parse_args()
    if args.логи:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    поля = {
        "model_key": args.model, "session": args.session, "remember": not args.no_memory,
        "on_overflow": args.overflow, "context_limit": args.window,
        "compress": not args.no_compress,
    }
    if args.summarizer:
        поля["summarizer_model"] = args.summarizer
    if args.keep_last:
        поля["keep_last"] = args.keep_last
    if args.compress_every:
        поля["compress_every"] = args.compress_every
    if args.threshold:
        поля["compress_threshold"] = args.threshold
    if args.db:
        поля["memory"] = args.db
    if args.limit is not None:
        поля["cost_limit"] = args.limit

    try:
        agent = DocumentAnalysisAgent(**поля)
    except (AgentError, KeyError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1

    # Команды работы с историей ключа API не требуют — обрабатываем их первыми.
    if args.сессии:
        return print_sessions(agent)
    if args.growth:
        return print_growth(agent)
    if args.show_summary:
        return print_summary(agent)
    if args.история:
        return print_history(agent)
    if args.сброс:
        print(f"История сессии «{agent.session}»: удалено сообщений "
              f"{agent.clear_history()}")
        return 0

    if not agent.ready:
        info = agent.info()
        print(
            f"Не задан ключ API для модели «{info['model_key']}».\n"
            f"Скопируйте .env.example в .env и впишите ключ. "
            f"Подробности — в README.md.",
            file=sys.stderr,
        )
        return 1

    if args.do_compress or args.rebuild:
        try:
            итог = agent.rebuild_summary() if args.rebuild else agent.compress(force=True)
        except AgentError as exc:
            print(f"Ошибка: {exc}", file=sys.stderr)
            return 1
        print_compression(итог)
        return 0

    if args.path:
        return analyze_and_report(agent, args.path, args.report, args.подробно)

    question = " ".join(args.вопрос).strip()
    if question:
        if args.show_tokens:
            print_budget(agent, question)
        try:
            print(agent.ask(question))
        except AgentError as exc:
            print(f"Ошибка: {exc}", file=sys.stderr)
            return 1
        if args.show_tokens:
            print()
            print_usage(agent)
        return 0

    return interactive(agent, args.подробно, args.show_tokens)


if __name__ == "__main__":
    raise SystemExit(main())
