# app/states/editing_states.py
from aiogram.fsm.state import State, StatesGroup

class ApplicantEditProfile(StatesGroup):
    waiting_for_field_to_edit = State()
    editing_city = State()
    editing_gender = State()
    editing_age = State()
    editing_experience = State()

class EmployerEditProfile(StatesGroup):
    waiting_for_field_to_edit = State()
    editing_city = State()
    editing_company_name = State()
    editing_position = State()
    editing_salary = State()
    editing_min_age = State()
    editing_company_description = State()
    editing_work_format = State()
    editing_photo_option = State()
    editing_photo_upload = State()