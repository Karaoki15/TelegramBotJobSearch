# app/handlers/employer_responses_handlers.py
import traceback
import re # Для regexp, если будем использовать
from datetime import datetime, timezone
from aiogram import Bot, Router, F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton

from app.db.database import AsyncSessionFactory
from app.db.models import User, ApplicantProfile, EmployerProfile, ApplicantEmployerInteraction, InteractionTypeEnum, GenderEnum
from sqlalchemy import select, update, func as sqlalchemy_func, delete
from app.db.models import Complaint, ComplaintStatusEnum

from app.handlers.registration_handlers import is_user_subscribed_to_channel
from app.keyboards.reply_keyboards import start_keyboard


from app.handlers.settings_handlers import BTN_VIEW_RESPONSES_TEXT, show_employer_main_menu 

employer_responses_router = Router()

# --- КОНСТАНТЫ ДЛЯ CALLBACK DATA ---
VIEW_SPECIFIC_RESPONSE_PREFIX = "view_specific_resp:" 
NEXT_RESPONSE_CALLBACK_DATA = "next_unread_resp"
REPORT_APPLICANT_CALLBACK_DATA_PREFIX = "report_appl:"


# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def format_applicant_profile_for_employer(
    applicant_profile: ApplicantProfile, 
    applicant_user: User, 
    interaction: ApplicantEmployerInteraction
) -> str:
    gender_display = getattr(applicant_profile.gender, 'name', "Не указан").title()
    contact_phone_display = f"+{applicant_user.contact_phone}" if applicant_user.contact_phone else "Не указан"
    username_display = f"@{applicant_user.username}" if applicant_user.username else "Не указан"

    text = (
        f"👤 Анкета соискателя (Отклик ID: {interaction.id}):\n\n"
        f"<b>Имя:</b> {applicant_user.first_name or ''} {applicant_user.last_name or ''}\n"
        f"<b>Username:</b> {username_display}\n"
        f"<b>Город:</b> {applicant_profile.city}\n"
        f"<b>Пол:</b> {gender_display}\n<b>Возраст:</b> {applicant_profile.age}\n"
        f"<b>Опыт работы:</b>\n{applicant_profile.experience}\n\n"
        f"<b>Контакт Telegram:</b> {username_display}\n"
        f"<b>Контактный телефон:</b> {contact_phone_display}\n"
    )
    if interaction.interaction_type == InteractionTypeEnum.QUESTION_SENT and interaction.question_text:
        text += f"\n<b>❓ Вопрос от соискателя:</b>\n{interaction.question_text}"
    elif interaction.interaction_type == InteractionTypeEnum.LIKE:
        text += f"\n👍 Соискатель лайкнул вашу вакансию."
    return text

async def build_response_inline_keyboard(remaining_count: int, applicant_user_id: int) -> InlineKeyboardMarkup:
    """Строит Inline клавиатуру для сообщения с анкетой соискателя."""
    buttons = [[InlineKeyboardButton(text="🚩 Пожаловаться", callback_data=f"{REPORT_APPLICANT_CALLBACK_DATA_PREFIX}{applicant_user_id}")]]
    if remaining_count > 0:
        buttons.append([InlineKeyboardButton(text=f"➡️ Следующий ({remaining_count})", callback_data=NEXT_RESPONSE_CALLBACK_DATA)])
    # Кнопка "Назад в меню" здесь не нужна, т.к. Reply-меню работодателя остается активным.
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def send_or_edit_response_message(
    bot_instance: Bot,
    chat_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup,
    message_to_edit: types.Message = None
):
    """Отправляет новое или редактирует существующее сообщение с откликом."""
    if message_to_edit:
        try:

            if isinstance(message_to_edit.reply_markup, InlineKeyboardMarkup):
                 await message_to_edit.delete() # Удаляем сообщение с Inline-кнопкой "Посмотреть отклик"
            # Отправляем новое сообщение с анкетой и новыми inline кнопками
            await bot_instance.send_message(chat_id, text, parse_mode="HTML", reply_markup=reply_markup)
            return
        except Exception as e:
            print(f"Failed to delete/edit previous message, sending new: {e}")
            # Если удаление/редактирование не удалось, просто отправляем новое
    await bot_instance.send_message(chat_id, text, parse_mode="HTML", reply_markup=reply_markup)


