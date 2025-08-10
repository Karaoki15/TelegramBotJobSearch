# app/handlers/settings_handlers.py
import traceback
from datetime import datetime, timezone
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram import types
from aiogram.types import (
    Message, ReplyKeyboardRemove, ReplyKeyboardMarkup, KeyboardButton, 
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
)
from aiogram.filters import Command, StateFilter

from app.db.database import AsyncSessionFactory
from app.db.models import User, UserRole, ApplicantProfile, EmployerProfile, GenderEnum, WorkFormatEnum, ApplicantEmployerInteraction, InteractionTypeEnum
from sqlalchemy import select, update, delete
from sqlalchemy.sql import func, func as sqlalchemy_func
from app.keyboards.reply_keyboards import start_keyboard

from app.states.editing_states import ApplicantEditProfile, EmployerEditProfile 

from app.handlers.browsing_handlers import show_next_employer_profile

settings_router = Router()

# --- ТЕКСТОВЫЕ КОНСТАНТЫ И КЛАВИАТУРЫ ---

APPLICANT_SETTINGS_MENU_TEXT = "Меню настроек соискателя:"
applicant_settings_keyboard_active = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Продолжить смотреть анкеты")],
        [KeyboardButton(text="Моя анкета")], # Эта кнопка будет вести к редактированию
        [KeyboardButton(text="Заполнить анкету заново")],
        [KeyboardButton(text="Я больше не ищу работу")]
    ], resize_keyboard=True
)
applicant_settings_keyboard_inactive = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Возобновить поиск работы")],
        [KeyboardButton(text="Моя анкета")], # И здесь ведет к редактированию
        [KeyboardButton(text="Заполнить анкету заново")]
    ], resize_keyboard=True
)

EMPLOYER_MAIN_MENU_TEXT = "Главное меню работодателя:"
BTN_VIEW_RESPONSES_TEXT = "Посмотреть отклики"
employer_main_menu_keyboard_active = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Редактировать анкету компании")],
        [KeyboardButton(text="Остановить поиск людей")],
        [KeyboardButton(text="Заполнить анкету заново")],
        #[KeyboardButton(text=BTN_VIEW_RESPONSES_TEXT)]
        # [KeyboardButton(text="Посмотреть отклики")] # Добавим позже
    ], resize_keyboard=True
)
employer_main_menu_keyboard_inactive = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Редактировать анкету компании")],
        [KeyboardButton(text="Возобновить поиск людей")],
        [KeyboardButton(text="Заполнить анкету заново")],
        #[KeyboardButton(text=BTN_VIEW_RESPONSES_TEXT)]
        # [KeyboardButton(text="Посмотреть отклики")] # Добавим позже
    ], resize_keyboard=True
)

