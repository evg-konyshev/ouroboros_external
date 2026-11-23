# aifa-workspaces · Независимое ревью · 23.11.2026

Первичный вердикт: **блокеры**. Итог после доработки и повторных ревью: **блокеров нет**.
Миграция на стенде остаётся отдельной задачей P138 после выкатки и сигнала владельца.

## Вход

База: `HEAD=e68aa1c979bdf45f6fd56eddc0379a33591850ae`; коммитов после базы нет.
Просмотрены `git diff HEAD` и все файлы из `git ls-files --others --exclude-standard`.
Ревью проводилось в чистом контексте отдельно по осям Standards и Spec.

Прочитаны:
- `openspec/changes/aifa-workspaces/{proposal.md,design.md,tasks.md,specs/aifa-workspaces/spec.md}`;
- ADR-0101, `ARCHITECTURE.md`, `CONTRACTS.md`, запись шага в `PROGRESS.md`;
- `aifa/AGENTS.md`, `aifa-symbiosis/plan/{PRINCIPLES.md,REVIEW_GUIDE.md}`;
- `aifa/finengine/docs/rules/coding_standards.md` и `docs/build-plan/01-nerushimye-resheniya.md`;
- корневые `docs/INVARIANTS.md`, `docs/DEVELOPMENT.md` §Coding Rules.

Отдельный implementation prompt в `aifa-symbiosis/plan/prompts/` не найден.

## Вертикальное ревью

### Frontend → upload → document store

`use-doc-upload.ts` передаёт `folder_id`, `uploadDocument` добавляет его в `FormData`, route проверяет id до
обращения к каталогу, `SqlDocumentStore` назначает workspace. Основной путь покрыт, но первый явный upload
собственного `owner:<id>` ошибочно получает 404 (В1).

### `/dialogs` → WorkspaceStore

Route проверяет владельца до upsert, но сам store при конфликте перезаписывает `owner_id`. Проверка и запись
не атомарны, а прямые вызовы store/backfill вообще обходят route (Б1).

### Backfill

Dry-run вызывает настоящий upsert (Б2). Ветка default записывает строку `owner:<id>` в документ, но не создаёт
соответствующий workspace (Б3). Тесты используют `SqlDocumentStore.add_document` до миграции, поэтому default
workspace уже существует и Б3 скрыт.

## Блокеры

### Б1. Store перезаписывает владельца чужого workspace

`aifa/finengine/src/finengine/store/workspaces.py:45-63,89-97` передаёт `owner_id` в conflict-update.
Это противоречит spec.md:74-77 («SHALL NOT change a workspace owned by another user») и tasks.md:34-35.
Проверки в `api/main.py:654-656,2372-2375` не закрывают прямые вызовы и гонку между проверкой и записью.

Воспроизведение: создать `F1` для owner 8, затем вызвать
`upsert_workspace(owner_id=7, workspace_id="F1")`; владельцем станет 7. Тот же путь доступен из
`migrate_workspaces.py:98-100` и `documents.py:130-135`.

Нужен атомарный conflict-guard в `WorkspaceStore` и тест из tasks 2.1: чужая запись остаётся неизменной.

### Б2. Dry-run миграции изменяет БД

`aifa/finengine/src/finengine/store/migrate_workspaces.py:93-101` вызывает `upsert_workspace` независимо от
`apply`, хотя design.md:108-110 задаёт dry run по умолчанию.

Воспроизведение: legacy-документ указан в `folders[].doc_ids`; выполнить `migrate(engine, apply=False)` —
папочный workspace появится. Dry-run не должен выполнять ни upsert, ни update; нужен регрессионный тест,
сравнивающий снимок БД до/после.

### Б3. Backfill не создаёт default workspace

`migrate_workspaces.py:94-101` делает upsert только для папки. Для документа вне папок колонка получает
`owner:<id>`, а строка workspace не создаётся, нарушая spec.md:11-16 и 89-98.

Воспроизведение: на genuinely legacy БД без `owner:7` выполнить apply для документа owner 7 вне папок;
`get_document(...).workspace_id == "owner:7"`, но `get_workspace("owner:7") is None`.
`test_document_in_no_folder_gets_default` проверяет только колонку и потому пропускает дефект.

