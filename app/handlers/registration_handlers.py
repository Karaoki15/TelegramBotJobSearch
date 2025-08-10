# app/handlers/registration_handlers.py
from aiogram import Router, F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, ReplyKeyboardRemove, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command
from aiogram import Bot

from app.states.registration_states import ApplicantRegistration
from app.db.database import AsyncSessionFactory
from app.db.models import User, UserRole
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.sql import func
from app.states.registration_states import EmployerRegistration
from app.db.models import EmployerProfile, WorkFormatEnum
from sqlalchemy import select, delete
from app.db.models import User, UserRole, ApplicantProfile, EmployerProfile, WorkFormatEnum, GenderEnum
from app.handlers.settings_handlers import show_employer_main_menu, applicant_continue_browsing
from app.handlers.browsing_handlers import show_next_employer_profile
import traceback
from app.keyboards.reply_keyboards import start_keyboard
from app.utils.validators import contains_urls
from aiogram.exceptions import TelegramBadRequest
from app.config import CHANNEL_ID, CHANNEL_URL



async def is_user_subscribed_to_channel(user_id: int, bot: Bot) -> bool:
    """Проверяет, подписан ли пользователь на обязательный канал."""
    if not CHANNEL_ID:
        return True
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status in ["creator", "administrator", "member", "restricted"]
    except TelegramBadRequest as e:
        if "user not found" in e.message:
            return False
        # В некоторых случаях может быть "Bad Request: chat member not found"
        if "chat member not found" in e.message:
            return False
        print(f"Unexpected TelegramBadRequest error checking channel subscription for user {user_id}: {e}")
        return False # Считаем, что не подписан при других ошибках
    except Exception as e:
        print(f"Error checking channel subscription for user {user_id}: {e}")
        return False

# Создаем роутер для этих хэндлеров
registration_router = Router()
from app.handlers.settings_handlers import show_applicant_settings_menu 
# --- РЕГИСТРАЦИЯ СОИСКАТЕЛЯ ---

# Хэндлер для кнопки "Я ищу работу"
@registration_router.message(F.text == "Я ищу работу")
async def start_applicant_registration(message: Message, state: FSMContext):
    await state.set_state(ApplicantRegistration.waiting_for_city)
    await message.answer(
        "Из какого ты города?",
        reply_markup=ReplyKeyboardRemove() # Убираем предыдущие кнопки
    )
    
    