async def display_applicant_response(
    interaction_to_show: ApplicantEmployerInteraction, # Объект из текущей сессии, если возможно
    employer_user_id: int, 
    bot_instance: Bot, 
    chat_id_to_reply: int, 
    state: FSMContext,
    original_message_with_button: types.Message = None 
):

    async with AsyncSessionFactory() as session, session.begin():
        # Получаем interaction, чтобы убедиться, что он существует и для работы в текущей сессии
        current_interaction_in_session = await session.get(ApplicantEmployerInteraction, interaction_to_show.id)
        if not current_interaction_in_session:
            await bot_instance.send_message(chat_id_to_reply, "Запрошенный отклик больше не доступен.")
            if original_message_with_button: # Если было PUSH-сообщение, его можно удалить
                try: await original_message_with_button.delete()
                except: pass
            return 

        # Получаем данные соискателя
        applicant_user = await session.get(User, current_interaction_in_session.applicant_user_id)
        applicant_profile_q = await session.execute(
            select(ApplicantProfile).where(ApplicantProfile.user_id == current_interaction_in_session.applicant_user_id)
        )
        applicant_profile = applicant_profile_q.scalar_one_or_none()

        # Проверка, что данные соискателя найдены
        if not (applicant_user and applicant_profile):
            await bot_instance.send_message(chat_id_to_reply, "Не удалось загрузить полные данные соискателя для этого отклика. Попробуйте следующий.")
            # Даже если данные соискателя не полные, отклик мы увидели
            if not current_interaction_in_session.is_viewed_by_employer:
                current_interaction_in_session.is_viewed_by_employer = True
                current_interaction_in_session.updated_at = datetime.now(timezone.utc)
            return 

        # Формируем текст анкеты
        profile_text = format_applicant_profile_for_employer(applicant_profile, applicant_user, current_interaction_in_session)
        
        # Помечаем отклик как просмотренный
        if not current_interaction_in_session.is_viewed_by_employer:
            current_interaction_in_session.is_viewed_by_employer = True
            current_interaction_in_session.updated_at = datetime.now(timezone.utc)
            print(f"DEBUG: Interaction ID {current_interaction_in_session.id} marked as viewed by employer {employer_user_id}.")
        
        # Сохраняем ID соискателя (на которого можно пожаловаться) и информацию о следующих откликах в FSM
        # Сначала считаем оставшиеся непросмотренные
        remaining_responses_count_q = await session.execute(
            select(sqlalchemy_func.count(ApplicantEmployerInteraction.id)).where(
                ApplicantEmployerInteraction.employer_profile_id == current_interaction_in_session.employer_profile_id,
                ApplicantEmployerInteraction.is_viewed_by_employer == False, # Считаем те, что еще не просмотрены
                ApplicantEmployerInteraction.interaction_type.in_([InteractionTypeEnum.LIKE, InteractionTypeEnum.QUESTION_SENT]),
                ApplicantEmployerInteraction.id != current_interaction_in_session.id # Исключаем текущий, т.к. он уже is_viewed=True
            )
        )
        remaining_count = remaining_responses_count_q.scalar_one() or 0
        
        await state.update_data(
            employer_can_report_applicant_id=applicant_user.telegram_id,
            employer_has_next_responses=(remaining_count > 0),
            employer_remaining_responses_count=remaining_count
        )
        
        # Строим Inline-клавиатуру для сообщения
        inline_kb = await build_response_inline_keyboard(remaining_count, applicant_user.telegram_id)
        
        # Если это было сообщение с кнопкой "Посмотреть отклик" из PUSH, удаляем его
        if original_message_with_button:
            try:
                await original_message_with_button.delete()
                print(f"DEBUG: Deleted original PUSH message_id {original_message_with_button.message_id}")
            except Exception as e_del_orig:
                print(f"DEBUG: Failed to delete original PUSH message: {e_del_orig}")

        # Отправляем НОВОЕ сообщение с анкетой соискателя и кнопками "Пожаловаться" / "Следующий"
        try:
            await bot_instance.send_message(chat_id_to_reply, profile_text, parse_mode="HTML", reply_markup=inline_kb)
        except Exception as e_send_response:
            print(f"ERROR sending applicant response to employer: {e_send_response}\n{traceback.format_exc()}")
            await bot_instance.send_message(chat_id_to_reply, "Произошла ошибка при отображении отклика.")


