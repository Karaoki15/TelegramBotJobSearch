# app/handlers/admin_handlers.py
import traceback
import secrets 
from aiogram import Router, F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, ReplyKeyboardRemove, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command, StateFilter, Filter
from aiogram import Bot

from app.config import ADMIN_IDS
from app.db.database import AsyncSessionFactory

from app.db.models import User, UserRole, BotSettings, EmployerProfile, ApplicantProfile, Complaint, ComplaintStatusEnum, WorkFormatEnum, MotivationalContent, MotivationalContentTypeEnum, ReferralLink, ReferralUsage
from sqlalchemy import select, update, delete, func 
from sqlalchemy.orm import selectinload 
from sqlalchemy.dialects.postgresql import insert
import sqlalchemy
from app.handlers.browsing_handlers import format_employer_profile_for_applicant



from app.states.admin_states import AdminStates, AdminAddDummyEmployer, AdminReferralManagement


from app.handlers.settings_handlers import show_applicant_settings_menu, show_employer_main_menu

DUMMY_PROFILE_CALLBACK_PREFIX = "dummy_profile_"
REAL_EMP_PROFILE_CALLBACK_PREFIX = "real_emp_profile_"
REAL_EMP_LIST_PAGE_CALLBACK_PREFIX = "real_emp_page_"
USER_DETAILS_CALLBACK_PREFIX = "admin_user_details_"
MOTIVATION_CALLBACK_PREFIX = "admin_motiv_"
REFERRAL_CALLBACK_PREFIX = "admin_ref_"


admin_router = Router()

# --- ФИЛЬТР АДМИНИСТРАТОРА ---
class IsAdminFilter(Filter):
    async def __call__(self, message: Message) -> bool:
        if not message.from_user:
            return False
        return message.from_user.id in ADMIN_IDS

# --- ОСНОВНОЕ МЕНЮ АДМИНКИ (добавляем новую кнопку) ---
admin_main_menu_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🤖 Анти-спам Пустышка")],
        [KeyboardButton(text="📝 Пустышки Работодателей")],
        [KeyboardButton(text="📄 Просмотр/Модерация Анкет Работодателей")],
        [KeyboardButton(text="📊 Управление трафиком")], 
        [KeyboardButton(text="ℹ️ Найти пользователя по ID")],
        [KeyboardButton(text="🎬 Управление Мотивационным Контентом")],
        [KeyboardButton(text="🚪 Выйти из Админки")]
    ],
    resize_keyboard=True
)
ADMIN_GREETING = "Добро пожаловать в Админ-панель!"

cancel_field_edit_button = KeyboardButton(text="❌ Отменить изменение") # Можно текст поменять на "Отменить ввод"
cancel_field_edit_keyboard = ReplyKeyboardMarkup(
    keyboard=[[cancel_field_edit_button]], 
    resize_keyboard=True, 
    one_time_keyboard=True 
)

motivation_type_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Видео"), KeyboardButton(text="Фото")],
        [KeyboardButton(text="Только текст")],
        [KeyboardButton(text="Отмена добавления")] # Общая кнопка отмены для FSM
    ],
    resize_keyboard=True,
    one_time_keyboard=True # Или False, чтобы "Отмена" оставалась
)


def get_manage_dummy_profiles_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="➕ Создать новую пустышку", callback_data="admin_action_create_dummy")],
        [InlineKeyboardButton(text="📄 Список/Редактирование пустышек (TODO)", callback_data="admin_action_list_dummies")],
        [InlineKeyboardButton(text="🔙 Назад в Админ-меню", callback_data="admin_back_to_main_from_dummies")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

@admin_router.message(Command("admin"), IsAdminFilter())
async def admin_panel_start(message: Message, state: FSMContext):
    print(f"DEBUG: Admin {message.from_user.id} entered admin panel.")
    await state.set_state(AdminStates.in_panel)
    await message.answer(ADMIN_GREETING, reply_markup=admin_main_menu_keyboard)

@admin_router.message(Command("admin")) # Если не прошел IsAdminFilter
async def admin_panel_attempt_not_admin(message: Message):
    print(f"DEBUG: Non-admin {message.from_user.id} tried to access /admin.")
    await message.answer("У вас нет доступа к этой команде.")

@admin_router.message(F.text == "🚪 Выйти из Админки", StateFilter(AdminStates.in_panel))
async def admin_panel_exit(message: Message, state: FSMContext):
    user_id = message.from_user.id
    print(f"DEBUG: Admin {user_id} is exiting admin panel.")
    await state.clear() 
    
    display_name = message.from_user.first_name
    user_role_on_exit = None
    
    async with AsyncSessionFactory() as session, session.begin():
        user_db = await session.get(User, user_id)
        if user_db:
            if user_db.first_name: display_name = user_db.first_name
            user_role_on_exit = user_db.role
            print(f"DEBUG: User {user_id} role after exit attempt: {user_role_on_exit}")
        else:
            print(f"DEBUG: User {user_id} not found in DB upon exiting admin panel.")

    await message.answer("Вы вышли из Админ-панели.", reply_markup=ReplyKeyboardRemove())

    if user_role_on_exit == UserRole.APPLICANT:
        await show_applicant_settings_menu(message, user_id, display_name)
    elif user_role_on_exit == UserRole.EMPLOYER:
        await show_employer_main_menu(message, user_id, display_name)
    else: 
        from app.bot import start_keyboard 
        await message.answer("Выберите роль, если хотите продолжить:", reply_markup=start_keyboard)

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ BOT_SETTINGS (для анти-спам пустышки) ---
async def get_bot_setting(session, key: str) -> str | None: # Убрал тип AsyncSession из сигнатуры, т.к. сессия передается
    result = await session.execute(select(BotSettings.value_str).where(BotSettings.setting_key == key))
    return result.scalar_one_or_none()

async def update_bot_setting(session, key: str, value_str: str | None = None): # Упростил, только value_str
    stmt = insert(BotSettings).values(setting_key=key, value_str=value_str)
    stmt = stmt.on_conflict_do_update(
        index_elements=['setting_key'], 
        set_=dict(value_str=value_str) 
    )
    await session.execute(stmt)

# --- УПРАВЛЕНИЕ АНТИ-СПАМ ПУСТЫШКОЙ ---

@admin_router.message(StateFilter(
    AdminStates.editing_antispam_dummy_text, 
    AdminStates.editing_antispam_dummy_photo # И другие будущие состояния редактирования админом, если эта кнопка будет общей
), F.text.startswith("❌ Отменить изменение")) # Используем startswith на случай, если текст кнопки чуть другой
async def admin_cancel_current_field_edit_by_button(message: Message, state: FSMContext):
    current_fsm_state_str = await state.get_state() # Получаем текущее состояние

    await message.answer("Действие отменено.", reply_markup=ReplyKeyboardRemove())
    
    # Проверяем, из какого конкретно состояния мы отменяем, чтобы вернуться в правильное подменю
    if current_fsm_state_str in [AdminStates.editing_antispam_dummy_text.state, 
                                 AdminStates.editing_antispam_dummy_photo.state]:
        await state.set_state(AdminStates.in_panel) # Устанавливаем состояние для меню настроек пустышки
        await show_antispam_dummy_config_menu(message, state)
    else: 
        # Если эта кнопка отмены используется для других админских действий в будущем,
        # можно предусмотреть возврат в главное меню админки.
        print(f"DEBUG: admin_cancel_current_field_edit_by_button called from unexpected state: {current_fsm_state_str}")
        await state.set_state(AdminStates.in_panel)
        await message.answer(ADMIN_GREETING, reply_markup=admin_main_menu_keyboard)


# Хэндлер для получения текста пустышки (идет ПОСЛЕ хэндлера кнопки отмены)
@admin_router.message(AdminStates.editing_antispam_dummy_text, F.text)
async def admin_process_antispam_text(message: Message, state: FSMContext):
    new_text = message.text.strip()
    if not (10 <= len(new_text) <= 1000): # Примерная валидация длины
        await message.answer(
            "Текст должен быть от 10 до 1000 символов. Попробуйте снова.",
            reply_markup=cancel_field_edit_keyboard # Снова предлагаем кнопку отмены, если ввод неверный
        )
        return # Оставляем пользователя в том же состоянии для повторного ввода
    
    async with AsyncSessionFactory() as session, session.begin():
        await update_bot_setting(session, "antispam_dummy_text", new_text)
    
    await message.answer("Текст анти-спам пустышки обновлен!", reply_markup=ReplyKeyboardRemove())
    
    await show_antispam_dummy_config_menu(message, state) # state здесь нужен для контекста, если show_... его использует

# Inline-клавиатура для управления анти-спам пустышкой
def get_antispam_dummy_management_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="✏️ Изменить текст", callback_data="admin_as_edit_text")],
        [InlineKeyboardButton(text="🖼️ Загрузить/Изменить фото", callback_data="admin_as_edit_photo")],
        [InlineKeyboardButton(text="🗑️ Удалить фото", callback_data="admin_as_delete_photo")],
        [InlineKeyboardButton(text="🔙 Назад в Админ-меню", callback_data="admin_as_back_to_main_panel_cb")] # Уникализировал callback_data
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# Функция для показа текущих настроек анти-spam пустышки и кнопок управления
async def show_antispam_dummy_config_menu(target_message_or_cq: Message | CallbackQuery, state: FSMContext):
    message_to_act_on = target_message_or_cq.message if isinstance(target_message_or_cq, CallbackQuery) else target_message_or_cq
    user_id = target_message_or_cq.from_user.id # Для отправки нового сообщения, если нужно
    
    text_to_show = "<b>Управление Анти-спам Пустышкой:</b>\n\n"
    current_text_value = "<code>Текст еще не задан.</code>" # Используем HTML для выделения
    current_photo_id_value = None
    photo_status_text = "Фото/Видео: <i>Не загружено</i>"

    async with AsyncSessionFactory() as session, session.begin():
        db_text_val = await get_bot_setting(session, "antispam_dummy_text")
        if db_text_val: current_text_value = f"<em>{db_text_val}</em>" # Курсив для текста
        current_photo_id_value = await get_bot_setting(session, "antispam_dummy_photo_id")
        if current_photo_id_value: photo_status_text = "Фото/Видео: <b>Есть</b>"

    text_to_show += f"<u>Текущий текст:</u>\n{current_text_value}\n\n"
    text_to_show += f"{photo_status_text}\n\nВыберите действие:"
    
    kb = get_antispam_dummy_management_keyboard()
    
    # Определяем, как обновить/отправить сообщение
    try: 
        message_had_photo = bool(message_to_act_on.photo)
        
        if isinstance(target_message_or_cq, CallbackQuery): # Пришел callback - значит, сообщение уже есть
            if current_photo_id_value: # Мы хотим показать фото
                if message_had_photo: # И у сообщения уже было фото
                    await message_to_act_on.edit_caption(caption=text_to_show, reply_markup=kb, parse_mode="HTML")
                else: # У сообщения не было фото, а теперь хотим с фото
                    await message_to_act_on.delete()
                    await target_message_or_cq.bot.send_photo(chat_id=user_id, photo=current_photo_id_value, caption=text_to_show, reply_markup=kb, parse_mode="HTML")
            else: # Мы хотим показать текстовое сообщение
                if message_had_photo: # А у сообщения было фото
                    await message_to_act_on.delete()
                    await target_message_or_cq.bot.send_message(chat_id=user_id, text=text_to_show, reply_markup=kb, parse_mode="HTML")
                else: # И было текстовое
                    await message_to_act_on.edit_text(text_to_show, reply_markup=kb, parse_mode="HTML")
            await target_message_or_cq.answer()
        else: # Это Message, отправляем новое сообщение
            if current_photo_id_value:
                await target_message_or_cq.answer_photo(photo=current_photo_id_value, caption=text_to_show, reply_markup=kb, parse_mode="HTML")
            else:
                await target_message_or_cq.answer(text_to_show, reply_markup=kb, parse_mode="HTML")

    except Exception as e:
        print(f"Error in show_antispam_dummy_config_menu (displaying): {e}\n{traceback.format_exc()}")
        # Фоллбэк на простое новое сообщение
        final_fallback_text = f"Настройки АС-пустышки:\nТекст: {await get_bot_setting(AsyncSessionFactory(), 'antispam_dummy_text') or 'не задан'}\nФото: {'есть' if await get_bot_setting(AsyncSessionFactory(), 'antispam_dummy_photo_id') else 'нет'}"
        await message_to_act_on.answer(final_fallback_text, reply_markup=kb) # Отправляем новое сообщение с кнопками
        if isinstance(target_message_or_cq, CallbackQuery): await target_message_or_cq.answer("Ошибка отображения, но меню доступно.")


# Хэндлер для Reply-кнопки "🤖 Анти-спам Пустышка"
@admin_router.message(F.text == "🤖 Анти-спам Пустышка", StateFilter(AdminStates.in_panel))
async def admin_manage_antispam_dummy_via_reply_button(message: Message, state: FSMContext):
    await message.answer("Настройки универсальной анти-спам пустышки:", reply_markup=ReplyKeyboardRemove())
    # Передаем state, так как show_antispam_dummy_config_menu его ожидает (хотя пока не использует активно)
    await show_antispam_dummy_config_menu(message, state) 

# Callback "🔙 Назад в Админ-меню" из управления анти-спам пустышкой
@admin_router.callback_query(F.data == "admin_as_back_to_main_panel_cb") # Добавлен StateFilter для контекста
async def admin_return_from_antispam_to_main_panel_cb(callback_query: CallbackQuery, state: FSMContext): # Убедимся, что StateFilter корректен или убрать его пока
    await state.set_state(AdminStates.in_panel) 
    try: 
        # Удаляем сообщение с inline-кнопками
        if callback_query.message.photo: await callback_query.message.delete() 
        else: await callback_query.message.edit_text("Возврат в главное меню...", reply_markup=None) # Убираем кнопки
    except Exception as e_del_back:
        print(f"Minor error trying to clean up message on back to admin panel: {e_del_back}")
    
    await callback_query.message.answer(ADMIN_GREETING, reply_markup=admin_main_menu_keyboard) # Отправляем Reply-меню
    await callback_query.answer()

# --- Изменение ТЕКСТА анти-спам пустышки ---
@admin_router.callback_query(F.data == "admin_as_edit_text")
async def admin_ask_antispam_text(callback_query: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.editing_antispam_dummy_text)
    prompt_message = "Введите новый текст для анти-спам пустышки:"
    try:
        if callback_query.message.photo:
            await callback_query.message.delete()
            await callback_query.message.answer(prompt_message, reply_markup=cancel_field_edit_keyboard) # Используем Reply кнопку
        else:
            await callback_query.message.edit_text(prompt_message, reply_markup=None) # Убираем inline
            await callback_query.message.answer("Если передумали, нажмите:", reply_markup=cancel_field_edit_keyboard) # Отправляем Reply кнопку
    except Exception as e:
        print(f"Error editing message to ask for antispam text: {e}")
        await callback_query.message.answer(prompt_message, reply_markup=cancel_field_edit_keyboard)
    await callback_query.answer()

@admin_router.message(AdminStates.editing_antispam_dummy_text, F.text)
async def admin_save_antispam_text(message: Message, state: FSMContext):
    new_text = message.text.strip()
    if not (10 <= len(new_text) <= 1000): # Примерная валидация
        await message.answer("Текст должен быть от 10 до 1000 символов. Попробуйте снова или введите /cancel_admin_action для отмены.")
        return
    
    async with AsyncSessionFactory() as session, session.begin():
        await update_bot_setting(session, "antispam_dummy_text", new_text)
    
    await message.answer("Текст анти-спам пустышки обновлен!")
    # await state.set_state(AdminStates.in_panel) # Или None, если show_... не ожидает определенного состояния
    await show_antispam_dummy_config_menu(message, state)


# Команда для отмены текущего действия админа (например, ввода текста)

async def admin_cancel_current_input_action(message: Message, state: FSMContext):
    await message.answer("Действие отменено.", reply_markup=ReplyKeyboardRemove())
    await state.set_state(AdminStates.in_panel) # Возвращаем в состояние главного меню админки (откуда обычно идет управление пустышками)
    await show_antispam_dummy_config_menu(message, state) # state теперь AdminStates.in_panel


# --- Управление ФОТО/ВИДЕО для Анти-спам Пустышки ---

