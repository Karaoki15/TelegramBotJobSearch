# app/handlers/browsing_handlers.py
import random
from aiogram import Router, F, types, Dispatcher
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, ReplyKeyboardRemove, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram import Bot

from app.db.database import AsyncSessionFactory
from app.db.models import EmployerProfile, User, Complaint, ComplaintStatusEnum, ApplicantProfile, MotivationalContentTypeEnum, MotivationalContent
from sqlalchemy import select, func as sqlalchemy_func, update

from app.db.models import ApplicantEmployerInteraction, InteractionTypeEnum
from datetime import datetime, timedelta, timezone 
import traceback
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import StateFilter
from app.states.browsing_states import ApplicantBrowsingStates
from app.keyboards.reply_keyboards import applicant_action_keyboard, continue_browsing_after_motivation_keyboard, cancel_question_input_keyboard
from aiogram.exceptions import TelegramAPIError

from app.config import MOTIVATION_THRESHOLD


browsing_router = Router()





# Вспомогательная функция для форматирования анкеты работодателя
def format_employer_profile_for_applicant(profile: EmployerProfile) -> str: # Убрали employer_user, пока не нужен
    work_format_display = getattr(profile.work_format, 'name', "Не указан").title()
    min_age_display = profile.min_age_candidate if profile.min_age_candidate is not None else "Не указан"
    
    text = (
        f"<b>{profile.company_name}</b>\n"
        f"Город: {profile.city}\n"
        f"Вакансия: <b>{profile.position}</b>\n"
        f"Зарплата: {profile.salary}\n"
        f"Минимальный возраст: {min_age_display}\n"
        f"Формат работы: {work_format_display}\n\n"
        f"<i>О компании/вакансии:</i>\n{profile.description}\n"
    )
    return text


async def get_bot_setting_from_browsing(session, key: str) -> str | None: 
    from app.db.models import BotSettings 
    from sqlalchemy import select
    result = await session.execute(select(BotSettings.value_str).where(BotSettings.setting_key == key))
    return result.scalar_one_or_none()

async def show_antispam_dummy(message: Message, state: FSMContext):
    default_antispam_text = ("Ваша активность слишком высока. Пожалуйста, воздержитесь от частых действий.\n"
                             "Вам временно будут показаны информационные сообщения.")
    antispam_text_to_show = default_antispam_text
    antispam_photo_id_to_show = None

    try:
        async with AsyncSessionFactory() as session, session.begin():
            db_text = await get_bot_setting_from_browsing(session, "antispam_dummy_text") 
            if db_text:
                antispam_text_to_show = db_text
            
            db_photo_id = await get_bot_setting_from_browsing(session, "antispam_dummy_photo_id")
            if db_photo_id:
                antispam_photo_id_to_show = db_photo_id
    except Exception as e:
        print(f"ERROR: Could not fetch antispam dummy settings from DB: {e}")
        # В случае ошибки используем значения по умолчанию

    await state.update_data(current_shown_employer_profile_id=-1) # Флаг, что это пустышка
    
    print(f"DEBUG: Showing antispam dummy. Photo ID: {antispam_photo_id_to_show}, Text: {antispam_text_to_show[:50]}...")

    if antispam_photo_id_to_show:
        try:
            await message.bot.send_photo(
                chat_id=message.from_user.id,
                photo=antispam_photo_id_to_show,
                caption=antispam_text_to_show,
                reply_markup=applicant_action_keyboard # С обычными кнопками
            )
        except Exception as e_photo:
            print(f"ERROR sending antispam dummy photo: {e_photo}. Sending text only.")
            # Если не удалось отправить фото, отправляем только текст
            await message.answer(antispam_text_to_show, reply_markup=applicant_action_keyboard)
    else:
        await message.answer(antispam_text_to_show, reply_markup=applicant_action_keyboard)

# Основная функция показа анкет
async def show_next_employer_profile(message: Message, user_id: int, state: FSMContext):
    data = await state.get_data()
    in_antispam_mode = data.get("in_antispam_mode", False)
    antispam_mode_until = data.get("antispam_mode_until")
    current_time_for_all_checks = datetime.now(timezone.utc)

    # 1. Проверка анти-спам режима
    if in_antispam_mode and antispam_mode_until and current_time_for_all_checks < antispam_mode_until:
        print(f"DEBUG: User {user_id} is in antispam mode. Showing dummy. Until: {antispam_mode_until}")
        await show_antispam_dummy(message, state)
        return 

    if in_antispam_mode and antispam_mode_until and current_time_for_all_checks >= antispam_mode_until:
        print(f"DEBUG: User {user_id} antispam mode HAS ENDED.")
        await state.update_data(in_antispam_mode=False, antispam_mode_until=None, recent_actions_timestamps=[])
    
    # 2. Логика получения реальной или пустышкиной анкеты работодателя
    employer_profile_to_show: EmployerProfile | None = None
    async with AsyncSessionFactory() as session, session.begin():
        applicant_profile_q = await session.execute(select(ApplicantProfile).where(ApplicantProfile.user_id == user_id))
        applicant_profile_obj = applicant_profile_q.scalar_one_or_none()
        
        applicant_city = None
        if applicant_profile_obj and applicant_profile_obj.city:
            applicant_city = applicant_profile_obj.city.strip().lower()
        
        subquery_cooled_down_profiles = (
            select(ApplicantEmployerInteraction.employer_profile_id).where(
                ApplicantEmployerInteraction.applicant_user_id == user_id,
                ApplicantEmployerInteraction.cooldown_until > current_time_for_all_checks
            ).distinct()
        ).scalar_subquery()

        # Поиск реальных анкет
        if applicant_city:
            query_in_city = (select(EmployerProfile).where(
                EmployerProfile.is_active == True, EmployerProfile.is_dummy == False,
                sqlalchemy_func.lower(EmployerProfile.city) == applicant_city,
                EmployerProfile.id.notin_(subquery_cooled_down_profiles)
            ).order_by(sqlalchemy_func.random()).limit(1))
            employer_profile_to_show = (await session.execute(query_in_city)).scalar_one_or_none()

        if not employer_profile_to_show:
            conditions_other = [
                EmployerProfile.is_active == True, EmployerProfile.is_dummy == False,
                EmployerProfile.id.notin_(subquery_cooled_down_profiles)
            ]
            if applicant_city: conditions_other.append(sqlalchemy_func.lower(EmployerProfile.city) != applicant_city)
            query_other_cities = (select(EmployerProfile).where(*conditions_other)
                                  .order_by(sqlalchemy_func.random()).limit(1))
            employer_profile_to_show = (await session.execute(query_other_cities)).scalar_one_or_none()
        
        # Если реальных нет, ищем пустышки (is_dummy=True)
        if not employer_profile_to_show:
            print(f"DEBUG: No real profiles found for user {user_id}. Looking for dummy profiles.")
            dummy_conditions = [
                EmployerProfile.is_active == True, EmployerProfile.is_dummy == True,
                EmployerProfile.id.notin_(subquery_cooled_down_profiles)
            ]
            if applicant_city: # Приоритет пустышек по городу соискателя
                 dummy_conditions.append(sqlalchemy_func.lower(EmployerProfile.city) == applicant_city)
            
            query_dummies_city = (select(EmployerProfile).where(*dummy_conditions)
                                  .order_by(sqlalchemy_func.random()).limit(1))
            employer_profile_to_show = (await session.execute(query_dummies_city)).scalar_one_or_none()

            if not employer_profile_to_show and applicant_city: # Если в городе нет, ищем пустышки в других городах
                dummy_conditions_other_city = [
                    EmployerProfile.is_active == True, EmployerProfile.is_dummy == True,
                    EmployerProfile.id.notin_(subquery_cooled_down_profiles),
                    sqlalchemy_func.lower(EmployerProfile.city) != applicant_city
                ]
                query_dummies_other = (select(EmployerProfile).where(*dummy_conditions_other_city)
                                       .order_by(sqlalchemy_func.random()).limit(1))
                employer_profile_to_show = (await session.execute(query_dummies_other)).scalar_one_or_none()
            
            if employer_profile_to_show:
                print(f"DEBUG: Found DUMMY profile to show: ID {employer_profile_to_show.id}")

        # 3. Показ анкеты или сообщения "нет анкет"
        if employer_profile_to_show:
            current_session_views = data.get("session_view_count_for_motivation", 0) + 1
            
            if current_session_views >= MOTIVATION_THRESHOLD:
                await state.update_data(session_view_count_for_motivation=0) 
                print(f"DEBUG: Motivational content TRIGGERED for user {user_id} after {current_session_views-1} views.")
                motivation_was_sent = await send_random_motivational_content(message, state)
                if motivation_was_sent: # Если мотивация успешно показана (и ждем "Продолжить")
                    return # Выходим, не показываем анкету работодателя сейчас
            else:
                await state.update_data(session_view_count_for_motivation=current_session_views)
            # --- Конец блока мотивации ---

            # Если мотивация не была показана (или не должна была), показываем анкету работодателя
            profile_text = format_employer_profile_for_applicant(employer_profile_to_show)
            await state.update_data(
                current_shown_employer_profile_id=employer_profile_to_show.id,
                current_shown_employer_user_id=employer_profile_to_show.user_id
            )
            if employer_profile_to_show.photo_file_id:
                try:
                    await message.bot.send_photo(chat_id=user_id, photo=employer_profile_to_show.photo_file_id,
                                                 caption=profile_text, parse_mode="HTML", reply_markup=applicant_action_keyboard)
                except Exception as e_photo_send:
                    print(f"Error sending employer profile photo: {e_photo_send}")
                    await message.answer(profile_text, parse_mode="HTML", reply_markup=applicant_action_keyboard)
            else:
                await message.answer(profile_text, parse_mode="HTML", reply_markup=applicant_action_keyboard)
        else: 
            await message.answer("На данный момент подходящих анкет нет. Попробуйте зайти позже!", reply_markup=ReplyKeyboardRemove())
            await state.clear() # Очищаем состояние просмотра
            from app.handlers.settings_handlers import show_applicant_settings_menu
            display_name_for_menu = message.from_user.first_name
            user_for_menu = await session.get(User, user_id)
            if user_for_menu and user_for_menu.first_name:
                display_name_for_menu = user_for_menu.first_name
            await show_applicant_settings_menu(message, user_id, display_name_for_menu)




