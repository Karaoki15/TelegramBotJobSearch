# app/services/scheduler_jobs.py
from datetime import datetime, timedelta, timezone
import random
from aiogram import Bot
from sqlalchemy import select, update, or_, and_, delete
import asyncio
from app.db.database import AsyncSessionFactory
from app.db.models import User, UserRole, ApplicantProfile, EmployerProfile
from app.handlers.registration_handlers import is_user_subscribed_to_channel

_user_last_reengagement_indices = {} 

REENGAGEMENT_TEXTS = {
    UserRole.APPLICANT: {
        "inactive_2_days": [ # Список для неактивных соискателей
            "Уже все рилсы пересмотрел? Может пора делом заняться?",
            "Уже все рилсы пересмотрел? А работодатели — нет, тебя ещё ждут.",
            "Около 50 новых вакансий рядом с тобой. Средняя зп - 30к!",
            "НИ В КОЕМ СЛУЧАЕ НЕ ЗАХОДИ В БОТА. Слишком много денег, может не выдержать психика.",
            "А теперь давай притворимся, что тебе не нужны деньги? Ведь так?",
            "НИ В КОЕМ СЛУЧАЕ НЕ ЗАХОДИ В БОТА."
        ],
        "stopped_search_2_days": [ # Список для соискателей, остановивших поиск
            "А теперь давай притворимся, что тебе не нужны деньги? Ведь так?",
            "Уже все рилсы пересмотрел? Может пора делом заняться?",
            "Уже все рилсы пересмотрел? А работодатели — нет, тебя ещё ждут.",
            "Около 50 новых вакансий рядом с тобой. Средняя зп - 30к!",
            "НИ В КОЕМ СЛУЧАЕ НЕ ЗАХОДИ В БОТА. Слишком много денег, может не выдержать психика.",
            "НИ В КОЕМ СЛУЧАЕ НЕ ЗАХОДИ В БОТА."
        ]
    },
    UserRole.EMPLOYER: {
        "inactive_2_days": [ # Список для неактивных работодателей
            "Сетку просматривают тысячи соискателей в день. Обновите вакансию — и вас точно заметят",
            "Напоминаем: в вашей анкете могут быть отклики. Рекомендуем проверить",
            "Не забудьте проверить отклики. Кто-то мог уже откликнуться на вашу вакансию"
        ],
        "stopped_search_2_days": [ # Список для работодателей, остановивших поиск
            "Сетку просматривают тысячи соискателей в день. Обновите вакансию — и вас точно заметят",
            "Напоминаем: в вашей анкете могут быть отклики. Рекомендуем проверить",
            "Не забудьте проверить отклики. Кто-то мог уже откликнуться на вашу вакансию"
        ]
    }
}



# Интервалы для проверки и отправки (можно вынести в конфиг)
DAYS_FOR_INACTIVITY_CHECK = 2
DAYS_FOR_DEACTIVATION_CHECK = 2
DAYS_BETWEEN_REENGAGEMENT_NOTIFS = 7 

DAYS_FOR_EMPLOYER_INACTIVITY_CHECK = 4
DAYS_FOR_EMPLOYER_DEACTIVATION_CHECK = 4


async def send_reengagement_notification(bot: Bot, user: User, reason_key: str):
    global _user_last_reengagement_indices # Объявляем, что используем глобальную переменную

    if not user.role: 
        print(f"DEBUG send_reengagement: No role for user {user.telegram_id}, skipping.")
        return False
    
    texts_for_role_map = REENGAGEMENT_TEXTS.get(user.role)
    if not texts_for_role_map:
        print(f"DEBUG send_reengagement: No texts for role {user.role.name} for user {user.telegram_id}.")
        return False

    list_of_texts = texts_for_role_map.get(reason_key)
    if not list_of_texts or not isinstance(list_of_texts, list) or len(list_of_texts) == 0:
        print(f"DEBUG send_reengagement: No/empty list of texts for reason '{reason_key}', role {user.role.name} for user {user.telegram_id}.")
        return False

    history_key = (user.telegram_id, f"{reason_key}_{user.role.name}")
    
    last_index_sent = _user_last_reengagement_indices.get(history_key, -1) 
    current_index_to_send = (last_index_sent + 1) % len(list_of_texts)
    text_to_send = list_of_texts[current_index_to_send]
    
    try:
        await bot.send_message(user.telegram_id, text_to_send)
        print(f"Sent re-engagement (reason: {reason_key}, index: {current_index_to_send}, text: '{text_to_send[:50]}...') to user {user.telegram_id}")
        
        _user_last_reengagement_indices[history_key] = current_index_to_send
        
        async with AsyncSessionFactory() as session, session.begin():
            await session.execute(
                update(User)
                .where(User.telegram_id == user.telegram_id)
                .values(last_reengagement_notif_sent_at=datetime.now(timezone.utc))
            )
        return True
    except Exception as e:
        print(f"Failed to send re-engagement to user {user.telegram_id}: {e}")
        # import traceback # Можно добавить для детальной ошибки отправки
        # traceback.print_exc()
        return False


