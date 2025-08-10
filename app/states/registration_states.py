# app/states/registration_states.py
from aiogram.fsm.state import State, StatesGroup

class ApplicantRegistration(StatesGroup):
    waiting_for_city = State()
    waiting_for_gender = State()
    waiting_for_age = State()
    waiting_for_experience = State()
    waiting_for_contact = State() 
    waiting_for_confirmation = State()

class EmployerRegistration(StatesGroup):
    waiting_for_city = State()
    waiting_for_company_name = State()
    waiting_for_position = State()
    waiting_for_salary = State()
    waiting_for_min_age = State()
    waiting_for_company_description = State()
    waiting_for_work_format = State() 
    waiting_for_photo_option = State() 
    waiting_for_photo_upload = State() 
    waiting_for_confirmation = State()