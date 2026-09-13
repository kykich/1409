

"""Встроенный HTTP-сервер (стандартная библиотека).

Чат "только в окне": сервер ничего не хранит на диске. История ведётся в
памяти вкладки и присылается целиком в POST /api/ask.

Сервер работает через АГЕНТА (rtk_app.agent.Agent) — отдельную сущность,
которая инкапсулирует всю логику запросов к LLM. Агент принимает вопрос и
историю диалога, сам обращается к моделям через API и возвращает готовый
результат (html, text, ответы моделей). HTTP-обработчик лишь передаёт данные
между браузером и агентом.

Запрос пользователя уходит агенту, который опрашивает модели:
  - DeepSeek-flash,
  - GigaChat (базовая, простая).

JSON-API:
    GET  /                  - страница (index.html)
    GET  /css/*,/js/*       - стили и скрипты
    GET  /api/model         - список моделей, которые обслуживает агент
    POST /api/ask           - {question, models[], max_tokens?} -> ответ
    POST /api/ask_files     - {files[], question?, models[], max_tokens?} ->
                              разовый анализ приложенных файлов (не сохраняется)
    POST /api/analyze       - {question, answers[]} -> анализ (через агента)
"""
import json
import mimetypes
import os
import webbrowser
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from rtk_app import config
from rtk_app.agent import Agent
from rtk_app.session_store import SessionStore


class _ServerState:
    """Глобальное состояние сервера: агент (единая сущность) и сессия."""
    agent = None
    session = None


def _set_agent(agent):
    """Запоминает агента, который обслуживает все запросы."""
    _ServerState.agent = agent


def _ensure_session():
    """Лениво создаёт единственное хранилище сессии диалога."""
    if _ServerState.session is None:
        _ServerState.session = SessionStore()
    return _ServerState.session


