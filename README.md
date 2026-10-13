# Employee Attendance & Analytics API

FastAPI and MongoDB implementation for employee attendance, manual corrections, and monthly analytics.

## Run locally

Requires Python 3.11+ and MongoDB 6.0+. From this directory, install dependencies with `python -m pip install -r requirements.txt`, set `MONGO_URI` and `MONGO_DB` in your environment (or a local, uncommitted `.env` file), then run `uvicorn app.main:app --port 8000`.

The API creates its indexes at startup. Open `http://localhost:8000/docs` for the interactive API documentation.