### Б4. В change входят файлы вне разрешённой области

`.agents/skills/.openspec-target` и шесть `.agents/skills/openspec-*/SKILL.md` не относятся к Impact из
proposal.md:54-70 и находятся вне разрешённых `aifa/`, `aifa-symbiosis/`, `skills/aifa-*`
(`PRINCIPLES.md` §А.4). Исключить их из этого change или оформить отдельное решение/изменение.

## Важно

1. **В1 — собственный default отклоняется при первом использовании.** `api/main.py:657-660` возвращает 404
   для любого ещё не созданного `owner:*`, хотя spec.md:41-44 запрещает только default другого владельца.
   Первый upload пользователя 7 с `workspace_id=owner:7` должен создать/использовать его default.
2. **В2 — upsert не идемпотентен.** `workspaces.py:89-97` при повторе перезаписывает `created_at` и
   `updated_at`, нарушая tasks.md:33. Тест `test_upsert_default_is_idempotent` проверяет только id/owner.
3. **В3 — системные ошибки SQLite молча подавляются.** `documents.py:112-116`: `except Exception: pass`
   в основном пути схемы; нарушены coding_standards.md §2.7 и PRINCIPLES.md §В.
4. **В4 — запрещённые вложенные функции.** `api/main.py:2378-2381` и
   `tests/test_workspaces_dialogs_sync.py:95-96`; coding_standards.md §2.1 MUST.
5. **В5 — lazy-импорты без одной из разрешённых причин.** `api/main.py:658,2359-2361`,
   `documents.py:100`, `workspaces.py:46`, `test_workspaces_documents.py:62-64`; §3.1 MUST.
6. **В6 — файловая SQLite вместо production-инфраструктуры.**
   `test_workspaces_dialogs_sync.py:20-23` создаёт SQLite в `tmp_path`; coding_standards.md §6.2 MUST/D23.
7. **В7 — незелёные ворота.** Полный `aifa/scripts/check.sh` не прошёл. Падения workspace-тестов не
   обнаружены, но DoD по PRINCIPLES.md §Д.1 этим прогоном не подтверждён.

## Замечания / judgement calls

- Дублируется проверка id в `api/main.py:650-660,2367-2371` и `workspaces.py:34-42`; лучше одна доменная
  операция вместо копий в толстом route (coding_standards.md §4.1 SHOULD; possible Duplicated Code).
- Upsert повторён в `workspaces.py:45-63`, `documents.py:145-165,173-191` (possible Duplicated Code).
- `WorkspaceStore.get_workspace` возвращает `dict[str, Any]` (`workspaces.py:99-106`) вместо типизированной
  записи (possible Primitive Obsession; PRINCIPLES.md §В).

## Связность тестов и контрактов

| Граница | Контракт | Тест | Настоящие стороны | Падал до изменения? |
|---|---|---|---|---|
| Frontend → FormData | tasks 4.1 | `aifa/web/src/lib/api.upload.test.ts` | настоящий `uploadDocument`, подставной XHR | не прогонялось |
| Upload route → registry | C1.5 | `tests/test_workspaces_api.py` | настоящие route + PostgreSQL; внешний catalog fake | не прогонялось |
| `/dialogs` → WorkspaceStore | W2 | `tests/test_workspaces_dialogs_sync.py` | настоящие route + WorkspaceStore; dialogs SQLite | не прогонялось |
| Backfill → document/workspace | W4 | `tests/test_migrate_workspaces.py` | настоящие stores + PostgreSQL | не прогонялось |
| Document upsert | W3 | `tests/test_workspaces_documents.py` | настоящий store + PostgreSQL | не прогонялось |

Пробелы: нет обязательного tasks 2.1 теста «foreign id unchanged»; dry-run не тестируется; default-тесты не
проверяют существование workspace; idempotency-тест не фиксирует timestamps; нет сценария первого `owner:7`.

## Независимые проверки

