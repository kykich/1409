/* Логика чата:
   слева — ввод + история, справа — область ответа и «ход запросов».

   Особенности версии:
   - История диалога хранится на сервере в папке session/ (JSON) и
     подхватывается при открытии страницы (непрерывность беседы).
   - Вверху выбраны модели (кнопки, несколько) и температура для каждой.
   - Запрос уходит на сервер вместе с выбором моделей и температур.
*/
(function () {
    "use strict";

    var qEl = document.getElementById("question");
    var submit = document.getElementById("submit");
    var statusEl = document.getElementById("status");
    var streamEl = document.getElementById("stream");
    var reqList = document.getElementById("reqlist");
    var modelEl = document.getElementById("model");
    var traceEl = document.getElementById("trace");
    var modelBox = document.getElementById("model-buttons");
    var mtEnable = document.getElementById("max-tokens-enable");
    var mtInput = document.getElementById("max-tokens");

    // Автоматическое сжатие истории (без кнопки — включается само).
    var compactKeep = document.getElementById("compact-keep");
    // Стратегия управления контекстом (Sliding / Facts / Branch).
    var strategyGroup = document.getElementById("strategy-group");
    var strategyWindow = document.getElementById("strategy-window");
    var factsBox = document.getElementById("facts-box");
    var branchesBox = document.getElementById("branches-box");
    var tsTotal = document.getElementById("ts-total");
    var tsIn = document.getElementById("ts-in");
    var tsOut = document.getElementById("ts-out");
    var tsHist = document.getElementById("ts-hist");
    var tsHistPct = document.getElementById("ts-hist-pct");
    var tsModelsTitle = document.getElementById("ts-models-title");
    var tsModels = document.getElementById("ts-models");
    var tsCompacted = document.getElementById("ts-compacted");
    var tsFromSummary = document.getElementById("ts-from-summary");
    var segIn = document.getElementById("seg-in");
    var segOut = document.getElementById("seg-out");
    var filesInput = document.getElementById("files-input");
    var dirInput = document.getElementById("dir-input");
    var filesList = document.getElementById("files-list");
    var filesClear = document.getElementById("files-clear");
    var filesAsk = document.getElementById("files-ask");
    var autotestStart = document.getElementById("autotest-start");
    var autotestStop = document.getElementById("autotest-stop");
    var autotestCount = document.getElementById("autotest-count");
    var autotestDelay = document.getElementById("autotest-delay");

    var busy = false;

    // Текущий диалог в окне: [{role:'user'|'assistant', content, html, answers}]
    var items = [];

    // Состояние выбранных моделей: [{label, cls, on:bool, temp:number|null}]
    var modelState = [];
    // Значение температуры по умолчанию (минимальные/шаг берём из сервера).
    var DEFAULT_TEMP = 0.7;
    var TEMP_MIN = 0.0, TEMP_MAX = 1.0, TEMP_STEP = 0.05;

    // Глобальная настройка max_tokens (вкл/выкл + значение).
    var MTOK_DEFAULT = 2048;
    var MTOK_MIN = 1, MTOK_MAX = 100000, MTOK_STEP = 50;

    // Состояние НАСТРОЙКИ сжатия: сколько последних сообщений хранить
    // полностью. Само сжатие (генерация summary) происходит автоматически
    // на сервере после накопления порога несжатых сообщений.
    var compactState = {
        keep: 10,
    };

    // Данные для кнопки «Анализ».
    var lastAnswers = null;
    var lastQuestion = "";
    var analysisItem = null;

    // ------------------------------------------------------------------
    // Хелперы
    // ------------------------------------------------------------------
    function esc(s) {
        return String(s == null ? "" : s)
            .replace(/&/g, "&amp;").replace(/</g, "&lt;")
            .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    }

    function setStatus(message, kind) {
        statusEl.hidden = !message;
        statusEl.textContent = message || "";
        statusEl.className = "side-status" + (kind === "error" ? " error" : (kind === "ok" ? " ok" : ""));
    }

    function scrollToBottom() { streamEl.scrollTop = streamEl.scrollHeight; }

    function shortTitle(t) {
        var s = String(t || "").replace(/\s+/g, " ").trim();
        return s.length > 40 ? s.slice(0, 40) + "\u2026" : s;
    }

    var TRACE_KIND = {
        enter: "агент", act: "работа", branch: "запрос",
        llm: "LLM", exit: "агент"
    };

    function renderTrace(trace, meta) {
        if (!traceEl) return;
        if (!trace || !trace.length) {
            traceEl.innerHTML = '<div class="trace-empty">Задайте вопрос — здесь появится<br>' +
                'обработка агентом:<br><code>Агент → LLM → … → ответ</code></div>';
            return;
        }
        var ul = document.createElement("ul");
        ul.className = "ts-list";
        trace.forEach(function (s, i) {
            var li = document.createElement("li");
            var cls = "tstep " + (s.kind || "act");
            if (s.kind === "llm" && s.ok === false) cls += " fail";
            li.className = cls;

            var badges = "";
            var kindLbl = TRACE_KIND[s.kind] || s.kind;
            badges += '<span class="badge kind">' + esc(kindLbl) + "</span>";
            if (s.ok === true) badges += '<span class="badge gok">OK</span>';
            else if (s.ok === false) badges += '<span class="badge gfail">FAIL</span>';
            if (s.dur_ms != null) {
                var d = s.dur_ms < 1000 ? Math.round(s.dur_ms) + " мс"
                                        : parseFloat((s.dur).toFixed(2)) + " c";
                badges += '<span class="badge dur">' + d + "</span>";
            }
            li.innerHTML = '<div class="t-head"><span class="t-badges">' + badges + "</span>" +
                esc(s.title || "") + "</div>";
            if (s.detail) {
                var det = document.createElement("div");
                det.className = "t-detail";
                det.textContent = s.detail;
                li.appendChild(det);
            }
            ul.appendChild(li);
        });
        if (meta) {
            var m = document.createElement("div");
            m.className = "t-detail";
            m.textContent = "Итог: " + meta;
            m.style.cssText = "margin-top:8px;border-top:1px dashed #cdd4e6;padding-top:8px;color:#3a4a6e;";
            ul.appendChild(m);
        }
        traceEl.innerHTML = "";
        traceEl.appendChild(ul);
    }

    // Полная перерисовка истории (левого списка и правого окна) из items.
    function render() {
        streamEl.innerHTML = "";
        reqList.innerHTML = "";

        items.forEach(function (item, idx) {
            if (item.role === "assistant") {
                var msg = document.createElement("div");
                msg.className = "msg a";
                var b = document.createElement("div");
                b.className = "bubble";
                b.innerHTML = item.html || esc(item.content);
                msg.appendChild(b);
                streamEl.appendChild(msg);
            } else {
                var qMsg = document.createElement("div");
                qMsg.className = "msg q";
                qMsg.setAttribute("data-idx", idx);
                var qb = document.createElement("div");
                qb.className = "bubble";
                qb.textContent = item.content;
                qMsg.appendChild(qb);
                streamEl.appendChild(qMsg);

                var li = document.createElement("li");
                li.textContent = shortTitle(item.content);
                li.title = item.content;
                li.setAttribute("data-idx", idx);
                li.addEventListener("click", (function (i) {
                    return function () { scrollToQuestion(i); };
                })(idx));
                reqList.appendChild(li);
            }
        });

        if (analysisItem && analysisItem.html) {
            var aMsg = document.createElement("div");
            aMsg.className = "msg a";
            var aB = document.createElement("div");
            aB.className = "bubble";
            aB.innerHTML = analysisItem.html;
            aMsg.appendChild(aB);
            streamEl.appendChild(aMsg);
        }
        scrollToBottom();
        // История — отдельная прокручиваемая область: показываем последние записи.
        if (reqList) reqList.scrollTop = reqList.scrollHeight;
    }

    function scrollToQuestion(idx) {
        clearActive();
        var q = streamEl.querySelector('.msg.q[data-idx="' + idx + '"]');
        if (q) {
            q.classList.add("scroll-target");
            q.scrollIntoView({ behavior: "smooth", block: "start" });
        }
        var lis = reqList.querySelectorAll("li");
        for (var i = 0; i < lis.length; i++) {
            if (lis[i].getAttribute("data-idx") === String(idx)) lis[i].classList.add("active");
        }
    }

    function clearActive() {
        var sel = streamEl.querySelectorAll(".scroll-target");
        for (var i = 0; i < sel.length; i++) sel[i].classList.remove("scroll-target");
        var act = reqList.querySelectorAll(".active");
        for (var j = 0; j < act.length; j++) act[j].classList.remove("active");
    }

    // ------------------------------------------------------------------
    // Панель выбора моделей (кнопки-переключатели + температура)
    // ------------------------------------------------------------------
    function buildModelControls(available) {
        if (!modelBox) return;
        modelBox.innerHTML = "";
        modelState = available.map(function (m) {
            return { label: m.label, cls: m.cls || "", on: true, temp: DEFAULT_TEMP };
        });
        modelState.forEach(function (st) {
            var wrap = document.createElement("div");
            wrap.className = "model-ctrl active";
            wrap.dataset.label = st.label;

            // кнопка с названием модели
            var btn = document.createElement("button");
            btn.type = "button";
            btn.className = "m-btn";
            btn.innerHTML = '<span class="m-dot"></span>' + esc(st.label);
            btn.addEventListener("click", function () {
                st.on = !st.on;
                syncModelUI(st.label);
                updateModelTag();
            });
            wrap.appendChild(btn);

            // параметр температура
            var tempWrap = document.createElement("div");
            tempWrap.className = "model-temp";
            var lab = document.createElement("label");
            lab.textContent = "temp";
            var inp = document.createElement("input");
            inp.type = "number";
            inp.min = TEMP_MIN;
            inp.max = TEMP_MAX;
            inp.step = TEMP_STEP;
            inp.value = st.temp;
            inp.title = "Температура генерации (0 – строго, выше – разнообразнее)";
            inp.addEventListener("change", function () {
                var v = parseFloat(inp.value);
                if (isNaN(v)) v = DEFAULT_TEMP;
                if (v < TEMP_MIN) v = TEMP_MIN;
                if (v > TEMP_MAX) v = TEMP_MAX;
                inp.value = v;
                st.temp = v;
            });
            tempWrap.appendChild(lab);
            tempWrap.appendChild(inp);
            wrap.appendChild(tempWrap);

            modelBox.appendChild(wrap);
        });
        updateModelTag();
    }

    // Изменяет внешний вид карточки модели по текущему состоянию вкл/выкл.
    function syncModelUI(label) {
        var st = modelState.find(function (m) { return m.label === label; });
        if (!modelBox || !st) return;
        var cards = modelBox.querySelectorAll('.model-ctrl');
        for (var i = 0; i < cards.length; i++) {
            if (cards[i].dataset.label === label) {
                cards[i].classList.toggle("active", st.on);
                cards[i].classList.toggle("inactive", !st.on);
            }
        }
    }

    function updateModelTag() {
        if (!modelEl) return;
        var on = modelState.filter(function (m) { return m.on; }).map(function (m) { return m.label; });
        modelEl.textContent = on.length ? on.join(" · ") : "Все модели (по умолчанию)";
        renderModelsTitle(on);
    }

    // Заголовок блока статистики («Токены по моделям») приводим в
    // соответствие с реально используемыми моделями.
    function renderModelsTitle(labels) {
        if (!tsModelsTitle) return;
        var on = labels || modelState
            .filter(function (m) { return m.on; })
            .map(function (m) { return m.label; });
        if (!on.length) {
            tsModelsTitle.textContent = "Токены по моделям";
            return;
        }
        tsModelsTitle.innerHTML = 'Токены по моделям <span class="tstat-head-models">' +
            esc(on.join(" · ")) + "</span>";
    }

    // Собирает список выбранных моделей для отправки на сервер.
    function selectedModelsPayload() {
        return modelState
            .filter(function (m) { return m.on; })
            .map(function (m) { return { label: m.label, temperature: m.temp }; });
    }

    // ------------------------------------------------------------------
    // Настройка max_tokens (вкл/выкл + значение)
    // ------------------------------------------------------------------
    function mtEnabled() { return mtEnable ? mtEnable.checked : false; }

    // Возвращает число токенов из поля, если включено и корректно,
    // иначе null (настройку не применяем).
    function mtValue() {
        if (!mtEnabled()) return null;
        var v = parseInt(mtInput.value, 10);
        if (isNaN(v)) return null;
        if (v < MTOK_MIN) v = MTOK_MIN;
        if (v > MTOK_MAX) v = MTOK_MAX;
        return v;
    }

    function refreshMtUI() {
        var on = mtEnabled();
        if (mtInput) {
            mtInput.disabled = !on;
            if (on && !mtInput.value) mtInput.value = MTOK_DEFAULT;
        }
    }

    if (mtEnable) mtEnable.addEventListener("change", refreshMtUI);
    if (mtInput) mtInput.addEventListener("change", refreshMtUI);

    // ------------------------------------------------------------------
    // Управление контекстом (автоматическое сжатие истории)
    // ------------------------------------------------------------------
    // Настройка: сколько последних сообщений сохранять полностью.
    // Ранняя история автоматически заменяется на summary на сервере —
    // отдельной кнопки «Сжать» нет.

    function getCompactPayload() {
        // Сжатие всегда включено (автоматическое). От клиента передаём
        // только настройку keep — сколько последних сообщений оставлять.
        // keep = 0 → сжатие НЕ применяется (ни подстановка summary, ни автосжатие).
        var keep = parseInt(compactKeep ? compactKeep.value : compactState.keep, 10);
        if (isNaN(keep) || keep < 0) keep = compactState.keep;
        if (keep < 0) keep = 0;
        return { enabled: true, keep: keep };
    }

    function applyCompactFromServer(compact) {
        if (!compact) return;
        var keep = parseInt(compact.keep, 10);
        if (isNaN(keep) || keep < 0) keep = compactState.keep;
        compactState.keep = keep;
        if (compactKeep) compactKeep.value = keep;
    }

    if (compactKeep) {
        compactState.keep = parseInt(compactKeep.value, 10);
        if (isNaN(compactState.keep)) compactState.keep = 10;
        compactKeep.addEventListener("change", function () {
            var v = parseInt(compactKeep.value, 10);
            if (isNaN(v) || v < 0) v = 0;     // 0 = сжатие выключено
            if (v > 100) v = 100;
            compactKeep.value = v;
            compactState.keep = v;
            saveCompactSettings();
        });
    }

    function saveCompactSettings() {
        // Сохраняем настройку keep на сервер (сжатие включено всегда).
        fetch("/api/compact", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(getCompactPayload()),
        }).catch(function () {});
    }

    // ------------------------------------------------------------------
    // Стратегия управления контекстом: Sliding / Facts / Branch
    // ------------------------------------------------------------------
    var strategyState = { strategy: "none", window: 10 };

    function getStrategyPayload() {
        var w = parseInt(strategyWindow ? strategyWindow.value : strategyState.window, 10);
        if (isNaN(w) || w < 0) w = strategyState.window;
        return { strategy: strategyState.strategy, window: w };
    }

    function saveStrategySettings() {
        fetch("/api/strategy", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(getStrategyPayload()),
        })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
            if (!d || !d.ok) return;
            strategyState.strategy = d.strategy.strategy;
            strategyState.window = d.strategy.window;
            syncStrategyUI();
            renderFacts(d.facts);
            renderBranches(d.branches);
            applyContextStats(d.context);
        })
        .catch(function () {});
    }

    function syncStrategyUI() {
        if (!strategyGroup) return;
        var btns = strategyGroup.querySelectorAll(".strat-btn");
        for (var i = 0; i < btns.length; i++) {
            btns[i].classList.toggle("active",
                btns[i].dataset.strategy === strategyState.strategy);
        }
        if (strategyWindow) strategyWindow.value = strategyState.window;
        // Панели facts/веток показываем только в соответствующих стратегиях.
        var factsTitle = document.getElementById("facts-title");
        var brTitle = document.getElementById("branches-title");
        if (factsTitle) factsTitle.style.display =
            strategyState.strategy === "facts" ? "" : "none";
        if (factsBox) factsBox.style.display =
            strategyState.strategy === "facts" ? "" : "none";
        if (brTitle) brTitle.style.display =
            strategyState.strategy === "branch" ? "" : "none";
        if (branchesBox) branchesBox.style.display =
            strategyState.strategy === "branch" ? "" : "none";
    }

    function applyStrategyFromServer(strategy) {
        if (!strategy) return;
        if (strategy.strategy) strategyState.strategy = strategy.strategy;
        var w = parseInt(strategy.window, 10);
        if (!isNaN(w)) strategyState.window = w;
        syncStrategyUI();
    }

    if (strategyGroup) {
        strategyGroup.addEventListener("click", function (e) {
            var btn = e.target.closest(".strat-btn");
            if (!btn) return;
            strategyState.strategy = btn.dataset.strategy;
            syncStrategyUI();
            saveStrategySettings();
        });
    }
    if (strategyWindow) {
        strategyWindow.addEventListener("change", function () {
            var v = parseInt(strategyWindow.value, 10);
            if (isNaN(v) || v < 0) v = 0;
            if (v > 200) v = 200;
            strategyWindow.value = v;
            strategyState.window = v;
            saveStrategySettings();
        });
    }

    // ---- Факты (key-value), стратегия Facts ----
    function renderFacts(facts) {
        if (!factsBox) return;
        factsBox.innerHTML = "";
        var keys = facts ? Object.keys(facts) : [];
        if (!keys.length) {
            var empty = document.createElement("div");
            empty.className = "facts-empty";
            empty.textContent = "фактов пока нет (обновляются после каждого запроса)";
            factsBox.appendChild(empty);
        } else {
            keys.forEach(function (k) {
                factsBox.appendChild(factRow(k, facts[k]));
            });
        }
        var actions = document.createElement("div");
        actions.className = "facts-actions";
        var add = document.createElement("button");
        add.type = "button";
        add.textContent = "+ факт";
        add.addEventListener("click", function () {
            factsBox.insertBefore(factRow("", ""), actions);
        });
        var save = document.createElement("button");
        save.type = "button";
        save.className = "primary";
        save.textContent = "Сохранить";
        save.addEventListener("click", saveFacts);
        actions.appendChild(add);
        actions.appendChild(save);
        factsBox.appendChild(actions);
    }

    function factRow(key, val) {
        var row = document.createElement("div");
        row.className = "fact-row";
        var k = document.createElement("input");
        k.type = "text"; k.className = "fact-key"; k.value = key || "";
        k.placeholder = "ключ";
        var v = document.createElement("input");
        v.type = "text"; v.className = "fact-val"; v.value = val || "";
        v.placeholder = "значение";
        var del = document.createElement("button");
        del.type = "button"; del.className = "fact-del"; del.textContent = "\u00d7";
        del.title = "Удалить факт";
        del.addEventListener("click", function () { row.remove(); });
        row.appendChild(k); row.appendChild(v); row.appendChild(del);
        return row;
    }

    function saveFacts() {
        if (!factsBox) return;
        var out = {};
        var rows = factsBox.querySelectorAll(".fact-row");
        for (var i = 0; i < rows.length; i++) {
            var k = rows[i].querySelector(".fact-key").value.trim();
            var v = rows[i].querySelector(".fact-val").value.trim();
            if (k) out[k] = v;
        }
        fetch("/api/facts", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ facts: out }),
        })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) { if (d && d.ok) renderFacts(d.facts); })
        .catch(function () {});
    }

    // ---- Ветки диалога, стратегия Branch ----
    function renderBranches(state) {
        if (!branchesBox) return;
        branchesBox.innerHTML = "";
        if (!state || !state.branches) return;
        var active = state.active_branch || 0;
        state.branches.forEach(function (b, i) {
            var row = document.createElement("div");
            row.className = "branch-row" + (i === active ? " active" : "");
            row.title = "Клик — переключиться; двойной клик по имени — переименовать";

            var name = document.createElement("span");
            name.className = "b-name";
            name.textContent = b.name;
            name.title = "Двойной клик — переименовать ветку";
            // Двойной клик по имени — редактирование «на месте».
            name.addEventListener("dblclick", function (e) {
                e.stopPropagation();
                startRenameBranch(row, name, i, b.name);
            });
            row.appendChild(name);

            var size = document.createElement("span");
            size.className = "b-size";
            size.textContent = b.size + " сообщ.";
            row.appendChild(size);

            // Кнопка переименования (карандаш).
            var ren = document.createElement("button");
            ren.type = "button"; ren.className = "b-ren"; ren.textContent = "\u270e";
            ren.title = "Переименовать ветку";
            ren.addEventListener("click", function (e) {
                e.stopPropagation();
                startRenameBranch(row, name, i, b.name);
            });
            row.appendChild(ren);

            if (state.branches.length > 1) {
                var del = document.createElement("button");
                del.type = "button"; del.className = "b-del"; del.textContent = "\u00d7";
                del.title = "Удалить ветку";
                del.addEventListener("click", function (e) {
                    e.stopPropagation();
                    branchAction({ action: "delete", index: i });
                });
                row.appendChild(del);
            }
            row.addEventListener("click", function () {
                branchAction({ action: "switch", index: i });
            });
            branchesBox.appendChild(row);
        });

        var actions = document.createElement("div");
        actions.className = "branches-actions";
        var countInp = document.createElement("input");
        countInp.type = "number"; countInp.min = "1"; countInp.max = "20";
        countInp.value = "2"; countInp.title = "Сколько веток создать";
        var create = document.createElement("button");
        create.type = "button"; create.className = "primary";
        create.textContent = "Создать ветки от текущего";
        create.addEventListener("click", function () {
            branchAction({ action: "create", count: parseInt(countInp.value, 10) || 2 });
        });
        actions.appendChild(countInp);
        actions.appendChild(create);
        branchesBox.appendChild(actions);
    }

    // Запускает inline-редактирование имени ветки прямо в строке.
    function startRenameBranch(row, nameEl, index, currentName) {
        if (row.querySelector(".b-name-edit")) return;   // уже редактируется
        nameEl.style.display = "none";

        var inp = document.createElement("input");
        inp.type = "text";
        inp.className = "b-name-edit";
        inp.value = currentName || "";
        inp.maxLength = 80;
        row.insertBefore(inp, nameEl);
        inp.focus();
        inp.select();

        var finished = false;
        function commit() {
            if (finished) return;
            finished = true;
            var newName = inp.value.trim();
            inp.remove();
            nameEl.style.display = "";
            if (newName && newName !== currentName) {
                branchAction({ action: "rename", index: index, name: newName });
            } else {
                nameEl.textContent = currentName;   // без изменений
            }
        }
        function cancel() {
            if (finished) return;
            finished = true;
            inp.remove();
            nameEl.style.display = "";
            nameEl.textContent = currentName;
        }

        inp.addEventListener("click", function (e) { e.stopPropagation(); });
        inp.addEventListener("dblclick", function (e) { e.stopPropagation(); });
        inp.addEventListener("keydown", function (e) {
            if (e.key === "Enter") { e.preventDefault(); commit(); }
            else if (e.key === "Escape") { e.preventDefault(); cancel(); }
        });
        inp.addEventListener("blur", commit);
    }

    function branchAction(payload) {
        fetch("/api/branches", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
            if (!d || !d.ok) return;
            renderBranches(d.branches);
            // Переименование историю не меняет. Create/switch/delete могут
            // сменить активную ветку — тогда обновляем окно из серверных данных.
            if (payload.action !== "rename" && Array.isArray(d.messages)) {
                items = d.messages.slice();
                render();
            }
        })
        .catch(function () {});
    }

    // ------------------------------------------------------------------
    // Статистика токенов в заголовке (текущая сессия)
    // ------------------------------------------------------------------
    // Суммарный расход токенов за сессию: tokIn — вход (запрос), tokOut — выход (ответ)
    var tokIn = 0;
    var tokOut = 0;
    // Оценка входных токенов, ушедших на контекст-историю диалога
    var tokHist = 0;

    // Форматирует число с разделителями тысяч.
    function fmt(n) {
        return (n || 0).toString().replace(/\B(?=(\d{3})+(?!\d))/g, " ");
    }

    function resetTokenStats() {
        tokIn = 0;
        tokOut = 0;
        tokHist = 0;
        renderTokenStats();
    }

    // Учитывает трафик очередного обмена (input/output токенов за запрос).
    function addTokenUsage(u) {
        if (!u) return;
        var i = Math.max(0, parseInt(u.input, 10) || 0);
        var o = Math.max(0, parseInt(u.output, 10) || 0);
        var h = Math.max(0, parseInt(u.history, 10) || 0);
        tokIn += i;
        tokOut += o;
        tokHist += h;
        renderTokenStats();
    }

    // Обновляет цифры и полоску-диаграмму «запрос/ответ» + долю истории.
    function renderTokenStats() {
        var total = tokIn + tokOut;
        if (tsTotal) tsTotal.textContent = fmt(total);
        if (tsIn) tsIn.textContent = fmt(tokIn);
        if (tsOut) tsOut.textContent = fmt(tokOut);
        if (tsHist) tsHist.textContent = fmt(tokHist);
        if (tsHistPct) {
            var pct = total > 0 ? (tokHist / total * 100) : 0;
            tsHistPct.textContent = "(" + pct.toFixed(1) + "%)";
        }
        var empty = document.getElementById("ts-empty");
        if (empty) empty.style.display = total ? "none" : "";
        if (segIn && segOut) {
            if (total) {
                segIn.style.width = (tokIn / total * 100).toFixed(2) + "%";
                segOut.style.width = (tokOut / total * 100).toFixed(2) + "%";
            } else {
                segIn.style.width = "50%";
                segOut.style.width = "50%";
            }
        }
    }

    // Счётчики управления контекстом: сколько сообщений сжато (summary)
    // и сколько сообщений в запросе заменено summary.
    function applyContextStats(ctx) {
        if (!ctx) return;
        if (tsCompacted) tsCompacted.textContent = fmt(parseInt(ctx.compacted, 10) || 0);
        if (tsFromSummary) tsFromSummary.textContent = fmt(parseInt(ctx.from_summary, 10) || 0);
    }

    function resetContextStats() {
        if (tsCompacted) tsCompacted.textContent = "0";
        if (tsFromSummary) tsFromSummary.textContent = "0";
    }

    // ------------------------------------------------------------------
    // Накопительная статистика по каждой модели (в столбик в заголовке)
    // ------------------------------------------------------------------
    // modelStats: { label: {input, output, cost, hasCost} }
    var modelStats = {};
    // Порядок отображения моделей (метки из /api/model).
    var availableLabels = [];

    function ensureModel(label) {
        if (!label) return null;
        if (!modelStats[label]) {
            modelStats[label] = { input: 0, output: 0, cost: 0, hasCost: false };
        }
        return modelStats[label];
    }

    // Учитывает расход по моделям за один обмен (массив answers).
    function addAnswerUsage(answers) {
        if (!answers || !answers.length) return;
        answers.forEach(function (a) {
            if (!a) return;
            var st = ensureModel(a.label);
            if (!st) return;
            st.input += Math.max(0, parseInt(a.input, 10) || 0);
            st.output += Math.max(0, parseInt(a.output, 10) || 0);
            if (a.cost != null && !isNaN(parseFloat(a.cost))) {
                st.cost += parseFloat(a.cost);
                st.hasCost = true;
            }
        });
        renderModelStats();
    }

    function resetModelStats() {
        Object.keys(modelStats).forEach(function (k) {
            modelStats[k] = { input: 0, output: 0, cost: 0, hasCost: false };
        });
        renderModelStats();
    }

    // Собирает упорядоченный список меток: сначала доступные, затем прочие.
    function orderedLabels() {
        var seen = {}, out = [];
        availableLabels.forEach(function (l) { if (!seen[l]) { seen[l] = 1; out.push(l); } });
        Object.keys(modelStats).forEach(function (l) { if (!seen[l]) { seen[l] = 1; out.push(l); } });
        return out;
    }

    function renderModelStats() {
        if (!tsModels) return;
        var labels = orderedLabels();
        tsModels.innerHTML = "";
        var any = false;
        labels.forEach(function (label) {
            var st = modelStats[label] || { input: 0, output: 0, cost: 0, hasCost: false };
            var tot = st.input + st.output;
            if (tot > 0) any = true;
            var li = document.createElement("li");
            li.className = "tstat-model";
            var cost = "";
            if (st.hasCost) cost = " · ¥" + st.cost.toFixed(6);
            li.innerHTML = '<span class="tm-name">' + esc(label) + '</span>' +
                '<span class="tm-val">' + fmt(st.input) + ' / ' + fmt(st.output) + cost + '</span>';
            tsModels.appendChild(li);
        });
        // Если ни одна модель ещё не отвечала — подсказка
        if (!labels.length || !any) {
            tsModels.innerHTML = '<li class="tstat-model empty">нет данных — задайте вопрос</li>';
        }
    }

    // ------------------------------------------------------------------
    // Подключение файлов/папок для анализа (чтение в браузере)
    // ------------------------------------------------------------------
    // Прикреплённые файлы: [{path, name, content, size}]
    var attachedFiles = [];

    function humanSize(bytes) {
        var b = bytes || 0;
        if (b < 1024) return b + " Б";
        if (b < 1024 * 1024) return (b / 1024).toFixed(1) + " КБ";
        return (b / 1024 / 1024).toFixed(2) + " МБ";
    }

    // Читает выбранные файлы через FileReader (в браузере) и добавляет их.
    function addFiles(fileList) {
        var arr = Array.prototype.slice.call(fileList || []);
        if (!arr.length) return;
        var pending = arr.length;
        arr.forEach(function (file) {
            var reader = new FileReader();
            reader.onload = function () {
                attachedFiles.push({
                    path: (file.webkitRelativePath || file.name || "файл"),
                    name: file.name || "файл",
                    content: String(reader.result || ""),
                    size: file.size || 0
                });
                if (--pending === 0) { renderFiles(); }
            };
            reader.onerror = function () {
                setStatus("Не удалось прочитать файл: " + (file.name || "?"), "error");
                if (--pending === 0) { renderFiles(); }
            };
            reader.readAsText(file);
        });
    }

    function renderFiles() {
        if (!filesList) return;
        filesList.innerHTML = "";
        if (!attachedFiles.length) {
            var empty = document.createElement("li");
            empty.className = "files-empty";
            empty.textContent = "файлы не подключены";
            filesList.appendChild(empty);
            return;
        }
        attachedFiles.forEach(function (f, i) {
            var li = document.createElement("li");
            li.className = "files-item";
            li.title = f.path;
            var name = document.createElement("span");
            name.className = "fname";
            name.textContent = f.name;
            var meta = document.createElement("span");
            meta.className = "fmeta";
            meta.textContent = humanSize(f.size);
            var del = document.createElement("button");
            del.type = "button";
            del.className = "fdel";
            del.textContent = "\u00d7";
            del.title = "Убрать файл";
            del.addEventListener("click", function () {
                attachedFiles.splice(i, 1);
                renderFiles();
            });
            li.appendChild(name);
            li.appendChild(meta);
            li.appendChild(del);
            filesList.appendChild(li);
        });
    }

    function clearFiles() {
        attachedFiles = [];
        renderFiles();
        if (filesInput) filesInput.value = "";
        if (dirInput) dirInput.value = "";
    }

    // Отправляет подключённые файлы на разовый анализ к выбранным моделям.
    function askFiles() {
        if (busy) return;
        if (!attachedFiles.length) {
            setStatus("Подключите хотя бы один файл.", "error");
            return;
        }
        busy = true;
        if (filesAsk) filesAsk.disabled = true;
        submit.disabled = true;
        setStatus("Анализирую файлы…", "");

        var question = qEl.value.trim();
        var body = {
            files: attachedFiles.map(function (f) {
                return { name: f.name, path: f.path, content: f.content };
            }),
            question: question,
            models: selectedModelsPayload(),
            max_tokens: mtValue(),
            compact: getCompactPayload(),
            strategy: getStrategyPayload(),
        };

        fetch("/api/ask_files", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body)
        })
        .then(function (resp) {
            return resp.json().catch(function () {
                return { ok: false, error: "Сервер вернул некорректный ответ." };
            });
        })
        .then(function (data) {
            setStatus("", "");
            if (!data.ok) {
                setStatus(data.error || "Не удалось выполнить анализ.", "error");
                return;
            }
            var qLabel = question
                ? question
                : "Анализ файлов: " + attachedFiles.map(function (f) { return f.name; }).join(", ");
            items.push({ role: "user", content: qLabel });
            items.push({ role: "assistant", content: data.text || "",
                         html: data.html || "", answers: data.answers || [] });
            lastQuestion = question;
            lastAnswers = data.answers || null;
            analysisItem = null;
            render();
            addTokenUsage(data.usage);
            addAnswerUsage(data.answers);
            applyContextStats(data.context);
            renderTrace(data.trace, data.meta);
        })
        .catch(function (err) { setStatus("Ошибка связи: " + err.message, "error"); })
        .finally(function () {
            busy = false;
            if (filesAsk) filesAsk.disabled = false;
            submit.disabled = false;
        });
    }

    if (filesInput) filesInput.addEventListener("change", function () {
        addFiles(filesInput.files);
        filesInput.value = "";
    });
    if (dirInput) dirInput.addEventListener("change", function () {
        addFiles(dirInput.files);
        dirInput.value = "";
    });
    if (filesClear) filesClear.addEventListener("click", clearFiles);
    if (filesAsk) filesAsk.addEventListener("click", askFiles);

    // ------------------------------------------------------------------
    // Автотест: тема из окна запроса отправляется N раз подряд
    // ------------------------------------------------------------------
    // Тема — текст в #question. Каждый запрос идёт как обычный (/api/ask),
    // пишется в историю и участвует в автосжатии. Ответы показываются в чате.
    var AUTO_DEFAULT_COUNT = 50;
    var autoRunning = false;      // идёт ли прогон
    var autoStopped = false;      // запрошен ли останов

    function autoStop() {
        if (!autoRunning) return;
        autoStopped = true;
        setStatus("Останавливаю автотест после текущего запроса…", "");
    }

    // Один запрос автотеста; возвращает Promise<{ok, data}>.
    function autoAskOnce(question) {
        var body = {
            question: question,
            models: selectedModelsPayload(),
            max_tokens: mtValue(),
            compact: getCompactPayload(),
            strategy: getStrategyPayload(),
        };
        return fetch("/api/ask", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body)
        })
        .then(function (resp) {
            return resp.json().catch(function () {
                return { ok: false, error: "Сервер вернул некорректный ответ." };
            });
        })
        .then(function (data) {
            if (data.ok) {
                items.push({ role: "user", content: question });
                items.push({ role: "assistant", content: data.text || "",
                             html: data.html || "", answers: data.answers || [] });
                lastQuestion = question;
                lastAnswers = data.answers || null;
                analysisItem = null;
                render();
                addTokenUsage(data.usage);
                addAnswerUsage(data.answers);
                applyContextStats(data.context);
                if (data.facts) renderFacts(data.facts);
                if (data.branches) renderBranches(data.branches);
                renderTrace(data.trace, data.meta);
            }
            return { ok: !!data.ok, error: data.error || "" };
        })
        .catch(function (err) {
            return { ok: false, error: err.message };
        });
    }

    function delay(ms) {
        return new Promise(function (res) { setTimeout(res, ms); });
    }

    function runAutoTest() {
        if (busy || autoRunning) return;
        var theme = qEl.value.trim();
        if (!theme) { setStatus("Введите тему в окне запроса.", "error"); return; }

        var count = parseInt(autotestCount ? autotestCount.value : AUTO_DEFAULT_COUNT, 10);
        if (isNaN(count) || count < 1) count = AUTO_DEFAULT_COUNT;
        if (count > 500) count = 500;
        if (autotestCount) autotestCount.value = count;

        var delaySec = parseFloat(autotestDelay ? autotestDelay.value : 1);
        if (isNaN(delaySec) || delaySec < 0) delaySec = 1;
        if (delaySec > 60) delaySec = 60;
        if (autotestDelay) autotestDelay.value = delaySec;

        autoRunning = true;
        autoStopped = false;
        busy = true;
        submit.disabled = true;
        if (filesAsk) filesAsk.disabled = true;
        if (autotestStart) autotestStart.disabled = true;
        if (autotestStop) autotestStop.disabled = false;
        qEl.value = "";

        var done = 0, errors = 0;
        var total = count;

        function step() {
            if (autoStopped || done >= total) {
                finish();
                return;
            }
            setStatus("Автотест: " + (done + 1) + "/" + total +
                      " · ошибок: " + errors, "");
            autoAskOnce(theme).then(function (res) {
                done++;
                if (!res.ok) errors++;
                if (autoStopped || done >= total) {
                    finish();
                    return;
                }
                delay(Math.round(delaySec * 1000)).then(step);
            });
        }

        function finish() {
            autoRunning = false;
            busy = false;
            submit.disabled = false;
            if (filesAsk) filesAsk.disabled = false;
            if (autotestStart) autotestStart.disabled = false;
            if (autotestStop) autotestStop.disabled = true;
            var msg = "Автотест завершён: " + done + "/" + total +
                      (errors ? (" · ошибок: " + errors) : "");
            if (autoStopped) msg = "Автотест остановлен: " + done + "/" + total +
                      (errors ? (" · ошибок: " + errors) : "");
            setStatus(msg, errors ? "error" : "ok");
        }

        step();
    }

    if (autotestStart) autotestStart.addEventListener("click", runAutoTest);
    if (autotestStop) autotestStop.addEventListener("click", autoStop);

    // ------------------------------------------------------------------
    // Отправка запроса
    // ------------------------------------------------------------------
    function ask() {
        var question = qEl.value.trim();
        if (!question || busy) { if (!question) setStatus("Введите запрос.", "error"); return; }

        busy = true;
        submit.disabled = true;
        setStatus("Обрабатываю…", "");
        qEl.value = "";

        // Сервер сам держит историю диалога; передаём вопрос, выбор моделей,
        // max_tokens и настройки сжатия.
        var body = {
            question: question,
            models: selectedModelsPayload(),
            max_tokens: mtValue(),
            compact: getCompactPayload(),
            strategy: getStrategyPayload(),
        };

        fetch("/api/ask", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body)
        })
        .then(function (resp) {
            return resp.json().catch(function () {
                return { ok: false, error: "Сервер вернул некорректный ответ." };
            });
        })
        .then(function (data) {
            setStatus("", "");
            if (!data.ok) {
                setStatus(data.error || "Произошла ошибка.", "error");
                return;
            }
            items.push({ role: "user", content: question });
            items.push({ role: "assistant", content: data.text || "",
                         html: data.html || "", answers: data.answers || [] });
            lastQuestion = question;
            lastAnswers = data.answers || null;
            analysisItem = null;
            render();
            addTokenUsage(data.usage);
            addAnswerUsage(data.answers);
            applyContextStats(data.context);
            if (data.facts) renderFacts(data.facts);
            if (data.branches) renderBranches(data.branches);
            renderTrace(data.trace, data.meta);
        })
        .catch(function (err) { setStatus("Ошибка связи: " + err.message, "error"); })
        .finally(function () {
            busy = false;
            submit.disabled = false;
        });
    }

    // Сброс (очистка) диалога — на сервере и локально.
    function startNewChat() {
        fetch("/api/newchat", { method: "POST" })
            .then(function () { clearLocalChat("Начат новый разговор."); })
            .catch(function () {
                // даже если сеть не ответила, очистим окно
                clearLocalChat("Новый разговор (сессия очищена локально).");
            });
    }

    function clearLocalChat(statusText) {
        items = [];
        analysisItem = null;
        lastAnswers = null;
        lastQuestion = "";
        var analyzeBtn = document.getElementById("analyze");
        if (analyzeBtn) analyzeBtn.disabled = false;
        // анализ доступен только когда есть три ответа; по умолчанию оставим активным
        resetTokenStats();
        resetModelStats();
        resetContextStats();
        render();
        renderTrace(null, null);
        setStatus(statusText, "ok");
        qEl.focus();
    }

    // Загрузка сохранённой истории с сервера (непрерывность беседы).
    function loadSession() {
        return fetch("/api/session")
            .then(function (r) { return r.ok ? r.json() : { ok: false }; })
            .then(function (d) {
                if (d && d.ok !== false && Array.isArray(d.messages)) {
                    items = d.messages.slice();
                    // Применяем настройки сжатия и стратегии с сервера
                    applyCompactFromServer(d.compact);
                    applyStrategyFromServer(d.strategy);
                    renderFacts(d.facts);
                    renderBranches(d.branches);
                    // накапливаем статистику токенов из сохранённой истории
                    tokIn = 0;
                    tokOut = 0;
                    tokHist = 0;
                    modelStats = {};
                    items.forEach(function (m) {
                        if (m && m.role === "assistant") {
                            if (m.usage) {
                                tokIn += Math.max(0, parseInt(m.usage.input, 10) || 0);
                                tokOut += Math.max(0, parseInt(m.usage.output, 10) || 0);
                                tokHist += Math.max(0, parseInt(m.usage.history, 10) || 0);
                            }
                            if (Array.isArray(m.answers)) {
                                m.answers.forEach(function (a) {
                                    if (!a || !a.label) return;
                                    var st = ensureModel(a.label);
                                    st.input += Math.max(0, parseInt(a.input, 10) || 0);
                                    st.output += Math.max(0, parseInt(a.output, 10) || 0);
                                    if (a.cost != null && !isNaN(parseFloat(a.cost))) {
                                        st.cost += parseFloat(a.cost);
                                        st.hasCost = true;
                                    }
                                });
                            }
                        }
                    });
                    renderTokenStats();
                    renderModelStats();
                    applyContextStats(d.context);
                    render();
                    if (d.has_history) {
                        setStatus("Загружена сохранённая сессия.", "ok");
                    }
                }
            })
            .catch(function () {});
    }

    // ------------------------------------------------------------------
    // Инициализация
    // ------------------------------------------------------------------
    submit.addEventListener("click", ask);
    qEl.addEventListener("keydown", function (e) {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            ask();
        }
    });

    var newchatBtn = document.getElementById("newchat");
    if (newchatBtn) newchatBtn.addEventListener("click", startNewChat);

    // Кнопка «Анализ» (анализ ответов через GigaChat).
    var analyzeBtn = document.getElementById("analyze");
    if (analyzeBtn) analyzeBtn.addEventListener("click", function () {
        if (!lastAnswers || !lastAnswers.length) {
            setStatus("Сначала получите ответы моделей.", "error");
            return;
        }
        if (busy) return;
        analyzeBtn.disabled = true;
        setStatus("Анализирую ответы через GigaChat…", "");
        fetch("/api/analyze", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ question: lastQuestion, answers: lastAnswers })
        })
        .then(function (resp) {
            return resp.json().catch(function () {
                return { ok: false, error: "Сервер вернул некорректный ответ." };
            });
        })
        .then(function (data) {
            if (!data.ok) { setStatus(data.error || "Анализ не выполнен.", "error"); return; }
            analysisItem = { html: data.html || "", content: data.text || "" };
            setStatus("Анализ готов.", "ok");
            render();
        })
        .catch(function (err) { setStatus("Ошибка связи: " + err.message, "error"); })
        .finally(function () { analyzeBtn.disabled = false; });
    });

    // Загружаем список доступных моделей и строим панель выбора.
    fetch("/api/model")
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
            if (!d) return;
            if (d.available && d.available.length) {
                availableLabels = d.available.map(function (m) { return m.label; });
                buildModelControls(d.available);
            } else if (d.models && d.models.length) {
                availableLabels = d.models.slice();
                buildModelControls(d.models.map(function (l) {
                    return { label: l, cls: "" };
                }));
            }
            renderModelStats();
        })
        .catch(function () {});

    // Инициализация настройки max_tokens (поле выключено по умолчанию).
    if (mtInput) { mtInput.value = MTOK_DEFAULT; }
    refreshMtUI();
    renderModelStats();
    renderModelsTitle();
    resetContextStats();
    renderFiles();
    // Показываем панели facts/веток согласно активной стратегии.
    syncStrategyUI();
    renderFacts({});
    renderBranches({ branches: [{ name: "main", size: 0 }], active_branch: 0 });

    // Загружаем историю с сервера (если она есть на диске).
    loadSession();
})();
