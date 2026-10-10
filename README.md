# ParkZen

ParkZen is a smart car-parking availability application. Parking managers upload a current image, review detections from the bundled YOLO model, and confirm the count. Customers search destinations and see the latest confirmed update with a Google Maps directions link.

## Architecture

- React 19 and Vite frontend (`src/`)
- Flask JSON API and SQLite persistence (`backend/app.py`)
- Bundled Ultralytics YOLO weights (`backend/best.pt`)
- SQLite database and manager uploads are created under `backend/` at runtime and excluded from Git.

The six included destination records are sample locations in Hyderabad. Replace their details in `DESTINATIONS` in `backend/app.py` with verified venue and parking data before production use. No initial parking counts are inserted. Customer availability appears only after a manager confirms an analysis.

## Run locally

1. Install frontend packages with `npm install`.
2. Install backend dependencies with `python -m pip install -r backend/requirements.txt`.
3. Start the API from the repository root: `python backend/app.py`.
4. In a second terminal run `npm run dev` and open the Vite URL.

The API defaults to `http://127.0.0.1:5000`; set `VITE_API_URL` to change the frontend API origin. The API allows the Vite origin `http://localhost:5173` by default; use `PARKZEN_ORIGIN` for another origin.

On first startup a manager account is created:

- Email: `manager@parkzen.local`
- Password: `1234`

Set `PARKZEN_MANAGER_PASSWORD` before first startup to choose the initial password. Change it before deployment. Passwords are stored as Werkzeug hashes; sessions use random bearer tokens stored in SQLite.

An administrator account is also created on first startup (`admin@parkzen.local`, initial password `1234`). Set `PARKZEN_ADMIN_PASSWORD` before first startup and change it before deployment. The admin interface provides account-directory and live availability summaries; it does not permit account role changes or destination edits.

## API overview

- `POST /api/auth/register`, `POST /api/auth/login`, `GET /api/auth/me`, `POST /api/auth/logout`
- `GET /api/destinations?q=&type=all|mall|restaurant`, `GET /api/destinations/<id>`
- `GET /api/history`, `GET/PATCH /api/profile`
- Manager only: `POST /api/manager/analyze` (multipart image + destination ID), `POST /api/manager/confirm/<prediction_id>`, `GET /api/manager/predictions`
- `GET /api/health`

The model's class 0 detections are treated as available car slots and class 1 as occupied car slots, matching the bundled model labels. Other classes are ignored. The included parking photographs are sample assets and are not automatically presented as live availability.

## Production notes

This repository is a local application baseline, not a hardened hosted deployment. Before public deployment, configure HTTPS, a strong initial manager password, an appropriate session expiration/rotation policy, production CORS origins, database backups, and verified destination records. Account recovery is not implemented because no email delivery provider is configured.
