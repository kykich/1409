"""
Построение HTML-фрагмента ответа из текста модели.
Включает: экранирование HTML, inline-Markdown, парсинг Markdown-таблиц.
"""
import re

__all__ = ["escape_html", "apply_inline_markdown", "text_to_html_paragraphs"]


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


def text_to_html_paragraphs(text):
    """Разбивает текст на абзацы, списки и Markdown-таблицы."""
    text = escape_html(text)
    text = apply_inline_markdown(text)
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

    return "\n".join(html)
