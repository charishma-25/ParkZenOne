# ParkZen

ParkZen is a smart car-parking availability application. Parking managers upload a current image, review detections from the bundled YOLO model, and confirm the count. Customers search destinations and see the latest confirmed update with a Google Maps directions link.


## Architecture

- React 19 and Vite frontend (`src/`)
- Flask JSON API and SQLite persistence (`backend/app.py`)
- Bundled Ultralytics YOLO weights (`backend/best.pt`)
- SQLite database and manager uploads are created under `backend/` at runtime and excluded from Git.

The six included destination records are sample locations in Hyderabad. Replace their details in `DESTINATIONS` in `backend/app.py` with verified venue and parking data before production use. Each destination is seeded with 2â€“5 parking levels in the `parking_levels` table. No initial parking counts are inserted. Customer availability appears only after a manager confirms an analysis for a level.

## Run locally

1. Install frontend packages with `npm install`.
2. Install backend dependencies with `python -m pip install -r backend/requirements.txt`.
3. Start the API from the repository root: `python backend/app.py`.
4. In a second terminal run `npm run dev` and open the Vite URL.

The API defaults to `http://127.0.0.1:5000`; set `VITE_API_URL` to change the frontend API origin. The API allows the Vite origin `http://localhost:5173` by default; use `PARKZEN_ORIGIN` for another origin.

On first startup a manager account is created and assigned to Mall A:

- Email: `manager@parkzen.local`
- Password: `1234`

Set `PARKZEN_MANAGER_PASSWORD` before first startup to choose the initial password. Change it before deployment. Passwords are stored as Werkzeug hashes; sessions use random bearer tokens stored in SQLite.

An administrator account is also created on first startup (`admin@parkzen.local`, initial password `1234`). Set `PARKZEN_ADMIN_PASSWORD` before first startup and change it before deployment. The admin interface provides account-directory, manager assignment, and live availability summaries; it does not permit arbitrary account role changes or destination record edits.

Sign in as the administrator, open **Accounts**, and create one manager account for each of the six destinations. The location assignment screen shows every destination and its current manager; changing an assignment removes the manager's previous location before assigning the new one. Manager accounts cannot choose a destination in the frontend, and the API derives the destination for analysis and confirmation from the authenticated account.

## API overview

- `POST /api/auth/register`, `POST /api/auth/login`, `GET /api/auth/me`, `POST /api/auth/logout`
- `GET /api/destinations?q=&type=all|mall|restaurant`, `GET /api/destinations/<id>` (includes level-wise availability)
- `GET /api/history`, `GET/PATCH /api/profile`
- Customer loyalty: `GET /api/loyalty`; customer booking: `GET /api/bookings`, `POST /api/bookings/hold`, `POST /api/bookings/<id>/verify-payment`
- Customer overstays: `GET /api/overstays`, `POST /api/overstays/<id>/payment-order`, `POST /api/overstays/<id>/verify-payment`
- Admin only: `GET /api/admin/loyalty` for loyalty tier and earned-discount summaries.
- Manager only: `GET /api/manager/levels`, `POST /api/manager/analyze` (multipart image and `level_id`; location comes from the authenticated manager), `POST /api/manager/confirm/<prediction_id>`, `GET /api/manager/predictions`
- Manager only: `GET /api/manager/overstays` for cases assigned to the manager's location

Each parking level starts with a persistent 4-by-10 visual slot layout in `parking_slots` and expands when a model result reports more spaces than the current layout. Slot identifiers use floor prefixes such as `B201`, `B101`, `G101`, and `L101`. A confirmed model result maps its bounding-box centers to the nearest persistent slots for that level; matched slots are marked available or occupied, while slots without a reliable detection remain unknown. Customer destination details expose the complete level slot map and the UI allows selecting only green available slots for inspection; this is visual identification, not a reservation.
- `GET /api/health`

