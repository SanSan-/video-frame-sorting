"""Доменные ошибки приложения."""


class FrameSorterError(RuntimeError):
    """Базовая понятная пользователю ошибка."""


class ValidationError(FrameSorterError):
    """Ошибка входного каталога, CSV или настроек."""


class AnalysisCancelledError(FrameSorterError):
    """Анализ отменён до публикации результата."""


class TransactionError(FrameSorterError):
    """Ошибка безопасного переименования или восстановления."""

