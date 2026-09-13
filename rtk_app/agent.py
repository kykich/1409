"""Агент чата — отдельная сущность, инкапсулирующая всю логику запросов к LLM.

Агент:
  * принимает запросы пользователя (вопрос + историю диалога);
  * позволяет выбрать, какие модели опросить, и задать температуру каждой;
  * сам строит итоговый набор сообщений (включая системный промпт);
  * обращается к выбранным большим языковым моделям через API;
  * собирает метрики (время, токены, стоимость);
  * возвращает готовый результат (текст, HTML-разметка, метаданные).

Логика обработки запроса спрятана внутри агента — внешний код
(HTTP-обработчик) лишь передаёт пользовательский ввод и получает ответ.
"""
import time
from datetime import datetime

from . import config, deepseek, gigachat, html_report

__all__ = ["Agent"]

# Источники "голосов": (провайдер, имя модели, метка для отображения, css-класс)
SOURCES = (
    ("deepseek", config.DS_MODELS[0], "DeepSeek-flash", "ds-flash"),
    ("gigachat", config.GC_MODEL, "GigaChat", "gc-base"),
)

# Системный промпт агента (роль). Ставится в начало каждого диалога.
SYSTEM_PROMPT = ("Ты — дружелюбный ассистент. Отвечай по-русски, понятно и "
                 "структурированно, следуя запросам пользователя.")