# Хэндлер для "⏹️ Остановить показ"
@browsing_router.message(F.text == "⏹️ Остановить показ") 
async def stop_browsing_profiles(message: Message, state: FSMContext):
    user_id = message.from_user.id
    
    # --- ПРОВЕРКА, СУЩЕСТВУЕТ ЛИ ЕЩЕ АКТИВНАЯ АНКЕТА СОИСКАТЕЛЯ ---
    async with AsyncSessionFactory() as session_check_profile, session_check_profile.begin():
        applicant_profile_exists = (await session_check_profile.execute(
            select(ApplicantProfile.id) # Просто проверяем наличие
            .where(ApplicantProfile.user_id == user_id, ApplicantProfile.is_active == True) 
            # user_id_who_interacted - это ID соискателя в текущем хэндлере
        )).scalar_one_or_none()

    if not applicant_profile_exists:
        await state.clear()
        await message.answer(
            "Ваша анкета была изменена или удалена администратором. "
            "Просмотр остановлен. Пожалуйста, начните сначала.",
            reply_markup=ReplyKeyboardRemove()
        )
        from app.bot import start_keyboard 
        await message.answer("Выберите вашу роль:", reply_markup=start_keyboard)
        return 
    # Очищаем данные FSM, связанные с просмотром
    await state.update_data(current_shown_employer_profile_id=None, current_shown_employer_user_id=None, last_shown_employer_profile_id=None)
 
    await message.answer("Показ анкет остановлен.", reply_markup=ReplyKeyboardRemove())
    
    from app.handlers.settings_handlers import show_applicant_settings_menu
    
    display_name = message.from_user.first_name
    async with AsyncSessionFactory() as new_session, new_session.begin(): # Новая сессия для получения имени
        user = await new_session.get(User, user_id)
        if user and user.first_name:
            display_name = user.first_name
    await show_applicant_settings_menu(message, user_id, display_name)
    