# Inline-клавиатура для выбора поля для редактирования анкеты СОИСКАТЕЛЯ
def get_applicant_edit_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="Изменить Город", callback_data="edit_applicant_city")],
        [InlineKeyboardButton(text="Изменить Пол", callback_data="edit_applicant_gender")],
        [InlineKeyboardButton(text="Изменить Возраст", callback_data="edit_applicant_age")],
        [InlineKeyboardButton(text="Изменить Опыт", callback_data="edit_applicant_experience")],
        [InlineKeyboardButton(text="🔙 Назад в меню настроек", callback_data="back_to_applicant_settings")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# Inline-клавиатура для выбора поля для редактирования анкеты РАБОТОДАТЕЛЯ
def get_employer_edit_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="Город", callback_data="edit_employer_city"),
         InlineKeyboardButton(text="Название компании", callback_data="edit_employer_company_name")],
        [InlineKeyboardButton(text="Позиция", callback_data="edit_employer_position"),
         InlineKeyboardButton(text="ЗП", callback_data="edit_employer_salary")],
        [InlineKeyboardButton(text="Мин. возраст", callback_data="edit_employer_min_age"),
         InlineKeyboardButton(text="Формат работы", callback_data="edit_employer_work_format")],
        [InlineKeyboardButton(text="Описание", callback_data="edit_employer_description")], # Было "Описание компании/вакансии"
        [InlineKeyboardButton(text="Фотография", callback_data="edit_employer_photo_router")], # callback_data для маршрутизации к опциям фото
        [InlineKeyboardButton(text="🔙 Назад в меню работодателя", callback_data="back_to_employer_main_menu")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# Reply-кнопка "Отменить изменение поля"
cancel_field_edit_button = KeyboardButton(text="❌ Отменить изменение поля")
cancel_field_edit_keyboard = ReplyKeyboardMarkup(
    keyboard=[[cancel_field_edit_button]], 
    resize_keyboard=True, 
    one_time_keyboard=True # Чтобы она исчезала после нажатия
)

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ОТОБРАЖЕНИЯ МЕНЮ И АНКЕТ ДЛЯ РЕДАКТИРОВАНИЯ ---

async def show_applicant_settings_menu(message_to_reply: Message, user_id_param: int, user_first_name: str = None):
    name_prefix = f"{user_first_name}, " if user_first_name else ""
    current_keyboard = applicant_settings_keyboard_active
    async with AsyncSessionFactory() as session, session.begin():
        profile_is_active_status = (await session.execute(
            select(ApplicantProfile.is_active).where(ApplicantProfile.user_id == user_id_param)
        )).scalar_one_or_none()
        if profile_is_active_status is False: # Профиль есть, но не активен
            current_keyboard = applicant_settings_keyboard_inactive
        elif profile_is_active_status is None: # Профиля нет совсем (хотя сюда не должны попадать, если нет профиля)
             pass # Остается active, но кнопки "продолжить" и "деактивировать" не сработают как надо

    await message_to_reply.answer(
        f"{name_prefix}добро пожаловать в ваше меню настроек!\n{APPLICANT_SETTINGS_MENU_TEXT}",
        reply_markup=current_keyboard
    )
    
# Кнопка "Анкету компании заново"
@settings_router.message(F.text == "Заполнить анкету заново")
async def employer_fill_again_from_settings(message: Message, state: FSMContext):
    user_id = message.from_user.id
    is_employer = False
    async with AsyncSessionFactory() as session, session.begin():
        user = await session.get(User, user_id)
        if user and user.role == UserRole.EMPLOYER:
            is_employer = True
            await session.execute(delete(EmployerProfile).where(EmployerProfile.user_id == user_id))
            await session.execute(update(User).where(User.telegram_id == user_id).values(role=None))
    
    if is_employer:
        await state.clear()
        from app.bot import start_keyboard # Локальный импорт
        await message.answer("Анкета компании удалена. Выберите роль, чтобы начать заново:", reply_markup=start_keyboard)
    else:
        display_name = message.from_user.first_name
        # async with AsyncSessionFactory() as session, session.begin(): ... (если нужно свежее имя)
        await show_employer_main_menu(message, user_id, display_name) # Показываем меню, если это не был работодатель
    
# Кнопка "Заполнить анкету заново"
@settings_router.message(F.text == "Заполнить анкету заново")
async def applicant_fill_again_from_settings(message: Message, state: FSMContext):
    user_id = message.from_user.id
    is_applicant = False
    
    async with AsyncSessionFactory() as session, session.begin():
        user_obj = await session.get(User, user_id)
        if user_obj and user_obj.role == UserRole.APPLICANT:
            is_applicant = True
            await session.execute(delete(ApplicantProfile).where(ApplicantProfile.user_id == user_id))
            await session.execute(update(User).where(User.telegram_id == user_id).values(role=None, contact_phone=user_obj.contact_phone)) # Сохраняем телефон!
    
    if is_applicant:
        await state.clear()
        from app.bot import start_keyboard # Локальный импорт
        await message.answer(
            "Ваша анкета соискателя удалена. Выберите роль, чтобы начать заново:", 
            reply_markup=start_keyboard
        )
    else:
        # Если пользователь как-то нажал эту кнопку, не будучи соискателем, 
        # или произошла ошибка, просто покажем его текущее меню или стартовое.
        # (Это поведение можно уточнить)
        if user_obj and user_obj.role == UserRole.EMPLOYER:
            display_name = user_obj.first_name if user_obj.first_name else message.from_user.first_name
            await show_employer_main_menu(message, user_id, display_name)
        else: # Если нет роли или что-то еще
            from app.bot import start_keyboard
            await message.answer("Действие не определено для вашей текущей роли.", reply_markup=start_keyboard)

async def show_employer_main_menu(message_to_reply_to: Message, user_id_param: int, user_first_name: str = None):
    user_id = user_id_param # ID текущего пользователя (работодателя)
    name_prefix = f"{user_first_name}, " if user_first_name else ""
    
    is_profile_active_for_keyboard = True # По умолчанию считаем, что профиль активен для выбора клавиатуры
    new_responses_count = 0

    async with AsyncSessionFactory() as session, session.begin():
        # Проверяем наличие профиля работодателя и его статус активности
        employer_profile_data = (await session.execute(
            select(EmployerProfile.id, EmployerProfile.is_active) # Нам нужен только ID для подсчета и is_active для клавиатуры
            .where(EmployerProfile.user_id == user_id)
        )).first() # Используем .first() так как ожидаем одну или ноль записей

        if not employer_profile_data:
            # Если профиля работодателя нет, отправляем на начальный выбор роли
            from app.bot import start_keyboard # Локальный импорт
            await message_to_reply_to.answer(
                f"{name_prefix}Анкета вашей компании не создана или не найдена. Пожалуйста, выберите роль:", 
                reply_markup=start_keyboard
            )
            print(f"DEBUG show_employer_main_menu: Employer profile NOT FOUND for user_id {user_id}. Showing start_keyboard.")
            return

        # Профиль найден, получаем его ID и статус активности
        employer_profile_id_for_count = employer_profile_data.id
        is_profile_active_for_keyboard = employer_profile_data.is_active
        
        # Подсчитываем новые непросмотренные отклики
        count_new_responses_result = await session.execute(
            select(sqlalchemy_func.count(ApplicantEmployerInteraction.id))
            .where(
                ApplicantEmployerInteraction.employer_profile_id == employer_profile_id_for_count,
                ApplicantEmployerInteraction.is_viewed_by_employer == False,
                ApplicantEmployerInteraction.interaction_type.in_([InteractionTypeEnum.LIKE, InteractionTypeEnum.QUESTION_SENT])
            )
        )
        new_responses_count = count_new_responses_result.scalar_one() or 0
        print(f"DEBUG show_employer_main_menu: Employer {user_id}, Profile ID {employer_profile_id_for_count}, New responses count: {new_responses_count}")
        
    # Формируем текст сообщения
    main_menu_message_text = f"{name_prefix}{EMPLOYER_MAIN_MENU_TEXT}"

    # Выбираем нужную базовую клавиатуру (активную или неактивную)
    base_keyboard_buttons = []
    if is_profile_active_for_keyboard:
        base_keyboard_buttons = [row[:] for row in employer_main_menu_keyboard_active.keyboard]
    else:
        base_keyboard_buttons = [row[:] for row in employer_main_menu_keyboard_inactive.keyboard]
    
    view_responses_button_text_updated = BTN_VIEW_RESPONSES_TEXT
    if new_responses_count > 0:
        view_responses_button_text_updated += f" ({new_responses_count} новых)"

    found_and_updated_button = False
    for row in base_keyboard_buttons:
        for i, button in enumerate(row):
            if isinstance(button, KeyboardButton) and button.text.startswith(BTN_VIEW_RESPONSES_TEXT.split(" (")[0]):
                row[i] = KeyboardButton(text=view_responses_button_text_updated) # Заменяем кнопку
                found_and_updated_button = True
                break
        if found_and_updated_button:
            break
    
    # Если по какой-то причине кнопка не была найдена для обновления (например, текст BTN_VIEW_RESPONSES_TEXT изменился),
    # добавляем ее как новую строку. Этого не должно происходить при правильном определении клавиатур.
    if not found_and_updated_button:
        base_keyboard_buttons.append([KeyboardButton(text=view_responses_button_text_updated)])
        print(f"WARN show_employer_main_menu: View responses button not found in base layout, appending new one for user {user_id}.")

    final_keyboard = ReplyKeyboardMarkup(keyboard=base_keyboard_buttons, resize_keyboard=True)
        
    # Отправляем короткое сообщение с меню
    await message_to_reply_to.answer(main_menu_message_text, reply_markup=final_keyboard)


async def show_applicant_profile_for_editing(target: Message | CallbackQuery, state: FSMContext):
    user_id = target.from_user.id
    message_to_interact_with = target if isinstance(target, Message) else target.message
    profile_display_text = "Не удалось загрузить анкету."
    keyboard_to_show = get_applicant_edit_keyboard()

    async with AsyncSessionFactory() as session, session.begin():
        user_obj = await session.get(User, user_id)
        applicant_p_db = (await session.execute(select(ApplicantProfile).where(ApplicantProfile.user_id == user_id))).scalar_one_or_none()
        if applicant_p_db and user_obj:
            gender_d = getattr(applicant_p_db.gender, 'name', "Не указан").title() # Male, Female
            contact_i = user_obj.contact_phone if user_obj.contact_phone else "Не указан"
            if contact_i != "Не указан" and not contact_i.startswith('+'): contact_i = f"+{contact_i}"
            profile_display_text = (
                f"📝 Редактирование анкеты соискателя:\n\n"
                f"Город: {applicant_p_db.city}\nПол: {gender_d}\n"
                f"Возраст: {applicant_p_db.age}\nОпыт: {applicant_p_db.experience}\n"
                f"Контакт: {contact_i}\nСтатус: {'Активна' if applicant_p_db.is_active else 'Неактивна'}\n\n"
                f"Что бы вы хотели изменить?"
            )
        else:
            profile_display_text = "Анкета соискателя не найдена."
            keyboard_to_show = None 
            from app.bot import start_keyboard
            await message_to_interact_with.answer(profile_display_text, reply_markup=start_keyboard)
            if isinstance(target, CallbackQuery): await target.answer()
            return
            
    await state.set_state(ApplicantEditProfile.waiting_for_field_to_edit)
    if isinstance(target, Message):
        await target.answer(profile_display_text, reply_markup=keyboard_to_show)
    elif isinstance(target, CallbackQuery):
        try: await target.message.edit_text(profile_display_text, reply_markup=keyboard_to_show)
        except: await target.message.answer(profile_display_text, reply_markup=keyboard_to_show)
        await target.answer()


async def show_employer_profile_for_editing(target: Message | CallbackQuery, state: FSMContext):
    user_id = target.from_user.id
    message_to_interact_with = target if isinstance(target, Message) else target.message
    profile_display_text = "Загрузка данных анкеты..."
    keyboard_to_show = get_employer_edit_keyboard()

    async with AsyncSessionFactory() as session, session.begin():
        emp_profile_db = (await session.execute(select(EmployerProfile).where(EmployerProfile.user_id == user_id))).scalar_one_or_none()
        if emp_profile_db:
            wf_d = getattr(emp_profile_db.work_format, 'name', "Не указан").title()
            min_age_d = emp_profile_db.min_age_candidate if emp_profile_db.min_age_candidate is not None else "Не указан"
            photo_i = "Есть" if emp_profile_db.photo_file_id else "Нет"
            profile_display_text = (
                f"📝 Редактирование анкеты компании:\n\n"
                f"Компания: {emp_profile_db.company_name}\nГород: {emp_profile_db.city}\n"
                f"Позиция: {emp_profile_db.position}\nЗП: {emp_profile_db.salary}\n"
                f"Мин. возраст: {min_age_d}\nФормат: {wf_d}\nФото: {photo_i}\n"
                f"Описание: {emp_profile_db.description[:70]}...\nСтатус: {'Активна' if emp_profile_db.is_active else 'Неактивна'}\n\n"
                f"Что бы вы хотели изменить?"
            )
        else:
            profile_display_text = "Анкета вашей компании не найдена."
            keyboard_to_show = None
            from app.bot import start_keyboard
            await message_to_interact_with.answer(profile_display_text, reply_markup=start_keyboard)
            if isinstance(target, CallbackQuery): await target.answer()
            return

    await state.set_state(EmployerEditProfile.waiting_for_field_to_edit)
    
    # Логика отображения/редактирования сообщения
    current_message_has_photo = bool(message_to_interact_with.photo)
    target_profile_has_photo = bool(emp_profile_db and emp_profile_db.photo_file_id)

    try:
        if isinstance(target, Message): # Вызвано из message хэндлера (кнопка "Редактировать анкету компании")
            await target.answer(text="Переходим в режим редактирования...", reply_markup=ReplyKeyboardRemove()) # Убираем Reply кнопки
            if target_profile_has_photo:
                await target.bot.send_photo(user_id, emp_profile_db.photo_file_id, caption=profile_display_text, reply_markup=keyboard_to_show)
            else:
                await target.answer(profile_display_text, reply_markup=keyboard_to_show)
        
        elif isinstance(target, CallbackQuery): # Вызвано из callback (например, "Назад" или после обновления поля)
            if target_profile_has_photo:
                if current_message_has_photo: 
                    await target.message.edit_caption(caption=profile_display_text, reply_markup=keyboard_to_show)
                else: 
                    await target.message.delete()
                    await target.message.answer_photo(photo=emp_profile_db.photo_file_id, caption=profile_display_text, reply_markup=keyboard_to_show)
            else: 
                if current_message_has_photo: 
                    await target.message.delete() 
                    await target.message.answer(profile_display_text, reply_markup=keyboard_to_show)
                else: 
                    await target.message.edit_text(profile_display_text, reply_markup=keyboard_to_show)
            await target.answer()
            
    except Exception as e:
        print(f"Error in show_employer_profile_for_editing: {e}\n{traceback.format_exc()}")
        # Фоллбэк на отправку нового сообщения
        if target_profile_has_photo:
            await message_to_interact_with.answer_photo(photo=emp_profile_db.photo_file_id, caption=profile_display_text, reply_markup=keyboard_to_show)
        else:
            await message_to_interact_with.answer(profile_display_text, reply_markup=keyboard_to_show)
        if isinstance(target, CallbackQuery): await target.answer("Не удалось обновить предыдущее сообщение.")


# Тестовый хэндлер для вызова меню соискателя (если нужен для отладки)
@settings_router.message(Command("applicantmenu"))
async def internal_show_applicant_menu_handler(message: Message, state: FSMContext):
    user_id = message.from_user.id
    display_name = message.from_user.first_name
    async with AsyncSessionFactory() as session, session.begin():
         user_db_data = await session.get(User, user_id)
         if user_db_data:
            if user_db_data.first_name: display_name = user_db_data.first_name
            if user_db_data.role == UserRole.APPLICANT:
                # Передаем user_id в show_applicant_settings_menu
                await show_applicant_settings_menu(message, user_id, display_name)
                return
    await message.answer("Эта команда только для зарегистрированных соискателей.")

# Кнопка "Продолжить смотреть анкеты"
@settings_router.message(F.text == "Продолжить смотреть анкеты")
async def applicant_continue_browsing(message: Message, state: FSMContext):
    user_id = message.from_user.id
    is_applicant_active = False
    display_name = message.from_user.first_name # Для возврата в меню, если что

    async with AsyncSessionFactory() as session, session.begin():
        # Проверяем, активен ли профиль соискателя
        db_user_check = await session.get(User, user_id) # Получаем User для имени
        if db_user_check and db_user_check.first_name:
            display_name = db_user_check.first_name

        applicant_profile_status = (await session.execute(
            select(ApplicantProfile.is_active).where(ApplicantProfile.user_id == user_id)
        )).scalar_one_or_none()
        
        if applicant_profile_status is True: # Профиль существует и активен
            is_applicant_active = True
    
    if is_applicant_active:
        await message.answer("💸🔍", reply_markup=ReplyKeyboardRemove())
        await show_next_employer_profile(message, user_id, state) # <--- ПЕРЕДАЕМ state

    else: # Если анкета неактивна или не создана
        await message.answer("Ваша анкета неактивна или не создана. Сначала возобновите поиск или создайте анкету.", reply_markup=ReplyKeyboardRemove())
        await show_applicant_settings_menu(message, user_id, display_name) # Показываем меню

# Кнопка "Моя анкета" (ведет к редактированию)
@settings_router.message(F.text == "Моя анкета", StateFilter(None))
async def applicant_my_profile_start_editing(message: Message, state: FSMContext):
    # Отправляем сообщение, чтобы убрать предыдущую Reply-клавиатуру
    await message.answer("Загружаю вашу анкету для редактирования...", reply_markup=ReplyKeyboardRemove())
    # Теперь вызываем функцию, которая покажет анкету и Inline-кнопки
    await show_applicant_profile_for_editing(message, state)



# Кнопка "Я больше не ищу работу" (деактивация)
@settings_router.message(F.text == "Я больше не ищу работу")
async def applicant_deactivate_profile(message: Message, state: FSMContext):
    user_id = message.from_user.id
    updated_in_db = False
    display_name = message.from_user.first_name
    try:
        async with AsyncSessionFactory() as session, session.begin():
            user_obj = await session.get(User, user_id)
            if user_obj and user_obj.first_name:
                display_name = user_obj.first_name
            
            result = await session.execute(
                update(ApplicantProfile)
                .where(ApplicantProfile.user_id == user_id, ApplicantProfile.is_active == True)
                .values(is_active=False, 
                        deactivation_date=datetime.now(timezone.utc), 
                        updated_at=func.now())
                .returning(ApplicantProfile.id)
            )
            if result.scalar_one_or_none() is not None:
                updated_in_db = True
                # Также сбросим дату последнего re-engagement уведомления, если он решит снова деактивировать
                await session.execute(
                    update(User).where(User.telegram_id == user_id).values(last_reengagement_notif_sent_at=None)
                )

        
        if updated_in_db:
            await message.answer("Ваша анкета деактивирована.", reply_markup=ReplyKeyboardRemove())
        else:
            await message.answer("Ваша анкета уже неактивна или не найдена.", reply_markup=ReplyKeyboardRemove())
    except Exception as e:
        print(f"Error in applicant_deactivate_profile: {e}\n{traceback.format_exc()}")
        await message.answer("Ошибка при деактивации анкеты.")
    finally:
        await show_applicant_settings_menu(message, user_id, display_name)

# Кнопка "Возобновить поиск работы" (активация)
@settings_router.message(F.text == "Возобновить поиск работы")
async def applicant_activate_profile(message: Message, state: FSMContext):
    user_id = message.from_user.id
    activated_successfully = False
    profile_was_already_active = False
    display_name = message.from_user.first_name

    try:
        async with AsyncSessionFactory() as session, session.begin():
            user_obj = await session.get(User, user_id) # Проверка роли и получение имени
            if user_obj:
                if user_obj.first_name: display_name = user_obj.first_name
                if user_obj.role != UserRole.APPLICANT:
                    await message.answer("Это действие доступно только для соискателей.")
                    await show_applicant_settings_menu(message, user_id, display_name) # Передаем user_id
                    return
            else:
                await message.answer("Ошибка: пользователь не найден.")
                from app.bot import start_keyboard
                await message.answer("Начните сначала.", reply_markup=start_keyboard)
                return

            values_to_set_for_activation = {
                "is_active": True,
                "deactivation_date": None, 
                "updated_at": func.now()
            }
            
            update_result = await session.execute(
                update(ApplicantProfile) # Обновляем ApplicantProfile
                .where(ApplicantProfile.user_id == user_id, ApplicantProfile.is_active == False)
                .values(**values_to_set_for_activation)
                .returning(ApplicantProfile.id)
            )
            
            if update_result.scalar_one_or_none() is not None:
                activated_successfully = True
            else:
                current_profile_status_q = await session.execute(
                    select(ApplicantProfile.is_active).where(ApplicantProfile.user_id == user_id)
                )
                current_profile_is_active = current_profile_status_q.scalar_one_or_none()
                if current_profile_is_active is True:
                    profile_was_already_active = True
        
        if activated_successfully:
            await message.answer("Ваша анкета снова активна.", reply_markup=ReplyKeyboardRemove())
        elif profile_was_already_active:
            await message.answer("Ваша анкета уже была активна.", reply_markup=ReplyKeyboardRemove())
        else:
            await message.answer("Не удалось активировать анкету. Возможно, она не найдена?", reply_markup=ReplyKeyboardRemove())
            
    except Exception as e:
        print(f"Error in applicant_activate_profile: {e}\n{traceback.format_exc()}")
        await message.answer("Ошибка при активации анкеты.")
    finally:
        await show_applicant_settings_menu(message, user_id, display_name) # Передаем user_id

# --- РЕДАКТИРОВАНИЕ АНКЕТЫ СОИСКАТЕЛЯ ---

# Callback-хэндлер для кнопки "Назад в меню настроек" из редактирования полей
@settings_router.callback_query(F.data == "back_to_applicant_settings", StateFilter(ApplicantEditProfile.waiting_for_field_to_edit))
async def applicant_back_to_settings_from_edit_fields(callback_query: CallbackQuery, state: FSMContext):
    await state.clear() 
    try:
        await callback_query.message.edit_reply_markup(reply_markup=None) 
    except Exception: pass    
    await callback_query.answer()
    
    user_id = callback_query.from_user.id
    display_name = callback_query.from_user.first_name
    async with AsyncSessionFactory() as session, session.begin():
        user_obj = await session.get(User, user_id)
        if user_obj and user_obj.first_name:
            display_name = user_obj.first_name
    await show_applicant_settings_menu(callback_query.message, user_id, display_name)

# Общая функция для запроса нового значения (текстового) при редактировании анкеты соискателя
async def request_new_applicant_field_value(callback_query: CallbackQuery, state: FSMContext, new_fsm_state: State, prompt_text: str):
    await state.set_state(new_fsm_state)
    try:
        await callback_query.message.edit_text(prompt_text, reply_markup=None) # Убираем inline кнопки
        # Отправляем Reply-кнопку "Отменить изменение поля" новым сообщением
        await callback_query.message.answer("Если передумали, нажмите:", reply_markup=cancel_field_edit_keyboard)
    except Exception: # Если не получилось отредактировать текст
        await callback_query.message.answer(prompt_text, reply_markup=cancel_field_edit_keyboard) # Отправляем новое с кнопкой отмены
    await callback_query.answer()

# Callback-хэндлеры для выбора полей для редактирования (анкета соискателя)
@settings_router.callback_query(F.data == "edit_applicant_city", StateFilter(ApplicantEditProfile.waiting_for_field_to_edit))
async def edit_applicant_city_start(callback_query: CallbackQuery, state: FSMContext):
    await request_new_applicant_field_value(callback_query, state, ApplicantEditProfile.editing_city, "Введите новый город (Украина):")

@settings_router.callback_query(F.data == "edit_applicant_gender", StateFilter(ApplicantEditProfile.waiting_for_field_to_edit))
async def edit_applicant_gender_start(callback_query: CallbackQuery, state: FSMContext):
    await state.set_state(ApplicantEditProfile.editing_gender)
    gender_kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Мужской"), KeyboardButton(text="Женский")], [cancel_field_edit_button]],
        resize_keyboard=True, one_time_keyboard=False # one_time=False чтобы "Отмена" осталась
    )
    try: await callback_query.message.delete() # Удаляем сообщение с inline кнопками
    except: pass
    await callback_query.message.answer("Выберите новый пол:", reply_markup=gender_kb)
    await callback_query.answer()

