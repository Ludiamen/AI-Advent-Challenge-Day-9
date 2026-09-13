#!/usr/bin/env python3
"""Веб-интерфейс агента: чат с памятью, выбор модели и сессии, разбор документов.

Запуск:
    python web.py
Затем открыть http://127.0.0.1:5000

Главное отличие от предыдущего дня: страница при открытии подтягивает
сохранённый диалог, поэтому после перезапуска сервера разговор продолжается
с того же места. Историю держит агент в SQLite, веб её только показывает.

Как и cli.py, этот файл — только интерфейс. Он создаёт агента, вызывает его
публичные методы и рисует ответ. Про LLM, её адрес, ключ и формат запроса он
не знает ничего.
"""

from __future__ import annotations

import logging
import os
import tempfile
import uuid

from flask import Flask, jsonify, render_template, request, send_file

from agent import AgentError, DocumentAnalysisAgent
from agent import catalog, tokens

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 МБ, как и у агента

agent = DocumentAnalysisAgent()

# Последние разборы по идентификатору — источник данных для выгрузки в Excel.
_analyses: dict[str, dict] = {}

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")


def _apply(данные: dict) -> None:
    """Применяет к агенту модель и сессию, пришедшие с формы.

    Веб-страница живёт дольше запроса, поэтому выбранные пользователем модель
    и тема присылаются с каждым обращением: так после перезапуска сервера
    страница не окажется рассинхронизирована с агентом.
    """
    сессия = (данные.get("session") or "").strip()
    if сессия and сессия != agent.session:
        agent.switch_session(сессия)
    модель = (данные.get("model") or "").strip()
    if модель and модель != agent.model_key:
        agent.switch_model(модель)


@app.route("/", methods=["GET"])
def index():
    """Страница чата. История подгружается отдельным запросом при открытии."""
    return render_template("index.html", info=agent.info(), models=catalog.describe())


@app.get("/api/state")
def api_state():
    """Текущее состояние агента и сохранённая история — этим страница и оживает.

    Модель и сессия приходят в строке запроса ровно так же, как в POST-ручках.
    Без этого получалось расхождение: агент один на весь сервер и помнит ту
    сессию, в которую был задан последний вопрос, а страница уже показывает
    другую — выбранную в форме. Тогда кнопки «Расход» и «Выжимка» честно
    отдавали цифры, но чужой сессии. Смена сессии здесь — не побочный эффект,
    а именно то, что пользователь и просил, выбрав её в форме.
    """
    try:
        _apply(request.args)
    except AgentError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({
        "info": agent.info(),
        "history": agent.history(),
        "sessions": agent.sessions(),
        "usage": agent.usage(),
        "growth": agent.growth(),
        "budget": agent.budget().to_dict(),
        "summary": agent.summary(),
    })


@app.post("/api/ask")
def api_ask():
    """Свободный вопрос: агент отвечает с учётом прошлых реплик и сохраняет пару."""
    данные = request.json or {}
    try:
        _apply(данные)
        вопрос = данные.get("question", "")
        # Оценка снимается ДО запроса: после ответа она уже перезаписана фактом.
        оценка = agent.budget(вопрос).to_dict()
        ответ = agent.ask(вопрос)
    except AgentError as exc:
        # Переполнение контекста — тоже ответ: интерфейс должен показать цифры,
        # а не просто «что-то пошло не так».
        return jsonify({"error": str(exc), "budget": agent.budget().to_dict(),
                        "info": agent.info()}), 502
    сжатие = dict(agent.last_compression)
    agent.last_compression = {}
    return jsonify({
        "answer": ответ,
        "info": agent.info(),
        "compression": сжатие,
        "budget": оценка,
        "usage": agent.last_usage,
        "session_usage": agent.usage(),
        "growth": agent.growth(),
    })


@app.post("/api/analyze")
def api_analyze():
    """Разбор загруженного документа.

    Файл кладётся во временную директорию и удаляется сразу после разбора:
    держать чужие договоры на диске дольше необходимого незачем.
    """
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        return jsonify({"error": "Файл не выбран."}), 400

    try:
        _apply(request.form)
    except AgentError as exc:
        return jsonify({"error": str(exc)}), 400

    extension = os.path.splitext(uploaded.filename)[1].lower()
    if extension not in agent.info()["supported_formats"]:
        return jsonify({
            "error": "Неподдерживаемый формат. Допустимы: "
                     + ", ".join(agent.info()["supported_formats"])
        }), 400

    temp_path = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4().hex}{extension}")
    uploaded.save(temp_path)
    try:
        result = agent.analyze(temp_path)
        # Имя во временном файле случайное — возвращаем пользователю его собственное.
        result["document_info"]["source_file"] = uploaded.filename
    except AgentError as exc:
        return jsonify({"error": str(exc)}), 422
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass

    analysis_id = uuid.uuid4().hex
    _analyses[analysis_id] = result
    result["analysis_id"] = analysis_id
    result["info"] = agent.info()
    result["session_usage"] = agent.usage()
    result["growth"] = agent.growth()
    return jsonify(result)


@app.post("/api/analyze/text")
def api_analyze_text():
    """Разбор текста, вставленного прямо в форму, без загрузки файла."""
    данные = request.json or {}
    try:
        _apply(данные)
        result = agent.analyze_text(данные.get("text", ""))
    except AgentError as exc:
        return jsonify({"error": str(exc)}), 422

    analysis_id = uuid.uuid4().hex
    _analyses[analysis_id] = result
    result["analysis_id"] = analysis_id
    return jsonify(result)


@app.post("/api/compress")
def api_compress():
    """Сворачивает давние реплики прямо сейчас или пересобирает конспект."""
    данные = request.json or {}
    try:
        _apply(данные)
        итог = (
            agent.rebuild_summary() if данные.get("rebuild")
            else agent.compress(force=True)
        )
    except AgentError as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify({
        "result": итог, "info": agent.info(),
        "summary": agent.summary(), "usage": agent.usage(),
    })


@app.post("/api/history/clear")
def api_clear():
    """Стирает историю текущей сессии."""
    данные = request.json or {}
    try:
        _apply(данные)
    except AgentError as exc:
        return jsonify({"error": str(exc)}), 400
    удалено = agent.clear_history()
    return jsonify({
        "removed": удалено, "info": agent.info(),
        "usage": agent.usage(), "growth": agent.growth(),
    })


@app.get("/api/export/<analysis_id>")
def api_export(analysis_id: str):
    """Отдаёт сводную таблицу Excel по ранее выполненному разбору."""
    result = _analyses.get(analysis_id)
    if result is None:
        return jsonify({"error": "Результат не найден — повторите разбор документа."}), 404

    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"фсбу-свод-{analysis_id[:8]}.xlsx")
    try:
        agent.build_pivot_report(result["items"], path)
    except AgentError as exc:
        return jsonify({"error": str(exc)}), 500

    return send_file(path, as_attachment=True, download_name=os.path.basename(path))


@app.get("/api/health")
def api_health():
    """Проверка готовности: поднят ли сервис и задан ли ключ API."""
    return jsonify({"status": "ok", "agent": agent.info()})


if __name__ == "__main__":
    # debug=False: не отдаём отладчик и трассировки наружу.
    app.run(host="127.0.0.1", port=5000, debug=False)