async def fetch_and_display_first_unread(employer_user_id: int, bot_instance: Bot, chat_id_to_reply: int, state: FSMContext, original_message_with_button: types.Message = None):
    """Находит первый непросмотренный отклик и вызывает его отображение."""
    async with AsyncSessionFactory() as session, session.begin():
        emp_profile_q = await session.execute(select(EmployerProfile.id).where(EmployerProfile.user_id == employer_user_id))
        employer_profile_id = emp_profile_q.scalar_one_or_none()

        if not employer_profile_id:
            await bot_instance.send_message(chat_id_to_reply, "Ваш профиль работодателя не найден. Скорее всего вашу анкету удалил администратор. пропиши /start для перезапуска.")
            return

        interaction_q = await session.execute(
            select(ApplicantEmployerInteraction).where(
                ApplicantEmployerInteraction.employer_profile_id == employer_profile_id,
                ApplicantEmployerInteraction.is_viewed_by_employer == False,
                ApplicantEmployerInteraction.interaction_type.in_([InteractionTypeEnum.LIKE, InteractionTypeEnum.QUESTION_SENT])
            ).order_by(ApplicantEmployerInteraction.created_at.asc()).limit(1)
        )
        interaction = interaction_q.scalar_one_or_none()

        if interaction:
            await display_applicant_response(interaction, employer_user_id, bot_instance, chat_id_to_reply, state, original_message_with_button)
        else:
            await bot_instance.send_message(chat_id_to_reply, "Новых откликов на данный момент нет.")


# --- ОСНОВНЫЕ ХЭНДЛЕРЫ ---

# 1. Для Inline-кнопки из Push-уведомления (например, "view_resp:123")
@employer_responses_router.callback_query(F.data.startswith(VIEW_SPECIFIC_RESPONSE_PREFIX))
async def cq_view_specific_response_from_push(callback_query: types.CallbackQuery, state: FSMContext):
    
    user_id = callback_query.from_user.id
    bot_instance = callback_query.bot
    is_subscribed = await is_user_subscribed_to_channel(user_id, bot_instance)

    if not is_subscribed:
        async with AsyncSessionFactory() as session, session.begin():
            await session.execute(delete(EmployerProfile).where(EmployerProfile.user_id == user_id))
            await session.execute(update(User).where(User.telegram_id == user_id).values(role=None))
        
        await state.clear()
        await callback_query.message.answer(
            "Вы не подписаны на канал, оформите подписку и тогда вы снова сможете получить доступ к боту",
            reply_markup=ReplyKeyboardRemove()
        )
        try:
            await callback_query.message.delete()
        except:
            pass
        await callback_query.answer()
        return

    # Если проверка пройдена, выполняется оригинальная логика функции
    try:
        interaction_id_str = callback_query.data[len(VIEW_SPECIFIC_RESPONSE_PREFIX):]
        interaction_id = int(interaction_id_str)
        await callback_query.answer(f"Загружаю отклик ID: {interaction_id}...")
        
        async with AsyncSessionFactory() as session, session.begin():
            interaction_obj_q = await session.execute(
                select(ApplicantEmployerInteraction).where(ApplicantEmployerInteraction.id == interaction_id)
            )
            interaction_obj = interaction_obj_q.scalar_one_or_none()

            if interaction_obj:
                employer_profile_for_interaction_q = await session.execute(
                    select(EmployerProfile.user_id).where(EmployerProfile.id == interaction_obj.employer_profile_id)
                )
                actual_employer_user_id = employer_profile_for_interaction_q.scalar_one_or_none()
                
                if actual_employer_user_id == callback_query.from_user.id:
                    if not interaction_obj.is_viewed_by_employer:
                        interaction_obj.is_viewed_by_employer = True
                        interaction_obj.updated_at = datetime.now(timezone.utc)
                        session.add(interaction_obj)
                    
                    await display_applicant_response(
                        interaction_obj,
                        callback_query.from_user.id,
                        callback_query.bot,
                        callback_query.message.chat.id,
                        state,
                        callback_query.message 
                    )
                else:
                    await callback_query.answer("Это не ваш отклик.", show_alert=True)
                    try: await callback_query.message.delete() 
                    except: pass
            else:
                await callback_query.message.answer("Этот отклик уже не доступен.")
                await callback_query.answer("Отклик не найден.", show_alert=True)

    except (ValueError, TypeError, IndexError) as e:
        print(f"Error parsing interaction_id or handling specific response: {callback_query.data}, error: {e}\n{traceback.format_exc()}")
        await callback_query.answer("Ошибка обработки запроса.", show_alert=True)