@browsing_router.message(F.text == "👎")
async def process_dislike_employer(message: Message, state: FSMContext):
    user_id = message.from_user.id # ID соискателя
    
    # --- ПРОВЕРКА, СУЩЕСТВУЕТ ЛИ ЕЩЕ АКТИВНАЯ АНКЕТА СОИСКАТЕЛЯ ---
    async with AsyncSessionFactory() as session_check_profile, session_check_profile.begin():
        applicant_profile_exists = (await session_check_profile.execute(
            select(ApplicantProfile.id) # Просто проверяем наличие
            .where(ApplicantProfile.user_id == user_id, ApplicantProfile.is_active == True) 
            # user_id_who_interacted - это ID соискателя в текущем хэндлере
        )).scalar_one_or_none()

    if not applicant_profile_exists:
        await state.clear() 
        await message.answer(
            "Ваша анкета была изменена или удалена администратором. "
            "Просмотр остановлен. Пожалуйста, начните сначала.",
            reply_markup=ReplyKeyboardRemove()
        )
        from app.bot import start_keyboard
        await message.answer("Выберите вашу роль:", reply_markup=start_keyboard)
        return 
    
    # <<<--- НАЧАЛО ОБЩЕГО АНТИ-СПАМ БЛОКА ---<<<
    current_data_fsm = await state.get_data()

    
    # 1. Проверяем, не на анти-спам пустышке ли мы сейчас
    if current_data_fsm.get("current_shown_employer_profile_id") == -1:
        print(f"DEBUG: User {user_id} interacted with ANTISPAM DUMMY via '{message.text}'.")
        
        # Проверяем, активен ли еще "бан" анти-спама
        is_still_in_antispam_ban = current_data_fsm.get("in_antispam_mode", False)
        antispam_ban_ends_at = current_data_fsm.get("antispam_mode_until")
        now_utc = datetime.now(timezone.utc)

        if is_still_in_antispam_ban and antispam_ban_ends_at and now_utc < antispam_ban_ends_at:
            await show_antispam_dummy(message, state) 
        else: # Бан истек или его не было (но мы на пустышке - это странно, но очистим)
            if is_still_in_antispam_ban: # Сбрасываем флаги, если они еще стоят
                 await state.update_data(in_antispam_mode=False, antispam_mode_until=None, recent_actions_timestamps=[])
                 print(f"DEBUG: Antispam ban for user {user_id} ended upon interaction with dummy.")
            await message.answer("Период информационных сообщений закончился. Попробуем найти следующую анкету.", 
                                 reply_markup=applicant_action_keyboard) # Возвращаем кнопки просмотра
            await show_next_employer_profile(message, user_id, state) # Показываем реальную
        return 
    # --- КОНЕЦ БЛОКА 1 ---

    # --- БЛОК 2: ТРИГГЕР АНТИ-СПАМА (если это было взаимодействие с реальной анкетой) ---
  
    recent_actions_timestamps = current_data_fsm.get("recent_actions_timestamps", [])
    current_action_time = datetime.now(timezone.utc)
    recent_actions_timestamps.append(current_action_time)
    MAX_RECENT_ACTIONS_TO_TRACK = 10; ACTION_LIMIT_FOR_ANTISPAM = 10; TIME_WINDOW_SECONDS_FOR_ANTISPAM = 10 
    if len(recent_actions_timestamps) > MAX_RECENT_ACTIONS_TO_TRACK:
        recent_actions_timestamps = recent_actions_timestamps[-MAX_RECENT_ACTIONS_TO_TRACK:]
    await state.update_data(recent_actions_timestamps=recent_actions_timestamps)
    if len(recent_actions_timestamps) >= ACTION_LIMIT_FOR_ANTISPAM:
        time_diff = recent_actions_timestamps[-1] - recent_actions_timestamps[-ACTION_LIMIT_FOR_ANTISPAM]
        if time_diff.total_seconds() <= TIME_WINDOW_SECONDS_FOR_ANTISPAM:
            print(f"ANTISPAM TRIGGERED for user {user_id} by '{message.text}' action!")
            antispam_duration_minutes = 5 
            antispam_end_time = datetime.now(timezone.utc) + timedelta(minutes=antispam_duration_minutes)
            await state.update_data(in_antispam_mode=True, antispam_mode_until=antispam_end_time, recent_actions_timestamps=[])
            await message.answer(
                f"Ваша активность кажется чрезмерной. Пожалуйста, сделайте перерыв.\n"
                f"В течение следующих {antispam_duration_minutes} минут вам будут показаны информационные сообщения.", 
                reply_markup=applicant_action_keyboard 
            )
            await show_antispam_dummy(message, state) 
            return
    # --- КОНЕЦ БЛОКА 2 ---
    
    # --- ЕСЛИ НЕ ВЫШЛИ ИЗ-ЗА БЛОКА 1 ИЛИ 2 -> ОСНОВНАЯ ЛОГИКА ХЭНДЛЕРА ---
    shown_employer_profile_id = current_data_fsm.get("current_shown_employer_profile_id")

    if not shown_employer_profile_id: 
        await message.answer("Произошла ошибка, не удалось определить анкету. Перезагрузите бота отправьте ему /start.", reply_markup=applicant_action_keyboard)
        return

    try:
        async with AsyncSessionFactory() as session, session.begin():
            cooldown_duration_hours = 0.1 # Кулдаун в часах для дизлайка
            now_utc = datetime.now(timezone.utc) 
            cooldown_end_time_utc = now_utc + timedelta(hours=cooldown_duration_hours)

            new_interaction = ApplicantEmployerInteraction(
                applicant_user_id=user_id,
                employer_profile_id=shown_employer_profile_id,
                interaction_type=InteractionTypeEnum.DISLIKE,
                created_at=now_utc, 
                cooldown_until=cooldown_end_time_utc
            )
            session.add(new_interaction)
            print(f"DEBUG: Dislike recorded. Applicant {user_id} -> EmpProfile {shown_employer_profile_id}. Cooldown until {cooldown_end_time_utc}")

        # Показываем следующую анкету
        await show_next_employer_profile(message, user_id, state)

    except Exception as e:
        print(f"Error processing dislike: {e}\n{traceback.format_exc()}")
        await message.answer("Произошла ошибка при обработке вашего действия. Попробуйте позже.")
        
        from app.handlers.settings_handlers import show_applicant_settings_menu 
        display_name = message.from_user.first_name
        async with AsyncSessionFactory() as session_err, session_err.begin(): # Новая сессия для получения имени
            user_for_menu_err = await session_err.get(User, user_id) # Используем user_id
            if user_for_menu_err and user_for_menu_err.first_name:
                display_name = user_for_menu_err.first_name
        await show_applicant_settings_menu(message, user_id, display_name) # Используем user_id
        