# Callback для кнопки "🖼️ Загрузить/Изменить фото"
@admin_router.callback_query(F.data == "admin_as_edit_photo", StateFilter(AdminStates.in_panel, None)) # Можно AdminStates.in_panel или если мы в подменю
async def admin_ask_antispam_photo(callback_query: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.editing_antispam_dummy_photo)
    prompt_message = "Отправьте новое фото или видео для анти-спам пустышки."
    
    # Используем существующую cancel_field_edit_keyboard, текст кнопки можно поменять, если нужно
    # cancel_photo_keyboard = ReplyKeyboardMarkup(
    #     keyboard=[[KeyboardButton(text="❌ Отменить загрузку фото")]],
    #     resize_keyboard=True, one_time_keyboard=True
    # )

    try:
        # Редактируем предыдущее сообщение, убирая inline кнопки
        if callback_query.message.photo or callback_query.message.video: # Если оно было с медиа
            await callback_query.message.delete() # Проще удалить и отправить новое
            await callback_query.message.answer(prompt_message, reply_markup=cancel_field_edit_keyboard)
        else: # Если было текстовым
            await callback_query.message.edit_text(prompt_message, reply_markup=None) # Убираем inline
            await callback_query.message.answer("Если передумали, нажмите:", reply_markup=cancel_field_edit_keyboard) # Добавляем Reply кнопку отмены
    except Exception as e:
        print(f"Error editing message to ask for antispam photo: {e}")
        await callback_query.message.answer(prompt_message, reply_markup=cancel_field_edit_keyboard)
    await callback_query.answer()

# Хэндлер для получения фото или видео (в состоянии editing_antispam_dummy_photo)
@admin_router.message(AdminStates.editing_antispam_dummy_photo, F.photo | F.video) # Ловим и фото, и видео
async def admin_save_antispam_media(message: Message, state: FSMContext):
    file_id = None
    if message.photo:
        file_id = message.photo[-1].file_id # Берем наибольшее разрешение
    elif message.video:
        file_id = message.video.file_id
    
    if not file_id:
        await message.answer("Не удалось обработать файл. Пожалуйста, отправьте фото или видео.", reply_markup=cancel_field_edit_keyboard)
        return

    async with AsyncSessionFactory() as session, session.begin():
        await update_bot_setting(session, "antispam_dummy_photo_id", file_id)
    
    await message.answer("Медиафайл для анти-спам пустышки обновлен!", reply_markup=ReplyKeyboardRemove())
    # Возвращаемся к показу настроек пустышки
    # await state.set_state(AdminStates.in_panel) # Или None, show_antispam_dummy_config_screen сама разберется
    await show_antispam_dummy_config_menu(message, state)

# Если в состоянии editing_antispam_dummy_photo прислали не медиа, а текст (кроме кнопки отмены)
@admin_router.message(AdminStates.editing_antispam_dummy_photo, F.text)
async def admin_wrong_file_for_antispam_media(message: Message, state: FSMContext):
    if message.text == "❌ Отменить изменение": # Предполагая, что это текст кнопки из cancel_field_edit_keyboard
        return await admin_cancel_current_field_edit_by_button(message, state) # Используем общий хэндлер отмены

    await message.answer(
        "Пожалуйста, отправьте фото или видео, или нажмите кнопку '❌ Отменить изменение'.",
        reply_markup=cancel_field_edit_keyboard # Предлагаем кнопку отмены
    )

# Callback для кнопки "🗑️ Удалить фото"
@admin_router.callback_query(F.data == "admin_as_delete_photo", StateFilter(AdminStates.in_panel, None)) # Можно из in_panel или другого состояния пустышки
async def admin_delete_antispam_media(callback_query: CallbackQuery, state: FSMContext):
    async with AsyncSessionFactory() as session, session.begin():
        await update_bot_setting(session, "antispam_dummy_photo_id", None) # Устанавливаем в None
    
    await callback_query.answer("Фото/видео для анти-спам пустышки удалено.")
    # Обновляем отображение настроек
    await show_antispam_dummy_config_menu(callback_query, state)


# --- УВЕДОМЛЕНИЯ АДМИНИСТРАТОРАМ О ЖАЛОБАХ ---

async def notify_admins_about_complaint(bot: Bot, complaint: Complaint):
    """Отправляет уведомление о новой жалобе всем администраторам."""
    
    reporter_display_info = f"ID: {complaint.reporter_user_id or 'неизвестен'}"
    
    reported_entity_type_text = "неизвестный объект" 
    reported_user_details_text = "Пользователь не определен" 
    profile_details_snippet = "" 
    photo_to_send_for_admin = None
    target_user_for_action_buttons = complaint.reported_user_id

    try:
        async with AsyncSessionFactory() as session, session.begin():
            # 1. Информация об отправителе жалобы
            if complaint.reporter_user_id:
                reporter = await session.get(User, complaint.reporter_user_id)
                if reporter:
                    reporter_display_info = f"{reporter.first_name or ''} (@{reporter.username or 'N/A'}, ID: {reporter.telegram_id})"

            # 2. Информация о цели жалобы
            if complaint.reported_user_id:
                reported_user_obj = await session.get(User, complaint.reported_user_id)
                if reported_user_obj:
                    # Заполняем по умолчанию, потом уточним, если это профиль
                    reported_user_details_text = f"Пользователь: {reported_user_obj.first_name or ''} (@{reported_user_obj.username or 'N/A'}, ID: {reported_user_obj.telegram_id})"
                else:
                    reported_user_details_text = f"Пользователь ID {complaint.reported_user_id} (не найден в таблице Users)"
            
            # Если жалоба на профиль работодателя
            if complaint.reported_employer_profile_id:
                reported_entity_type_text = "анкету РАБОТОДАТЕЛЯ"
                emp_profile = await session.get(EmployerProfile, complaint.reported_employer_profile_id)
                if emp_profile:
                    photo_to_send_for_admin = emp_profile.photo_file_id
                    # Уточняем reported_user_details_text на основе владельца профиля
                    owner_id = emp_profile.user_id
                    if not target_user_for_action_buttons: target_user_for_action_buttons = owner_id # Если вдруг не был установлен
                    
                    owner = await session.get(User, owner_id)
                    if owner: 
                        reported_user_details_text = f"Работодатель: {owner.first_name or ''} (@{owner.username or 'N/A'}, ID: {owner.telegram_id})"
                    
                    wf_display = getattr(emp_profile.work_format, 'name', "Не указан").title()
                    min_age_d = emp_profile.min_age_candidate if emp_profile.min_age_candidate is not None else "Не указан"
                    profile_details_snippet = (
                        f"\n\n<b>--- Детали анкеты (работодатель ID: {emp_profile.id}) ---</b>\n"
                        f"<b>Компания:</b> {emp_profile.company_name}\n<b>Город:</b> {emp_profile.city}\n"
                        f"<b>Позиция:</b> {emp_profile.position}\n<b>ЗП:</b> {emp_profile.salary}\n"
                        f"<b>Мин. возраст:</b> {min_age_d}\n<b>Формат:</b> {wf_display}\n"
                        f"<b>Описание:</b>\n{emp_profile.description or 'Нет'}\n"
                        f"<b>Активна:</b> {'Да' if emp_profile.is_active else 'Нет'}"
                    )
            # Если жалоба на профиль соискателя
            elif complaint.reported_applicant_profile_id:
                reported_entity_type_text = "анкету СОИСКАТЕЛЯ"
                app_profile = await session.get(ApplicantProfile, complaint.reported_applicant_profile_id)
                if app_profile:
                    owner_id = app_profile.user_id
                    if not target_user_for_action_buttons: target_user_for_action_buttons = owner_id
                    
                    owner = await session.get(User, owner_id)
                    if owner: 
                        reported_user_details_text = f"Соискатель: {owner.first_name or ''} (@{owner.username or 'N/A'}, ID: {owner.telegram_id})"
                    
                    gender_d = getattr(app_profile.gender, 'name', "Не указан").title()
                    contact_phone_text = "Не указан"
                    if owner and owner.contact_phone: # Телефон берем из User
                        contact_phone_text = f"+{owner.contact_phone}"
                    
                    profile_details_snippet = (
                        f"\n\n<b>--- Детали анкеты (соискатель ID: {app_profile.id}) ---</b>\n"
                        f"<b>Город:</b> {app_profile.city}\n<b>Пол:</b> {gender_d}\n"
                        f"<b>Возраст:</b> {app_profile.age}\n<b>Опыт:</b>\n{app_profile.experience or 'Нет'}\n"
                        f"<b>Контактный телефон:</b> {contact_phone_text}"
                    )
            # Если жалоба только на пользователя (уже обработано в reported_user_details_text)
            elif complaint.reported_user_id and not complaint.reported_employer_profile_id and not complaint.reported_applicant_profile_id:
                 reported_entity_type_text = "ПОЛЬЗОВАТЕЛЯ"

        # Формируем полный текст для PUSH
        full_notification_text = (
            f"🚨 Новая жалоба! (ID: {complaint.id})\n\n"
            f"<b>Отправитель:</b>\n{reporter_display_info}\n\n"
            f"<b>Жалоба на {reported_entity_type_text}:</b>\n{reported_user_details_text}"
            f"{profile_details_snippet}" # Добавляем детали анкеты, если они есть
        )

        # Формирование кнопок
        action_buttons = []
        # Кнопка удаления/сброса анкеты
        if complaint.reported_employer_profile_id:
            action_buttons.append([InlineKeyboardButton(
                text="🗑️ Удалить анкету раб-ля (сброс)", 
                callback_data=f"admin_complaint_delete_reset_emp_profile:{complaint.id}:{complaint.reported_employer_profile_id}:{target_user_for_action_buttons or emp_profile.user_id}"
            )])
        elif complaint.reported_applicant_profile_id:
             action_buttons.append([InlineKeyboardButton(
                text="🗑️ Удалить анкету соиск-ля (сброс)", 
                callback_data=f"admin_complaint_delete_reset_app_profile:{complaint.id}:{complaint.reported_applicant_profile_id}:{target_user_for_action_buttons or app_profile.user_id}"
            )])
        
        # Кнопка блокировки пользователя
        if target_user_for_action_buttons:
            action_buttons.append([InlineKeyboardButton(
                text="🚫 Заблокировать этого пользователя", 
                callback_data=f"admin_complaint_ban_user:{complaint.id}:{target_user_for_action_buttons}"
            )])
        
        # Кнопка "Пометить как обработанную"
        action_buttons.append([InlineKeyboardButton(
            text="☑️ Пометить как обработанную", 
            callback_data=f"admin_complaint_resolve:{complaint.id}"
        )])
        
        admin_action_kb = InlineKeyboardMarkup(inline_keyboard=action_buttons)

        # Отправка уведомлений админам
        for admin_id_loop in ADMIN_IDS:
            try:
                # Отправляем фото, если это жалоба на работодателя и у него есть фото
                if photo_to_send_for_admin and complaint.reported_employer_profile_id:
                    await bot.send_photo(
                        chat_id=admin_id_loop,
                        photo=photo_to_send_for_admin,
                        caption=full_notification_text[:1024], # Ограничение длины caption
                        reply_markup=admin_action_kb,
                        parse_mode="HTML"
                    )
                else: # В остальных случаях (жалоба на соискателя или на работодателя без фото) - текстовое сообщение
                    await bot.send_message(
                        chat_id=admin_id_loop,
                        text=full_notification_text,
                        reply_markup=admin_action_kb,
                        parse_mode="HTML"
                    )
                print(f"DEBUG: Complaint PUSH sent to admin {admin_id_loop} for complaint ID {complaint.id}")
            except Exception as e_send:
                print(f"ERROR sending PUSH to admin {admin_id_loop} for complaint {complaint.id}: {e_send}\n{traceback.format_exc()}")
    
    except Exception as e_outer:
        print(f"CRITICAL ERROR in notify_admins_about_complaint (complaint_id {complaint.id if complaint else 'Unknown'}): {e_outer}\n{traceback.format_exc()}")



@admin_router.callback_query(F.data.startswith("admin_complaint_resolve:"))
async def admin_resolve_complaint(callback_query: CallbackQuery, state: FSMContext):
    try:
        complaint_id = int(callback_query.data.split(":")[1])
    except (IndexError, ValueError):
        await callback_query.answer("Ошибка: неверный ID жалобы.", show_alert=True)
        return

    async with AsyncSessionFactory() as session, session.begin():
        complaint = await session.get(Complaint, complaint_id)
        if complaint:
            if complaint.status != ComplaintStatusEnum.NEW:
                await callback_query.answer("Эта жалоба уже была обработана.", show_alert=True)
                # Можно обновить сообщение, убрав кнопки
                try: await callback_query.message.edit_reply_markup(reply_markup=None)
                except: pass
                return

            complaint.status = ComplaintStatusEnum.RESOLVED # Или VIEWED, а потом админ меняет на RESOLVED
            complaint.updated_at = func.now() # SQLAlchemy обычно сама это делает, но для явности
            # session.add(complaint) # Не обязательно, если объект уже отслеживается
            await callback_query.answer("Жалоба помечена как обработанная.", show_alert=True)
            # Обновляем сообщение у админа, который нажал кнопку (например, убираем кнопки)
            try:
                new_text = callback_query.message.text + "\n\n<b>Статус: Обработана ✅</b>"
                await callback_query.message.edit_text(text=new_text, reply_markup=None, parse_mode="HTML")
            except Exception as e:
                print(f"Failed to edit admin complaint message after resolving: {e}")
        else:
            await callback_query.answer("Жалоба не найдена.", show_alert=True)

# Обработка "Заблокировать пользователя"
@admin_router.callback_query(F.data.startswith("admin_complaint_ban_user:"))
async def admin_ban_user_from_complaint(callback_query: types.CallbackQuery, state: FSMContext):
    acting_admin_id = callback_query.from_user.id
    # ... (парсинг complaint_id и user_to_ban_id из callback_query.data) ...
    try:
        parts = callback_query.data.split(":")
        complaint_id = int(parts[1])
        user_to_ban_id = int(parts[2])
    except (IndexError, ValueError): # ... (обработка ошибки) ...
        return

    action_taken_message = "Действие не выполнено."
    message_text_for_admin = callback_query.message.text or callback_query.message.caption or "Информация о жалобе"

    async with AsyncSessionFactory() as session, session.begin():
        # 1. Загружаем жалобу с блокировкой
        complaint_result = await session.execute(
            select(Complaint).where(Complaint.id == complaint_id).with_for_update() # FOR UPDATE здесь
        )
        complaint = complaint_result.scalar_one_or_none()

        if not complaint:
            await callback_query.answer("Жалоба не найдена.", show_alert=True); return
        if complaint.status != ComplaintStatusEnum.NEW:
            await callback_query.answer(f"Жалоба уже обработана (статус: {complaint.status.name}).", show_alert=True)
            # ... (обновить сообщение у админа)
            return

        # 2. Блокируем пользователя
        user_to_ban_obj = await session.get(User, user_to_ban_id) # Получаем объект User
        if user_to_ban_obj:
            if not user_to_ban_obj.is_banned:
                user_to_ban_obj.is_banned = True
                # session.add(user_to_ban_obj) # SQLAlchemy отследит изменение
                action_taken_message = f"Пользователь ID {user_to_ban_id} заблокирован."
                print(f"DEBUG: Admin {acting_admin_id} BANNED User ID {user_to_ban_id}.")
            else:
                action_taken_message = f"Пользователь ID {user_to_ban_id} уже был заблокирован."
                print(f"DEBUG: User ID {user_to_ban_id} was ALREADY BANNED (Admin: {acting_admin_id}).")
            
            complaint.status = ComplaintStatusEnum.RESOLVED # Обновляем статус жалобы
            complaint.updated_at = func.now()
            # session.add(complaint) # SQLAlchemy отследит
        else:
            action_taken_message = f"Пользователь ID {user_to_ban_id} не найден для блокировки."
            print(f"ERROR: User ID {user_to_ban_id} to ban NOT FOUND by admin {acting_admin_id}.")
 

    await callback_query.answer(action_taken_message, show_alert=True)
    try: # Обновляем PUSH-сообщение у админа
        new_text = message_text_for_admin + f"\n\n<b>Действие админа {acting_admin_id}:</b> {action_taken_message}"
        if callback_query.message.photo: await callback_query.message.edit_caption(caption=new_text, reply_markup=None, parse_mode="HTML")
        else: await callback_query.message.edit_text(text=new_text, reply_markup=None, parse_mode="HTML")
    except Exception as e_edit:
        print(f"Failed to edit admin PUSH after ban action: {e_edit}")

