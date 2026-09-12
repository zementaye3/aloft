import os
import subprocess
import sys

# Change to the backend directory
script_dir = os.path.dirname(os.path.abspath(__file__))
os.chdir(script_dir)

# Run the server
subprocess.run([sys.executable, "-m", "uvicorn", "app.main:app", "--reload"])
