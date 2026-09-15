"""
Построение HTML-фрагмента ответа из текста модели.
Включает: экранирование HTML, inline-Markdown, парсинг Markdown-таблиц.
"""
import re

__all__ = ["escape_html", "apply_inline_markdown", "text_to_html_paragraphs",
           "render_memory_marks", "mark_memory_fragments",
           "count_memory_fragments", "render_with_memory_counts"]

# Маркеры, которыми помечаются фрагменты, взятые из памяти агента.
#   [[R]]…[[/R]] — из РАБОЧЕЙ памяти   (подсветка фисташковым);
#   [[L]]…[[/L]] — из ДОЛГОВРЕМЕННОЙ   (подсветка фуксией).
# Сначала эти маркеры ставит детерминированный подстановщик
# (mark_memory_fragments) — по фактическому содержимому памяти, независимо
# от того, послушалась ли модель. Если модель САМА расставила такие маркеры,
# они обрабатываются тем же путём.
_MEM_OPEN_R = "[[R]]"
_MEM_CLOSE_R = "[[/R]]"
_MEM_OPEN_L = "[[L]]"
_MEM_CLOSE_L = "[[/L]]"


def _memory_terms(memory):
    """Собирает список терминов памяти как (текст, метка-маркер).

    Возвращает список пар (термин, "R"|"L") по значениям И ключам рабочей
    ("R", фисташковый) и долговременной ("L", фуксия) памяти. Значения идут
    в приоритете, длинные термины — раньше (чтобы совпадали целиком).
    Короткие/пустые термины (короче 3 символов) пропускаются, чтобы не
    подсвечивать случайные односложные совпадения.
    """
    terms = []
    w = (memory or {}).get("working") or {}
    l = (memory or {}).get("longterm") or {}
    for value, mark in [(v, "R") for v in w.values()] + \
                       [(v, "L") for v in l.values()] + \
                       [(k, "R") for k in w.keys()] + \
                       [(k, "L") for k in l.keys()]:
        s = str(value or "").strip()
        # Не подсвечиваем слишком короткие термины и «чисто числовые»
        # значения (№, суммы) — иначе подсветка «расползается» по тексту.
        if len(s) < 4:
            continue
        if re.fullmatch(r"[\d\s.,₽¥$%+-]+", s):
            continue
        terms.append((s, mark))
    # Длинные — первыми, чтобы более специфичные совпадения не перекрывались
    # короткими; убираем дубли, сохраняя первый (наиболее приоритетный) маркер.
    terms.sort(key=lambda t: len(t[0]), reverse=True)
    seen, ordered = set(), []
    for term, mark in terms:
        low = term.lower()
        if low in seen:
            continue
        seen.add(low)
        ordered.append((term, mark))
    return ordered


def mark_memory_fragments(text, memory):
    """Помечает в тексте фрагменты, совпадающие с данными памяти.

    Детерминированно (без опоры на «послушность» модели) оборачивает
    вхождения значений/ключей рабочей памяти в [[R]]…[[/R]], а
    долговременной — в [[L]]…[[/L]]. Регистр совпадения сохраняется.

    Совпадение — ТОЛЬКО по границам слов/фраз: подсвечивается вхождение,
    являющееся отдельным словом (или целой фразой), а не частью другого
    слова. Так «день» НЕ подсветится внутри «ежедневно»/«деньги», а фраза
    «3 дня» — только как цельная последовательность. Это исключает ложные
    срабатывания на частичных совпадениях.
    """
    marked, _counts = mark_memory_fragments_ex(text, memory)
    return marked


