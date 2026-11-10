from fastapi import FastAPI
from pydantic import AliasPath, BaseModel, ConfigDict, Field, EmailStr
from datetime import datetime

app = FastAPI()

class UserBase(BaseModel):
    username: str = Field(..., max_length=50)
    email: EmailStr = Field(..., max_length=120)

class UserCreate(UserBase):
    pass

class UserResponse(UserBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    image_file: str | None = None

class Post(BaseModel):
    author: str = Field(..., max_length=50)
    title: str = Field(..., max_length=100)
    content: str = Field(..., max_length=1000)


class PostCreate(Post):
    pass

class PostResponse(Post):
    model_config = ConfigDict(from_attributes=True)
    author: str = Field(validation_alias=AliasPath("author", "username"))
    id: int
    date_posted: datetime