# Обработка "Удалить анкету нарушителя"
@admin_router.callback_query(F.data.startswith("admin_complaint_delete_reset_"))
async def admin_delete_reset_profile_from_complaint(callback_query: types.CallbackQuery, state: FSMContext):
    acting_admin_id = callback_query.from_user.id
    action_performed_message = "Действие не было выполнено." # Сообщение по умолчанию
    new_text_for_admin_push = callback_query.message.text or callback_query.message.caption or "PUSH-уведомление о жалобе"

    try:
        parts = callback_query.data.split(":")
        action_prefix = parts[0]
        complaint_id = int(parts[1])
        profile_id_to_delete = int(parts[2])
        owner_user_id = int(parts[3])
    except (IndexError, ValueError):
        await callback_query.answer("Ошибка в данных команды.", show_alert=True)
        return

    profile_model_to_delete = None
    entity_name_for_logs = ""
    if action_prefix == "admin_complaint_delete_reset_emp_profile":
        profile_model_to_delete = EmployerProfile
        entity_name_for_logs = "работодателя"
    elif action_prefix == "admin_complaint_delete_reset_app_profile":
        profile_model_to_delete = ApplicantProfile
        entity_name_for_logs = "соискателя"
    else:
        await callback_query.answer("Неизвестный тип действия.", show_alert=True)
        return

    async with AsyncSessionFactory() as session, session.begin():
        # 1. Загружаем жалобу с блокировкой
        complaint_select_stmt = select(Complaint).where(Complaint.id == complaint_id).with_for_update()
        complaint_obj = (await session.execute(complaint_select_stmt)).scalar_one_or_none()

        if not complaint_obj:
            await callback_query.answer("Жалоба не найдена (возможно, уже обработана).", show_alert=True)
            try: await callback_query.message.delete()
            except: pass
            return

        if complaint_obj.status != ComplaintStatusEnum.NEW:
            await callback_query.answer(f"Эта жалоба уже была обработана (статус: {complaint_obj.status.name}).", show_alert=True)
            new_text_for_admin_push += f"\n\n<b>Статус: Уже обработана ({complaint_obj.status.name}) админом</b>"
            try:
                if callback_query.message.photo: await callback_query.message.edit_caption(caption=new_text_for_admin_push, reply_markup=None, parse_mode="HTML")
                else: await callback_query.message.edit_text(text=new_text_for_admin_push, reply_markup=None, parse_mode="HTML")
            except: pass
            return

        # --- Если статус NEW, выполняем действия ---
        profile_deleted = False
        role_reset = False

        # 2. Удаляем профиль
        delete_profile_stmt = delete(profile_model_to_delete).where(profile_model_to_delete.id == profile_id_to_delete)
        profile_delete_result = await session.execute(delete_profile_stmt)
        if profile_delete_result.rowcount > 0:
            profile_deleted = True
            print(f"DEBUG: Admin {acting_admin_id} deleted {entity_name_for_logs} profile ID {profile_id_to_delete}")
        
        # 3. Сбрасываем роль пользователя
        user_role_update_stmt = update(User).where(User.telegram_id == owner_user_id).values(role=None)
        user_update_result = await session.execute(user_role_update_stmt)
        if user_update_result.rowcount > 0:
            role_reset = True
            print(f"DEBUG: Admin {acting_admin_id} reset role for User ID {owner_user_id}")

        # 4. Обновляем статус жалобы на RESOLVED
        complaint_status_update_stmt = (
            update(Complaint)
            .where(Complaint.id == complaint_id) # Статус уже проверили, можно не дублировать
            .values(status=ComplaintStatusEnum.RESOLVED, updated_at=func.now())
        )
        await session.execute(complaint_status_update_stmt)
        print(f"DEBUG: Complaint ID {complaint_id} status set to RESOLVED by admin {acting_admin_id}")
        
        # Формируем сообщение
        if profile_deleted and role_reset:
            action_performed_message = f"Анкета {entity_name_for_logs} (ID {profile_id_to_delete}) удалена, роль пользователя (ID {owner_user_id}) сброшена."
        # ... (другие варианты action_performed_message)
        else:
            action_performed_message = f"Жалоба {complaint_id} обработана (Удаление анкеты/сброс роли). Анкета удалена: {'Да' if profile_deleted else 'Нет/Не найдена'}. Роль сброшена: {'Да' if role_reset else 'Нет/Пользователь не найден'}."
            
        new_text_for_admin_push += f"\n\n<b>Действие админа {acting_admin_id}:</b> {action_performed_message}"
        # Коммит будет при выходе из session.begin()

    # Отправляем финальное уведомление и обновляем сообщение у админа
    await callback_query.answer(action_performed_message, show_alert=True)
    try:
        if callback_query.message.photo:
            await callback_query.message.edit_caption(caption=new_text_for_admin_push, reply_markup=None, parse_mode="HTML")
        else:
            await callback_query.message.edit_text(text=new_text_for_admin_push, reply_markup=None, parse_mode="HTML")
    except Exception as e_edit_final:
        print(f"Failed to edit admin PUSH after delete/reset action: {e_edit_final}")


@admin_router.callback_query(F.data.startswith("admin_complaint_resolve:"))
async def admin_resolve_complaint_action(callback_query: types.CallbackQuery, state: FSMContext):
    acting_admin_id = callback_query.from_user.id
    try:
        complaint_id = int(callback_query.data.split(":")[1])
    except (IndexError, ValueError):
        await callback_query.answer("Ошибка: неверный ID жалобы.", show_alert=True)
        return

    async with AsyncSessionFactory() as session, session.begin():
        complaint = await session.get(Complaint, complaint_id)
        
        if not complaint:
            await callback_query.answer("Жалоба не найдена (возможно, уже удалена).", show_alert=True)
            try: await callback_query.message.delete() # Удаляем кнопку у этого админа
            except: pass
            return

        if complaint.status != ComplaintStatusEnum.NEW:
            await callback_query.answer(f"Эта жалоба уже была обработана (статус: {complaint.status.name}).", show_alert=True)
            try: # Пытаемся отредактировать сообщение, убрав кнопки
                new_text = callback_query.message.text + f"\n\n<b>Статус: Уже обработана ({complaint.status.name})</b>"
                await callback_query.message.edit_text(text=new_text, reply_markup=None, parse_mode="HTML")
            except Exception: # Если не вышло, просто удаляем
                try: await callback_query.message.delete() 
                except: pass
            return

        # Если статус NEW, обрабатываем
        complaint.status = ComplaintStatusEnum.RESOLVED # Или другой подходящий статус
        complaint.updated_at = func.now() 
        # Можно добавить поле 'processed_by_admin_id = acting_admin_id' в модель Complaint
        # session.add(complaint) # Не обязательно, если объект отслеживается
        
        await session.commit() # Коммитим изменение статуса жалобы
        print(f"DEBUG: Admin {acting_admin_id} resolved complaint ID {complaint_id}.")

    # Действие выполнено, теперь отвечаем на callback и удаляем сообщение у нажавшего админа
    await callback_query.answer("Жалоба помечена как обработанная.", show_alert=True)
    try:
        await callback_query.message.delete()
        print(f"DEBUG: Admin's PUSH message for resolved complaint {complaint_id} deleted for admin {acting_admin_id}.")
    except Exception as e_del_admin_msg:
        print(f"DEBUG: Could not delete admin's PUSH message {callback_query.message.message_id}: {e_del_admin_msg}")


# Хэндлер для кнопки "➕ Добавить Пустышку Работодателя"
@admin_router.callback_query(F.data == "admin_action_create_dummy")
async def admin_start_add_dummy_employer_cb(callback_query: CallbackQuery, state: FSMContext): # Пока без StateFilter
    await state.set_state(AdminAddDummyEmployer.waiting_for_city)
    
    # Убираем inline-кнопки из предыдущего сообщения
    try:
        await callback_query.message.edit_reply_markup(reply_markup=None)
    except Exception as e:
        print(f"Could not edit reply markup for create_dummy: {e}")

    await callback_query.message.answer( # Отправляем новое сообщение
        "Начинаем создание фейковой анкеты работодателя (пустышки).\n"
        "Для отмены на любом шаге введите /cancel_add_dummy\n\n"
        "Укажите город для этой анкеты:",
        reply_markup=ReplyKeyboardRemove() # Убираем Reply кнопки, если они были
    )
    await callback_query.answer()
    
    
# Отмена создания пустышки
async def _cancel_dummy_creation(message: Message, state: FSMContext):
    await state.clear() # Сначала очищаем все данные FSM от создания пустышки
    await message.answer("Создание пустышки отменено.", reply_markup=ReplyKeyboardRemove())
    
    # Устанавливаем состояние главного меню админки ПЕРЕД его показом
    await state.set_state(AdminStates.in_panel) 
    await message.answer(ADMIN_GREETING, reply_markup=admin_main_menu_keyboard)

@admin_router.message(Command("cancel_add_dummy"), StateFilter(AdminAddDummyEmployer))
@admin_router.message(F.text.casefold() == "отмена", StateFilter(AdminAddDummyEmployer)) # Можно убрать, если только команда
async def admin_cancel_add_dummy_employer_command(message: Message, state: FSMContext):
    await _cancel_dummy_creation(message, state)

# Город
@admin_router.message(AdminAddDummyEmployer.waiting_for_city, F.text)
async def admin_dummy_emp_city_input(message: Message, state: FSMContext):
    city_parts = [part.capitalize() for part in message.text.strip().split()]
    new_city = " ".join(city_parts)
    if not (2 <= len(new_city) <= 100):
        await message.answer("Город: 2-100 симв. Попробуйте снова или /cancel_add_dummy"); return
    await state.update_data(dummy_city=new_city)
    await state.set_state(AdminAddDummyEmployer.waiting_for_company_name)
    await message.answer("Название компании/проекта для пустышки:")

# Название компании
@admin_router.message(AdminAddDummyEmployer.waiting_for_company_name, F.text)
async def admin_dummy_emp_company_name_input(message: Message, state: FSMContext):
    name = message.text.strip()
    if not (2 <= len(name) <= 200):
        await message.answer("Название: 2-200 симв. Попробуйте снова или /cancel_add_dummy"); return
    await state.update_data(dummy_company_name=name)
    await state.set_state(AdminAddDummyEmployer.waiting_for_position)
    await message.answer("Позиция для пустышки:")

# Позиция
@admin_router.message(AdminAddDummyEmployer.waiting_for_position, F.text)
async def admin_dummy_emp_position_input(message: Message, state: FSMContext):
    position = message.text.strip()
    if not (3 <= len(position) <= 150):
        await message.answer("Позиция: 3-150 симв. Попробуйте снова или /cancel_add_dummy"); return
    await state.update_data(dummy_position=position)
    await state.set_state(AdminAddDummyEmployer.waiting_for_salary)
    await message.answer("Ставка/Зарплата (ЗП) для пустышки:")

# Зарплата
@admin_router.message(AdminAddDummyEmployer.waiting_for_salary, F.text)
async def admin_dummy_emp_salary_input(message: Message, state: FSMContext):
    salary = message.text.strip()
    if not (3 <= len(salary) <= 100):
        await message.answer("ЗП: 3-100 симв. Попробуйте снова или /cancel_add_dummy"); return
    await state.update_data(dummy_salary=salary)
    await state.set_state(AdminAddDummyEmployer.waiting_for_min_age)
    await message.answer("Минимальный возраст для кандидата (числом, или '-' если не важно):")

# Минимальный возраст
@admin_router.message(AdminAddDummyEmployer.waiting_for_min_age, F.text)
async def admin_dummy_emp_min_age_input(message: Message, state: FSMContext):
    min_age_text = message.text.strip()
    new_min_age = None
    if min_age_text == "-" or min_age_text == "0": new_min_age = None 
    elif min_age_text.isdigit():
        age_val = int(min_age_text)
        if 16 <= age_val <= 70: new_min_age = age_val
        else: await message.answer("Возраст 16-70 или '-'. Снова или /cancel_add_dummy"); return
    else: await message.answer("Введите число или '-'. Снова или /cancel_add_dummy"); return
    await state.update_data(dummy_min_age=new_min_age)
    await state.set_state(AdminAddDummyEmployer.waiting_for_company_description)
    await message.answer("Небольшой текст о компании/вакансии для пустышки:")

# Описание
@admin_router.message(AdminAddDummyEmployer.waiting_for_company_description, F.text)
async def admin_dummy_emp_description_input(message: Message, state: FSMContext):
    desc = message.text.strip()
    if not (10 <= len(desc) <= 2000):
        await message.answer("Описание: 10-2000 симв. Снова или /cancel_add_dummy"); return
    await state.update_data(dummy_description=desc)
    await state.set_state(AdminAddDummyEmployer.waiting_for_work_format)
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Офлайн"), KeyboardButton(text="Онлайн")]], resize_keyboard=True, one_time_keyboard=True)
    await message.answer("Формат работы для пустышки (офлайн/онлайн):", reply_markup=kb)

# Формат работы
@admin_router.message(AdminAddDummyEmployer.waiting_for_work_format, F.text.in_({"Офлайн", "Онлайн"}))
async def admin_dummy_emp_work_format_input(message: Message, state: FSMContext):
    await state.update_data(dummy_work_format_text=message.text)
    await state.set_state(AdminAddDummyEmployer.waiting_for_photo)
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Пропустить фото")]], resize_keyboard=True, one_time_keyboard=True)
    await message.answer("Отправьте фото для пустышки (или нажмите 'Пропустить фото'):", reply_markup=kb)

@admin_router.message(AdminAddDummyEmployer.waiting_for_work_format) # Неверный выбор формата
async def admin_dummy_emp_work_format_invalid(message: Message, state: FSMContext):
    await message.answer("Выберите 'Офлайн' или 'Онлайн'.")

# Фото
@admin_router.message(AdminAddDummyEmployer.waiting_for_photo, F.photo)
async def admin_dummy_emp_photo_set(message: Message, state: FSMContext):
    await state.update_data(dummy_photo_file_id=message.photo[-1].file_id)
    await message.answer("Фото для пустышки принято.")
    await _admin_show_dummy_confirmation_message(message, state)

@admin_router.message(AdminAddDummyEmployer.waiting_for_photo, F.text == "Пропустить фото")
async def admin_dummy_emp_photo_skip(message: Message, state: FSMContext):
    await state.update_data(dummy_photo_file_id=None)
    await message.answer("Фото пропущено.", reply_markup=ReplyKeyboardRemove())
    await _admin_show_dummy_confirmation_message(message, state)

@admin_router.message(AdminAddDummyEmployer.waiting_for_photo) 
async def admin_dummy_emp_photo_invalid(message: Message, state: FSMContext):
    await message.answer("Пожалуйста, отправьте фото или нажмите 'Пропустить фото'.")

# Подтверждение пустышки
async def _admin_show_dummy_confirmation_message(message: Message, state: FSMContext):
    data = await state.get_data()
    photo_status = "Есть" if data.get("dummy_photo_file_id") else "Нет"
    text = (f"Проверьте данные для пустышки работодателя:\n\n"
            f"Город: {data.get('dummy_city', 'Не указан')}\n"
            f"Компания: {data.get('dummy_company_name', 'Не указана')}\n"
            f"Позиция: {data.get('dummy_position', 'Не указана')}\n"
            f"ЗП: {data.get('dummy_salary', 'Не указана')}\n"
            f"Мин. возраст: {data.get('dummy_min_age', 'Не указан')}\n"
            f"Описание: {data.get('dummy_description', 'Не указано')}\n"
            f"Формат: {data.get('dummy_work_format_text', 'Не указан')}\n"
            f"Фото: {photo_status}\n\n"
            f"Сохранить эту пустышку?")
    
    kb = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="✅ Да, сохранить пустышку")],
        [KeyboardButton(text="❌ Нет, отменить создание")] 
    ], resize_keyboard=True, one_time_keyboard=True) # Сделаем one_time=True
    
    await state.set_state(AdminAddDummyEmployer.waiting_for_confirmation)
    
    photo_id_to_send = data.get("dummy_photo_file_id")
    if photo_id_to_send:
        await message.answer_photo(photo=photo_id_to_send, caption=text, reply_markup=kb, parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=kb, parse_mode="HTML")

