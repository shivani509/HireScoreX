# HireScoreX Final

HireScoreX Final is a polished Flask-based hiring intelligence app built for strong project demos, resume showcases, and internship interviews. It lets a candidate upload a resume, compare it against a job description, review section-wise fit scores, get AI-style feedback, practice interview questions, download reports, and view recruiter-style rankings.

## What is included
- premium login and register UI
- candidate dashboard with charts
- resume vs JD scoring
- ATS keyword gap detection
- AI-ready summaries with fallback mode
- interview question generation
- history tracking
- recruiter ranking dashboard
- PDF report generation
- JSON endpoints for analysis data
- CSV export for recruiter filtering
- VS Code launch support
- Docker, Procfile, and PostgreSQL-ready setup

## Demo accounts
- Candidate: `demo@hirescorex.com`
- Recruiter: `recruiter@hirescorex.com`
- Password: `demo1234`

## Quick start
```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:5000`

## PostgreSQL setup
Set environment variables before running:
```bash
SECRET_KEY=change-me
DATABASE_URL=postgresql://postgres:password@localhost:5432/hirescorex
OPENAI_API_KEY=your_key_here
OPENAI_MODEL=gpt-4.1-mini
```

## Docker run
```bash
docker compose up --build
```

## Useful routes
- `/login`
- `/dashboard`
- `/analyze`
- `/history`
- `/recruiter`
- `/healthz`
- `/api/dashboard/summary`
- `/api/analysis/<id>`
- `/recruiter/export.csv`

## Honest note
This is a strong final project version for demos and portfolio use. It is not a giant enterprise platform with microservices, queues, RBAC, and cloud infra, but it is much closer to a production-style project than a basic college app.
