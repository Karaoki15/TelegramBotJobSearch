from aiogram.types import ReplyKeyboardMarkup, KeyboardButton


start_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Я ищу работу")],
        [KeyboardButton(text="Я предлагаю работу")]
    ],
    resize_keyboard=True,
    one_time_keyboard=True
)

applicant_action_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="❤️"), KeyboardButton(text="👎")],
        [KeyboardButton(text="❓ Отправить вопрос"), KeyboardButton(text="🚩 Жалоба")],
        [KeyboardButton(text="⏹️ Остановить показ")]
    ],
    resize_keyboard=True
)
# Клавиатура для отмены ввода вопроса
cancel_question_input_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🚫 Отменить ввод вопроса")]
    ],
    resize_keyboard=True,
    one_time_keyboard=True 
)

continue_browsing_after_motivation_keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="▶️ Продолжить просмотр")]],
    resize_keyboard=True,
    one_time_keyboard=True 
)