@settings_router.callback_query(F.data == "edit_applicant_age", StateFilter(ApplicantEditProfile.waiting_for_field_to_edit))
async def edit_applicant_age_start(callback_query: CallbackQuery, state: FSMContext):
    await request_new_applicant_field_value(callback_query, state, ApplicantEditProfile.editing_age, "Введите новый возраст (числом):")

@settings_router.callback_query(F.data == "edit_applicant_experience", StateFilter(ApplicantEditProfile.waiting_for_field_to_edit))
async def edit_applicant_experience_start(callback_query: CallbackQuery, state: FSMContext):
    await request_new_applicant_field_value(callback_query, state, ApplicantEditProfile.editing_experience, "Опишите ваш новый опыт работы:")

# Общая функция для обновления поля соискателя и возврата к просмотру анкеты
async def update_applicant_field_and_show(message: Message, state: FSMContext, field_name: str, new_value):
    user_id = message.from_user.id
    async with AsyncSessionFactory() as session, session.begin():
        await session.execute(
            update(ApplicantProfile)
            .where(ApplicantProfile.user_id == user_id)
            .values({field_name: new_value, "updated_at": func.now()})
        )
    await message.answer(f"Поле '{field_name.replace('_', ' ').capitalize()}' обновлено.", reply_markup=ReplyKeyboardRemove())
    await show_applicant_profile_for_editing(message, state)