- 33 workspace backend-теста на PostgreSQL 16: **33 passed**.
- Ruff по всем затронутым Python-файлам: **passed**.
- `aifa/web`: upload tests **2 passed**; полный `npm test` — **447 passed, 47 files**.
- `aifa/tests/architecture/test_adr_discipline.py`: **13 passed**.
- Полный `aifa/scripts/check.sh`: **FAIL**. Ouroboros stage не стартовала (`Permission denied`, exit 126);
  ruff/mypy службы красные по прежним путям; pytest службы — **134 failed, 5368 passed, 7 errors**;
  mypy симбиоза — прежняя ошибка в `test_dev_config.py`; архитектурные F1/F11 — 2 падения из-за отсутствующего
  `base/ouroboros-1.5.1`; frontend зелёный. Workspace-файлов в списке падений нет.

Прямой деструктивный repro на изолированной схеме не выполнялся: команда с `DROP SCHEMA` остановлена shell-guard.
Сценарии выше подтверждены чтением конкретных веток кода, не представлены как выполненные прогоны.
Миграция на `aifa-sym` не запускалась; остаётся P138 по сигналу владельца после исправления Б2/Б3.

Ревьюер код продукта и тесты не менял.

---

## Доработка и повторные ревью · 23.11.2026

Итог после доработки: **блокеров в проверенном объёме нет**. Первичные Б1–Б4 и В1–В6 закрыты.
Красный общий gate остаётся отдельным известным долгом и не маскируется исправлениями workspaces.

Реализовано:
- атомарный ownership-guard workspace, идемпотентные timestamps и централизованная свежесть rename;
- dry-run без workspace/document updates, создание default и NULL-guard в backfill UPDATE;
- сохранение имени и клиентского timestamp папки, fallback в default при foreign/invalid folder id;
- принятие собственного default в upload и запрет любого `owner:*` как folder id в push;
- запрет смены владельца документа, включая PostgreSQL conflict-guard и конкурентный insert;
- `WorkspaceRow` вместо `dict[str, Any]`, явная SQLite-схема без молчаливого `except`;
- удалены новые nested/lazy imports; тест dialogs переведён на PostgreSQL.

После первого повторного ревью дополнительно закрыты: смена owner документа, остановка backfill на
чужом folder id и восстановление имени placeholder отложенным push. После второго — гонка смены
owner при concurrent insert и затирание параллельно назначенного workspace в backfill.

Все новые регрессии сначала падают по ожидаемой причине. Для backfill NULL-guard RED отдельно
повторён на исходном UPDATE после исправления mock фабрики `WorkspaceStore`.

Независимый ревьюер проверил исправления трижды. Финальное заключение по атомарности:
`documents.py:182-197` запрещает conflict-update чужого owner, а
`migrate_workspaces.py:136-143` обновляет только строки, у которых workspace всё ещё NULL.
Конкретных блокеров в проверенном объёме не осталось.

Проверки доработки:
- **44 workspace backend-теста passed** на PostgreSQL 16, включая конкурентные регрессии;
- ruff всех затронутых Python-файлов — **passed**;
- mypy `workspaces.py`, `migrate_workspaces.py`, `documents.py` — **no issues**;
- SQLite start/restart smoke — **passed**;
- `test_documents_move_to_the_database.py` и остальные смежные тесты документов — **28 passed**;
  отдельный `test_vector_search_stays_removed.py` имеет прежнее падение из-за отсутствующего
  `aifa/finengine/CLAUDE.md`, не связанное с diff.

`.agents/skills/openspec-*` и `.openspec-target` по решению владельца остаются untracked и не
добавляются в commit. Миграция на `aifa-sym` остаётся P138 после выкатки и сигнала владельца.

### Финальный полный gate

`aifa/scripts/check.sh` завершился за 415 секунд со сводкой:
- ouroboros stage — `Permission denied`, exit 126;
- прежние ruff/mypy службы красные (426 mypy errors);
- служба — **134 failed, 5379 passed, 7 errors**: число падений и их состав совпадают с
  базовым полным прогоном, а 11 дополнительных passed соответствуют новым регрессиям;
- mypy симбиоза — прежняя ошибка `test_dev_config.py`; F1/F11 — отсутствует базовый тег;
- frontend — **447 passed, 47 files**.

Общий gate остаётся красным по известным причинам вне workspaces; новых падений изменения не
добавили. Один предыдущий запуск завис на 36% (класс P131), финальный прошёл полностью.
