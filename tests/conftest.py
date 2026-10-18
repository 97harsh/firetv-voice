import os
import sys
import tempfile

# Hermetic: ignore any local install's config.json / apps.json and use the package defaults.
os.environ["FTV_DATA"] = os.path.join(tempfile.mkdtemp(), "FireTVVoice")
os.environ["FTV_LOGS"] = os.path.join(tempfile.mkdtemp(), "logs")

# Make the ftvoice package importable without installing it.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