# Хэндлер "❌ Нет, отменить создание" на шаге подтверждения
@admin_router.message(AdminAddDummyEmployer.waiting_for_confirmation, F.text == "❌ Нет, отменить создание")
async def admin_dummy_cancel_at_confirmation(message: Message, state: FSMContext):
    await state.clear() # Сначала очищаем все данные FSM от создания пустышки
    await message.answer("Создание пустышки отменено.", reply_markup=ReplyKeyboardRemove())
    
    # Устанавливаем состояние главного меню админки ПЕРЕД его показом
    await state.set_state(AdminStates.in_panel) 
    await message.answer(ADMIN_GREETING, reply_markup=admin_main_menu_keyboard)


# Сохранение пустышки
@admin_router.message(AdminAddDummyEmployer.waiting_for_confirmation, F.text == "✅ Да, сохранить пустышку")
async def admin_dummy_emp_save_action(message: Message, state: FSMContext):
    data = await state.get_data()
    work_format_map = {"Офлайн": WorkFormatEnum.OFFLINE, "Онлайн": WorkFormatEnum.ONLINE}

    async with AsyncSessionFactory() as session, session.begin():
        new_dummy_profile = EmployerProfile(
            user_id=None, # Явно ставим None, если модель позволяет
            company_name=data.get('dummy_company_name'),
            city=data.get('dummy_city'),
            position=data.get('dummy_position'),
            salary=data.get('dummy_salary'),
            min_age_candidate=data.get('dummy_min_age'),
            description=data.get('dummy_description'),
            work_format=work_format_map.get(data.get('dummy_work_format_text')),
            photo_file_id=data.get('dummy_photo_file_id'),
            is_active=True, 
            is_dummy=True,  # Главный флаг
            
        )
        session.add(new_dummy_profile)
    
    await message.answer("Пустышка работодателя успешно создана!", reply_markup=ReplyKeyboardRemove())
    await state.clear() # Очищаем состояние FSM от создания пустышки
    
    # Устанавливаем состояние главного меню админки ПЕРЕД его показом
    await state.set_state(AdminStates.in_panel) 
    await message.answer(ADMIN_GREETING, reply_markup=admin_main_menu_keyboard)


@admin_router.message(F.text == "📝 Пустышки Работодателей", StateFilter(AdminStates.in_panel))
async def admin_manage_dummy_profiles_menu(message: Message, state: FSMContext):
    # Убираем Reply-клавиатуру главного админ-меню
    await message.answer(
        "Управление пустышками (фейковыми анкетами работодателей для наполнения):", 
        reply_markup=ReplyKeyboardRemove()
    )
    # Показываем Inline-меню для действий с пустышками
    await message.answer(
        "Выберите действие:",
        reply_markup=get_manage_dummy_profiles_keyboard()
    )

@admin_router.callback_query(F.data == "admin_back_to_main_from_dummies")

async def admin_back_to_main_panel_from_dummies_cb(callback_query: CallbackQuery, state: FSMContext): # Пока без StateFilter
    await state.set_state(AdminStates.in_panel) # Возвращаем в главное состояние админки
    try: 
        await callback_query.message.edit_text(ADMIN_GREETING, reply_markup=None) # Убираем inline-кнопки
        # Затем отправляем Reply-меню админки как новое сообщение
        await callback_query.message.answer("Главное меню админ-панели:",reply_markup=admin_main_menu_keyboard)
    except Exception as e_edit_back:
        print(f"Error editing message on back to admin panel from dummies: {e_edit_back}")
        # Если не вышло отредактировать/удалить, просто шлем новое с Reply-меню
        await callback_query.message.answer(ADMIN_GREETING, reply_markup=admin_main_menu_keyboard)
    await callback_query.answer()
    
    
async def build_dummy_list_keyboard(dummies: list[EmployerProfile], current_page: int = 0, per_page: int = 5) -> InlineKeyboardMarkup:
    buttons = []
    # Логика пагинации (пока упрощенная - просто отображаем часть списка)
    start_index = current_page * per_page
    end_index = start_index + per_page
    
    for dummy in dummies[start_index:end_index]:
        # Для каждой пустышки своя строка с кнопками
        buttons.append([
            InlineKeyboardButton(text=f"{dummy.id}: {dummy.company_name[:20]} ({dummy.city[:15]})", 
                                 callback_data=f"{DUMMY_PROFILE_CALLBACK_PREFIX}view:{dummy.id}"),
            #InlineKeyboardButton(text="✏️", callback_data=f"{DUMMY_PROFILE_CALLBACK_PREFIX}edit_start:{dummy.id}"), # TODO
            InlineKeyboardButton(text="🗑️", callback_data=f"{DUMMY_PROFILE_CALLBACK_PREFIX}delete_confirm:{dummy.id}")
        ])
    
            
    buttons.append([InlineKeyboardButton(text="🔙 Назад (в меню пустышек)", callback_data="admin_back_to_dummy_menu_from_list")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

@admin_router.callback_query(F.data == "admin_action_list_dummies")
async def admin_list_dummy_profiles(callback_query: CallbackQuery, state: FSMContext):
    async with AsyncSessionFactory() as session, session.begin():
        dummies_result = await session.execute(
            select(EmployerProfile).where(EmployerProfile.is_dummy == True).order_by(EmployerProfile.id)
        )
        all_dummies = dummies_result.scalars().all()

    if not all_dummies:
        await callback_query.message.edit_text(
            "Список созданных пустышек пуст. Вы можете создать новую.",
            reply_markup=get_manage_dummy_profiles_keyboard() # Возвращаем кнопки "Создать", "Список", "Назад"
        )
        await callback_query.answer()
        return

    # Пока без пагинации, просто покажем первые N (например, 5)
    keyboard = await build_dummy_list_keyboard(all_dummies, 0, 5) 
    try:
        await callback_query.message.edit_text(
            f"Список пустышек работодателей (ID: Компания (Город)):\nВыберите для действий.",
            reply_markup=keyboard
        )
    except Exception as e: # Если не вышло отредактировать (например, сообщение было с фото)
        print(f"Error editing message for dummy list: {e}")
        await callback_query.message.delete() # Удаляем старое
        await callback_query.message.answer(
             f"Список пустышек работодателей (ID: Компания (Город)):\nВыберите для действий.",
            reply_markup=keyboard
        )
    await callback_query.answer()


# Callback "Назад (в меню пустышек)" из списка пустышек
@admin_router.callback_query(F.data == "admin_back_to_dummy_menu_from_list")
async def admin_back_to_dummy_menu(callback_query: CallbackQuery, state: FSMContext):

    await callback_query.message.edit_text(
        "Управление пустышками (фейковыми анкетами работодателей для наполнения):\nВыберите действие:",
        reply_markup=get_manage_dummy_profiles_keyboard()
    )
    await callback_query.answer()

# --- Действия с конкретной пустышкой ---



# Редактирование пустышки (заглушка - запуск FSM создания)
@admin_router.callback_query(F.data.startswith(f"{DUMMY_PROFILE_CALLBACK_PREFIX}edit_start:"))
async def admin_edit_dummy_profile_start(callback_query: CallbackQuery, state: FSMContext):
    profile_id = int(callback_query.data.split(":")[-1])

    await callback_query.answer(f"Редактирование пустышки ID {profile_id} (TODO). Пока не реализовано.", show_alert=True)

# Подтверждение удаления пустышки
@admin_router.callback_query(F.data.startswith(f"{DUMMY_PROFILE_CALLBACK_PREFIX}delete_confirm:"))
async def admin_dummy_delete_confirm(callback_query: CallbackQuery, state: FSMContext):
    profile_id = int(callback_query.data.split(":")[-1])
    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"{DUMMY_PROFILE_CALLBACK_PREFIX}delete_do:{profile_id}"),
            InlineKeyboardButton(text="❌ Нет, отмена", callback_data=f"{DUMMY_PROFILE_CALLBACK_PREFIX}delete_cancel:{profile_id}")
        ]
    ])
    await callback_query.message.edit_text(f"Вы уверены, что хотите удалить пустышку ID {profile_id}?", reply_markup=confirm_kb)
    await callback_query.answer()

# Отмена удаления
@admin_router.callback_query(F.data.startswith(f"{DUMMY_PROFILE_CALLBACK_PREFIX}delete_cancel:"))
async def admin_dummy_delete_cancel(callback_query: CallbackQuery, state: FSMContext):
    await callback_query.answer("Удаление отменено.")
    await callback_query.message.edit_text("Удаление отменено. Нажмите 'Список/Редактирование пустышек', чтобы увидеть список.", reply_markup=get_manage_dummy_profiles_keyboard())


# Финальное удаление пустышки
@admin_router.callback_query(F.data.startswith(f"{DUMMY_PROFILE_CALLBACK_PREFIX}delete_do:"))
async def admin_dummy_delete_do(callback_query: CallbackQuery, state: FSMContext):
    profile_id = int(callback_query.data.split(":")[-1])
    deleted_count = 0
    async with AsyncSessionFactory() as session, session.begin():
        result = await session.execute(
            delete(EmployerProfile).where(EmployerProfile.id == profile_id, EmployerProfile.is_dummy == True)
        )
        deleted_count = result.rowcount # Количество удаленных строк
    
    if deleted_count > 0:
        await callback_query.answer(f"Пустышка ID {profile_id} удалена.", show_alert=True)
    else:
        await callback_query.answer(f"Пустышка ID {profile_id} не найдена или уже удалена.", show_alert=True)

    await callback_query.message.edit_text(
        f"Действие с пустышкой ID {profile_id} выполнено. Нажмите 'Список/Редактирование...', чтобы обновить список.", 
        reply_markup=get_manage_dummy_profiles_keyboard()
    )


@admin_router.callback_query(F.data.startswith(f"{DUMMY_PROFILE_CALLBACK_PREFIX}view:"))
async def admin_view_full_dummy_profile(callback_query: CallbackQuery, state: FSMContext):
    try:
        profile_id = int(callback_query.data.split(":")[-1])
        print(f"DEBUG admin_view_full_dummy_profile: Attempting to view profile ID {profile_id}")
    except (ValueError, IndexError):
        await callback_query.answer("Ошибка: неверный ID профиля.", show_alert=True)
        return

    dummy_profile_instance = None # Переименовал для ясности
    async with AsyncSessionFactory() as session, session.begin():
        dummy_profile_instance = await session.get(EmployerProfile, profile_id)

    if dummy_profile_instance and dummy_profile_instance.is_dummy:
        print(f"DEBUG admin_view_full_dummy_profile: Profile ID {profile_id} found and is a dummy.")
        await callback_query.answer() # Отвечаем на callback сразу

        profile_text_for_admin = format_employer_profile_for_applicant(dummy_profile_instance)
        admin_info = f"👁️‍🗨️ Просмотр Пустышки (ID: {dummy_profile_instance.id}, UserID: {dummy_profile_instance.user_id or 'N/A'})\n"
        admin_info += "------------------------------------\n"
        full_text_to_send = admin_info + profile_text_for_admin

        back_to_list_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад к списку пустышек", callback_data="admin_action_list_dummies_from_view")]
        ])

        # Удаляем предыдущее сообщение (со списком), чтобы избежать накопления сообщений
        try:
            await callback_query.message.delete() 
            print(f"DEBUG admin_view_full_dummy_profile: Deleted previous message (ID: {callback_query.message.message_id})")
        except Exception as e_del:
            print(f"DEBUG admin_view_full_dummy_profile: Could not delete previous message: {e_del}")
            # Продолжаем, даже если не удалилось

        # Отправляем новое сообщение с полной анкетой пустышки
        try:
            if dummy_profile_instance.photo_file_id:
                print(f"DEBUG admin_view_full_dummy_profile: Sending photo {dummy_profile_instance.photo_file_id}")
                await callback_query.bot.send_photo( # Используем bot из callback_query
                    chat_id=callback_query.message.chat.id, # Отправляем в тот же чат
                    photo=dummy_profile_instance.photo_file_id,
                    caption=full_text_to_send[:1024], # Ограничение длины caption
                    parse_mode="HTML",
                    reply_markup=back_to_list_kb
                )
            else:
                print("DEBUG admin_view_full_dummy_profile: Sending text only")
                await callback_query.message.answer( # Отправляем новое сообщение в чат
                    full_text_to_send, 
                    parse_mode="HTML", 
                    reply_markup=back_to_list_kb
                )
            print(f"DEBUG admin_view_full_dummy_profile: Profile {profile_id} displayed.")
        except Exception as e_send:
            print(f"CRITICAL ERROR admin_view_full_dummy_profile: Could not send profile display: {e_send}")
            traceback.print_exc() # Для полного трейсбека
            await callback_query.message.answer("Не удалось отобразить полную анкету пустышки. Попробуйте обновить список.")
            # В случае такой ошибки, callback_query.answer() уже был вызван.
            
    else:
        if dummy_profile_instance:
            print(f"DEBUG admin_view_full_dummy_profile: Profile ID {profile_id} found, but IS NOT a dummy.")
            await callback_query.answer("Это не анкета-пустышка.", show_alert=True)
        else:
            print(f"DEBUG admin_view_full_dummy_profile: Profile ID {profile_id} NOT found.")
            await callback_query.answer("Пустышка не найдена.", show_alert=True)

        async with AsyncSessionFactory() as session, session.begin():
            dummies_result = await session.execute(select(EmployerProfile).where(EmployerProfile.is_dummy == True).order_by(EmployerProfile.id))
            all_dummies = dummies_result.scalars().all()
        keyboard_for_list = await build_dummy_list_keyboard(all_dummies, 0, 5)
        try:
            await callback_query.message.edit_text("Список пустышек:", reply_markup=keyboard_for_list)
        except: # Если предыдущее сообщение не текст или удалено
             await callback_query.message.answer("Список пустышек:", reply_markup=keyboard_for_list)

@admin_router.callback_query(F.data == "admin_action_list_dummies_from_view")
async def admin_back_to_dummy_list_from_view(callback_query: CallbackQuery, state: FSMContext):
    
    await callback_query.answer() # Отвечаем на текущий callback
    
    
    # Для обновления сообщения на список:
    # 1. Загружаем все пустышки
    async with AsyncSessionFactory() as session, session.begin():
        dummies_result = await session.execute(
            select(EmployerProfile).where(EmployerProfile.is_dummy == True).order_by(EmployerProfile.id)
        )
        all_dummies = dummies_result.scalars().all()

    if not all_dummies:
        new_text = "Список созданных пустышек пуст."
        new_kb = get_manage_dummy_profiles_keyboard() # Кнопки "Создать", "Список (TODO)", "Назад в админ-меню"
    else:
        new_text = f"Список пустышек работодателей (ID: Компания (Город)):\nВыберите для действий."
        new_kb = await build_dummy_list_keyboard(all_dummies, 0, 5)

    try:
        # Если сообщение, с которого мы пришли, имело фото, его нельзя просто отредактировать на текст
        if callback_query.message.photo:
            await callback_query.message.delete()
            await callback_query.message.answer(new_text, reply_markup=new_kb)
        else:
            await callback_query.message.edit_text(new_text, reply_markup=new_kb)
    except Exception as e:
        print(f"Error returning to dummy list from view: {e}")
        await callback_query.message.answer(new_text, reply_markup=new_kb) # Фоллбэк на новое сообщение