async def check_and_send_reengagement_notifications(bot: Bot):
    print(f"SCHEDULER JOB: Running at {datetime.now(timezone.utc)}")
    now = datetime.now(timezone.utc)
    users_to_notify_info = []

    
    # Для теста установим очень короткие интервалы, чтобы было легче поймать
    deactivation_check_time = now - timedelta(days=DAYS_FOR_INACTIVITY_CHECK) # Пользователи, деактивировавшие профиль 1 мин назад
    inactivity_check_time = now - timedelta(days=DAYS_FOR_DEACTIVATION_CHECK)   # Пользователи, неактивные 2 мин
    min_interval_notif_time = now - timedelta(days=DAYS_BETWEEN_REENGAGEMENT_NOTIFS) # Последнее уведомление было более 30 сек назад
    
    emp_deactivation_check_time = now - timedelta(days=DAYS_FOR_EMPLOYER_DEACTIVATION_CHECK) # Работодатели, деактивировавшие профиль 1 мин назад
    emp_inactivity_check_time = now - timedelta(days=DAYS_FOR_EMPLOYER_INACTIVITY_CHECK)   # Работодатели, неактивные 2 мин

    users_to_notify_info = [] # Будем хранить {'user_id': id, 'reason': reason}

    async with AsyncSessionFactory() as session, session.begin():
        print("SCHEDULER JOB: Checking for users who stopped search...")
        
        # 1. Пользователи, остановившие поиск
        # Соискатели
        stopped_search_applicants_q = await session.execute(
            select(User).join(
                ApplicantProfile, 
                User.telegram_id == ApplicantProfile.user_id 
            ).where(
                ApplicantProfile.is_active == False,
                ApplicantProfile.deactivation_date >= deactivation_check_time - timedelta(seconds=30),
                ApplicantProfile.deactivation_date <= deactivation_check_time + timedelta(seconds=30),
                or_(User.last_reengagement_notif_sent_at == None, User.last_reengagement_notif_sent_at < min_interval_notif_time)
            )
        )
        for user in stopped_search_applicants_q.scalars().all():
            users_to_notify_info.append({"user_id": user.telegram_id, "user_role": user.role, "reason": "stopped_search"})
        print(f"SCHEDULER JOB: Found {len(users_to_notify_info)} users who stopped applicant search.")

        # (Аналогично для stopped_search_employers_q, обновляя users_to_notify_info)
        stopped_search_employers_q = await session.execute(
            select(User).join(
                EmployerProfile, 
                User.telegram_id == EmployerProfile.user_id 
            ).where(
                EmployerProfile.is_active == False,
                EmployerProfile.deactivation_date >= emp_deactivation_check_time - timedelta(seconds=30),
                EmployerProfile.deactivation_date <= emp_deactivation_check_time + timedelta(seconds=30),
                or_(User.last_reengagement_notif_sent_at == None, User.last_reengagement_notif_sent_at < min_interval_notif_time)
            )
        )
        current_count = len(users_to_notify_info)
        for user in stopped_search_employers_q.scalars().all():
             if not any(u["user_id"] == user.telegram_id for u in users_to_notify_info): # Избегаем дублирования
                users_to_notify_info.append({"user_id": user.telegram_id, "user_role": user.role, "reason": "stopped_search"})
        print(f"SCHEDULER JOB: Found {len(users_to_notify_info) - current_count} users who stopped employer search. Total now: {len(users_to_notify_info)}")


        print("SCHEDULER JOB: Checking for inactive users with active profiles...")
        
        # Пример для неактивных соискателей
        print("SCHEDULER JOB: Checking for inactive users with active profiles...")
        # 2. Неактивные пользователи с активными профилями
        # Соискатели
        inactive_applicants_q = await session.execute(
            select(User).join(
                ApplicantProfile,
                User.telegram_id == ApplicantProfile.user_id 
            ).where(
                User.last_activity_date < inactivity_check_time,
                User.role == UserRole.APPLICANT,
                ApplicantProfile.is_active == True,
                or_(User.last_reengagement_notif_sent_at == None, User.last_reengagement_notif_sent_at < min_interval_notif_time)
            )
        )
        current_count = len(users_to_notify_info)
        for user in inactive_applicants_q.scalars().all():
            if not any(u["user_id"] == user.telegram_id for u in users_to_notify_info):
                users_to_notify_info.append({"user_id": user.telegram_id, "user_role": user.role, "reason": "inactive"})
        print(f"SCHEDULER JOB: Found {len(users_to_notify_info) - current_count} inactive applicants. Total now: {len(users_to_notify_info)}")
        
        # (Аналогично для inactive_employers_q)
        inactive_employers_q = await session.execute(
            select(User).join(
                EmployerProfile,
                User.telegram_id == EmployerProfile.user_id 
            ).where(
                User.last_activity_date < emp_inactivity_check_time,
                User.role == UserRole.EMPLOYER,
                EmployerProfile.is_active == True,
                or_(User.last_reengagement_notif_sent_at == None, User.last_reengagement_notif_sent_at < min_interval_notif_time)
            )
        )
        current_count = len(users_to_notify_info)
        for user in inactive_employers_q.scalars().all():
            if not any(u["user_id"] == user.telegram_id for u in users_to_notify_info):
                 users_to_notify_info.append({"user_id": user.telegram_id, "user_role": user.role, "reason": "inactive"})
        print(f"SCHEDULER JOB: Found {len(users_to_notify_info) - current_count} inactive employers. Total now: {len(users_to_notify_info)}")

    # Отправляем уведомления вне сессии БД
    sent_count = 0
    if users_to_notify_info:
        print(f"SCHEDULER JOB: Total users to notify: {len(users_to_notify_info)}")
        for item_info in users_to_notify_info:
            async with AsyncSessionFactory() as session_send, session_send.begin():
                user_to_send = await session_send.get(User, item_info["user_id"])
            
            if user_to_send:
                reason_key_for_text = f"{item_info['reason']}_2_days" # Формируем ключ для REENGAGEMENT_TEXTS
                if await send_reengagement_notification(bot, user_to_send, reason_key_for_text):
                    sent_count +=1
            else:
                print(f"SCHEDULER JOB: Could not retrieve User object for ID {item_info['user_id']} for sending notification.")
    print(f"SCHEDULER JOB: Re-engagement notifications attempt finished. Sent to {sent_count} users.")
    
    