# Команда отмены в процессе регистрации
# Можно сделать более общую команду отмены, но пока так для регистрации
@registration_router.message(Command("cancel_registration"))
@registration_router.message(F.text.casefold() == "отмена") # И текстовая отмена
async def cancel_registration_handler(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("Нет активного процесса для отмены.", reply_markup=ReplyKeyboardRemove())
        return

    # Можно будет потом добавить очистку данных из state, если они там уже есть
    # data = await state.get_data()
    # if data:
    #     logging.info(f"Cancelling registration. State data was: {data}")
    
    await state.clear()
    
    # Возвращаем стартовую клавиатуру выбора роли
    from app.bot import start_keyboard # Импортируем стартовую клавиатуру
    await message.answer(
        "Заполнение анкеты отменено. Вы можете начать заново.",
        reply_markup=start_keyboard
    )

# --- Дальше будут хэндлеры для каждого шага анкеты соискателя ---

# Шаг 1: Получение города
@registration_router.message(ApplicantRegistration.waiting_for_city, F.text)
async def process_applicant_city(message: Message, state: FSMContext):
    city = message.text.strip().capitalize()
    
    if contains_urls(city):
        await message.answer("Пожалуйста, не используйте ссылки в описании вашего опыта. Введите текст снова:")
        return # Оставляем пользователя в том же состоянии для повторного ввода
    
    if not (2 <= len(city) <= 30): # Простое ограничение длины
        await message.answer("Название города должно быть от 2 до 30 символов. Пожалуйста, введите снова:")
        return
    await state.update_data(city=city)
    
    # Запрашиваем пол
    await state.set_state(ApplicantRegistration.waiting_for_gender)
    gender_kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Мужской"), KeyboardButton(text="Женский")],
            # [KeyboardButton(text="Другой")] # Если нужно
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await message.answer("Теперь определимся с полом", reply_markup=gender_kb)

# Шаг 2: Получение пола
@registration_router.message(ApplicantRegistration.waiting_for_gender, F.text.in_({"Мужской", "Женский"})) # "Другой"
async def process_applicant_gender(message: Message, state: FSMContext):
    # Сохраняем значение из Enum GenderEnum (app/db/models.py), если хотите типизацию
    # gender_map = {"Мужской": GenderEnum.MALE, "Женский": GenderEnum.FEMALE} # "Другой": GenderEnum.OTHER
    # await state.update_data(gender=gender_map[message.text])
    await state.update_data(gender_text=message.text) # Пока сохраним текст для простоты отображения

    # Запрашиваем возраст
    await state.set_state(ApplicantRegistration.waiting_for_age)
    await message.answer("Сколько тебе лет?", reply_markup=ReplyKeyboardRemove())

@registration_router.message(ApplicantRegistration.waiting_for_gender) # Если ввели не тот пол
async def process_invalid_applicant_gender(message: Message, state: FSMContext):
    await message.answer("Пожалуйста, выберите пол из предложенных вариантов на клавиатуре.")

# Шаг 3: Получение возраста
@registration_router.message(ApplicantRegistration.waiting_for_age, F.text)
async def process_applicant_age(message: Message, state: FSMContext):
    age_text = message.text.strip()
    
    if contains_urls(age_text):
        await message.answer("Пожалуйста, не используйте ссылки в описании вашего опыта. Введите текст снова:")
        return # Оставляем пользователя в том же состоянии для повторного ввода
    
    if not age_text.isdigit():
        await message.answer("Пожалуйста, введите возраст числом.")
        return
    age = int(age_text)
    if not (16 <= age <= 70): # Примерные рамки
        await message.answer("Возраст должен быть в диапазоне от 16 до 70 лет. Пожалуйста, введите корректный возраст:")
        return
    await state.update_data(age=age)

    # Запрашиваем опыт работы
    await state.set_state(ApplicantRegistration.waiting_for_experience)
    await message.answer("У тебя есть опыт работы?")

# Шаг 4: Получение опыта работы
@registration_router.message(ApplicantRegistration.waiting_for_experience, F.text)
async def process_applicant_experience(message: Message, state: FSMContext):
    experience = message.text.strip()
    
    if contains_urls(experience):
        await message.answer("Пожалуйста, не используйте ссылки в описании вашего опыта. Введите текст снова:")
        return 
    
    if not (2 <= len(experience) <= 1000): # Ограничение для Text поля
        await message.answer("Описание опыта должно быть от 2 до 1000 символов. Пожалуйста, введите снова:")
        return
    await state.update_data(experience=experience)

    # Запрашиваем контакт
    await state.set_state(ApplicantRegistration.waiting_for_contact)
    contact_kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Поделиться контактом", request_contact=True)],
            [KeyboardButton(text="Отмена")] # Кнопка отмены на этом шаге
        ],
        resize_keyboard=True,
        one_time_keyboard=True # Может быть лучше False, чтобы кнопка отмены осталась
    )
    await message.answer(
        "Поделись своим контактом, это обязательно для регистрации.\n",
        reply_markup=contact_kb
    )

