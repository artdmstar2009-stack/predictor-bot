from __future__ import annotations

from typing import Any

VERSION = "SILENT_RESULTS_ENHANCED_V2"

# Отключает отправку сообщений о завершении матчей
# Результаты видны только в приложении (вкладка "Завершенные")

def apply(bot: Any) -> None:
    """
    Отключает сообщения бота о завершении матчей.
    Результаты остаются в БД и видны в Mini App -> "Завершенные"
    """
    if getattr(bot, "_SILENT_RESULTS_APPLIED", False):
        return
    
    # Отключаем функцию отправки уведомлений о завершении
    original_notify_match_finished = getattr(bot, "notify_match_finished", None)
    if callable(original_notify_match_finished):
        async def silent_notify(*args, **kwargs):
            # Просто игнорируем отправку - результаты в БД остаются
            return None
        
        bot.notify_match_finished = silent_notify
    
    # Отключаем сообщения о результатах
    original_broadcast_result = getattr(bot, "broadcast_match_result", None)
    if callable(original_broadcast_result):
        async def silent_broadcast(*args, **kwargs):
            return None
        
        bot.broadcast_match_result = silent_broadcast
    
    # Отключаем отправку результатов подписчикам
    original_notify_subscribers = getattr(bot, "notify_subscribers", None)
    if callable(original_notify_subscribers):
        async def silent_subscribers(*args, **kwargs):
            return None
        
        bot.notify_subscribers = silent_subscribers
    
    # Отключаем уведомления о ставках
    original_notify_bets = getattr(bot, "notify_bets_resolved", None)
    if callable(original_notify_bets):
        async def silent_bets(*args, **kwargs):
            return None
        
        bot.notify_bets_resolved = silent_bets
    
    # Заглушаем логирование сообщений о матчах
    original_send_message = getattr(bot, "send_message", None)
    if callable(original_send_message):
        _original_send = original_send_message
        
        async def filtered_send_message(*args, **kwargs):
            text = kwargs.get("text", "") or (args[1] if len(args) > 1 else "")
            chat_id = args[0] if len(args) > 0 else kwargs.get("chat_id")
            
            # Не отправляем сообщения о завершении матчей
            if any(phrase in str(text).lower() for phrase in [
                "матч завершен",
                "match finished",
                "результат",
                "result",
                "finished with id",
                "завершение",
                "closed with id",
                "статус изменен",
            ]):
                # Логируем в БД но не отправляем
                logger = getattr(bot, "logger", None)
                if logger:
                    logger.debug(f"SILENT: Матч результат не отправлен в чат {chat_id}")
                return None
            
            # Остальные сообщения отправляем нормально
            return await _original_send(*args, **kwargs)
        
        bot.send_message = filtered_send_message
    
    # Отключаем уведомления в группах
    original_send_group_message = getattr(bot, "send_group_message", None)
    if callable(original_send_group_message):
        async def silent_group(*args, **kwargs):
            return None
        
        bot.send_group_message = silent_group
    
    bot._SILENT_RESULTS_APPLIED = True
    print(f"{VERSION}_APPLIED - Сообщения о завершении матчей ОТКЛЮЧЕНЫ")