The model's class 0 detections are treated as available car slots and class 1 as occupied car slots, matching the bundled model labels. Other classes are ignored. The included parking photographs are sample assets and are not automatically presented as live availability.

## Customer pre-booking and Razorpay sandbox

Customers can select a green slot, vehicle type, and a duration from 30-minute blocks, then use **My bookings** to review confirmed reservations. Location pricing is stored in `location_pricing` and seeded at â‚¹5/â‚¹8/â‚¹6/â‚¹10/â‚¹7/â‚¹9 per 30-minute block for Mall A, Mall B, Mall C, Restaurant A, Restaurant B, and Restaurant C respectively. The backend calculates the amount; frontend amounts are informational only.

Configure Razorpay test credentials before starting the backend:

```powershell
$env:RAZORPAY_KEY_ID="rzp_test_..."
$env:RAZORPAY_KEY_SECRET="..."
python backend\app.py
```

The backend creates a Razorpay order, holds the selected slot for the configured payment window (10 minutes by default), and confirms the booking only after verifying the Razorpay payment signature server-side. It never stores card details, CVV, UPI PINs, or payment passwords. If the sandbox keys are missing, booking creation fails explicitly and no booking hold is persisted.

Booking times are stored as UTC timestamps and displayed in Asia/Kolkata by default. Configure `PARKZEN_TIMEZONE` to change the display/input zone, `PARKZEN_MAX_BOOKING_HOURS` (default 24) and `PARKZEN_MAX_BOOKING_DAYS` (default 30) to change booking limits, `PARKZEN_PAYMENT_HOLD_MINUTES` (default 10) to change the checkout hold, and `PARKZEN_OCCUPANCY_MAX_AGE_MINUTES` (default 60) to set how recent a slot's confirmed availability must be before it can be reserved.

## Production notes

This repository is a local application baseline, not a hardened hosted deployment. Before public deployment, configure HTTPS, a strong initial manager password, an appropriate session expiration/rotation policy, production CORS origins, database backups, and verified destination records. Account recovery is not implemented because no email delivery provider is configured.

## Parking overstays and penalty payments

The backend creates one overstay case per confirmed booking after its stored expiry timestamp. It estimates penalties at **?1 per billable minute** by default; any positive fraction of a minute rounds up. Configure `PARKZEN_OVERSTAY_RATE_PER_MINUTE` to change the integer rupees-per-minute rate and `PARKZEN_OVERSTAY_MONITOR_INTERVAL_SECONDS` (default 60, minimum 15) to change the backend expiry-check interval. Run `python backend\app.py` as a persistent backend process for expiry checks to continue while customers have closed their browsers.

ParkZen's current YOLO workflow processes manager-uploaded images only; it does not ingest a CCTV stream. The background worker can mark expired bookings as awaiting departure and update estimates, but it cannot determine a vehicle's departure by itself. A manager must upload an image for the relevant level and confirm its analysis. A fresh confirmed occupied result keeps the case open and the slot blocked; a fresh confirmed available result records the ML analysis time for the confirmed image as departure evidence, calculates the final penalty from the booking's stored expiry, and allows the slot to become reservable only while availability remains fresh. Missing or stale detection stays unconfirmed. Detection history is linked to the case and manager location access is enforced by the API.

Customers can see estimates and final charges on the dashboard and **My bookings**. A penalty uses its own Razorpay order and payment record; ParkZen does not automatically debit a saved method. Configure the same `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET` test credentials described above. The customer can pay only after departure evidence finalizes a positive penalty, and the API records it as paid only after verifying the signature and captured transaction with Razorpay. The overstay notice includes a separate penalty receipt breakdown after verification.

## Customer account capacity and cleanup