# Хэндлеры для обработки введенных значений при редактировании анкеты соискателя
@settings_router.message(ApplicantEditProfile.editing_city, F.text)
async def process_editing_applicant_city(message: Message, state: FSMContext):
    if message.text == "❌ Отменить изменение поля": return await cancel_current_applicant_field_edit(message, state)
    city_parts = [part.capitalize() for part in message.text.strip().split()]
    new_city = " ".join(city_parts)
    if not (2 <= len(new_city) <= 100):
        await message.answer("Город должен быть от 2 до 100 симв. Введите снова:", reply_markup=cancel_field_edit_keyboard)
        return
    await update_applicant_field_and_show(message, state, "city", new_city)

@settings_router.message(ApplicantEditProfile.editing_gender, F.text.in_({"Мужской", "Женский"}))
async def process_editing_applicant_gender(message: Message, state: FSMContext):
    gender_map = {"Мужской": GenderEnum.MALE, "Женский": GenderEnum.FEMALE}
    await update_applicant_field_and_show(message, state, "gender", gender_map[message.text])
    # await message.delete() # Reply кнопка сама исчезнет если one_time_keyboard=True

@settings_router.message(ApplicantEditProfile.editing_gender, F.text == "❌ Отменить изменение поля") # Отдельный для кнопки отмены, если она в Reply
async def cancel_gender_edit_button(message: Message, state: FSMContext):
    await cancel_current_applicant_field_edit(message, state)