# Шаг 5: Получение контакта
@registration_router.message(ApplicantRegistration.waiting_for_contact, F.contact)
async def process_applicant_contact(message: Message, state: FSMContext):
    if message.contact.user_id != message.from_user.id:
        await message.answer("Пожалуйста, поделитесь своим собственным контактом.", reply_markup=ReplyKeyboardRemove())
        # Можно вернуть на шаг запроса контакта или предложить отмену
        await state.set_state(ApplicantRegistration.waiting_for_contact) # Возврат к ожиданию контакта
        return

    contact_phone = message.contact.phone_number
    await state.update_data(contact_phone=contact_phone)
    
    # Переходим к подтверждению анкеты
    user_data = await state.get_data()
    
    # Формируем текст анкеты для проверки
    # gender_text = user_data.get('gender_text', 'Не указан') # если храним текст
    # если бы хранили enum: gender_display = "Мужской" if user_data.get('gender') == GenderEnum.MALE else "Женский" 
    gender_text = user_data.get('gender_text', 'Не указан')
    
    profile_text = (
        f"Проверьте вашу анкету соискателя:\n\n"
        f"Город: {user_data.get('city')}\n"
        f"Пол: {gender_text}\n"
        f"Возраст: {user_data.get('age')}\n"
        f"Опыт работы: {user_data.get('experience')}\n"
        f"Контактный телефон: {contact_phone}\n\n" # Добавляем + для наглядности
        f"Все верно?"
    )
    
    await state.set_state(ApplicantRegistration.waiting_for_confirmation)
    confirmation_kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Да")],
            [KeyboardButton(text="Заполнить заново")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True # или False, чтобы оставить кнопки
    )
    await message.answer(profile_text, reply_markup=confirmation_kb)

@registration_router.message(ApplicantRegistration.waiting_for_contact, F.text.casefold() == "отмена")
async def process_cancel_at_contact(message: Message, state: FSMContext):
    # Вызываем общий обработчик отмены
    await cancel_registration_handler(message, state)

@registration_router.message(ApplicantRegistration.waiting_for_contact) # Если отправил не контакт и не "Отмена"
async def process_invalid_applicant_contact(message: Message, state: FSMContext):
    await message.answer("Пожалуйста, используйте кнопку 'Поделиться контактом' или отправьте 'Отмена'.")

# Шаг 6: Подтверждение анкеты
@registration_router.message(ApplicantRegistration.waiting_for_confirmation, F.text == "Да")
async def process_applicant_confirmation(message: Message, state: FSMContext):
    user_data = await state.get_data()
    user_id = message.from_user.id
    # display_name_after_save больше не нужен, так как мы не вызываем show_applicant_settings_menu напрямую отсюда с этим именем

    try:
        async with AsyncSessionFactory() as session, session.begin(): # Одна транзакция для всех операций
            # 1. Проверяем, не был ли пользователь ранее Работодателем, и если да - удаляем его профиль работодателя
            current_user_role_q = await session.execute(
                select(User.role).where(User.telegram_id == user_id)
            )
            current_user_db_role = current_user_role_q.scalar_one_or_none()

            if current_user_db_role == UserRole.EMPLOYER:
                await session.execute(
                    delete(EmployerProfile).where(EmployerProfile.user_id == user_id)
                )
                print(f"DEBUG: Employer profile for user {user_id} deleted as they are registering as Applicant.")

            # 2. Обновляем/создаем запись User с ролью APPLICANT и телефоном
            user_update_stmt = (
                insert(User).values(
                    telegram_id=user_id,
                    username=message.from_user.username, # Обновляем username и first/last name
                    first_name=message.from_user.first_name,
                    last_name=message.from_user.last_name,
                    contact_phone=user_data.get('contact_phone'), 
                    role=UserRole.APPLICANT # Устанавливаем роль
                ).on_conflict_do_update(
                    index_elements=['telegram_id'],
                    set_={
                        'username': message.from_user.username,
                        'first_name': message.from_user.first_name,
                        'last_name': message.from_user.last_name,
                        'contact_phone': user_data.get('contact_phone'),
                        'role': UserRole.APPLICANT,
                        'last_activity_date': func.now()
                    }
                )
            )
            await session.execute(user_update_stmt)

            # 3. Создаем/обновляем ApplicantProfile
            gender_text_map = {"Мужской": GenderEnum.MALE, "Женский": GenderEnum.FEMALE}
            applicant_profile_values = {
                'user_id': user_id,
                'city': user_data.get('city'),
                'gender': gender_text_map.get(user_data.get('gender_text')),
                'age': user_data.get('age'),
                'experience': user_data.get('experience'),
                'is_active': True, # Новая анкета всегда активна
                'deactivation_date': None # Сбрасываем дату деактивации, если она была
            }
            applicant_profile_stmt = (
                insert(ApplicantProfile).values(**applicant_profile_values)
                .on_conflict_do_update( # Если профиль уже был (например, ошибка на прошлом шаге и повтор)
                    index_elements=[ApplicantProfile.user_id], # Обновляем по user_id
                    set_=applicant_profile_values # Перезаписываем все поля
                )
            )
            await session.execute(applicant_profile_stmt)
            
            # Коммит произойдет автоматически при выходе из блока "async with session.begin():"
            print(f"DEBUG: Applicant profile for user {user_id} saved/updated successfully.")

        # --- Действия ПОСЛЕ успешной транзакции ---
        await state.clear() # Очищаем состояние FSM регистрации
        
        await message.answer(
            "✅", 
            reply_markup=ReplyKeyboardRemove() # Убираем кнопки "Подтвердить анкету" и т.д.
        )
        
        # Запускаем просмотр анкет работодателей
        # Убедитесь, что show_next_employer_profile импортирована из app.handlers.browsing_handlers
        await show_next_employer_profile(message, user_id, state) # Передаем state для FSM просмотра

    except Exception as e:
        print(f"CRITICAL ERROR during applicant confirmation or starting browsing: {e}\n{traceback.format_exc()}")
        await message.answer(
            "Произошла серьезная ошибка при сохранении вашей анкеты или запуске поиска.\n"
            "Пожалуйста, попробуйте заполнить анкету заново через некоторое время или свяжитесь с поддержкой, если проблема повторится.", 
            reply_markup=ReplyKeyboardRemove()
        )
        # Очищаем состояние FSM, если оно еще не было очищено
        current_fsm_state = await state.get_state()
        if current_fsm_state is not None:
             await state.clear()
        # Возвращаем пользователя к выбору роли, так как процесс не завершился корректно
        await message.answer("Пожалуйста, выберите вашу роль:", reply_markup=start_keyboard)



