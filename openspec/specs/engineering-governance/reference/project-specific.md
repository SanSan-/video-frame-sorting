# Локальные инженерные правила

Нормативное приложение [инженерной спецификации](../spec.md): проектные ограничения, параметры и рабочие команды.

## Язык и совместимость скриптов

- Текстовые файлы проекта — UTF-8 без BOM.
- Комментарии, журналы приложения и строки документации Python — по-русски.
- Сценарии запуска приложения на PowerShell, включая run_web.ps1, сохраняют ASCII для Windows PowerShell 5.1.

## Владельцы предметных контрактов

При выборе операции применяются требования её спецификации; инженерное приложение не дублирует файловые проверки,
состояния транзакции или поведение интерфейса.

| Область                                                     | Нормативный владелец                                                                                                             |
|-------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------|
| Вход JPEG, анализ и переносимый план                        | [ANA-001…006](../../frame-analysis/spec.md)                                                                                      |
| Применение, отказ, восстановление и обратное переименование | [REN-001…006](../../rename-transactions/spec.md)                                                                                 |
| Пересборка, проверка и публикация MP4                       | [VID-001…008](../../video-rebuild/spec.md)                                                                                       |
| Локальный web, общий сервис и состояние операций            | [WEB-001…007](../../local-web-workflow/spec.md)                                                                                  |
| Границы текущего продукта и будущие возможности             | [CTX-001/002](../../project-context/spec.md), [действующее изменение](../../../changes/plan-local-frame-enhancement/proposal.md) |

Сведения об изменениях исходников ведутся в [CHANGELOG.md](../../../../CHANGELOG.md); изменение поведения требует его обновления вместе
с документацией предметного контракта.

## Среда и зависимости

[pyproject.toml](../../../../pyproject.toml) определяет пакет, минимальную версию Python, сборку и наборы зависимостей;
[requirements.txt](../../../../requirements.txt) — закреплённый установочный набор. Перечень версий здесь не копируется.
Рабочие команды используют Python 3.14 и `.venv`. FFmpeg/ffprobe берутся из PATH; фактические версии фиксируются при
проверке приложения. Диалог выбора каталога Windows использует tkinter.

Существующая установка по необходимости:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Рабочие команды из корня; область их применения определяется предметным контрактом и разрешённой задачей:

```powershell
.\.venv\Scripts\python.exe -m frame_sorter analyze --folder "<frames-directory>"
.\.venv\Scripts\python.exe -m frame_sorter preview --folder "<frames-directory>" --csv "<plan.csv>"
.\.venv\Scripts\python.exe -m frame_sorter apply --folder "<frames-directory>" --csv "<plan.csv>"
.\.venv\Scripts\python.exe -m frame_sorter recover --folder "<frames-directory>"
.\.venv\Scripts\python.exe -m frame_sorter rebuild --folder "<frames-directory>" --original-video "<original.mp4>" --output-video "<new-name.mp4>"
powershell -NoProfile -ExecutionPolicy Bypass -File .\run_web.ps1 -HostAddress 127.0.0.1 -Port 7863
```

Ошибки пользовательского ввода и предметной области CLI возвращают 1 без traceback, KeyboardInterrupt — 130.
Дополнительный рабочий вход — [rename_frames.py](../../../../rename_frames.py); контракт его подтверждения определяет
REN-001.

## Проверки

Применимый обязательный набор для изменения поведения:

```powershell
.\.venv\Scripts\python.exe -B -m ruff check .
.\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider -q
.\.venv\Scripts\python.exe -m compileall -q frame_sorter tests rename_frames.py
node --check frame_sorter\web\static\app.js
.\.venv\Scripts\python.exe -B -m pip check
openspec validate --all --strict --no-interactive
```

Общий pytest включает `test_real_ffmpeg_rebuilds_small_video`: он требует FFmpeg и обрабатывает синтетическое видео в
`tmp_path`. Это проверка приложения, а не только статических правил. Если разрешена пользовательская приёмка медиа,
она включает анализ реального каталога без изменения JPEG. Проверки с переименованием и сквозная пересборка используют
отдельные копии JPEG, оригинального MP4 и материалов восстановления, а также новый путь результата; пользовательские
оригиналы и существующие результаты сохраняются.