def mark_memory_fragments_ex(text, memory):
    """Как mark_memory_fragments, но ещё возвращает СЧЁТЧИКИ использований.

    Возвращает кортеж (размеченный_текст, counts), где counts —
    {"working": сколько_вхождений_рабочей, "longterm": сколько_долговрем.}.
    """
    counts = {"working": 0, "longterm": 0}
    if not text or not memory:
        return text, counts
    for term, mark in _memory_terms(memory):
        # Границы слова по Unicode (\w понимает кириллицу): слева и справа
        # не должно быть буквенно-цифрового символа. Внутренние пробелы фраз
        # допускают ЛЮБОЕ количество пробелов, чтобы «3 дня»/«3  дня» совпали.
        inner = r"\s+".join(re.escape(part) for part in term.split())
        pattern = re.compile(r"(?<!\w)" + inner + r"(?!\w)", re.IGNORECASE)
        opener, closer = ("[[R]]", "[[/R]]") if mark == "R" else ("[[L]]", "[[/L]]")

        matched_here = [0]

        def _sub(m):
            matched_here[0] += 1
            return opener + m.group(0) + closer

        text = pattern.sub(_sub, text)
        key = "working" if mark == "R" else "longterm"
        counts[key] += matched_here[0]
    return text, counts


def count_memory_fragments(text, memory):
    """Считает, сколько раз в тексте встречаются данные РАБОЧЕЙ и ДОЛГОВРЕМ.

    Возвращает dict {"working": N, "longterm": M} — число подсвеченных
    (полных, по границам слов) вхождений. Совпадения не изменяют текст.
    """
    _marked, counts = mark_memory_fragments_ex(text, memory)
    return counts


def render_memory_marks(text):
    """Превращает маркеры памяти в цветные <span> (обе пары закрываются).

    Вызывается ПОСЛЕ экранирования и inline-markdown: маркеры [[R]]/[[L]]
    экранирование не меняет (это обычный текст), поэтому их можно безопасно
    заменить на HTML-теги уже на финальном шаге.
    """
    if not text:
        return text
    # Рабочая память — фисташковый.
    text = text.replace(_MEM_OPEN_R,
                        "<span class='mem-src mem-src-working'>")
    text = text.replace(_MEM_CLOSE_R, "</span>")
    # Долговременная память — фуксия.
    text = text.replace(_MEM_OPEN_L,
                        "<span class='mem-src mem-src-longterm'>")
    text = text.replace(_MEM_CLOSE_L, "</span>")
    return text


def escape_html(text):
    """Экранирует спецсимволы для безопасного отображения в HTML."""
    return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def apply_inline_markdown(text):
    """Преобразует inline-Markdown (жирный, курсив, код) в HTML-теги.

    Вызывается ПОСЛЕ escape_html — на безопасном тексте, у которого нет
    настоящих тегов, поэтому подстановка не ломает уже сгенерированный код.
    Порядок: сначала код (чтобы звёздочки/подчёркивания внутри него не
    размечались), затем жирный, затем курсив.
    """
    # inline-код
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    # жирный **text** / __text__
    text = re.sub(r"\*\*([^*]+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"__([^_]+?)__", r"<strong>\1</strong>", text)
        # курсив *text* / _text_
    text = re.sub(r"\*([^*\n]+?)\*", r"<em>\1</em>", text)
    text = re.sub(r"_([^_\n]+?)_", r"<em>\1</em>", text)
    return text


def parse_markdown_table_block(lines, start_idx):
    """Собирает строки Markdown-таблицы, начиная с start_idx, в HTML-таблицу.

    Возвращает кортеж (html_таблица, индекс_последней_обработанной_строки).
    """
    header_cells = [c.strip().replace("**", "") for c in lines[start_idx].strip("|").split("|")]
    i = start_idx
    rows = []

    # Пропускаем разделительную строку (например, |---|---|---|)
    j = start_idx + 1
    if j < len(lines) and re.match(r"^\s*\|?[\s:\-|]+\|?\s*$", lines[j]):
        j += 1

    for k in range(j, len(lines)):
        line = lines[k].strip()
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in line.strip("|").split("|")]
        rows.append(cells)

    table_html = ["<table>", "<thead><tr>"]
    for idx, h in enumerate(header_cells):
        if idx == 1:
            table_html.append(f"<th class='desc-col'>{h}</th>")
        else:
            table_html.append(f"<th>{h}</th>")
    table_html.append("</tr></thead><tbody>")
    for row in rows:
        table_html.append("<tr>")
        for idx, cell in enumerate(row):
            if idx == 0:
                table_html.append(f"<td class='date-col'>{cell}</td>")
            elif idx == 1:
                table_html.append(f"<td class='desc-col'>{cell}</td>")
            elif idx == 2:
                table_html.append(f"<td class='size-col'>{cell}</td>")
            else:
                table_html.append(f"<td>{cell}</td>")
        table_html.append("</tr>")
    table_html.append("</tbody></table>")

    return "\n".join(table_html), j - 1