@settings_router.message(ApplicantEditProfile.editing_gender) # Невалидный выбор пола
async def process_invalid_editing_applicant_gender(message: Message, state: FSMContext):
    await message.answer("Пожалуйста, выберите пол, нажав на одну из кнопок.")

@settings_router.message(ApplicantEditProfile.editing_age, F.text)
async def process_editing_applicant_age(message: Message, state: FSMContext):
    if message.text == "❌ Отменить изменение поля": return await cancel_current_applicant_field_edit(message, state)
    if not message.text.isdigit():
        await message.answer("Возраст должен быть числом. Введите снова:", reply_markup=cancel_field_edit_keyboard)
        return
    new_age = int(message.text)
    if not (16 <= new_age <= 70):
        await message.answer("Возраст от 16 до 70. Введите снова:", reply_markup=cancel_field_edit_keyboard)
        return
    await update_applicant_field_and_show(message, state, "age", new_age)

@settings_router.message(ApplicantEditProfile.editing_experience, F.text)
async def process_editing_applicant_experience(message: Message, state: FSMContext):
    if message.text == "❌ Отменить изменение поля": return await cancel_current_applicant_field_edit(message, state)
    new_experience = message.text.strip()
    if not (2 <= len(new_experience) <= 2000):
        await message.answer("Опыт от 2 до 2000 симв. Введите снова:", reply_markup=cancel_field_edit_keyboard)
        return
    await update_applicant_field_and_show(message, state, "experience", new_experience)

# Общий обработчик отмены редактирования текущего поля СОИСКАТЕЛЯ (для Reply-кнопки)
async def cancel_current_applicant_field_edit(message: Message, state: FSMContext):
    await message.answer("Изменение текущего поля отменено.", reply_markup=ReplyKeyboardRemove())
    await show_applicant_profile_for_editing(message, state)