@registration_router.message(ApplicantRegistration.waiting_for_confirmation, F.text == "Заполнить заново")
async def process_applicant_fill_again(message: Message, state: FSMContext):
    await state.clear() # Очищаем текущие данные и состояние
    # Снова предлагаем выбрать роль (так как /start сейчас делает то же самое)
    # По-хорошему, здесь можно было бы сразу перевести на ApplicantRegistration.waiting_for_city
    # Но для согласованности с "Примечание: Если пользователь был в процессе заполнения анкеты и ввел /start,
    # его предыдущий ввод не сохраняется, процесс начинается заново." - возврат к выбору роли логичен.
    from app.bot import start_keyboard # Импортируем стартовую клавиатуру
    await message.answer("Хорошо, давайте начнем заполнение анкеты заново. Выберите вашу роль:", reply_markup=start_keyboard)


@registration_router.message(ApplicantRegistration.waiting_for_confirmation, F.text == "Отмена")
async def process_applicant_cancel_at_confirmation(message: Message, state: FSMContext):
    await cancel_registration_handler(message, state) # Используем общий обработчик отмены


@registration_router.message(ApplicantRegistration.waiting_for_confirmation) # Если что-то не то на шаге подтверждения
async def process_invalid_applicant_confirmation(message: Message, state: FSMContext):
    await message.answer("Пожалуйста, используйте кнопки: 'Да', 'Заполнить заново' или 'Отмена'.")
    

# --- РЕГИСТРАЦИЯ РАБОТОДАТЕЛЯ ---

