# Violation Management Web Application

This project converts the original macros‑enabled Excel workbook into a
web‑based management system.  It is designed to be easy to deploy on
standard web hosting and preserves the key features of the original file.

## Features

* **User authentication** – students can register with a display name,
  unique username and email address.  Passwords are hashed for security.
* **Session cookies** – after login a cookie remembers the session so
  repeated logins are unnecessary.
* **Summary view** – displays all violation records imported from the
  workbook.  Filters allow you to narrow results by week, day, student
  name and error code.  Anonymous users must select a single week.
* **Complaints** – logged‑in users may file a complaint against any
  record.  Complaints capture the error code, the email of the
  complainant and a free‑form message.  Administrators can review and
  resolve complaints from the admin panel.
* **Payments** – each user can view their outstanding balance and see a
  dynamically generated transfer description (``Họ và tên + số tiền + mã lỗi``).
  A QR code is generated automatically for convenience and step‑by‑step
  instructions from the original workbook are displayed.
* **History** – after making payments administrators can record the
  transaction using the admin panel and students can view their
  personal payment history.
* **Admin dashboard** – administrators can view and resolve complaints.
  Extending the dashboard to edit violation records, create new error
  codes or adjust payments is straightforward and left as an exercise.

## Installing dependencies

This application relies on a handful of Python packages.  They can be
installed with pip:

```bash
pip install -r requirements.txt
```

The ``qrcode`` dependency automatically brings in Pillow.  If you
encounter issues generating QR codes you can remove that dependency and
the system will fallback to displaying a simple placeholder image.

## Initialising the database

On the first run the application will create an SQLite database file
(``database.db``) in the ``violation_web`` folder.  It will then
import all relevant data from the Excel workbook located one level above
the project directory (``Danh Sách Vi Phạm .xlsm``).  The import
happens only once – subsequent runs will reuse the existing database.

## Running the application

Execute the following command in the ``violation_web`` directory:

```bash
python app.py
```

The server will listen on `http://localhost:5000`.  Navigate there in
your browser and you should see the homepage.  From there you can
register, log in and explore the features.  Administrators can log in
with an account for which the ``is_admin`` flag is set in the database.

## System architecture

The application follows a classic three‑tier architecture:

* **Presentation layer** – HTML templates in the ``templates`` directory,
  styled with a minimal amount of inline CSS.  JavaScript is used only
  to support copying the transfer message to the clipboard on the
  payment page.
* **Application layer** – the Flask application in ``app.py`` defines
  routes, manages sessions and orchestrates interactions between the
  user interface and the data model.  Helper functions in
  ``utils.py`` handle Excel import, formatting and QR code generation.
* **Data layer** – SQLAlchemy models encapsulate users, violation
  records, payments, complaints and error codes.  Data is persisted
  to ``database.db``; the original workbook is only consulted when
  populating the database for the first time.

The diagram below summarises the high‑level design (see file
``system_diagram.png``).