# Команда отмены для всего процесса редактирования анкеты соискателя
@settings_router.message(Command("cancel_edit_applicant"), StateFilter(ApplicantEditProfile))
async def cancel_all_applicant_editing(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Редактирование анкеты отменено.", reply_markup=ReplyKeyboardRemove())
    user_id = message.from_user.id
    display_name = message.from_user.first_name # ... (можно получить актуальное из БД) ...
    await show_applicant_settings_menu(message, user_id, display_name)


# Кнопка "Редактировать анкету компании" из главного меню работодателя
@settings_router.message(F.text == "Редактировать анкету компании", StateFilter(None))
async def employer_start_editing_profile_handler(message: Message, state: FSMContext):
    user_id = message.from_user.id
    async with AsyncSessionFactory() as session, session.begin():
        user = await session.get(User, user_id)
        if not (user and user.role == UserRole.EMPLOYER):
            await message.answer("Эта опция доступна только для работодателей.", reply_markup=ReplyKeyboardRemove())
            if user and user.role == UserRole.APPLICANT:
                display_name = user.first_name if user.first_name else message.from_user.first_name
                await show_applicant_settings_menu(message, user_id, display_name)
            else:
                from app.bot import start_keyboard
                await message.answer("Пожалуйста, выберите вашу роль:", reply_markup=start_keyboard)
            return
            
    await message.answer("Загружаю редактор анкеты компании...", reply_markup=ReplyKeyboardRemove())
    await show_employer_profile_for_editing(message, state)


# Кнопка "Остановить поиск людей"
@settings_router.message(F.text == "Остановить поиск людей")
async def employer_deactivate_profile(message: Message, state: FSMContext):
    user_id = message.from_user.id
    updated = False
    display_name = message.from_user.first_name
    try:
        async with AsyncSessionFactory() as session, session.begin():
            user_obj = await session.get(User, user_id)
            if user_obj:
                if user_obj.first_name: display_name = user_obj.first_name
                if user_obj.role != UserRole.EMPLOYER: # Доп. проверка, если вдруг попали не туда
                    await message.answer("Это действие для работодателей.")
                    await show_employer_main_menu(message, user_id, display_name)
                    return

            result = await session.execute(
                update(EmployerProfile)
                .where(EmployerProfile.user_id == user_id, EmployerProfile.is_active == True)
                .values(is_active=False, 
                        deactivation_date=datetime.now(timezone.utc), 
                        updated_at=func.now())
                .returning(EmployerProfile.id)
            )
            if result.scalar_one_or_none() is not None: 
                updated = True
                await session.execute(
                    update(User).where(User.telegram_id == user_id).values(last_reengagement_notif_sent_at=None)
                )
        
        if updated:
            await message.answer("Поиск сотрудников остановлен.", reply_markup=employer_main_menu_keyboard_inactive)
        else:
            await message.answer("Поиск уже был остановлен или анкета не найдена. Пропишите /start для перезагрузки бота.", reply_markup=ReplyKeyboardRemove())
    except Exception as e:
        print(f"Error in employer_deactivate_profile: {e}\n{traceback.format_exc()}")
        await message.answer("Ошибка при остановке поиска.")
    #finally:
        #await show_employer_main_menu(message, user_id, display_name)

# Кнопка "Возобновить поиск людей"
@settings_router.message(F.text == "Возобновить поиск людей")
async def employer_activate_profile(message: Message, state: FSMContext):
    user_id = message.from_user.id
    activated_successfully = False # Переименовал для ясности
    profile_was_already_active = False
    display_name = message.from_user.first_name

    try:
        async with AsyncSessionFactory() as session, session.begin():
            # Сначала получаем пользователя, чтобы убедиться, что это работодатель и взять имя
            user_obj = await session.get(User, user_id)
            if user_obj:
                if user_obj.first_name: 
                    display_name = user_obj.first_name
                if user_obj.role != UserRole.EMPLOYER:
                    await message.answer("Это действие доступно только для работодателей.")
                    await show_employer_main_menu(message, user_id, display_name)
                    return
            else: # Если пользователя нет в БД, что маловероятно, если он дошел до этого меню
                await message.answer("Произошла ошибка: пользователь не найден.")
                from app.bot import start_keyboard # Локальный импорт
                await message.answer("Пожалуйста, начните сначала.", reply_markup=start_keyboard)
                return

            # Теперь пытаемся активировать профиль работодателя и сбросить дату деактивации
            values_to_set_for_activation = {
                "is_active": True,
                "deactivation_date": None, 
                "updated_at": func.now()
            }
            
            update_result = await session.execute(
                update(EmployerProfile)
                .where(EmployerProfile.user_id == user_id, EmployerProfile.is_active == False) # Активируем, только если был неактивен
                .values(**values_to_set_for_activation)
                .returning(EmployerProfile.id) # Чтобы проверить, была ли строка обновлена
            )
            
            if update_result.scalar_one_or_none() is not None:
                activated_successfully = True
            else:
                # Если ничего не обновилось, проверяем, может он уже активен
                current_profile_status_q = await session.execute(
                    select(EmployerProfile.is_active).where(EmployerProfile.user_id == user_id)
                )
                current_profile_is_active = current_profile_status_q.scalar_one_or_none()
                if current_profile_is_active is True:
                    profile_was_already_active = True
                # Если current_profile_is_active is None, значит, профиля вообще нет (хотя user есть)
                # Если current_profile_is_active is False, но update не сработал - это странно
        
        # Сообщения пользователю после транзакции
        if activated_successfully:
            await message.answer("Поиск сотрудников возобновлен.", reply_markup=employer_main_menu_keyboard_active)
        elif profile_was_already_active:
            await message.answer("Поиск сотрудников уже был активен.", reply_markup=employer_main_menu_keyboard_active)
        else:
            # Это может случиться, если профиля EmployerProfile для данного user_id вообще нет,
            # или если он был is_active=False, но update по какой-то причине не затронул строки.
            await message.answer("Не удалось возобновить поиск. Возможно, анкета не найдена или уже активна.", reply_markup=ReplyKeyboardRemove())
            
    except Exception as e:
        print(f"Error in employer_activate_profile: {e}\n{traceback.format_exc()}")
        await message.answer("Произошла ошибка при возобновлении поиска.")
    #finally:
        # В любом случае показываем главное меню работодателя
        # user_id и display_name уже должны быть корректно установлены к этому моменту
        #await show_employer_main_menu(message, user_id, display_name)



# --- РЕДАКТИРОВАНИЕ АНКЕТЫ РАБОТОДАТЕЛЯ ---

# Callback для "🔙 Назад в меню работодателя" из экрана выбора полей для редактирования
@settings_router.callback_query(F.data == "back_to_employer_main_menu", StateFilter(EmployerEditProfile.waiting_for_field_to_edit))
async def employer_back_to_main_menu_from_field_edit(callback_query: CallbackQuery, state: FSMContext):
    await state.clear()
    try: await callback_query.message.edit_reply_markup(reply_markup=None)
    except: pass
    await callback_query.answer()
    
    user_id = callback_query.from_user.id
    display_name = callback_query.from_user.first_name
    async with AsyncSessionFactory() as session, session.begin():
        user_obj = await session.get(User, user_id)
        if user_obj and user_obj.first_name: display_name = user_obj.first_name
    await show_employer_main_menu(callback_query.message, user_id, display_name)

# Общая функция для запроса нового значения (текстового) работодателем
async def request_new_employer_field_value(callback_query: CallbackQuery, state: FSMContext, new_fsm_state: State, prompt_text: str):
    await state.set_state(new_fsm_state)
    try: # Пытаемся отредактировать текущее сообщение
        await callback_query.message.edit_text(prompt_text, reply_markup=cancel_field_edit_keyboard) # Прикрепляем Reply-кнопку Отмены
    except Exception: # Если не получилось (например, сообщение было с фото)
        await callback_query.message.delete() # Удаляем старое
        await callback_query.message.answer(prompt_text, reply_markup=cancel_field_edit_keyboard) # Отправляем новое
    await callback_query.answer()

# Общая функция для обновления поля анкеты работодателя и показа экрана редактирования
async def update_employer_field_and_show(message_or_target: Message | CallbackQuery, state: FSMContext, field_name: str, new_value):
    user_id = message_or_target.from_user.id
    message_for_reply = message_or_target if isinstance(message_or_target, Message) else message_or_target.message
    
    async with AsyncSessionFactory() as session, session.begin():
        await session.execute(
            update(EmployerProfile)
            .where(EmployerProfile.user_id == user_id)
            .values({field_name: new_value, "updated_at": func.now()})
        )
    # Не отправляем сообщение об обновлении здесь, т.к. show_..._for_editing сама обновит анкету
    await show_employer_profile_for_editing(message_for_reply, state) # Показываем обновленную анкету для дальнейшего редактирования

# --- Callback-хэндлеры для выбора ПОЛЯ для редактирования (Работодатель) ---
@settings_router.callback_query(F.data == "edit_employer_city", StateFilter(EmployerEditProfile.waiting_for_field_to_edit))
async def edit_employer_city_start(callback_query: CallbackQuery, state: FSMContext):
    await request_new_employer_field_value(callback_query, state, EmployerEditProfile.editing_city, "Введите новый город:")

@settings_router.callback_query(F.data == "edit_employer_company_name", StateFilter(EmployerEditProfile.waiting_for_field_to_edit))
async def edit_employer_company_name_start(callback_query: CallbackQuery, state: FSMContext):
    await request_new_employer_field_value(callback_query, state, EmployerEditProfile.editing_company_name, "Новое название компании:")

@settings_router.callback_query(F.data == "edit_employer_position", StateFilter(EmployerEditProfile.waiting_for_field_to_edit))
async def edit_employer_position_start(callback_query: CallbackQuery, state: FSMContext):
    await request_new_employer_field_value(callback_query, state, EmployerEditProfile.editing_position, "Новая позиция:")

@settings_router.callback_query(F.data == "edit_employer_salary", StateFilter(EmployerEditProfile.waiting_for_field_to_edit))
async def edit_employer_salary_start(callback_query: CallbackQuery, state: FSMContext):
    await request_new_employer_field_value(callback_query, state, EmployerEditProfile.editing_salary, "Новая ЗП:")

@settings_router.callback_query(F.data == "edit_employer_min_age", StateFilter(EmployerEditProfile.waiting_for_field_to_edit))
async def edit_employer_min_age_start(callback_query: CallbackQuery, state: FSMContext):
    await request_new_employer_field_value(callback_query, state, EmployerEditProfile.editing_min_age, "Новый мин. возраст (число или '-'):")

@settings_router.callback_query(F.data == "edit_employer_description", StateFilter(EmployerEditProfile.waiting_for_field_to_edit))
async def edit_employer_description_start(callback_query: CallbackQuery, state: FSMContext):
    await request_new_employer_field_value(callback_query, state, EmployerEditProfile.editing_company_description, "Новое описание:")

@settings_router.callback_query(F.data == "edit_employer_work_format", StateFilter(EmployerEditProfile.waiting_for_field_to_edit))
async def edit_employer_work_format_start(callback_query: CallbackQuery, state: FSMContext):
    await state.set_state(EmployerEditProfile.editing_work_format)
    work_format_reply_kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Офлайн"), KeyboardButton(text="Онлайн")], [cancel_field_edit_button]],
        resize_keyboard=True, one_time_keyboard=False 
    )
    try: await callback_query.message.delete()
    except: pass
    await callback_query.message.answer("Выберите новый формат работы:", reply_markup=work_format_reply_kb)
    await callback_query.answer()

