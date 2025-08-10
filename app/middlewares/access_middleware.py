# app/middlewares/access_middleware.py
from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware, Bot
from aiogram.types import Update, Message, CallbackQuery # Добавил Bot

from app.db.database import AsyncSessionFactory
from app.db.models import User
from sqlalchemy import select
import traceback # Для детальных ошибок

class BanCheckMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Update, Dict[str, Any]], Awaitable[Any]],
        event: Update, 
        data: Dict[str, Any] # data['bot'] будет содержать экземпляр Bot
    ) -> Awaitable[Any]:
        
        user_id = None
        chat_id_to_reply = None # Может быть None, если не чат (например, inline query)
        
        # Пытаемся получить user_id из различных типов событий в Update
        if event.message and event.message.from_user:
            user_id = event.message.from_user.id
            chat_id_to_reply = event.message.chat.id
            print(f"DEBUG BanCheck: Event is Message from user_id: {user_id} in chat_id: {chat_id_to_reply}")
        elif event.callback_query and event.callback_query.from_user:
            user_id = event.callback_query.from_user.id
            if event.callback_query.message: # У CallbackQuery может быть message
                 chat_id_to_reply = event.callback_query.message.chat.id
            print(f"DEBUG BanCheck: Event is CallbackQuery from user_id: {user_id}, reply_chat_id: {chat_id_to_reply}")
        # Добавьте другие elif для других типов event, если они релевантны для бана (например, inline_query)
        
        # Если user_id не удалось определить, пропускаем проверку
        if not user_id:
            print("DEBUG BanCheck: user_id could not be determined from the event. Proceeding without ban check.")
            return await handler(event, data)

        print(f"DEBUG BanCheck: Checking ban status for user_id: {user_id}")
        is_banned_in_db = False
        try:
            async with AsyncSessionFactory() as session, session.begin():
                db_flag_result = await session.execute(
                    select(User.is_banned).where(User.telegram_id == user_id)
                )
                db_flag = db_flag_result.scalar_one_or_none()
                
                print(f"DEBUG BanCheck: DB is_banned_flag for {user_id}: {db_flag} (type: {type(db_flag)})")
                
                if db_flag is True: # Явная проверка на булево True
                    is_banned_in_db = True
        except Exception as e_db:
            print(f"ERROR BanCheck: DB error checking ban status for {user_id}: {e_db}\n{traceback.format_exc()}")
            # В случае ошибки БД, решаем, пропускать пользователя или блокировать. 
            # Безопаснее пропустить, чтобы не заблокировать из-за временной проблемы с БД.
            print(f"DEBUG BanCheck: Proceeding user {user_id} due to DB error during ban check.")
            return await handler(event, data)
        
        if is_banned_in_db:
            print(f"DEBUG BanCheck: User {user_id} IS BANNED. Denying access.")
            if chat_id_to_reply and 'bot' in data: # Убедимся, что bot есть в data
                try:
                    bot_instance: Bot = data['bot'] # Получаем экземпляр Bot
                    await bot_instance.send_message(
                        chat_id=chat_id_to_reply,
                        text="🚫 Ваш доступ к этому боту ограничен администрацией."
                    )
                except Exception as e_send:
                    print(f"DEBUG BanCheck: Error sending ban message to chat {chat_id_to_reply} for user {user_id}: {e_send}")
            
            # Прерываем дальнейшую обработку апдейта, не вызывая следующий handler
            return # Это должно остановить цепочку middleware и хэндлеров
        else:
            print(f"DEBUG BanCheck: User {user_id} is NOT banned. Proceeding with handler.")
            return await handler(event, data)