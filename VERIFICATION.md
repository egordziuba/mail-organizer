# Проверка основы скрытого запуска

- Общий набор: `145 passed` — реальный запуск `.venv/Scripts/python.exe -m pytest -q`.
- Реальная задача `MailOrganizer.HiddenWorker` зарегистрирована; запуск выполнен через Task Scheduler, `launch_mode=windows_task`.
- Реальный worker: живой PID с проверкой времени создания, свежий heartbeat, видимых окон `0`.
- Проверены отдельные synthetic процессы: double-start, pause/resume, освобождение mutex после аварии, тайм-аут с завершением всего принадлежащего команде дерева.
- Проверены временная Windows read-lock при atomic replace, сохранение credential cache при сбое refresh, narrow OAuth scopes, защита общего архива от параллельного replay.
- Live mailbox consent не выполнен: Gmail-1/Gmail-2 — `auth_required`; Outlook-1 — `configuration_required`, public client ID отсутствует.
- Реальные письма не выгружались, календарь не изменялся. Full backfill/history/delta, OCR, семантический реестр дел и calendar writer не считаются готовыми.
- Физическая перезагрузка ПК не проводилась: подтверждены зарегистрированные триггеры после входа и реальный скрытый запуск.

Пакет исходников не содержит писем, токенов, приватных OAuth JSON, runtime.json, .runtime или виртуального окружения. Установленный worker продолжает использовать исходную папку проекта.