class WebRequestHandler(BaseHTTPRequestHandler):
    server_version = "MultiModelChat/1.0"

    @property
    def agent(self):
        """Единый агент (rtk_app.agent.Agent), созданный при старте сервера."""
        return _ServerState.agent

    @property
    def session(self):
        """Хранилище сессии диалога (rtk_app.session_store.SessionStore)."""
        return _ensure_session()

    def _send_bytes(self, status, body, content_type):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status, obj):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, payload, "application/json; charset=utf-8")

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))

    # ---------------- GET ----------------
    def do_GET(self):
        import urllib.parse
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path in ("/", "/index.html"):
            return self._serve_static_safe("index.html")
        if path.startswith(("/css/", "/js/")):
            return self._serve_static_safe(path.lstrip("/"))
        if path == "/api/model":
            return self._send_json(200, {
                "ok": True,
                "model": self.agent.label if self.agent else config.MODEL,
                "models": [m["label"] for m in
                           (self.agent.available() if self.agent else [])],
                "available": (self.agent.available() if self.agent else []),
            })
        if path == "/api/session":
            has = self.session.has_history()
            return self._send_json(200, {
                "ok": True,
                "has_history": has,
                "messages": self.session.snapshot(),
                "compact": self.session.get_compact(),
                "context": self.session.context_stats(),
            })
        self._send_json(404, {"ok": False, "error": "Not Found"})

    def do_POST(self):
        import urllib.parse
        if urllib.parse.urlparse(self.path).path == "/api/ask":
            return self._handle_ask()
        if urllib.parse.urlparse(self.path).path == "/api/ask_files":
            return self._handle_ask_files()
        if urllib.parse.urlparse(self.path).path == "/api/analyze":
            return self._handle_analyze()
        if urllib.parse.urlparse(self.path).path == "/api/newchat":
            self.session.reset()
            return self._send_json(200, {"ok": True,
                                         "messages": self.session.snapshot()})
        if urllib.parse.urlparse(self.path).path == "/api/compact":
            return self._handle_compact()
        if urllib.parse.urlparse(self.path).path == "/api/compact_summary":
            return self._handle_compact_summary()
        self._send_json(404, {"ok": False, "error": "Not Found"})

    # ---------------- Статика ----------------
    def _serve_static_safe(self, rel):
        root_real = os.path.realpath(config.WEB_ROOT)
        full = os.path.realpath(os.path.join(root_real, os.path.normpath(rel)))
        if not (full.startswith(root_real + os.sep) or full == root_real):
            return self._send_bytes(403, "Forbidden", "text/plain")
        if not os.path.isfile(full):
            return self._send_bytes(404, "Not Found", "text/plain")
        ctype, _ = mimetypes.guess_type(full)
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---------------- Обработчики: делегируют всю работу агенту ----------------

    def _maybe_auto_compact(self):
        """Инкрементальное сжатие истории: дописывает вытесненные сообщения.

        Логика: как только история становится больше keep, каждое новое
        вытесненное сообщение ДОПИСЫВАЕТСЯ в summary (Вариант A). Для keep = 5
        сжатие начинается с 6-го сообщения и продолжается по мере вытеснения.
        Summary хранится ОТДЕЛЬНО (compact.summary) и подставляется в
        следующий запрос вместо вытесненной части истории.
        """
        try:
            if not self.session.should_auto_compact():
                return
            head, end = self.session.head_to_compact()
            if not head:
                return
            keep = self.session.get_compact().get("keep", config.COMPACT_KEEP)
            prev_summary = self.session.get_compact().get("summary", "")
            # Дописываем вытесненную часть в существующее summary.
            summary = self.agent.compact_update(prev_summary, head)
            if summary:
                # Сохраняем summary и новую границу: сообщения [0:end) покрыты.
                self.session.apply_summary(summary, upto=end, keep=keep)
                print("[COMPACT] сжатие: +%d сообщ. -> summary (upto=%d)"
                      % (len(head), end), flush=True)
        except Exception as exc:
            # Сжатие не должно ломать основной запрос.
            print("[COMPACT] сжатие не удалось: %s" % exc, flush=True)

    def _handle_ask(self):
        """Принимает запрос пользователя и передаёт его агенту.

        История диалога хранится на сервере (папка session/) и подхватывается
        при каждом запуске. Сервер передаёт агенту историю, выбор моделей и
        температуру, а после успешного ответа добавляет ход в сессию и
        сохраняет её на диск.
        """
        data = self._read_json_body()
        if not data:
            return self._send_json(400, {"ok": False, "error": "Bad JSON."})
        question = str(data.get("question", "")).strip()
        # Выбор моделей пользователем: [{"label": "…", "temperature": 0.7}]
        selected = data.get("models")
        if not isinstance(selected, list) or not selected:
            selected = None

        # Глобальное ограничение max_tokens (None — не применяется)
        max_tokens = data.get("max_tokens")
        try:
            max_tokens = int(max_tokens)
        except (TypeError, ValueError):
            max_tokens = None

        # Настройки сжатия
        compact = data.get("compact")
        if isinstance(compact, dict):
            # Клиент может прислать свои настройки (enabled/keep) — применяем.
            self.session.set_compact(compact.get("enabled"),
                                     compact.get("keep"),
                                     None)
        compact = self.session.get_compact()

        # Управление контекстом: в запрос уходит сжатая история
        # (summary + последние keep сообщений) вместо полной истории.
        history = self.session.get_compacted_messages()

        result = self.agent.answer(question, history, selected,
                                   max_tokens=max_tokens,
                                   compact=compact)
        if result.get("ok"):
            # По одному ходу на ответ модели с уже готовой разметкой
            self.session.append_turn(question, {
                "role": "assistant",
                "content": result.get("text", ""),
                "html": result.get("html", ""),
                "answers": result.get("answers", []),
                "meta": result.get("meta", ""),
                "usage": result.get("usage"),
            })
            # Сжатие: если появились вытесненные сообщения — дописываем их
            # в summary (инкрементально; для keep=5 — начиная с 6-го сообщения).
            self._maybe_auto_compact()
            result["compact"] = self.session.get_compact()
            # Статистика управления контекстом (сжатых/использованных из
            # summary сообщений) для панели интерфейса.
            result["context"] = self.session.context_stats()
        return self._send_json(200, result)

    def _handle_ask_files(self):
        """Разовый анализ приложенных файлов.

        Файлы уже прочитаны в браузере и приходят как текст {name, path,
        content}. Сервер НИЧЕГО не сохраняет на диске: результат анализа
        возвращается клиенту и в историю сессии не пишется.
        """
        data = self._read_json_body()
        if not data:
            return self._send_json(400, {"ok": False, "error": "Bad JSON."})
        files = data.get("files")
        if not isinstance(files, list) or not files:
            return self._send_json(400, {
                "ok": False,
                "error": "Не подключено ни одного файла для анализа.",
            })
        # Оставляем только корректные записи с текстовым содержимым.
        clean = []
        for f in files:
            if not isinstance(f, dict):
                continue
            content = f.get("content")
            if not isinstance(content, str):
                content = "" if content is None else str(content)
            clean.append({
                "name": str(f.get("name", "")),
                "path": str(f.get("path", "")),
                "content": content,
            })
        if not clean:
            return self._send_json(400, {
                "ok": False,
                "error": "Нет пригодных файлов для анализа.",
            })
        question = str(data.get("question", "")).strip()
        selected = data.get("models")
        if not isinstance(selected, list) or not selected:
            selected = None
        max_tokens = data.get("max_tokens")
        try:
            max_tokens = int(max_tokens)
        except (TypeError, ValueError):
            max_tokens = None

        result = self.agent.answer_files(clean, question,
                                         selected=selected,
                                         max_tokens=max_tokens)
        # ВАЖНО: в сессию не пишем — анализ разовый, сервер ничего не хранит.
        return self._send_json(200, result)

    def _handle_analyze(self):
        """Передаёт агенту запрос на сравнение нескольких ответов моделей."""
        data = self._read_json_body()
        if not data:
            return self._send_json(400, {"ok": False, "error": "Bad JSON."})
        answers = data.get("answers")
        if not isinstance(answers, list) or not answers:
            return self._send_json(400, {
                "ok": False,
                "error": "Нет ответов для анализа.",
            })
        question = str(data.get("question", "")).strip()
        result = self.agent.analyze(question, answers)
        return self._send_json(200, result)

    def _handle_compact(self):
        """Обновляет настройки сжатия сессии (enabled/keep/summary)."""
        data = self._read_json_body()
        if not data:
            return self._send_json(400, {"ok": False, "error": "Bad JSON."})
        enabled = data.get("enabled")
        keep = data.get("keep")
        summary = data.get("summary")
        # Пустой summary из клиента НЕ должен затирать уже сгенерированный
        # на сервере — иначе настройки каждый раз обнуляют сжатие.
        if not summary:
            summary = None
        self.session.set_compact(enabled, keep, summary)
        return self._send_json(200, {
            "ok": True,
            "compact": self.session.get_compact(),
        })

    def _handle_compact_summary(self):
        """Дописывает вытесненную часть истории в summary (по запросу).

        Сжимаем только то, что вышло за пределы последних keep сообщений,
        и ДОПИСЫВАЕМ это в существующее summary (инкрементально, Вариант A).
        Summary сохраняется отдельно и будет подставлено в следующий запрос
        вместо вытесненной части истории.
        """
        data = self._read_json_body()
        if not data:
            return self._send_json(400, {"ok": False, "error": "Bad JSON."})
        keep = data.get("keep", config.COMPACT_KEEP)
        try:
            keep = max(0, int(keep))
        except (TypeError, ValueError):
            keep = config.COMPACT_KEEP
        # Применяем актуальный keep перед вычислением вытесняемой части.
        self.session.set_compact(True, keep, None)
        head, end = self.session.head_to_compact()
        if not head:
            return self._send_json(200, {
                "ok": True, "summary": self.session.get_compact().get("summary", ""),
                "detail": "Нет вытесненных сообщений — сжимать нечего.",
            })
        prev_summary = self.session.get_compact().get("summary", "")
        summary = self.agent.compact_update(prev_summary, head)
        if summary:
            self.session.apply_summary(summary, upto=end, keep=keep)
            return self._send_json(200, {
                "ok": True, "summary": summary,
                "detail": "Summary дополнен (%d сообщ.)." % len(head),
            })
        return self._send_json(200, {
            "ok": False, "summary": self.session.get_compact().get("summary", ""),
            "detail": "Не удалось обновить summary.",
        })


