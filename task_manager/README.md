# Task Manager

Django REST Framework + PostgreSQL + Redis + Celery, run with Docker. Plain HTML/CSS/JS frontend.

## Run with Docker

```powershell
cd D:\Practice\task_manager
docker compose up --build
```

Open http://localhost:8000. Sign up, then create and assign tasks.

Optional admin user:

```powershell
docker compose exec web python manage.py createsuperuser
```

## Local virtual environment (for your editor / autocomplete)

Docker installs the packages inside the containers. A local venv is only needed so VS Code/PyCharm can resolve imports:

```powershell
cd D:\Practice\task_manager
python -m venv env
env\Scripts\activate
pip install -r requirements.txt
```

## Services

| Service | What it does | Port |
|---|---|---|
| `web` | Django + DRF API and HTML pages | 8000 |
| `worker` | Celery worker (sends the "task assigned" notification) | – |
| `db` | PostgreSQL 16 | 5432 |
| `redis` | Cache (user list) + Celery broker/result backend | 6379 |

## API

All endpoints except signup/login need the header `Authorization: Token <token>`.

| Method | URL | Who |
|---|---|---|
| POST | `/api/auth/signup/` | anyone → returns token |
| POST | `/api/auth/login/` | anyone → returns token |
| POST | `/api/auth/logout/` | logged in |
| GET | `/api/auth/me/` | logged in |
| GET | `/api/auth/users/` | logged in (cached in Redis) |
| GET | `/api/tasks/` | tasks I created + tasks assigned to me |
| POST | `/api/tasks/` | logged in |
| GET/PUT/PATCH | `/api/tasks/{id}/` | creator or assignee |
| DELETE | `/api/tasks/{id}/` | creator only |

Rules: only the creator can delete a task or change `assigned_to`. The assignee can edit name and description.

## Request flow (useful for tracing later)

```
Browser ──HTTP──> Django (web)
                    ├── Postgres  (read/write tasks, users, tokens)
                    ├── Redis     (cached user list)
                    └── Redis     (Celery broker) ──> Celery worker ──> Postgres
```
