from violation_web.app import app, db, User
from werkzeug.security import generate_password_hash

with app.app_context():
    existing = User.query.filter_by(username="admin").first()

    if existing:
        print("User admin already exists!")
    else:
        admin = User(
            username="admin",
            display_name="Admin",
            email="admin@admin.com",
            password=generate_password_hash("Administrator111"),
            is_admin=True,
            is_super_admin=True
        )

        db.session.add(admin)
        db.session.commit()

        print("Super admin created successfully!")