async def build_real_employer_list_keyboard(
    profiles: list[EmployerProfile], 
    current_page: int = 0, 
    per_page: int = 3 # Давайте по 3 для теста, потом можно увеличить до 5-7
) -> InlineKeyboardMarkup:
    buttons = []
    
    # Рассчитываем общее количество страниц
    total_items = len(profiles)
    total_pages = (total_items + per_page - 1) // per_page # Округление вверх

    # Определяем срез для текущей страницы
    start_index = current_page * per_page
    end_index = start_index + per_page
    
    paginated_profiles = profiles[start_index:end_index]

    if not paginated_profiles and current_page > 0: # Если на текущей странице пусто, а это не первая страница (например, удалили все на последней)
        current_page -=1 # Возвращаемся на предыдущую
        start_index = current_page * per_page
        end_index = start_index + per_page
        paginated_profiles = profiles[start_index:end_index]

    for profile in paginated_profiles:
        status_emoji = "🟢" if profile.is_active else "🔴"
        ban_status_owner_text = ""
        # Проверку бана владельца лучше делать один раз при загрузке всех профилей и передавать,
        # но для простоты пока оставим здесь (может быть неоптимально для многих профилей)
        async with AsyncSessionFactory() as session:
            owner = await session.get(User, profile.user_id) if profile.user_id else None
            if owner and owner.is_banned:
                ban_status_owner_text = " (🚫Владелец забанен)"
        
        buttons.append([
            InlineKeyboardButton(
                text=f"{status_emoji} ID:{profile.id} {profile.company_name[:15]}{ban_status_owner_text}", 
                callback_data=f"{REAL_EMP_PROFILE_CALLBACK_PREFIX}view:{profile.id}" # Оставляем ID профиля
            )
        ])
        buttons.append([
            InlineKeyboardButton(text="👁️Подробно", callback_data=f"{REAL_EMP_PROFILE_CALLBACK_PREFIX}view:{profile.id}"),
            InlineKeyboardButton(text="🚫Деакт." if profile.is_active else "🟢Актив.", callback_data=f"{REAL_EMP_PROFILE_CALLBACK_PREFIX}toggle_active:{profile.id}:{current_page}"), # Добавляем страницу
            InlineKeyboardButton(text="🛡️Бан вл.", callback_data=f"{REAL_EMP_PROFILE_CALLBACK_PREFIX}ban_owner:{profile.id}:{profile.user_id}:{current_page}") # Добавляем страницу
        ])
        buttons.append([ # Отдельная кнопка для удаления реальной анкеты
            InlineKeyboardButton(
                text="🗑️Удалить (Сброс)", 
                callback_data=f"{REAL_EMP_PROFILE_CALLBACK_PREFIX}delete_reset_confirm:{profile.id}:{profile.user_id}:{current_page}"
            ) # Передаем profile.id, profile.user_id (для сброса роли) и current_page
        ])
        buttons.append([InlineKeyboardButton(text="-"*20, callback_data="no_action")])


    # Кнопки пагинации
    pagination_row = []
    if current_page > 0:
        pagination_row.append(InlineKeyboardButton(text="◀️ Пред.", callback_data=f"{REAL_EMP_LIST_PAGE_CALLBACK_PREFIX}{current_page-1}"))
    
    if total_pages > 1: # Показываем номер страницы, если их больше одной
        pagination_row.append(InlineKeyboardButton(text=f"📄 {current_page+1}/{total_pages}", callback_data="no_action_page_info"))

    if end_index < total_items:
        pagination_row.append(InlineKeyboardButton(text="След. ▶️", callback_data=f"{REAL_EMP_LIST_PAGE_CALLBACK_PREFIX}{current_page+1}"))
    
    if pagination_row:
        buttons.append(pagination_row)
            
    buttons.append([InlineKeyboardButton(text="🔙 Назад в Админ-меню", callback_data="admin_back_to_main_from_real_list")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@admin_router.callback_query(F.data.startswith(f"{REAL_EMP_PROFILE_CALLBACK_PREFIX}delete_reset_confirm:"))
async def admin_confirm_delete_real_employer_profile(callback_query: CallbackQuery, state: FSMContext):
    try:
        parts = callback_query.data.split(":")
        profile_id_to_delete = int(parts[1])
        # owner_user_id = int(parts[2]) # Мы его передаем в кнопку delete_do
        # current_page = int(parts[3])  # И страницу тоже
    except (IndexError, ValueError):
        await callback_query.answer("Ошибка данных для подтверждения удаления.", show_alert=True)
        return

    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, удалить и сбросить роль", 
                                 callback_data=f"{REAL_EMP_PROFILE_CALLBACK_PREFIX}delete_reset_do:{callback_query.data[len(REAL_EMP_PROFILE_CALLBACK_PREFIX+'delete_reset_confirm:'):]}"), # Передаем все ID дальше
            InlineKeyboardButton(text="❌ Нет, отмена", 
                                 callback_data=f"{REAL_EMP_PROFILE_CALLBACK_PREFIX}delete_reset_cancel:{profile_id_to_delete}:{parts[2]}:{parts[3]}") # Передаем ID и страницу для возврата
        ]
    ])
    await callback_query.message.edit_text(
        f"Вы уверены, что хотите удалить анкету работодателя ID {profile_id_to_delete} и сбросить роль ее владельца?", 
        reply_markup=confirm_kb
    )
    await callback_query.answer()

# Хэндлер для отмены удаления РЕАЛЬНОЙ анкеты
@admin_router.callback_query(F.data.startswith(f"{REAL_EMP_PROFILE_CALLBACK_PREFIX}delete_reset_cancel:"))
async def admin_cancel_delete_real_employer_profile(callback_query: CallbackQuery, state: FSMContext):
    try:
        parts = callback_query.data.split(":")
        # profile_id = int(parts[1]) # Не нужен для отмены, но есть в callback_data
        # owner_user_id = int(parts[2])
        current_page = int(parts[3])
    except (IndexError, ValueError):
        await callback_query.answer("Ошибка данных для отмены.", show_alert=True)
        return # Если ошибка, просто отвечаем

    await callback_query.answer("Удаление отменено.")
    # Возвращаемся к списку анкет на текущей странице
    await show_real_employer_profiles_page(callback_query, state, page=current_page)

# Хэндлер для фактического удаления РЕАЛЬНОЙ анкеты и сброса роли
@admin_router.callback_query(F.data.startswith(f"{REAL_EMP_PROFILE_CALLBACK_PREFIX}delete_reset_do:"))
async def admin_do_delete_real_employer_profile(callback_query: CallbackQuery, state: FSMContext):
    try:
        parts = callback_query.data.split(":")
        profile_id_to_delete = int(parts[1])
        owner_user_id = int(parts[2])
        current_page_to_return_to = int(parts[3])
    except (IndexError, ValueError):
        await callback_query.answer("Ошибка данных для выполнения удаления.", show_alert=True)
        return

    profile_deleted = False
    role_reset = False

    async with AsyncSessionFactory() as session, session.begin():
        # Удаляем профиль работодателя
        target_profile = await session.get(EmployerProfile, profile_id_to_delete)
        if target_profile and not target_profile.is_dummy: # Убеждаемся, что это не пустышка
            await session.delete(target_profile)
            profile_deleted = True
            print(f"DEBUG: Admin {callback_query.from_user.id} deleted REAL EmployerProfile ID {profile_id_to_delete}")
        
        # Сбрасываем роль пользователя
        if owner_user_id: # Если owner_user_id был передан
            user_to_update = await session.get(User, owner_user_id)
            if user_to_update:
                user_to_update.role = None
                role_reset = True
                print(f"DEBUG: Admin {callback_query.from_user.id} reset role for User ID {owner_user_id}")
    
    action_message = "Действие не выполнено."
    if profile_deleted and role_reset:
        action_message = f"Анкета ID {profile_id_to_delete} удалена, роль пользователя ID {owner_user_id} сброшена."
    elif profile_deleted:
        action_message = f"Анкета ID {profile_id_to_delete} удалена, но не удалось сбросить роль пользователя."
    elif role_reset: # Маловероятно, если профиль не удален
        action_message = f"Роль пользователя ID {owner_user_id} сброшена, но анкета не была удалена/найдена."
    else:
        action_message = f"Анкета ID {profile_id_to_delete} не найдена или это пустышка."

    await callback_query.answer(action_message, show_alert=True)
    
    # Обновляем список, возвращаясь на ту же страницу
    await show_real_employer_profiles_page(callback_query, state, page=current_page_to_return_to)


# Хэндлер для Reply-кнопки "Просмотр/Модерация Анкет Работодателей"
@admin_router.message(F.text == "📄 Просмотр/Модерация Анкет Работодателей", StateFilter(AdminStates.in_panel))
async def admin_list_real_employer_profiles_entry(message: Message, state: FSMContext): # Переименовал для ясности
    await message.answer("Загружаю список анкет...", reply_markup=ReplyKeyboardRemove())
    # Вызываем функцию, которая теперь умеет показывать конкретную страницу
    await show_real_employer_profiles_page(message, state, page=0)

# Новая функция для отображения конкретной страницы списка (чтобы ее можно было вызвать и из callback)
async def show_real_employer_profiles_page(target_message_or_cq: Message | CallbackQuery, state: FSMContext, page: int = 0):
    message_to_act_on = target_message_or_cq.message if isinstance(target_message_or_cq, CallbackQuery) else target_message_or_cq
    per_page = 3 # Сколько анкет на странице

    async with AsyncSessionFactory() as session, session.begin():
        profiles_result = await session.execute(
            select(EmployerProfile).where(EmployerProfile.is_dummy == False).order_by(EmployerProfile.id.desc())
        )
        all_real_profiles = profiles_result.scalars().all()

    if not all_real_profiles:
        text_to_send = "В системе еще нет анкет реальных работодателей."
        # Если это callback, нужно отредактировать предыдущее сообщение
        if isinstance(target_message_or_cq, CallbackQuery):
            try: await message_to_act_on.edit_text(text_to_send, reply_markup=get_manage_dummy_profiles_keyboard()) # Возврат в меню пустышек или админки
            except: await message_to_act_on.answer(text_to_send, reply_markup=admin_main_menu_keyboard) # Фоллбэк
            await target_message_or_cq.answer()
        else: # Если это Message
            await message_to_act_on.answer(text_to_send, reply_markup=admin_main_menu_keyboard)
        return

    keyboard = await build_real_employer_list_keyboard(all_real_profiles, current_page=page, per_page=per_page)
    list_text = f"Анкеты работодателей (Стр. {page+1}):"
    
    try:
        if isinstance(target_message_or_cq, CallbackQuery):
            await message_to_act_on.edit_text(list_text, reply_markup=keyboard)
            await target_message_or_cq.answer()
        else: # Message
            await message_to_act_on.answer(list_text, reply_markup=keyboard)
    except Exception as e:
        print(f"Error displaying real_employer_profiles page {page}: {e}")
        await message_to_act_on.answer(list_text, reply_markup=keyboard) # Фоллбэк)
        

@admin_router.callback_query(F.data.startswith(REAL_EMP_LIST_PAGE_CALLBACK_PREFIX))
async def admin_paginate_real_employer_list(callback_query: CallbackQuery, state: FSMContext):
    try:
        page = int(callback_query.data[len(REAL_EMP_LIST_PAGE_CALLBACK_PREFIX):])
    except ValueError:
        await callback_query.answer("Ошибка страницы.", show_alert=True)
        return
    
    # Вызываем функцию отображения нужной страницы
    await show_real_employer_profiles_page(callback_query, state, page=page)
    # callback_query.answer() уже будет вызван внутри show_real_employer_profiles_page        


# Callback "Назад в Админ-меню" из списка реальных анкет
@admin_router.callback_query(F.data == "admin_back_to_main_from_real_list")
async def admin_back_to_main_from_real_list_cb(callback_query: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.in_panel)
    try: 
        await callback_query.message.delete() # Удаляем список
    except: pass
    await callback_query.message.answer(ADMIN_GREETING, reply_markup=admin_main_menu_keyboard)
    await callback_query.answer()
    

@admin_router.callback_query(F.data == "no_action")
async def no_action_callback(callback_query: CallbackQuery):
    await callback_query.answer() # Просто отвечаем, ничего не делая

# Просмотр полной анкеты реального работодателя
@admin_router.callback_query(F.data.startswith(f"{REAL_EMP_PROFILE_CALLBACK_PREFIX}view:"))
async def admin_view_real_employer_profile(callback_query: CallbackQuery, state: FSMContext):
    profile_id = int(callback_query.data.split(":")[-1])
    async with AsyncSessionFactory() as session, session.begin():
        profile = await session.get(EmployerProfile, profile_id)
    
    if profile and not profile.is_dummy:
        await callback_query.answer()

        wf_display = getattr(profile.work_format, 'name', "Не указан").title()
        min_age_d = profile.min_age_candidate if profile.min_age_candidate is not None else "Не указан"
        photo_i = "Есть" if profile.photo_file_id else "Нет"
        owner = "Неизвестен"
        async with AsyncSessionFactory() as session, session.begin(): # Новая сессия для получения владельца
            user_owner = await session.get(User, profile.user_id)
            if user_owner: owner = f"{user_owner.first_name or ''} (@{user_owner.username or 'N/A'}, ID: {user_owner.telegram_id})"

        profile_text = (
            f"<b>Просмотр анкеты работодателя ID: {profile.id}</b>\n"
            f"Владелец: {owner}\n"
            f"Компания: {profile.company_name}\nГород: {profile.city}\n"
            f"Позиция: {profile.position}\nЗП: {profile.salary}\n"
            f"Мин. возраст: {min_age_d}\nФормат: {wf_display}\nФото: {photo_i}\n"
            f"Описание:\n{profile.description}\n"
            f"Активна: {'Да' if profile.is_active else 'Нет'}\nЗабанен владелец: {'Да' if user_owner and user_owner.is_banned else 'Нет'}"
        )
        back_kb = InlineKeyboardMarkup(inline_keyboard=[
            # Кнопка для возврата к СПИСКУ анкет (потребует вызова admin_list_real_employer_profiles_handler)
            # Это сложно, проще вернуться в админ-меню или в меню управления пустышками
            [InlineKeyboardButton(text="🔙 Назад к списку (Обновить)", callback_data="admin_action_list_real_emp_profiles_nav")]
        ])
        
        try: await callback_query.message.delete()
        except: pass

        if profile.photo_file_id:
            await callback_query.message.answer_photo(profile.photo_file_id, caption=profile_text, reply_markup=back_kb, parse_mode="HTML")
        else:
            await callback_query.message.answer(profile_text, reply_markup=back_kb, parse_mode="HTML")
    else:
        await callback_query.answer("Анкета не найдена или это пустышка.", show_alert=True)

# Callback для возврата к списку анкет работодателей (после просмотра одной)
@admin_router.callback_query(F.data == "admin_action_list_real_emp_profiles_nav")
async def admin_back_to_real_emp_list_from_view(callback_query: CallbackQuery, state: FSMContext):
    await callback_query.answer()
    try: await callback_query.message.delete()
    except: pass
    await callback_query.message.answer(
        "Для обновления списка вернитесь в Админ-меню и снова выберите 'Просмотр/Модерация Анкет Работодателей'.",
        reply_markup=admin_main_menu_keyboard # Возвращаем главное админ-меню, чтобы он мог нажать кнопку списка
    )
    await state.set_state(AdminStates.in_panel)


# Переключение активности анкеты работодателя
@admin_router.callback_query(F.data.startswith(f"{REAL_EMP_PROFILE_CALLBACK_PREFIX}toggle_active:"))
async def admin_toggle_real_employer_profile_active(callback_query: CallbackQuery, state: FSMContext):
    parts = callback_query.data.split(":")
    profile_id = int(parts[1])
    current_page_after_action = int(parts[2]) # Получаем номер страницы
    action_message = ""
    
    # ... (логика изменения is_active, как была) ...
    async with AsyncSessionFactory() as session, session.begin():
        profile = await session.get(EmployerProfile, profile_id)
        if profile and not profile.is_dummy:
            profile.is_active = not profile.is_active
            profile.updated_at = func.now()
            action_message = f"Статус анкеты ID {profile.id} изменен."
            await callback_query.answer(action_message) # Сначала отвечаем на callback
        else: # ... (обработка ошибки)
            await callback_query.answer("Анкета не найдена.", show_alert=True); return
            
    # Возвращаемся на ТУ ЖЕ СТРАНИЦУ СПИСКА
    await show_real_employer_profiles_page(callback_query, state, page=current_page_after_action)

# Хэндлер для команды отмены, когда админ вводит ID пользователя
@admin_router.message(Command("cancel_admin_action"), StateFilter(AdminStates.find_user_id_input))
@admin_router.message(F.text == "❌ Отменить изменение поля", StateFilter(AdminStates.find_user_id_input))
async def admin_cancel_find_user_input(message: Message, state: FSMContext):
    await state.set_state(AdminStates.in_panel) # Возвращаем в главное меню админки
    await message.answer("Поиск пользователя отменен.", reply_markup=admin_main_menu_keyboard)

# Бан владельца анкеты
@admin_router.callback_query(F.data.startswith(f"{REAL_EMP_PROFILE_CALLBACK_PREFIX}ban_owner:"))
async def admin_ban_real_employer_owner(callback_query: CallbackQuery, state: FSMContext):
    parts = callback_query.data.split(":")
    profile_id = int(parts[1]) # ID профиля для контекста
    user_to_ban_id = int(parts[2])
    action_message = ""

    async with AsyncSessionFactory() as session, session.begin():
        user = await session.get(User, user_to_ban_id)
        if user:
            user.is_banned = True
            # session.add(user)
            action_message = f"Владелец анкеты ID {profile_id} (User ID: {user_to_ban_id}) заблокирован."
            await callback_query.answer(action_message, show_alert=True)
        else:
            action_message = f"Пользователь ID {user_to_ban_id} не найден для блокировки."
            await callback_query.answer(action_message, show_alert=True)
            
    await callback_query.message.edit_text(f"{callback_query.message.text}\n\n{action_message}\n(Список обновится при следующем открытии)", reply_markup=callback_query.message.reply_markup)


