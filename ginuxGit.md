"""
Ginux - A modern Git GUI built with Python
==========================================
Features:
    - Sign in to GitHub (Personal Access Token) and browse your repositories
    - Clone or open a local repository
    - "git add"    -> drag files from Changes to Staged
                                        (or drop files from Explorer/Finder into the app window)
    - "git commit" -> enter a commit message
    - "git push"   -> choose a remote and branch, then push (also pull, checkout,
                                        and create branches)

Install:
        pip install customtkinter tkinterdnd2 keyring
        (keyring is optional - it stores your token in the OS keychain)

Run:
        python gitpilot.py

Requirement: Git must be installed and available on PATH.
GitHub sign-in:
    Click "Sign in with GitHub" to open the GitHub sign-in window through Git
    Credential Manager, which is included with Git. Sign in with your account.
    Alternatively, an existing GitHub CLI session (`gh auth login`) will be used.
"""
