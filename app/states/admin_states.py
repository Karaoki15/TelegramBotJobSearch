from aiogram.fsm.state import State, StatesGroup

class AdminStates(StatesGroup):
    in_panel = State()
    adding_dummy_employer_city = State()
    adding_dummy_employer_confirmation = State()
    editing_antispam_dummy_text = State()
    editing_antispam_dummy_photo = State()
    adding_motivation_type = State()
    adding_motivation_file_id = State()
    adding_motivation_text = State()
    find_user_by_id_input = State()
    block_user_by_id_input = State()
    unblock_user_by_id_input = State()
    find_user_id_input = State()
    viewing_user_details = State()
    adding_motivation_file = State()
    adding_motivation_text_caption = State()
    adding_motivation_confirmation = State()

class AdminAddDummyEmployer(StatesGroup):
    waiting_for_city = State()
    waiting_for_company_name = State()
    waiting_for_position = State()
    waiting_for_salary = State()
    waiting_for_min_age = State()
    waiting_for_company_description = State()
    waiting_for_work_format = State()
    waiting_for_photo = State()
    waiting_for_confirmation = State()

class AdminReferralManagement(StatesGroup):
    waiting_for_name = State()
