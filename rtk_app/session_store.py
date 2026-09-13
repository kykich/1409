"""
Хранилище сессии диалога на диске (JSON в папке session/).

Диалог ведётся на сервере и периодически/поэтапно сохраняется в файл
session/session.json. При запуске сервера хранилище подхватывает ранее
сохранённую историю (если она есть), тем самым сохраняя непрерывность
беседы между запусками.

Структура файла:
{
  "updated": "ISO-время последнего сохранения",
  "count":   число сообщений,
  "messages":[ ... элементы диалога ... ],
    "compact": {
    "enabled": true,       // сжатие включено (автоматическое)
    "keep": 10,            // сколько последних сообщений хранить полностью
    "summary": "",         // сжатое содержание ранней части истории
    "upto": 0              // сколько первых сообщений УЖЕ покрыты summary
  }
}

Элемент диалога (один ход):
  - пользователь: {"role": "user",  "content": "текст вопроса"}
  - ассистент:    {"role": "assistant", "content": "текст-конкат ответов",
                   "html": "готовая HTML-разметка ответа",
                   "answers": [{"label": "…", model/text…}, …]}
"""
import json
import os
import threading
import time

from . import config

__all__ = ["SessionStore"]


class SessionStore:
    """Потокобезопасное хранилище одного активного диалога в JSON-файле."""

    def __init__(self, path=None):
        self.path = path or config.SESSION_FILE
        self.lock = threading.RLock()
        self.messages = []
        self.compact = {
            "enabled": bool(config.COMPACT_ENABLED),
            "keep": config.COMPACT_KEEP,
            "summary": "",
            "upto": 0,
        }
        self.load()

    # ---- чтение ----
    def load(self):
        """Читает сохранённую историю из файла (None/нет файла -> пустая)."""
        with self.lock:
            self.messages = []
            self.compact = {
                "enabled": bool(config.COMPACT_ENABLED),
                "keep": config.COMPACT_KEEP,
                "summary": "",
                "upto": 0,
            }
            if not self.path or not os.path.isfile(self.path):
                return
            try:
                with open(self.path, encoding="utf-8") as fh:
                    data = json.load(fh)
                msgs = data.get("messages") if isinstance(data, dict) else None
                if isinstance(msgs, list):
                    # оставляем только корректные элементы диалога
                    self.messages = [m for m in msgs
                                     if isinstance(m, dict)
                                     and m.get("role") in ("user", "assistant")]
                # Загружаем настройки сжатия
                compact = data.get("compact")
                if isinstance(compact, dict):
                    self.compact["enabled"] = bool(compact.get(
                        "enabled", config.COMPACT_ENABLED))
                    self.compact["keep"] = int(compact.get("keep", config.COMPACT_KEEP))
                    self.compact["summary"] = str(compact.get("summary", ""))
                    self.compact["upto"] = int(compact.get("upto", 0))
                    # Согласованность: если summary есть, но граница покрытия
                    # не задана (старый файл без поля upto) — выводим её из
                    # правила «всё, кроме последних keep сообщений».
                    # При keep = 0 сжатие не применяется — границу не выводим.
                    if (self.compact["summary"] and self.compact["upto"] == 0
                            and self.compact["keep"] > 0):
                        keep = max(0, self.compact["keep"])
                        self.compact["upto"] = max(0, len(self.messages) - keep)
            except Exception:
                # Битый файл не должен ронять сервер — стартуем с чистой истрии
                self.messages = []

    def snapshot(self):
        """Копия списка сообщений текущей сессии."""
        with self.lock:
            return list(self.messages)

    def snapshot_full(self):
        """Полная копия сессии: сообщения + настройки сжатия."""
        with self.lock:
            return {
                "messages": list(self.messages),
                "compact": dict(self.compact),
            }

    def has_history(self):
        """True, если в сессии уже есть сохранённый диалог."""
        with self.lock:
            return bool(self.messages)

    # ---- запись ----
    def append_turn(self, question, assistant_entry):
        """Добавляет в историю ход «вопрос -> ответ(ы)» и сохраняет на диск."""
        with self.lock:
            self.messages.append({"role": "user", "content": str(question)})
            entry = dict(assistant_entry or {})
            entry.setdefault("role", "assistant")
            self.messages.append(entry)
            self._save_locked()

    def save(self):
        """Принудительное сохранение текущего состояния на диск."""
        with self.lock:
            self._save_locked()

    def _save_locked(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        data = {
            "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "count": len(self.messages),
            "messages": self.messages,
            "compact": self.compact,
        }
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except Exception as exc:                    # не роняем запрос из-за диска
            print("[session] не удалось сохранить сессию: %s" % exc, flush=True)

    # ---- сброс ----
    def reset(self):
        """Очищает сессию: начинает новый разговор с чистого листа."""
        with self.lock:
            self.messages = []
            self.compact = {
                "enabled": bool(config.COMPACT_ENABLED),
                "keep": config.COMPACT_KEEP,
                "summary": "",
                "upto": 0,
            }
            self._save_locked()

    # ---- Настройки сжатия ----

    def set_compact(self, enabled, keep=None, summary=None, upto=None):
        """Обновляет настройки сжатия и сохраняет на диск."""
        with self.lock:
            if enabled is not None:
                self.compact["enabled"] = bool(enabled)
            if keep is not None:
                self.compact["keep"] = int(keep)
            if summary is not None:
                self.compact["summary"] = str(summary)
            if upto is not None:
                self.compact["upto"] = int(upto)
            self._save_locked()

    def get_compact(self):
        """Возвращает текущие настройки сжатия (копия)."""
        with self.lock:
            return dict(self.compact)

    def get_compacted_messages(self):
        """Возвращает историю для отправки в запрос (сжатие подставлено).

        Правило управления контекстом:
          * если сжатие включено и есть summary — в запрос уходят
            [summary] + последние keep сообщений (как есть);
          * сообщения, уже покрытые summary (индекс < upto), в запрос
            НЕ попадают — вместо них идёт summary;
          * если сжатие выключено — возвращаются все сообщения как есть.
        """
        with self.lock:
            if not self.compact["enabled"] or not self.compact["summary"]:
                return list(self.messages)
            keep = max(0, self.compact["keep"])
            # keep = 0 → сжатие не применяется: отдаём полную историю как есть.
            if keep <= 0:
                return list(self.messages)
            recent = list(self.messages[-keep:]) if keep > 0 else []
            summary_msg = {
                "role": "system",
                "content": self.compact["summary"],
            }
            return [summary_msg] + recent

    def context_stats(self):
        """Статистика управления контекстом (для интерфейса).

        Возвращает dict:
          total      — всего сообщений в полной истории;
          keep       — сколько последних сообщений передаётся полностью;
          compacted  — сколько сообщений покрыто summary (сжато), >= 0;
          sent       — сколько сообщений реально уйдёт в запрос
                       (summary + последние keep);
          from_summary — сколько сообщений заменено summary в запросе
                       (это же число = compacted, т.е. «использовано из summary»);
          has_summary  — сгенерирован ли summary вообще;
          summary_len  — длина текста summary.
        """
        with self.lock:
            total = len(self.messages)
            keep = max(0, self.compact["keep"])
            upto = max(0, self.compact.get("upto", 0))
            # keep = 0 → сжатие не применяется (ни подстановка, ни покрытие).
            has_summary = bool(self.compact["enabled"] and self.compact["summary"]
                               and keep > 0)
            if has_summary:
                # Граница покрытия: сохранённое upto. Если оно не задано
                # (0) при непустой истории — выводим «всё, кроме последних keep».
                if upto <= 0 and total > 0:
                    upto = max(0, total - keep)
                # Покрыто summary ровно `upto` сообщений (но не больше общего).
                compacted = min(upto, total)
                recent = min(keep, total)
                sent = recent + 1  # +1 = само сообщение summary
                from_summary = compacted
            else:
                compacted = 0
                sent = total
                from_summary = 0
            return {
                "total": total,
                "keep": keep,
                "compacted": compacted,
                "sent": sent,
                "from_summary": from_summary,
                "has_summary": has_summary,
                "summary_len": len(self.compact["summary"]) if has_summary else 0,
            }

    def head_to_compact(self):
        """Возвращает НОВЫЕ вытесненные (ещё не сжатые) сообщения.

        Это сообщения, которые ещё не покрыты summary (индекс >= upto),
        но уже вытеснены за пределы последних keep сообщений — их нужно
        ДОПИСАТЬ в существующее summary (инкрементальное сжатие).

        Возвращает кортеж (head, end_index):
          head      — список новых вытесненных сообщений (может быть пуст);
          end_index — индекс, до которого (не включая) история будет покрыта
                      summary после дописывания (новая граница upto).
        """
        with self.lock:
            keep = max(0, self.compact["keep"])
            # keep = 0 → сжатие не применяется: сжимать нечего.
            if keep <= 0:
                return [], max(0, self.compact.get("upto", 0))
            upto = max(0, self.compact.get("upto", 0))
            end = len(self.messages) - keep
            if end <= upto:
                return [], upto
            head = list(self.messages[upto:end])
            return head, end

    def should_auto_compact(self):
        """Есть ли вытесненные (несжатые) сообщения для дописывания в summary.

        Логика инкрементальная: сжатие начинается, как только история
        становится больше keep (первое вытесненное сообщение), и продолжается
        по мере дальнейшего вытеснения. Достаточно хотя бы одного сообщения.
        """
        # Сжатие не применяется при keep = 0 — сжимать нечего.
        if max(0, self.compact["keep"]) <= 0:
            return False
        head, _end = self.head_to_compact()
        return len(head) >= 1

    def apply_summary(self, summary, upto=None, keep=None):
        """Сохраняет summary и границу покрытия (до какого сообщения)."""
        with self.lock:
            self.compact["enabled"] = True
            if keep is not None:
                self.compact["keep"] = max(0, int(keep))
            if upto is not None:
                self.compact["upto"] = max(0, int(upto))
            if summary is not None:
                self.compact["summary"] = str(summary)
            self._save_locked()