# Хэндлер для кнопки "Я предлагаю работу"
@registration_router.message(F.text == "Я предлагаю работу")
async def start_employer_registration(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id

    # 1. СРАЗУ ПРОВЕРЯЕМ ПОДПИСКУ
    is_subscribed = await is_user_subscribed_to_channel(user_id, bot)

    if not is_subscribed:
        # 2. ЕСЛИ НЕ ПОДПИСАН - ПРОСИМ ПОДПИСАТЬСЯ
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Перейти в канал", url=CHANNEL_URL)],
            [InlineKeyboardButton(text="✅ Я подписался", callback_data="check_subscription_before_register")]
        ])
        await message.answer(
            "Чтобы предлагать работу, пожалуйста, подпишитесь на наш канал. "
            "Это открывает доступ ко всем функциям для работодателей.",
            reply_markup=kb
        )
        return # Останавливаем выполнение, ждем нажатия кнопки

    # 3. ЕСЛИ ПОДПИСАН - ПРОДОЛЖАЕМ КАК ОБЫЧНО
    await state.set_state(EmployerRegistration.waiting_for_city)
    await message.answer(
        "В каком городе находится ваша компания?",
        reply_markup=ReplyKeyboardRemove()
    )

@registration_router.callback_query(F.data == "check_subscription_before_register")
async def handle_subscription_check_before_register(callback_query: CallbackQuery, state: FSMContext, bot: Bot):
    is_subscribed = await is_user_subscribed_to_channel(callback_query.from_user.id, bot)

    if is_subscribed:
        # Если подписка есть, удаляем сообщение с кнопками и начинаем регистрацию
        await callback_query.answer("Отлично, спасибо! Начинаем.", show_alert=False)
        try:
            await callback_query.message.delete()
        except Exception:
            pass
        
        # Запускаем тот же шаг, что и при успешной проверке
        await state.set_state(EmployerRegistration.waiting_for_city)
        await callback_query.message.answer(
            "В каком городе находится ваша компания?",
            reply_markup=ReplyKeyboardRemove()
        )
    else:
        # Если все еще не подписан, просто информируем
        await callback_query.answer("Похоже, вы все еще не подписаны. Пожалуйста, подпишитесь и нажмите кнопку снова.", show_alert=True)
# --- КОНЕЦ БЛОКА ДЛЯ ДОБАВЛЕНИЯ ---
# --- Дальше хэндлеры для пошагового заполнения анкеты РАБОТОДАТЕЛЯ ---

# Шаг 1: Получение города
@registration_router.message(EmployerRegistration.waiting_for_city, F.text)
async def process_employer_city(message: Message, state: FSMContext):
    city = message.text.strip().capitalize() # Нормализация
    
    if contains_urls(city):
        await message.answer("Пожалуйста, не используйте ссылки в описании компании/вакансии. Введите текст снова:")
        return
    
    if not (2 <= len(city) <= 100):
        await message.answer("Название города должно быть от 2 до 100 символов. Пожалуйста, введите снова:")
        return
    await state.update_data(city=city)
    
    await state.set_state(EmployerRegistration.waiting_for_company_name)
    await message.answer("Название вашей компании / проекта?")

# Шаг 2: Получение названия компании
@registration_router.message(EmployerRegistration.waiting_for_company_name, F.text)
async def process_employer_company_name(message: Message, state: FSMContext):
    company_name = message.text.strip()
    
    if contains_urls(company_name):
        await message.answer("Пожалуйста, не используйте ссылки в описании компании/вакансии. Введите текст снова:")
        return
    
    if not (2 <= len(company_name) <= 200):
        await message.answer("Название компании должно быть от 2 до 200 символов. Введите снова:")
        return
    await state.update_data(company_name=company_name)

    await state.set_state(EmployerRegistration.waiting_for_position)
    await message.answer("На какую позицию вы ищете сотрудника?")

# Шаг 3: Получение позиции
@registration_router.message(EmployerRegistration.waiting_for_position, F.text)
async def process_employer_position(message: Message, state: FSMContext):
    position = message.text.strip()
    
    if contains_urls(position):
        await message.answer("Пожалуйста, не используйте ссылки в описании компании/вакансии. Введите текст снова:")
        return
    
    if not (3 <= len(position) <= 100):
        await message.answer("Название позиции должно быть от 3 до 100 символов. Введите снова:")
        return
    await state.update_data(position=position)

    await state.set_state(EmployerRegistration.waiting_for_salary)
    await message.answer("Укажите предлагаемую ставку / зарплату:")