# 2. Для Reply-кнопки "Посмотреть отклики (N новых)" из Главного Меню
@employer_responses_router.message(F.text.startswith(BTN_VIEW_RESPONSES_TEXT.split(" (")[0]))
async def msg_view_unread_responses_queue(message: types.Message, state: FSMContext):
    # await message.answer("Ищу новые отклики...") # Можно убрать, чтобы не было лишнего сообщения
    await fetch_and_display_first_unread(
        employer_user_id=message.from_user.id,
        bot_instance=message.bot,
        chat_id_to_reply=message.chat.id,
        state=state
        # original_message_with_button здесь None, т.к. это не от callback'а с push-уведомления
    )

# 3. Для Inline-кнопки "➡️ Следующий отклик"
@employer_responses_router.callback_query(F.data == NEXT_RESPONSE_CALLBACK_DATA)
async def cq_view_next_unread_response(callback_query: types.CallbackQuery, state: FSMContext):
    try:
        # Редактируем текущее сообщение с анкетой: убираем кнопку "Следующий отклик", оставляем только "Пожаловаться"
        current_data = await state.get_data()
        applicant_id_for_report = current_data.get('employer_viewing_applicant_user_id')
        
        updated_inline_kb = None
        if applicant_id_for_report: # Если есть ID соискателя для жалобы
            updated_inline_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🚩 Пожаловаться", callback_data=f"{REPORT_APPLICANT_CALLBACK_DATA_PREFIX}{applicant_id_for_report}")]
            ])
        
        # Редактируем только клавиатуру, текст и фото оставляем
        await callback_query.message.edit_reply_markup(reply_markup=updated_inline_kb)
        await callback_query.answer("Загружаю следующий...") 
    except Exception as e:
        print(f"Error editing prev message on next_response: {e}")
        await callback_query.answer("Загружаю следующий...", show_alert=False) # Отвечаем на callback в любом случае
    
    # Показываем следующий непросмотренный (отправляем новое сообщение)
    await fetch_and_display_first_unread(
        employer_user_id=callback_query.from_user.id,
        bot_instance=callback_query.bot,
        chat_id_to_reply=callback_query.message.chat.id,
        state=state
    )


