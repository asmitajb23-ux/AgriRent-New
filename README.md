# AgriRent

AgriRent is a beginner-friendly Flask application for tractor and agricultural equipment rentals. It uses server-rendered HTML, SQLite through Python's built-in `sqlite3` module, and Tailwind CSS from its CDN for responsive page styling.

## Project structure

```text
AgriRent/
|-- app.py
|-- database.py
|-- requirements.txt
|-- README.md
|-- instance/
|   `-- agrirent.sqlite      Created automatically on first run
`-- templates/
    |-- base.html
    |-- index.html
    |-- register.html
    |-- login.html
    |-- tractors.html
    |-- tractor_details.html
    |-- add_tractor.html
    |-- availability.html
    |-- booking.html
    |-- booking_details.html
    |-- owner_dashboard.html
    |-- customer_dashboard.html
    |-- booking_requests.html
    |-- booking_history.html
    |-- customer_history.html
    |-- customer_details.html
    |-- payment.html
    |-- payment_history.html
    |-- earnings.html
    |-- about.html
    |-- contact.html
    |-- 404.html
    `-- error.html
```

## Run locally in VS Code on Windows

1. In VS Code, choose **File > Open Folder...** and open this project folder (it may be named `AgriRent` or `AgriRent_New`).
2. Open **Terminal > New Terminal**. Confirm the terminal is in the project folder.
3. Create a virtual environment:

   ```powershell
   py -m venv .venv
   ```

4. Activate it in PowerShell:

   ```powershell
   .\.venv\Scripts\Activate.ps1
   ```

   If PowerShell blocks activation, either use the VS Code Command Prompt terminal and run `.\.venv\Scripts\activate.bat`, or allow scripts for the current PowerShell user according to your organization's policy.

5. Install Flask:

   ```powershell
   python -m pip install -r requirements.txt
   ```

6. (Recommended) Set a stable random secret key for the local session. Generate a value privately and keep it out of source control:

   ```powershell
   $env:AGRIRENT_SECRET_KEY = "replace-this-with-a-long-random-secret"
   ```

   For a local demo only, the application otherwise generates a new random key at each start, which logs out sessions when the process restarts.

7. Initialize the SQLite database and start the app:

   ```powershell
   python app.py
   ```

   `database.py` creates `instance\agrirent.sqlite` and all required tables and indexes automatically when the application starts. There is no separate migration command for a new local installation.

8. Open <http://127.0.0.1:5000> in a browser. Stop the server with **Ctrl+C** in the terminal.

The design loads Tailwind CSS from its CDN and the Inter font from Google Fonts, so styled pages need internet access. Booking and account behavior still runs locally in Flask if external design assets are unavailable. The CDN is convenient for this learning project; for a production deployment, compile Tailwind into a static stylesheet instead.

The listing UI shows **Not rated yet** rather than inventing customer reviews; the database does not currently collect ratings. Prices use the app's supported hourly and per-acre rates rather than showing a daily rate that owners have not configured.

Optional: run with Flask's development debugger by setting `$env:FLASK_DEBUG = "1"` before `python app.py`. Do not enable debug mode on a public server.

## Test both roles and the booking workflow

Use separate browser profiles or log out and back in to switch roles.

### Stage 1 — Authentication

1. Register one **Tractor owner** and one **Customer / farmer**, each with a different email and a password of at least eight characters.
2. Log in as each user. The owner and customer dashboards are role-restricted.
3. Log out and verify that a private dashboard sends you to login.

Passwords are stored as Werkzeug password hashes. POST forms require a session-bound CSRF token, and database values are passed using SQLite parameter placeholders.

### Stage 2 — Tractor listings and equipment

1. As owner, choose **Add a tractor listing**.
2. Enter the tractor details, service area, contact number, photo URL (optional), services and hourly and/or per-acre rates.
3. Add optional equipment using one line per item in the form `Rotavator | 800` or `Trailer | 500`.
4. Save the listing, edit it, and check it from the public tractor search. Removing a listing hides it from new searches without deleting historical bookings.

### Stage 3 — Availability

1. From the owner dashboard, choose **Availability** for the listing.
2. Publish a future date and start/end time.
3. As customer, open the tractor and choose **Request this tractor**. Choose the same date and confirm the published slot is offered.
4. Requests outside a published slot, in the past, or outside the slot's hours are rejected by the server.

### Stage 4 — Booking and pricing

1. As customer, select hourly, per-acre, or combined pricing; enter the acres where required; select optional equipment; choose a service and payment agreement.
2. Choose **Calculate price quote** to see rental, equipment, total and advance amounts calculated by Python. Then submit the request with that signed quote. If a rate or any work detail changes after quoting, calculate a new quote.
3. The server saves the rates, equipment names/charges, total and advance with the booking, so later price edits do not change old quotes.
4. As owner, open **Booking requests**, inspect the booking and accept or reject it.
5. Request overlaps are checked again when the owner accepts. Accepted/confirmed bookings reserve that time on the tractor.

### Stage 5 — Payment tracking and work status

1. For an advance or full-advance agreement, the customer submits payment after owner acceptance. For an after-work agreement, the booking is confirmed when accepted.
2. Cash, UPI and **Demo online** submissions are all marked pending. The demo option does not charge money. Never enter a card number or UPI PIN.
3. As owner, review a booking and verify or reject submitted payments. Only verified amounts count toward paid totals. The booking becomes confirmed after its agreed advance is verified.
4. Mark confirmed work **In Progress**, then **Completed**. A customer may submit the remaining balance after completion.
5. Review payment records and balances in **Payments** and **Earnings**.

### Stage 6 — Customer history and dashboards

As owner, open **Customers** to see owner-specific customer lists and detailed booking/payment histories. A customer with three or more completed jobs is Regular; a previously booked customer with a most recent booking older than 90 days is Inactive; otherwise they are Repeat. New customers have no prior booking with that owner. Only completed jobs contribute to completed-job, acre and completed-charge totals. Customers can see only their own dashboard, bookings and payment history.

## Database details

The schema includes `users`, `tractor_listings`, `equipment`, `availability`, `bookings`, `booking_equipment`, and `payments`. Foreign keys are enabled on each SQLite connection. Booking totals and payment amounts are stored as integer paise to avoid currency rounding drift; rates are snapshotted on each booking.

To initialize from Python without starting the web server, activate the virtual environment and run:

```powershell
python -c "from app import app; from database import init_db; app.app_context().push(); init_db(); print('Database initialized')"
```

## Mobile JSON API

The browser routes continue to use Flask sessions and HTML forms. Mobile clients can use the separate JSON endpoints:

- `POST /api/login` — JSON body `{"email":"farmer@example.com","password":"your-password"}`. Returns a short-lived bearer token and public account details.
- `GET /api/tractors` — public tractor JSON. Optional filters: `q`, `location`, `type`, and `work_date` (`YYYY-MM-DD`). Use `GET /api/tractors/<id>` for one listing.
- `POST /api/book` — customer bearer token and JSON booking fields: `listing_id`, `work_date`, `start_time`, `end_time`, `rental_mode` (`hourly`, `acre`, `combined`), `acres`, `payment_plan` (`advance`, `full_advance`, `after_work`), optional `equipment_ids` (array of IDs), `service`, and `notes`. The server validates availability and calculates the saved price.
- `GET /api/my-bookings` — customer bearer token; returns only the authenticated customer's bookings.

After login, send `Authorization: Bearer <access_token>` on the two authenticated endpoints. Send JSON requests with `Content-Type: application/json`. Errors use a JSON `error` field and an appropriate HTTP status. API tokens expire after 24 hours by default (`API_TOKEN_MAX_AGE` can be configured). Configure allowed browser origins using the comma-separated `AGRIRENT_CORS_ORIGINS` environment variable; the default `*` is convenient for local development. CORS does not provide authentication or encryption: public mobile deployments must use HTTPS and a specific trusted origin list where browser clients are involved.

Example booking JSON:

```json
{
  "listing_id": 1,
  "work_date": "2027-01-15",
  "start_time": "09:00",
  "end_time": "11:00",
  "rental_mode": "hourly",
  "acres": 0,
  "payment_plan": "advance",
  "equipment_ids": [2],
  "service": "Ploughing",
  "notes": "North field"
}
```

## Troubleshooting

- **`py` or `python` is not recognized:** install Python 3 from python.org, enable the option to add it to PATH, restart VS Code, then retry.
- **Flask or Flask-CORS is not found:** activate `.venv` and run `python -m pip install -r requirements.txt` again.
- **PowerShell will not activate `.venv`:** use the VS Code Command Prompt terminal and `.\.venv\Scripts\activate.bat`, or follow your organization's approved PowerShell execution policy.
- **Port 5000 is in use:** stop the other local Flask process, or change the `app.run` port in `app.py` to an unused local port.
- **Lost local demo data:** it is stored in `instance\agrirent.sqlite`. Back up that file before removing it. The app creates a fresh database automatically if it is missing.
- **Login sessions end after restart:** set a stable `AGRIRENT_SECRET_KEY` in the terminal before starting the app.
- **An image does not load:** listing photos and the homepage illustrations use image URLs and require an internet connection. Listing text and bookings still work without those images.

## Before public or Android use

This is a local learning/demo application, not a production payment service. For public deployment, use HTTPS, a stable secret stored outside the source tree, secure session-cookie settings, a production WSGI server, backups, privacy controls and deployment-specific validation. Cash/UPI receipts require owner verification; the demo online option never contacts a payment provider. Do not represent any submission as a real online payment.

Do not expose the development server or deploy this demo publicly as-is.
