from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class UserBase(BaseModel):
    username: str = Field(min_length=1, max_length=50)
    email: EmailStr = Field(max_length=120)


class UserCreate(UserBase):
    password: str = Field(min_length=8, max_length=128)


class UserPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    image_file: str | None
    image_path: str


class UserPrivate(UserPublic):
    email: EmailStr


class UserUpdate(BaseModel):
    username: str | None = Field(default=None, min_length=1, max_length=50)
    email: EmailStr | None = Field(default=None, max_length=120)
    image_file: str | None = Field(default=None, min_length=1, max_length=200)


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)
    confirm_password: str = Field(min_length=8, max_length=128)


class PasswordReset(BaseModel):
    new_password: str = Field(min_length=8, max_length=128)
    confirm_password: str = Field(min_length=8, max_length=128)


class Token(BaseModel):
    access_token: str
    token_type: str


class PostBase(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=10000)
    summary: str | None = Field(default=None, max_length=240)
    category: str | None = Field(default=None, max_length=50)
    tags: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, tags: list[str]) -> list[str]:
        normalized = [tag.strip() for tag in tags if tag.strip()]
        if any(len(tag) > 40 for tag in normalized):
            raise ValueError("Tags must be 40 characters or fewer.")
        if len({tag.casefold() for tag in normalized}) != len(normalized):
            raise ValueError("Tags cannot contain duplicates.")
        return normalized


class PostCreate(PostBase):
    pass


class PostUpdate(PostBase):
    pass


class PostPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=100)
    content: str | None = Field(default=None, min_length=1, max_length=10000)
    summary: str | None = Field(default=None, max_length=240)
    category: str | None = Field(default=None, max_length=50)
    tags: list[str] | None = Field(default=None, max_length=10)

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, tags: list[str] | None) -> list[str] | None:
        if tags is None:
            return None
        normalized = [tag.strip() for tag in tags if tag.strip()]
        if any(len(tag) > 40 for tag in normalized):
            raise ValueError("Tags must be 40 characters or fewer.")
        if len({tag.casefold() for tag in normalized}) != len(normalized):
            raise ValueError("Tags cannot contain duplicates.")
        return normalized


class CategoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    slug: str


class TagResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    slug: str


class PostResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    content: str
    summary: str | None
    category: CategoryResponse | None
    tags: list[TagResponse]
    user_id: int
    date_posted: datetime
    author: UserPublic


UserResponse = UserPublic