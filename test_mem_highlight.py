# -*- coding: utf-8 -*-
"""Проверка подсветки фрагментов ответа, взятых из памяти агента."""
import sys
sys.stdout.reconfigure(encoding="utf-8")

from rtk_app.session_store import SessionStore
from rtk_app.agent import Agent
from rtk_app.key_store import read_api_key
from rtk_app import config

# Сессия с непустой рабочей и долговременной памятью.
store = SessionStore()
store.set_memory_bulk("working", {"бюджет": "15000", "расходы": "день"})
store.set_memory_bulk("longterm", {"город": "казань", "срок": "3 дня"})

# Что уйдёт в запрос (включая память и инструкцию о маркерах).
ctx = store.get_context_messages()
print("=== Контекст (system-сообщения) ===")
for m in ctx:
    if m.get("role") == "system":
        print(m["content"])
        print("---")

# Реальный запрос к GigaChat через агента.
key = ""
try:
    key = read_api_key(config.DS_KEY_FILE)
except Exception:
    pass
agent = Agent(key)
res = agent.answer(
    "Сколько я трачу в день и в каком городе я нахожусь? Напомни бюджет и срок.",
    ctx, selected=[{"label": "GigaChat", "temperature": 0.3}],
    memory=store.memory_state(),
)
print("=== ok:", res["ok"], "===")
print("=== memory_used:", res.get("memory_used"), "===")
print("=== HTML ===")
print(res["html"])
