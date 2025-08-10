# app/db/database.py
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from typing import AsyncGenerator 
from app.config import DATABASE_URL

engine = create_async_engine(DATABASE_URL, echo=True)

AsyncSessionFactory = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False
)

Base = declarative_base()

async def get_db_session() -> AsyncGenerator[AsyncSession, None]: # <--- ИЗМЕНИТЕ ЗДЕСЬ
    async with AsyncSessionFactory() as session:
        yield session

async def init_db_models():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)