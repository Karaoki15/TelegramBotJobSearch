# app/db/models.py
from sqlalchemy import Column, BigInteger, String, DateTime, Boolean, Enum as SQLAlchemyEnum, Integer, Text, ForeignKey, Index
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship 
from app.db.database import Base
import enum
import sqlalchemy as sa 

class UserRole(enum.Enum):
    APPLICANT = "applicant"
    EMPLOYER = "employer"

class GenderEnum(enum.Enum):
    MALE = "male"
    FEMALE = "female"
    OTHER = "other"

class WorkFormatEnum(enum.Enum):
    OFFLINE = "offline"
    ONLINE = "online"
    HYBRID = "hybrid"
    
class InteractionTypeEnum(enum.Enum):
    LIKE = "like"
    DISLIKE = "dislike"
    QUESTION_SENT = "question_sent"
    
class ComplaintStatusEnum(enum.Enum):
    NEW = "new"
    VIEWED = "viewed"
    RESOLVED = "resolved"


class User(Base):
    __tablename__ = "users"

    telegram_id = Column(BigInteger, primary_key=True, index=True, unique=True)
    username = Column(String, nullable=True)
    first_name = Column(String, nullable=True)
    last_name = Column(String, nullable=True)
    role = Column(SQLAlchemyEnum(UserRole), nullable=True)
    contact_phone = Column(String, nullable=True)
    registration_date = Column(DateTime(timezone=True), server_default=func.now())
    last_activity_date = Column(DateTime(timezone=True), onupdate=func.now(), server_default=func.now())
    is_banned = Column(Boolean, default=False, nullable=False) # Лучше nullable=False, default=False
    last_reengagement_notif_sent_at = Column(DateTime(timezone=True), nullable=True)
    applicant_profile = relationship("ApplicantProfile", back_populates="user", uselist=False, cascade="all, delete-orphan")
    employer_profile = relationship(
        "EmployerProfile", 
        primaryjoin="User.telegram_id == EmployerProfile.user_id",
        back_populates="user_owner",
        uselist=False, 
        cascade="all, delete-orphan"
    )
    created_dummy_profiles = relationship(
        "EmployerProfile",
        primaryjoin="User.telegram_id == EmployerProfile.created_by_admin_id",
        back_populates="creator_admin"
    )
    reported_complaints = relationship("Complaint", foreign_keys="[Complaint.reporter_user_id]", back_populates="reporter")
    complaints_against = relationship("Complaint", foreign_keys="[Complaint.reported_user_id]", back_populates="reported_user_object")

    def __repr__(self):
        return f"<User(telegram_id={self.telegram_id}, role={self.role})>"