# Шаг 4: Получение ЗП
@registration_router.message(EmployerRegistration.waiting_for_salary, F.text)
async def process_employer_salary(message: Message, state: FSMContext):
    salary = message.text.strip()
    
    if contains_urls(salary):
        await message.answer("Пожалуйста, не используйте ссылки в описании компании/вакансии. Введите текст снова:")
        return
    
    if not (3 <= len(salary) <= 100):
        await message.answer("Информация о зарплате должна быть от 3 до 100 символов. Введите снова:")
        return
    await state.update_data(salary=salary)

    await state.set_state(EmployerRegistration.waiting_for_min_age)
    await message.answer("Минимальный возраст кандидата?")

# Шаг 5: Получение минимального возраста
@registration_router.message(EmployerRegistration.waiting_for_min_age, F.text)
async def process_employer_min_age(message: Message, state: FSMContext):
    min_age_text = message.text.strip()
    min_age = None
    if min_age_text == "-" or min_age_text == "0":
        min_age = None # или 0, в зависимости от того, как хотите хранить "не важно"
    elif min_age_text.isdigit():
        age_val = int(min_age_text)
        if 16 <= age_val <= 70:
            min_age = age_val
        else:
            await message.answer("Возраст должен быть в диапазоне от 16 до 70 лет или '-' если не важно. Введите корректно:")
            return
    else:
        await message.answer("Пожалуйста, введите возраст числом или '-' если не важно.")
        return
        
    await state.update_data(min_age_candidate=min_age)

    await state.set_state(EmployerRegistration.waiting_for_company_description)
    await message.answer("Краткое описание компании / вакансии:")

# Шаг 6: Получение описания
@registration_router.message(EmployerRegistration.waiting_for_company_description, F.text)
async def process_employer_description(message: Message, state: FSMContext):
    description = message.text.strip()
    
    if contains_urls(description):
        await message.answer("Пожалуйста, не используйте ссылки в описании компании/вакансии. Введите текст снова:")
        return
    
    if not (10 <= len(description) <= 700):
        await message.answer("Описание должно быть от 10 до 2000 символов. Введите снова:")
        return
    await state.update_data(description=description)

    await state.set_state(EmployerRegistration.waiting_for_work_format)
    work_format_kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Офлайн"), KeyboardButton(text="Онлайн")],
            # [KeyboardButton(text="Гибрид")] # Если нужно
        ],
        resize_keyboard=True, one_time_keyboard=True
    )
    await message.answer("Офлайн / онлайн работа?", reply_markup=work_format_kb)

# Шаг 7: Получение формата работы
@registration_router.message(EmployerRegistration.waiting_for_work_format, F.text.in_({"Офлайн", "Онлайн"})) # "Гибрид"
async def process_employer_work_format(message: Message, state: FSMContext):
    await state.update_data(work_format_text=message.text)

    # ---- НОВЫЙ ШАГ: Спрашиваем про фото ----
    await state.set_state(EmployerRegistration.waiting_for_photo_option)
    photo_option_kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Добавить фото")],
            [KeyboardButton(text="Пропустить (без фото)")]
        ],
        resize_keyboard=True, one_time_keyboard=True
    )
    await message.answer("Добавить фото к вашей вакансии?", reply_markup=photo_option_kb)

# Новый хэндлер для обработки выбора опции по фото
@registration_router.message(EmployerRegistration.waiting_for_photo_option, F.text == "Добавить фото")
async def process_employer_add_photo(message: Message, state: FSMContext):
    await state.set_state(EmployerRegistration.waiting_for_photo_upload)
    await message.answer("Отлично! Пожалуйста, отправьте одну фотографию.", reply_markup=ReplyKeyboardRemove())

@registration_router.message(EmployerRegistration.waiting_for_photo_option, F.text == "Пропустить (без фото)")
async def process_employer_skip_photo(message: Message, state: FSMContext):
    await state.update_data(photo_file_id=None) # Явно указываем, что фото нет
    await show_employer_profile_for_confirmation(message, state) # Переходим к подтверждению