@admin_router.message(F.text == "ℹ️ Найти пользователя по ID", StateFilter(AdminStates.in_panel))
async def admin_find_user_start(message: Message, state: FSMContext):
    await state.set_state(AdminStates.find_user_id_input)
    # Используем существующую cancel_field_edit_keyboard для отмены ввода
    await message.answer(
        "Введите Telegram ID пользователя, которого хотите найти.\n"
        "Для отмены нажмите кнопку ниже или введите /cancel_admin_action."
    )




# Хэндлер для получения ID пользователя от админа
@admin_router.message(AdminStates.find_user_id_input, F.text)
async def admin_process_find_user_id(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("ID пользователя должен быть числом. Попробуйте снова или отмените.",
                             reply_markup=cancel_field_edit_keyboard)
        return
    
    user_to_find_id = int(message.text)
    await state.update_data(found_user_id_for_actions=user_to_find_id) # Сохраняем для кнопок действий
    
    await message.answer(f"Ищу информацию по пользователю ID: {user_to_find_id}...", reply_markup=ReplyKeyboardRemove())
    await show_found_user_details(message, state, user_to_find_id)


# Функция для отображения деталей найденного пользователя и кнопок действий
async def show_found_user_details(target_message_or_cq: Message | CallbackQuery, state: FSMContext, user_id_to_show: int):
    message_to_act_on = target_message_or_cq.message if isinstance(target_message_or_cq, CallbackQuery) else target_message_or_cq
    
    user_info_text = f"Информация по пользователю ID: {user_id_to_show}\n\n"
    user_db_object: User | None = None # Для хранения объекта User
    app_profile_for_buttons: ApplicantProfile | None = None # Для кнопки удаления анкеты соискателя
    emp_profile_for_buttons: EmployerProfile | None = None # Для кнопки удаления анкеты работодателя

    async with AsyncSessionFactory() as session, session.begin(): # Один session.begin для всех чтений
        user_db_object = await session.get(User, user_id_to_show)

        if user_db_object:
            user_info_text += f"<b>Telegram:</b> @{user_db_object.username or 'N/A'}, {user_db_object.first_name or ''} {user_db_object.last_name or ''}\n"
            user_info_text += f"<b>Роль:</b> {getattr(user_db_object.role, 'value', 'Не установлена')}\n"
            user_info_text += f"<b>Телефон:</b> {user_db_object.contact_phone or 'Не указан'}\n"
            user_info_text += f"<b>Зарегистрирован:</b> {user_db_object.registration_date.strftime('%Y-%m-%d %H:%M') if user_db_object.registration_date else 'N/A'}\n"
            user_info_text += f"<b>Статус бана:</b> {'🚫 Забанен' if user_db_object.is_banned else '✅ Активен'}\n\n"

            # Загружаем профили только если есть соответствующая роль, чтобы не делать лишних запросов
            if user_db_object.role == UserRole.APPLICANT:
                app_profile_q = await session.execute(select(ApplicantProfile).where(ApplicantProfile.user_id == user_id_to_show))
                app_profile_for_buttons = app_profile_q.scalar_one_or_none()
                if app_profile_for_buttons:
                    user_info_text += (
                        f"<b>--- Анкета Соискателя (ID: {app_profile_for_buttons.id}) ---</b>\n"
                        f"Город: {app_profile_for_buttons.city}, Пол: {getattr(app_profile_for_buttons.gender, 'name', 'N/A').title()}\n"
                        f"Возраст: {app_profile_for_buttons.age}, Опыт: {app_profile_for_buttons.experience[:100]}...\n"
                        f"Активна: {'Да' if app_profile_for_buttons.is_active else 'Нет'}\n\n"
                    )
            elif user_db_object.role == UserRole.EMPLOYER:
                emp_profile_q = await session.execute(select(EmployerProfile).where(EmployerProfile.user_id == user_id_to_show))
                emp_profile_for_buttons = emp_profile_q.scalar_one_or_none()
                if emp_profile_for_buttons:
                    photo_status = "Есть" if emp_profile_for_buttons.photo_file_id else "Нет"
                    user_info_text += (
                        f"<b>--- Анкета Работодателя (ID: {emp_profile_for_buttons.id}) ---</b>\n"
                        f"Компания: {emp_profile_for_buttons.company_name}, Город: {emp_profile_for_buttons.city}\n"
                        f"Позиция: {emp_profile_for_buttons.position}, Фото: {photo_status}\n"
                        f"Активна: {'Да' if emp_profile_for_buttons.is_active else 'Нет'}\n"
                        f"Пустышка: {'Да' if emp_profile_for_buttons.is_dummy else 'Нет'}\n\n"
                    )
        else:
            user_info_text = f"Пользователь с ID {user_id_to_show} не найден в базе."

    # Формируем Inline-кнопки действий
    action_buttons_list = [] # Переименовал для ясности
    if user_db_object: # Кнопки действий имеют смысл, только если пользователь существует
        if user_db_object.is_banned:
            action_buttons_list.append([InlineKeyboardButton(text="✅ Разблокировать", callback_data=f"{USER_DETAILS_CALLBACK_PREFIX}unban:{user_id_to_show}")])
        else:
            action_buttons_list.append([InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"{USER_DETAILS_CALLBACK_PREFIX}ban:{user_id_to_show}")])
        
        # Кнопки удаления анкет (используем user_id_to_show, т.к. хэндлеры удаления работают по user_id)
        if app_profile_for_buttons: 
             action_buttons_list.append([InlineKeyboardButton(text="🗑️ Удалить анкету соискателя", callback_data=f"{USER_DETAILS_CALLBACK_PREFIX}del_app_profile:{user_id_to_show}")])
        if emp_profile_for_buttons: 
             action_buttons_list.append([InlineKeyboardButton(text="🗑️ Удалить анкету работодателя", callback_data=f"{USER_DETAILS_CALLBACK_PREFIX}del_emp_profile:{user_id_to_show}")])

    action_buttons_list.append([InlineKeyboardButton(text="🔙 Назад в Админ-меню", callback_data=f"{USER_DETAILS_CALLBACK_PREFIX}back_to_admin_main")])
    
    # Создаем клавиатуру, только если есть кнопки (хотя кнопка "Назад" будет всегда, если user_exists)
    details_action_kb = InlineKeyboardMarkup(inline_keyboard=action_buttons_list) if action_buttons_list else None
    
    # Устанавливаем состояние для обработки нажатий на inline-кнопки
    if user_db_object: # Устанавливаем состояние, только если пользователь найден и есть кнопки действий
        await state.set_state(AdminStates.viewing_user_details)
    else: # Если пользователь не найден, сбрасываем состояние на всякий случай
        await state.set_state(AdminStates.in_panel) # или await state.clear() и затем показать главное меню
        details_action_kb = admin_main_menu_keyboard # Если пользователя нет, то и кнопок действий нет, показываем главное меню админки
        user_info_text += "\nВозврат в главное меню."


    # Отправка или редактирование сообщения
    try:
        if isinstance(target_message_or_cq, Message): # Если это первое отображение после команды /find
            await target_message_or_cq.answer(user_info_text, reply_markup=details_action_kb, parse_mode="HTML")
        elif isinstance(target_message_or_cq, CallbackQuery): # Если это обновление после действия по кнопке
            await target_message_or_cq.message.edit_text(user_info_text, reply_markup=details_action_kb, parse_mode="HTML")
            await target_message_or_cq.answer() # Отвечаем на callback
    except Exception as e:
        print(f"Error sending/editing user details: {e}\n{traceback.format_exc()}")
        # Фоллбэк на новое сообщение
        await message_to_act_on.answer(user_info_text, reply_markup=details_action_kb, parse_mode="HTML")
        if isinstance(target_message_or_cq, CallbackQuery):
            await target_message_or_cq.answer("Не удалось обновить предыдущее сообщение.")



# Callback "Назад в Админ-меню" из просмотра деталей пользователя
@admin_router.callback_query(F.data == f"{USER_DETAILS_CALLBACK_PREFIX}back_to_admin_main", StateFilter(AdminStates.viewing_user_details))
async def admin_back_to_main_from_user_details(callback_query: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.in_panel)
    try: await callback_query.message.delete() # Удаляем сообщение с деталями
    except: pass
    await callback_query.message.answer(ADMIN_GREETING, reply_markup=admin_main_menu_keyboard)
    await callback_query.answer()

# Блокировка пользователя
@admin_router.callback_query(F.data.startswith(f"{USER_DETAILS_CALLBACK_PREFIX}ban:"), StateFilter(AdminStates.viewing_user_details))
async def admin_ban_user_action(callback_query: CallbackQuery, state: FSMContext):
    try:
        user_to_ban_id = int(callback_query.data.split(":")[-1])
    except (ValueError, IndexError):
        await callback_query.answer("Ошибка: неверный ID пользователя.", show_alert=True); return

    async with AsyncSessionFactory() as session, session.begin():
        user_to_ban = await session.get(User, user_to_ban_id)
        if user_to_ban:
            if not user_to_ban.is_banned:
                user_to_ban.is_banned = True
                # session.add(user_to_ban) # SQLAlchemy отследит изменение
                await session.commit() # Коммитим изменение
                await callback_query.answer(f"Пользователь ID {user_to_ban_id} заблокирован.", show_alert=True)
                print(f"DEBUG: Admin {callback_query.from_user.id} BANNED user {user_to_ban_id}")
            else:
                await callback_query.answer(f"Пользователь ID {user_to_ban_id} уже был заблокирован.", show_alert=True)
        else:
            await callback_query.answer(f"Пользователь ID {user_to_ban_id} не найден.", show_alert=True)
            # Если пользователя нет, выходим из режима просмотра его деталей, так как их нет
            await state.set_state(AdminStates.in_panel)
            await callback_query.message.edit_text(ADMIN_GREETING, reply_markup=admin_main_menu_keyboard)
            return
            
    # Обновляем сообщение с деталями пользователя, чтобы отразить новый статус бана
    await show_found_user_details(callback_query, state, user_to_ban_id) # Передаем callback_query

# Разблокировка пользователя
@admin_router.callback_query(F.data.startswith(f"{USER_DETAILS_CALLBACK_PREFIX}unban:"), StateFilter(AdminStates.viewing_user_details))
async def admin_unban_user_action(callback_query: CallbackQuery, state: FSMContext):
    try:
        user_to_unban_id = int(callback_query.data.split(":")[-1])
    except (ValueError, IndexError):
        await callback_query.answer("Ошибка: неверный ID пользователя.", show_alert=True); return

    async with AsyncSessionFactory() as session, session.begin():
        user_to_unban = await session.get(User, user_to_unban_id)
        if user_to_unban:
            if user_to_unban.is_banned:
                user_to_unban.is_banned = False
                await session.commit()
                await callback_query.answer(f"Пользователь ID {user_to_unban_id} разблокирован.", show_alert=True)
                print(f"DEBUG: Admin {callback_query.from_user.id} UNBANNED user {user_to_unban_id}")
            else:
                await callback_query.answer(f"Пользователь ID {user_to_unban_id} не был заблокирован.", show_alert=True)
        else:
            await callback_query.answer(f"Пользователь ID {user_to_unban_id} не найден.", show_alert=True)
            await state.set_state(AdminStates.in_panel)
            await callback_query.message.edit_text(ADMIN_GREETING, reply_markup=admin_main_menu_keyboard)
            return
            
    await show_found_user_details(callback_query, state, user_to_unban_id)

@admin_router.callback_query(F.data.startswith(f"{USER_DETAILS_CALLBACK_PREFIX}del_app_profile:"), StateFilter(AdminStates.viewing_user_details))
async def admin_delete_applicant_profile_action(callback_query: CallbackQuery, state: FSMContext):
    try:
        parts = callback_query.data.split(":")
        user_id_of_profile_owner = int(parts[1]) # ID пользователя, чью анкету удаляем
        # profile_id = int(parts[2]) # ID самого профиля в ApplicantProfile, если он нужен для delete (пока удаляем по user_id)
    except (ValueError, IndexError):
        await callback_query.answer("Ошибка: неверные данные для удаления.", show_alert=True); return

    deleted = False
    async with AsyncSessionFactory() as session, session.begin():
        # Удаляем профиль соискателя по user_id
        result = await session.execute(
            delete(ApplicantProfile).where(ApplicantProfile.user_id == user_id_of_profile_owner)
        )
        if result.rowcount > 0:
            # Сбрасываем роль, если он был соискателем
            user_obj = await session.get(User, user_id_of_profile_owner)
            if user_obj and user_obj.role == UserRole.APPLICANT:
                user_obj.role = None
            deleted = True
            print(f"DEBUG: Admin {callback_query.from_user.id} deleted ApplicantProfile for user {user_id_of_profile_owner}")
        await session.commit()

    if deleted:
        await callback_query.answer("Анкета соискателя удалена, роль сброшена.", show_alert=True)
    else:
        await callback_query.answer("Анкета соискателя не найдена или уже удалена.", show_alert=True)
    
    await show_found_user_details(callback_query, state, user_id_of_profile_owner)


# Удаление анкеты работодателя
@admin_router.callback_query(F.data.startswith(f"{USER_DETAILS_CALLBACK_PREFIX}del_emp_profile:"), StateFilter(AdminStates.viewing_user_details))
async def admin_delete_employer_profile_action(callback_query: CallbackQuery, state: FSMContext):
    try:
        parts = callback_query.data.split(":")
        user_id_of_profile_owner = int(parts[1])
        # profile_id = int(parts[2]) # ID самого профиля в EmployerProfile
    except (ValueError, IndexError):
        await callback_query.answer("Ошибка: неверные данные для удаления.", show_alert=True); return
    
    deleted = False
    async with AsyncSessionFactory() as session, session.begin():
        result = await session.execute(
            delete(EmployerProfile).where(EmployerProfile.user_id == user_id_of_profile_owner)
        )
        if result.rowcount > 0:
            user_obj = await session.get(User, user_id_of_profile_owner)
            if user_obj and user_obj.role == UserRole.EMPLOYER:
                user_obj.role = None
            if user_obj: # Дополнительно, если нужно
                user_obj.active_notification_message_id = None # Ошибка: EmployerProfile.active_notification_message_id

            deleted = True
            print(f"DEBUG: Admin {callback_query.from_user.id} deleted EmployerProfile for user {user_id_of_profile_owner}")
        await session.commit()

    if deleted:
        await callback_query.answer("Анкета работодателя удалена, роль сброшена.", show_alert=True)
    else:
        await callback_query.answer("Анкета работодателя не найдена или уже удалена.", show_alert=True)
        
    await show_found_user_details(callback_query, state, user_id_of_profile_owner)