@employer_responses_router.callback_query(F.data == "view_unread_responses_btn")
async def cq_view_unread_responses_from_push_button(callback_query: types.CallbackQuery, state: FSMContext):
    employer_user_id = callback_query.from_user.id
    
    # --- ПРОВЕРКА, СУЩЕСТВУЕТ ЛИ ЕЩЕ АКТИВНАЯ АНКЕТА СОИСКАТЕЛЯ ---
    async with AsyncSessionFactory() as session_check_profile, session_check_profile.begin():
        applicant_profile_exists = (await session_check_profile.execute(
            select(ApplicantProfile.id) # Просто проверяем наличие
            .where(ApplicantProfile.user_id == employer_user_id, ApplicantProfile.is_active == True) 
            # user_id_who_interacted - это ID соискателя в текущем хэндлере
        )).scalar_one_or_none()

    if not applicant_profile_exists:
        await state.clear() # Очищаем FSM соискателя
        await callback_query.message.answer(
            "Ваша анкета была изменена или удалена администратором. "
            "Просмотр остановлен. Пожалуйста, начните сначала.",
            reply_markup=ReplyKeyboardRemove()
        )
        from app.bot import start_keyboard # Локальный импорт
        await callback_query.message.answer("Выберите вашу роль:", reply_markup=start_keyboard)
        return 
    
    # Уведомление успешно сработало, можно его "погасить"
    if callback_query.message: # Если это сообщение от бота
        try:
            # Просто убираем кнопки, можно и текст изменить
            await callback_query.message.edit_text(
                text=f"{callback_query.message.text}\n\n(Переход к просмотру откликов...)", 
                reply_markup=None
            ) 
        except Exception as e_edit_notif:
            print(f"DEBUG: Could not edit notification message on click: {e_edit_notif}")
        # Очищаем ID активного уведомления в БД
        async with AsyncSessionFactory() as session, session.begin():
            await session.execute(
                update(EmployerProfile)
                .where(EmployerProfile.user_id == employer_user_id)
                .values(active_notification_message_id=None)
            )
    
    await callback_query.answer("Загружаю ваши отклики...")
    
    # Вызываем вашу существующую логику для показа первого непросмотренного отклика
    await fetch_and_display_first_unread(
        employer_user_id=employer_user_id,
        bot_instance=callback_query.bot,
        chat_id_to_reply=callback_query.message.chat.id,
        state=state,
        # original_message_with_button можно не передавать, так как мы уже обработали/удалили его
    )
    
@employer_responses_router.callback_query(F.data == "view_unread_responses_push_btn")
async def cq_view_unread_from_push_button_handler(callback_query: types.CallbackQuery, state: FSMContext):
    employer_user_id = callback_query.from_user.id
    message_to_process = callback_query.message # Сообщение с PUSH-кнопкой
    
    await callback_query.answer("Загружаю ваши отклики...") # Быстрый ответ пользователю

    # 1. "Погасить" PUSH-уведомление (удалить его кнопки и обновить текст)
    if message_to_process:
        try:
            await message_to_process.edit_text(
                text=f"{message_to_process.text}\n\n(Просматриваю отклики...)", # Добавляем к существующему тексту
                reply_markup=None # Убираем inline кнопку
            )
            print(f"DEBUG: PUSH notification message {message_to_process.message_id} edited (button removed).")
        except Exception as e_edit_notif:
            print(f"DEBUG: Could not edit PUSH notification {message_to_process.message_id} on click: {e_edit_notif}")
            # Если не удалось отредактировать, можно его просто удалить, чтобы не смущало
            try: await message_to_process.delete()
            except: pass
    
    # 2. Очистить active_notification_message_id в БД, т.к. пользователь отреагировал
    async with AsyncSessionFactory() as session, session.begin():
        await session.execute(
            update(EmployerProfile)
            .where(EmployerProfile.user_id == employer_user_id)
            .values(active_notification_message_id=None)
        )
        print(f"DEBUG: Cleared active_notification_message_id for employer {employer_user_id}")
    
    # 3. Запустить просмотр первого непросмотренного отклика 
    # (fetch_and_display_first_unread должна отправить НОВОЕ сообщение с анкетой соискателя и своими кнопками)
    await fetch_and_display_first_unread(
        employer_user_id=employer_user_id,
        bot_instance=callback_query.bot, # или message.bot
        chat_id_to_reply=message_to_process.chat.id, 
        state=state
    )
    
    
