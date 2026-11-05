from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Jinja2Templates(directory="Templates")
post1 = {
    "id": 1,
    "author": "Aertorias",
    "title": "FastAPI is Awesome",
    "content": "This framework is really easy to use and super fast.",
    "date_posted": "April 20, 2025",
}


Posts = [    
     {
        "id": 1,
        "author": "Aertorias",
        "title": "FastAPI is Awesome",
        "content": "This framework is really easy to use and super fast.",
        "date_posted": "April 20, 2025",
    },
    {
        "id": 2,
        "author": "Jane Doe",
        "title": "Python is Great for Web Development",
        "content": "Python is a great language for web development, and FastAPI makes it even better.",
        "date_posted": "April 21, 2025",
    },
]
@app.get("/", name = "home")
def read_root(request: Request):
    return templates.TemplateResponse(request, "home.html", {"posts": Posts, "title" : "Home"})

@app.get("/Posts")
def read_posts(request : Request):
    return Posts

@app.get("/Posts/{post_id}")
def read_post(post_id: int, request: Request):
    for post in Posts:
        if post["id"] == post_id:
            return templates.TemplateResponse(request, "post.html", {"post": post, "title": post["title"]})
    return {"error": "Post not found"}