# --- Хэндлеры ОБРАБОТКИ ВВОДА для полей работодателя ---

# Общий хэндлер отмены для Reply-кнопки "❌ Отменить изменение поля" (для Работодателя)
@settings_router.message(StateFilter(
    EmployerEditProfile.editing_city, EmployerEditProfile.editing_company_name, 
    EmployerEditProfile.editing_position, EmployerEditProfile.editing_salary,
    EmployerEditProfile.editing_min_age, EmployerEditProfile.editing_company_description,
    EmployerEditProfile.editing_work_format 
    # Не включаем editing_photo_upload, т.к. для него своя inline-отмена
), F.text == "❌ Отменить изменение поля")
async def employer_cancel_current_field_input(message: Message, state: FSMContext):
    await message.answer("Изменение текущего поля отменено.", reply_markup=ReplyKeyboardRemove())
    await show_employer_profile_for_editing(message, state)

# Хэндлеры для каждого поля
@settings_router.message(EmployerEditProfile.editing_city, F.text)
async def process_editing_employer_city(message: Message, state: FSMContext):
    if message.text == "❌ Отменить изменение поля": return await employer_cancel_current_field_input(message, state)
    city_parts = [part.capitalize() for part in message.text.strip().split()]
    new_city = " ".join(city_parts)
    if not (2 <= len(new_city) <= 100):
        await message.answer("Город: 2-100 симв.", reply_markup=cancel_field_edit_keyboard)
        return
    await update_employer_field_and_show(message, state, "city", new_city)

# (Вам нужно будет ДОПИСАТЬ остальные `process_editing_employer_...` хэндлеры по аналогии с городом)
# Например, для company_name:
@settings_router.message(EmployerEditProfile.editing_company_name, F.text)
async def process_editing_employer_company_name(message: Message, state: FSMContext):
    if message.text == "❌ Отменить изменение поля": return await employer_cancel_current_field_input(message, state)
    new_name = message.text.strip()
    if not (2 <= len(new_name) <= 200):
        await message.answer("Название: 2-200 симв.", reply_markup=cancel_field_edit_keyboard)
        return
    await update_employer_field_and_show(message, state, "company_name", new_name)

@settings_router.message(EmployerEditProfile.editing_position, F.text)
async def process_editing_employer_position(message: Message, state: FSMContext):
    if message.text == "❌ Отменить изменение поля": return await employer_cancel_current_field_input(message, state)
    new_pos = message.text.strip()
    if not (3 <= len(new_pos) <= 150):
        await message.answer("Позиция: 3-150 симв.", reply_markup=cancel_field_edit_keyboard)
        return
    await update_employer_field_and_show(message, state, "position", new_pos)

@settings_router.message(EmployerEditProfile.editing_salary, F.text)
async def process_editing_employer_salary(message: Message, state: FSMContext):
    if message.text == "❌ Отменить изменение поля": return await employer_cancel_current_field_input(message, state)
    new_salary = message.text.strip()
    if not (3 <= len(new_salary) <= 100):
        await message.answer("ЗП: 3-100 симв.", reply_markup=cancel_field_edit_keyboard)
        return
    await update_employer_field_and_show(message, state, "salary", new_salary)

@settings_router.message(EmployerEditProfile.editing_min_age, F.text)
async def process_editing_employer_min_age(message: Message, state: FSMContext):
    if message.text == "❌ Отменить изменение поля": return await employer_cancel_current_field_input(message, state)
    # ... (ваша логика валидации и сохранения для min_age, вызывающая update_employer_field_and_show)
    min_age_text = message.text.strip()
    new_min_age_val = None
    if min_age_text in ["-", "0"]: new_min_age_val = None
    elif min_age_text.isdigit():
        age_val = int(min_age_text)
        if 16 <= age_val <= 70: new_min_age_val = age_val
        else:
            await message.answer("Возраст от 16 до 70 или '-'/'0'.", reply_markup=cancel_field_edit_keyboard); return
    else: await message.answer("Введите число или '-'/'0'.", reply_markup=cancel_field_edit_keyboard); return
    await update_employer_field_and_show(message, state, "min_age_candidate", new_min_age_val)


@settings_router.message(EmployerEditProfile.editing_company_description, F.text)
async def process_editing_employer_description(message: Message, state: FSMContext):
    if message.text == "❌ Отменить изменение поля": return await employer_cancel_current_field_input(message, state)
    new_desc = message.text.strip()
    if not (10 <= len(new_desc) <= 2000):
        await message.answer("Описание: 10-2000 симв.", reply_markup=cancel_field_edit_keyboard)
        return
    await update_employer_field_and_show(message, state, "description", new_desc)

@settings_router.message(EmployerEditProfile.editing_work_format, F.text.in_({"Офлайн", "Онлайн"}))
async def process_editing_employer_work_format(message: Message, state: FSMContext):
    work_format_map = {"Офлайн": WorkFormatEnum.OFFLINE, "Онлайн": WorkFormatEnum.ONLINE}
    await update_employer_field_and_show(message, state, "work_format", work_format_map[message.text])
    # ReplyKeyboardRemove не нужно, т.к. update_... вызовет show_..._for_editing, которая вернет inline

@settings_router.message(EmployerEditProfile.editing_work_format, F.text == "❌ Отменить изменение поля")
async def cancel_work_format_edit_button(message: Message, state: FSMContext): # Отдельный для кнопки отмены из этого состояния
    await employer_cancel_current_field_input(message, state)