class Agent:
    """Единая точка общения пользователя с большими языковыми моделями."""

    def __init__(self, api_key, sources=None):
        """Создаёт агента.

        api_key — ключ DeepSeek; sources — последовательность кортежей
        (provider, model, label, css_class), если нужен свой набор моделей.
        """
        self.api_key = api_key
        self.sources = list(sources or SOURCES)
        self.enabled_models = [s[2] for s in self.sources]
        # Индекс доступных моделей по их метке (label) для быстрого выбора
        # по имени в запросе пользователя.
        self.by_label = {s[2]: s for s in self.sources}

    # ---- открытый интерфейс агента ----

    @property
    def label(self):
        """Краткое описание состава агента (для интерфейса)."""
        return " | ".join(self.enabled_models)

    def available(self):
        """Список моделей, которые умеет обслуживать агент (метаданные для UI)."""
        out = []
        for provider, model, label, cls in self.sources:
            out.append({
                "label": label,
                "cls": cls,
                "provider": provider,
                "model": model,
            })
        return out

    def _coerce_temperature(self, value):
        """Приводит значение температуры к float в допустимом диапазоне."""
        try:
            t = float(value)
        except (TypeError, ValueError):
            return None
        if t < config.TEMP_MIN:
            return float(config.TEMP_MIN)
        if t > config.TEMP_MAX:
            return float(config.TEMP_MAX)
        return t

    def _coerce_max_tokens(self, value):
        """Приводит значение max_tokens к int в допустимом диапазоне.

        None и нечисловые значения означают «не применять» (возвращаем None —
        модель сама выбирает лимит вывода). Выход за границы клампится.
        """
        try:
            v = int(value)
        except (TypeError, ValueError):
            return None
        if v < config.MAX_TOKENS_MIN:
            return int(config.MAX_TOKENS_MIN)
        if v > config.MAX_TOKENS_MAX:
            return int(config.MAX_TOKENS_MAX)
        return v

    def _selected_sources(self, selected):
        """Разрешает пользовательский выбор в кортежи источников.

        selected — список элементов вида {label, temperature}. Возвращает
        словарь {label: (source_tuple, temperature_or_None)} только для тех
        моделей, что реально доступны агенту. Если выборка пуста или не
        содержит подходящих меток — используются все источники (без явной
        температуры, т.е. системное значение модели).
        """
        chosen = {}
        if isinstance(selected, list) and selected:
            for opt in selected:
                if isinstance(opt, dict):
                    label = str(opt.get("label", ""))
                else:
                    label = str(opt)
                base = self.by_label.get(label)
                if base is None:
                    continue
                temp = self._coerce_temperature(
                    opt.get("temperature") if isinstance(opt, dict) else None)
                chosen[label] = (base, temp)
        if not chosen:
            for base in self.sources:
                chosen[base[2]] = (base, None)
        return chosen

    def answer(self, question, history=None, selected=None, max_tokens=None,
               compact=None):
        """Обрабатывает запрос пользователя и возвращает результат.

        Принимает:
            question — строка с вопросом пользователя;
            history  — список сообщений {role, content} (допустим пустой);
            selected — список dict {label, temperature}: модели, которые нужно
                       опросить, и температура каждой (необязательно; если
                       пусто — опрашиваются все доступные модели);
            max_tokens — int или None: глобальное ограничение числа новых
                        токенов ответа для каждой модели. None — не применять
                        (модель сама выбирает лимит вывода);
            compact — dict {enabled, keep, summary} или None — настройки
                      сжатия истории.
        Возвращает dict, единообразный для успеха и ошибок:
            ok      — True, если хотя бы одна модель ответила;
            text    — текстовое представление ответов;
            html    — HTML-разметка для интерфейса;
            answers — список {label, text, temperature} с ответами моделей;
            meta    — строка служебных данных.
        Внутренние исключения перехватываются и не выходят за пределы агента.
        """
        trace = []                      # «ход запросов» агента для правой панели
        question = str(question or "").strip()
        trace.append({"kind": "enter",
                      "title": "Агент принял запрос пользователя"})
        print("[TRACE] Agent.answer() ВХОД. question=%r" % question, flush=True)
        if not question:
            step = {"kind": "exit", "ok": False,
                    "title": "Агент: пустой вопрос",
                    "detail": "вернул {ok:False, error:'Пустой вопрос.'}"}
            trace.append(step)
            print("[TRACE] Agent.answer() пустой вопрос -> ранний выход", flush=True)
            return Ok(self).value(ok=False, error="Пустой вопрос.", trace=trace)

        # Сжатие истории.
        # Основной путь: сервер уже прислал сжатую историю
        # ([summary] + последние keep сообщений). Тогда повторно сжимать
        # НЕЛЬЗЯ — иначе summary будет отброшен. Здесь _apply_compact
        # используется лишь как fallback, если история пришла полной,
        # но сжатие включено (например, при вызове агента вне сервера).
        already_compacted = (
            isinstance(history, list) and history
            and isinstance(history[0], dict)
            and history[0].get("role") == "system"
        )
        if (not already_compacted and compact
                and compact.get("enabled") and compact.get("summary")):
            history = self._apply_compact(history, compact)
            trace.append({"kind": "act", "title": "Агент: применено сжатие истории",
                          "detail": "сохранено %d последних сообщений, остальное — summary"
                                    % compact.get("keep", config.COMPACT_KEEP)})
        elif already_compacted:
            trace.append({"kind": "act",
                          "title": "Агент: контекст уже сжат (summary + последние)",
                          "detail": "сжатие применено на сервере, повторно не выполняется"})

        messages = self._build_messages(history, question)
        hlen = len(history) if isinstance(history, list) else 0
        trace.append({"kind": "act", "title": "Агент собрал сообщения для API",
                      "detail": "%d сообщений (%d из истории + текущий) | системный "
                                "промпт добавлен" % (len(messages), hlen)})
        print("[TRACE] Agent.answer() собрал %d сообщений для API" % len(messages),
              flush=True)

        sources = self._selected_sources(selected)
        trace.append({"kind": "branch", "title": "Агент -> запрос к LLM",
                      "detail": "агент опрашивает %d модель(ей) из выбранных"
                                % len(sources)})
        print("[TRACE] Agent.answer() опрашивает %d источника:"
              % len(sources), flush=True)

        blocks, text_parts, collected = [], [], []
        ok_any = False
        input_total = 0
        output_total = 0
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        mt = self._coerce_max_tokens(max_tokens)
        mt_note = ("max_tokens=%d" % mt) if mt is not None else "max_tokens=auto"

        for label, (base, temperature) in sources.items():
            provider, model, _lbl, cls = base
            temp_note = ("temperature=%.2f" % temperature
                         if temperature is not None else "temperature=auto")
            node = {"kind": "llm", "model": label,
                    "title": "LLM: %s" % label,
                    "detail": "отправка… " + temp_note + " · " + mt_note}
            trace.append(node)
            print("[TRACE]   -> вызов модели label=%r %s %s"
                  % (label, temp_note, mt_note), flush=True)
            single = self._call_one(provider, model, messages, label, temperature,
                                    max_tokens=mt)
            node["ok"] = single["ok"]
            node["detail"] = ("запрос выполнен за %.2f c · ввод %d/вывод %d ток"
                              % (single["elapsed"], single["prompt_tokens"],
                                 single["completion_tokens"]))
            node["dur"] = round(single["elapsed"], 2)
            node["dur_ms"] = int(single["elapsed"] * 1000)
            print("[TRACE]   <- результат %r: ok=%s" % (label, single["ok"]),
                  flush=True)
            input_total += single["prompt_tokens"]
            output_total += single["completion_tokens"]
            if single["ok"]:
                ok_any = True
            blocks.append(self._render_block(provider, model, label, cls, single))
            text_parts.append(self._render_text(provider, model, label, single))
            collected.append({
                "label": label,
                "text": single["content"],
                "temperature": temperature,
                "input": single["prompt_tokens"],
                "output": single["completion_tokens"],
                "cost": self._estimate_cost(provider, model,
                                            single["prompt_tokens"],
                                            single["completion_tokens"]),
            })

        total_tokens = input_total + output_total
        # Оценка доли входных токенов, пришедшейся на контекст-историю
        # (система и текущий вопрос не считаются историей).
        history_tokens = self._history_token_estimate(messages, input_total)
        meta = self._build_meta(ts, total_tokens, ok_any)
        html = "".join(blocks)
        text = "\n\n".join(text_parts)
        trace.append({"kind": "exit", "ok": ok_any,
                      "title": "Агент возвращает ответ",
                      "detail": "собрано %d ответов · суммарно %d ток · ok=%s"
                                % (len(collected), total_tokens, ok_any)})
        print("[TRACE] Agent.answer() ГОТОВО. ok=%s, токенов=%d, история~%d"
              % (ok_any, total_tokens, history_tokens), flush=True)
        usage = {
            "input": input_total,
            "output": output_total,
            "total": total_tokens,
            "history": history_tokens,
        }
        return Ok(self).value(ok=ok_any, html=html, text=text,
                              answers=collected, meta=meta, trace=trace,
                              usage=usage)

    def answer_files(self, files, question="", selected=None, max_tokens=None):
        """Анализирует приложенные файлы (исходники/текст) через выбранные модели.

        files — список dict {name, path, content}: содержимое уже прочитано
        в браузере и передано как текст. Историю диалога не подмешиваем —
        анализ разовый. Возвращает результат того же формата, что и answer().
        """
        files = [f for f in (files or []) if isinstance(f, dict)]
        chunks = []
        total_chars = 0
        for f in files:
            content = f.get("content")
            content = content if isinstance(content, str) else str(content or "")
            name = f.get("path") or f.get("name") or "файл"
            total_chars += len(content)
            chunks.append("### Файл: %s\n```\n%s\n```" % (name, content))

        instr = str(question or "").strip()
        if not instr:
            instr = ("Проанализируй приложенные исходники/текст: объясни, "
                     "что делает код (или о чём текст), отметь структуру, "
                     "назначение ключевых частей и возможные проблемы.")
        prompt = ("Проанализируй приложенные файлы.\n\n"
                  "Задача: %s\n\n"
                  "Приложено файлов: %d (суммарно %d символов).\n\n%s"
                  % (instr, len(files), total_chars, "\n\n".join(chunks)))

        # Разовый анализ: без истории диалога.
        result = self.answer(prompt, history=[], selected=selected,
                             max_tokens=max_tokens)
        return result

    # ---- приватная логика запроса (инкапсулирована в агенте) ----

    @staticmethod
    def _apply_compact(history, compact):
        """Применяет сжатие к истории диалога.

        history — полный список сообщений.
        compact — dict {enabled, keep, summary}.
        Возвращает новый список: summary + последние keep сообщений.
        """
        if not history or not compact.get("summary"):
            return history
        keep = max(0, compact.get("keep", config.COMPACT_KEEP))
        # keep = 0 → сжатие не применяется: возвращаем полную историю.
        if keep <= 0:
            return history
        if keep >= len(history):
            return history
        recent = list(history[-keep:]) if keep > 0 else []
        summary_msg = {"role": "system", "content": compact["summary"]}
        return [summary_msg] + recent

    def _build_messages(self, history, question):
        """Собирает полный список сообщений для API (системный промпт + диалог)."""
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        if isinstance(history, list):
            for m in history:
                if (isinstance(m, dict)
                        and m.get("role") in ("user", "assistant")
                        and isinstance(m.get("content"), str)
                        and m["content"]):
                    messages.append({"role": m["role"], "content": m["content"]})
        messages.append({"role": "user", "content": question})
        return messages

    @staticmethod
    def _history_token_estimate(messages, input_tokens):
        """Оценка числа входных токенов, приходящихся на историю диалога.

        Точной разбивки по сообщениям API не даёт, поэтому распределяем
        фактическое число входных токенов пропорционально длине текста:
        история — это все сообщения, кроме системного промпта (первое) и
        текущего вопроса пользователя (последнее).
        """
        try:
            total_chars = sum(len(str(m.get("content", ""))) for m in messages)
            hist_chars = sum(len(str(m.get("content", "")))
                             for m in messages[1:-1])
        except Exception:
            return 0
        if total_chars <= 0 or input_tokens <= 0:
            return 0
        return int(round(input_tokens * hist_chars / total_chars))

    @staticmethod
    def _estimate_cost(provider, model, prompt_tokens, completion_tokens):
        """Оценка стоимости запроса в юанях.

        Возвращает число (¥) для моделей DeepSeek по известным ценам или
        None, если цена для модели неизвестна (например, GigaChat).
        """
        if provider != "deepseek":
            return None
        in_price = config.DS_PRICE_INPUT_PER_M.get(model)
        out_price = config.DS_PRICE_OUTPUT_PER_M.get(model)
        if in_price is None or out_price is None:
            return None
        return (prompt_tokens * in_price
                + completion_tokens * out_price) / 1_000_000.0

    def _call_one(self, provider, model, messages, label, temperature=None,
                  max_tokens=None):
        """Вызывает одну конкретную модель через API и фиксирует результат.

        Любые ошибки превращаются в "мягкий" результат с признаком ok=False,
        чтобы сбой одной модели не ломал весь агент.
        """
        t0 = time.perf_counter()
        try:
            if provider == "deepseek":
                res = deepseek.chat(self.api_key, messages,
                                    model=model, temperature=temperature,
                                    max_tokens=max_tokens)
            else:
                res = gigachat.chat(messages, model=model,
                                    temperature=temperature,
                                    max_tokens=max_tokens)
            return {
                "ok": True,
                "content": res.get("content", ""),
                "prompt_tokens": int(res.get("prompt_tokens", 0) or 0),
                "completion_tokens": int(res.get("completion_tokens", 0) or 0),
                "elapsed": time.perf_counter() - t0,
                "error": None,
            }
        except Exception as exc:
            return {
                "ok": False,
                "content": "[Ошибка модели %s: %s]" % (label, exc),
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "elapsed": time.perf_counter() - t0,
                "error": str(exc),
            }

    def _render_block(self, provider, model, label, cls, single):
        """HTML-блок (карточка ответа одной модели)."""
        content = single["content"]
        try:
            frag = html_report.text_to_html_paragraphs(content)
        except Exception:
            frag = html_report.escape_html(content)

        metrics = []
        metrics.append("время: %.2f c" % single["elapsed"])
        metrics.append("токены: ввод %d / вывод %d"
                       % (single["prompt_tokens"], single["completion_tokens"]))
        if provider == "deepseek":
            in_price = config.DS_PRICE_INPUT_PER_M.get(model, 0.0)
            out_price = config.DS_PRICE_OUTPUT_PER_M.get(model, 0.0)
            cost = (single["prompt_tokens"] * in_price
                    + single["completion_tokens"] * out_price) / 1_000_000.0
            metrics.append("стоимость: ≈ ¥%.6f" % cost)
        metrics_html = "".join("<span>%s</span>" % m for m in metrics)

        return (
            "<div class='variant variant-%s'>"
            "<div class='variant-head'>"
            "<span class='variant-name'>%s</span>"
            "<div class='variant-metrics'>%s</div>"
            "</div>"
            "<div class='variant-body'>%s</div></div>"
            % (cls, label, metrics_html, frag)
        )

    def _render_text(self, provider, model, label, single):
        """Текстовая строка-представление ответа одной модели."""
        cost = ""
        if provider == "deepseek":
            in_price = config.DS_PRICE_INPUT_PER_M.get(model, 0.0)
            out_price = config.DS_PRICE_OUTPUT_PER_M.get(model, 0.0)
            c = (single["prompt_tokens"] * in_price
                 + single["completion_tokens"] * out_price) / 1_000_000.0
            cost = ", ¥%.6f" % c
        return ("%s (%s): [%.2f c · %d/%d tok%s]\n%s"
                % (label, model, single["elapsed"],
                   single["prompt_tokens"], single["completion_tokens"],
                   cost, single["content"]))

    def _build_meta(self, ts, total_tokens, ok_any):
        """Служебная строка с информацией о генерации."""
        sources = ", ".join(self.enabled_models)
        if not ok_any:
            return ("Сгенерировано: %s · есть ошибки моделей · моделей: %s"
                    % (ts, sources))
        return ("Сгенерировано: %s · суммарно токенов: %d · моделей: %s"
                % (ts, total_tokens, sources))

    # ---- вспомогательная операция "анализ" (тоже через агента) ----

    def analyze(self, question, answers, cap=6000):
        """Просит модель проанализировать несколько ответов.

        Реализует внутреннюю логику построения аналитического запроса.
        """
        lines = [
            "Сравни эти ответы нескольких ИИ-моделей на один вопрос.",
            "Для каждого ответа: разбери сильные и слабые стороны, отметь",
            "совпадения и расхождения между ответами, сделай итоговый вывод",
            "о том, какой ответ наиболее полный и точный.",
            "В конце для КАЖДОГО ответа укажи оценку по шкале от 0 до 5",
            "(целая или с одним знаком после запятой) в строке вида:",
            '  ОЦЕНКА "<название модели>": X из 5',
        ]
        if question:
            lines += ["", "Вопрос: " + str(question)]
        lines += ["", "----- Ответы моделей -----"]
        for a in answers or []:
            label = a.get("label") or "Модель"
            text = self._cap(a.get("text", ""), cap)
            lines += ["", "### " + label, text]
        user_content = "\n".join(lines)
        messages = [{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_content}]
        try:
            t0 = time.perf_counter()
            res = gigachat.chat(messages, model=config.GC_MODEL)
            elapsed = time.perf_counter() - t0
            analysis_text = res.get("content", "") if isinstance(res, dict) else str(res)
            try:
                frag = html_report.text_to_html_paragraphs(analysis_text)
            except Exception:
                frag = "<p>" + str(analysis_text) + "</p>"
            meta = "Анализ ответов · GigaChat · время: %.2f c" % elapsed
            return Ok(self).value(ok=True,
                                  html="<div class='analyze-block'>%s</div>" % frag,
                                  text=analysis_text, meta=meta)
        except Exception as exc:
            return Ok(self).value(
                ok=False, html="", text="",
                error="Ошибка анализа через GigaChat: %s" % exc)

    @staticmethod
    def _cap(text, n):
        s = str(text or "")
        if len(s) <= n:
            return s
        return s[:n] + "\n[... текст обрезан для анализа ...]"

    def compact_history(self, messages, keep=None):
        """Генерирует summary для ВЫТЕСНЯЕМОЙ части истории диалога.

        messages — полный список сообщений диалога (без системного промпта).
        keep — сколько последних сообщений НЕ сжимать (остаются как есть).

        Возвращает строку summary (по части истории до последних keep
        сообщений) или None, если сжимать нечего / произошла ошибка.
        """
        keep = config.COMPACT_KEEP if keep is None else max(0, int(keep))
        # Сжимаем только то, что вытесняется из контекста: всё, кроме
        # последних keep сообщений. Если истории мало — сжимать нечего.
        tail = messages[-keep:] if keep > 0 else []
        head = messages[:-keep] if keep > 0 else list(messages)
        if len(head) < config.COMPACT_MIN:
            return None
        return self.compact_update("", head)

    def compact_update(self, prev_summary, new_messages):
        """Инкрементально ДОПИСЫВАЕТ вытесненные сообщения в summary (Вариант A).

        prev_summary — уже существующее summary (может быть пустым).
        new_messages — НОВЫЕ вытесненные сообщения (ещё не сжатые).

        Возвращает обновлённое summary (строку) или None при ошибке/пустоте.
        Новое summary = слияние прежнего текста и нового фрагмента.
        """
        new_messages = [m for m in (new_messages or []) if isinstance(m, dict)]
        if not new_messages:
            return None

        cap = config.COMPACT_MSG_CAP
        lines = [
            "Обнови краткое summary диалога, добавив в него новый фрагмент.",
            "Сохрани ключевые факты, темы, договорённости и решения,",
            "чтобы по summary можно было продолжить беседу.",
            "Не повторяйся, объедини прежнее и новое в единый связный текст.",
            "",
        ]
        if prev_summary:
            lines += ["Прежнее summary:", str(prev_summary), ""]
        lines.append("Новый фрагмент диалога (вытеснен из контекста):")
        for m in new_messages:
            role = m.get("role", "unknown")
            content = str(m.get("content", ""))
            if not content:
                continue
            if len(content) > cap:
                content = content[:cap] + " […]"
            if role == "user":
                lines.append("Пользователь: " + content)
            elif role == "assistant":
                lines.append("Ассистент: " + content)

        prompt = "\n\n".join(lines)
        messages_for_compact = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]

        try:
            t0 = time.perf_counter()
            res = gigachat.chat(
                messages_for_compact,
                model=config.GC_MODEL,
                temperature=0.3,
            )
            elapsed = time.perf_counter() - t0
            summary = res.get("content", "") if isinstance(res, dict) else str(res)
            if summary:
                print("[COMPACT] summary дополнен %.2f c, длина %d, +%d сообщ."
                      % (elapsed, len(summary), len(new_messages)), flush=True)
            return summary
        except Exception as exc:
            print("[COMPACT] ошибка: %s" % exc, flush=True)
            return None


class Ok:
    """Маленький помощник конструирования единообразного результата агента."""

    def __init__(self, agent):
        self.agent = agent

    def value(self, **kwargs):
        d = {"ok": False, "html": "", "text": "", "answers": [],
             "meta": "", "model": self.agent.label,
             "usage": {"input": 0, "output": 0, "total": 0, "history": 0}}
        d.update(kwargs)
        d.setdefault("error", None)
        return d

    def error(self, message):
        return self.value(ok=False, error=message)