# Новый хэндлер для получения загруженной фотографии
@registration_router.message(EmployerRegistration.waiting_for_photo_upload, F.photo)
async def process_employer_photo_upload(message: Message, state: FSMContext):
    # Берем фото наибольшего размера из доступных (последнее в списке sizes)
    photo_file_id = message.photo[-1].file_id
    await state.update_data(photo_file_id=photo_file_id)
    await message.answer("Фотография принята!")
    await show_employer_profile_for_confirmation(message, state) # Переходим к подтверждению

@registration_router.message(EmployerRegistration.waiting_for_photo_upload, ~F.photo) # Если отправили не фото
async def process_employer_wrong_photo_upload(message: Message, state: FSMContext):
    await message.answer("Пожалуйста, отправьте именно фотографию или вернитесь назад, если передумали (команду отмены мы добавим).")
    # Тут можно добавить кнопку "Пропустить фото" или "Отмена"


# Вспомогательная функция для показа анкеты перед подтверждением
# (вынесем логику из process_employer_work_format)
async def show_employer_profile_for_confirmation(message: Message, state: FSMContext):
    user_data = await state.get_data()
    min_age_display = user_data.get('min_age_candidate') if user_data.get('min_age_candidate') is not None else "Не указан"
    photo_added_text = "Да" if user_data.get('photo_file_id') else "Нет"

    profile_text = (
        f"Проверьте анкету вашей вакансии:\n\n"
        f"Город: {user_data.get('city')}\n"
        f"Название компании: {user_data.get('company_name')}\n"
        f"Позиция: {user_data.get('position')}\n"
        f"Зарплата: {user_data.get('salary')}\n"
        f"Мин. возраст кандидата: {min_age_display}\n"
        f"Описание: {user_data.get('description')}\n"
        f"Формат работы: {user_data.get('work_format_text')}\n"
        f"Фотография добавлена: {photo_added_text}\n\n" 
        f"Все верно?"
    )

    await state.set_state(EmployerRegistration.waiting_for_confirmation)
    confirmation_kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Да")],
            [KeyboardButton(text="Заполнить заново")],
            
        ],
        resize_keyboard=True, one_time_keyboard=True
    )
    # Если фото есть, можно попробовать отправить его вместе с текстом
    photo_file_id_to_send = user_data.get('photo_file_id')
    if photo_file_id_to_send:
        try:
            await message.bot.send_photo(chat_id=message.chat.id, photo=photo_file_id_to_send, caption=profile_text, reply_markup=confirmation_kb)
        except Exception as e:
            print(f"Не удалось отправить фото при подтверждении: {e}")
            # Если фото не отправилось (например, file_id невалиден или бот его уже не найдет), отправляем только текст
            await message.answer(profile_text, reply_markup=confirmation_kb)
    else:
        await message.answer(profile_text, reply_markup=confirmation_kb)