Customer capacity defaults to 300 active customer accounts. Set `PARKZEN_MAX_CUSTOMER_ACCOUNTS` before starting the API to change it. Set `PARKZEN_INACTIVITY_DAYS` to change the default inactive-account filter (90 days). The admin **Accounts & capacity** page shows the capacity meter and alerts, supports searching and paging through inactive customers, and provides confirmed deactivation/reactivation. An account with an active booking or unresolved overstay penalty cannot be deactivated. Deletion is offered only when no booking or activity history must be retained and requires typing the customer's email.

Capacity checks run with the existing backend monitor while `python backend\\app.py` is running. Capacity warning events are always shown in the admin dashboard. Email is optional; configure all of the following to enable SMTP notifications:

```powershell
$env:PARKZEN_SMTP_HOST="smtp.example.com"
$env:PARKZEN_SMTP_PORT="587"
$env:PARKZEN_SMTP_USERNAME="parkzen@example.com" # optional for SMTP relay without auth
$env:PARKZEN_SMTP_PASSWORD="..."                # optional when username is omitted
$env:PARKZEN_ALERT_FROM="parkzen@example.com"
$env:PARKZEN_ADMIN_ALERT_EMAIL="admin@example.com"
python backend\\app.py
```

The monitor emits one alert per 80%, 95%, and 100% threshold crossing and rearms each threshold after usage falls below it. For local verification, run `python -m unittest backend.test_feature6` from the repository root. Manual checks: register up to the configured limit and verify the 80%, 95%, and full alerts; attempt two registrations simultaneously at the last account slot; review inactive customers using search and different inactivity periods; verify managers/admins never appear in cleanup; confirm active bookings and unresolved penalties block deactivation; deactivate a safe customer and confirm their existing token and login are rejected; reactivate them and confirm login works; and confirm the admin audit log records each action.

## Loyalty rewards and progressive parking discounts

ParkZen awards the highest qualifying customer tier: 20% after 30 days of regular use, 50% after six calendar months, and 80% after one calendar year. Registration age alone does not qualify. A qualifying day comes from a successful login or meaningful authenticated customer activity, recorded by the backend in the configured `PARKZEN_TIMEZONE`; only one day is counted per local calendar date. By default, each tier window requires at least one active day in each calendar week, with one missed week allowed. Configure the rule with `PARKZEN_LOYALTY_DAYS_PER_WEEK` (default `1`) and `PARKZEN_LOYALTY_MISSED_WEEKS` (default `1`). The `PARKZEN_TIMEZONE` setting controls both display and calendar-week boundaries.

Discounts use each location's configured `location_pricing` rate. Rates and final totals are rounded half-up to the nearest paise per 30-minute block; discounts do not stack. The backend saves the tier and full price breakdown with each booking, creates the Razorpay order in paise, and checks the captured payment amount against the saved booking total. A later tier change does not alter an existing hold or booking. Customers can see loyalty progress and savings on their dashboard/profile and a live price breakdown while selecting a booking. Admins can review customer tiers and earned savings under **Accounts & capacity**; activity dates cannot be edited in the admin UI.

The loyalty activity and booking-price migration is additive: it creates a per-customer daily activity table (seeding qualifying days from retained customer destination-view history) and adds loyalty quote/paise columns to `bookings` and a paise amount column to `payments`. Existing users, bookings, payment rows, and amounts are retained; legacy bookings are backfilled with their existing rate and total in paise and have no loyalty discount.

Run the backend and frontend using the existing local setup. To test loyalty and account-capacity flows, run `python -m unittest backend.test_feature6` from the repository root. Manual checks: use test accounts with one qualifying day per week to earn each tier; confirm repeated logins/actions on the same local date count as one day; check that irregular weeks and account age alone do not earn a tier; compare Mall A and Mall B rates; verify the 50% tier turns INR 5/block into INR 2.50 and the 80% tier into INR 1; change duration and location and confirm the preview updates; create a Razorpay test booking and check the amount, receipt, history, and saved booking breakdown; let a hold expire and confirm it cannot be paid; attempt two simultaneous holds for one slot; and review customer tiers as an admin while confirming the endpoint is denied to non-admins.
