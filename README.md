# Employee Attendance & Analytics API

A FastAPI application backed by MongoDB for employee management, attendance tracking, attendance regularization, and monthly analytics.

## Tech Stack

- Python 3.11+
- FastAPI
- MongoDB

## Setup

Create and activate a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Configure the following environment variables locally:

- `MONGO_URI` — MongoDB connection URI
- `MONGO_DB` — MongoDB database name

Do not commit credentials or `.env` files.

## Run the API

```powershell
uvicorn app.main:app --port 8000
```

When the server is running, interactive API documentation is available at `/docs`.

## Design and Review

See `DECISIONS.md` for implementation decisions and `REVIEW.md` for the code review.

## Submission

This repository contains the implementation and documentation for the Employee Attendance & Analytics API assignment.