@browsing_router.message(F.text == "❤️")
async def process_like_employer(message: Message, state: FSMContext):
    user_id_from_message = message.from_user.id # ID соискателя
    
    # --- ПРОВЕРКА, СУЩЕСТВУЕТ ЛИ ЕЩЕ АКТИВНАЯ АНКЕТА СОИСКАТЕЛЯ ---
    async with AsyncSessionFactory() as session_check_profile, session_check_profile.begin():
        applicant_profile_exists = (await session_check_profile.execute(
            select(ApplicantProfile.id) # Просто проверяем наличие
            .where(ApplicantProfile.user_id == user_id_from_message, ApplicantProfile.is_active == True) 
            # user_id_who_interacted - это ID соискателя в текущем хэндлере
        )).scalar_one_or_none()

    if not applicant_profile_exists:
        await state.clear() # Очищаем FSM соискателя
        await message.answer(
            "Ваша анкета была изменена или удалена администратором. "
            "Просмотр остановлен. Пожалуйста, начните сначала.",
            reply_markup=ReplyKeyboardRemove()
        )
        from app.bot import start_keyboard # Локальный импорт
        await message.answer("Выберите вашу роль:", reply_markup=start_keyboard)
        return # ВАЖНО: Выходим из хэндлера
    
    # <<<--- НАЧАЛО ОБЩЕГО АНТИ-СПАМ БЛОКА ---<<<
    current_data_fsm = await state.get_data()

    # --- БЛОК 1: Обработка взаимодействия с АНТИ-СПАМ ПУСТЫШКОЙ ---
    if current_data_fsm.get("current_shown_employer_profile_id") == -1:
        print(f"DEBUG: User {user_id_from_message} interacted with ANTISPAM DUMMY via '{message.text}'.")
        
        # Проверяем, активен ли еще "бан" анти-спама
        is_still_in_antispam_ban = current_data_fsm.get("in_antispam_mode", False)
        antispam_ban_ends_at = current_data_fsm.get("antispam_mode_until")
        now_utc = datetime.now(timezone.utc)

        if is_still_in_antispam_ban and antispam_ban_ends_at and now_utc < antispam_ban_ends_at:
            await show_antispam_dummy(message, state) # Снова показываем пустышку
        else: # Бан истек или его не было (но мы на пустышке - это странно, но очистим)
            if is_still_in_antispam_ban: # Сбрасываем флаги, если они еще стоят
                 await state.update_data(in_antispam_mode=False, antispam_mode_until=None, recent_actions_timestamps=[])
                 print(f"DEBUG: Antispam ban for user {user_id_from_message} ended upon interaction with dummy.")
            await message.answer("Период информационных сообщений закончился. Попробуем найти следующую анкету.", 
                                 reply_markup=applicant_action_keyboard) # Возвращаем кнопки просмотра
            await show_next_employer_profile(message, user_id_from_message, state) # Показываем реальную
        return # Важно: ВЫХОДИМ из хэндлера, не обрабатываем дальше
    # --- КОНЕЦ БЛОКА 1 ---

    # --- БЛОК 2: ТРИГГЕР АНТИ-СПАМА (если это было взаимодействие с реальной анкетой) ---

    recent_actions_timestamps = current_data_fsm.get("recent_actions_timestamps", [])

    current_action_time = datetime.now(timezone.utc)
    recent_actions_timestamps.append(current_action_time)
    MAX_RECENT_ACTIONS_TO_TRACK = 10; ACTION_LIMIT_FOR_ANTISPAM = 10; TIME_WINDOW_SECONDS_FOR_ANTISPAM = 10 
    if len(recent_actions_timestamps) > MAX_RECENT_ACTIONS_TO_TRACK:
        recent_actions_timestamps = recent_actions_timestamps[-MAX_RECENT_ACTIONS_TO_TRACK:]
    await state.update_data(recent_actions_timestamps=recent_actions_timestamps)
    if len(recent_actions_timestamps) >= ACTION_LIMIT_FOR_ANTISPAM:
        time_diff = recent_actions_timestamps[-1] - recent_actions_timestamps[-ACTION_LIMIT_FOR_ANTISPAM]
        if time_diff.total_seconds() <= TIME_WINDOW_SECONDS_FOR_ANTISPAM:
            print(f"ANTISPAM TRIGGERED for user {user_id_from_message} by '{message.text}' action!")
            antispam_duration_minutes = 5 
            antispam_end_time = datetime.now(timezone.utc) + timedelta(minutes=antispam_duration_minutes)
            await state.update_data(in_antispam_mode=True, antispam_mode_until=antispam_end_time, recent_actions_timestamps=[])
            await message.answer(
                f"Ваша активность кажется чрезмерной. Пожалуйста, сделайте перерыв.\n"
                f"В течение следующих {antispam_duration_minutes} минут вам будут показаны информационные сообщения.", 
                reply_markup=applicant_action_keyboard 
            )
            await show_antispam_dummy(message, state) 
            return
    # --- КОНЕЦ БЛОКА 2 ---
    
    # --- ЕСЛИ НЕ ВЫШЛИ ИЗ-ЗА БЛОКА 1 ИЛИ 2 -> ОСНОВНАЯ ЛОГИКА ХЭНДЛЕРА ---
    shown_employer_profile_id = current_data_fsm.get("current_shown_employer_profile_id")

    target_employer_user_id = current_data_fsm.get("current_shown_employer_user_id") 
    interaction_id_for_push = None 

    if not shown_employer_profile_id: # Эта проверка здесь на всякий случай, но по идее уже не нужна
        await message.answer("Ошибка определения анкеты.", reply_markup=applicant_action_keyboard)
        return


    try:
        async with AsyncSessionFactory() as session, session.begin():
            # Проверяем, есть ли уже активный (непросмотренный) лайк
            existing_like_check = await session.execute(
                select(ApplicantEmployerInteraction).where(
                    ApplicantEmployerInteraction.applicant_user_id == user_id_from_message,
                    ApplicantEmployerInteraction.employer_profile_id == shown_employer_profile_id,
                    ApplicantEmployerInteraction.interaction_type == InteractionTypeEnum.LIKE,
                    ApplicantEmployerInteraction.is_viewed_by_employer == False 
                )
            )
            existing_active_like = existing_like_check.scalar_one_or_none()

            cooldown_duration_hours_like = 0.1 
            cooldown_end_time_utc = datetime.now(timezone.utc) + timedelta(hours=cooldown_duration_hours_like)
            
            if existing_active_like:
                existing_active_like.cooldown_until = cooldown_end_time_utc 
                existing_active_like.created_at = datetime.now(timezone.utc) 
                session.add(existing_active_like)
                await session.flush() # Получаем ID
                interaction_id_for_push = existing_active_like.id
                await message.answer("Вы уже откликались на эту вакансию, и ваш отклик еще не просмотрен. Мы напомнили о вас!")
                print(f"DEBUG: Repeated Like recorded. Applicant {user_id_from_message} -> EmpProfile {shown_employer_profile_id}. Interaction ID: {interaction_id_for_push}")
            else:
                new_interaction = ApplicantEmployerInteraction(
                    applicant_user_id=user_id_from_message, 
                    employer_profile_id=shown_employer_profile_id,
                    interaction_type=InteractionTypeEnum.LIKE, 
                    created_at=datetime.now(timezone.utc),
                    cooldown_until=cooldown_end_time_utc, 
                    is_viewed_by_employer=False
                )
                session.add(new_interaction)
                await session.flush() 
                interaction_id_for_push = new_interaction.id
                await message.answer("Ваш отклик (лайк) отправлен работодателю!")
                print(f"DEBUG: New Like recorded. Applicant {user_id_from_message} -> EmpProfile {shown_employer_profile_id}. Interaction ID: {interaction_id_for_push}")
        # --- Транзакция сохранения лайка здесь завершена и закоммичена ---

        # Отправляем PUSH-уведомление ПОСЛЕ того, как лайк сохранен
        if target_employer_user_id and interaction_id_for_push:
            await send_or_update_employer_notification(
                bot_instance=message.bot, 
                employer_user_id=target_employer_user_id,
                interaction_id=interaction_id_for_push, # Этот ID пока не используется в send_or_update..., но может пригодиться
                interaction_type_text="лайк"
            )
        
        # Показываем следующую анкету соискателю
        await show_next_employer_profile(message, user_id_from_message, state)

    except Exception as e:
        print(f"Error processing like: {e}\n{traceback.format_exc()}")
        await message.answer("Произошла ошибка при отправке вашего отклика. Попробуйте позже.")
        
        from app.handlers.settings_handlers import show_applicant_settings_menu 
        display_name = message.from_user.first_name
        async with AsyncSessionFactory() as session_err, session_err.begin():
            user_for_menu_err = await session_err.get(User, user_id_from_message)
            if user_for_menu_err and user_for_menu_err.first_name:
                display_name = user_for_menu_err.first_name
        await show_applicant_settings_menu(message, user_id_from_message, display_name)
        
        