def create_server(agent, host=None, port=None):
    """Создаёт HTTP-сервер, связанный с конкретным экземпляром агента."""
    if agent is None:
        raise ValueError("create_server: требуется экземпляр агента (Agent).")
    _set_agent(agent)
    addr = (host or config.WEB_HOST, port or config.WEB_PORT)
    return ThreadingHTTPServer(addr, WebRequestHandler)


def _open_browser_later(url, delay=1.0):
    def _job():
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception as exc:
            print("[WEB] браузер: %s" % exc)
    threading.Thread(target=_job, daemon=True).start()


def serve(agent_or_key, host=None, port=None, open_page=True, agent=None):
    """Запускает сервер, используя агента в качестве единой сущности.

    agent_or_key — либо строка API-ключа DeepSeek (тогда создаётся агент),
    либо уже готовый экземпляр rtk_app.agent.Agent.
    agent — ещё один способ передать готового агента явно.
    """
    if agent is None:
        agent = agent_or_key

    # Агент — отдельная сущность, построенная вокруг ключа либо переданная.
    if not isinstance(agent, Agent):
        agent = Agent(agent)

    httpd = create_server(agent, host, port)
    shown_host, shown_port = httpd.server_address[:2]
    url = "http://%s:%s/" % (shown_host, shown_port)
    print("[WEB] Сервер запущен: %s" % url)
    print("[WEB] Агент обслуживает модели: %s" % agent.label)
    if open_page:
        _open_browser_later(url)
    print("[WEB] Остановка: нажмите Ctrl+C.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[WEB] Остановка...")
    finally:
        httpd.server_close()


def main():
    import sys
    from rtk_app.key_store import read_api_key
    try:
        api_key = read_api_key(config.DS_KEY_FILE)
        print("[OK] Ключ DeepSeek прочитан из %s" % config.DS_KEY_FILE)
    except FileNotFoundError:
        # Файла ключа нет: сервер всё равно стартует — интерфейс откроется,
        # а недоступные модели покажут подсказку, как добавить ключ
        # (самодиагностика уже напечатана через rtk_web.py/key_check).
        api_key = ""
        print("[!] Файл %s не найден. DeepSeek-flash в чате станет "
              "недоступен до тех пор, пока вы не впишете ключ в apidpsk.txt."
              % config.DS_KEY_FILE)
    args = sys.argv[1:]
    host, port = config.WEB_HOST, config.WEB_PORT
    if args and args[0].isdigit():
        port = int(args[0])
        if len(args) > 1:
            host = args[1]
    sys.exit(serve(api_key, host, port) or 0)