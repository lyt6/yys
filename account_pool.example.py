"""Copy this file to account_pool.py and fill local temporary accounts."""

ACCOUNT_POOL = [
    {
        "name": "temp_account_1",
        "username": "your_account",
        "password": "your_password",
        "profile_dir": "./browser_profiles/temp_account_1",
        # Optional. Defaults to the canonical equipment-list page.
        # "target_url": "https://yys.cbg.163.com/cgi/mweb/pl?view_loc=equip_list&tfid=f_kingkong",
        # Optional. All accounts can safely share the default SQLite database.
        # "database_path": "./data/cbg.sqlite3",
        "enabled": True,
    },
]

ACTIVE_ACCOUNT_INDEX = 0
