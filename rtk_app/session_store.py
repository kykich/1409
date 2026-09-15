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
  } ],
  "memory": {               // ПАМЯТЬ АГЕНТА — три типа, хранятся ОТДЕЛЬНО
      "working":  {"ключ": "значение", …},   // рабочая: текущая задача/шаги
      "longterm": {"ключ": "значение", …}    // долговременная: профиль/решения
  }
}

Память агента — ТРИ независимых типа (задание A), которые хранятся РАЗДЕЛЬНО
(задание B1) и заполняются ЯВНО, с выбором «что и куда» (задание B2):
  * "short"    — краткосрочная: срез текущего диалога (messages / активная
                 ветка). Заполняется автоматически каждым ходом; в JSON-поле
                 memory НЕ дублируется, т.к. короткая память = сам диалог;
  * "working"  — рабочая: key-value данные ТЕКУЩЕЙ ЗАДАЧИ (активная задача,
                 подцели, шаги, статус). Пишется явно через set_memory();
  * "longterm" — долговременная: key-value профиль пользователя, принятые
                 решения и знания. Пишется явно через set_memory().

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
        # ПАМЯТЬ АГЕНТА: два ЯВНО заполняемых key-value слоя — рабочая
        # (данные текущей задачи) и долговременная (профиль/решения/знания).
        # Краткосрочная память = self.messages (диалог), поэтому отдельно
        # НЕ хранится — это и есть разделение типов (задание B1).
        self.memory_working = {}
        self.memory_longterm = {}
        # СЧЁТЧИКИ ИСПОЛЬЗОВАНИЯ памяти: сколько фрагментов ответов моделей
        # было заимствовано из рабочей/долговременной памяти (сумма за сессию).
        self.memory_use = {"working": 0, "longterm": 0}
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
            self.memory_working = {}
            self.memory_longterm = {}
            self.memory_use = {"working": 0, "longterm": 0}
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
                # Память агента: рабочая и долговременная (key-value).
                # Читаем ЯВНО из отдельного блока memory — типы не смешиваются.
                memory = data.get("memory")
                if isinstance(memory, dict):
                    working = memory.get("working")
                    if isinstance(working, dict):
                        self.memory_working = {str(k): str(v)
                                               for k, v in working.items()}
                    longterm = memory.get("longterm")
                    if isinstance(longterm, dict):
                        self.memory_longterm = {str(k): str(v)
                                                for k, v in longterm.items()}
                # Счётчики использования памяти (за сессию).
                mused = data.get("memory_use")
                if isinstance(mused, dict):
                    try:
                        self.memory_use["working"] = max(
                            0, int(mused.get("working", 0) or 0))
                        self.memory_use["longterm"] = max(
                            0, int(mused.get("longterm", 0) or 0))
                    except (TypeError, ValueError):
                        self.memory_use = {"working": 0, "longterm": 0}
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
                "memory": self.memory_state(),
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
            # Память агента: два ЯВНО заполняемых слоя (типы хранятся отдельно).
            "memory": {
                "working": self.memory_working,
                "longterm": self.memory_longterm,
            },
            # Счётчики использования данных памяти (сумма за сессию).
            "memory_use": dict(self.memory_use),
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
        """Очищает сессию: начинает новый разговор с чистого листа.

        Краткосрочную память (диалог) и РАБОЧУЮ память текущей задачи чистим —
        они относятся к завершаемому разговору. ДОЛГОВРЕМЕННУЮ память
        (профиль/решения/знания) СОХРАНЯЕМ: она переносится между сессиями.
        """
        with self.lock:
            self.messages = []
            self.compact = self._default_compact()
            self.facts = {}
            self.branches = [{"name": "main", "messages": []}]
            self.active_branch = 0
            # Рабочая память завершённой задачи больше не нужна.
            self.memory_working = {}
            # Счётчики использования памяти — обнуляем для нового разговора.
            self.memory_use = {"working": 0, "longterm": 0}
            # Стратегию (strategy/window) и долговременную память НЕ сбрасываем.
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
                # Сколько элементов РАБОЧЕЙ и ДОЛГОВРЕМЕННОЙ памяти реально
                # уходит в запрос (используется в интерфейсе для подсветки:
                # Рабочая — фисташковый, Долговременная — фуксия).
                "working_count": len(self.memory_working),
                "longterm_count": len(self.memory_longterm),
                # СЧЁТЧИКИ ИСПОЛЬЗОВАНИЯ данных памяти (за сессию): сколько
                # фрагментов ответов моделей заимствовано из каждой памяти.
                "memory_used_working": int(self.memory_use.get("working", 0)),
                "memory_used_longterm": int(self.memory_use.get("longterm", 0)),
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
            # Память агента: рабочая + долговременная подмешиваются как
            # системное сообщение (краткосрочная = сам диалог `base`).
            mem_msg = self.memory_message()
            if mem_msg:
                base = [mem_msg] + base
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

    # ---- Память агента: краткосрочная / рабочая / долговременная ----
    #
    # Три типа памяти (задание A) хранятся РАЗДЕЛЬНО (задание B1):
    #   * "short"    — краткосрочная: сам текущий диалог (messages/ветки),
    #                  заполняется автоматически; отдельного поля не имеет;
    #   * "working"  — рабочая: key-value текущей задачи (memory_working);
    #   * "longterm" — долговременная: key-value профиль/решения/знания
    #                  (memory_longterm).
    # Запись в working/longterm — только ЯВНАЯ, с указанием типа (задание B2):
    # вызывающий код сам решает, в какой слой сохранить каждый ключ.

    def add_memory_usage(self, working=0, longterm=0):
        """Добавляет к счётчикам использования памяти число фрагментов.

        working/longterm — сколько фрагментов ответа заимствовано из
        соответствующей памяти в этом обмене. Возвращает обновлённые счётчики.
        """
        with self.lock:
            try:
                self.memory_use["working"] += max(0, int(working or 0))
                self.memory_use["longterm"] += max(0, int(longterm or 0))
            except (TypeError, ValueError):
                pass
            self._save_locked()
            return dict(self.memory_use)

    def memory_state(self):
        """Снимок всех трёх типов памяти (для интерфейса/отладки).

        Возвращает dict:
          short    — {items: N} краткая сводка краткосрочной памяти (диалога);
          working  — копия рабочей памяти (key-value);
          longterm — копия долговременной памяти (key-value).
        """
        with self.lock:
            return {
                "short": {
                    "kind": "диалог",
                    "items": len(self.messages),
                    "active_branch": self.active_branch,
                    "branches": len(self.branches),
                },
                "working": dict(self.memory_working),
                "longterm": dict(self.memory_longterm),
            }

    def get_memory(self, mem_type):
        """Возвращает копию памяти указанного типа.

        mem_type: "short" | "working" | "longterm".
        Для "short" возвращает сводку диалога (память ведётся сообщениями).
        Для "working"/"longterm" — копию соответствующего key-value словаря.
        """
        with self.lock:
            if mem_type == "short":
                return {
                    "kind": "диалог",
                    "items": len(self.messages),
                    "active_branch": self.active_branch,
                    "branches": len(self.branches),
                }
            if mem_type == "working":
                return dict(self.memory_working)
            if mem_type == "longterm":
                return dict(self.memory_longterm)
            return None

    @staticmethod
    def _clean_memory_dict(data):
        """Нормализует словарь памяти: строковые ключи/значения, лимиты.

        Возвращает (clean, dropped): clean — очищенный словарь в пределах
        config.MEMORY_MAX_KEYS, dropped — сколько ключей отброшено по лимиту.
        Значения обрезаются до config.MEMORY_VALUE_CAP символов.
        """
        clean = {}
        if not isinstance(data, dict):
            return clean, 0
        cap = int(config.MEMORY_VALUE_CAP)
        limit = int(config.MEMORY_MAX_KEYS)
        keys = list(data.keys())
        dropped = max(0, len(keys) - limit)
        for k in keys[:limit]:
            key = str(k).strip()
            if not key:
                continue
            val = data[k]
            val = "" if val is None else str(val)
            if len(val) > cap:
                val = val[:cap]
            clean[key] = val
        return clean, dropped

    def set_memory_key(self, mem_type, key, value):
        """Явно записывает пару key=value в память указанного типа.

        Возвращает dict записанного элемента либо возбуждает ValueError
        при неверном типе/пустом ключе.
        """
        mem_type = str(mem_type or "").strip()
        if mem_type not in config.MEMORY_TYPES:
            raise ValueError(
                "Неизвестный тип памяти: %r. Допустимо: %s"
                % (mem_type, ", ".join(config.MEMORY_TYPES)))
        key = str(key or "").strip()
        if not key:
            raise ValueError("Пустой ключ памяти.")
        val = "" if value is None else str(value)
        cap = int(config.MEMORY_VALUE_CAP)
        if len(val) > cap:
            val = val[:cap]
        with self.lock:
            if mem_type == "short":
                # Краткосрочную память вручную не пишем — это сам диалог.
                raise ValueError(
                    "Тип 'short' (текущий диалог) заполняется автоматически "
                    "и не редактируется через память. Используйте 'working' "
                    "для задач или 'longterm' для профиля/знаний.")
            if mem_type == "working":
                if key not in self.memory_working \
                        and len(self.memory_working) >= int(config.MEMORY_MAX_KEYS):
                    # вытесняем самый старый ключ (FIFO), чтобы не расти бесконечно
                    self.memory_working.pop(next(iter(self.memory_working)))
                self.memory_working[key] = val
            else:  # longterm
                if key not in self.memory_longterm \
                        and len(self.memory_longterm) >= int(config.MEMORY_MAX_KEYS):
                    self.memory_longterm.pop(next(iter(self.memory_longterm)))
                self.memory_longterm[key] = val
            self._save_locked()
        return {"type": mem_type, "key": key, "value": val}

    def delete_memory_key(self, mem_type, key):
        """Явно удаляет ключ из памяти указанного типа. True, если удалён."""
        mem_type = str(mem_type or "").strip()
        if mem_type not in config.MEMORY_TYPES:
            raise ValueError("Неизвестный тип памяти: %r" % (mem_type,))
        key = str(key or "").strip()
        if not key:
            return False
        with self.lock:
            if mem_type == "working":
                existed = self.memory_working.pop(key, None) is not None
            elif mem_type == "longterm":
                existed = self.memory_longterm.pop(key, None) is not None
            else:
                raise ValueError("Тип 'short' (диалог) не редактируется вручную.")
            if existed:
                self._save_locked()
            return existed

    def set_memory_bulk(self, mem_type, data):
        """Явно ЗАМЕНЯЕТ память указанного типа целиком словарём data.

        Используется интерфейсом для сохранения отредактированной панели
        «Рабочая/Долговременная память». Возвращает итоговый словарь.
        """
        mem_type = str(mem_type or "").strip()
        if mem_type not in config.MEMORY_TYPES:
            raise ValueError("Неизвестный тип памяти: %r" % (mem_type,))
        if mem_type == "short":
            raise ValueError("Тип 'short' (диалог) не редактируется вручную.")
        clean, _dropped = self._clean_memory_dict(data)
        with self.lock:
            if mem_type == "working":
                self.memory_working = clean
            else:
                self.memory_longterm = clean
            self._save_locked()
            return dict(clean)

    def memory_message(self):
        """Системное сообщение с памятью для запроса к LLM.

        В контекст добавляются ТОЛЬКО рабочая и долговременная память
        (краткосрочная и так присутствует как история диалога). Возвращает
        dict-сообщение или None, если обе памяти пусты.

        Агенту даётся инструкция помечать маркерами фрагменты ответа,
        опирающиеся на память, чтобы интерфейс подсветил их цветом:
          * [[R]]…[[/R]] — данные из РАБОЧЕЙ памяти (фисташковый);
          * [[L]]…[[/L]] — данные из ДОЛГОВРЕМЕННОЙ памяти (фуксия).
        """
        with self.lock:
            lines = []
            if self.memory_working:
                lines.append("Рабочая память (данные текущей задачи):")
                for k, v in self.memory_working.items():
                    lines.append("- %s: %s" % (k, v))
            if self.memory_longterm:
                if lines:
                    lines.append("")
                lines.append("Долговременная память (профиль, решения, знания):")
                for k, v in self.memory_longterm.items():
                    lines.append("- %s: %s" % (k, v))
            if not lines:
                return None
            lines.append("")
            lines.append("Правило разметки ответа: если фрагмент ответа "
                         "основан на данных из ПАМЯТИ, оберни именно этот "
                         "фрагмент маркерами:")
            lines.append("- из рабочей памяти — [[R]]…[[/R]];")
            lines.append("- из долговременной памяти — [[L]]…[[/L]].")
            lines.append("Маркеры ставь только вокруг заимствованных из памяти "
                         "слов/фраз; остальной текст — без маркеров.")
            return {"role": "system",
                    "content": "Память агента:\n" + "\n".join(lines)}

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