def get_manage_motivation_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="➕ Добавить новый контент", callback_data="admin_motivation_add")],
        [InlineKeyboardButton(text="📋 Просмотреть/Удалить контент", callback_data="admin_motivation_list")], # (TODO)
        [InlineKeyboardButton(text="🔙 Назад в Админ-меню", callback_data="admin_motivation_back_to_main")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

@admin_router.message(F.text == "🎬 Управление Мотивационным Контентом", StateFilter(AdminStates.in_panel))
async def admin_manage_motivation_menu(message: Message, state: FSMContext):
    # await state.set_state(AdminStates.managing_motivation_content) # Устанавливаем состояние этого подменю
    await message.answer(
        "Управление мотивационным контентом для соискателей:",
        reply_markup=get_manage_motivation_keyboard()
    )
    # Reply-клавиатуру главного меню не убираем, так как это inline-меню поверх

# Callback "Назад в Админ-меню" из управления мотивационным контентом
@admin_router.callback_query(F.data == "admin_motivation_back_to_main")
# @admin_router.callback_query(F.data == "admin_motivation_back_to_main", StateFilter(AdminStates.managing_motivation_content))
async def admin_back_to_main_from_motivation(callback_query: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.in_panel) 
    try: 
        await callback_query.message.edit_text(ADMIN_GREETING, reply_markup=None) 
        await callback_query.message.answer("Главное меню админ-панели:", reply_markup=admin_main_menu_keyboard)
    except:
        await callback_query.message.answer(ADMIN_GREETING, reply_markup=admin_main_menu_keyboard)
    await callback_query.answer()




# Функция для возврата в меню управления мотивационным контентом (чтобы не дублировать код)
async def return_to_motivation_management_menu(message: Message, state: FSMContext, edit_previous: bool = False):
    await state.set_state(AdminStates.in_panel) # Или AdminStates.managing_motivation_content, если оно есть
    text = "Управление мотивационным контентом для соискателей:\nВыберите действие:"
    kb = get_manage_motivation_keyboard()
    if edit_previous and message.is_bot: # Пытаемся отредактировать сообщение бота
        try:
            if message.photo or message.video: await message.delete() # Нельзя текст на медиа
            await message.edit_text(text, reply_markup=kb)
        except: # Фоллбэк на новое сообщение
            await message.answer(text, reply_markup=kb)
    else: # Отправляем новое сообщение
        await message.answer(text, reply_markup=kb)

# Callback для "➕ Добавить новый контент"
@admin_router.callback_query(F.data == "admin_motivation_add") 

async def admin_motivation_add_start(callback_query: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.adding_motivation_type)
    try: # Убираем inline кнопки и предыдущий текст
        await callback_query.message.edit_text(
            "Выберите тип нового мотивационного контента:",
            reply_markup=None
        )
    except: # Если не вышло, просто отправляем новое
        await callback_query.message.answer("Выберите тип нового мотивационного контента:")
    
    await callback_query.message.answer("Тип контента:", reply_markup=motivation_type_keyboard)
    await callback_query.answer()

# Отмена на любом шаге FSM добавления мотивации
@admin_router.message(StateFilter(
    AdminStates.adding_motivation_type,
    AdminStates.adding_motivation_file,
    AdminStates.adding_motivation_text_caption,
    AdminStates.adding_motivation_confirmation
), F.text == "Отмена добавления")
async def admin_cancel_motivation_add(message: Message, state: FSMContext):
    await message.answer("Добавление мотивационного контента отменено.", reply_markup=ReplyKeyboardRemove())
    await return_to_motivation_management_menu(message, state, edit_previous=False)


# Шаг 1: Выбор типа контента
@admin_router.message(AdminStates.adding_motivation_type, F.text.in_({"Видео", "Фото", "Только текст"}))
async def admin_motivation_process_type(message: Message, state: FSMContext):
    content_type_str = message.text
    selected_type = None
    if content_type_str == "Видео": selected_type = MotivationalContentTypeEnum.VIDEO
    elif content_type_str == "Фото": selected_type = MotivationalContentTypeEnum.PHOTO
    elif content_type_str == "Только текст": selected_type = MotivationalContentTypeEnum.TEXT_ONLY
    
    await state.update_data(motivation_content_type=selected_type)
    
    if selected_type == MotivationalContentTypeEnum.TEXT_ONLY:
        await state.set_state(AdminStates.adding_motivation_text_caption)
        await message.answer("Введите текст для мотивационного сообщения:", reply_markup=cancel_field_edit_keyboard) # Используем общую кнопку отмены поля
    else: # Видео или Фото
        await state.set_state(AdminStates.adding_motivation_file)
        await message.answer(f"Отправьте файл ({content_type_str.lower()}):", reply_markup=cancel_field_edit_keyboard)

@admin_router.message(AdminStates.adding_motivation_type) # Неверный тип
async def admin_motivation_invalid_type(message: Message, state: FSMContext):
    await message.answer("Пожалуйста, выберите тип контента с помощью кнопок.", reply_markup=motivation_type_keyboard)


# Шаг 2: Получение файла (Фото или Видео)
@admin_router.message(AdminStates.adding_motivation_file, F.photo | F.video)
async def admin_motivation_process_file(message: Message, state: FSMContext):
    file_id = None
    if message.photo: file_id = message.photo[-1].file_id
    elif message.video: file_id = message.video.file_id
    
    await state.update_data(motivation_file_id=file_id)
    await state.set_state(AdminStates.adding_motivation_text_caption)
    await message.answer("Теперь введите текст/подпись для этого медиа:", reply_markup=cancel_field_edit_keyboard)

@admin_router.message(AdminStates.adding_motivation_file, ~ (F.photo | F.video)) # Если не фото/видео (и не кнопка отмены)
async def admin_motivation_invalid_file(message: Message, state: FSMContext):
    if message.text == "❌ Отменить изменение": # Предполагая, что это текст из cancel_field_edit_keyboard
        # Эта кнопка должна вести к отмене всего процесса добавления мотивации
        return await admin_cancel_motivation_add(message, state)
    await message.answer("Пожалуйста, отправьте фото или видео, или отмените действие.", reply_markup=cancel_field_edit_keyboard)

# Шаг 3: Получение текста/подписи
@admin_router.message(AdminStates.adding_motivation_text_caption, F.text)
async def admin_motivation_process_text_caption(message: Message, state: FSMContext):
    if message.text == "❌ Отменить изменение": 
        return await admin_cancel_motivation_add(message, state) # Отмена всего процесса
        
    text_caption = message.text.strip()
    if not (5 <= len(text_caption) <= 1000): # Валидация
        await message.answer("Текст/подпись должен быть от 5 до 1000 символов. Попробуйте снова.", reply_markup=cancel_field_edit_keyboard)
        return
    
    await state.update_data(motivation_text_caption=text_caption)
    
    # Переход к подтверждению
    data = await state.get_data()
    confirmation_text = f"<b>Проверьте данные мотивационного контента:</b>\n"
    content_type_enum: MotivationalContentTypeEnum = data.get('motivation_content_type')
    confirmation_text += f"Тип: {content_type_enum.name.replace('_', ' ').title()}\n"
    if data.get('motivation_file_id'):
        confirmation_text += f"File ID: <code>{data.get('motivation_file_id')}</code> (медиа будет показано при сохранении)\n"
    confirmation_text += f"Текст/Подпись:\n<em>{data.get('motivation_text_caption')}</em>\n\nСохранить?"

    confirm_kb = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="✅ Да, сохранить контент")],
        [KeyboardButton(text="❌ Нет, отменить добавление")] # Вызовет admin_cancel_motivation_add
    ], resize_keyboard=True, one_time_keyboard=True)
    
    await state.set_state(AdminStates.adding_motivation_confirmation)
    await message.answer(confirmation_text, reply_markup=confirm_kb, parse_mode="HTML")

# Шаг 4: Подтверждение и сохранение
@admin_router.message(AdminStates.adding_motivation_confirmation, F.text == "✅ Да, сохранить контент")
async def admin_motivation_save(message: Message, state: FSMContext):
    data = await state.get_data()
    
    async with AsyncSessionFactory() as session, session.begin():
        new_content = MotivationalContent(
            content_type=data.get('motivation_content_type'),
            file_id=data.get('motivation_file_id'), # Будет None для TEXT_ONLY
            text_caption=data.get('motivation_text_caption'),
            is_active=True # По умолчанию активен
        )
        session.add(new_content)
    
    await message.answer("Новый мотивационный контент успешно добавлен!", reply_markup=ReplyKeyboardRemove())
    await return_to_motivation_management_menu(message, state, edit_previous=False)



# Используем StateFilter(AdminStates) для кнопки "❌ Отменить изменение", чтобы она работала во всех шагах
@admin_router.message(StateFilter(
    AdminStates.editing_antispam_dummy_text, 
    AdminStates.editing_antispam_dummy_photo,
    AdminStates.adding_motivation_type, # Добавляем состояния FSM мотивации
    AdminStates.adding_motivation_file,
    AdminStates.adding_motivation_text_caption
), F.text == "❌ Отменить изменение") # Этот текст должен быть на кнопке cancel_field_edit_keyboard
async def admin_cancel_current_field_edit_by_button(message: Message, state: FSMContext):
    current_fsm_state_str = await state.get_state()
    await message.answer("Ввод отменен.", reply_markup=ReplyKeyboardRemove())
    
    if current_fsm_state_str in [AdminStates.editing_antispam_dummy_text.state, AdminStates.editing_antispam_dummy_photo.state]:
        await state.set_state(AdminStates.in_panel) 
        await show_antispam_dummy_config_menu(message, state)
    elif current_fsm_state_str in [AdminStates.adding_motivation_type.state, AdminStates.adding_motivation_file.state, AdminStates.adding_motivation_text_caption.state]:
        # При отмене ввода поля для мотивации, возвращаемся в меню управления мотивацией
        await return_to_motivation_management_menu(message, state, edit_previous=False)
    else: 
        await state.set_state(AdminStates.in_panel)
        await message.answer(ADMIN_GREETING, reply_markup=admin_main_menu_keyboard)
    
# Функция для построения клавиатуры списка мотивационного контента
async def build_motivation_list_keyboard(
    contents: list[MotivationalContent], 
    current_page: int = 0, 
    per_page: int = 5 # Установим 5 по умолчанию, для теста можно менять на 3
) -> InlineKeyboardMarkup:
    buttons = []
    
    total_items = len(contents)
    if total_items == 0: # Если список контента пуст
        buttons.append([InlineKeyboardButton(text="Пока нет контента. Добавить новый?", callback_data="admin_motivation_add")])
        buttons.append([InlineKeyboardButton(text="🔙 Назад (в меню мотивации)", callback_data="admin_motivation_back_to_manage_menu")])
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    # Рассчитываем общее количество страниц, округляя вверх
    total_pages = (total_items + per_page - 1) // per_page
    if total_pages == 0: total_pages = 1 # Минимум 1 страница

    # Корректируем current_page, если он выходит за пределы (например, после удаления элементов)
    current_page = max(0, min(current_page, total_pages - 1))

    # Определяем срез для текущей страницы
    start_index = current_page * per_page
    end_index = start_index + per_page
    paginated_contents = contents[start_index:end_index]

    for content_item in paginated_contents:
        item_text_preview = content_item.text_caption[:25].strip() + "..." if len(content_item.text_caption) > 25 else content_item.text_caption.strip()
        status_emoji = "🟢" if content_item.is_active else "🔴"
        
        # Эмодзи для типа контента
        type_emoji = "❓" # По умолчанию
        if content_item.content_type == MotivationalContentTypeEnum.VIDEO:
            type_emoji = "🎬"
        elif content_item.content_type == MotivationalContentTypeEnum.PHOTO:
            type_emoji = "🖼️"
        elif content_item.content_type == MotivationalContentTypeEnum.TEXT_ONLY:
            type_emoji = "📄"
        
    
        buttons.append([
            InlineKeyboardButton(
                text=f"{status_emoji}{type_emoji} ID:{content_item.id} - {item_text_preview}",
                callback_data=f"{MOTIVATION_CALLBACK_PREFIX}view:{content_item.id}:{current_page}" 
            )
        ])
        
        # Кнопки действий для этого элемента в отдельной строке
        action_row_for_item = [
            InlineKeyboardButton(text="👁️Показ", callback_data=f"{MOTIVATION_CALLBACK_PREFIX}view:{content_item.id}:{current_page}"),
            InlineKeyboardButton(
                text="🗑️Удал.", 
                callback_data=f"{MOTIVATION_CALLBACK_PREFIX}delete_confirm:{content_item.id}:{current_page}"
            ),
            InlineKeyboardButton(
                text="⚡Акт." if not content_item.is_active else "💤Неакт.", 
                callback_data=f"{MOTIVATION_CALLBACK_PREFIX}toggle_active:{content_item.id}:{current_page}"
            )
        ]
        buttons.append(action_row_for_item)
        # buttons.append([InlineKeyboardButton(text="-"*20, callback_data="no_action_separator")]) # Разделитель можно убрать для компактности

    # Кнопки пагинации
    pagination_row = []
    if current_page > 0: # Показывать кнопку "Пред.", только если это не первая страница
        pagination_row.append(InlineKeyboardButton(text="◀️ Пред.", callback_data=f"{MOTIVATION_CALLBACK_PREFIX}page:{current_page-1}"))
    
    if total_pages > 1: # Показываем номер страницы, только если их больше одной
        pagination_row.append(InlineKeyboardButton(text=f"📄 {current_page+1}/{total_pages}", callback_data="no_action_page_num")) # Некликабельная кнопка номера страницы

    # Проверяем, есть ли следующая страница
    if (current_page + 1) < total_pages:
        pagination_row.append(InlineKeyboardButton(text="След. ▶️", callback_data=f"{MOTIVATION_CALLBACK_PREFIX}page:{current_page+1}"))
    
    if pagination_row: # Добавляем строку пагинации, только если она не пуста
        buttons.append(pagination_row)
            
    buttons.append([InlineKeyboardButton(text="🔙 Назад (в меню мотивации)", callback_data="admin_motivation_back_to_manage_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# Функция для отображения конкретной страницы списка мотивационного контента
async def show_motivation_content_list_page(target: Message | CallbackQuery, state: FSMContext, page: int = 0):
    message_to_act_on = target.message if isinstance(target, CallbackQuery) else target
    per_page = 3 # Или ваше значение

    async with AsyncSessionFactory() as session, session.begin():
        content_result = await session.execute(
            select(MotivationalContent).order_by(MotivationalContent.id.desc()) # Сначала новые
        )
        all_content_items = content_result.scalars().all()

    if not all_content_items:
        text_to_send = "Мотивационный контент еще не добавлен."
        # Возвращаем к предыдущему меню (где кнопки "Добавить", "Список", "Назад в админку")
        kb_to_send = get_manage_motivation_keyboard() 
    else:
        text_to_send = f"Список мотивационного контента (Стр. {page+1}):\nВыберите для действий."
        kb_to_send = await build_motivation_list_keyboard(all_content_items, current_page=page, per_page=per_page)
    
    try:
        if isinstance(target, CallbackQuery):
            await message_to_act_on.edit_text(text_to_send, reply_markup=kb_to_send)
            await target.answer()
        else: # Message
            await message_to_act_on.answer(text_to_send, reply_markup=kb_to_send)
    except Exception as e:
        print(f"Error displaying motivation content list page {page}: {e}")
        # Фоллбэк, если редактирование не удалось
        await message_to_act_on.answer(text_to_send, reply_markup=kb_to_send)
        if isinstance(target, CallbackQuery): await target.answer("Не удалось обновить список.")


# Заменяем заглушку для "📋 Просмотреть/Удалить контент"
@admin_router.callback_query(F.data == "admin_motivation_list")
# Добавьте StateFilter, если managing_motivation_content используется: StateFilter(AdminStates.managing_motivation_content)
async def admin_list_motivation_content_start(callback_query: CallbackQuery, state: FSMContext):
    await show_motivation_content_list_page(callback_query, state, page=0) # Показываем первую страницу

# Хэндлер для кнопок пагинации списка мотивационного контента
@admin_router.callback_query(F.data.startswith(f"{MOTIVATION_CALLBACK_PREFIX}page:"))
async def admin_paginate_motivation_list(callback_query: CallbackQuery, state: FSMContext):
    try:
        page = int(callback_query.data.split(":")[-1])
    except ValueError:
        await callback_query.answer("Ошибка страницы.", show_alert=True); return
    await show_motivation_content_list_page(callback_query, state, page=page)

# Callback "🔙 Назад (в меню мотивации)" из списка контента
@admin_router.callback_query(F.data == "admin_motivation_back_to_manage_menu")
async def admin_motivation_list_back_to_menu(callback_query: CallbackQuery, state: FSMContext):
    await callback_query.message.edit_text(
        "Управление мотивационным контентом для соискателей:\nВыберите действие:",
        reply_markup=get_manage_motivation_keyboard() # get_manage_motivation_keyboard должна быть определена
    )
    await callback_query.answer()
    
    
# Хэндлер для просмотра конкретного элемента мотивационного контента ("👁️Показ")
@admin_router.callback_query(F.data.startswith(f"{MOTIVATION_CALLBACK_PREFIX}view:"))
async def admin_motivation_view_specific_item(callback_query: CallbackQuery, state: FSMContext):
    try:
        parts = callback_query.data.split(":")
        item_id = int(parts[1])
        page_to_return_to = int(parts[2]) # Страница списка, на которую вернуться
    except (IndexError, ValueError):
        await callback_query.answer("Ошибка: неверные данные для просмотра.", show_alert=True)
        return

    async with AsyncSessionFactory() as session, session.begin():
        content_item = await session.get(MotivationalContent, item_id)

    if not content_item:
        await callback_query.answer("Элемент мотивационного контента не найден.", show_alert=True)
        # Можно попытаться обновить список, если элемент был удален
        await show_motivation_content_list_page(callback_query, state, page=page_to_return_to)
        return

    await callback_query.answer() # Отвечаем на callback, чтобы убрать "часики"

    # Клавиатура для возврата к списку
    back_to_list_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"🔙 Назад к списку (стр. {page_to_return_to + 1})", 
            callback_data=f"{MOTIVATION_CALLBACK_PREFIX}page:{page_to_return_to}" # Возвращаемся на ту же страницу
        )]
    ])

    # Удаляем предыдущее сообщение со списком
    try:
        await callback_query.message.delete()
    except Exception as e_del:
        print(f"DEBUG: Could not delete message before viewing motivation item: {e_del}")

    # Отправляем контент
    bot_instance: Bot = callback_query.bot # Получаем экземпляр бота из callback_query
    try:
        if content_item.content_type == MotivationalContentTypeEnum.VIDEO and content_item.file_id:
            await bot_instance.send_video(
                chat_id=callback_query.from_user.id,
                video=content_item.file_id,
                caption=content_item.text_caption[:1024], # Ограничение caption
                reply_markup=back_to_list_kb,
                parse_mode="HTML" # Если используете HTML в text_caption
            )
        elif content_item.content_type == MotivationalContentTypeEnum.PHOTO and content_item.file_id:
            await bot_instance.send_photo(
                chat_id=callback_query.from_user.id,
                photo=content_item.file_id,
                caption=content_item.text_caption[:1024], # Ограничение caption
                reply_markup=back_to_list_kb,
                parse_mode="HTML"
            )
        elif content_item.content_type == MotivationalContentTypeEnum.TEXT_ONLY:
            await bot_instance.send_message(
                chat_id=callback_query.from_user.id,
                text=f"<b>Текстовое мотивационное сообщение (ID: {content_item.id}):</b>\n\n{content_item.text_caption}",
                reply_markup=back_to_list_kb,
                parse_mode="HTML"
            )
        else: # Неизвестный тип или нет file_id для медиа
            await bot_instance.send_message(
                chat_id=callback_query.from_user.id,
                text=f"Не удалось отобразить контент ID {content_item.id}.\nТип: {content_item.content_type.name}\nТекст: {content_item.text_caption}",
                reply_markup=back_to_list_kb
            )
    except Exception as e_send:
        print(f"ERROR sending motivational content item ID {content_item.id}: {e_send}\n{traceback.format_exc()}")
        await callback_query.message.answer( # Отправляем в тот же чат, где было исходное сообщение
            "Произошла ошибка при отображении контента. Попробуйте вернуться к списку.",
            reply_markup=back_to_list_kb
        )