# Кнопка "❓ Отправить вопрос" - этот хэндлер остается как есть
@browsing_router.message(F.text == "❓ Отправить вопрос")
async def ask_question_to_employer_start(message: Message, state: FSMContext):
    user_id_from_message = message.from_user.id # Для единообразия используем это имя
    current_data_fsm = await state.get_data()
    
    # --- ПРОВЕРКА, СУЩЕСТВУЕТ ЛИ ЕЩЕ АКТИВНАЯ АНКЕТА СОИСКАТЕЛЯ ---
    async with AsyncSessionFactory() as session_check_profile, session_check_profile.begin():
        applicant_profile_exists = (await session_check_profile.execute(
            select(ApplicantProfile.id) # Просто проверяем наличие
            .where(ApplicantProfile.user_id == user_id_from_message, ApplicantProfile.is_active == True) 
            # user_id_who_interacted - это ID соискателя в текущем хэндлере
        )).scalar_one_or_none()

    if not applicant_profile_exists:
        await state.clear() # Очищаем FSM соискателя
        await message.answer(
            "Ваша анкета была изменена или удалена администратором. "
            "Просмотр остановлен. Пожалуйста, начните сначала.",
            reply_markup=ReplyKeyboardRemove()
        )
        from app.bot import start_keyboard # Локальный импорт
        await message.answer("Выберите вашу роль:", reply_markup=start_keyboard)
        return # ВАЖНО: Выходим из хэндлера
    

    # --- БЛОК 1: Обработка взаимодействия с АНТИ-СПАМ ПУСТЫШКОЙ ---
    if current_data_fsm.get("current_shown_employer_profile_id") == -1:
        print(f"DEBUG: User {user_id_from_message} interacted with ANTISPAM DUMMY via '{message.text}'.")
        
        # Проверяем, активен ли еще "бан" анти-спама
        is_still_in_antispam_ban = current_data_fsm.get("in_antispam_mode", False)
        antispam_ban_ends_at = current_data_fsm.get("antispam_mode_until")
        now_utc = datetime.now(timezone.utc)

        if is_still_in_antispam_ban and antispam_ban_ends_at and now_utc < antispam_ban_ends_at:
            await show_antispam_dummy(message, state) # Снова показываем пустышку
        else: # Бан истек или его не было (но мы на пустышке - это странно, но очистим)
            if is_still_in_antispam_ban: # Сбрасываем флаги, если они еще стоят
                 await state.update_data(in_antispam_mode=False, antispam_mode_until=None, recent_actions_timestamps=[])
                 print(f"DEBUG: Antispam ban for user {user_id_from_message} ended upon interaction with dummy.")
            await message.answer("Период информационных сообщений закончился. Попробуем найти следующую анкету.", 
                                 reply_markup=applicant_action_keyboard) # Возвращаем кнопки просмотра
            await show_next_employer_profile(message, user_id_from_message, state) # Показываем реальную
        return # Важно: ВЫХОДИМ из хэндлера, не обрабатываем дальше
    # --- КОНЕЦ БЛОКА 1 ---

    # --- БЛОК 2: ТРИГГЕР АНТИ-СПАМА (если это было взаимодействие с реальной анкетой) ---

    recent_actions_timestamps = current_data_fsm.get("recent_actions_timestamps", [])

    current_action_time = datetime.now(timezone.utc)
    recent_actions_timestamps.append(current_action_time)
    MAX_RECENT_ACTIONS_TO_TRACK = 10; ACTION_LIMIT_FOR_ANTISPAM = 10; TIME_WINDOW_SECONDS_FOR_ANTISPAM = 10 
    if len(recent_actions_timestamps) > MAX_RECENT_ACTIONS_TO_TRACK:
        recent_actions_timestamps = recent_actions_timestamps[-MAX_RECENT_ACTIONS_TO_TRACK:]
    await state.update_data(recent_actions_timestamps=recent_actions_timestamps)
    if len(recent_actions_timestamps) >= ACTION_LIMIT_FOR_ANTISPAM:
        time_diff = recent_actions_timestamps[-1] - recent_actions_timestamps[-ACTION_LIMIT_FOR_ANTISPAM]
        if time_diff.total_seconds() <= TIME_WINDOW_SECONDS_FOR_ANTISPAM:
            print(f"ANTISPAM TRIGGERED for user {user_id_from_message} by '{message.text}' action!")
            antispam_duration_minutes = 5 
            antispam_end_time = datetime.now(timezone.utc) + timedelta(minutes=antispam_duration_minutes)
            await state.update_data(in_antispam_mode=True, antispam_mode_until=antispam_end_time, recent_actions_timestamps=[])
            await message.answer(
                f"Ваша активность кажется чрезмерной. Пожалуйста, сделайте перерыв.\n"
                f"В течение следующих {antispam_duration_minutes} минут вам будут показаны информационные сообщения.", 
                reply_markup=applicant_action_keyboard 
            )
            await show_antispam_dummy(message, state) 
            return
    # --- КОНЕЦ БЛОКА 2 ---
    
    # --- ЕСЛИ НЕ ВЫШЛИ ИЗ-ЗА БЛОКА 1 ИЛИ 2 -> ОСНОВНАЯ ЛОГИКА ХЭНДЛЕРА ---
    shown_employer_profile_id = current_data_fsm.get("current_shown_employer_profile_id")
    if not shown_employer_profile_id:
        await message.answer("Не могу определить, какой анкете вы хотите задать вопрос.", reply_markup=applicant_action_keyboard)
        return

    await state.update_data(question_target_profile_id=shown_employer_profile_id)
    await state.set_state(ApplicantBrowsingStates.asking_question)
    await message.answer("Введите ваш вопрос для работодателя (до 500 символов).", reply_markup=cancel_question_input_keyboard)

    
    
# Отмена ввода вопроса
@browsing_router.message(ApplicantBrowsingStates.asking_question, F.text == "🚫 Отменить ввод вопроса")
async def cancel_question_input(message: Message, state: FSMContext):
    user_id = message.from_user.id
    current_data = await state.get_data()
    # current_shown_employer_profile_id должен все еще быть в FSM с предыдущего показа анкеты
    shown_employer_profile_id = current_data.get("current_shown_employer_profile_id") 
    # shown_employer_user_id = current_data.get("current_shown_employer_user_id")

    await message.answer("Ввод вопроса отменен.", reply_markup=applicant_action_keyboard) # Возвращаем клавиатуру действий
    
    # Сбрасываем состояние ввода вопроса
    await state.set_state(None) # Или на ApplicantBrowsingStates.viewing_profile
    await state.update_data(question_target_profile_id=None) # Очищаем ID цели вопроса

    
    if shown_employer_profile_id:
        async with AsyncSessionFactory() as session, session.begin():
            employer_profile_to_reshow = await session.get(EmployerProfile, shown_employer_profile_id) # Получаем по PK (id)
            
            if employer_profile_to_reshow and employer_profile_to_reshow.is_active:
                profile_text = format_employer_profile_for_applicant(employer_profile_to_reshow)
                
                # Восстанавливаем данные в FSM, как будто мы ее только что показали
                await state.update_data(
                    current_shown_employer_profile_id=employer_profile_to_reshow.id,
                    current_shown_employer_user_id=employer_profile_to_reshow.user_id
                )

                if employer_profile_to_reshow.photo_file_id:
                    try:
                        await message.bot.send_photo(chat_id=user_id, photo=employer_profile_to_reshow.photo_file_id,
                                                     caption=profile_text, parse_mode="HTML", reply_markup=applicant_action_keyboard)
                    except: # Фоллбэк на текстовое сообщение
                        await message.answer(profile_text, parse_mode="HTML", reply_markup=applicant_action_keyboard)
                else:
                    await message.answer(profile_text, parse_mode="HTML", reply_markup=applicant_action_keyboard)
            else:
                # Если анкета вдруг стала неактивна или удалена, показываем следующую
                await message.answer("Анкета, к которой вы хотели задать вопрос, больше не доступна. Показываю следующую.")
                await show_next_employer_profile(message, user_id, state)
    else:
        # Если не смогли восстановить ID, просто показываем следующую (или меню, если нет анкет)
        await message.answer("Не удалось вернуться к предыдущей анкете. Показываю следующую.")
        await show_next_employer_profile(message, user_id, state)