# Шаг 8: Подтверждение анкеты Работодателя
@registration_router.message(EmployerRegistration.waiting_for_confirmation, F.text == "Да")
async def process_employer_confirmation(message: Message, state: FSMContext):
    user_data = await state.get_data()
    user_id = message.from_user.id
    display_name_after_save = message.from_user.first_name

    try:
        async with AsyncSessionFactory() as session:
            async with session.begin(): # Начинаем транзакцию, commit/rollback будет автоматическим

                # 0. Получаем текущую информацию о пользователе из БД (если есть)
                # Это нужно, чтобы знать его предыдущую роль
                current_user_db_data = (await session.execute(
                    select(User.role).where(User.telegram_id == user_id)
                )).scalar_one_or_none()

                # Если пользователь был соискателем (UserRole.APPLICANT) и сейчас становится работодателем,
                # то удаляем его профиль соискателя.
                if current_user_db_data == UserRole.APPLICANT:
                    await session.execute(
                        delete(ApplicantProfile).where(ApplicantProfile.user_id == user_id)
                    )
                    print(f"DEBUG: Applicant profile for user {user_id} deleted due to role change to Employer.")


                user_update_stmt = (
                    insert(User)
                    .values(
                        telegram_id=user_id, 
                        # username=telegram_username, # Если решили обновлять
                        # first_name=telegram_first_name,
                        # last_name=telegram_last_name,
                        role=UserRole.EMPLOYER # Устанавливаем новую роль
                    )
                    .on_conflict_do_update(
                        index_elements=['telegram_id'],
                        set_={
                            # 'username': telegram_username, # Если решили обновлять
                            # 'first_name': telegram_first_name,
                            # 'last_name': telegram_last_name,
                            'role': UserRole.EMPLOYER, # Убеждаемся, что роль обновляется
                            'last_activity_date': func.now()
                        }
                    )
                )
                await session.execute(user_update_stmt)

                # 2. Создаем или обновляем профиль работодателя
                work_format_map = {"Офлайн": WorkFormatEnum.OFFLINE, "Онлайн": WorkFormatEnum.ONLINE}
                                 # "Гибрид": WorkFormatEnum.HYBRID, если добавили
                
                employer_profile_values = {
                    'user_id': user_id,
                    'city': user_data.get('city'),
                    'company_name': user_data.get('company_name'),
                    'position': user_data.get('position'),
                    'salary': user_data.get('salary'),
                    'min_age_candidate': user_data.get('min_age_candidate'),
                    'description': user_data.get('description'),
                    'work_format': work_format_map.get(user_data.get('work_format_text')), # work_format_map должен быть определен
                    'photo_file_id': user_data.get('photo_file_id'), # <--- СОХРАНЕНИЕ photo_file_id
                    'is_active': True
                }
                user_db_data_temp = await session.get(User, user_id) 
                if user_db_data_temp and user_db_data_temp.first_name:
                    display_name_after_save = user_db_data_temp.first_name
                                 # Убедитесь что work_format_map определен где-то выше в этой функции или глобально
                if 'work_format_map' not in locals() and 'work_format_map' not in globals():
                     work_format_map = {"Офлайн": WorkFormatEnum.OFFLINE, "Онлайн": WorkFormatEnum.ONLINE} # "Гибрид": WorkFormatEnum.HYBRID
                
                employer_profile_stmt = (
                    insert(EmployerProfile).values(**employer_profile_values)
                    .on_conflict_do_update(
                        index_elements=[EmployerProfile.user_id], # Конфликт по user_id (уникальный FK)
                        set_=employer_profile_values # Обновляем все поля, включая is_active
                    )
                )
                await session.execute(employer_profile_stmt)
            
        await state.clear()
        await message.answer(
            "✅",
            reply_markup=ReplyKeyboardRemove()
        )
        # СРАЗУ ПОКАЗЫВАЕМ ГЛАВНОЕ МЕНЮ РАБОТОДАТЕЛЯ
        await show_employer_main_menu(message, user_id, display_name_after_save) # <--- ВЫЗЫВАЕМ МЕНЮ

    except Exception as e:
        print(f"Ошибка при сохранении анкеты работодателя: {e}") # Логирование здесь важно
        import traceback
        traceback.print_exc() # Выведет полный traceback ошибки
        await message.answer("Произошла ошибка при сохранении вашей анкеты. Пожалуйста, попробуйте позже или свяжитесь с поддержкой.")
        await state.clear() # Важно очистить состояние и при ошибке
        


# Хэндлеры для "Заполнить заново" и "Отмена" на шаге подтверждения для Работодателя
# Они могут быть почти идентичны тем, что для соискателя, или вызывать общие функции
@registration_router.message(EmployerRegistration.waiting_for_confirmation, F.text == "Заполнить заново")
async def process_employer_fill_again(message: Message, state: FSMContext):
    await cancel_registration_handler(message, state) # или более специфичная логика

@registration_router.message(EmployerRegistration.waiting_for_confirmation, F.text == "Отмена")
async def process_employer_cancel_at_confirmation(message: Message, state: FSMContext):
    await cancel_registration_handler(message, state)

@registration_router.message(EmployerRegistration.waiting_for_confirmation) 
async def process_invalid_employer_confirmation(message: Message, state: FSMContext):
    await message.answer("Пожалуйста, используйте кнопки: 'Да', 'Заполнить заново' или 'Отмена'.")