# Хэндлер для кнопки "🗑️Удал." (запрос подтверждения)
@admin_router.callback_query(F.data.startswith(f"{MOTIVATION_CALLBACK_PREFIX}delete_confirm:"))
async def admin_motivation_confirm_delete_item(callback_query: CallbackQuery, state: FSMContext):
    try:
        parts = callback_query.data.split(":")
        item_id = int(parts[1])
        page_to_return_to = int(parts[2])
    except (IndexError, ValueError):
        await callback_query.answer("Ошибка: неверные данные для удаления.", show_alert=True); return

    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"{MOTIVATION_CALLBACK_PREFIX}delete_do:{item_id}:{page_to_return_to}"),
            InlineKeyboardButton(text="❌ Нет, отмена", callback_data=f"{MOTIVATION_CALLBACK_PREFIX}delete_cancel:{item_id}:{page_to_return_to}")
        ]
    ])
    await callback_query.message.edit_text(
        f"Вы уверены, что хотите удалить мотивационный контент ID {item_id}?",
        reply_markup=confirm_kb
    )
    await callback_query.answer()

# Хэндлер для "❌ Нет, отмена" (отмена удаления)
@admin_router.callback_query(F.data.startswith(f"{MOTIVATION_CALLBACK_PREFIX}delete_cancel:"))
async def admin_motivation_cancel_delete_item(callback_query: CallbackQuery, state: FSMContext):
    try:
        page_to_return_to = int(callback_query.data.split(":")[-1]) # Последний элемент - номер страницы
    except (IndexError, ValueError):
        page_to_return_to = 0 # Фоллбэк на первую страницу
        
    await callback_query.answer("Удаление отменено.")
    # Возвращаемся к списку на той же странице
    await show_motivation_content_list_page(callback_query, state, page=page_to_return_to)

# Хэндлер для "✅ Да, удалить" (фактическое удаление)
@admin_router.callback_query(F.data.startswith(f"{MOTIVATION_CALLBACK_PREFIX}delete_do:"))
async def admin_motivation_do_delete_item(callback_query: CallbackQuery, state: FSMContext):
    try:
        parts = callback_query.data.split(":")
        item_id = int(parts[1])
        page_to_return_to = int(parts[2])
    except (IndexError, ValueError):
        await callback_query.answer("Ошибка: неверные данные для удаления.", show_alert=True); return

    deleted_count = 0
    async with AsyncSessionFactory() as session, session.begin():
        result = await session.execute(
            delete(MotivationalContent).where(MotivationalContent.id == item_id)
        )
        deleted_count = result.rowcount 
    
    if deleted_count > 0:
        await callback_query.answer(f"Мотивационный контент ID {item_id} удален.", show_alert=True)
    else:
        await callback_query.answer(f"Контент ID {item_id} не найден или уже был удален.", show_alert=True)
    
    # Обновляем список, возвращаясь на ту же (или предыдущую, если текущая стала пустой) страницу
    await show_motivation_content_list_page(callback_query, state, page=page_to_return_to)

@admin_router.callback_query(F.data.startswith(f"{MOTIVATION_CALLBACK_PREFIX}toggle_active:"))
async def admin_motivation_toggle_active_status(callback_query: CallbackQuery, state: FSMContext):
    try:
        parts = callback_query.data.split(":")
        item_id = int(parts[1])
        page_to_return_to = int(parts[2])
    except (IndexError, ValueError):
        await callback_query.answer("Ошибка: неверные данные для смены статуса.", show_alert=True); return

    new_status_is_active = None
    async with AsyncSessionFactory() as session, session.begin():
        content_item = await session.get(MotivationalContent, item_id)
        if content_item:
            content_item.is_active = not content_item.is_active # Инвертируем статус
            content_item.updated_at = func.now()
            # session.add(content_item) # Не обязательно, SQLAlchemy отследит
            new_status_is_active = content_item.is_active
            await session.commit() # Коммитим изменение
        else:
            await callback_query.answer(f"Контент ID {item_id} не найден.", show_alert=True)
            return # Выходим, список не обновляем, т.к. элемента нет

    if new_status_is_active is not None:
        status_text = "активирован" if new_status_is_active else "деактивирован"
        await callback_query.answer(f"Контент ID {item_id} {status_text}.")
    
    # Обновляем список, возвращаясь на ту же страницу
    await show_motivation_content_list_page(callback_query, state, page=page_to_return_to)


# Заглушка для "no_action_separator" и "no_action_page_num"
@admin_router.callback_query(F.data.in_({"no_action_separator", "no_action_page_num"}))
async def no_action_pagination_info(callback_query: CallbackQuery):
    await callback_query.answer() # Просто отвечаем, чтобы убрать "часики"

# --- ЗАГЛУШКИ ДЛЯ ДЕЙСТВИЙ С КОНКРЕТНЫМ МОТИВАЦИОННЫМ КОНТЕНТОМ ---
@admin_router.callback_query(F.data.startswith(f"{MOTIVATION_CALLBACK_PREFIX}view:"))
async def admin_motivation_view_item(callback_query: CallbackQuery, state: FSMContext):
    parts = callback_query.data.split(":")
    item_id = int(parts[1])
    await callback_query.answer(f"Просмотр мотивационного контента ID {item_id} (TODO)", show_alert=True)

@admin_router.callback_query(F.data.startswith(f"{MOTIVATION_CALLBACK_PREFIX}delete_confirm:"))
async def admin_motivation_delete_item_confirm(callback_query: CallbackQuery, state: FSMContext):
    parts = callback_query.data.split(":")
    item_id = int(parts[1])
    await callback_query.answer(f"Подтверждение удаления ID {item_id} (TODO)", show_alert=True)

@admin_router.callback_query(F.data.startswith(f"{MOTIVATION_CALLBACK_PREFIX}toggle_active:"))
async def admin_motivation_toggle_active_item(callback_query: CallbackQuery, state: FSMContext):
    parts = callback_query.data.split(":")
    item_id = int(parts[1])
    await callback_query.answer(f"Переключение активности ID {item_id} (TODO)", show_alert=True)
    
    
# --- УПРАВЛЕНИЕ ТРАФИКОМ И РЕФЕРАЛЬНЫМИ ССЫЛКАМИ ---

def get_referral_management_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для главного меню управления трафиком."""
    buttons = [
        [InlineKeyboardButton(text="➕ Создать новую ссылку", callback_data=f"{REFERRAL_CALLBACK_PREFIX}create_start")],
        [InlineKeyboardButton(text="📈 Статистика по ссылкам", callback_data=f"{REFERRAL_CALLBACK_PREFIX}stats_page:0")],
        [InlineKeyboardButton(text="🔙 Назад в Админ-меню", callback_data=f"{REFERRAL_CALLBACK_PREFIX}back_to_main")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# Хэндлер для кнопки "Управление трафиком"
@admin_router.message(F.text == "📊 Управление трафиком", StateFilter(AdminStates.in_panel))
async def admin_manage_referrals_menu(message: Message, state: FSMContext):
    await message.answer(
        "Выберите действие для управления реферальными ссылками:",
        reply_markup=get_referral_management_keyboard()
    )
    
@admin_router.message(StateFilter(AdminReferralManagement.waiting_for_name), Command("cancel-admin-action"))
async def admin_referral_create_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Создание ссылки отменено.")
    await message.answer("Управление реферальными ссылками:", reply_markup=get_referral_management_keyboard())

# Возврат в главное меню админки из управления трафиком
@admin_router.callback_query(F.data == f"{REFERRAL_CALLBACK_PREFIX}back_to_main")
async def admin_referral_back_to_main(callback_query: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.in_panel)
    try:
        await callback_query.message.edit_text(ADMIN_GREETING, reply_markup=None)
        await callback_query.message.answer("Главное меню админ-панели:", reply_markup=admin_main_menu_keyboard)
    except:
        await callback_query.message.answer(ADMIN_GREETING, reply_markup=admin_main_menu_keyboard)
    await callback_query.answer()
    
# --- Создание новой реферальной ссылки (FSM) ---

@admin_router.callback_query(F.data == f"{REFERRAL_CALLBACK_PREFIX}create_start")
async def admin_referral_create_start(callback_query: CallbackQuery, state: FSMContext):
    await state.set_state(AdminReferralManagement.waiting_for_name)
    await callback_query.message.edit_text(
        "Введите описательное имя для новой реферальной ссылки.\n"
        "Например: `Реклама в канале @partner_channel`\n\n"
        "Для отмены введите /cancel-admin-action",
        parse_mode="Markdown"
    )
    await callback_query.answer()



@admin_router.message(StateFilter(AdminReferralManagement.waiting_for_name), F.text)
async def admin_referral_create_process_name(message: Message, state: FSMContext, bot: Bot):
    link_name = message.text.strip()
    if not (3 <= len(link_name) <= 200):
        await message.answer("Имя должно быть от 3 до 200 символов. Попробуйте снова."); return

    # Генерируем уникальный и безопасный код для ссылки
    ref_code = secrets.token_urlsafe(8)

    async with AsyncSessionFactory() as session, session.begin():
        
        while True:
            exists = await session.execute(select(ReferralLink).where(ReferralLink.code == ref_code))
            if not exists.scalar_one_or_none():
                break
            ref_code = secrets.token_urlsafe(8)

        # Сохраняем новую ссылку в БД
        new_link = ReferralLink(
            code=ref_code,
            name=link_name,
            creator_admin_id=message.from_user.id
        )
        session.add(new_link)
        await session.commit() # Сохраняем, чтобы получить new_link.id

    bot_info = await bot.get_me()
    bot_username = bot_info.username
    final_link = f"https://t.me/{bot_username}?start={ref_code}"

    await state.clear()
    await message.answer(
        f"✅ Новая ссылка успешно создана!\n\n"
        f"<b>Название:</b> {link_name}\n"
        f"<b>Ваша ссылка для отслеживания:</b>\n"
        f"<code>{final_link}</code>\n\n"
        f"Используйте ее для привлечения пользователей. Статистика появится после первых переходов.",
        parse_mode="HTML",
        reply_markup=get_referral_management_keyboard()
    )

# --- Просмотр статистики по ссылкам ---

async def build_referral_stats_keyboard(links_with_stats: list, current_page: int, per_page: int, total_items: int) -> InlineKeyboardMarkup:
    buttons = []
    total_pages = (total_items + per_page - 1) // per_page
    
    for link_stat in links_with_stats:
        buttons.append([
            InlineKeyboardButton(
                text=f"'{link_stat.name[:25]}' ({link_stat.total_clicks} / {link_stat.unique_users} уник.)",
                callback_data=f"{REFERRAL_CALLBACK_PREFIX}details:{link_stat.id}" # TODO: Детальный просмотр
            )
        ])

    pagination_row = []
    if current_page > 0:
        pagination_row.append(InlineKeyboardButton(text="◀️ Пред.", callback_data=f"{REFERRAL_CALLBACK_PREFIX}stats_page:{current_page-1}"))
    if total_pages > 1:
        pagination_row.append(InlineKeyboardButton(text=f"📄 {current_page+1}/{total_pages}", callback_data="no_action"))
    if (current_page + 1) < total_pages:
        pagination_row.append(InlineKeyboardButton(text="След. ▶️", callback_data=f"{REFERRAL_CALLBACK_PREFIX}stats_page:{current_page+1}"))
    
    if pagination_row:
        buttons.append(pagination_row)

    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"{REFERRAL_CALLBACK_PREFIX}back_to_menu_from_stats")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@admin_router.callback_query(F.data.startswith(f"{REFERRAL_CALLBACK_PREFIX}stats_page:"))
async def admin_referral_show_stats(callback_query: CallbackQuery, state: FSMContext):
    try:
        page = int(callback_query.data.split(":")[-1])
    except (ValueError, IndexError):
        page = 0
    
    per_page = 5

    async with AsyncSessionFactory() as session:

        total_items_res = await session.execute(select(func.count(ReferralLink.id)))
        total_items = total_items_res.scalar_one()

        if total_items == 0:
            await callback_query.message.edit_text(
                "Еще не создано ни одной реферальной ссылки. Создайте первую!",
                reply_markup=get_referral_management_keyboard()
            )
            await callback_query.answer()
            return

        # Теперь получаем данные для текущей страницы
        stmt = (
            select(
                ReferralLink.id,
                ReferralLink.name,
                ReferralLink.code,
                func.count(ReferralUsage.id).label('total_clicks'),
                func.count(func.distinct(ReferralUsage.user_id)).label('unique_users')
            )
            .outerjoin(ReferralUsage, ReferralLink.id == ReferralUsage.link_id)
            .group_by(ReferralLink.id)
            .order_by(ReferralLink.created_at.desc())
            .offset(page * per_page)
            .limit(per_page)
        )
        result = await session.execute(stmt)
        links_with_stats = result.all()

    text = "<b>Статистика по реферальным ссылкам</b>\n(Всего кликов / Уникальных пользователей)\n\n"
    for link in links_with_stats:
        text += f"▪️ <b>{link.name}</b>\n"
        text += f"   - <code>{link.code}</code>\n"
        text += f"   - Переходов: <b>{link.total_clicks}</b> (<b>{link.unique_users}</b> уник.)\n"

    keyboard = await build_referral_stats_keyboard(links_with_stats, page, per_page, total_items)
    await callback_query.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback_query.answer()

# Возврат из статистики в меню управления трафиком
@admin_router.callback_query(F.data == f"{REFERRAL_CALLBACK_PREFIX}back_to_menu_from_stats")
async def admin_referral_stats_back_to_menu(callback_query: CallbackQuery, state: FSMContext):
    await callback_query.message.edit_text(
        "Выберите действие для управления реферальными ссылками:",
        reply_markup=get_referral_management_keyboard()
    )
    await callback_query.answer()