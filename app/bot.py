# app/bot.py
import asyncio
import logging
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.context import FSMContext

from app.handlers.employer_responses_handlers import employer_responses_router
from app.config import BOT_TOKEN
from app.db.database import AsyncSessionFactory
from app.db.models import User, UserRole, ApplicantProfile, EmployerProfile, ReferralLink, ReferralUsage
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.sql import func

from app.handlers.registration_handlers import registration_router
from app.handlers.settings_handlers import settings_router, show_applicant_settings_menu, show_employer_main_menu
from app.handlers.browsing_handlers import browsing_router
from app.handlers.admin_handlers import admin_router
from app.middlewares.access_middleware import BanCheckMiddleware
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from app.services.scheduler_jobs import check_and_send_reengagement_notifications
from datetime import datetime, timezone
import functools
from app.services.scheduler_jobs import daily_check_employers_subscription

from app.keyboards.reply_keyboards import start_keyboard


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
logger = logging.getLogger(__name__)

bot_instance = Bot(token=BOT_TOKEN)
dp = Dispatcher()

dp.update.outer_middleware(BanCheckMiddleware())


@dp.message(CommandStart())
async def command_start_handler(message: Message, state: FSMContext, command: CommandObject) -> None:
    await state.clear() 
    
    user_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name
    last_name = message.from_user.last_name
    
    ref_code = command.args
    if ref_code:
        logger.info(f"User {user_id} started with referral code: {ref_code}")
        async with AsyncSessionFactory() as session:
            async with session.begin():
                link_result = await session.execute(
                    select(ReferralLink).where(ReferralLink.code == ref_code)
                )
                link = link_result.scalar_one_or_none()
                
                if link:
                    new_usage = ReferralUsage(link_id=link.id, user_id=user_id)
                    session.add(new_usage)
                    logger.info(f"Logged usage for link ID {link.id} by user {user_id}")
                else:
                    logger.warning(f"User {user_id} used an invalid referral code: {ref_code}")

    async with AsyncSessionFactory() as session:
        async with session.begin(): 
            stmt = insert(User).values(
                telegram_id=user_id,
                username=username,
                first_name=first_name,
                last_name=last_name,
            ).on_conflict_do_update(
                index_elements=['telegram_id'],
                set_={
                    'username': username,
                    'first_name': first_name,
                    'last_name': last_name,
                    'last_activity_date': func.now()
                }
            )
            await session.execute(stmt)
            logger.info(f"User {user_id} ({username}) data upserted by /start.")
            db_user = await session.get(User, user_id)
        
        if db_user:
            display_name_for_menu = db_user.first_name if db_user.first_name else message.from_user.first_name
            
            if db_user.role == UserRole.APPLICANT:
                async with session.begin(): 
                    applicant_profile_db = (await session.execute(
                        select(ApplicantProfile).where(ApplicantProfile.user_id == user_id)
                    )).scalar_one_or_none()

                if applicant_profile_db:
                    logger.info(f"User {user_id} is Applicant. Showing settings menu.")
                    await show_applicant_settings_menu(message, user_id, display_name_for_menu) 
                    return
                else:
                    logger.warning(f"User {user_id} has APPLICANT role but no profile. Offering role selection.")
            
            elif db_user.role == UserRole.EMPLOYER:
                async with session.begin(): 
                    employer_profile_db = (await session.execute(
                        select(EmployerProfile).where(EmployerProfile.user_id == user_id)
                    )).scalar_one_or_none()
                
                if employer_profile_db:
                    logger.info(f"User {user_id} is Employer. Showing employer main menu.")
                    await show_employer_main_menu(message, user_id, display_name_for_menu)
                    return
                else:
                    logger.warning(f"User {user_id} has EMPLOYER role but no profile. Offering role selection.")
        else:
            logger.error(f"User {user_id} NOT FOUND in DB after UPSERT in /start handler!")

        await message.answer(
            f"Привет, {message.from_user.full_name}!\n"
            "Что тебе нужно: работа или работник?\n\n",
            reply_markup=start_keyboard
        )
        

async def on_startup_scheduler(**kwargs): 
    bot_from_data: Bot = kwargs['bot']
    scheduler_from_data: AsyncIOScheduler = kwargs['scheduler_instance']
    
    print("SCHEDULER: on_startup_scheduler called. Attempting to add and start job.")
    try:
        scheduler_from_data.add_job(
            check_and_send_reengagement_notifications, 
            'interval', 
            minutes=1, 
            args=[bot_from_data],
            id="reengagement_job", 
            replace_existing=True
        )

        scheduler_from_data.add_job(
            daily_check_employers_subscription,
            'interval', 
            hours=24,  
            args=[bot_from_data],
            id="daily_subscription_check_job",
            replace_existing=True
        )
        
        if not scheduler_from_data.running:
            scheduler_from_data.start()
            print("SCHEDULER: APScheduler started successfully and job added.")
        else:
            print("SCHEDULER: APScheduler already running. Job (re)added.")
        scheduler_from_data.print_jobs()
    except Exception as e:
        print(f"SCHEDULER: Error starting or adding job: {e}")
        import traceback
        traceback.print_exc()


async def main() -> None:
    logger.info("Starting bot...")
    
    scheduler = AsyncIOScheduler(timezone="Europe/Kyiv")
    
    workflow_data = {"scheduler_instance": scheduler}
    
    dp.include_router(registration_router)
    dp.include_router(settings_router)
    dp.include_router(browsing_router)
    dp.include_router(employer_responses_router)
    dp.include_router(admin_router)
    
    dp.startup.register(on_startup_scheduler) 

    try:
        await dp.start_polling(bot_instance, **workflow_data)
    finally:
        if scheduler.running:
            print("SCHEDULER: Shutting down APScheduler...")
            scheduler.shutdown()
            print("SCHEDULER: APScheduler shut down.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped!")
