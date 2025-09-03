# Cleverly Connected Meals (CCM)

Cleverly Connected Meals is a small Flask application to help teams organize shared lunches. Users can add recipes, propose meals for specific dates, join discussions, claim grocery or cooking duties and receive email notifications when things change.

Features
- User registration and login
- Recipe management (add, edit, upload image)
- Week-based calendar for proposing and joining meals
- Discussion threads per proposal with message notifications
- Grocery and cook claim functionality
- Admin UI to configure SMTP and site hostname

Repository
- Source: https://github.com/tmaiwald/ccm

Getting started
1. Create a virtualenv and install requirements: pip install -r requirements.txt
2. Initialize the database (see migrations folder) and run the app with: python run.py

Contributing
Contributions welcome — open an issue or PR on the GitHub repository.
