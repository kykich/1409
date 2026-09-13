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
    "keep": 0,             // сколько последних сообщений хранить полностью
    "summary": "",         // сжатое содержание ранней части истории
    "upto": 0              // сколько первых сообщений УЖЕ покрыты summary
  },
  "strategy": "none",       // активная стратегия управления контекстом
  "window": 10,             // окно N для Sliding/Facts
  "facts": {"цель": "…"},   // key-value память (стратегия Facts)
  "active_branch": 0,       // индекс активной ветки
  "branches": [ {           // ветки диалога (стратегия Branch)
      "name": "main",
      "messages": [ … ]     // собственные сообщения ветки
  } ]
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

VALID_STRATEGIES = ("none", "sliding", "facts", "branch")


class SessionStore:
    """Потокобезопасное хранилище одного активного диалога в JSON-файле."""

    def __init__(self, path=None):
        self.path = path or config.SESSION_FILE
        self.lock = threading.RLock()
        self.messages = []
        self.compact = self._default_compact()
        # Стратегия управления контекстом.
        self.strategy = config.STRATEGY
        self.window = int(config.STRATEGY_WINDOW)
        # Key-value память (стратегия Facts).
        self.facts = {}
        # Ветки диалога (стратегия Branch). По умолчанию одна основная ветка.
        self.branches = [{"name": "main", "messages": []}]
        self.active_branch = 0
        self.load()

    @staticmethod
    def _default_compact():
        return {
            "enabled": bool(config.COMPACT_ENABLED),
            "keep": config.COMPACT_KEEP,
            "summary": "",
            "upto": 0,
        }

    # ---- чтение ----
    def load(self):
        """Читает сохранённую историю из файла (None/нет файла -> пустая)."""
        with self.lock:
            self.messages = []
            self.compact = self._default_compact()
            self.strategy = config.STRATEGY
            self.window = int(config.STRATEGY_WINDOW)
            self.facts = {}
            self.branches = [{"name": "main", "messages": []}]
            self.active_branch = 0
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
                # Стратегия управления контекстом.
                strat = data.get("strategy")
                if isinstance(strat, str) and strat in VALID_STRATEGIES:
                    self.strategy = strat
                try:
                    self.window = int(data.get("window", config.STRATEGY_WINDOW))
                except (TypeError, ValueError):
                    self.window = int(config.STRATEGY_WINDOW)
                if self.window < 0:
                    self.window = 0
                # Facts (key-value память).
                facts = data.get("facts")
                if isinstance(facts, dict):
                    self.facts = {str(k): str(v) for k, v in facts.items()}
                # Ветки диалога (стратегия Branch).
                branches = data.get("branches")
                if isinstance(branches, list) and branches:
                    clean = []
                    for b in branches:
                        if not isinstance(b, dict):
                            continue
                        bmsgs = b.get("messages")
                        bmsgs = [m for m in bmsgs if isinstance(m, dict)
                                 and m.get("role") in ("user", "assistant")] \
                            if isinstance(bmsgs, list) else []
                        clean.append({"name": str(b.get("name", "branch")),
                                      "messages": bmsgs})
                    if clean:
                        self.branches = clean
                try:
                    ab = int(data.get("active_branch", 0))
                except (TypeError, ValueError):
                    ab = 0
                self.active_branch = ab if 0 <= ab < len(self.branches) else 0
                # При активной стратегии Branch сообщения берём из активной ветки.
                if self.strategy == "branch":
                    self.messages = list(self.branches[self.active_branch]["messages"])
            except Exception:
                # Битый файл не должен ронять сервер — стартуем с чистой историей
                self.messages = []

    def snapshot(self):
        """Копия списка сообщений текущей сессии (активной ветки)."""
        with self.lock:
            return list(self.messages)

    def snapshot_full(self):
        """Полная копия сессии: сообщения + настройки сжатия и стратегии."""
        with self.lock:
            return {
                "messages": list(self.messages),
                "compact": dict(self.compact),
                "strategy": self.strategy,
                "window": self.window,
                "facts": dict(self.facts),
                "branches": [{"name": b["name"], "messages": list(b["messages"])}
                             for b in self.branches],
                "active_branch": self.active_branch,
            }

    def has_history(self):
        """True, если в сессии уже есть сохранённый диалог."""
        with self.lock:
            return bool(self.messages)

    # ---- запись ----
    def append_turn(self, question, assistant_entry):
        """Добавляет в историю ход «вопрос -> ответ(ы)» и сохраняет на диск."""
        with self.lock:
            user_msg = {"role": "user", "content": str(question)}
            entry = dict(assistant_entry or {})
            entry.setdefault("role", "assistant")
            self.messages.append(user_msg)
            self.messages.append(entry)
            # При стратегии Branch история живёт в активной ветке.
            if self.strategy == "branch":
                self.branches[self.active_branch]["messages"] = list(self.messages)
            self._save_locked()

    def save(self):
        """Принудительное сохранение текущего состояния на диск."""
        with self.lock:
            self._save_locked()

    def _save_locked(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        # При стратегии Branch синхронизируем активную ветку с текущей историей.
        if self.strategy == "branch":
            self.branches[self.active_branch]["messages"] = list(self.messages)
        data = {
            "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "count": len(self.messages),
            "messages": self.messages,
            "compact": self.compact,
            "strategy": self.strategy,
            "window": self.window,
            "facts": self.facts,
            "branches": self.branches,
            "active_branch": self.active_branch,
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
            self.compact = self._default_compact()
            self.facts = {}
            self.branches = [{"name": "main", "messages": []}]
            self.active_branch = 0
            # Стратегию (strategy/window) НЕ сбрасываем — это выбор пользователя.
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
                "strategy": self.strategy,
                "window": max(0, self.window),
                "facts_count": len(self.facts),
                "branches": len(self.branches),
                "active_branch": self.active_branch,
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

    # ---- Стратегии управления контекстом ----

    def set_strategy(self, strategy=None, window=None):
        """Устанавливает активную стратегию и/или окно N и сохраняет."""
        with self.lock:
            if strategy is not None and strategy in VALID_STRATEGIES:
                self.strategy = strategy
            if window is not None:
                try:
                    w = int(window)
                except (TypeError, ValueError):
                    w = self.window
                self.window = w if w >= 0 else 0
            # При входе в режим Branch синхронизируем активную ветку.
            if self.strategy == "branch":
                self.branches[self.active_branch]["messages"] = list(self.messages)
            self._save_locked()
            return {"strategy": self.strategy, "window": self.window}

    def get_strategy(self):
        """Текущие стратегия и окно (копия)."""
        with self.lock:
            return {"strategy": self.strategy, "window": self.window}

    def _summary_messages(self):
        """Возвращает [summary] + последние keep сообщений (если сжатие активно)."""
        if not self.compact["enabled"] or not self.compact["summary"]:
            return list(self.messages)
        keep = max(0, self.compact["keep"])
        if keep <= 0:
            return list(self.messages)
        recent = list(self.messages[-keep:])
        return [{"role": "system", "content": self.compact["summary"]}] + recent

    def get_context_messages(self):
        """История для запроса с учётом АКТИВНОЙ стратегии (и сжатия).

        Комбинируем: сначала применяется стратегия (sliding/facts/branch),
        затем — сжатие summary (если включено и есть summary). Фактически
        summary применяется к выбранной стратегией истории.
        """
        with self.lock:
            strategy = self.strategy if self.strategy in VALID_STRATEGIES else "none"
            window = max(0, self.window)
            if strategy == "sliding":
                # Sliding Window: только последние N сообщений (N=0 — вся история).
                base = list(self.messages[-window:]) if window > 0 else list(self.messages)
            elif strategy == "facts":
                # Facts: блок facts (key-value) + последние N сообщений.
                recent = list(self.messages[-window:]) if window > 0 else list(self.messages)
                fact_msg = self._facts_message()
                base = ([fact_msg] if fact_msg else []) + recent
            elif strategy == "branch":
                # Ветки работают поверх активной истории (она уже = активная ветка).
                base = list(self.messages)
            else:
                base = list(self.messages)
            # Сжатие summary — поверх выбранной стратегией истории.
            return self._apply_summary_over(base)

    def _apply_summary_over(self, base):
        """Накладывает summary на произвольный список сообщений (если активно)."""
        if not self.compact["enabled"] or not self.compact["summary"]:
            return base
        keep = max(0, self.compact["keep"])
        if keep <= 0:
            return base
        recent = list(base[-keep:]) if keep > 0 else []
        return [{"role": "system", "content": self.compact["summary"]}] + recent

    def _facts_message(self):
        """Формирует системное сообщение с блоком facts (key-value)."""
        if not self.facts:
            return None
        lines = ["Известные факты о диалоге (key: value):"]
        for k, v in self.facts.items():
            lines.append("- %s: %s" % (k, v))
        return {"role": "system", "content": "\n".join(lines)}

    # ---- Facts (key-value память) ----

    def set_facts(self, facts):
        """Заменяет блок facts целиком (словарь) и сохраняет."""
        with self.lock:
            if isinstance(facts, dict):
                self.facts = {str(k): str(v) for k, v in facts.items()}
            self._save_locked()
            return dict(self.facts)

    def get_facts(self):
        """Копия текущего блока facts."""
        with self.lock:
            return dict(self.facts)

    # ---- Ветки диалога (Branching) ----

    def branches_state(self):
        """Состояние веток: список имён, активная ветка, размеры."""
        with self.lock:
            return {
                "branches": [{"name": b["name"], "size": len(b["messages"])}
                             for b in self.branches],
                "active_branch": self.active_branch,
            }

    def create_branch(self, name=None, count=None, from_checkpoint=True):
        """Создаёт одну или несколько веток от текущего состояния диалога.

        from_checkpoint=True — новые ветки копируют текущую историю (checkpoint).
        count — сколько веток создать (по умолчанию config.BRANCH_DEFAULT_COUNT).
        Возвращает состояние веток после операции.
        """
        with self.lock:
            # Переключаемся в режим Branch и синхронизируем активную ветку.
            self.branches[self.active_branch]["messages"] = list(self.messages)
            n = int(count) if count else int(config.BRANCH_DEFAULT_COUNT)
            if n < 1:
                n = 1
            snapshot = list(self.messages) if from_checkpoint else []
            base_n = len(self.branches)
            for i in range(n):
                bname = (name + ("-%d" % (i + 1) if n > 1 else "")) if name \
                    else ("ветка-%d" % (base_n + i))
                self.branches.append({"name": bname, "messages": list(snapshot)})
            self.strategy = "branch"
            # Активной делаем первую новую ветку (можно переключить).
            self.active_branch = len(self.branches) - n
            self.messages = list(self.branches[self.active_branch]["messages"])
            self._save_locked()
            return self.branches_state()

    def switch_branch(self, index):
        """Переключает активную ветку по индексу и сохраняет."""
        with self.lock:
            # Перед переключением сохраняем текущую историю в её ветку.
            if 0 <= self.active_branch < len(self.branches):
                self.branches[self.active_branch]["messages"] = list(self.messages)
            try:
                idx = int(index)
            except (TypeError, ValueError):
                idx = self.active_branch
            if 0 <= idx < len(self.branches):
                self.active_branch = idx
                self.messages = list(self.branches[idx]["messages"])
                self.strategy = "branch"
            self._save_locked()
            return self.branches_state()

    def delete_branch(self, index):
        """Удаляет ветку по индексу (нельзя удалить последнюю)."""
        with self.lock:
            try:
                idx = int(index)
            except (TypeError, ValueError):
                return self.branches_state()
            if len(self.branches) <= 1 or not (0 <= idx < len(self.branches)):
                return self.branches_state()
            del self.branches[idx]
            if self.active_branch >= len(self.branches):
                self.active_branch = len(self.branches) - 1
            self.messages = list(self.branches[self.active_branch]["messages"])
            self._save_locked()
            return self.branches_state()

    def rename_branch(self, index, name):
        """Переименовывает ветку по индексу и сохраняет на диск.

        Пустое имя или имя из пробелов игнорируется. Возвращает состояние
        веток после операции.
        """
        with self.lock:
            try:
                idx = int(index)
            except (TypeError, ValueError):
                return self.branches_state()
            new_name = str(name if name is not None else "").strip()
            # Обрезаем чрезмерно длинные имена, чтобы не ломать интерфейс.
            if len(new_name) > 80:
                new_name = new_name[:80]
            if not new_name or not (0 <= idx < len(self.branches)):
                return self.branches_state()
            self.branches[idx]["name"] = new_name
            self._save_locked()
            return self.branches_state()
