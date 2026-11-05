from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, EmailStr

app = FastAPI()

class Post(BaseModel):
    title: str = Field(..., max_length=100)
    content: str = Field(..., max_length=1000)
    author: str = Field(..., max_length=20)


class PostCreate(Post):
    pass