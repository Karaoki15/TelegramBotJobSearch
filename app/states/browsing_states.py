from aiogram.fsm.state import State, StatesGroup


class ApplicantBrowsingStates(StatesGroup):
    asking_question = State()
    watching_motivation = State()