# --- НОВАЯ ЗАДАЧА ДЛЯ ПРОВЕРКИ ПОДПИСОК ---

async def daily_check_employers_subscription(bot: Bot):
    print(f"SCHEDULER: Running daily employer subscription check at {datetime.now(timezone.utc)}")
    unsubscribed_user_ids = []

    async with AsyncSessionFactory() as session:
        # Получаем всех пользователей с ролью Работодатель и активным профилем
        result = await session.execute(
            select(User).join(
                EmployerProfile, User.telegram_id == EmployerProfile.user_id # <-- УТОЧНЯЕМ УСЛОВИЕ JOIN
            ).where(
                User.role == UserRole.EMPLOYER,
                EmployerProfile.is_active == True
            )
        )
        employers = result.scalars().all()
        
        if not employers:
            print("SCHEDULER: Subscription check finished. No active employers found.")
            return

        print(f"SCHEDULER: Found {len(employers)} active employers to check.")

        for employer in employers:
            try:
                is_subscribed = await is_user_subscribed_to_channel(employer.telegram_id, bot)
                if not is_subscribed:
                    unsubscribed_user_ids.append(employer.telegram_id)
                await asyncio.sleep(0.1) 
            except Exception as e:
                print(f"SCHEDULER: Error checking user {employer.telegram_id}: {e}")

    if not unsubscribed_user_ids:
        print("SCHEDULER: Daily subscription check finished. All active employers are subscribed.")
        return

    print(f"SCHEDULER: Found {len(unsubscribed_user_ids)} unsubscribed employers. Deleting their profiles...")

    async with AsyncSessionFactory() as session, session.begin():
        for user_id in unsubscribed_user_ids:
            try:
                # Удаляем анкету и сбрасываем роль
                await session.execute(delete(EmployerProfile).where(EmployerProfile.user_id == user_id))
                await session.execute(update(User).where(User.telegram_id == user_id).values(role=None))
                
                # Отправляем уведомление
                await bot.send_message(
                    chat_id=user_id,
                    text="Вы не подписаны на канал, оформите подписку и тогда вы снова сможете получить доступ к боту"
                )
                print(f"SCHEDULER: Profile for user {user_id} deleted and notification sent.")
            except Exception as e:
                print(f"SCHEDULER: Could not process/notify user {user_id}. Maybe bot is blocked. Error: {e}")
            
            await asyncio.sleep(0.1)
    
    print("SCHEDULER: Daily subscription check and processing of unsubscribed employers finished.")
