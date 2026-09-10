"""Долговременная память агента: история диалога в SQLite.

Задача дня — чтобы агент продолжал разговор так, будто его не выключали.
Значит, история должна пережить остановку процесса, а при следующем запуске
вернуться на место. Здесь это сделано на sqlite3 из стандартной библиотеки:
новых зависимостей не нужно, запись атомарна, и оборванный на полуслове процесс
не оставляет после себя испорченный файл — в отличие от JSON, который
переписывается целиком на каждом сообщении.

Одна база держит сколько угодно именованных сессий: «работа», «черновик»,
«клиент-Ромашка». Это и разделение тем, и способ начать с чистого листа,
не стирая прежнее.

Публичное API:
  ConversationMemory(path)                — открыть (и при необходимости создать) базу
  .append(session, role, content, ...)    — дописать сообщение
  .messages(session)                      — вся история сессии
  .context(session, ...)                  — последние сообщения для отправки модели
  .sessions()                             — список сессий со сводкой
  .save_summary(session, ...)             — сохранить выжимку давних реплик
  .latest_summary(session)                — последняя выжимка сессии
  .summaries(session)                     — все поколения выжимок
  .pending(session, keep_last)            — что состарилось и ждёт сжатия
  .clear(session)                         — стереть историю и выжимки сессии
  .stats(session)                         — счётчики по сессии, включая токены и деньги
  .growth(session)                        — как накапливались токены и стоимость
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

# Роли в терминах OpenAI-совместимого API — история хранится сразу в том виде,
# в котором её потом отдавать модели.
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLES = (ROLE_USER, ROLE_ASSISTANT)

# Вид сообщения: обычная реплика чата или запись о разборе документа.
KIND_CHAT = "chat"
KIND_DOCUMENT = "document"

# Сколько истории уходит в модель. Ограничения два, и работают они вместе:
# по числу сообщений — чтобы разговор не разрастался бесконечно, по символам —
# чтобы одна огромная реплика не съела весь контекст.
DEFAULT_MAX_MESSAGES = 20
DEFAULT_MAX_CHARS = 12_000

DEFAULT_SESSION = "основная"
DEFAULT_DB_PATH = os.getenv("HISTORY_DB", "history.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session    TEXT NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'chat',
    model      TEXT NOT NULL DEFAULT '',
    meta       TEXT NOT NULL DEFAULT '',
    tokens     INTEGER NOT NULL DEFAULT 0,
    cost       REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session, id);

-- Выжимки хранятся отдельно от сообщений и никогда их не заменяют: сырой
-- разговор остаётся в базе целиком, поэтому конспект всегда можно пересобрать,
-- а саму переписку — прочитать как есть.
CREATE TABLE IF NOT EXISTS summaries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session      TEXT NOT NULL,
    content      TEXT NOT NULL,
    covers_until INTEGER NOT NULL,   -- номер последнего свёрнутого сообщения
    messages     INTEGER NOT NULL,   -- сколько сообщений вошло в этот конспект
    generation   INTEGER NOT NULL,   -- поколение: 1, 2, 3...
    model        TEXT NOT NULL DEFAULT '',
    tokens       INTEGER NOT NULL DEFAULT 0,
    cost         REAL NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_summaries_session ON summaries(session, id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class MemoryError(RuntimeError):
    """Ошибка работы с хранилищем истории."""


class ConversationMemory:
    """Хранилище истории диалога поверх файла SQLite."""

    def __init__(self, path: str = DEFAULT_DB_PATH) -> None:
        """Открывает базу, создавая файл и таблицы, если их ещё нет.

        path=":memory:" даёт временное хранилище в оперативной памяти — им
        пользуются тесты, чтобы не трогать файл на диске.
        """
        self.path = path
        if path != ":memory:":
            directory = os.path.dirname(os.path.abspath(path))
            os.makedirs(directory, exist_ok=True)

        try:
            # check_same_thread=False: веб-сервер обслуживает запросы в разных
            # потоках, а запись у нас короткая и защищена самим SQLite.
            self._db = sqlite3.connect(path, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            self._db.executescript(_SCHEMA)
            self._migrate()
            self._db.commit()
        except sqlite3.Error as exc:
            raise MemoryError(f"Не удалось открыть хранилище истории «{path}»: {exc}") from exc

    def _migrate(self) -> None:
        """Дописывает колонки, появившиеся позже, в базы прежних версий.

        База из предыдущего дня не знает про токены и стоимость. Пересоздавать
        её ради этого нельзя — там лежит история разговоров, — поэтому недостающие
        колонки добавляются на месте, со значением по умолчанию для старых строк.
        """
        существующие = {
            строка["name"] for строка in self._db.execute("PRAGMA table_info(messages)")
        }
        for имя, определение in (
            ("tokens", "INTEGER NOT NULL DEFAULT 0"),
            ("cost", "REAL NOT NULL DEFAULT 0"),
        ):
            if имя not in существующие:
                self._db.execute(f"ALTER TABLE messages ADD COLUMN {имя} {определение}")

    # --- запись --------------------------------------------------------------

    def append(
        self,
        session: str,
        role: str,
        content: str,
        kind: str = KIND_CHAT,
        model: str = "",
        meta: dict[str, Any] | None = None,
        tokens: int = 0,
        cost: float = 0.0,
    ) -> int:
        """Дописывает сообщение в конец истории сессии и возвращает его номер.

        tokens и cost — фактические значения из ответа API: их возвращает
        провайдер, и именно они идут в счётчики сессии.
        """
        if role not in ROLES:
            raise MemoryError(f"Неизвестная роль «{role}». Допустимы: {', '.join(ROLES)}.")
        content = (content or "").strip()
        if not content:
            raise MemoryError("Пустое сообщение не сохраняется.")

        try:
            cursor = self._db.execute(
                "INSERT INTO messages"
                " (session, role, content, kind, model, meta, tokens, cost, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session or DEFAULT_SESSION,
                    role,
                    content,
                    kind,
                    model,
                    json.dumps(meta, ensure_ascii=False) if meta else "",
                    int(tokens),
                    float(cost),
                    _now(),
                ),
            )
            self._db.commit()
        except sqlite3.Error as exc:
            raise MemoryError(f"Не удалось сохранить сообщение: {exc}") from exc
        return int(cursor.lastrowid)

    def append_pair(
        self,
        session: str,
        question: str,
        answer: str,
        model: str = "",
        kind: str = KIND_CHAT,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost: float = 0.0,
    ) -> None:
        """Сохраняет вопрос и ответ одной парой — так они не разъедутся.

        Токены раскладываются по своим сторонам: входные — на вопрос, выходные —
        на ответ. Стоимость целиком записывается на ответ, чтобы при суммировании
        по сессии она не удвоилась.
        """
        self.append(session, ROLE_USER, question, kind=kind, model=model,
                    tokens=prompt_tokens)
        self.append(session, ROLE_ASSISTANT, answer, kind=kind, model=model,
                    tokens=completion_tokens, cost=cost)

    # --- чтение --------------------------------------------------------------

    def messages(self, session: str = DEFAULT_SESSION, limit: int | None = None) -> list[dict]:
        """Вся история сессии по возрастанию времени; limit оставляет последние."""
        query = "SELECT * FROM messages WHERE session = ? ORDER BY id"
        rows = self._db.execute(query, (session,)).fetchall()
        if limit is not None and limit >= 0:
            rows = rows[-limit:]
        return [dict(row) for row in rows]

    def context(
        self,
        session: str = DEFAULT_SESSION,
        max_messages: int = DEFAULT_MAX_MESSAGES,
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> list[dict[str, str]]:
        """Готовит историю для отправки модели: только role и content.

        Берутся последние сообщения — они важнее давних, — пока не упрёмся в
        любое из двух ограничений. Если первым в выборку попал ответ агента,
        он отбрасывается: разговор для модели должен начинаться с реплики
        пользователя, иначе она принимает чужой ответ за начало беседы.
        """
        rows = self._db.execute(
            "SELECT role, content FROM messages WHERE session = ? ORDER BY id DESC LIMIT ?",
            (session, max(0, max_messages)),
        ).fetchall()

        selected: list[dict[str, str]] = []
        used = 0
        for row in rows:  # идём от свежих к старым
            length = len(row["content"])
            if selected and used + length > max_chars:
                break
            used += length
            selected.append({"role": row["role"], "content": row["content"]})

        selected.reverse()
        while selected and selected[0]["role"] != ROLE_USER:
            selected.pop(0)
        return selected

    def sessions(self) -> list[dict[str, Any]]:
        """Все сессии со сводкой: сколько сообщений и когда были последними."""
        rows = self._db.execute(
            "SELECT session,"
            "       COUNT(*) AS messages,"
            "       MAX(created_at) AS updated_at,"
            "       SUM(CASE WHEN kind = ? THEN 1 ELSE 0 END) AS documents,"
            "       SUM(tokens) AS tokens,"
            "       SUM(cost) AS cost"
            "  FROM messages GROUP BY session ORDER BY updated_at DESC",
            (KIND_DOCUMENT,),
        ).fetchall()
        return [dict(row) for row in rows]

    def stats(self, session: str = DEFAULT_SESSION) -> dict[str, Any]:
        """Счётчики по одной сессии — их показывают интерфейсы."""
        row = self._db.execute(
            "SELECT COUNT(*) AS messages,"
            "       SUM(CASE WHEN role = ? THEN 1 ELSE 0 END) AS questions,"
            "       SUM(CASE WHEN kind = ? THEN 1 ELSE 0 END) AS documents,"
            "       SUM(tokens) AS tokens,"
            "       SUM(CASE WHEN role = 'user' THEN tokens ELSE 0 END) AS prompt_tokens,"
            "       SUM(CASE WHEN role = 'assistant' THEN tokens ELSE 0 END) AS completion_tokens,"
            "       SUM(cost) AS cost,"
            "       MIN(created_at) AS started_at,"
            "       MAX(created_at) AS updated_at"
            "  FROM messages WHERE session = ?",
            (ROLE_USER, KIND_DOCUMENT, session),
        ).fetchone()
        stats = dict(row) if row else {}
        stats["session"] = session
        stats["messages"] = stats.get("messages") or 0
        stats["questions"] = stats.get("questions") or 0
        stats["documents"] = stats.get("documents") or 0
        for поле in ("tokens", "prompt_tokens", "completion_tokens"):
            stats[поле] = stats.get(поле) or 0
        stats["cost"] = stats.get("cost") or 0.0

        выжимки = self.summaries(session)
        stats["summaries"] = len(выжимки)
        stats["summary_tokens"] = sum(в["tokens"] for в in выжимки)
        stats["summary_cost"] = sum(в["cost"] for в in выжимки)
        stats["compressed_messages"] = выжимки[-1]["messages"] if выжимки else 0
        return stats

    def growth(self, session: str = DEFAULT_SESSION) -> list[dict[str, Any]]:
        """Показывает, как накапливались токены и деньги от реплики к реплике.

        Именно эта выборка отвечает на вопрос дня «как растёт стоимость по мере
        диалога»: по каждому обмену видно, сколько ушло в запрос, сколько
        вернулось в ответе и во что обошёлся разговор нарастающим итогом.
        """
        строки = self._db.execute(
            "SELECT role, tokens, cost, created_at, kind FROM messages"
            " WHERE session = ? ORDER BY id",
            (session,),
        ).fetchall()

        шаги: list[dict[str, Any]] = []
        всего_токенов = 0
        всего_денег = 0.0
        обмен: dict[str, Any] | None = None

        for строка in строки:
            всего_токенов += строка["tokens"]
            всего_денег += строка["cost"]
            if строка["role"] == ROLE_USER:
                обмен = {
                    "number": len(шаги) + 1,
                    "kind": строка["kind"],
                    "prompt_tokens": строка["tokens"],
                    "completion_tokens": 0,
                    "cost": 0.0,
                    "at": строка["created_at"],
                }
            elif обмен is not None:
                обмен["completion_tokens"] = строка["tokens"]
                обмен["cost"] = строка["cost"]
                обмен["total_tokens"] = всего_токенов
                обмен["total_cost"] = всего_денег
                шаги.append(обмен)
                обмен = None
        return шаги

    # --- выжимки -------------------------------------------------------------

    def save_summary(
        self,
        session: str,
        content: str,
        covers_until: int,
        messages: int,
        model: str = "",
        tokens: int = 0,
        cost: float = 0.0,
    ) -> int:
        """Сохраняет новое поколение выжимки. Прежние остаются для истории.

        Старые поколения не удаляются намеренно: по ним видно, как конспект
        менялся, и можно заметить, если детали начали размываться.
        """
        content = (content or "").strip()
        if not content:
            raise MemoryError("Пустая выжимка не сохраняется.")

        поколение = len(self.summaries(session)) + 1
        try:
            курсор = self._db.execute(
                "INSERT INTO summaries"
                " (session, content, covers_until, messages, generation,"
                "  model, tokens, cost, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (session or DEFAULT_SESSION, content, int(covers_until), int(messages),
                 поколение, model, int(tokens), float(cost), _now()),
            )
            self._db.commit()
        except sqlite3.Error as exc:
            raise MemoryError(f"Не удалось сохранить выжимку: {exc}") from exc
        return int(курсор.lastrowid)

    def latest_summary(self, session: str = DEFAULT_SESSION) -> dict[str, Any] | None:
        """Последнее поколение выжимки или None, если сжатия ещё не было."""
        строка = self._db.execute(
            "SELECT * FROM summaries WHERE session = ? ORDER BY id DESC LIMIT 1",
            (session,),
        ).fetchone()
        return dict(строка) if строка else None

    def summaries(self, session: str = DEFAULT_SESSION) -> list[dict[str, Any]]:
        """Все поколения выжимок по возрастанию — видно, как менялся конспект."""
        строки = self._db.execute(
            "SELECT * FROM summaries WHERE session = ? ORDER BY id", (session,)
        ).fetchall()
        return [dict(строка) for строка in строки]

    def pending(self, session: str = DEFAULT_SESSION, keep_last: int = 6) -> list[dict]:
        """Сообщения, которые уже состарились и ждут сжатия.

        Это всё, что идёт после последней выжимки, кроме keep_last свежих:
        свежие остаются дословными, потому что в них уточнения и местоимения,
        без которых разговор рассыпается.
        """
        выжимка = self.latest_summary(session)
        граница = выжимка["covers_until"] if выжимка else 0
        строки = self._db.execute(
            "SELECT * FROM messages WHERE session = ? AND id > ? ORDER BY id",
            (session, граница),
        ).fetchall()
        сообщения = [dict(строка) for строка in строки]
        if keep_last <= 0:
            return сообщения
        return сообщения[:-keep_last] if len(сообщения) > keep_last else []

    def after_summary(self, session: str = DEFAULT_SESSION, limit: int = 0) -> list[dict]:
        """Все сообщения, ещё не вошедшие в выжимку, — они уходят в запрос дословно.

        Важно, что это именно ВСЕ сообщения после границы выжимки, а не только
        последние keep_last. Разница неочевидна и стоила ошибки: keep_last
        решает, что НЕ сжимать, а не что отправлять. Если отправлять только
        последние keep_last, то реплики, уже вышедшие из этого окна, но ещё не
        попавшие в выжимку, исчезают из разговора бесследно — модель их просто
        не видит, хотя в базе они есть.

        limit — предохранитель на случай, если сжатие почему-то не сработало:
        0 означает «без ограничения».
        """
        выжимка = self.latest_summary(session)
        граница = выжимка["covers_until"] if выжимка else 0
        строки = self._db.execute(
            "SELECT role, content FROM messages WHERE session = ? AND id > ? ORDER BY id",
            (session, граница),
        ).fetchall()
        сообщения = [{"role": с["role"], "content": с["content"]} for с in строки]
        if limit > 0:
            сообщения = сообщения[-limit:]
        # Разговор для модели должен начинаться с реплики пользователя.
        while сообщения and сообщения[0]["role"] != ROLE_USER:
            сообщения.pop(0)
        return сообщения

    # --- удаление ------------------------------------------------------------

    def clear(self, session: str = DEFAULT_SESSION) -> int:
        """Стирает историю сессии вместе с выжимками; возвращает число сообщений.

        Выжимки удаляются заодно: конспект без разговора бессмыслен, а оставь
        мы его — новый разговор начался бы с чужого контекста.
        """
        cursor = self._db.execute("DELETE FROM messages WHERE session = ?", (session,))
        self._db.execute("DELETE FROM summaries WHERE session = ?", (session,))
        self._db.commit()
        return cursor.rowcount

    def drop_summaries(self, session: str = DEFAULT_SESSION) -> int:
        """Удаляет только выжимки, оставляя разговор: нужно для пересборки."""
        cursor = self._db.execute("DELETE FROM summaries WHERE session = ?", (session,))
        self._db.commit()
        return cursor.rowcount

    def close(self) -> None:
        self._db.close()
