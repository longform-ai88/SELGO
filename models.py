from datetime import datetime
from sqlalchemy import Column, Integer, String, Float, DateTime, Boolean
from database import Base

class ItemDB(Base):
    __tablename__ = "items"
    id = Column(Integer, primary_key=True)
    title = Column(String)
    description = Column(String)
    price = Column(Float)
    location = Column(String)
    category = Column(String)
    image_url = Column(String)
    status = Column(String, default="active")
    listing_type = Column(String)
    listing_price_paid = Column(Float, default=0)
    listing_duration_days = Column(Integer, default=60)
    expires_at = Column(DateTime)
    is_featured = Column(Boolean, default=False)
    boost_selected = Column(Boolean, default=False)
    address = Column(String, nullable=True)
    seller_phone = Column(String, nullable=True)

class UserDB(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True)
    password = Column(String)
    full_name = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    email = Column(String, nullable=True)
    email_verified = Column(Boolean, default=False)
    email_token = Column(String, nullable=True)
    vipps_sub = Column(String, nullable=True)
    is_verified = Column(Boolean, default=False)
    is_free = Column(Boolean, default=False)
    seller_type = Column(String, default="privat")
    company_name = Column(String, nullable=True)

class ListingOwnerDB(Base):
    __tablename__ = "owners"
    id = Column(Integer, primary_key=True)
    item_id = Column(Integer)
    username = Column(String)


class ContactMessageDB(Base):
    __tablename__ = "contact_messages"
    id = Column(Integer, primary_key=True)
    item_id = Column(Integer)
    buyer_username = Column(String)
    seller_username = Column(String)
    message = Column(String)
    status = Column(String, default="sent")
    created_at = Column(DateTime, default=datetime.utcnow)


class PaymentOrderDB(Base):
    __tablename__ = "payment_orders"
    id = Column(Integer, primary_key=True)
    item_id = Column(Integer)
    buyer_username = Column(String)
    provider = Column(String)
    amount = Column(Float)
    currency = Column(String, default="NOK")
    status = Column(String, default="created")
    provider_reference = Column(String)
    listing_type = Column(String)
    listing_duration_days = Column(Integer)
    expires_at = Column(DateTime)
    item_price = Column(Float)
    created_at = Column(DateTime, default=datetime.utcnow)