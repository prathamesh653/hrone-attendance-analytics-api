# Candidate kit

Read in this order:

1. `PROBLEM_STATEMENT.docx`: what you are asked to do, how it is evaluated.
2. `openapi.yaml`: the API contract and the business rules (R1 - R10).
3. `DATA_MODEL.md`: what is stored in MongoDB, with nine sample documents.
4. `app/main.py`: the starter. Put **all** your code in this one file. It must start with `uvicorn app.main:app --port 8000`.
5. `REVIEW.md`, `DECISIONS.md`: templates to fill in.

`sample_data/` holds the nine sample documents as MongoDB Extended JSON; `sample_seed.py` loads them. The grader's dataset is
much larger and contains cases they do not show.

## Local setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # set MONGO_URI / MONGO_DB (local docker: docker run -p 27017:27017 mongo:7)
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```
MongoDB **6.0 or newer** is required (the grader uses 7.0). MongoDB Atlas free tier (M0) works.

## Submit
A public Git repository with `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md` and a short `README.md`.
No `.env`, no secrets, no Dockerfile.

# Employee Attendance & Analytics API

## Overview

A REST API built with Python, FastAPI, and MongoDB to manage employee records, attendance, attendance corrections, and workforce analytics.

## Technology Stack

- Python 3.11+
- FastAPI
- MongoDB 6.0+
- Uvicorn
- PyMongo

## Features

- Health-check endpoint
- Employee creation and listing
- Employee attendance punch-in and punch-out
- Attendance listing with filtering and pagination
- Attendance regularization with an audit history
- Monthly employee attendance analytics
- Department-level attendance summaries
- Late-arrival leaderboard and department trend analytics
- Endpoint explanation endpoint

## Prerequisites

- Python 3.11 or newer
- MongoDB 6.0 or newer
- pip

## Setup and Run

1. Clone or download this repository.
2. Open the project directory in a terminal.
3. Create and activate a Python virtual environment.
4. Install the dependencies:

   `pip install -r requirements.txt`

5. Configure the MongoDB connection using the settings expected by the application.
6. Start the API:

   `python -m uvicorn app.main:app --port 8000`

## API Documentation

When the server is running, open:

- Swagger UI: http://127.0.0.1:8000/docs
- OpenAPI schema: http://127.0.0.1:8000/openapi.json

## Main Endpoints

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/health` | Health check |
| POST | `/employees` | Create an employee |
| GET | `/employees` | List employees |
| POST | `/attendance/punch-in` | Record punch-in |
| POST | `/attendance/punch-out` | Record punch-out |
| GET | `/attendance` | List attendance records |
| PATCH | `/attendance/{emp_code}/{date}` | Regularize attendance |
| GET | `/analytics/employees/{emp_code}/monthly` | Monthly employee analytics |
| GET | `/analytics/departments/summary` | Department summary |
| GET | `/analytics/leaderboard/late` | Late-arrival leaderboard |
| GET | `/analytics/departments/{department}/trend` | Department trends |
| GET | `/admin/explain/{endpoint}` | Explain an endpoint |

## Testing

The API has been manually tested using Swagger UI. The health check, employee operations, attendance operations, regularization, and analytics endpoints have been exercised. Additional automated and edge-case testing should be documented according to the actual test results.

## Design and Review Notes

See `REVIEW.md` and `DECISIONS.md` for code-review findings and design decisions.

## Security

Do not commit credentials, API keys, database passwords, or `.env` files to the public repository.