def text_to_html_paragraphs(text, memory=None):
    """Разбивает текст на абзацы, списки и Markdown-таблицы.

    Дополнительно помечает фрагменты, совпадающие с данными ПАМЯТИ агента
    (значения/ключи рабочей — фисташковым, долговременной — фуксией) и
    подсвечивает их цветом. Разметка ставится детерминированно по memory
    (dict вида {"working": {...}, "longterm": {...}}), а также распознаются
    маркеры [[R]]/[[L]], если их расставила сама модель.
    """
    html, _counts = render_with_memory_counts(text, memory)
    return html


def render_with_memory_counts(text, memory=None):
    """Как text_to_html_paragraphs, но ещё возвращает СЧЁТЧИКИ использований.

    Возвращает кортеж (html, counts), где counts — {"working": N,
    "longterm": M}: сколько фрагментов ответа заимствовано из рабочей и
    долговременной памяти (по полным совпадениям с учётом границ слов и/или
    маркеров, расставленных моделью).
    """
    counts = {"working": 0, "longterm": 0}
    # 1) Детерминированная разметка фрагментов памяти — до экранирования,
    #    пока текст ещё «сырой» (маркеры [[…]] экранирование не затрагивает).
    #    Считаем ТОЛЬКО реально поставленные метки (возвращает функция).
    if memory:
        text, det = mark_memory_fragments_ex(text, memory)
        counts["working"] += det["working"]
        counts["longterm"] += det["longterm"]
    text = escape_html(text)
    text = apply_inline_markdown(text)
    # 2) Маркеры, расставленные САМОЙ моделью ([[R]]/[[L]]): учитываем те,
    #    что остались сверх уже подсчитанных детерминированных меток.
    counts["working"] = max(counts["working"], text.count(_MEM_OPEN_R))
    counts["longterm"] = max(counts["longterm"], text.count(_MEM_OPEN_L))
    # Подсветка фрагментов из памяти агента (после экранирования —
    # вставка своих <span> безопасна).
    text = render_memory_marks(text)
    lines = text.split("\n")
    html = []
    in_list = False
    i = 0

    while i < len(lines):
        line = lines[i].rstrip()

        # Если строка — начало Markdown-таблицы (содержит "|")
        if line.strip().startswith("|") and "|" in line[1:]:
            if in_list:
                html.append("</ul>")
                in_list = False
            table_html, i = parse_markdown_table_block(lines, i)
            html.append(table_html)
            i += 1
            continue

        if not line:
            if in_list:
                html.append("</ul>")
                in_list = False
            i += 1
            continue

        if line.startswith("### "):
            if in_list:
                html.append("</ul>")
                in_list = False
            html.append(f"<h4>{line[4:]}</h4>")
        elif line.startswith("## "):
            if in_list:
                html.append("</ul>")
                in_list = False
            html.append(f"<h3>{line[3:]}</h3>")
        elif line.startswith("# "):
            if in_list:
                html.append("</ul>")
                in_list = False
            html.append(f"<h2>{line[2:]}</h2>")
        elif line.strip().startswith("- "):
            if not in_list:
                html.append("<ul>")
                in_list = True
            html.append(f"<li>{line.strip()[2:]}</li>")
        elif re.match(r"^\d+\.\s", line.strip()):
            if not in_list:
                html.append("<ul>")
                in_list = True
            html.append(f"<li>{re.sub(r'^\d+\.\s', '', line.strip())}</li>")
        else:
            if in_list:
                html.append("</ul>")
                in_list = False
            html.append(f"<p>{line}</p>")

        i += 1

    if in_list:
        html.append("</ul>")

    return "\n".join(html), counts