@employer_responses_router.callback_query(F.data.startswith(REPORT_APPLICANT_CALLBACK_DATA_PREFIX))
async def cq_report_applicant(callback_query: types.CallbackQuery, state: FSMContext):
    try:
        reported_applicant_user_id_str = callback_query.data[len(REPORT_APPLICANT_CALLBACK_DATA_PREFIX):]
        reported_applicant_user_id = int(reported_applicant_user_id_str)
    except (ValueError, TypeError, IndexError):
        await callback_query.answer("Ошибка в данных жалобы.", show_alert=True)
        return

    reporter_employer_user_id = callback_query.from_user.id
    complaint_obj_to_notify = None

    try:
        async with AsyncSessionFactory() as session, session.begin():
            # --- ПОЛУЧАЕМ ID ПРОФИЛЯ СОИСКАТЕЛЯ ---
            applicant_profile_q = await session.execute(
                select(ApplicantProfile.id).where(ApplicantProfile.user_id == reported_applicant_user_id)
            )
            reported_applicant_profile_id_val = applicant_profile_q.scalar_one_or_none()
            # ------------------------------------

            new_complaint = Complaint(
                reporter_user_id=reporter_employer_user_id,
                reported_user_id=reported_applicant_user_id, 
                reported_applicant_profile_id=reported_applicant_profile_id_val, 
                reported_employer_profile_id=None, # Явно указываем None
                status=ComplaintStatusEnum.NEW
            )
            session.add(new_complaint)
            await session.flush() 
            
            if new_complaint.id:
                complaint_obj_to_notify = new_complaint
                print(f"DEBUG: Complaint (ID: {new_complaint.id}) CREATED by employer {reporter_employer_user_id} on applicant {reported_applicant_user_id} (profile_id: {reported_applicant_profile_id_val})")
            else:
                print(f"CRITICAL ERROR: Complaint ID not generated for report by employer.")
                await callback_query.answer("Внутренняя ошибка при создании жалобы.", show_alert=True)
                return 
        
        if complaint_obj_to_notify:
            from app.handlers.admin_handlers import notify_admins_about_complaint
            try:
                await notify_admins_about_complaint(callback_query.bot, complaint_obj_to_notify)
            except Exception as e_notify:
                print(f"ERROR sending complaint notification to admins from cq_report_applicant: {e_notify}\n{traceback.format_exc()}")
        
        await callback_query.answer("Жалоба на соискателя отправлена.", show_alert=False) 
            
        # ... (ВАШ КОД ОБНОВЛЕНИЯ СООБЩЕНИЯ РАБОТОДАТЕЛЯ, как он был, он должен работать) ...
        current_message_text = callback_query.message.text if callback_query.message.text else callback_query.message.caption
        if not current_message_text: current_message_text = "Анкета соискателя."
        updated_text_for_employer = current_message_text + "\n\n<em>(✓ Вы отправили жалобу на этого соискателя)</em>"
        fsm_data = await state.get_data()
        has_next = fsm_data.get('employer_has_next_responses', False)
        remaining_count_for_button = fsm_data.get('employer_remaining_responses_count', 0)
        new_inline_buttons = []
        if has_next:
            next_button_text = f"➡️ Следующий отклик ({remaining_count_for_button})" if remaining_count_for_button > 0 else "➡️ Следующий отклик"
            new_inline_buttons.append([InlineKeyboardButton(text=next_button_text, callback_data=NEXT_RESPONSE_CALLBACK_DATA)])
        new_kb_after_report = InlineKeyboardMarkup(inline_keyboard=new_inline_buttons) if new_inline_buttons else None
        try:
            if callback_query.message.photo: 
                await callback_query.message.edit_caption(caption=updated_text_for_employer, reply_markup=new_kb_after_report, parse_mode="HTML")
            else: 
                await callback_query.message.edit_text(updated_text_for_employer, reply_markup=new_kb_after_report, parse_mode="HTML")
        except Exception as e_edit_msg:
            print(f"Failed to edit employer's message after reporting applicant: {e_edit_msg}")
            await callback_query.message.answer("Жалоба отправлена. Нажмите 'Продолжить просмотр откликов' в меню, если остались еще.", reply_markup=ReplyKeyboardRemove())


    except Exception as e:
        print(f"CRITICAL Error processing report on applicant: {e}\n{traceback.format_exc()}")
        await callback_query.answer("Произошла ошибка при отправке жалобы. Попробуйте позже.", show_alert=True)