# Получение текста вопроса от соискателя
@browsing_router.message(ApplicantBrowsingStates.asking_question, F.text)
async def process_question_to_employer(message: Message, state: FSMContext):
    applicant_user_id = message.from_user.id
    applicant_name_for_notif = message.from_user.full_name
    question_text = message.text.strip()

    if question_text == "🚫 Отменить ввод вопроса": # Эта проверка должна быть до валидации длины
        return await cancel_question_input(message, state) # Используем уже существующий cancel_question_input

    if not (5 <= len(question_text) <= 500):
        await message.answer(
            "Текст вопроса должен быть от 5 до 500 символов. Пожалуйста, введите снова или отмените.",
            reply_markup=cancel_question_input_keyboard 
        )
        return

    current_data = await state.get_data()
    target_profile_id = current_data.get("question_target_profile_id")
    target_employer_user_id = current_data.get("current_shown_employer_user_id") 
    interaction_id_for_push = None

    if not target_profile_id:
        await message.answer("Произошла ошибка при определении анкеты. Попробуйте снова.", reply_markup=ReplyKeyboardRemove())
        await state.clear()
        from app.handlers.settings_handlers import show_applicant_settings_menu
        display_name = message.from_user.first_name
        async with AsyncSessionFactory() as session_err, session_err.begin():
            user_err = await session_err.get(User, applicant_user_id)
            if user_err and user_err.first_name: display_name = user_err.first_name
        await show_applicant_settings_menu(message, applicant_user_id, display_name)
        return
    
    if not target_employer_user_id:
        print(f"CRITICAL: target_employer_user_id is None in process_question for profile {target_profile_id}. Notification may not be sent.")
        
    try:
        async with AsyncSessionFactory() as session, session.begin():
            cooldown_duration_hours_question = 0.1
            cooldown_end_time_utc = datetime.now(timezone.utc) + timedelta(hours=cooldown_duration_hours_question)
            new_interaction = ApplicantEmployerInteraction(
                applicant_user_id=applicant_user_id, 
                employer_profile_id=target_profile_id,
                interaction_type=InteractionTypeEnum.QUESTION_SENT, 
                question_text=question_text,
                created_at=datetime.now(timezone.utc), 
                cooldown_until=cooldown_end_time_utc,
                is_viewed_by_employer=False
            )
            session.add(new_interaction)
            await session.flush() 
            interaction_id_for_push = new_interaction.id
            print(f"DEBUG: Question Sent & flushed. Applicant {applicant_user_id} -> EmpProfile {target_profile_id}. Interaction ID: {interaction_id_for_push}")
        # --- Транзакция сохранения вопроса здесь завершена и закоммичена ---

        await message.answer("Ваш вопрос отправлен работодателю!", reply_markup=ReplyKeyboardRemove()) 
        await state.set_state(None) 
        await state.update_data(question_target_profile_id=None) # Очищаем ID цели вопроса
        
        # Показываем следующую анкету соискателю
        await show_next_employer_profile(message, applicant_user_id, state)

        # ОТПРАВЛЯЕМ PUSH УВЕДОМЛЕНИЕ ПОСЛЕ
        if target_employer_user_id and interaction_id_for_push:
            await send_or_update_employer_notification(
                bot_instance=message.bot,
                employer_user_id=target_employer_user_id,
                interaction_id=interaction_id_for_push,
                interaction_type_text="вопрос"
            )
            
    except Exception as e:
        print(f"Error processing question to employer: {e}\n{traceback.format_exc()}")
        await message.answer("Произошла ошибка при отправке вашего вопроса. Попробуйте позже.", reply_markup=ReplyKeyboardRemove())
        await state.clear() 
        from app.handlers.settings_handlers import show_applicant_settings_menu
        display_name = message.from_user.first_name
        async with AsyncSessionFactory() as session_err, session_err.begin():
            user_err = await session_err.get(User, applicant_user_id)
            if user_err and user_err.first_name: display_name = user_err.first_name
        await show_applicant_settings_menu(message, applicant_user_id, display_name)

@browsing_router.message(F.text == "🚩 Жалоба")
async def process_report_employer(message: Message, state: FSMContext):
    user_id_who_reported = message.from_user.id # ID соискателя, который жалуется
    
    # <<<--- НАЧАЛО ОБЩЕГО АНТИ-СПАМ БЛОКА ---<<<
    current_data_fsm = await state.get_data()
    
    # 1. Проверяем, не на анти-спам пустышке ли мы сейчас
    if current_data_fsm.get("current_shown_employer_profile_id") == -1:
        print(f"DEBUG: User {user_id_who_reported} clicked REPORT on antispam dummy.")
        is_still_in_antispam_ban = current_data_fsm.get("in_antispam_mode", False)
        antispam_ban_ends_at = current_data_fsm.get("antispam_mode_until")
        now_utc = datetime.now(timezone.utc)

        if is_still_in_antispam_ban and antispam_ban_ends_at and now_utc < antispam_ban_ends_at:
            await show_antispam_dummy(message, state) 
        else: 
            if is_still_in_antispam_ban:
                 await state.update_data(in_antispam_mode=False, antispam_mode_until=None, recent_actions_timestamps=[])
                 print(f"DEBUG: Antispam ban for user {user_id_who_reported} ended upon interaction with dummy.")
            await message.answer("Период информационных сообщений закончился. Попробуем найти следующую анкету.", 
                                 reply_markup=applicant_action_keyboard) 
            await show_next_employer_profile(message, user_id_who_reported, state) 
        return 
    
    # 2. Анти-спам ТРИГГЕР (если предыдущая анкета была не пустышкой)
    recent_actions_timestamps = current_data_fsm.get("recent_actions_timestamps", [])
    current_action_time = datetime.now(timezone.utc)
    recent_actions_timestamps.append(current_action_time)
    MAX_RECENT_ACTIONS_TO_TRACK = 10  # сколько последних действий мы учитываем
    ACTION_LIMIT_FOR_ANTISPAM = 10    # сколько действий допускается за ограниченное время
    TIME_WINDOW_SECONDS_FOR_ANTISPAM = 10  # это время, за которое эти действия считаются "подозрительными"


    if len(recent_actions_timestamps) > MAX_RECENT_ACTIONS_TO_TRACK:
        recent_actions_timestamps = recent_actions_timestamps[-MAX_RECENT_ACTIONS_TO_TRACK:]
    await state.update_data(recent_actions_timestamps=recent_actions_timestamps)

    if len(recent_actions_timestamps) >= ACTION_LIMIT_FOR_ANTISPAM:
        time_difference = recent_actions_timestamps[-1] - recent_actions_timestamps[-ACTION_LIMIT_FOR_ANTISPAM]
        if time_difference.total_seconds() <= TIME_WINDOW_SECONDS_FOR_ANTISPAM:
            print(f"ANTISPAM TRIGGERED for user {user_id_who_reported} by REPORT action!") # Отладочное сообщение
            antispam_duration_minutes = 5 
            antispam_end_time = datetime.now(timezone.utc) + timedelta(minutes=antispam_duration_minutes)
            await state.update_data(
                in_antispam_mode=True, 
                antispam_mode_until=antispam_end_time,
                recent_actions_timestamps=[] 
            )
            await message.answer(
                f"Ваша активность кажется чрезмерной. Пожалуйста, сделайте перерыв.\n"
                f"В течение следующих {antispam_duration_minutes} минут вам будут показаны информационные сообщения.", 
                reply_markup=applicant_action_keyboard 
            )
            await show_antispam_dummy(message, state) 
            return 
    # >>>--- КОНЕЦ ОБЩЕГО АНТИ-СПАМ БЛОКА ---<<<
    
    # --- Если все проверки пройдены, основная логика жалобы ---
    # current_data_fsm уже получен
    profile_id_being_reported = current_data_fsm.get("current_shown_employer_profile_id")
    user_id_of_profile_owner = current_data_fsm.get("current_shown_employer_user_id") 

    if not profile_id_being_reported: # Проверка, что ID профиля для жалобы есть
        await message.answer("Не удалось определить анкету для жалобы. Попробуйте снова.", reply_markup=applicant_action_keyboard)
        return

    if not user_id_of_profile_owner and profile_id_being_reported:
        async with AsyncSessionFactory() as temp_session, temp_session.begin():
            emp_profile_for_owner_id_q = await temp_session.execute(
                select(EmployerProfile.user_id).where(EmployerProfile.id == profile_id_being_reported)
            )
            user_id_of_profile_owner = emp_profile_for_owner_id_q.scalar_one_or_none()
            print(f"DEBUG: Had to fetch employer_user_id ({user_id_of_profile_owner}) from DB for complaint on profile {profile_id_being_reported}")

    if not user_id_of_profile_owner: # Если так и не смогли определить владельца
        print(f"CRITICAL ERROR: Could not determine owner for employer_profile_id {profile_id_being_reported} to file a complaint.")
        await message.answer("Произошла внутренняя ошибка при оформлении жалобы. Свяжитесь с поддержкой.", reply_markup=applicant_action_keyboard)
        return

    try:
        complaint_obj_to_notify = None # Переменная для хранения объекта жалобы

        async with AsyncSessionFactory() as session, session.begin():
            # 1. Создаем запись о жалобе
            new_complaint = Complaint(
                reporter_user_id=user_id_who_reported,
                reported_employer_profile_id=profile_id_being_reported, 
                reported_user_id=user_id_of_profile_owner,
                reported_applicant_profile_id=None,                     
                status=ComplaintStatusEnum.NEW
            )
            session.add(new_complaint)
            await session.flush()

            
            # 2. Устанавливаем кулдаун
            cooldown_duration_hours_report = 0.1
            cooldown_end_time_utc = datetime.now(timezone.utc) + timedelta(hours=cooldown_duration_hours_report)
            complaint_cooldown_interaction = ApplicantEmployerInteraction(
                applicant_user_id=user_id_who_reported,
                employer_profile_id=profile_id_being_reported,
                interaction_type=InteractionTypeEnum.DISLIKE, 
                cooldown_until=cooldown_end_time_utc,
                created_at=datetime.now(timezone.utc)
            )
            session.add(complaint_cooldown_interaction)
            
            # Получаем ID жалобы после добавления в сессию, но до коммита
            await session.flush() # Это присвоит new_complaint.id
            if new_complaint.id: # Убедимся, что ID есть
                complaint_obj_to_notify = new_complaint # Сохраняем сам объект для передачи
                print(f"DEBUG: Complaint CREATED (ID: {new_complaint.id}) by {user_id_who_reported} on profile {profile_id_being_reported}")
                print(f"DEBUG: Cooldown set for profile {profile_id_being_reported} for user {user_id_who_reported} due to complaint.")
            else:
                print("ERROR: new_complaint.id was not set after flush!")
        
        # --- ДЕЙСТВИЯ ПОСЛЕ УСПЕШНОЙ ТРАНЗАКЦИИ ---
        await message.answer("Спасибо, ваша жалоба принята и будет рассмотрена.", reply_markup=ReplyKeyboardRemove())
        
        # Отправляем уведомление админам, если жалоба была успешно создана и имеет ID
        if complaint_obj_to_notify and complaint_obj_to_notify.id:
            from app.handlers.admin_handlers import notify_admins_about_complaint # Локальный импорт
            try:
                await notify_admins_about_complaint(message.bot, complaint_obj_to_notify)
            except Exception as e_notify:
                print(f"ERROR sending complaint notification to admins: {e_notify}\n{traceback.format_exc()}")
        
        await show_next_employer_profile(message, user_id_who_reported, state)

    except Exception as e:
        print(f"Error processing report: {e}\n{traceback.format_exc()}")
        await message.answer("Произошла ошибка при отправке жалобы. Попробуйте позже.")
        from app.handlers.settings_handlers import show_applicant_settings_menu
        display_name = message.from_user.first_name
        async with AsyncSessionFactory() as session_err, session_err.begin():
            user_err = await session_err.get(User, user_id_who_reported)
            if user_err and user_err.first_name: display_name = user_err.first_name
        await show_applicant_settings_menu(message, user_id_who_reported, display_name) # передаем user_id

        
        
