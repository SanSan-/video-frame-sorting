# Примеры по реальному коду

Примеры описывают условия существующих тестов, а не новый результат их запуска. Контракт находится в указанной
спецификации; выполнение на пользовательских медиа требует самостоятельного доказательства.

## Положительный и граничный случаи

1. **Известный лаг 7 и неизвестный лаг 6.** В [test_analysis.py](../../../tests/test_analysis.py) тест
   `test_strict_known_lag_accepts_near_duplicate_when_bypass_is_not_smooth` сравнивает исходную позицию 1 с повтором
   в позиции 8: ветка `strict-known-lag` принимает его при неплавном обходе. Тест
   `test_strict_similarity_does_not_override_unknown_lag` переносит исходную позицию на 2; повтор в позиции 8
   больше не принимается. Контракт: [ANA-003](../../specs/frame-analysis/spec.md).

2. **Отказ второй операции первой фазы.** В [test_renamer.py](../../../tests/test_renamer.py) тест
   `test_failure_during_first_phase_rolls_back` подменяет второе перемещение ошибкой `OSError`.
   Проверяются `TransactionError`, неизменное соответствие имён и хешей содержимого и единственный журнал
   со статусом `rolled_back`. Контракт: [REN-003/004](../../specs/rename-transactions/spec.md).

## Отвергаемые случаи

3. **Несвязанный обратный CSV.** В [test_workflow_artifacts.py](../../../tests/test_workflow_artifacts.py) тесты
   `test_undo_pair_without_journal_is_ignored` и `test_authoritative_sort_does_not_fall_back_to_orphan_undo`
   не допускают пересборку по обратному CSV без соответствующего журнала.
   Связь отмены задаёт [REN-005](../../specs/rename-transactions/spec.md), готовность каталога —
   [WEB-004](../../specs/local-web-workflow/spec.md).

4. **Потеря аудиокадров или длительности последнего кадра.**
   В [test_video.py](../../../tests/test_video.py) тесты `test_output_verification_rejects_lost_audio_frames`
   и `test_output_verification_rejects_collapsed_last_frame_duration` отклоняют соответствующие нарушения.
   Контракты: [VID-003/006](../../specs/video-rebuild/spec.md).

## Сквозной пример и повтор операции

5. **Синтетический VFR-результат с настоящим FFmpeg.**
   В [test_video.py](../../../tests/test_video.py) тест `test_real_ffmpeg_rebuilds_small_video` использует
   изображения 96×64 и AAC в `tmp_path`. Ожидаются четыре кадра, `audio_copied=True`, последний интервал
   0.120000 с, общая длительность 0.480000 с и непустой новый MP4.
   Тест входит в общий pytest и запускает реальные процессы. Если FFmpeg или ffprobe отсутствует, он получает
   `SKIPPED`; это не подтверждает пересборку. Контракт: [VID-001…007](../../specs/video-rebuild/spec.md).

6. **Однократное восстановление истории после перезапуска.**
   В [test_job_store.py](../../../tests/test_job_store.py) тест `test_recovery_terminalizes_active_job_once`
   проверяет переход `running → interrupted`, признаки `active=False`, `terminal=True` и событие `done`;
   повторное восстановление возвращает пустой список.
   `test_roundtrip_unicode_and_bounded_history` при лимите 2 сохраняет две последние задачи и события 2/3.
   Контракт: [WEB-006](../../specs/local-web-workflow/spec.md).

7. **История подключается только при явном веб-запуске.**
   В [test_web_app.py](../../../tests/test_web_app.py) тест `test_sqlite_is_enabled_only_for_explicit_web_runtime`
   сначала проверяет отсутствие файла БД без явного режима веб-запуска. Затем явный запуск восстанавливает задачу
   со статусом `interrupted` и подключает хранилище; после завершения сессии хранилище освобождено.
   Контракт: [WEB-006](../../specs/local-web-workflow/spec.md#requirement-web-006-sse-и-ограниченная-история).

8. **Прямое переименование по устаревшему плану.**
   В [test_web_app.py](../../../tests/test_web_app.py) тест
   `test_direct_rename_rejects_stale_algorithm_without_changing_frames` передаёт согласованный план версии
   `local-return-v1` в подтверждённый `POST /api/rename`. Принятая задача получает `failed`; имена и хеши JPEG
   сохраняются, журнал и обратный CSV не создаются. В [test_renamer.py](../../../tests/test_renamer.py)
   `test_plan_algorithm_is_checked_before_transaction` проверяет тот же отказ при предварительном просмотре
   и применении для устаревшей, отсутствующей и некорректной версии. Контракты:
   [REN-002](../../specs/rename-transactions/spec.md), [WEB-004](../../specs/local-web-workflow/spec.md).

9. **Следующая совместимая аудиодорожка с настоящим FFmpeg.**
   В [test_video.py](../../../tests/test_video.py) тест `test_real_ffmpeg_selects_later_aac_from_mov_named_mp4`
   создаёт синтетический MOV с расширением `.mp4`: первая дорожка PCM μ-law длится 0,8 с, следующая AAC и четыре
   видеокадра — 0,16 с. Результат содержит AAC выбранного потока с тем же числом аудиокадров и сэмплов; длительность
   проверяется как 0,16 с с допуском 0,025 с, исходный файл сохраняется. Этот тест требует установленных FFmpeg
   и ffprobe; их отсутствие означает `SKIPPED`, а не подтверждённую пересборку.
   `test_inconsistent_aac_timestamps_trigger_sample_preserving_normalization` отдельно проверяет нормализацию
   выбранной AAC-дорожки с индексом 3 при неподдерживаемой первой. Контракты:
   [VID-005/006](../../specs/video-rebuild/spec.md).

10. **Полная диагностика длительности.**
    В [test_video.py](../../../tests/test_video.py) тест
    `test_duration_errors_include_actual_expected_delta_and_tolerance` проверяет сообщения о сокращённом
    последнем кадре и увеличенной длительности видеоряда: фактическое и ожидаемое значения, абсолютную разницу
    и допуск в секундах. Контракт: [VID-008](../../specs/video-rebuild/spec.md).