class ApplicantProfile(Base):
    __tablename__ = "applicant_profiles"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(BigInteger, ForeignKey("users.telegram_id", name="fk_applicantprofile_user_id", ondelete="CASCADE"), unique=True, nullable=False)
    city = Column(String(100), nullable=False)
    gender = Column(SQLAlchemyEnum(GenderEnum), nullable=False)
    age = Column(Integer, nullable=False)
    experience = Column(Text, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    deactivation_date = Column(DateTime(timezone=True), nullable=True)

    user = relationship("User", back_populates="applicant_profile")

    def __repr__(self):
        return f"<ApplicantProfile(user_id={self.user_id}, city='{self.city}')>"

class EmployerProfile(Base):
    __tablename__ = "employer_profiles"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(BigInteger, ForeignKey("users.telegram_id", name="fk_employerprofile_user_id", ondelete="CASCADE"), unique=True, nullable=True) 
    company_name = Column(String(200), nullable=False)
    city = Column(String(100), nullable=False)
    position = Column(String(150), nullable=False)
    salary = Column(String(100), nullable=True)
    min_age_candidate = Column(Integer, nullable=True)
    description = Column(Text, nullable=False)
    work_format = Column(SQLAlchemyEnum(WorkFormatEnum), nullable=False)
    photo_file_id = Column(String, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    active_notification_message_id = Column(BigInteger, nullable=True)
    is_dummy = Column(Boolean, nullable=False, default=False, server_default=sa.false())
    created_by_admin_id = Column(BigInteger, ForeignKey("users.telegram_id", name="fk_employerprofile_created_by_admin_id", ondelete="SET NULL"), nullable=True, index=True)
    deactivation_date = Column(DateTime(timezone=True), nullable=True)

    user_owner = relationship(
        "User", 
        back_populates="employer_profile",
        foreign_keys=[user_id]
    )

    creator_admin = relationship(
        "User",
        back_populates="created_dummy_profiles",
        foreign_keys=[created_by_admin_id]
    )

    def __repr__(self):
        return f"<EmployerProfile(id={self.id}, company_name='{self.company_name}')>"
    
class ApplicantEmployerInteraction(Base):
    __tablename__ = "applicant_employer_interactions"
    id = Column(Integer, primary_key=True, index=True)
    applicant_user_id = Column(BigInteger, ForeignKey("users.telegram_id", name="fk_interaction_applicant_id", ondelete="CASCADE"), nullable=False, index=True)
    employer_profile_id = Column(Integer, ForeignKey("employer_profiles.id", name="fk_interaction_employer_profile_id", ondelete="CASCADE"), nullable=False, index=True)
    interaction_type = Column(SQLAlchemyEnum(InteractionTypeEnum), nullable=False)
    question_text = Column(Text, nullable=True) 
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    cooldown_until = Column(DateTime(timezone=True), nullable=True, index=True)
    is_viewed_by_employer = Column(Boolean, default=False, nullable=False)
    __table_args__ = (Index('ix_applicant_cooldown', 'applicant_user_id', 'cooldown_until'),)
    def __repr__(self):
        return f"<Interaction(applicant={self.applicant_user_id} -> profile={self.employer_profile_id}, type={self.interaction_type})>"
    
class Complaint(Base):
    __tablename__ = "complaints"
    id = Column(Integer, primary_key=True, index=True)
    reporter_user_id = Column(BigInteger, ForeignKey("users.telegram_id", name="fk_complaint_reporter_id", ondelete="SET NULL"), nullable=True, index=True) 
    reported_employer_profile_id = Column(Integer, ForeignKey("employer_profiles.id", name="fk_complaint_emp_profile_id", ondelete="CASCADE"), nullable=True, index=True)
    reported_applicant_profile_id = Column(Integer, ForeignKey("applicant_profiles.id", name="fk_complaint_app_profile_id", ondelete="CASCADE"), nullable=True, index=True)
    reported_user_id = Column(BigInteger, ForeignKey("users.telegram_id", name="fk_complaint_reported_user_id", ondelete="SET NULL"), nullable=True, index=True)
    reason_text = Column(Text, nullable=True)
    status = Column(SQLAlchemyEnum(ComplaintStatusEnum), default=ComplaintStatusEnum.NEW, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    reporter = relationship("User", foreign_keys=[reporter_user_id], back_populates="reported_complaints")
    reported_user_object = relationship("User", foreign_keys=[reported_user_id], back_populates="complaints_against")


    def __repr__(self):
        return f"<Complaint(id={self.id}, reporter={self.reporter_user_id}, status='{self.status.name}')>"

class BotSettings(Base):
    __tablename__ = "bot_settings"
    setting_key = Column(String, primary_key=True)
    value_str = Column(Text, nullable=True)
    value_int = Column(Integer, nullable=True)
    
class MotivationalContentTypeEnum(enum.Enum):
    VIDEO = "video"
    PHOTO = "photo"
    TEXT_ONLY = "text_only"

class MotivationalContent(Base):
    __tablename__ = "motivational_content"

    id = Column(Integer, primary_key=True, index=True)
    content_type = Column(SQLAlchemyEnum(MotivationalContentTypeEnum), nullable=False)
    file_id = Column(String, nullable=True)
    text_caption = Column(Text, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    usage_count = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self):
        return f"<MotivationalContent(id={self.id}, type='{self.content_type.name}', active={self.is_active})>"

class ReferralLink(Base):
    __tablename__ = "referral_links"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(50), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=False)
    creator_admin_id = Column(BigInteger, ForeignKey("users.telegram_id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    usages = relationship("ReferralUsage", back_populates="link", cascade="all, delete-orphan")
    creator_admin = relationship("User")

    def __repr__(self):
        return f"<ReferralLink(id={self.id}, code='{self.code}', name='{self.name}')>"

class ReferralUsage(Base):
    __tablename__ = "referral_usages"

    id = Column(Integer, primary_key=True, index=True)
    link_id = Column(Integer, ForeignKey("referral_links.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False, index=True)
    used_at = Column(DateTime(timezone=True), server_default=func.now())
    link = relationship("ReferralLink", back_populates="usages")
    user = relationship("User")

    def __repr__(self):
        return f"<ReferralUsage(user_id={self.user_id}, link_id={self.link_id})>"