async def send_or_update_employer_notification(
    bot_instance: Bot, 
    employer_user_id: int, 
    interaction_id: int, 
    interaction_type_text: str 
):
    print(f"\n---send_or_update_employer_notification START for employer {employer_user_id}---")

    db_employer_profile_id = None
    db_active_notification_message_id_from_db = None # ID, прочитанный из БД
    new_responses_count_for_text = 0

    # 1. Получить данные из БД
    async with AsyncSessionFactory() as session, session.begin():
        profile_record = (await session.execute(
            select(EmployerProfile) # Выбираем весь объект, чтобы получить и .id, и .user_id
            .where(EmployerProfile.user_id == employer_user_id)
        )).scalar_one_or_none()

        if not profile_record:
            print(f"  DEBUG_PUSH: EXIT - No EmployerProfile record for user_id {employer_user_id}. Cannot send PUSH.")
            return
        
        db_employer_profile_id = profile_record.id # Теперь это точно ID профиля работодателя
        db_active_notification_message_id = profile_record.active_notification_message_id
        print(f"  DEBUG_PUSH: For employer_user_id {employer_user_id}, found profile_id: {db_employer_profile_id}, DB active_notif_msg_id: {db_active_notification_message_id}")

        # Теперь используем db_employer_profile_id для подсчета
        count_result = (await session.execute(
            select(sqlalchemy_func.count(ApplicantEmployerInteraction.id))
            .where(
                ApplicantEmployerInteraction.employer_profile_id == db_employer_profile_id, # Используем правильный ID
                ApplicantEmployerInteraction.is_viewed_by_employer == False,
                ApplicantEmployerInteraction.interaction_type.in_([InteractionTypeEnum.LIKE, InteractionTypeEnum.QUESTION_SENT])
            )
        )).scalar_one()
        new_responses_count_for_text = count_result or 0
        print(f"  DEBUG_PUSH: Calculated new_responses_count: {new_responses_count_for_text} for profile_id {db_employer_profile_id}")

    
    # 2. Если нет откликов, удалить старое уведомление (если было)
    if new_responses_count_for_text == 0:
        if db_active_notification_message_id_from_db:
            try:
                await bot_instance.delete_message(chat_id=employer_user_id, message_id=db_active_notification_message_id_from_db)
                print(f"  DEBUG_PUSH: Deleted stale PUSH (msg_id: {db_active_notification_message_id_from_db}) for employer {employer_user_id} (no new responses).")
            except Exception as e_del:
                print(f"  DEBUG_PUSH: Failed to delete stale PUSH (msg_id: {db_active_notification_message_id_from_db}): {e_del}")
            async with AsyncSessionFactory() as session_cleanup, session_cleanup.begin():
                 await session_cleanup.execute(update(EmployerProfile).where(EmployerProfile.user_id == employer_user_id).values(active_notification_message_id=None))
        print("  DEBUG_PUSH: No new responses, exiting notification function.")
        return

    # 3. Формируем текст и клавиатуру
    now_time_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    notification_text = f"У вас {new_responses_count_for_text} новых откликов! (Последний - {interaction_type_text} в {now_time_str})"
    inline_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"👀 Посмотреть отклики ({new_responses_count_for_text})", callback_data="view_unread_responses_push_btn")]
    ])

    final_message_id_to_store_in_db = None
    
    # 4. Пытаемся отредактировать
    if db_active_notification_message_id_from_db:
        print(f"  DEBUG_PUSH: Attempting to EDIT PUSH msg_id: {db_active_notification_message_id_from_db}")
        try:
            await bot_instance.edit_message_text(
                text=notification_text,
                chat_id=employer_user_id,
                message_id=db_active_notification_message_id_from_db,
                reply_markup=inline_kb
            )
            final_message_id_to_store_in_db = db_active_notification_message_id_from_db
            print(f"  DEBUG_PUSH: Notification EDITED successfully, msg_id: {final_message_id_to_store_in_db}")
        except TelegramAPIError as e_telegram_api: # Ловим специфичные ошибки Aiogram
            print(f"  --- FAILED TO EDIT PUSH (TelegramAPIError on msg_id: {db_active_notification_message_id_from_db}) ---")
            print(f"  REASON: {type(e_telegram_api).__name__} - {e_telegram_api} (message: '{e_telegram_api.message}')")
            # traceback.print_exc() # Можно раскомментировать, если нужно больше деталей
            # Если сообщение не изменено, то ID остается тот же, но мы должны это обработать.
            if "message is not modified" in e_telegram_api.message.lower():
                print("  DEBUG_PUSH: Message was not modified, content is the same. Keeping old msg_id.")
                final_message_id_to_store_in_db = db_active_notification_message_id_from_db # Сохраняем старый ID
            else:
                # Другая ошибка редактирования, обнуляем ID, чтобы отправить новое
                final_message_id_to_store_in_db = None
                async with AsyncSessionFactory() as session_cleanup, session_cleanup.begin():
                    await session_cleanup.execute(update(EmployerProfile).where(EmployerProfile.user_id == employer_user_id).values(active_notification_message_id=None))
                print("  DEBUG_PUSH: active_notification_message_id cleared in DB due to edit failure (not 'not modified').")

        except Exception as e_edit_other: # Ловим все остальные ошибки
            print(f"  --- FAILED TO EDIT PUSH (Other Exception on msg_id: {db_active_notification_message_id_from_db}) ---")
            print(f"  REASON: {type(e_edit_other).__name__} - {e_edit_other}")
            traceback.print_exc()
            print(f"  --- END OTHER EDIT ERROR ---")
            final_message_id_to_store_in_db = None # Обнуляем, чтобы отправить новое
            async with AsyncSessionFactory() as session_cleanup, session_cleanup.begin():
                await session_cleanup.execute(update(EmployerProfile).where(EmployerProfile.user_id == employer_user_id).values(active_notification_message_id=None))
            print("  DEBUG_PUSH: active_notification_message_id cleared in DB due to other edit failure.")

    # 5. Если не было ID для редактирования ИЛИ редактирование не удалось (и final_message_id_to_store_in_db сброшен)
    if not final_message_id_to_store_in_db:
        if db_active_notification_message_id_from_db: # Это ID, которое мы пытались, но не смогли отредактировать
            try:
                await bot_instance.delete_message(chat_id=employer_user_id, message_id=db_active_notification_message_id_from_db)
                print(f"  DEBUG_PUSH: Deleted (because edit failed) old notification (msg_id: {db_active_notification_message_id_from_db}).")
            except Exception as e_del_failed_edit:
                print(f"  DEBUG_PUSH: Failed to delete (because edit failed) old notification (msg_id: {db_active_notification_message_id_from_db}): {e_del_failed_edit}")
        
        print(f"  DEBUG_PUSH: Sending NEW PUSH notification for employer {employer_user_id}.")
        try:
            sent_msg_obj = await bot_instance.send_message(
                chat_id=employer_user_id,
                text=notification_text,
                reply_markup=inline_kb
            )
            final_message_id_to_store_in_db = sent_msg_obj.message_id
            print(f"  DEBUG_PUSH: PUSH Notification SENT (new), msg_id: {final_message_id_to_store_in_db}")
        except Exception as e_send:
            print(f"  ERROR: Could not send NEW PUSH notification to employer {employer_user_id}: {e_send}")
            traceback.print_exc()
            return

    # 6. Сохраняем ID последнего PUSH-сообщения в БД (если он есть)
    if final_message_id_to_store_in_db:
        async with AsyncSessionFactory() as session, session.begin():
            await session.execute(
                update(EmployerProfile)
                .where(EmployerProfile.user_id == employer_user_id)
                .values(active_notification_message_id=final_message_id_to_store_in_db)
            )
            print(f"  DEBUG_PUSH: DB Updated: employer {employer_user_id} active_notification_message_id = {final_message_id_to_store_in_db}")
    print(f"---send_or_update_employer_notification END for employer {employer_user_id}---\n")

