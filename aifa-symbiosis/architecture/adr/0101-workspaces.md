# ADR-0101. Workspaces: folders become server-side document groups

Статус: предложено
Заменяет: —

## Контекст

Папка (`Folder`) в браузере группирует чаты, и чат внутри папки уже отправляет `project_id =
folder_id` (`aifa/web/src/lib/use-chat-run.ts:950`). Но принадлежность документа папке сейчас
живёт только в клиентском снимке `dialogs`: список `folders[].doc_ids` приезжает с `/dialogs` и
сливается по правилу last-write, а серверный реестр документов (`document(doc_id, owner_id,
meta)`) не знает ни папки, ни проверяет список.

Для multi-document turn (`aifa-multi-document-turn`) и поиска (`aifa-document-search`) нужна
серверная группировка документов, которая соответствует папке. Решение — workspace: папка есть
workspace, у каждого владельца есть default workspace для чатов вне папок.

## Решение

### W1. Workspace id = folder id, plus `owner:<owner_id>` for chats outside folders

- Папке соответствует workspace с тем же id.
- У каждого владельца есть default workspace `owner:<owner_id>`, создаваемый при первом
  использовании.
- Id папки должен удовлетворять `[A-Za-z0-9_-]{1,64}`. Префикс `owner:` зарезервирован: клиент
  не может создать workspace с таким id.

### W2. Workspace создаётся и переименовывается из `/dialogs` и из upload

- После `_merge_dialog_states` (`api/main.py:2140`) для каждой валидной папки вызывается upsert:
  insert при новом id, rename если папка новее, skip + log если id чужое.
- Upsert выполняется в `try`: запись workspace не ломает push.
- `POST /documents/upload` принимает `workspace_id`. Пустое — default. Неизвестный id
  создаётся для вызывающего. Чужой или `owner:другой` — 404.

### W3. `document.workspace_id` — отдельная колонка, добавляемая при старте store

- Колонка `workspace_id` (nullable, с индексом) — источник истины. Значение в `meta` JSON
  игнорируется.
- `SqlDocumentStore.add_document` на insert ставит `meta.workspace_id or f"owner:{owner_id}"`,
  upsert'яя default workspace. На conflict: `workspace_id = COALESCE(NULLIF(excluded.workspace_id,
  ''), document.workspace_id)`, так что перезапись без workspace не сбрасывает папку.
- Колонка создаётся в `SqlDocumentStore.__init__` командой `ALTER TABLE document ADD COLUMN IF
  NOT EXISTS workspace_id …` и индексом сразу после `create_all`.

### W4. One-shot backfill из dialog states

`store/migrate_workspaces.py [--apply]` (dry run по умолчанию):
- Перебирает владельцев из `SELECT DISTINCT owner_id FROM document`;
- Для каждого читает его dialog state;
- Сопоставляет документ ↔ папки из `folders[].doc_ids` и `projects[].doc_ids` чатов с
  `folder_id`;
- Учитывает только пары, где владелец документа совпадает с владельцем state;
- Одна папка → она; несколько → самая старая, конфликты логируются; нет → default;
- Повторный запуск не трогает уже заполненные строки.

### Non-goals

- Нет shared workspaces и members: доступ остаётся owner-based.
- Нет перемещения документов между workspace.
- Нет удаления workspace вместе с папкой (нет tombstone).
- Нет `GET /workspaces/{id}/documents` и папочной панели в v2.
- Нет `state` jsonb: сообщения и UI state остаются в dialogs/sessions.
- Нет `NOT NULL` constraint: это сломал бы rollback к образу, пишущему без колонки.

## Следствия

- `aifa-multi-document-turn` может выбирать документы по workspace.
- `aifa-document-search` хранит `workspace_id` в индексе, но доступ по-прежнему owner-based.
- Контракт C1 получает исключение: `POST /documents/upload` принимает `workspace_id`, и
  фронтенд `use-doc-upload.ts` отправляет folder id при загрузке из папки (исключение из
  ADR-0015).
- В `ARCHITECTURE.md` §4.3 `doc_store` помечен как «переделка + workspace_id».

## Чем проверяется

- F13 (`aifa/tests/architecture/test_adr_discipline.py`) — новый ADR валиден и ARCHITECTURE/
  CONTRACTS обновлены.
- Контрактные тесты C1: upload с `workspace_id` и без него.