@settings_router.message(EmployerEditProfile.editing_work_format) # Невалидный выбор формата
async def process_invalid_editing_employer_work_format(message: Message, state: FSMContext):
    await message.answer("Пожалуйста, выберите формат работы, нажав на одну из кнопок.", reply_markup=cancel_field_edit_keyboard)


# ---- СЕКЦИЯ РЕДАКТИРОВАНИЯ ФОТО РАБОТОДАТЕЛЯ (реализация желаемой логики) ----

# Callback для кнопки "Фотография" -> показать опции работы с фото
@settings_router.callback_query(F.data == "edit_employer_photo_router", StateFilter(EmployerEditProfile.waiting_for_field_to_edit))
async def route_to_employer_photo_options(callback_query: CallbackQuery, state: FSMContext):
    await show_employer_photo_options_menu_logic(callback_query, state)

async def show_employer_photo_options_menu_logic(target: types.CallbackQuery, state: FSMContext): # Изменил тип target для ясности
    user_id = target.from_user.id
    message_to_handle = target.message # Сообщение, которое будем редактировать/удалять

    await state.set_state(EmployerEditProfile.editing_photo_option)

    current_photo_file_id = None
    async with AsyncSessionFactory() as session, session.begin():
        # Исправлено: используем select().where() для поиска по user_id
        profile_query = await session.execute(
            select(EmployerProfile).where(EmployerProfile.user_id == user_id)
        )
        employer_profile = profile_query.scalar_one_or_none()
        
        if employer_profile:
            current_photo_file_id = employer_profile.photo_file_id

    buttons_list = [[InlineKeyboardButton(text="📷 Загрузить/Изменить фото", callback_data="emp_photo_ask_new_photo")]]
    if current_photo_file_id:
        buttons_list.append([InlineKeyboardButton(text="🗑️ Удалить фото", callback_data="emp_photo_delete_action")])
    buttons_list.append([InlineKeyboardButton(text="🔙 Назад (к полям анкеты)", callback_data="emp_photo_back_to_fields_from_options")])
    
    options_kb = InlineKeyboardMarkup(inline_keyboard=buttons_list)

    text_to_show = "Управление фотографией компании/вакансии.\n"
    
    # Логика отображения и редактирования сообщения
    try:
        if current_photo_file_id:
            text_to_show += "\nТекущее фото прикреплено." # Упростил текст для надежности редактирования
            if message_to_handle.photo: 
                await message_to_handle.edit_caption(caption=text_to_show, reply_markup=options_kb)
            else: 
                await message_to_handle.delete()
                await message_to_handle.answer_photo(photo=current_photo_file_id, caption=text_to_show, reply_markup=options_kb)
        else:
            text_to_show += "Фотография еще не загружена."
            # Если предыдущее сообщение было с фото (например, после удаления фото), удаляем его и шлем текст
            if message_to_handle.photo:
                await message_to_handle.delete()
                await message_to_handle.answer(text_to_show, reply_markup=options_kb)
            else: # Если и было текстовым, просто редактируем текст
                await message_to_handle.edit_text(text_to_show, reply_markup=options_kb)
        
        await target.answer() # Отвечаем на исходный callback_query
            
    except Exception as e:
        print(f"Error showing/editing in show_employer_photo_options_menu_logic: {e}\n{traceback.format_exc()}")
        # Фоллбэк: отправить новое сообщение, если редактирование сломалось
        # Это сообщение будет от бота, а не ответом на предыдущее пользовательское сообщение
        fallback_text = "Управление фотографией.\n" + ("Текущее фото (если есть)." if current_photo_file_id else "Фото не загружено.")
        if current_photo_file_id:
            await target.bot.send_photo(chat_id=user_id, photo=current_photo_file_id, caption=fallback_text, reply_markup=options_kb)
        else:
            await target.bot.send_message(chat_id=user_id, text=fallback_text, reply_markup=options_kb)
        if isinstance(target, types.CallbackQuery): # Убедимся, что ответили на первоначальный callback
             await target.answer("Не удалось обновить предыдущее сообщение, показаны опции.")


@settings_router.callback_query(F.data == "emp_photo_ask_new_photo", StateFilter(EmployerEditProfile.editing_photo_option))
async def ask_for_new_employer_photo_action(callback_query: CallbackQuery, state: FSMContext):
    await state.set_state(EmployerEditProfile.editing_photo_upload)
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚫 Отмена загрузки", callback_data="emp_photo_cancel_this_upload_attempt")]])
    try: await callback_query.message.edit_text("Пожалуйста, отправьте новую фотографию:", reply_markup=cancel_kb)
    except: await callback_query.message.answer("Пожалуйста, отправьте новую фотографию:", reply_markup=cancel_kb)
    await callback_query.answer()

@settings_router.message(EmployerEditProfile.editing_photo_upload, F.photo)
async def handle_new_employer_photo_upload(message: Message, state: FSMContext):
    new_photo_id = message.photo[-1].file_id
    await message.answer("Фотография обновлена!", reply_markup=ReplyKeyboardRemove())
    await update_employer_field_and_show(message, state, "photo_file_id", new_photo_id)

@settings_router.callback_query(F.data == "emp_photo_cancel_this_upload_attempt", StateFilter(EmployerEditProfile.editing_photo_upload))
async def cancel_this_employer_photo_upload(callback_query: CallbackQuery, state: FSMContext):
    await callback_query.answer("Загрузка отменена.")
    await show_employer_photo_options_menu_logic(callback_query, state) # Возврат к опциям фото

@settings_router.callback_query(F.data == "emp_photo_delete_action", StateFilter(EmployerEditProfile.editing_photo_option))
async def do_delete_employer_photo(callback_query: CallbackQuery, state: FSMContext):
    async with AsyncSessionFactory() as session, session.begin():
        await session.execute(update(EmployerProfile).where(EmployerProfile.user_id == callback_query.from_user.id).values(photo_file_id=None, updated_at=func.now()))
    await callback_query.answer("Фотография удалена.")
    await show_employer_profile_for_editing(callback_query, state) # Возврат к выбору полей

@settings_router.callback_query(F.data == "emp_photo_back_to_fields_from_options", StateFilter(EmployerEditProfile.editing_photo_option))
async def back_to_fields_from_photo_options(callback_query: CallbackQuery, state: FSMContext):
    await callback_query.answer()
    await show_employer_profile_for_editing(callback_query, state) # Возврат к выбору полей

# Общая команда отмены для всего процесса редактирования анкеты работодателя
@settings_router.message(Command("cancel_edit_employer"), StateFilter(EmployerEditProfile))
async def cancel_all_employer_editing(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Редактирование анкеты компании отменено.", reply_markup=ReplyKeyboardRemove())
    user_id = message.from_user.id
    display_name = message.from_user.first_name 
    async with AsyncSessionFactory() as session, session.begin():
        user = await session.get(User, user_id)
        if user and user.first_name: display_name = user.first_name
    await show_employer_main_menu(message, user_id, display_name)
    
    
 
    