async def send_random_motivational_content(message: Message, state: FSMContext) -> bool:
    user_id = message.from_user.id
    selected_content_item: MotivationalContent | None = None

    async with AsyncSessionFactory() as session, session.begin():
        active_motivation_query = (
            select(MotivationalContent)
            .where(MotivationalContent.is_active == True)
            .order_by(sqlalchemy_func.random()).limit(1)
        )
        selected_content_item = (await session.execute(active_motivation_query)).scalar_one_or_none()

    if selected_content_item:
        print(f"DEBUG: Sending motivational content ID {selected_content_item.id} to user {user_id}")
        await state.set_state(ApplicantBrowsingStates.watching_motivation)
        
        # Отправляем "техническое" сообщение для удаления предыдущей Reply клавиатуры
        await message.answer("✨", reply_markup=ReplyKeyboardRemove()) 

        caption = selected_content_item.text_caption
        bot_instance: Bot = message.bot
        try:
            if selected_content_item.content_type == MotivationalContentTypeEnum.VIDEO and selected_content_item.file_id:
                await bot_instance.send_video(chat_id=user_id, video=selected_content_item.file_id, caption=caption[:1024], 
                                              reply_markup=continue_browsing_after_motivation_keyboard, parse_mode="HTML")
            elif selected_content_item.content_type == MotivationalContentTypeEnum.PHOTO and selected_content_item.file_id:
                 await bot_instance.send_photo(chat_id=user_id, photo=selected_content_item.file_id, caption=caption[:1024], 
                                              reply_markup=continue_browsing_after_motivation_keyboard, parse_mode="HTML")
            elif selected_content_item.content_type == MotivationalContentTypeEnum.TEXT_ONLY:
                await bot_instance.send_message(chat_id=user_id, text=caption, 
                                                reply_markup=continue_browsing_after_motivation_keyboard, parse_mode="HTML")
            else: # Неизвестный тип или нет file_id для медиа (но текст есть)
                print(f"WARN: Motivational content ID {selected_content_item.id} - unknown type or missing file_id, sending text only.")
                await bot_instance.send_message(chat_id=user_id, text=caption, 
                                                reply_markup=continue_browsing_after_motivation_keyboard, parse_mode="HTML")
            return True # Контент успешно отправлен (или попытка отправки была)
        except Exception as e:
            print(f"Error sending motivational content (ID: {selected_content_item.id}): {e}\n{traceback.format_exc()}")
            await state.set_state(None) # Сбрасываем состояние, если отправка не удалась
            return False 
    else:
        print(f"DEBUG: No active motivational content found for user {user_id}. Skipping motivational content.")
        return False # Контент не найден/не отправлен


@browsing_router.message(F.text == "▶️ Продолжить просмотр", StateFilter(ApplicantBrowsingStates.watching_motivation))
async def resume_browsing_after_motivation(message: Message, state: FSMContext):
    user_id = message.from_user.id
    await message.answer("Отлично! Ищем дальше...", reply_markup=ReplyKeyboardRemove()) # Убираем кнопку "Продолжить"
    await state.set_state(None) # Сбрасываем состояние просмотра мотивации
    await show_next_employer_profile(message, user_id, state) # Показываем следующую вакансию
    
    
