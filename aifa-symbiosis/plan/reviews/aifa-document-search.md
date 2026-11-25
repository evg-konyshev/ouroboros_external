# Ревью `aifa-document-search`

Дата: 24.11.2026

## Вход

- Коммит: `786a80ca67869d8ada9f93ea290364f717a8cf21` (`feat(aifa): add document search`).
- Диапазон: `HEAD~1...HEAD`, база `e2fbcd65912c3439e3ce0f33194e00b0467cb53a`.
- Спецификация: `openspec/changes/aifa-document-search/`.
- Архитектура: `aifa-symbiosis/architecture/adr/0102-document-search.md`, `CONTRACTS.md`.
- Стандарты: `AGENTS.md`, `docs/DEVELOPMENT.md`, `docs/INVARIANTS.md`, `aifa/AGENTS.md`,
  `aifa-symbiosis/plan/PRINCIPLES.md`, `aifa-symbiosis/plan/REVIEW_GUIDE.md`,
  `aifa/finengine/docs/rules/coding_standards.md`, `.rules/skills.md`, `.rules/testing.md`.
- Проведены независимые проходы Standards и Spec.

## Вердикт

**Кодовых блокеров не найдено, можно дальше с замечаниями.**

Формально Definition of Done ещё не выполнен: общий gate остаётся красным на известном базовом долге.

## Standards

### Важно, оценочное

`aifa/finengine/src/finengine/store/documents.py:323-335` удерживает `SELECT … FOR UPDATE` во время
вызова `publish()`, внутри которого выполняются сетевые операции OpenSearch — `delete_by_query` и два
`bulk` (`aifa/finengine/src/finengine/search/indexing.py:141-149`). При недоступном или медленном
OpenSearch параллельное изменение или удаление документа ждёт сетевые таймауты, а транзакция и
соединение PostgreSQL остаются занятыми. Блокировка защищает от гонок, но сетевой вызов внутри неё
создаёт операционный риск.

### Мелкие замечания

- `aifa/finengine/src/finengine/search/index_schema.py:10-11` — значения настроек в аргументах по
  умолчанию фиксируются при импорте.
- `aifa/finengine/src/finengine/config.py:7,23` — множество провайдеров дублируется в `frozenset` и
  `Literal`.
- `aifa/finengine/src/finengine/search/client.py:82,94`,
  `aifa/finengine/src/finengine/store/documents.py:341` — публичные методы продакшен-классов
  используются только тестами.
- `aifa/finengine/src/finengine/search/search.py` — 367 строк при рекомендованном пределе 300.
- `aifa/finengine/src/finengine/assistant/search_tool.py:38-42` — строковый `owner_id` помещается в
  поле `User.username`.
- `aifa/finengine/src/finengine/search/chunker.py:126-130` — некорректные
  `chunk_size/chunk_overlap` не валидируются настройками.
- Сообщение коммита на английском, хотя правила симбиоза предписывают русский.

Нарушений архитектурных границ F1/F2/F6/F12/F13 и инвариантов `skills/**` не обнаружено.

## Spec

### Замечание — выход за пределы задачи

`skills/aifa-planning/SKILL.md:18-61` дополнительно сокращает разделы о цикле, приёмке плана,
`aifa_finish` и `ЛИМИТ`. Задача 5.2 (`openspec/changes/aifa-document-search/tasks.md:208-214`)
требовала только добавить правила использования `aifa_search`. Это не ломает код, но расширяет diff и
меняет поведенческие инструкции без отдельного обоснования.

Пропущенных или неверно реализованных требований спецификации не найдено: доступ и allowed ids,
двухстадийный поиск, hybrid/lexical, деградация, лимиты, индексация, удаление и reconcile соответствуют
OpenSpec и ADR-0102.

## Проверки

Проверки выполнены на отдельной архивной копии ровно `HEAD`, без параллельных незакоммиченных
изменений рабочего дерева.

| Проверка | Результат |
|---|---|
| Поисковые тесты с реальными PostgreSQL 16 и OpenSearch 2.19.2 | **80 passed, 1 live-тест skipped** |
| Конфигурация и навыки | **63 passed** |
| Ruff по изменённым модулям | **passed** |
| Новые поисковые тесты на родительском коде | Падают при сборке из-за отсутствия `finengine.search`, как ожидалось |

Полный gate зелёным подтвердить нельзя: `aifa-symbiosis/plan/PROGRESS.md` фиксирует известные ошибки
форматирования, ruff/mypy и инфраструктурные проверки; новых падений относительно базы там не заявлено.

## Итог

- **Standards:** 1 важное оценочное замечание и 7 мелких; самое существенное — сетевые вызовы под
  блокировкой PostgreSQL.
- **Spec:** 1 мелкое замечание о лишних правках навыка.
